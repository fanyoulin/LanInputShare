from __future__ import annotations

import asyncio
import inspect
import shutil
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from lan_input_share.transfer.destination import DestinationWorkspace
from lan_input_share.transfer.manifest import EntryKind, FileEntry, FileManifest
from lan_input_share.transfer.protocol import (
    EndFile,
    EndTransfer,
    FileChunk,
    StartFile,
    TransferHeader,
    TransferProgress,
    TransferProtocolError,
    TransferResult,
    TransferStream,
    read_frame,
)
from lan_input_share.transfer.staging import StagingArea, TransferWorkspace

type ProgressCallback = Callable[[TransferProgress], None | Awaitable[None]]


class FileReceiver:
    def __init__(
        self,
        staging: StagingArea | None = None,
        *,
        expected_session_id: str,
    ) -> None:
        self._staging = staging
        self._expected_session_id = expected_session_id
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    async def receive(
        self,
        manifest: FileManifest,
        stream: TransferStream,
        workspace: TransferWorkspace | DestinationWorkspace,
        progress: ProgressCallback | None = None,
    ) -> TransferResult:
        transferred = 0
        completed = 0
        self._cancelled = False
        try:
            header = await read_frame(stream)
            if not isinstance(header, TransferHeader):
                raise TransferProtocolError("文件通道缺少认证头")
            if (
                header.transfer_id != manifest.transfer_id
                or header.session_id != self._expected_session_id
            ):
                raise TransferProtocolError("文件通道身份与控制会话不匹配")
            files = [entry for entry in manifest.entries if entry.kind is EntryKind.FILE]
            for entry in files:
                if self._cancelled:
                    raise RuntimeError("文件传输已取消")
                start = await read_frame(stream)
                if not isinstance(start, StartFile) or not _matches(start, entry):
                    raise TransferProtocolError("文件开始帧与清单不匹配")
                part = self._part_path(entry, workspace)
                part.parent.mkdir(parents=True, exist_ok=True)
                received_for_file = 0
                with part.open("wb") as output:
                    while True:
                        if self._cancelled:
                            raise RuntimeError("文件传输已取消")
                        frame = await read_frame(stream)
                        if isinstance(frame, FileChunk):
                            received_for_file += len(frame.data)
                            if received_for_file > entry.size:
                                raise TransferProtocolError("接收文件超过清单大小")
                            output.write(frame.data)
                            transferred += len(frame.data)
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
                        elif isinstance(frame, EndFile):
                            digest = frame.digest
                            break
                        else:
                            raise TransferProtocolError("文件帧顺序无效")
                self._verify_and_stage(entry, digest, workspace)
                completed += 1
            if not isinstance(await read_frame(stream), EndTransfer):
                raise TransferProtocolError("文件传输缺少结束帧")
            if isinstance(workspace, DestinationWorkspace):
                paths = workspace.commit()
            else:
                workspace.active = False
                paths = _top_level_paths(manifest, workspace)
            return TransferResult(
                manifest.transfer_id,
                True,
                transferred,
                completed,
                paths,
            )
        except asyncio.CancelledError:
            self._clean_failed_workspace(workspace)
            raise
        except (
            OSError,
            RuntimeError,
            ValueError,
            asyncio.IncompleteReadError,
        ) as exc:
            self._clean_failed_workspace(workspace)
            return TransferResult(
                manifest.transfer_id,
                False,
                transferred,
                completed,
                error=str(exc),
            )

    @staticmethod
    def _clean_failed_workspace(
        workspace: TransferWorkspace | DestinationWorkspace,
    ) -> None:
        if isinstance(workspace, DestinationWorkspace):
            with suppress(RuntimeError):
                workspace.rollback()
            workspace.cleanup()
        else:
            _clean_workspace(workspace)

    def _part_path(
        self, entry: FileEntry, workspace: TransferWorkspace | DestinationWorkspace
    ) -> Path:
        if isinstance(workspace, DestinationWorkspace):
            return workspace.part_path(entry)
        if self._staging is None:
            raise RuntimeError("兼容暂存接收器缺少 StagingArea")
        return self._staging.part_path(entry, workspace)

    def _verify_and_stage(
        self,
        entry: FileEntry,
        digest: str,
        workspace: TransferWorkspace | DestinationWorkspace,
    ) -> None:
        if isinstance(workspace, DestinationWorkspace):
            workspace.verify_and_stage(entry, digest)
            return
        if self._staging is None:
            raise RuntimeError("兼容暂存接收器缺少 StagingArea")
        self._staging.commit(entry, digest, workspace)


def _matches(start: StartFile, entry: FileEntry) -> bool:
    return (
        start.relative_path == entry.relative_path.as_posix()
        and start.size == entry.size
        and start.mtime_ns == entry.mtime_ns
    )


async def _notify(
    callback: ProgressCallback | None, progress: TransferProgress
) -> None:
    if callback is None:
        return
    result = callback(progress)
    if inspect.isawaitable(result):
        await result


def _top_level_paths(
    manifest: FileManifest, workspace: TransferWorkspace
) -> tuple[Path, ...]:
    names: list[str] = []
    for entry in manifest.entries:
        root = entry.relative_path.parts[0]
        if root not in names:
            names.append(root)
    return tuple(workspace.path / name for name in names)


def _clean_workspace(workspace: TransferWorkspace) -> None:
    for child in workspace.path.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    for entry in workspace.manifest.entries:
        if entry.kind is EntryKind.DIRECTORY:
            workspace.path.joinpath(*entry.relative_path.parts).mkdir(
                parents=True, exist_ok=True
            )


__all__ = ["FileReceiver", "ProgressCallback"]
