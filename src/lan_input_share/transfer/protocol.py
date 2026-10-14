from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

MAX_CHUNK_SIZE = 1024 * 1024
_MAX_METADATA_SIZE = 64 * 1024
_HEADER = struct.Struct(">BI")


class TransferProtocolError(ValueError):
    """Raised for invalid or out-of-order file channel data."""


class TransferStream(Protocol):
    async def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    async def readexactly(self, size: int) -> bytes: ...


class TransferWriter(Protocol):
    async def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TransferHeader:
    transfer_id: str
    session_id: str
    source_device_id: str


@dataclass(frozen=True, slots=True)
class StartFile:
    relative_path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class FileChunk:
    data: bytes


@dataclass(frozen=True, slots=True)
class EndFile:
    digest: str


@dataclass(frozen=True, slots=True)
class EndTransfer:
    pass


type TransferFrame = TransferHeader | StartFile | FileChunk | EndFile | EndTransfer


@dataclass(frozen=True, slots=True)
class TransferProgress:
    transfer_id: str
    relative_path: str
    bytes_transferred: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class TransferResult:
    transfer_id: str
    success: bool
    bytes_transferred: int
    files_completed: int
    paths: tuple[Path, ...] = ()
    error: str | None = None


async def write_frame(stream: TransferWriter, frame: TransferFrame) -> None:
    frame_type, payload = _encode(frame)
    if isinstance(frame, FileChunk) and len(payload) > MAX_CHUNK_SIZE:
        raise ValueError(f"文件分块超过 {MAX_CHUNK_SIZE} 字节上限")
    if not isinstance(frame, FileChunk) and len(payload) > _MAX_METADATA_SIZE:
        raise ValueError("文件通道元数据超过上限")
    await stream.write(_HEADER.pack(frame_type, len(payload)) + payload)
    await stream.drain()


async def read_frame(stream: TransferStream) -> TransferFrame:
    header = await stream.readexactly(_HEADER.size)
    frame_type, length = _HEADER.unpack(header)
    limit = MAX_CHUNK_SIZE if frame_type == 3 else _MAX_METADATA_SIZE
    if length > limit:
        raise TransferProtocolError(f"文件通道帧长度 {length} 超过上限 {limit}")
    payload = await stream.readexactly(length)
    return _decode(frame_type, payload)


def _encode(frame: TransferFrame) -> tuple[int, bytes]:
    if isinstance(frame, TransferHeader):
        return 1, _json_bytes(
            {
                "transfer_id": frame.transfer_id,
                "session_id": frame.session_id,
                "source_device_id": frame.source_device_id,
            }
        )
    if isinstance(frame, StartFile):
        return 2, _json_bytes(
            {
                "relative_path": frame.relative_path,
                "size": frame.size,
                "mtime_ns": frame.mtime_ns,
            }
        )
    if isinstance(frame, FileChunk):
        return 3, frame.data
    if isinstance(frame, EndFile):
        return 4, _json_bytes({"digest": frame.digest})
    if isinstance(frame, EndTransfer):
        return 5, b""
    raise TypeError(f"未知文件通道帧：{type(frame).__name__}")


def _decode(frame_type: int, payload: bytes) -> TransferFrame:
    if frame_type == 3:
        return FileChunk(payload)
    if frame_type == 5:
        if payload:
            raise TransferProtocolError("结束帧不能带数据")
        return EndTransfer()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransferProtocolError("文件通道元数据 JSON 无效") from exc
    if not isinstance(value, dict):
        raise TransferProtocolError("文件通道元数据必须是对象")
    mapping = cast(dict[str, object], value)
    try:
        if frame_type == 1:
            return TransferHeader(
                _text(mapping["transfer_id"]),
                _text(mapping["session_id"]),
                _text(mapping["source_device_id"]),
            )
        if frame_type == 2:
            return StartFile(
                _text(mapping["relative_path"]),
                _integer(mapping["size"]),
                _integer(mapping["mtime_ns"]),
            )
        if frame_type == 4:
            return EndFile(_text(mapping["digest"]))
    except (KeyError, ValueError) as exc:
        raise TransferProtocolError(f"文件通道元数据无效：{exc}") from exc
    raise TransferProtocolError(f"未知文件通道帧类型：{frame_type}")


def _json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("字段必须是非空字符串")
    return value


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("字段必须是非负整数")
    return value


__all__ = [
    "EndFile",
    "EndTransfer",
    "FileChunk",
    "MAX_CHUNK_SIZE",
    "StartFile",
    "TransferFrame",
    "TransferHeader",
    "TransferProgress",
    "TransferProtocolError",
    "TransferResult",
    "TransferStream",
    "TransferWriter",
    "read_frame",
    "write_frame",
]
