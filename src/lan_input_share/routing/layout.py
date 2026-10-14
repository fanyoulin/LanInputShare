from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from lan_input_share.core.models import ScreenInfo, ScreenPosition


class Direction(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    UP = "up"
    DOWN = "down"


class RoutingLayout(Protocol):
    @property
    def device_ids(self) -> tuple[str, ...]: ...

    def neighbor(self, device_id: str, direction: Direction) -> str | None: ...

    def at(self, slot: int) -> str | None: ...

    def validate_connected(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LinearLayout:
    device_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.device_ids) <= 3:
            raise ValueError("屏幕布局必须包含 1 到 3 台设备")
        if any(not device_id.strip() for device_id in self.device_ids):
            raise ValueError("设备 ID 不能为空")
        if len(set(self.device_ids)) != len(self.device_ids):
            raise ValueError("屏幕布局不能包含重复设备")

    def left_of(self, device_id: str) -> str | None:
        index = self._index(device_id)
        return self.device_ids[index - 1] if index > 0 else None

    def right_of(self, device_id: str) -> str | None:
        index = self._index(device_id)
        return self.device_ids[index + 1] if index + 1 < len(self.device_ids) else None

    def at(self, slot: int) -> str | None:
        if not 0 <= slot < len(self.device_ids):
            return None
        return self.device_ids[slot]

    def neighbor(self, device_id: str, direction: Direction) -> str | None:
        if direction == Direction.LEFT:
            return self.left_of(device_id)
        if direction == Direction.RIGHT:
            return self.right_of(device_id)
        return None

    def validate_connected(self) -> None:
        return None

    def _index(self, device_id: str) -> int:
        try:
            return self.device_ids.index(device_id)
        except ValueError as exc:
            raise KeyError(f"设备不在屏幕布局中：{device_id}") from exc


class CrossLayout:
    _CANONICAL_ORDER = (
        ScreenPosition.TOP,
        ScreenPosition.LEFT,
        ScreenPosition.CENTER,
        ScreenPosition.RIGHT,
        ScreenPosition.BOTTOM,
    )
    _NEIGHBOR_POSITIONS = {
        (ScreenPosition.CENTER, Direction.UP): ScreenPosition.TOP,
        (ScreenPosition.CENTER, Direction.DOWN): ScreenPosition.BOTTOM,
        (ScreenPosition.CENTER, Direction.LEFT): ScreenPosition.LEFT,
        (ScreenPosition.CENTER, Direction.RIGHT): ScreenPosition.RIGHT,
        (ScreenPosition.TOP, Direction.DOWN): ScreenPosition.CENTER,
        (ScreenPosition.BOTTOM, Direction.UP): ScreenPosition.CENTER,
        (ScreenPosition.LEFT, Direction.RIGHT): ScreenPosition.CENTER,
        (ScreenPosition.RIGHT, Direction.LEFT): ScreenPosition.CENTER,
    }

    def __init__(self, positions: Mapping[ScreenPosition, str]) -> None:
        if not positions:
            raise ValueError("屏幕布局至少需要一台设备")
        if any(not device_id.strip() for device_id in positions.values()):
            raise ValueError("设备 ID 不能为空")
        if len(set(positions.values())) != len(positions):
            raise ValueError("屏幕布局不能包含重复设备")
        self._positions = dict(positions)
        self._device_positions = {
            device_id: position for position, device_id in self._positions.items()
        }

    @property
    def device_ids(self) -> tuple[str, ...]:
        return tuple(
            self._positions[position]
            for position in self._CANONICAL_ORDER
            if position in self._positions
        )

    def neighbor(self, device_id: str, direction: Direction) -> str | None:
        try:
            source_position = self._device_positions[device_id]
        except KeyError as exc:
            raise KeyError(f"设备不在屏幕布局中：{device_id}") from exc
        target_position = self._NEIGHBOR_POSITIONS.get((source_position, direction))
        return None if target_position is None else self._positions.get(target_position)

    def at_position(self, position: ScreenPosition) -> str | None:
        return self._positions.get(position)

    def at(self, slot: int) -> str | None:
        if not 0 <= slot < len(self.device_ids):
            return None
        return self.device_ids[slot]

    def validate_connected(self) -> None:
        if len(self._positions) <= 1:
            return
        if ScreenPosition.CENTER not in self._positions:
            raise ValueError("屏幕布局不连通：多台设备必须通过中间位置相邻")


def _map_axis_position(source_size: int, target_size: int, coordinate: int) -> int:
    clamped = min(max(coordinate, 0), source_size - 1)
    if source_size == 1:
        return 0
    return round(clamped * (target_size - 1) / (source_size - 1))


def map_horizontal_edge_position(
    source: ScreenInfo, target: ScreenInfo, y: int
) -> int:
    return _map_axis_position(source.height, target.height, y)


def map_vertical_edge_position(source: ScreenInfo, target: ScreenInfo, x: int) -> int:
    return _map_axis_position(source.width, target.width, x)


def map_edge_position(source: ScreenInfo, target: ScreenInfo, y: int) -> int:
    return map_horizontal_edge_position(source, target, y)


__all__ = [
    "CrossLayout",
    "Direction",
    "LinearLayout",
    "RoutingLayout",
    "map_edge_position",
    "map_horizontal_edge_position",
    "map_vertical_edge_position",
]
