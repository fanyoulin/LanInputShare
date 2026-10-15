from __future__ import annotations

from lan_input_share.input.events import (
    InputEvent,
    KeyEvent,
    MouseButton,
    MouseButtonEvent,
    MouseMoveEvent,
    MouseWheelEvent,
    Point,
)
from lan_input_share.protocol.messages import JsonValue


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


__all__ = ["decode_input_event", "encode_input_event"]
