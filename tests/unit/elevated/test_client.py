from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import pytest

from lan_input_share.elevated.client import (
    HelperInputBridge,
    HelperQueueFull,
    _InputQueue,
    reconnect_delays,
)
from lan_input_share.elevated.models import ApprovedClient, HelperState, HelperStatus
from lan_input_share.elevated.protocol import (
    Hello,
    HelperFrameDecoder,
    Input,
    ReleaseAll,
    Status,
    encode_helper_message,
)
from lan_input_share.input.events import KeyEvent, MouseMoveEvent, Point


class FakeConnection:
    def __init__(self) -> None:
        self.incoming: queue.Queue[bytes | None] = queue.Queue()
        self.writes: list[bytes] = []
        self.written = threading.Event()
        self.closed = threading.Event()

    def read(self, size: int = 0) -> bytes:
        value = self.incoming.get(timeout=2)
        return b"" if value is None else value

    def write(self, data: bytes) -> None:
        self.writes.append(data)
        self.written.set()

    def close(self) -> None:
        self.closed.set()
        self.incoming.put(None)


class FakePipeClient:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.connect_calls = 0

    def connect(self, timeout: float) -> FakeConnection:
        self.connect_calls += 1
        return self.connection


def approved() -> ApprovedClient:
    return ApprovedClient(Path(r"C:\LanInputShare.exe"), "A" * 64, "S-1-5-21-1", 1)


def test_bridge_handshake_status_and_ordered_input() -> None:
    connection = FakeConnection()
    bridge = HelperInputBridge(
        approved(),
        pipe_client=FakePipeClient(connection),
        process_id=321,
        heartbeat_interval=0.05,
    )
    bridge.start()
    assert connection.written.wait(1)
    messages = _written_messages(connection)
    assert messages[0] == Hello(0, 1, 321)

    connection.incoming.put(
        encode_helper_message(Status(0, HelperState.RUNNING, "ready"))
    )
    _wait_until(lambda: bridge.status.state is HelperState.RUNNING)
    key = KeyEvent(42, 0x10, True)
    move = MouseMoveEvent(Point(100, 200))
    bridge.inject(key)
    bridge.inject(move)
    _wait_until(
        lambda: len(
            [m for m in _written_messages(connection) if isinstance(m, Input)]
        )
        == 2
    )
    bridge.stop()

    inputs = [
        message.event
        for message in _written_messages(connection)
        if isinstance(message, Input)
    ]
    assert inputs == [key, move]


def test_reconnect_delays_are_bounded_exponential() -> None:
    delays = reconnect_delays()
    assert [next(delays) for _ in range(7)] == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0]


def test_input_queue_coalesces_motion_and_preserves_transitions() -> None:
    events = _InputQueue(capacity=3)
    key_down = KeyEvent(42, 0x10, True)
    key_up = KeyEvent(42, 0x10, False)
    events.put_event(MouseMoveEvent(Point(1, 1)))
    events.put_event(key_down)
    events.put_event(MouseMoveEvent(Point(2, 2)))
    events.put_event(MouseMoveEvent(Point(3, 3)))
    events.put_event(key_up)

    assert events.get(0) == MouseMoveEvent(Point(1, 1))
    assert events.get(0) == key_down
    assert events.get(0) == key_up


def test_input_queue_preserves_first_motion_of_a_continuous_burst() -> None:
    events = _InputQueue(capacity=3)

    events.put_event(MouseMoveEvent(Point(1919, 500)))
    events.put_event(MouseMoveEvent(Point(1900, 500)))
    events.put_event(MouseMoveEvent(Point(1870, 500)))

    assert events.get(0) == MouseMoveEvent(Point(1919, 500))
    assert events.get(0) == MouseMoveEvent(Point(1870, 500))


def test_input_queue_preserves_first_and_latest_motion_when_saturated() -> None:
    events = _InputQueue(capacity=2)
    key = KeyEvent(1, 1, True)
    first = MouseMoveEvent(Point(1919, 500))
    latest = MouseMoveEvent(Point(1800, 500))

    events.put_event(key)
    events.put_event(first)
    events.put_event(latest)

    assert events.get(0) == key
    assert events.get(0) == first
    assert events.get(0) == latest


def test_queue_saturation_never_silently_drops_transition_or_release() -> None:
    events = _InputQueue(capacity=2)
    events.put_event(KeyEvent(1, 1, True))
    events.put_event(KeyEvent(2, 2, True))
    with pytest.raises(HelperQueueFull):
        events.put_event(KeyEvent(3, 3, True))

    events.put_release()
    assert events.get(0) is ReleaseAll


