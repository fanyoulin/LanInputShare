from __future__ import annotations

import asyncio


class QueuedTransferStream:
    """Adapts ordered control-message payloads to FileReceiver's stream API."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue()
        self._buffer = bytearray()
        self._ended = False

    def feed(self, data: bytes) -> None:
        if self._ended:
            return
        if data:
            self._queue.put_nowait(data)

    def finish(self) -> None:
        if not self._ended:
            self._ended = True
            self._queue.put_nowait(None)

    def abort(self, reason: str) -> None:
        if not self._ended:
            self._ended = True
            self._queue.put_nowait(ConnectionError(reason))

    async def write(self, data: bytes) -> None:
        self.feed(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    async def readexactly(self, size: int) -> bytes:
        if size < 0:
            raise ValueError("读取长度不能为负数")
        while len(self._buffer) < size:
            item = await self._queue.get()
            if isinstance(item, BaseException):
                raise item
            if item is None:
                partial = bytes(self._buffer)
                self._buffer.clear()
                raise asyncio.IncompleteReadError(partial, size)
            self._buffer.extend(item)
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result


__all__ = ["QueuedTransferStream"]
