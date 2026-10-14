from __future__ import annotations

import json
import struct

from lan_input_share.protocol.messages import (
    ProtocolError,
    WireMessage,
    message_from_mapping,
)

_HEADER = struct.Struct(">I")


def encode_frame(message: WireMessage) -> bytes:
    body = json.dumps(
        message.to_mapping(),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _HEADER.pack(len(body)) + body


class FrameDecoder:
    def __init__(self, max_payload: int = 1_048_576) -> None:
        if max_payload <= 0:
            raise ValueError("max_payload 必须为正数")
        self._max_payload = max_payload
        self._buffer = bytearray()
        self._poisoned = False

    def feed(self, data: bytes) -> list[WireMessage]:
        if self._poisoned:
            raise ProtocolError("解码器发生过协议错误，不可继续使用")
        self._buffer.extend(data)
        messages: list[WireMessage] = []
        try:
            while len(self._buffer) >= _HEADER.size:
                (length,) = _HEADER.unpack_from(self._buffer)
                if length == 0:
                    raise ProtocolError("消息长度不能为零")
                if length > self._max_payload:
                    raise ProtocolError(f"消息长度 {length} 超过限制 {self._max_payload}")
                frame_size = _HEADER.size + length
                if len(self._buffer) < frame_size:
                    break
                body = bytes(self._buffer[_HEADER.size:frame_size])
                del self._buffer[:frame_size]
                try:
                    mapping = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProtocolError(f"消息 JSON 无效：{exc}") from exc
                messages.append(message_from_mapping(mapping))
        except ProtocolError:
            self._buffer.clear()
            self._poisoned = True
            raise
        return messages


__all__ = ["FrameDecoder", "ProtocolError", "encode_frame"]
