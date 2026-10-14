from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lan_input_share.transfer.manifest import build_manifest
from lan_input_share.transfer.receiver import FileReceiver
from lan_input_share.transfer.sender import FileSender
from lan_input_share.transfer.staging import StagingArea


class NetworkStream:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    async def readexactly(self, size: int) -> bytes:
        return await self.reader.readexactly(size)

    async def write(self, data: bytes) -> None:
        self.writer.write(data)

    async def drain(self) -> None:
        await self.writer.drain()


@pytest.mark.asyncio
async def test_file_channel_sends_chunks_without_delaying_control_heartbeat(
    tmp_path: Path,
) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"abcd" * 300_000)
    manifest = build_manifest([source])
    staging = StagingArea(tmp_path / "staging")
    workspace = staging.reserve(manifest, available_bytes=10_000_000)
    receiver_result = None

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal receiver_result
        receiver = FileReceiver(staging, expected_session_id="session")
        receiver_result = await receiver.receive(manifest, NetworkStream(reader, writer), workspace)
        writer.close()

    server = await asyncio.start_server(receive, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    ticks = 0
    running = True

    async def heartbeat() -> None:
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0)

    heartbeat_task = asyncio.create_task(heartbeat())
    sender = FileSender(session_id="session", source_device_id="source", chunk_size=16_384)
    result = await sender.send(manifest, NetworkStream(reader, writer))
    await asyncio.sleep(0.05)
    running = False
    await heartbeat_task
    writer.close()
    await writer.wait_closed()
    server.close()
    await server.wait_closed()

    assert result.success
    assert receiver_result is not None and receiver_result.success
    assert ticks > 10
    assert (workspace.path / "large.bin").read_bytes() == source.read_bytes()
