from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from lan_input_share.core.models import DeviceInfo, DeviceRole, ScreenInfo
from lan_input_share.input.events import (
    KeyEvent,
    MouseButton,
    MouseButtonEvent,
    MouseMoveEvent,
    Point,
    Suppression,
)
from lan_input_share.network.session import SessionEvent, SessionState
from lan_input_share.protocol.messages import InputMessage, ReleaseInputMessage, WireMessage
from lan_input_share.routing.controller import RoutingController
from lan_input_share.routing.layout import LinearLayout
from lan_input_share.runtime.app_runtime import AppRuntime, RuntimeStatus
from lan_input_share.runtime.controller import ControllerRuntime


class FakeHook:
    def __init__(self) -> None:
        self.callback = None
        self.remote_active = False
        self.stopped = False

    def start(self, callback):  # type: ignore[no-untyped-def]
        self.callback = callback

    def stop(self) -> None:
        self.stopped = True

    def set_remote_active(self, active: bool) -> None:
        self.remote_active = active

    def emit(self, event):  # type: ignore[no-untyped-def]
        assert self.callback is not None
        return self.callback(event)


class FakeCursor:
    def __init__(self) -> None:
        self.confined = 0
        self.restored = 0
        self.recentered = 0
        self.current = Point(100, 100)
        self.restored_position: Point | None = None
        self.on_confine = None
        self.confine_anchor: Point | None = None

    def position(self) -> Point:
        return self.current

    def confine_for_remote(self) -> Point:
        self.confined += 1
        if self.on_confine is not None:
            self.on_confine()
        return self.confine_anchor or self.current

    def restore_local(self, position: Point | None = None) -> None:
        self.restored += 1
        self.restored_position = position
        if position is not None:
            self.current = position

    def recenter_remote(self) -> None:
        self.recentered += 1


class FakeSession:
    def __init__(self, peer: DeviceInfo, state: SessionState = SessionState.ACTIVE) -> None:
        self.peer = peer
        self.state = state
        self.session_id = f"session-{peer.device_id}"
        self.sent: list[WireMessage] = []
        self.sent_at: list[float] = []
        self.queue: asyncio.Queue[SessionEvent] = asyncio.Queue()

    async def send(self, message: WireMessage) -> None:
        self.sent.append(message)
        self.sent_at.append(asyncio.get_running_loop().time())

    async def events(self) -> AsyncIterator[SessionEvent]:
        while True:
            yield await self.queue.get()


def make_runtime() -> tuple[ControllerRuntime, FakeHook, FakeCursor, FakeSession]:
    desk = DeviceInfo("desk", "Desk", DeviceRole.CONTROLLER, ScreenInfo(1920, 1080))
    client = DeviceInfo("client", "Client", DeviceRole.CLIENT, ScreenInfo(1366, 768))
    routing = RoutingController(
        "desk", {"desk": desk, "client": client}, LinearLayout(("desk", "client"))
    )
    hook = FakeHook()
    cursor = FakeCursor()
    cursor.on_confine = lambda: asyncio.get_running_loop().call_soon(
        hook.emit, MouseMoveEvent(cursor.current)
    )
    session = FakeSession(client)
    runtime = ControllerRuntime(routing, hook, cursor)
    runtime.attach_session(session)
    return runtime, hook, cursor, session


def make_runtime_with_paste_handler(
    paste_handler,  # type: ignore[no-untyped-def]
) -> tuple[ControllerRuntime, FakeHook, FakeCursor, FakeSession]:
    desk = DeviceInfo("desk", "Desk", DeviceRole.CONTROLLER, ScreenInfo(1920, 1080))
    client = DeviceInfo("client", "Client", DeviceRole.CLIENT, ScreenInfo(1366, 768))
    routing = RoutingController(
        "desk", {"desk": desk, "client": client}, LinearLayout(("desk", "client"))
    )
    hook = FakeHook()
    cursor = FakeCursor()
    session = FakeSession(client)
    runtime = ControllerRuntime(
        routing, hook, cursor, paste_handler=paste_handler
    )
    runtime.attach_session(session)
    return runtime, hook, cursor, session


async def eventually() -> None:
    await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_remote_input_is_sent_only_to_active_authenticated_client() -> None:
    runtime, hook, _cursor, session = make_runtime()
    await runtime.start()

    assert hook.emit(MouseMoveEvent(Point(1919, 500))) is Suppression.PASS_THROUGH
    await eventually()
    assert hook.remote_active is True
    hook.emit(KeyEvent(30, 65, True))
    await eventually()

    assert any(isinstance(message, InputMessage) for message in session.sent)
    session.state = SessionState.AUTHENTICATED
    count = len(session.sent)
    hook.emit(KeyEvent(30, 65, False))
    await eventually()
    assert len(session.sent) == count
    await runtime.stop()


