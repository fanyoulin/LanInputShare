from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from lan_input_share.clipboard.coordinator import ClipboardCoordinator
from lan_input_share.config.models import AppConfig
from lan_input_share.config.paths import AppPaths
from lan_input_share.core.models import DeviceInfo, DeviceRole, ScreenInfo
from lan_input_share.network.session import SessionState
from lan_input_share.protocol.messages import ClipboardMessage, FileDataMessage, WireMessage
from lan_input_share.runtime.service import NetworkRoleService


@dataclass(frozen=True)
class ImageFixture:
    format: str
    data: bytes


class FakeSession:
    def __init__(self, peer_id: str, peer_role: DeviceRole) -> None:
        self.state = SessionState.ACTIVE
        self.session_id = "session"
        self.peer = DeviceInfo(
            peer_id,
            peer_id.title(),
            peer_role,
            ScreenInfo(1920, 1080),
        )
        self.sent: list[WireMessage] = []

    async def send(self, message: WireMessage) -> None:
        self.sent.append(message)


class SourceClipboard:
    def __init__(self, image: ImageFixture) -> None:
        self.image = image

    def read_marker(self) -> str:
        return ""

    def read_file_drop(self) -> tuple[Path, ...]:
        return ()

    def read_image(self) -> ImageFixture:
        return self.image

    def read_unicode_text(self) -> str:
        raise AssertionError("截图存在时不应退化为文字剪贴板")


class TargetClipboard:
    def __init__(self) -> None:
        self.image: object | None = None
        self.marker = ""

    def write_image(self, image: object, marker: str) -> None:
        self.image = image
        self.marker = marker


def make_paths(root: Path) -> AppPaths:
    return AppPaths(
        root,
        root / "config.json",
        root / "logs",
        root / "identity",
        root / "staging",
    )


@pytest.mark.asyncio
async def test_screenshot_is_chunked_and_reconstructed_as_image_clipboard(
    tmp_path: Path,
) -> None:
    # Deterministic incompressible bytes ensure the transfer spans several frames.
    original = ImageFixture("dibv5", random.Random(7).randbytes(100_000))
    source = NetworkRoleService(
        AppConfig.default(device_name="Controller", role=DeviceRole.CONTROLLER),
        make_paths(tmp_path / "source"),
    )
    source._loop = asyncio.get_running_loop()
    source._identity = SimpleNamespace(device_id="controller")  # type: ignore[assignment]
    source._clipboard = SourceClipboard(original)  # type: ignore[assignment]
    source._clipboard_coordinator = ClipboardCoordinator("controller")
    outbound = FakeSession("client", DeviceRole.CLIENT)
    source._controller_sessions = {object(): outbound}  # type: ignore[dict-item]

    source._clipboard_changed_from_windows()
    for _ in range(100):
        if any(
            isinstance(message, FileDataMessage)
            and message.payload.get("final") is True
            for message in outbound.sent
        ):
            break
        await asyncio.sleep(0.01)

    assert isinstance(outbound.sent[0], ClipboardMessage)
    assert outbound.sent[0].payload["kind"] == "image"
    chunks = [
        message
        for message in outbound.sent[1:]
        if isinstance(message, FileDataMessage)
    ]
    assert len(chunks) >= 3
    assert all(message.payload["channel"] == "clipboard_image" for message in chunks)
    assert chunks[-1].payload["final"] is True

    target = NetworkRoleService(
        AppConfig.default(device_name="Client", role=DeviceRole.CLIENT),
        make_paths(tmp_path / "target"),
    )
    target._identity = SimpleNamespace(device_id="client")  # type: ignore[assignment]
    target._clipboard_coordinator = ClipboardCoordinator(
        "client", forward_remote=False
    )
    target_clipboard = TargetClipboard()
    target._clipboard = target_clipboard  # type: ignore[assignment]
    target._client_session = FakeSession("controller", DeviceRole.CONTROLLER)  # type: ignore[assignment]

    for message in outbound.sent:
        await target._client_message(message)

    assert target_clipboard.image is not None
    assert (
        target_clipboard.image.format,  # type: ignore[attr-defined]
        target_clipboard.image.data,  # type: ignore[attr-defined]
    ) == (original.format, original.data)
    assert target_clipboard.marker.startswith("controller:")


@pytest.mark.asyncio
async def test_client_screenshot_is_reconstructed_on_controller(tmp_path: Path) -> None:
    original = ImageFixture("dibv5", random.Random(11).randbytes(100_000))
    source = NetworkRoleService(
        AppConfig.default(device_name="Client", role=DeviceRole.CLIENT),
        make_paths(tmp_path / "source"),
    )
    source._loop = asyncio.get_running_loop()
    source._identity = SimpleNamespace(device_id="client")  # type: ignore[assignment]
    source._clipboard = SourceClipboard(original)  # type: ignore[assignment]
    source._clipboard_coordinator = ClipboardCoordinator(
        "client", forward_remote=False
    )
    outbound = FakeSession("controller", DeviceRole.CONTROLLER)
    source._client_session = outbound  # type: ignore[assignment]

    source._clipboard_changed_from_windows()
    for _ in range(100):
        if any(
            isinstance(message, FileDataMessage)
            and message.payload.get("final") is True
            for message in outbound.sent
        ):
            break
        await asyncio.sleep(0.01)

    target = NetworkRoleService(
        AppConfig.default(device_name="Controller", role=DeviceRole.CONTROLLER),
        make_paths(tmp_path / "target"),
    )
    target._identity = SimpleNamespace(device_id="controller")  # type: ignore[assignment]
    target._clipboard_coordinator = ClipboardCoordinator("controller")
    target_clipboard = TargetClipboard()
    target._clipboard = target_clipboard  # type: ignore[assignment]

    for message in outbound.sent:
        await target._controller_message("client", message)

    assert target_clipboard.image is not None
    assert (
        target_clipboard.image.format,  # type: ignore[attr-defined]
        target_clipboard.image.data,  # type: ignore[attr-defined]
    ) == (original.format, original.data)
    assert target_clipboard.marker.startswith("client:")
