from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DeviceRole(StrEnum):
    CONTROLLER = "controller"
    CLIENT = "client"


class ScreenPosition(StrEnum):
    TOP = "top"
    BOTTOM = "bottom"
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


@dataclass(frozen=True, slots=True)
class ScreenInfo:
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("屏幕尺寸必须为正数")


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    port: int = 24870

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("主机地址不能为空")
        if not 1 <= self.port <= 65535:
            raise ValueError("端口必须在 1 到 65535 之间")


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    device_id: str
    name: str
    role: DeviceRole
    screen: ScreenInfo
    addresses: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.device_id.strip():
            raise ValueError("设备 ID 不能为空")
        if not self.name.strip():
            raise ValueError("设备名称不能为空")


@dataclass(frozen=True, slots=True)
class HotkeyConfig:
    switch_device_1: str = "Ctrl+Alt+1"
    switch_device_2: str = "Ctrl+Alt+2"
    switch_device_3: str = "Ctrl+Alt+3"
    emergency_return: str = "Ctrl+Alt+Esc"
