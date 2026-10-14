from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from lan_input_share.transfer.destination import DestinationTransferWorkspace
from lan_input_share.transfer.manifest import build_manifest
from lan_input_share.transfer.preflight import PasteDecision, PasteDecisionResult
from lan_input_share.transfer.protocol import (
    EndFile,
    EndTransfer,
    FileChunk,
    StartFile,
    TransferHeader,
    write_frame,
)
from lan_input_share.transfer.receiver import FileReceiver


class MemoryDuplex:
    def __init__(self) -> None:
        self.data = bytearray()
        self.position = 0

    async def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    async def readexactly(self, size: int) -> bytes:
        end = self.position + size
        if end > len(self.data):
            raise asyncio.IncompleteReadError(bytes(self.data[self.position :]), size)
        result = bytes(self.data[self.position : end])
        self.position = end
        return result


@pytest.mark.asyncio
async def test_receiver_verifies_every_file_before_replacing_any_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "first.bin").write_bytes(b"new-first")
    (source / "second.bin").write_bytes(b"new-second")
    manifest = build_manifest([source])
    destination = tmp_path / "destination"
    target = destination / "source"
    target.mkdir(parents=True)
    (target / "first.bin").write_bytes(b"old-first")
    (target / "second.bin").write_bytes(b"old-second")
    decision = PasteDecisionResult(
        manifest.transfer_id, PasteDecision.OVERWRITE, (), False
    )
    workspace = DestinationTransferWorkspace.reserve(
        destination, manifest, decision
    )
    stream = MemoryDuplex()
    await write_frame(stream, TransferHeader(manifest.transfer_id, "session", "source"))
    files = [entry for entry in manifest.entries if entry.size]
    for index, entry in enumerate(files):
        content = (source / entry.relative_path.name).read_bytes()
        await write_frame(
            stream,
            StartFile(entry.relative_path.as_posix(), entry.size, entry.mtime_ns),
        )
        await write_frame(stream, FileChunk(content))
        digest = hashlib.sha256(content).hexdigest() if index == 0 else "0" * 64
        await write_frame(stream, EndFile(digest))
    await write_frame(stream, EndTransfer())

    result = await FileReceiver(expected_session_id="session").receive(
        manifest, stream, workspace
    )

    assert result.success is False
    assert (target / "first.bin").read_bytes() == b"old-first"
    assert (target / "second.bin").read_bytes() == b"old-second"