def test_bridge_inject_is_non_blocking_and_queue_failure_marks_unavailable() -> None:
    connection = FakeConnection()
    bridge = HelperInputBridge(
        approved(),
        pipe_client=FakePipeClient(connection),
        process_id=321,
        capacity=1,
    )
    bridge._set_status(HelperState.RUNNING, "ready")
    bridge._queue.put_event(KeyEvent(1, 1, True))

    started = time.monotonic()
    with pytest.raises(HelperQueueFull):
        bridge.inject(KeyEvent(2, 2, True))

    assert time.monotonic() - started < 0.05
    assert bridge.status.state is HelperState.UNAVAILABLE


def test_release_all_uses_urgent_release_marker() -> None:
    connection = FakeConnection()
    bridge = HelperInputBridge(
        approved(),
        pipe_client=FakePipeClient(connection),
        process_id=321,
    )
    bridge._set_status(HelperState.RUNNING, "ready")
    bridge.release_all((KeyEvent(42, 0x10, False),))

    assert bridge._queue.get(0) is ReleaseAll


def test_disconnect_immediately_marks_bridge_unavailable() -> None:
    connection = FakeConnection()
    bridge = HelperInputBridge(
        approved(),
        pipe_client=FakePipeClient(connection),
        process_id=321,
        heartbeat_interval=0.05,
    )
    bridge.start()
    assert connection.written.wait(1)
    connection.incoming.put(
        encode_helper_message(Status(0, HelperState.RUNNING, "ready"))
    )
    _wait_until(lambda: bridge.status.state is HelperState.RUNNING)

    connection.incoming.put(None)

    _wait_until(lambda: bridge.status.state is HelperState.UNAVAILABLE)
    bridge.stop()


def test_bridge_detects_live_install_and_starts_helper_without_app_restart() -> None:
    connection = FakeConnection()
    pipe_client = FakePipeClient(connection)
    probe_calls = 0
    starts = 0

    def installation_status() -> HelperStatus:
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 1:
            return HelperStatus(HelperState.NOT_INSTALLED, "not installed")
        return HelperStatus(HelperState.RUNNING, "installed")

    def ensure_started() -> None:
        nonlocal starts
        starts += 1

    bridge = HelperInputBridge(
        approved(),
        pipe_client=pipe_client,
        process_id=321,
        heartbeat_interval=0.05,
        initial_status=HelperStatus(HelperState.NOT_INSTALLED, "not installed"),
        installation_status=installation_status,
        ensure_started=ensure_started,
    )
    bridge.start()
    assert connection.written.wait(2)
    bridge.stop()

    assert probe_calls >= 2
    assert starts >= 1
    assert pipe_client.connect_calls == 1


def test_bridge_restarts_installed_helper_after_disconnect() -> None:
    first = FakeConnection()
    second = FakeConnection()

    class SequencedPipeClient:
        def __init__(self) -> None:
            self.connections = iter((first, second))

        def connect(self, timeout: float) -> FakeConnection:
            del timeout
            return next(self.connections)

    starts = 0

    def ensure_started() -> None:
        nonlocal starts
        starts += 1

    bridge = HelperInputBridge(
        approved(),
        pipe_client=SequencedPipeClient(),
        process_id=321,
        heartbeat_interval=0.05,
        installation_status=lambda: HelperStatus(HelperState.RUNNING, "installed"),
        ensure_started=ensure_started,
    )
    bridge.start()
    assert first.written.wait(1)
    first.incoming.put(None)
    assert second.written.wait(2)
    bridge.stop()

    assert starts >= 2


def test_bridge_keeps_retrying_when_task_start_temporarily_fails() -> None:
    connection = FakeConnection()
    starts = 0

    def ensure_started() -> None:
        nonlocal starts
        starts += 1
        if starts == 1:
            raise LookupError("task is being replaced")

    bridge = HelperInputBridge(
        approved(),
        pipe_client=FakePipeClient(connection),
        process_id=321,
        heartbeat_interval=0.05,
        installation_status=lambda: HelperStatus(HelperState.RUNNING, "installed"),
        ensure_started=ensure_started,
    )
    bridge.start()
    assert connection.written.wait(2)
    bridge.stop()

    assert starts >= 2


def _written_messages(connection: FakeConnection) -> list[object]:
    decoder = HelperFrameDecoder()
    return [message for data in list(connection.writes) for message in decoder.feed(data)]


def _wait_until(predicate: object, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(predicate) and predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")
