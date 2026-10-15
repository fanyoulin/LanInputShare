import asyncio

import pytest

import lan_input_share.runtime.service as service_module
from lan_input_share.config.models import AppConfig, PeerTarget
from lan_input_share.config.paths import AppPaths
from lan_input_share.core.models import (
    DeviceInfo,
    DeviceRole,
    ScreenInfo,
    ScreenPosition,
)
from lan_input_share.network.connection import SessionConnectError
from lan_input_share.network.session import CloseReason, SessionState
from lan_input_share.runtime.service import (
    NetworkRoleService,
    build_role_network_plan,
    reachable_connected_positions,
)
from lan_input_share.security.pairing import PairingCoordinator


class _FakeControllerSession:
    def __init__(
        self, state: SessionState, *, device_id: str = "controller-id"
    ) -> None:
        self.state = state
        self.session_id = "session"
        self.sent: list[object] = []
        self.peer = DeviceInfo(
            device_id,
            "controller",
            DeviceRole.CONTROLLER,
            ScreenInfo(1920, 1080),
        )
        self.close_reason: CloseReason | None = None
        self._closed = asyncio.Event()
        if state is SessionState.CLOSED:
            self._closed.set()

    async def events(self):  # type: ignore[no-untyped-def]
        await asyncio.Future()
        yield

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self, reason: CloseReason) -> None:
        self.close_reason = reason
        self.state = SessionState.CLOSED
        self._closed.set()

    async def send(self, message: object) -> None:
        self.sent.append(message)


class _FakeRuntime:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def test_client_listens_and_never_connects_outward() -> None:
    config = AppConfig.default(device_name="laptop", role=DeviceRole.CLIENT)

    plan = build_role_network_plan(config, "192.168.110.157")

    assert plan.listener is not None
    assert plan.listener.host == "192.168.110.157"
    assert plan.listener.port == 24870
    assert plan.targets == ()


def test_controller_connects_only_to_explicit_configured_client_ips() -> None:
    config = AppConfig(
        version=2,
        device_name="desk",
        role=DeviceRole.CONTROLLER,
        adapter_id=None,
        bind_ip=None,
        port=24870,
        local_position=ScreenPosition.CENTER,
        peers=(
            PeerTarget(ScreenPosition.LEFT, "192.168.110.157", 24870),
            PeerTarget(ScreenPosition.TOP, "192.168.110.158", 24871),
        ),
    )

    plan = build_role_network_plan(config, "192.168.110.156")

    assert plan.listener is None
    assert [(target.host, target.port) for target in plan.targets] == [
        ("192.168.110.157", 24870),
        ("192.168.110.158", 24871),
    ]


def test_outer_controller_cannot_route_through_offline_center() -> None:
    assert reachable_connected_positions(
        ScreenPosition.LEFT,
        {ScreenPosition.TOP, ScreenPosition.RIGHT},
    ) == set()
    assert reachable_connected_positions(
        ScreenPosition.LEFT,
        {ScreenPosition.CENTER, ScreenPosition.TOP},
    ) == {ScreenPosition.CENTER, ScreenPosition.TOP}


