from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Awaitable, Callable

from lan_input_share.transfer.manifest import EntryKind, FileManifest
from lan_input_share.transfer.protocol import (
    MAX_CHUNK_SIZE,
    EndFile,
    EndTransfer,
    FileChunk,
    StartFile,
    TransferHeader,
    TransferProgress,
    TransferResult,
    TransferWriter,
    write_frame,
)

type ProgressCallback = Callable[[TransferProgress], None | Awaitable[None]]


class FileSender:
    def __init__(
        self,
        *,
        session_id: str,
        source_device_id: str,
        chunk_size: int = 256 * 1024,
    ) -> None:
        if not session_id or not source_device_id:
            raise ValueError("文件通道身份不能为空")
        if not 1 <= chunk_size <= MAX_CHUNK_SIZE:
            raise ValueError("文件分块大小无效")
        self._session_id = session_id
        self._source_device_id = source_device_id
        self._chunk_size = chunk_size
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    async def send(
        self,
        manifest: FileManifest,
        stream: TransferWriter,
        progress: ProgressCallback | None = None,
    ) -> TransferResult:
        transferred = 0
        completed = 0
        self._cancelled = False
        try:
            await write_frame(
                stream,
                TransferHeader(
                    manifest.transfer_id,
                    self._session_id,
                    self._source_device_id,
                ),
            )
            for entry in manifest.entries:
                if entry.kind is not EntryKind.FILE:
                    continue
                if self._cancelled:
                    raise RuntimeError("文件传输已取消")
                source = manifest.source_paths.get(entry.relative_path)
                if source is None:
                    raise RuntimeError(f"缺少源文件映射：{entry.relative_path.as_posix()}")
                before = source.stat()
                if before.st_size != entry.size or before.st_mtime_ns != entry.mtime_ns:
                    raise RuntimeError(f"源文件在传输前发生变化：{source}")
                await write_frame(
                    stream,
                    StartFile(entry.relative_path.as_posix(), entry.size, entry.mtime_ns),
                )
                digest = hashlib.sha256()
                with source.open("rb") as input_file:
                    while chunk := input_file.read(self._chunk_size):
                        if self._cancelled:
                            raise RuntimeError("文件传输已取消")
                        digest.update(chunk)
                        await write_frame(stream, FileChunk(chunk))
                        transferred += len(chunk)
                        await _notify(
                            progress,
                            TransferProgress(
                                manifest.transfer_id,
                                entry.relative_path.as_posix(),
                                transferred,
                                manifest.total_size,
                            ),
                        )
                        await asyncio.sleep(0)
                after = source.stat()
                if after.st_size != entry.size or after.st_mtime_ns != entry.mtime_ns:
                    raise RuntimeError(f"源文件在传输中发生变化：{source}")
                await write_frame(stream, EndFile(digest.hexdigest()))
                completed += 1
            await write_frame(stream, EndTransfer())
            return TransferResult(manifest.transfer_id, True, transferred, completed)
        except (OSError, RuntimeError, ValueError, asyncio.IncompleteReadError) as exc:
            return TransferResult(
                manifest.transfer_id,
                False,
                transferred,
                completed,
                error=str(exc),
            )


async def _notify(
    callback: ProgressCallback | None, progress: TransferProgress
) -> None:
    if callback is None:
        return
    result = callback(progress)
    if inspect.isawaitable(result):
        await result


__all__ = ["FileSender", "ProgressCallback"]
