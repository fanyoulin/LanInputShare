from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from lan_input_share.input.events import (
    InputEvent,
    KeyEvent,
    MouseButton,
    MouseButtonEvent,
    MouseMoveEvent,
    MouseWheelEvent,
    Point,
    Suppression,
)
from lan_input_share.input.motion import RemoteMotionAccumulator
from lan_input_share.input.win32_hook import HookEventQueue
from lan_input_share.network.session import (
    Disconnected,
    MessageReceived,
    SessionEvent,
    SessionState,
)
from lan_input_share.protocol.messages import (
    InputMessage,
    JsonValue,
    ReleaseInputMessage,
    WireMessage,
)
from lan_input_share.routing.controller import (
    ConfineLocalCursor,
    ReleaseTarget,
    RestoreLocalCursor,
    RoutingAction,
    RoutingController,
    SendRemote,
)
from lan_input_share.routing.hotkeys import HotkeyAction, HotkeyMatcher

_LOGGER = logging.getLogger("lan_input_share.input")


class InputHook(Protocol):
    def start(self, callback: Callable[[InputEvent], Suppression]) -> None: ...

    def stop(self) -> None: ...

    def set_remote_active(self, active: bool) -> None: ...


class CursorController(Protocol):
    def position(self) -> Point: ...

    def confine_for_remote(self) -> Point: ...

    def recenter_remote(self) -> None: ...

    def restore_local(self, position: Point | None = None) -> None: ...


class RuntimeSession(Protocol):
    session_id: str

    @property
    def state(self) -> SessionState: ...

    @property
    def peer(self) -> object: ...

    async def send(self, message: WireMessage) -> None: ...

    def events(self) -> AsyncIterator[SessionEvent]: ...


