from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from lan_input_share.transfer.manifest import build_manifest
from lan_input_share.transfer.protocol import (
    EndFile,
    EndTransfer,
    FileChunk,
    StartFile,
    TransferHeader,
    write_frame,
)
from lan_input_share.transfer.receiver import FileReceiver
from lan_input_share.transfer.sender import FileSender
from lan_input_share.transfer.staging import StagingArea


class SinkStream:
    def __init__(self) -> None:
        self.data = bytearray()

    async def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)


class MemoryDuplex(SinkStream):
    def __init__(self) -> None:
        super().__init__()
        self.position = 0

    async def readexactly(self, size: int) -> bytes:
        end = self.position + size
        if end > len(self.data):
            raise asyncio.IncompleteReadError(bytes(self.data[self.position :]), size)
        result = bytes(self.data[self.position : end])
        self.position = end
        return result


@pytest.mark.asyncio
async def test_source_size_or_mtime_change_aborts_file(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("before", encoding="utf-8")
    manifest = build_manifest([source])
    source.write_text("changed-size", encoding="utf-8")
    sender = FileSender(session_id="session", source_device_id="source")

    result = await sender.send(manifest, SinkStream())

    assert result.success is False
    assert "发生变化" in (result.error or "")


@pytest.mark.asyncio
async def test_hash_mismatch_keeps_part_file_out_of_clipboard(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"data")
    manifest = build_manifest([source])
    staging = StagingArea(tmp_path / "staging")
    workspace = staging.reserve(manifest, 100)
    entry = next(item for item in manifest.entries if item.size)
    stream = MemoryDuplex()
    await write_frame(stream, TransferHeader(manifest.transfer_id, "session", "source"))
    await write_frame(stream, StartFile(entry.relative_path.as_posix(), 4, entry.mtime_ns))
    await write_frame(stream, FileChunk(b"data"))
    await write_frame(stream, EndFile("0" * 64))
    await write_frame(stream, EndTransfer())

    result = await FileReceiver(staging, expected_session_id="session").receive(
        manifest, stream, workspace
    )

    assert result.success is False
    assert result.paths == ()
    assert not (workspace.path / entry.relative_path).exists()


@pytest.mark.asyncio
async def test_interrupted_transfer_can_retry_from_a_clean_workspace(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"data")
    manifest = build_manifest([source])
    staging = StagingArea(tmp_path / "staging")
    workspace = staging.reserve(manifest, 100)
    entry = next(item for item in manifest.entries if item.size)
    interrupted = MemoryDuplex()
    await write_frame(
        interrupted, TransferHeader(manifest.transfer_id, "session", "source")
    )
    await write_frame(
        interrupted, StartFile(entry.relative_path.as_posix(), 4, entry.mtime_ns)
    )
    await write_frame(interrupted, FileChunk(b"da"))
    receiver = FileReceiver(staging, expected_session_id="session")
    first = await receiver.receive(manifest, interrupted, workspace)
    assert first.success is False

    retry = MemoryDuplex()
    await write_frame(retry, TransferHeader(manifest.transfer_id, "session", "source"))
    await write_frame(retry, StartFile(entry.relative_path.as_posix(), 4, entry.mtime_ns))
    await write_frame(retry, FileChunk(b"data"))
    await write_frame(retry, EndFile(hashlib.sha256(b"data").hexdigest()))
    await write_frame(retry, EndTransfer())
    second = await receiver.receive(manifest, retry, workspace)

    assert second.success is True
    assert (workspace.path / entry.relative_path).read_bytes() == b"data"
