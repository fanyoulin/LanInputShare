from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from lan_input_share.config.models import AppConfig
from lan_input_share.config.paths import AppPaths
from lan_input_share.core.models import DeviceInfo, DeviceRole, ScreenInfo
from lan_input_share.network.session import SessionState
from lan_input_share.protocol.messages import (
    FileOfferMessage,
    FilePasteDecisionMessage,
    FilePasteProbeMessage,
    FileRequestMessage,
    WireMessage,
)
from lan_input_share.runtime.service import (
    NetworkRoleService,
    OverwritePromptRequest,
    _ControlMessageWriter,
)
from lan_input_share.transfer.manifest import FileManifest, build_manifest
from lan_input_share.transfer.preflight import (
    PasteDecision,
    PasteDecisionResult,
    PasteProbe,
)
from lan_input_share.transfer.protocol import TransferProgress
from lan_input_share.transfer.status import TransferState
from lan_input_share.transfer.windows_destination import (
    DestinationKind,
    ResolvedDestination,
)
from lan_input_share.transfer.wire import manifest_from_payload


class FakeSession:
    def __init__(self, peer_id: str = "controller") -> None:
        self.state = SessionState.ACTIVE
        self.session_id = "client-session"
        self.peer = DeviceInfo(
            peer_id, peer_id.title(), DeviceRole.CLIENT, ScreenInfo(1920, 1080)
        )
        self.sent: list[WireMessage] = []

    async def send(self, message: WireMessage) -> None:
        self.sent.append(message)


class SlowFakeSession(FakeSession):
    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    async def send(self, message: WireMessage) -> None:
        import asyncio

        await asyncio.sleep(self.delay)
        await super().send(message)


class FakeClipboard:
    def __init__(self) -> None:
        self.paths: tuple[Path, ...] = ()
        self.marker = ""

    def write_file_drop(self, paths: tuple[Path, ...], marker: str) -> None:
        self.paths = paths
        self.marker = marker


class FakeDestinationResolver:
    def __init__(self, result: ResolvedDestination) -> None:
        self.result = result
        self.calls = 0

    async def resolve_foreground(self) -> ResolvedDestination:
        self.calls += 1
        return self.result


@pytest.mark.asyncio
async def test_file_writer_paces_network_data_to_leave_input_headroom() -> None:
    sent: list[bytes] = []
    delays: list[float] = []
    times = iter((0.0, 0.0))

    async def send(data: bytes) -> None:
        sent.append(data)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    writer = _ControlMessageWriter(
        send,
        bytes_per_second=1024,
        utilization=0.75,
        clock=lambda: next(times),
        sleeper=sleep,
    )

    await writer.write(b"x" * 512)

    assert sent == [b"x" * 512]
    assert delays == [pytest.approx(0.5)]


@pytest.mark.asyncio
async def test_file_writer_reserves_idle_time_when_socket_is_already_slow() -> None:
    delays: list[float] = []
    times = iter((10.0, 10.3))

    async def send(_data: bytes) -> None:
        return None

    async def sleep(delay: float) -> None:
        delays.append(delay)

    writer = _ControlMessageWriter(
        send,
        bytes_per_second=10_000,
        utilization=0.75,
        clock=lambda: next(times),
        sleeper=sleep,
    )

    await writer.write(b"x" * 1000)

    assert delays == [pytest.approx(0.1)]


def make_paths(root: Path) -> AppPaths:
    return AppPaths(
        root,
        root / "config.json",
        root / "logs",
        root / "identity",
        root / "staging",
    )


@pytest.mark.asyncio
async def test_copying_files_advertises_metadata_without_reading_or_staging_contents(
    tmp_path: Path,
) -> None:
    source = tmp_path / "private" / "payload.bin"
    source.parent.mkdir()
    source.write_bytes(b"secret-file-contents")
    paths = make_paths(tmp_path / "app")
    service = NetworkRoleService(
        AppConfig.default(device_name="Client", role=DeviceRole.CLIENT), paths
    )
    session = FakeSession()
    service._identity = SimpleNamespace(device_id="client")  # type: ignore[assignment]
    service._client_session = session  # type: ignore[assignment]

    await service._handle_local_files((source,))

    assert len(session.sent) == 1
    offer = session.sent[0]
    assert isinstance(offer, FileOfferMessage)
    assert str(tmp_path) not in repr(offer.payload)
    assert "secret-file-contents" not in repr(offer.payload)
    assert not paths.staging_dir.exists()