class ControllerRuntime:
    def __init__(
        self,
        routing: RoutingController,
        hook: InputHook,
        cursor: CursorController,
        *,
        queue_capacity: int = 512,
        motion_interval: float = 0.004,
        message_handler: Callable[[str, WireMessage], Awaitable[None]] | None = None,
        paste_handler: Callable[[str], bool] | None = None,
    ) -> None:
        if motion_interval <= 0:
            raise ValueError("鼠标刷新间隔必须为正数")
        self._routing = routing
        self._hook = hook
        self._cursor = cursor
        self._queue = HookEventQueue(queue_capacity)
        self._matcher = HotkeyMatcher()
        self._sessions: dict[str, RuntimeSession] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._wakeup: asyncio.Event | None = None
        self._worker: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._paused = False
        self._started = False
        self._sequence = 0
        self._message_handler = message_handler
        self._paste_handler = paste_handler
        self._control_down = False
        self._suppress_paste_key = False
        self._motion = RemoteMotionAccumulator()
        self._motion_interval = motion_interval
        self._last_motion_flush = 0.0
        self._remote_anchor: Point | None = None
        self._remote_pointer: Point | None = None
        self._awaiting_entry_warp = False
        self._motion_diagnostic_remaining = 0

    def attach_session(self, session: RuntimeSession) -> None:
        peer = session.peer
        device_id = getattr(peer, "device_id", None)
        if not isinstance(device_id, str):
            raise ValueError("会话缺少对端设备信息")
        self._sessions[device_id] = session
        if self._started:
            self._watchers[device_id] = asyncio.create_task(
                self._watch_session(device_id, session)
            )

    async def detach_session(self, device_id: str) -> None:
        session = self._sessions.get(device_id)
        actions = self._routing.handle_disconnect(device_id)
        if session is not None:
            await self._execute_actions(actions, release_session=session)
        else:
            await self._execute_actions(actions)
        self._sessions.pop(device_id, None)
        self._hook.set_remote_active(
            self._routing.active_device_id in self._sessions and not self._paused
        )
        watcher = self._watchers.pop(device_id, None)
        current = asyncio.current_task()
        if watcher is not None and watcher is not current:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._loop = asyncio.get_running_loop()
        self._wakeup = asyncio.Event()
        self._worker = asyncio.create_task(self._input_worker())
        self._hook.start(self._on_hook_event)
        for device_id, session in self._sessions.items():
            self._watchers[device_id] = asyncio.create_task(
                self._watch_session(device_id, session)
            )

    async def pause(self) -> None:
        self._paused = True
        await self.return_local()
        self._hook.set_remote_active(False)
        self._cursor.restore_local()

    async def resume(self) -> None:
        self._paused = False

    async def return_local(self) -> None:
        await self._execute_actions(self._routing.return_local())
        self._hook.set_remote_active(False)

    async def stop(self) -> None:
        if not self._started:
            return
        await self.pause()
        self._started = False
        self._hook.stop()
        tasks = [*self._watchers.values()]
        if self._worker is not None:
            tasks.append(self._worker)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._watchers.clear()
        self._worker = None

    def _on_hook_event(self, event: InputEvent) -> Suppression:
        if self._paused or not self._started:
            return Suppression.PASS_THROUGH
        remote_active = self._routing.active_device_id in self._sessions
        if isinstance(event, KeyEvent):
            if event.virtual_key in (0x11, 0xA2, 0xA3):
                self._control_down = event.is_down
            elif event.virtual_key == 0x56:
                if event.is_down and self._control_down and self._paste_handler is not None:
                    target_id = self._routing.active_device_id
                    if self._paste_handler(target_id):
                        self._suppress_paste_key = True
                        return Suppression.SUPPRESS_ALWAYS
                elif not event.is_down and self._suppress_paste_key:
                    self._suppress_paste_key = False
                    return Suppression.SUPPRESS_ALWAYS
        if remote_active and isinstance(event, MouseMoveEvent):
            if self._remote_anchor is not None:
                if self._awaiting_entry_warp:
                    self._log_motion_sample("entry_settle", event.position)
                    if event.position == self._remote_anchor:
                        self._awaiting_entry_warp = False
                        return Suppression.SUPPRESS
                    if not _is_post_warp_motion(event.position, self._remote_anchor):
                        self._cursor.recenter_remote()
                        return Suppression.SUPPRESS
                    self._awaiting_entry_warp = False
                self._log_motion_sample("remote_motion", event.position)
                self._motion.add(
                    event.position.x - self._remote_anchor.x,
                    event.position.y - self._remote_anchor.y,
                    event.timestamp,
                )
                self._cursor.recenter_remote()
        else:
            self._queue.put(event)
        loop = self._loop
        wakeup = self._wakeup
        if loop is not None and wakeup is not None:
            loop.call_soon_threadsafe(wakeup.set)
        return (
            Suppression.SUPPRESS
            if remote_active
            else Suppression.PASS_THROUGH
        )

    async def _input_worker(self) -> None:
        assert self._wakeup is not None
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            events = self._queue.drain()
            await self._flush_remote_motion(force=bool(events))
            for event in events:
                if not isinstance(event, MouseMoveEvent):
                    await self._flush_remote_motion(force=True)
                await self._handle_event(event)
            if self._motion.has_pending():
                self._wakeup.set()

    async def _handle_event(self, event: InputEvent) -> None:
        if isinstance(event, KeyEvent):
            hotkey = self._matcher.feed(event)
            if hotkey is HotkeyAction.RETURN_LOCAL:
                await self.return_local()
                return
            slots = {
                HotkeyAction.SELECT_DEVICE_1: 0,
                HotkeyAction.SELECT_DEVICE_2: 1,
                HotkeyAction.SELECT_DEVICE_3: 2,
            }
            if hotkey in slots:
                await self._execute_actions(self._routing.activate_slot(slots[hotkey]))
                self._hook.set_remote_active(
                    self._routing.active_device_id in self._sessions and not self._paused
                )
                return
        await self._execute_actions(self._routing.handle_input(event))
        self._hook.set_remote_active(
            self._routing.active_device_id in self._sessions and not self._paused
        )

    async def _execute_actions(
        self,
        actions: tuple[RoutingAction, ...],
        *,
        release_session: RuntimeSession | None = None,
    ) -> None:
        for action in actions:
            if isinstance(action, SendRemote):
                session = self._sessions.get(action.device_id)
                if session is not None and session.state is SessionState.ACTIVE:
                    if isinstance(action.event, MouseMoveEvent):
                        self._remote_pointer = action.event.position
                    await session.send(
                        InputMessage(
                            session.session_id,
                            self._next_sequence(),
                            encode_input_event(action.event),
                        )
                    )
            elif isinstance(action, ReleaseTarget):
                session = release_session or self._sessions.get(action.device_id)
                if session is not None and session.state is SessionState.ACTIVE:
                    await session.send(
                        ReleaseInputMessage(
                            session.session_id,
                            self._next_sequence(),
                            {
                                "reason": "target_change",
                                "events": [encode_input_event(event) for event in action.events],
                            },
                        )
                    )
            elif isinstance(action, ConfineLocalCursor):
                self._remote_anchor = self._cursor.confine_for_remote()
                self._awaiting_entry_warp = True
                self._motion_diagnostic_remaining = 8
                self._motion.clear()
            elif isinstance(action, RestoreLocalCursor):
                self._cursor.restore_local(action.position)
                self._motion.clear()
                self._remote_anchor = None
                self._remote_pointer = None
                self._awaiting_entry_warp = False
                self._motion_diagnostic_remaining = 0

    async def _flush_remote_motion(self, *, force: bool) -> None:
        if (
            self._routing.active_device_id not in self._sessions
            or self._remote_pointer is None
        ):
            self._motion.clear()
            return
        loop = asyncio.get_running_loop()
        if not force:
            remaining = self._motion_interval - (loop.time() - self._last_motion_flush)
            if remaining > 0:
                await asyncio.sleep(remaining)
        motion = self._motion.drain()
        if motion is None or self._remote_pointer is None:
            return
        event = MouseMoveEvent(
            Point(
                self._remote_pointer.x + motion.dx,
                self._remote_pointer.y + motion.dy,
            ),
            timestamp=motion.timestamp,
        )
        self._last_motion_flush = loop.time()
        await self._execute_actions(self._routing.handle_input(event))
        self._hook.set_remote_active(
            self._routing.active_device_id in self._sessions and not self._paused
        )

    async def _watch_session(self, device_id: str, session: RuntimeSession) -> None:
        async for event in session.events():
            if isinstance(event, Disconnected):
                await self.detach_session(device_id)
                return
            if isinstance(event, MessageReceived) and self._message_handler is not None:
                await self._message_handler(device_id, event.message)

    def _next_sequence(self) -> int:
        sequence = self._sequence
        self._sequence += 1
        return sequence

    def _log_motion_sample(self, phase: str, point: Point) -> None:
        anchor = self._remote_anchor
        loop = self._loop
        if (
            anchor is None
            or loop is None
            or self._motion_diagnostic_remaining <= 0
        ):
            return
        self._motion_diagnostic_remaining -= 1
        loop.call_soon_threadsafe(
            _LOGGER.info,
            "mouse_handoff phase=%s point=%d,%d anchor=%d,%d active=%s",
            phase,
            point.x,
            point.y,
            anchor.x,
            anchor.y,
            self._routing.active_device_id,
        )


