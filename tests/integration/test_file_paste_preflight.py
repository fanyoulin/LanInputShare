from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lan_input_share.config.models import AppConfig
from lan_input_share.config.paths import AppPaths
from lan_input_share.core.models import DeviceInfo, DeviceRole, ScreenInfo
from lan_input_share.network.session import SessionState
from lan_input_share.protocol.messages import (
    FileDataMessage,
    FilePasteProbeMessage,
    FileRequestMessage,
    WireMessage,
)
from lan_input_share.runtime.service import NetworkRoleService
from lan_input_share.transfer.manifest import build_manifest


class FakeSession:
    def __init__(self, peer_id: str) -> None:
        self.state = SessionState.ACTIVE
        self.session_id = f"{peer_id}-session"
        self.peer = DeviceInfo(
            peer_id, peer_id.title(), DeviceRole.CLIENT, ScreenInfo(1920, 1080)
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
async def test_three_role_relay_emits_no_start_or_file_data_until_target_decides(
    tmp_path: Path,
) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"data")
    manifest = build_manifest([source])
    controller = NetworkRoleService(
        AppConfig.default(device_name="Controller", role=DeviceRole.CONTROLLER),
        make_paths(tmp_path / "app"),
        overwrite_timeout=0.02,
    )
    controller._identity = SimpleNamespace(device_id="controller")  # type: ignore[assignment]
    source_session = FakeSession("source")
    target_session = FakeSession("target")
    controller._controller_sessions = {  # type: ignore[dict-item]
        object(): source_session,
        object(): target_session,
    }

    await controller._request_file_paste(
        SimpleNamespace(manifest=manifest, source_id="source"), "target"
    )

    assert isinstance(target_session.sent[0], FilePasteProbeMessage)
    assert not any(isinstance(message, FileRequestMessage) for message in target_session.sent)
    assert not any(isinstance(message, FileRequestMessage) for message in source_session.sent)
    assert not any(isinstance(message, FileDataMessage) for message in target_session.sent)
    assert not any(isinstance(message, FileDataMessage) for message in source_session.sent)