@pytest.mark.asyncio
async def test_cross_device_paste_stages_bytes_only_after_transfer_starts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source" / "payload.bin"
    source.parent.mkdir()
    source.write_bytes(b"transferred-on-paste")
    local_manifest = build_manifest([source])
    remote_manifest = FileManifest(local_manifest.transfer_id, local_manifest.entries)
    paths = make_paths(tmp_path / "app")
    paths.staging_dir.mkdir(parents=True)
    service = NetworkRoleService(
        AppConfig.default(device_name="Controller", role=DeviceRole.CONTROLLER), paths
    )
    service._identity = SimpleNamespace(device_id="target")  # type: ignore[assignment]
    clipboard = FakeClipboard()
    service._clipboard = clipboard  # type: ignore[assignment]
    replayed: list[bool] = []
    service._replay_paste = lambda: replayed.append(True)  # type: ignore[method-assign]

    assert not any(paths.staging_dir.iterdir())
    await service._start_file_receiver(remote_manifest, "source", "target")
    await service._start_file_sender(local_manifest, "source", "target")
    send_task = service._file_send_tasks[local_manifest.transfer_id]
    receive_task = service._file_receive_tasks[local_manifest.transfer_id]
    await send_task
    await receive_task

    assert [path.read_bytes() for path in clipboard.paths] == [
        b"transferred-on-paste"
    ]
    assert replayed == [True]


@pytest.mark.asyncio
async def test_direct_destination_commits_without_clipboard_or_paste_replay(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source" / "payload.bin"
    source.parent.mkdir()
    source.write_bytes(b"direct-to-explorer")
    local_manifest = build_manifest([source])
    remote_manifest = FileManifest(local_manifest.transfer_id, local_manifest.entries)
    destination = tmp_path / "destination"
    destination.mkdir()
    paths = make_paths(tmp_path / "app")
    paths.staging_dir.mkdir(parents=True)
    statuses = []
    service = NetworkRoleService(
        AppConfig.default(device_name="Target", role=DeviceRole.CONTROLLER),
        paths,
        destination_resolver=FakeDestinationResolver(
            ResolvedDestination(DestinationKind.DIRECT, destination, None)
        ),
        transfer_status=statuses.append,
        transfer_progress_delay=0.0,
        transfer_progress_interval=0.0,
    )
    service._identity = SimpleNamespace(device_id="target")  # type: ignore[assignment]
    clipboard = FakeClipboard()
    service._clipboard = clipboard  # type: ignore[assignment]
    replayed: list[bool] = []
    service._replay_paste = lambda: replayed.append(True)  # type: ignore[method-assign]
    decision = await service._prepare_target_preflight(
        PasteProbe(local_manifest.transfer_id, "source", "target", remote_manifest)
    )
    assert decision.compatibility is False

    await service._start_file_receiver(remote_manifest, "source", "target")
    await service._start_file_sender(local_manifest, "source", "target")
    send_task = service._file_send_tasks[local_manifest.transfer_id]
    receive_task = service._file_receive_tasks[local_manifest.transfer_id]
    await send_task
    await receive_task

    assert (destination / "payload.bin").read_bytes() == b"direct-to-explorer"
    assert clipboard.paths == ()
    assert replayed == []
    assert not any(paths.staging_dir.iterdir())
    assert any(status.state is TransferState.ACTIVE for status in statuses)
    assert statuses[-1].state is TransferState.COMPLETE


@pytest.mark.asyncio
async def test_source_sender_never_reports_target_progress(
    tmp_path: Path,
) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"x" * (128 * 1024))
    manifest = build_manifest([source])
    statuses = []
    service = NetworkRoleService(
        AppConfig.default(device_name="Source", role=DeviceRole.CLIENT),
        make_paths(tmp_path / "app"),
        transfer_status=statuses.append,
        transfer_progress_delay=0.01,
        transfer_progress_interval=0.0,
    )
    service._identity = SimpleNamespace(device_id="source")  # type: ignore[assignment]
    service._client_session = SlowFakeSession(0.004)  # type: ignore[assignment]

    await service._start_file_sender(manifest, "source", "target")
    await service._file_send_tasks[manifest.transfer_id]

    assert statuses == []


