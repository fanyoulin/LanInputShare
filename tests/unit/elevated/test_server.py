from __future__ import annotations

import queue
import threading
from pathlib import Path

from lan_input_share.elevated.models import ApprovedClient, ClientIdentity, HelperState
from lan_input_share.elevated.protocol import (
    HELPER_PROTOCOL_VERSION,
    Hello,
    HelperFrameDecoder,
    Input,
    Status,
    encode_helper_message,
)
from lan_input_share.elevated.server import ElevatedInputHelper
from lan_input_share.input.events import KeyEvent, MouseButton, MouseButtonEvent


class FakeConnection:
    def __init__(self, client_pid: int = 123) -> None:
        self.client_pid = client_pid
        self.incoming: queue.Queue[bytes | None] = queue.Queue()
        self.writes: list[bytes] = []
        self.read_calls = 0
        self.closed = threading.Event()

    def read(self, size: int = 0) -> bytes:
        self.read_calls += 1
        value = self.incoming.get(timeout=1)
        return b"" if value is None else value

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed.set()
        self.incoming.put(None)


class FakePipeServer:
    def accept(self) -> FakeConnection:
        raise AssertionError("not used by focused unit tests")

    def close(self) -> None:
        return


class FakeInjector:
    def __init__(self) -> None:
        self.events: list[object] = []

    def inject(self, event: object) -> None:
        self.events.append(event)

    def release_all(self, events: tuple[object, ...]) -> None:
        self.events.extend(events)


def approved() -> ApprovedClient:
    return ApprovedClient(Path(r"C:\LanInputShare.exe"), "A" * 64, "S-1-5-21-1", 1)


def identity(**changes: object) -> ClientIdentity:
    values: dict[str, object] = {
        "pid": 123,
        "path": Path(r"C:\LanInputShare.exe"),
        "sha256": "A" * 64,
        "user_sid": "S-1-5-21-1",
        "session_id": 2,
    }
    values.update(changes)
    return ClientIdentity(**values)  # type: ignore[arg-type]


def test_server_authorizes_process_before_reading_first_frame() -> None:
    connection = FakeConnection()
    injector = FakeInjector()
    helper = ElevatedInputHelper(
        approved(),
        injector,
        FakePipeServer(),
        helper_session_id=2,
        identity_inspector=lambda _pid: identity(sha256="B" * 64),
    )

    helper.serve_connection(connection)

    assert connection.read_calls == 0
    assert injector.events == []
    assert connection.closed.is_set()


def test_server_injects_and_releases_pressed_input_on_disconnect() -> None:
    connection = FakeConnection()
    injector = FakeInjector()
    helper = ElevatedInputHelper(
        approved(),
        injector,
        FakePipeServer(),
        helper_session_id=2,
        identity_inspector=lambda _pid: identity(),
        desktop_name=lambda: "Default",
    )
    key_down = KeyEvent(42, 0x10, True)
    button_down = MouseButtonEvent(MouseButton.LEFT, True)
    connection.incoming.put(encode_helper_message(Hello(0, HELPER_PROTOCOL_VERSION, 123)))
    connection.incoming.put(encode_helper_message(Input(1, key_down)))
    connection.incoming.put(encode_helper_message(Input(2, button_down)))
    connection.incoming.put(None)

    helper.serve_connection(connection)

    assert injector.events == [
        key_down,
        button_down,
        KeyEvent(42, 0x10, False),
        MouseButtonEvent(MouseButton.LEFT, False),
    ]
    statuses = _written_statuses(connection)
    assert statuses[-1].state is HelperState.RUNNING


def test_server_suppresses_input_on_secure_desktop() -> None:
    connection = FakeConnection()
    injector = FakeInjector()
    helper = ElevatedInputHelper(
        approved(),
        injector,
        FakePipeServer(),
        helper_session_id=2,
        identity_inspector=lambda _pid: identity(),
        desktop_name=lambda: "Winlogon",
    )
    connection.incoming.put(encode_helper_message(Hello(0, 1, 123)))
    connection.incoming.put(encode_helper_message(Input(1, KeyEvent(42, 0x10, True))))
    connection.incoming.put(None)

    helper.serve_connection(connection)

    assert injector.events == []
    assert _written_statuses(connection)[-1].state is HelperState.SECURE_DESKTOP


def test_server_checks_desktop_on_status_boundaries_not_every_input() -> None:
    connection = FakeConnection()
    injector = FakeInjector()
    desktop_checks = 0

    def desktop_name() -> str:
        nonlocal desktop_checks
        desktop_checks += 1
        return "Default"

    helper = ElevatedInputHelper(
        approved(),
        injector,
        FakePipeServer(),
        helper_session_id=2,
        identity_inspector=lambda _pid: identity(),
        desktop_name=desktop_name,
    )
    connection.incoming.put(encode_helper_message(Hello(0, 1, 123)))
    connection.incoming.put(encode_helper_message(Input(1, KeyEvent(1, 1, True))))
    connection.incoming.put(encode_helper_message(Input(2, KeyEvent(1, 1, False))))
    connection.incoming.put(None)

    helper.serve_connection(connection)

    assert desktop_checks == 1


def test_server_releases_pressed_input_after_heartbeat_timeout() -> None:
    connection = FakeConnection()
    injector = FakeInjector()
    helper = ElevatedInputHelper(
        approved(),
        injector,
        FakePipeServer(),
        helper_session_id=2,
        heartbeat_timeout=0.05,
        identity_inspector=lambda _pid: identity(),
        desktop_name=lambda: "Default",
    )
    connection.incoming.put(encode_helper_message(Hello(0, 1, 123)))
    connection.incoming.put(encode_helper_message(Input(1, KeyEvent(42, 0x10, True))))

    helper.serve_connection(connection)

    assert injector.events[-1] == KeyEvent(42, 0x10, False)
    assert connection.closed.is_set()


def test_malformed_connection_does_not_poison_next_connection() -> None:
    injector = FakeInjector()
    helper = ElevatedInputHelper(
        approved(),
        injector,
        FakePipeServer(),
        helper_session_id=2,
        identity_inspector=lambda _pid: identity(),
        desktop_name=lambda: "Default",
    )
    malformed = FakeConnection()
    malformed.incoming.put(b"\x00\x00\x00\x01{")
    malformed.incoming.put(None)
    valid = FakeConnection()
    valid.incoming.put(encode_helper_message(Hello(0, 1, 123)))
    valid.incoming.put(encode_helper_message(Input(1, KeyEvent(42, 0x10, True))))
    valid.incoming.put(None)

    helper.serve_connection(malformed)
    helper.serve_connection(valid)

    assert valid.closed.is_set()
    assert injector.events[-2:] == [KeyEvent(42, 0x10, True), KeyEvent(42, 0x10, False)]


def _written_statuses(connection: FakeConnection) -> list[Status]:
    decoder = HelperFrameDecoder()
    messages = [message for data in connection.writes for message in decoder.feed(data)]
    return [message for message in messages if isinstance(message, Status)]
