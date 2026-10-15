from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

import pytest

from lan_input_share.elevated.client import HelperInputBridge
from lan_input_share.elevated.models import HELPER_PROTOCOL_VERSION, ApprovedClient, HelperState
from lan_input_share.elevated.pipe_transport import NamedPipeClient, NamedPipeServer
from lan_input_share.elevated.server import ElevatedInputHelper
from lan_input_share.input.events import InputEvent, KeyEvent
from lan_input_share.windows.process_identity import (
    current_user_sid,
    inspect_process_identity,
)

pytestmark = pytest.mark.windows_integration


class RecordingInjector:
    def __init__(self) -> None:
        self.events: list[InputEvent] = []
        self.changed = threading.Event()

    def inject(self, event: InputEvent) -> None:
        self.events.append(event)
        self.changed.set()

    def release_all(self, events: tuple[InputEvent, ...]) -> None:
        self.events.extend(events)
        self.changed.set()


def test_real_named_pipe_bridge_and_helper_round_trip() -> None:
    identity = inspect_process_identity(os.getpid())
    approved = ApprovedClient(
        Path(identity.path),
        identity.sha256,
        identity.user_sid,
        HELPER_PROTOCOL_VERSION,
    )
    pipe_name = rf"\\.\pipe\LanInputShare.Test.{uuid.uuid4().hex}"
    pipe_server = NamedPipeServer(pipe_name, current_user_sid())
    injector = RecordingInjector()
    helper = ElevatedInputHelper(
        approved,
        injector,
        pipe_server,
        helper_session_id=identity.session_id,
        desktop_name=lambda: "Default",
    )
    helper_thread = threading.Thread(target=helper.run, daemon=True)
    helper_thread.start()
    bridge = HelperInputBridge(
        approved,
        pipe_client=NamedPipeClient(pipe_name),
        process_id=os.getpid(),
        heartbeat_interval=0.05,
    )

    try:
        bridge.start()
        _wait_until(lambda: bridge.status.state is HelperState.RUNNING)
        event = KeyEvent(42, 0x10, True)
        bridge.inject(event)
        assert injector.changed.wait(2)
        assert injector.events[0] == event
    finally:
        bridge.stop()
        helper.stop()
        helper_thread.join(timeout=2)

    assert not helper_thread.is_alive()


def _wait_until(predicate: object, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(predicate) and predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")