@pytest.mark.asyncio
async def test_subsecond_file_transfer_never_opens_progress_ui(tmp_path: Path) -> None:
    source = tmp_path / "small.txt"
    source.write_text("small", encoding="utf-8")
    local_manifest = build_manifest([source])
    remote_manifest = FileManifest(local_manifest.transfer_id, local_manifest.entries)
    statuses = []
    paths = make_paths(tmp_path / "app")
    paths.staging_dir.mkdir(parents=True)
    service = NetworkRoleService(
        AppConfig.default(device_name="Target", role=DeviceRole.CONTROLLER),
        paths,
        transfer_status=statuses.append,
        transfer_progress_delay=1.0,
    )
    service._identity = SimpleNamespace(device_id="target")  # type: ignore[assignment]
    service._clipboard = FakeClipboard()  # type: ignore[assignment]
    service._replay_paste = lambda: None  # type: ignore[method-assign]

    await service._start_file_receiver(remote_manifest, "source", "target")
    await service._start_file_sender(local_manifest, "source", "target")
    send_task = service._file_send_tasks[local_manifest.transfer_id]
    receive_task = service._file_receive_tasks[local_manifest.transfer_id]
    await send_task
    await receive_task

    assert statuses == []


@pytest.mark.asyncio
async def test_receiver_progress_updates_are_coalesced_to_ten_hz(
    tmp_path: Path,
) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"x" * 100)
    manifest = build_manifest([source])
    statuses = []
    service = NetworkRoleService(
        AppConfig.default(device_name="Target", role=DeviceRole.CLIENT),
        make_paths(tmp_path / "app"),
        transfer_status=statuses.append,
        transfer_progress_delay=0.0,
        transfer_progress_interval=0.1,
    )
    service._begin_transfer(manifest)
    timer = service._transfer_progress[manifest.transfer_id].timer
    assert timer is not None
    await timer
    assert len(statuses) == 1

    for transferred in (10, 20, 30):
        service._report_transfer_progress(
            TransferProgress(
                manifest.transfer_id, "large.bin", transferred, 100
            )
        )
    assert len(statuses) == 1

    await asyncio.sleep(0.11)
    service._report_transfer_progress(
        TransferProgress(manifest.transfer_id, "large.bin", 40, 100)
    )
    assert len(statuses) == 2


