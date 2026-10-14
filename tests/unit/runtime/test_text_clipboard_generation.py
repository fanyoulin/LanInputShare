import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from lan_input_share.clipboard.coordinator import ClipboardCoordinator
from lan_input_share.clipboard.models import ClipboardItem
from lan_input_share.config.models import AppConfig
from lan_input_share.config.paths import AppPaths
from lan_input_share.core.models import DeviceInfo, DeviceRole, ScreenInfo
from lan_input_share.network.session import SessionState
from lan_input_share.protocol.messages import ClipboardMessage
from lan_input_share.runtime.service import (
    NetworkRoleService,
    _clipboard_item_from_message,
    _item_from_marker,
)


class FakeClipboard:
    def read_marker(self) -> str:
        return ""

    def read_file_drop(self) -> tuple[Path, ...]:
        return ()

    def read_unicode_text(self) -> str:
        return "copied after restart"


class FakeSession:
    def __init__(self) -> None:
        self.state = SessionState.ACTIVE
        self.session_id = "session"
        self.peer = DeviceInfo(
            "client",
            "Client",
            DeviceRole.CLIENT,
            ScreenInfo(1920, 1080),
        )
        self.sent: asyncio.Queue[ClipboardMessage] = asyncio.Queue()

    async def send(self, message: ClipboardMessage) -> None:
        await self.sent.put(message)


def make_paths(root: Path) -> AppPaths:
    return AppPaths(
        root,
        root / "config.json",
        root / "logs",
        root / "identity",
        root / "staging",
    )


def test_generated_marker_round_trips_clipboard_item() -> None:
    copied = ClipboardItem.text("controller", 1, "new run", generation="run-two")

    assert _item_from_marker(copied.marker, "new run") == copied


def test_wire_message_preserves_sender_generation() -> None:
    copied = ClipboardItem.text("controller", 1, "new run", generation="run-two")
    message = ClipboardMessage(
        "session",
        1,
        {
            "origin_id": copied.origin_id,
            "generation": copied.generation,
            "sequence": copied.sequence,
            "digest": copied.digest,
            "kind": copied.kind.value,
            "payload": copied.payload,
        },
    )

    assert _clipboard_item_from_message(message) == copied


@pytest.mark.asyncio
async def test_local_copy_broadcasts_nonempty_process_generation(
    tmp_path: Path,
) -> None:
    service = NetworkRoleService(
        AppConfig.default(device_name="Controller", role=DeviceRole.CONTROLLER),
        make_paths(tmp_path),
    )
    service._loop = asyncio.get_running_loop()
    service._identity = SimpleNamespace(device_id="controller")  # type: ignore[assignment]
    service._clipboard = FakeClipboard()  # type: ignore[assignment]
    service._clipboard_coordinator = ClipboardCoordinator("controller")
    session = FakeSession()
    service._controller_sessions = {object(): session}  # type: ignore[dict-item]

    service._clipboard_changed_from_windows()
    message = await asyncio.wait_for(session.sent.get(), timeout=1)

    assert message.payload["generation"]
