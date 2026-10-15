from __future__ import annotations

from dataclasses import dataclass

from lan_input_share.core.models import DeviceInfo, ScreenInfo
from lan_input_share.input.events import InputEvent, MouseMoveEvent, Point
from lan_input_share.input.key_state import PressedInputState
from lan_input_share.routing.layout import (
    Direction,
    RoutingLayout,
    map_horizontal_edge_position,
    map_vertical_edge_position,
)


@dataclass(frozen=True, slots=True)
class DeliverLocal:
    event: InputEvent


@dataclass(frozen=True, slots=True)
class SendRemote:
    device_id: str
    event: InputEvent


@dataclass(frozen=True, slots=True)
class SwitchTarget:
    previous_device_id: str
    device_id: str


@dataclass(frozen=True, slots=True)
class ReleaseTarget:
    device_id: str
    events: tuple[InputEvent, ...]


@dataclass(frozen=True, slots=True)
class ConfineLocalCursor:
    pass


@dataclass(frozen=True, slots=True)
class RestoreLocalCursor:
    position: Point | None = None


type RoutingAction = (
    DeliverLocal
    | SendRemote
    | SwitchTarget
    | ReleaseTarget
    | ConfineLocalCursor
    | RestoreLocalCursor
)


class RoutingController:
    def __init__(
        self,
        local_device_id: str,
        devices: dict[str, DeviceInfo],
        layout: RoutingLayout,
        *,
        hysteresis_pixels: int = 1,
    ) -> None:
        if local_device_id not in devices or local_device_id not in layout.device_ids:
            raise ValueError("本机必须存在于设备列表和屏幕布局中")
        if set(layout.device_ids) - devices.keys():
            raise ValueError("屏幕布局包含未知设备")
        layout.validate_connected()
        if hysteresis_pixels < 1:
            raise ValueError("边缘防抖距离必须为正数")
        self._local_device_id = local_device_id
        self._devices = dict(devices)
        self._layout = layout
        self._hysteresis = hysteresis_pixels
        self._active_device_id = local_device_id
        self._pressed = PressedInputState()
        self._blocked_edge: Direction | None = None

    @property
    def active_device_id(self) -> str:
        return self._active_device_id

    def handle_input(self, event: InputEvent) -> tuple[RoutingAction, ...]:
        active_id = self._active_device_id
        active_screen = self._devices[active_id].screen
        if isinstance(event, MouseMoveEvent):
            self._update_hysteresis(event, active_screen.width, active_screen.height)
            neighbor, direction = self._edge_neighbor(
                event, active_id, active_screen.width, active_screen.height
            )
            if neighbor is not None and direction is not None:
                target_screen = self._devices[neighbor].screen
                mapped = MouseMoveEvent(
                    self._map_to_target_edge(
                        event, active_screen, target_screen, direction
                    ),
                    injected=event.injected,
                    timestamp=event.timestamp,
                )
                actions = list(self.activate(neighbor))
                self._blocked_edge = (
                    self._opposite(direction)
                    if neighbor == self._local_device_id
                    else None
                )
                if neighbor != self._local_device_id:
                    actions.append(SendRemote(neighbor, mapped))
                else:
                    actions = [
                        RestoreLocalCursor(mapped.position)
                        if isinstance(action, RestoreLocalCursor)
                        else action
                        for action in actions
                    ]
                return tuple(actions)

        if active_id == self._local_device_id:
            return (DeliverLocal(event),)
        if isinstance(event, MouseMoveEvent):
            event = self._clamp_mouse(event, active_screen)
        self._pressed.apply(event)
        return (SendRemote(active_id, event),)

    def activate(self, device_id: str) -> tuple[RoutingAction, ...]:
        if device_id not in self._devices or device_id not in self._layout.device_ids:
            raise KeyError(f"设备不可路由：{device_id}")
        previous = self._active_device_id
        if previous == device_id:
            return ()
        actions: list[RoutingAction] = []
        if previous != self._local_device_id:
            releases = self._pressed.release_events()
            if releases:
                actions.append(ReleaseTarget(previous, releases))
            self._pressed.clear()
        self._active_device_id = device_id
        self._blocked_edge = None
        actions.append(SwitchTarget(previous, device_id))
        if device_id == self._local_device_id:
            actions.append(RestoreLocalCursor())
        elif previous == self._local_device_id:
            actions.append(ConfineLocalCursor())
        return tuple(actions)

    def handle_disconnect(self, device_id: str) -> tuple[RoutingAction, ...]:
        if device_id != self._active_device_id:
            return ()
        return self.return_local()

    def activate_slot(self, slot: int) -> tuple[RoutingAction, ...]:
        device_id = self._layout.at(slot)
        return () if device_id is None else self.activate(device_id)

    def return_local(self) -> tuple[RoutingAction, ...]:
        return self.activate(self._local_device_id)

    def _update_hysteresis(
        self, event: MouseMoveEvent, width: int, height: int
    ) -> None:
        left_cleared = (
            self._blocked_edge == Direction.LEFT
            and event.position.x >= self._hysteresis
        )
        right_cleared = (
            self._blocked_edge == Direction.RIGHT
            and event.position.x <= width - 1 - self._hysteresis
        )
        up_cleared = (
            self._blocked_edge == Direction.UP
            and event.position.y >= self._hysteresis
        )
        down_cleared = (
            self._blocked_edge == Direction.DOWN
            and event.position.y <= height - 1 - self._hysteresis
        )
        if left_cleared or right_cleared or up_cleared or down_cleared:
            self._blocked_edge = None

    def _edge_neighbor(
        self, event: MouseMoveEvent, device_id: str, width: int, height: int
    ) -> tuple[str | None, Direction | None]:
        local = device_id == self._local_device_id
        candidates = (
            (event.position.x <= 0 if local else event.position.x < 0, Direction.LEFT),
            (
                event.position.x >= width - 1
                if local
                else event.position.x > width - 1,
                Direction.RIGHT,
            ),
            (event.position.y <= 0 if local else event.position.y < 0, Direction.UP),
            (
                event.position.y >= height - 1
                if local
                else event.position.y > height - 1,
                Direction.DOWN,
            ),
        )
        for reached, direction in candidates:
            if reached and self._blocked_edge != direction:
                neighbor = self._layout.neighbor(device_id, direction)
                if neighbor is not None:
                    return neighbor, direction
        return None, None

    @staticmethod
    def _map_to_target_edge(
        event: MouseMoveEvent,
        source: ScreenInfo,
        target: ScreenInfo,
        direction: Direction,
    ) -> Point:
        if direction in (Direction.LEFT, Direction.RIGHT):
            x = 0 if direction == Direction.RIGHT else target.width - 1
            y = map_horizontal_edge_position(source, target, event.position.y)
            return Point(x, y)
        x = map_vertical_edge_position(source, target, event.position.x)
        y = 0 if direction == Direction.DOWN else target.height - 1
        return Point(x, y)

    @staticmethod
    def _clamp_mouse(event: MouseMoveEvent, screen: ScreenInfo) -> MouseMoveEvent:
        return MouseMoveEvent(
            Point(
                min(max(event.position.x, 0), screen.width - 1),
                min(max(event.position.y, 0), screen.height - 1),
            ),
            injected=event.injected,
            timestamp=event.timestamp,
        )

    @staticmethod
    def _opposite(direction: Direction) -> Direction:
        return {
            Direction.LEFT: Direction.RIGHT,
            Direction.RIGHT: Direction.LEFT,
            Direction.UP: Direction.DOWN,
            Direction.DOWN: Direction.UP,
        }[direction]


__all__ = [
    "ConfineLocalCursor",
    "DeliverLocal",
    "ReleaseTarget",
    "RestoreLocalCursor",
    "RoutingAction",
    "RoutingController",
    "SendRemote",
    "SwitchTarget",
]