@pytest.mark.asyncio
async def test_conflicting_local_target_sends_no_start_before_overwrite_decision(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source" / "payload.bin"
    source.parent.mkdir()
    source.write_bytes(b"new")
    manifest = build_manifest([source])
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "payload.bin").write_bytes(b"old")
    pending: asyncio.Future[PasteDecision] | None = None

    async def decide(request: OverwritePromptRequest) -> PasteDecision:
        nonlocal pending
        assert request.transfer_id == manifest.transfer_id
        assert request.conflict_count == 1
        assert request.conflict_preview == ("payload.bin",)
        pending = asyncio.get_running_loop().create_future()
        return await pending

    service = NetworkRoleService(
        AppConfig.default(device_name="Target", role=DeviceRole.CONTROLLER),
        make_paths(tmp_path / "app"),
        overwrite_decider=decide,
        destination_resolver=FakeDestinationResolver(
            ResolvedDestination(DestinationKind.DIRECT, destination, None)
        ),
    )
    service._identity = SimpleNamespace(device_id="target")  # type: ignore[assignment]
    source_session = FakeSession("source")
    service._controller_sessions = {object(): source_session}  # type: ignore[dict-item]
    started: list[str] = []

    async def start_receiver(*_args) -> None:  # type: ignore[no-untyped-def]
        started.append("receiver")

    service._start_file_receiver = start_receiver  # type: ignore[method-assign]
    task = asyncio.create_task(
        service._request_file_paste(
            SimpleNamespace(manifest=manifest, source_id="source"), "target"
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert pending is not None
    assert started == []
    assert not any(isinstance(item, FileRequestMessage) for item in source_session.sent)

    pending.set_result(PasteDecision.OVERWRITE)
    await task

    assert started == ["receiver"]
    assert any(isinstance(item, FileRequestMessage) for item in source_session.sent)


@pytest.mark.asyncio
async def test_remote_overwrite_prompt_does_not_block_client_message_loop(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source" / "payload.bin"
    source.parent.mkdir()
    source.write_bytes(b"new")
    manifest = build_manifest([source])
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "payload.bin").write_bytes(b"old")
    decision_started = asyncio.Event()
    release_decision = asyncio.Event()

    async def decide(_request: OverwritePromptRequest) -> PasteDecision:
        decision_started.set()
        await release_decision.wait()
        return PasteDecision.CANCEL

    service = NetworkRoleService(
        AppConfig.default(device_name="Target", role=DeviceRole.CLIENT),
        make_paths(tmp_path / "app"),
        overwrite_decider=decide,
        destination_resolver=FakeDestinationResolver(
            ResolvedDestination(DestinationKind.DIRECT, destination, None)
        ),
    )
    service._identity = SimpleNamespace(device_id="target")  # type: ignore[assignment]
    service._client_session = FakeSession("controller")  # type: ignore[assignment]
    probe = PasteProbe(
        manifest.transfer_id, "controller", "target", manifest
    )

    await asyncio.wait_for(
        service._client_message(
            FilePasteProbeMessage("session", 1, probe.to_payload())
        ),
        timeout=0.1,
    )
    await asyncio.wait_for(decision_started.wait(), timeout=0.1)

    release_decision.set()
    await asyncio.wait_for(
        asyncio.gather(*service._paste_probe_tasks.values()), timeout=0.1
    )

    assert isinstance(service._client_session.sent[-1], FilePasteDecisionMessage)


@pytest.mark.asyncio
async def test_remote_target_receives_probe_before_any_start_message(tmp_path: Path) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"data")
    manifest = build_manifest([source])
    service = NetworkRoleService(
        AppConfig.default(device_name="Controller", role=DeviceRole.CONTROLLER),
        make_paths(tmp_path / "app"),
        overwrite_timeout=1.0,
    )
    service._identity = SimpleNamespace(device_id="controller")  # type: ignore[assignment]
    source_session = FakeSession("source")
    target_session = FakeSession("target")
    service._controller_sessions = {  # type: ignore[dict-item]
        object(): source_session,
        object(): target_session,
    }

    task = asyncio.create_task(
        service._request_file_paste(
            SimpleNamespace(manifest=manifest, source_id="source"), "target"
        )
    )
    await asyncio.sleep(0)

    assert [type(item) for item in target_session.sent] == [FilePasteProbeMessage]
    assert source_session.sent == []

    await service._receive_file_paste_decision(
        "target",
        FilePasteDecisionMessage(
            "session",
            1,
            PasteDecisionResult(
                manifest.transfer_id, PasteDecision.OVERWRITE, (), False
            ).to_payload(),
        ),
    )
    await task

    assert isinstance(target_session.sent[-1], FileRequestMessage)
    assert isinstance(source_session.sent[-1], FileRequestMessage)


@pytest.mark.asyncio
async def test_cancelled_preflight_never_starts_sender_or_receiver(tmp_path: Path) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"data")
    manifest = build_manifest([source])
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "payload.bin").write_bytes(b"old")
    service = NetworkRoleService(
        AppConfig.default(device_name="Target", role=DeviceRole.CONTROLLER),
        make_paths(tmp_path / "app"),
        overwrite_decider=lambda _request: asyncio.sleep(
            0, result=PasteDecision.CANCEL
        ),
        destination_resolver=FakeDestinationResolver(
            ResolvedDestination(DestinationKind.DIRECT, destination, None)
        ),
    )
    service._identity = SimpleNamespace(device_id="target")  # type: ignore[assignment]
    source_session = FakeSession("source")
    service._controller_sessions = {object(): source_session}  # type: ignore[dict-item]

    await service._request_file_paste(
        SimpleNamespace(manifest=manifest, source_id="source"), "target"
    )

    assert not any(isinstance(item, FileRequestMessage) for item in source_session.sent)
    assert manifest.transfer_id not in service._file_send_tasks
    assert manifest.transfer_id not in service._file_receive_tasks


@pytest.mark.asyncio
async def test_skip_conflicts_filters_manifest_before_source_start(tmp_path: Path) -> None:
    source_root = tmp_path / "bundle"
    source_root.mkdir()
    (source_root / "conflict.txt").write_text("new", encoding="utf-8")
    (source_root / "keep.txt").write_text("keep", encoding="utf-8")
    manifest = build_manifest([source_root])
    destination = tmp_path / "destination"
    (destination / "bundle").mkdir(parents=True)
    (destination / "bundle" / "conflict.txt").write_text("old", encoding="utf-8")
    service = NetworkRoleService(
        AppConfig.default(device_name="Target", role=DeviceRole.CONTROLLER),
        make_paths(tmp_path / "app"),
        overwrite_decider=lambda _request: asyncio.sleep(
            0, result=PasteDecision.SKIP
        ),
        destination_resolver=FakeDestinationResolver(
            ResolvedDestination(DestinationKind.DIRECT, destination, None)
        ),
    )
    service._identity = SimpleNamespace(device_id="target")  # type: ignore[assignment]
    source_session = FakeSession("source")
    service._controller_sessions = {object(): source_session}  # type: ignore[dict-item]

    async def start_receiver(*_args) -> None:  # type: ignore[no-untyped-def]
        return None

    service._start_file_receiver = start_receiver  # type: ignore[method-assign]

    await service._request_file_paste(
        SimpleNamespace(manifest=manifest, source_id="source"), "target"
    )

    start = next(
        message for message in source_session.sent if isinstance(message, FileRequestMessage)
    )
    filtered = manifest_from_payload(start.payload["manifest"])
    assert {entry.relative_path.as_posix() for entry in filtered.entries} == {
        "bundle",
        "bundle/keep.txt",
    }
    assert start.payload["skipped_paths"] == ["bundle/conflict.txt"]


