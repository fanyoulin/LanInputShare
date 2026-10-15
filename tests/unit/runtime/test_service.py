from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

import lan_input_share.runtime.service as service_module
from lan_input_share.config.models import AppConfig, PeerTarget
from lan_input_share.config.paths import AppPaths
from lan_input_share.core.models import DeviceRole, ScreenPosition
from lan_input_share.elevated.models import HelperState, HelperStatus
from lan_input_share.protocol.messages import InputCapabilityMessage
from lan_input_share.runtime.service import NetworkRoleService


class FakeHelper:
    def __init__(self) -> None:
        self.status = HelperStatus(HelperState.RUNNING, "ready")
        self.callbacks: list[Callable[[HelperStatus], None]] = []
        self.starts = 0
        self.stops = 0

    def add_status_callback(self, callback: Callable[[HelperStatus], None]) -> None:
        self.callbacks.append(callback)

    def inject(self, event: object) -> None:
        return

    def release_all(self, events: tuple[object, ...]) -> None:
        return

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1


class FakeLocal:
    def inject(self, event: object) -> None:
        return

    def release_all(self, events: tuple[object, ...]) -> None:
        return


@pytest.mark.asyncio
async def test_client_helper_lifecycle_has_no_duplicate_threads(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    helper = FakeHelper()
    statuses: list[HelperStatus] = []
    monkeypatch.setattr(service_module, "Win32InputInjector", FakeLocal)
    service = NetworkRoleService(
        AppConfig.default(device_name="client", role=DeviceRole.CLIENT),
        _paths(tmp_path),
        helper_factory=lambda: helper,  # type: ignore[arg-type]
        helper_status=statuses.append,
    )

    service._ensure_client_input()
    service._ensure_client_input()

    assert helper.starts == 1
    assert statuses[-1].state is HelperState.RUNNING
    await service.stop()
    assert helper.stops == 1


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        config_file=root / "config.json",
        log_dir=root / "logs",
        identity_dir=root / "identity",
        staging_dir=root / "staging",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("standard", "标准控制"),
        ("elevated", "增强控制"),
        ("secure_desktop", "安全桌面"),
        ("unavailable", "暂不可用"),
        ("repair_required", "需要修复"),
    ],
)
async def test_controller_surfaces_client_input_capability_without_disconnect(
    tmp_path: Path,
    mode: str,
    expected: str,
) -> None:
    slot_updates: list[tuple[ScreenPosition, str, str]] = []
    target = PeerTarget(ScreenPosition.RIGHT, "192.168.1.8")
    session = SimpleNamespace(peer=SimpleNamespace(device_id="client-1", name="Client"))
    service = NetworkRoleService(
        AppConfig.default(device_name="controller", role=DeviceRole.CONTROLLER),
        _paths(tmp_path),
        slot_status=lambda position, name, status: slot_updates.append(
            (position, name, status)
        ),
    )
    service._controller_sessions[target] = session  # type: ignore[assignment]

    await service._controller_message(
        "client-1",
        InputCapabilityMessage("s", 1, {"mode": mode, "detail": "detail"}),
    )

    assert slot_updates[-1][0] is ScreenPosition.RIGHT
    assert expected in slot_updates[-1][2]


@pytest.mark.asyncio
async def test_client_sends_input_capability_only_on_state_change(tmp_path: Path) -> None:
    sent: list[object] = []

    class Session:
        session_id = "s"

        async def send(self, message: object) -> None:
            sent.append(message)

    service = NetworkRoleService(
        AppConfig.default(device_name="client", role=DeviceRole.CLIENT),
        _paths(tmp_path),
    )
    service._client_session = Session()  # type: ignore[assignment]
    running = HelperStatus(HelperState.RUNNING, "ready")

    await service._send_input_capability(running)
    await service._send_input_capability(running)
    await service._send_input_capability(
        HelperStatus(HelperState.SECURE_DESKTOP, "secure")
    )

    assert [message.payload["mode"] for message in sent] == [
        "elevated",
        "secure_desktop",
    ]
