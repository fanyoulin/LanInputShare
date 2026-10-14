from __future__ import annotations

import asyncio

import pytest

from lan_input_share.transfer.protocol import (
    MAX_CHUNK_SIZE,
    FileChunk,
    TransferHeader,
    read_frame,
    write_frame,
)


class MemoryStream:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.position = 0

    async def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    async def readexactly(self, size: int) -> bytes:
        end = self.position + size
        if end > len(self.buffer):
            raise asyncio.IncompleteReadError(bytes(self.buffer[self.position :]), size)
        result = bytes(self.buffer[self.position : end])
        self.position = end
        return result


@pytest.mark.asyncio
async def test_transfer_header_round_trips_session_and_transfer_identity() -> None:
    stream = MemoryStream()
    header = TransferHeader("transfer", "control-session", "source-device")

    await write_frame(stream, header)

    assert await read_frame(stream) == header


@pytest.mark.asyncio
async def test_file_chunk_is_bounded() -> None:
    stream = MemoryStream()

    with pytest.raises(ValueError, match="上限"):
        await write_frame(stream, FileChunk(b"x" * (MAX_CHUNK_SIZE + 1)))