@pytest.mark.asyncio
async def test_compatibility_destination_skips_prompt_and_allows_start(
    tmp_path: Path,
) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"data")
    manifest = build_manifest([source])

    async def must_not_prompt(_request: OverwritePromptRequest) -> PasteDecision:
        raise AssertionError("compatibility path must not prompt")

    service = NetworkRoleService(
        AppConfig.default(device_name="Target", role=DeviceRole.CONTROLLER),
        make_paths(tmp_path / "app"),
        overwrite_decider=must_not_prompt,
        destination_resolver=FakeDestinationResolver(
            ResolvedDestination(
                DestinationKind.COMPATIBILITY, None, "not_explorer"
            )
        ),
    )
    service._identity = SimpleNamespace(device_id="target")  # type: ignore[assignment]
    source_session = FakeSession("source")
    service._controller_sessions = {object(): source_session}  # type: ignore[dict-item]
    started: list[bool] = []

    async def start_receiver(*_args) -> None:  # type: ignore[no-untyped-def]
        started.append(True)

    service._start_file_receiver = start_receiver  # type: ignore[method-assign]
    await service._request_file_paste(
        SimpleNamespace(manifest=manifest, source_id="source"), "target"
    )

    assert started == [True]
    start = next(
        message for message in source_session.sent if isinstance(message, FileRequestMessage)
    )
    assert start.payload["compatibility"] is True


@pytest.mark.asyncio
async def test_overwrite_timeout_cancels_and_releases_target_lock(tmp_path: Path) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"new")
    manifest = build_manifest([source])
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "payload.bin").write_bytes(b"old")

    async def never_answers(_request: OverwritePromptRequest) -> PasteDecision:
        await asyncio.Future()
        raise AssertionError("unreachable")

    service = NetworkRoleService(
        AppConfig.default(device_name="Target", role=DeviceRole.CONTROLLER),
        make_paths(tmp_path / "app"),
        overwrite_decider=never_answers,
        destination_resolver=FakeDestinationResolver(
            ResolvedDestination(DestinationKind.DIRECT, destination, None)
        ),
        overwrite_timeout=0.01,
    )
    service._identity = SimpleNamespace(device_id="target")  # type: ignore[assignment]
    source_session = FakeSession("source")
    service._controller_sessions = {object(): source_session}  # type: ignore[dict-item]

    await service._request_file_paste(
        SimpleNamespace(manifest=manifest, source_id="source"), "target"
    )

    assert service._target_paste_transfer_id is None
    assert service._target_preflights == {}
    assert not any(isinstance(item, FileRequestMessage) for item in source_session.sent)


@pytest.mark.asyncio
async def test_second_target_probe_is_busy_until_first_transfer_finishes(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.txt"
    first_path.write_text("first", encoding="utf-8")
    second_path.write_text("second", encoding="utf-8")
    first = build_manifest([first_path])
    second = build_manifest([second_path])
    service = NetworkRoleService(
        AppConfig.default(device_name="Target", role=DeviceRole.CLIENT),
        make_paths(tmp_path / "app"),
        destination_resolver=FakeDestinationResolver(
            ResolvedDestination(
                DestinationKind.COMPATIBILITY, None, "not_explorer"
            )
        ),
    )

    first_result = await service._prepare_target_preflight(
        PasteProbe(first.transfer_id, "source", "target", first)
    )
    second_result = await service._prepare_target_preflight(
        PasteProbe(second.transfer_id, "source", "target", second)
    )

    assert first_result.decision is PasteDecision.OVERWRITE
    assert second_result.decision is PasteDecision.CANCEL
    assert service._target_paste_transfer_id == first.transfer_id
