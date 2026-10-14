from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lan_input_share.config.models import AppConfig
from lan_input_share.config.paths import AppPaths
from lan_input_share.core.models import DeviceInfo, DeviceRole, ScreenInfo
from lan_input_share.network.session import SessionState
from lan_input_share.protocol.messages import FileAbortMessage, WireMessage
from lan_input_share.runtime.service import NetworkRoleService
from lan_input_share.transfer.manifest import build_manifest


class FakeSession:
    def __init__(self) -> None:
        self.state = SessionState.ACTIVE
        self.session_id = "controller-session"
        self.peer = DeviceInfo(
            "controller", "Controller", DeviceRole.CONTROLLER, ScreenInfo(1920, 1080)
        )
        self.sent: list[WireMessage] = []

    async def send(self, message: WireMessage) -> None:
        self.sent.append(message)


def make_paths(root: Path) -> AppPaths:
    return AppPaths(
        root,
        root / "config.json",
        root / "logs",
        root / "identity",
        root / "staging",
    )


@pytest.mark.asyncio
async def test_source_failure_abort_never_exposes_absolute_path_or_file_name(
    tmp_path: Path,
) -> None:
    private = tmp_path / "TOP-SECRET-NAME.bin"
    private.write_bytes(b"before")
    manifest = build_manifest([private])
    private.write_bytes(b"changed-size")
    service = NetworkRoleService(
        AppConfig.default(device_name="Source", role=DeviceRole.CLIENT),
        make_paths(tmp_path / "app"),
    )
    service._identity = SimpleNamespace(device_id="source")  # type: ignore[assignment]
    session = FakeSession()
    service._client_session = session  # type: ignore[assignment]

    await service._start_file_sender(manifest, "source", "target")
    await service._file_send_tasks[manifest.transfer_id]

    abort = next(message for message in session.sent if isinstance(message, FileAbortMessage))
    assert str(tmp_path) not in repr(abort.payload)
    assert "TOP-SECRET-NAME.bin" not in repr(abort.payload)
    assert abort.payload["reason"] == "源文件已变化"


@pytest.mark.asyncio
async def test_duplicate_abort_is_idempotent_and_releases_target_state(
    tmp_path: Path,
) -> None:
    service = NetworkRoleService(
        AppConfig.default(device_name="Target", role=DeviceRole.CLIENT),
        make_paths(tmp_path / "app"),
    )
    service._identity = SimpleNamespace(device_id="target")  # type: ignore[assignment]
    service._target_paste_transfer_id = "tx-1"
    message = FileAbortMessage(
        "session", 1, {"transfer_id": "tx-1", "reason": "连接已断开"}
    )

    await service._receive_file_abort("controller", message)
    await service._receive_file_abort("controller", message)

    assert service._target_paste_transfer_id is None
    assert service._paste_decision_waiters == {}
    assert service._file_streams == {}