@pytest.mark.asyncio
async def test_client_accepts_reconnect_while_closed_session_cleanup_is_pending(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    config = AppConfig.default(device_name="client", role=DeviceRole.CLIENT)
    paths = AppPaths(
        tmp_path,
        tmp_path / "config.json",
        tmp_path / "logs",
        tmp_path / "identity",
        tmp_path / "staging",
    )
    service = NetworkRoleService(config, paths)
    closed = _FakeControllerSession(SessionState.CLOSED)
    reconnect = _FakeControllerSession(SessionState.ACTIVE)
    service._client_session = closed  # type: ignore[assignment]

    assert service._allow_inbound_controller(reconnect.peer)
    await service._accept_controller_session(reconnect)  # type: ignore[arg-type]

    assert service._client_session is reconnect
    assert reconnect.close_reason is None
    await service.stop()


def test_idle_client_allows_new_controller_to_reach_pairing(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    config = AppConfig.default(device_name="client", role=DeviceRole.CLIENT)
    paths = AppPaths(
        tmp_path,
        tmp_path / "config.json",
        tmp_path / "logs",
        tmp_path / "identity",
        tmp_path / "staging",
    )
    service = NetworkRoleService(config, paths)
    new_controller = _FakeControllerSession(
        SessionState.ACTIVE, device_id="new-controller"
    ).peer

    assert service._allow_inbound_controller(new_controller)


def test_active_client_allows_same_controller_reconnect_but_rejects_takeover(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    config = AppConfig.default(device_name="client", role=DeviceRole.CLIENT)
    paths = AppPaths(
        tmp_path,
        tmp_path / "config.json",
        tmp_path / "logs",
        tmp_path / "identity",
        tmp_path / "staging",
    )
    service = NetworkRoleService(config, paths)
    service._client_session = _FakeControllerSession(  # type: ignore[assignment]
        SessionState.ACTIVE
    )

    same_controller = _FakeControllerSession(SessionState.ACTIVE).peer
    different_controller = _FakeControllerSession(
        SessionState.ACTIVE, device_id="different-controller"
    ).peer

    assert service._allow_inbound_controller(same_controller)
    assert (
        service._allow_inbound_controller(different_controller)
        is CloseReason.ROLE_CONFLICT
    )


@pytest.mark.asyncio
async def test_new_idle_controller_prompts_once_then_reconnects_without_prompt(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    decisions = 0

    async def approve(_request):  # type: ignore[no-untyped-def]
        nonlocal decisions
        decisions += 1
        return True

    config = AppConfig.default(device_name="client", role=DeviceRole.CLIENT)
    paths = AppPaths(
        tmp_path,
        tmp_path / "config.json",
        tmp_path / "logs",
        tmp_path / "identity",
        tmp_path / "staging",
    )
    service = NetworkRoleService(config, paths, pairing_decider=approve)
    service._pairing = PairingCoordinator(  # type: ignore[arg-type]
        object(), service._trust_store
    )
    peer = _FakeControllerSession(
        SessionState.ACTIVE, device_id="new-controller"
    ).peer
    certificate = b"new-controller-certificate"

    assert service._verify_inbound_controller(peer, certificate)
    await service._approve_inbound_controller(peer, certificate)
    assert service._verify_inbound_controller(peer, certificate)
    await service._approve_inbound_controller(peer, certificate)

    assert decisions == 1


def test_known_controller_rejects_changed_or_reused_certificate(tmp_path) -> None:  # type: ignore[no-untyped-def]
    config = AppConfig.default(device_name="client", role=DeviceRole.CLIENT)
    paths = AppPaths(
        tmp_path,
        tmp_path / "config.json",
        tmp_path / "logs",
        tmp_path / "identity",
        tmp_path / "staging",
    )
    service = NetworkRoleService(config, paths)
    certificate = b"remembered-certificate"
    service._trust_store.approve("remembered", certificate)
    remembered = _FakeControllerSession(
        SessionState.ACTIVE, device_id="remembered"
    ).peer
    impersonator = _FakeControllerSession(
        SessionState.ACTIVE, device_id="impersonator"
    ).peer

    assert service._verify_inbound_controller(remembered, certificate)
    assert not service._verify_inbound_controller(remembered, b"changed-certificate")
    assert not service._verify_inbound_controller(impersonator, certificate)


@pytest.mark.asyncio
async def test_same_controller_reconnect_replaces_stale_active_session(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    config = AppConfig.default(device_name="client", role=DeviceRole.CLIENT)
    paths = AppPaths(
        tmp_path,
        tmp_path / "config.json",
        tmp_path / "logs",
        tmp_path / "identity",
        tmp_path / "staging",
    )
    service = NetworkRoleService(config, paths)
    stale = _FakeControllerSession(SessionState.ACTIVE)
    reconnect = _FakeControllerSession(SessionState.ACTIVE)
    stale_runtime = _FakeRuntime()
    service._client_session = stale  # type: ignore[assignment]
    service._client_runtime = stale_runtime  # type: ignore[assignment]
    await service._accept_controller_session(reconnect)  # type: ignore[arg-type]

    assert stale.close_reason is CloseReason.LOCAL_REQUEST
    assert stale_runtime.stopped
    assert service._client_session is reconnect
    assert reconnect.close_reason is None
    await service.stop()


@pytest.mark.asyncio
async def test_client_disconnect_releases_session_before_secondary_cleanup(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    config = AppConfig.default(device_name="client", role=DeviceRole.CLIENT)
    paths = AppPaths(
        tmp_path,
        tmp_path / "config.json",
        tmp_path / "logs",
        tmp_path / "identity",
        tmp_path / "staging",
    )
    statuses: list[str] = []
    service = NetworkRoleService(config, paths, status=statuses.append)
    closed = _FakeControllerSession(SessionState.CLOSED)

    runtime = _FakeRuntime()
    service._client_session = closed  # type: ignore[assignment]
    service._client_runtime = runtime  # type: ignore[assignment]

    async def fail_cleanup(_device_id: str, _reason: str) -> None:
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(service, "_cancel_pastes_for_device", fail_cleanup)

    await service._watch_client_session(closed)  # type: ignore[arg-type]

    assert service._client_session is None
    assert runtime.stopped
    assert statuses[-1] == "主控已断开"


@pytest.mark.asyncio
async def test_controller_retries_rejected_handshake_without_rebuilding_runtime(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    config = AppConfig.default(device_name="controller", role=DeviceRole.CONTROLLER)
    paths = AppPaths(
        tmp_path,
        tmp_path / "config.json",
        tmp_path / "logs",
        tmp_path / "identity",
        tmp_path / "staging",
    )
    service = NetworkRoleService(config, paths)
    service._local_device = DeviceInfo(
        "controller-id",
        "controller",
        DeviceRole.CONTROLLER,
        ScreenInfo(1920, 1080),
    )
    service._identity = object()  # type: ignore[assignment]
    target = PeerTarget(ScreenPosition.LEFT, "192.0.2.10", 24870)
    attempts = 0
    rebuilds = 0

    async def reject_connection(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            service._stop.set()
        raise SessionConnectError(CloseReason.ROLE_CONFLICT)

    async def count_rebuilds() -> None:
        nonlocal rebuilds
        rebuilds += 1

    async def no_delay(_delay: float) -> None:
        return None

    monkeypatch.setattr(service_module, "connect_session", reject_connection)
    monkeypatch.setattr(service, "_rebuild_controller_runtime", count_rebuilds)
    monkeypatch.setattr(service_module.asyncio, "sleep", no_delay)

    await service._controller_connection_loop(target)

    assert attempts == 2
    assert rebuilds == 0


@pytest.mark.asyncio
async def test_controller_connection_supervisor_restarts_crashed_worker(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    config = AppConfig.default(device_name="controller", role=DeviceRole.CONTROLLER)
    paths = AppPaths(
        tmp_path,
        tmp_path / "config.json",
        tmp_path / "logs",
        tmp_path / "identity",
        tmp_path / "staging",
    )
    statuses: list[str] = []
    service = NetworkRoleService(config, paths, status=statuses.append)
    target = PeerTarget(ScreenPosition.LEFT, "192.0.2.10", 24870)
    attempts = 0

    async def crash_then_stop(_target: PeerTarget) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("worker crashed")
        service._stop.set()

    async def no_delay(_delay: float) -> None:
        return None

    monkeypatch.setattr(service, "_controller_connection_loop", crash_then_stop)
    monkeypatch.setattr(service_module.asyncio, "sleep", no_delay)

    await service._supervise_controller_connection(target)

    assert attempts == 2
    assert any("重启连接任务" in status for status in statuses)