@pytest.mark.asyncio
async def test_cross_device_ctrl_v_is_suppressed_and_requested_once() -> None:
    targets: list[str] = []
    runtime, hook, _cursor, session = make_runtime_with_paste_handler(
        lambda target: targets.append(target) or True
    )
    await runtime.start()
    hook.emit(MouseMoveEvent(Point(1919, 500)))
    await eventually()
    session.sent.clear()

    hook.emit(KeyEvent(29, 0x11, True))
    assert hook.emit(KeyEvent(47, 0x56, True)) is Suppression.SUPPRESS_ALWAYS
    assert hook.emit(KeyEvent(47, 0x56, False)) is Suppression.SUPPRESS_ALWAYS
    hook.emit(KeyEvent(29, 0x11, False))
    await eventually()

    assert targets == ["client"]
    assert not any(
        isinstance(message, InputMessage) and message.payload.get("virtual_key") == 0x56
        for message in session.sent
    )
    await runtime.stop()


@pytest.mark.asyncio
async def test_same_device_ctrl_v_is_not_suppressed() -> None:
    runtime, hook, _cursor, _session = make_runtime_with_paste_handler(
        lambda _target: False
    )
    await runtime.start()

    hook.emit(KeyEvent(29, 0x11, True))
    assert hook.emit(KeyEvent(47, 0x56, True)) is Suppression.PASS_THROUGH
    assert hook.emit(KeyEvent(47, 0x56, False)) is Suppression.PASS_THROUGH
    await runtime.stop()


@pytest.mark.asyncio
async def test_remote_mouse_accumulates_relative_motion_from_confined_anchor() -> None:
    runtime, hook, _cursor, session = make_runtime()
    await runtime.start()

    hook.emit(MouseMoveEvent(Point(1919, 500)))
    await eventually()
    session.sent.clear()

    hook.emit(MouseMoveEvent(Point(101, 100)))
    hook.emit(MouseMoveEvent(Point(102, 100)))
    await asyncio.sleep(0.01)

    mouse_positions = [
        (message.payload["x"], message.payload["y"])
        for message in session.sent
        if isinstance(message, InputMessage)
        and message.payload.get("kind") == "mouse_move"
    ]
    assert mouse_positions == [(3, 355)]
    await runtime.stop()


@pytest.mark.asyncio
async def test_synthetic_recentering_cannot_overwrite_mapped_entry_position() -> None:
    runtime, hook, cursor, session = make_runtime()
    cursor.on_confine = lambda: (
        hook.emit(MouseMoveEvent(Point(100, 100))),
        asyncio.get_running_loop().call_soon(
            hook.emit, MouseMoveEvent(Point(100, 100))
        ),
    )
    await runtime.start()

    hook.emit(MouseMoveEvent(Point(1919, 200)))
    await asyncio.sleep(0.01)

    positions = [
        (message.payload["x"], message.payload["y"])
        for message in session.sent
        if isinstance(message, InputMessage)
        and message.payload.get("kind") == "mouse_move"
    ]
    assert positions == [(0, 142)]
    await runtime.stop()


@pytest.mark.asyncio
async def test_fast_crossing_ignores_stale_edge_motion_until_warp_is_observed() -> None:
    runtime, hook, cursor, session = make_runtime()

    def schedule_stale_then_warp() -> None:
        loop = asyncio.get_running_loop()
        loop.call_soon(hook.emit, MouseMoveEvent(Point(0, 200)))
        loop.call_soon(hook.emit, MouseMoveEvent(cursor.current))

    cursor.on_confine = schedule_stale_then_warp
    await runtime.start()

    hook.emit(MouseMoveEvent(Point(1919, 200)))
    await asyncio.sleep(0.01)

    positions = [
        (message.payload["x"], message.payload["y"])
        for message in session.sent
        if isinstance(message, InputMessage)
        and message.payload.get("kind") == "mouse_move"
    ]
    assert positions == [(0, 142)]
    assert cursor.recentered == 1
    await runtime.stop()


@pytest.mark.asyncio
async def test_fast_crossing_unlocks_on_first_physical_motion_near_warp_anchor() -> None:
    runtime, hook, cursor, session = make_runtime()

    def schedule_stale_then_physical_motion() -> None:
        loop = asyncio.get_running_loop()
        loop.call_soon(hook.emit, MouseMoveEvent(Point(0, 200)))
        loop.call_soon(hook.emit, MouseMoveEvent(Point(112, 93)))

    cursor.on_confine = schedule_stale_then_physical_motion
    await runtime.start()

    hook.emit(MouseMoveEvent(Point(1919, 200)))
    await asyncio.sleep(0.01)

    positions = [
        (message.payload["x"], message.payload["y"])
        for message in session.sent
        if isinstance(message, InputMessage)
        and message.payload.get("kind") == "mouse_move"
    ]
    assert positions == [(0, 142), (12, 135)]
    assert cursor.recentered == 2
    await runtime.stop()


