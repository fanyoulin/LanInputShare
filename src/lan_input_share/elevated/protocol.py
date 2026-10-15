from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import cast

from lan_input_share.elevated.models import HELPER_PROTOCOL_VERSION, HelperState
from lan_input_share.input.codec import decode_input_event, encode_input_event
from lan_input_share.input.events import InputEvent
from lan_input_share.protocol.messages import JsonValue

MAX_HELPER_FRAME_BYTES = 65_536
_HEADER = struct.Struct(">I")


class HelperProtocolError(ValueError):
    """Raised when local helper traffic violates the fixed protocol."""


@dataclass(frozen=True, slots=True)
class Hello:
    sequence: int
    protocol_version: int
    client_pid: int


@dataclass(frozen=True, slots=True)
class Input:
    sequence: int
    event: InputEvent


@dataclass(frozen=True, slots=True)
class ReleaseAll:
    sequence: int


@dataclass(frozen=True, slots=True)
class Ping:
    sequence: int


@dataclass(frozen=True, slots=True)
class Status:
    sequence: int
    state: HelperState
    detail: str


@dataclass(frozen=True, slots=True)
class ShutdownIfIdle:
    sequence: int


type HelperMessage = Hello | Input | ReleaseAll | Ping | Status | ShutdownIfIdle


def encode_helper_message(message: HelperMessage) -> bytes:
    mapping = _message_to_mapping(message)
    body = json.dumps(mapping, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_HELPER_FRAME_BYTES:
        raise HelperProtocolError(
            f"助手消息长度 {len(body)} 超过限制 {MAX_HELPER_FRAME_BYTES}"
        )
    return _HEADER.pack(len(body)) + body


class HelperFrameDecoder:
    def __init__(self) -> None:
        self._buffer = bytearray()
        self._poisoned = False

    def feed(self, data: bytes) -> tuple[HelperMessage, ...]:
        if self._poisoned:
            raise HelperProtocolError("解码器发生过协议错误，不可继续使用")
        self._buffer.extend(data)
        messages: list[HelperMessage] = []
        try:
            while len(self._buffer) >= _HEADER.size:
                (length,) = _HEADER.unpack_from(self._buffer)
                if length == 0:
                    raise HelperProtocolError("助手消息长度不能为零")
                if length > MAX_HELPER_FRAME_BYTES:
                    raise HelperProtocolError(
                        f"助手消息长度 {length} 超过限制 {MAX_HELPER_FRAME_BYTES}"
                    )
                frame_size = _HEADER.size + length
                if len(self._buffer) < frame_size:
                    break
                raw = bytes(self._buffer[_HEADER.size:frame_size])
                del self._buffer[:frame_size]
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise HelperProtocolError(f"助手消息 JSON 无效：{exc}") from exc
                messages.append(_message_from_mapping(value))
        except HelperProtocolError:
            self._buffer.clear()
            self._poisoned = True
            raise
        return tuple(messages)


def _message_to_mapping(message: HelperMessage) -> dict[str, JsonValue]:
    _validate_sequence(message.sequence)
    payload: dict[str, JsonValue]
    if isinstance(message, Hello):
        kind = "hello"
        payload = {
            "protocol_version": message.protocol_version,
            "client_pid": message.client_pid,
        }
    elif isinstance(message, Input):
        kind = "input"
        payload = encode_input_event(message.event)
    elif isinstance(message, ReleaseAll):
        kind = "release_all"
        payload = {}
    elif isinstance(message, Ping):
        kind = "ping"
        payload = {}
    elif isinstance(message, Status):
        kind = "status"
        payload = {"state": message.state.value, "detail": message.detail}
    elif isinstance(message, ShutdownIfIdle):
        kind = "shutdown_if_idle"
        payload = {}
    else:
        raise TypeError(f"不支持的助手消息：{type(message).__name__}")
    return {
        "version": HELPER_PROTOCOL_VERSION,
        "kind": kind,
        "sequence": message.sequence,
        "payload": payload,
    }


def _message_from_mapping(value: object) -> HelperMessage:
    mapping = _string_mapping(value, "根节点")
    _require_exact_keys(mapping, {"version", "kind", "sequence", "payload"}, "根节点")
    if _integer(mapping["version"], "version") != HELPER_PROTOCOL_VERSION:
        raise HelperProtocolError("助手协议版本不一致")
    kind = _string(mapping["kind"], "kind")
    sequence = _integer(mapping["sequence"], "sequence")
    _validate_sequence(sequence)
    payload = _string_mapping(mapping["payload"], "payload")
    try:
        if kind == "hello":
            _require_exact_keys(payload, {"protocol_version", "client_pid"}, "hello")
            return Hello(
                sequence,
                _integer(payload["protocol_version"], "protocol_version"),
                _positive_integer(payload["client_pid"], "client_pid"),
            )
        if kind == "input":
            return Input(sequence, decode_input_event(cast(dict[str, JsonValue], payload)))
        if kind in {"release_all", "ping", "shutdown_if_idle"}:
            _require_exact_keys(payload, set(), kind)
            if kind == "release_all":
                return ReleaseAll(sequence)
            if kind == "ping":
                return Ping(sequence)
            return ShutdownIfIdle(sequence)
        if kind == "status":
            _require_exact_keys(payload, {"state", "detail"}, "status")
            return Status(
                sequence,
                HelperState(_string(payload["state"], "state")),
                _string(payload["detail"], "detail"),
            )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, HelperProtocolError):
            raise
        raise HelperProtocolError(f"助手消息 {kind} 内容无效：{exc}") from exc
    raise HelperProtocolError(f"未知助手消息类型：{kind}")


def _string_mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise HelperProtocolError(f"{field} 必须是字符串键对象")
    return cast(dict[str, object], value)


def _require_exact_keys(
    mapping: dict[str, object], expected: set[str], field: str
) -> None:
    if set(mapping) != expected:
        raise HelperProtocolError(f"{field} 字段无效")


def _integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise HelperProtocolError(f"{field} 必须是整数")
    return value


def _positive_integer(value: object, field: str) -> int:
    result = _integer(value, field)
    if result <= 0:
        raise HelperProtocolError(f"{field} 必须为正数")
    return result


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise HelperProtocolError(f"{field} 必须是字符串")
    return value


def _validate_sequence(sequence: int) -> None:
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise HelperProtocolError("sequence 必须是非负整数")


__all__ = [
    "HELPER_PROTOCOL_VERSION",
    "MAX_HELPER_FRAME_BYTES",
    "Hello",
    "HelperFrameDecoder",
    "HelperMessage",
    "HelperProtocolError",
    "Input",
    "Ping",
    "ReleaseAll",
    "ShutdownIfIdle",
    "Status",
    "encode_helper_message",
]
