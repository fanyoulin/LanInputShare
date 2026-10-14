from __future__ import annotations

import asyncio

import pytest

from lan_input_share.transfer.control_stream import QueuedTransferStream


@pytest.mark.asyncio
async def test_control_stream_reads_exactly_across_message_boundaries() -> None:
    stream = QueuedTransferStream()
    stream.feed(b"ab")
    stream.feed(b"cdef")

    assert await stream.readexactly(3) == b"abc"
    assert await stream.readexactly(3) == b"def"


@pytest.mark.asyncio
async def test_control_stream_abort_unblocks_receiver() -> None:
    stream = QueuedTransferStream()
    pending = asyncio.create_task(stream.readexactly(4))
    await asyncio.sleep(0)

    stream.abort("network failed")

    with pytest.raises(ConnectionError, match="network failed"):
        await pending


@pytest.mark.asyncio
async def test_control_stream_eof_reports_incomplete_read() -> None:
    stream = QueuedTransferStream()
    stream.feed(b"ab")
    stream.finish()

    with pytest.raises(asyncio.IncompleteReadError):
        await stream.readexactly(3)