def _is_post_warp_motion(position: Point, anchor: Point) -> bool:
    """Reject old edge samples while accepting the first real post-warp motion."""
    x_limit = max(64, abs(anchor.x) * 3 // 4)
    y_limit = max(64, abs(anchor.y) * 3 // 4)
    return (
        abs(position.x - anchor.x) <= x_limit
        and abs(position.y - anchor.y) <= y_limit
    )


def encode_input_event(event: InputEvent) -> dict[str, JsonValue]:
    common: dict[str, JsonValue] = {
        "injected": event.injected,
        "timestamp": event.timestamp,
    }
    if isinstance(event, KeyEvent):
        return common | {
            "kind": "key",
            "scan_code": event.scan_code,
            "virtual_key": event.virtual_key,
            "is_down": event.is_down,
            "is_extended": event.is_extended,
        }
    if isinstance(event, MouseMoveEvent):
        return common | {"kind": "mouse_move", "x": event.position.x, "y": event.position.y}
    if isinstance(event, MouseButtonEvent):
        return common | {
            "kind": "mouse_button",
            "button": event.button.value,
            "is_down": event.is_down,
        }
    if isinstance(event, MouseWheelEvent):
        return common | {
            "kind": "mouse_wheel",
            "delta": event.delta,
            "horizontal": event.horizontal,
        }
    raise TypeError(f"不支持的输入事件：{type(event).__name__}")


def decode_input_event(payload: dict[str, JsonValue]) -> InputEvent:
    kind = payload.get("kind")
    injected = _bool(payload.get("injected", False))
    timestamp = _number(payload.get("timestamp", 0.0))
    if kind == "key":
        return KeyEvent(
            _int(payload.get("scan_code")),
            _int(payload.get("virtual_key")),
            _bool(payload.get("is_down")),
            _bool(payload.get("is_extended")),
            injected,
            timestamp,
        )
    if kind == "mouse_move":
        return MouseMoveEvent(
            Point(_int(payload.get("x")), _int(payload.get("y"))),
            injected,
            timestamp,
        )
    if kind == "mouse_button":
        return MouseButtonEvent(
            MouseButton(_string(payload.get("button"))),
            _bool(payload.get("is_down")),
            injected,
            timestamp,
        )
    if kind == "mouse_wheel":
        return MouseWheelEvent(
            _int(payload.get("delta")),
            _bool(payload.get("horizontal")),
            injected,
            timestamp,
        )
    raise ValueError(f"未知输入事件类型：{kind}")


def _int(value: JsonValue) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("输入事件整数无效")
    return value


def _number(value: JsonValue) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("输入事件时间戳无效")
    return float(value)


def _bool(value: JsonValue) -> bool:
    if not isinstance(value, bool):
        raise ValueError("输入事件布尔值无效")
    return value


def _string(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise ValueError("输入事件字符串无效")
    return value


__all__ = ["ControllerRuntime", "decode_input_event", "encode_input_event"]