@pytest.mark.asyncio
async def test_repeated_crossing_uses_the_anchor_selected_by_cursor_controller() -> None:
    runtime, hook, cursor, session = make_runtime()
    cursor.current = Point(10, 906)
    cursor.confine_anchor = Point(960, 540)

    def schedule_stale_edge_then_physical_motion() -> None:
        loop = asyncio.get_running_loop()
        loop.call_soon(hook.emit, MouseMoveEvent(Point(1910, 1080)))
        loop.call_soon(hook.emit, MouseMoveEvent(Point(970, 540)))

    cursor.on_confine = schedule_stale_edge_then_physical_motion
    await runtime.start()

    hook.emit(MouseMoveEvent(Point(1919, 200)))
    await asyncio.sleep(0.01)

    positions = [
        (message.payload["x"], message.payload["y"])
        for message in session.sent
        if isinstance(message, InputMessage)
        and message.payload.get("kind") == "mouse_move"
    ]
    assert positions == [(0, 142), (10, 142)]
    await runtime.stop()


@pytest.mark.asyncio
async def test_remote_motion_is_flushed_before_click_and_never_drops_burst_distance() -> None:
    runtime, hook, _cursor, session = make_runtime()
    await runtime.start()
    hook.emit(MouseMoveEvent(Point(1919, 500)))
    await eventually()
    session.sent.clear()
    session.sent_at.clear()

    for _ in range(1_000):
        hook.emit(MouseMoveEvent(Point(101, 100)))
    hook.emit(MouseButtonEvent(MouseButton.LEFT, True))
    await asyncio.sleep(0.01)

    inputs = [message for message in session.sent if isinstance(message, InputMessage)]
    assert [message.payload["kind"] for message in inputs] == [
        "mouse_move",
        "mouse_button",
    ]
    assert inputs[0].payload["x"] == 1_000
    assert inputs[0].payload["y"] == 355
    await runtime.stop()


@pytest.mark.asyncio
async def test_remote_motion_flush_rate_is_bounded_to_250_hz() -> None:
    runtime, hook, _cursor, session = make_runtime()
    await runtime.start()
    hook.emit(MouseMoveEvent(Point(1919, 500)))
    await eventually()
    session.sent.clear()
    session.sent_at.clear()

    for _ in range(50):
        hook.emit(MouseMoveEvent(Point(101, 100)))
        await asyncio.sleep(0)
    await asyncio.sleep(0.015)

    mouse_send_times = [
        sent_at
        for message, sent_at in zip(session.sent, session.sent_at, strict=True)
        if isinstance(message, InputMessage)
        and message.payload.get("kind") == "mouse_move"
    ]
    assert all(
        later - earlier >= 0.0035
        for earlier, later in zip(
            mouse_send_times, mouse_send_times[1:], strict=False
        )
    )
    await runtime.stop()


@pytest.mark.asyncio
async def test_moving_back_across_client_edge_returns_local_control() -> None:
    runtime, hook, cursor, _session = make_runtime()
    await runtime.start()
    hook.emit(MouseMoveEvent(Point(1919, 500)))
    await eventually()

    hook.emit(MouseMoveEvent(Point(140, 100)))
    await asyncio.sleep(0.01)
    hook.emit(MouseMoveEvent(Point(50, 100)))
    await asyncio.sleep(0.01)

    assert hook.remote_active is False
    assert cursor.restored >= 1
    assert cursor.restored_position == Point(1919, 499)
    assert cursor.recentered == 2
    await runtime.stop()


@pytest.mark.asyncio
async def test_pause_restores_local_cursor_and_stops_suppression() -> None:
    runtime, hook, cursor, _session = make_runtime()
    await runtime.start()
    hook.emit(MouseMoveEvent(Point(1919, 500)))
    await eventually()

    await runtime.pause()

    assert hook.remote_active is False
    assert cursor.restored >= 1
    assert hook.emit(KeyEvent(30, 65, True)) is Suppression.PASS_THROUGH
    await runtime.stop()


@pytest.mark.asyncio
async def test_disconnect_releases_controller_state_before_recovery() -> None:
    runtime, hook, _cursor, session = make_runtime()
    await runtime.start()
    hook.emit(MouseMoveEvent(Point(1919, 500)))
    hook.emit(KeyEvent(30, 65, True))
    await eventually()

    await runtime.detach_session("client")

    assert any(isinstance(message, ReleaseInputMessage) for message in session.sent)
    assert hook.remote_active is False
    await runtime.stop()


@pytest.mark.asyncio
async def test_slow_status_messages_cannot_block_input_queue() -> None:
    runtime, hook, _cursor, session = make_runtime()
    app = AppRuntime()
    for index in range(5_000):
        app.publish_status(RuntimeStatus("transfer", str(index)))
    await runtime.start()

    hook.emit(MouseMoveEvent(Point(1919, 500)))
    hook.emit(KeyEvent(30, 65, True))
    await eventually()

    assert any(isinstance(message, InputMessage) for message in session.sent)
    await runtime.stop()
