from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class Point:
    x: int
    y: int


class Suppression(StrEnum):
    PASS_THROUGH = "pass_through"
    SUPPRESS = "suppress"
    SUPPRESS_ALWAYS = "suppress_always"


class MouseButton(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"
    X1 = "x1"
    X2 = "x2"


@dataclass(frozen=True, slots=True)
class KeyEvent:
    scan_code: int
    virtual_key: int
    is_down: bool
    is_extended: bool = False
    injected: bool = False
    timestamp: float = field(default_factory=time.monotonic, compare=False)


@dataclass(frozen=True, slots=True)
class MouseMoveEvent:
    position: Point
    injected: bool = False
    timestamp: float = field(default_factory=time.monotonic, compare=False)


@dataclass(frozen=True, slots=True)
class MouseButtonEvent:
    button: MouseButton
    is_down: bool
    injected: bool = False
    timestamp: float = field(default_factory=time.monotonic, compare=False)


@dataclass(frozen=True, slots=True)
class MouseWheelEvent:
    delta: int
    horizontal: bool = False
    injected: bool = False
    timestamp: float = field(default_factory=time.monotonic, compare=False)


type InputEvent = KeyEvent | MouseMoveEvent | MouseButtonEvent | MouseWheelEvent


__all__ = [
    "InputEvent",
    "KeyEvent",
    "MouseButton",
    "MouseButtonEvent",
    "MouseMoveEvent",
    "MouseWheelEvent",
    "Point",
    "Suppression",
]
