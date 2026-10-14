from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from lan_input_share.transfer.manifest import FileManifest
from lan_input_share.transfer.protocol import TransferResult

type TransferCallable = Callable[
    [FileManifest, str, str], Awaitable[TransferResult]
]


@dataclass(frozen=True, slots=True)
class PasteOutcome:
    transfer_id: str
    success: bool
    suppressed: bool
    replayed: bool
    copy_only: bool = True
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _RemoteOffer:
    manifest: FileManifest
    source_id: str
    advertised_cut: bool


class RemoteFileClipboard:
    def __init__(
        self,
        transfer: TransferCallable,
        set_local_files: Callable[[tuple[Path, ...]], None],
        replay_paste: Callable[[], None],
    ) -> None:
        self._transfer = transfer
        self._set_local_files = set_local_files
        self._replay_paste = replay_paste
        self._offer: _RemoteOffer | None = None
        self._completed: set[str] = set()
        self._tasks: dict[str, asyncio.Future[TransferResult]] = {}

    def copied(
        self, manifest: FileManifest, source_id: str, *, cut: bool = False
    ) -> None:
        if not source_id:
            raise ValueError("远程文件来源不能为空")
        self._offer = _RemoteOffer(manifest, source_id, cut)

    async def handle_paste(self, target_id: str) -> PasteOutcome:
        offer = self._offer
        if offer is None:
            return PasteOutcome("", False, False, False, error="没有远程文件剪贴板")
        transfer_id = offer.manifest.transfer_id
        if target_id == offer.source_id:
            return PasteOutcome(
                transfer_id,
                False,
                False,
                False,
                error="文件剪贴板属于当前设备",
            )
        if transfer_id in self._completed:
            return PasteOutcome(
                transfer_id,
                False,
                True,
                False,
                error="该远程粘贴已经执行",
            )
        existing = self._tasks.get(transfer_id)
        if existing is not None:
            result = await existing
        else:
            task: asyncio.Future[TransferResult] = asyncio.ensure_future(
                self._transfer(offer.manifest, offer.source_id, target_id)
            )
            self._tasks[transfer_id] = task
            try:
                result = await task
            except asyncio.CancelledError:
                return PasteOutcome(
                    transfer_id,
                    False,
                    True,
                    False,
                    error="文件传输已取消",
                )
            finally:
                self._tasks.pop(transfer_id, None)
        if not result.success:
            return PasteOutcome(
                transfer_id,
                False,
                True,
                False,
                error=result.error or "文件传输失败",
            )
        self._set_local_files(result.paths)
        self._replay_paste()
        self._completed.add(transfer_id)
        return PasteOutcome(transfer_id, True, True, True)

    def cancel(self, transfer_id: str) -> None:
        task = self._tasks.get(transfer_id)
        if task is not None:
            task.cancel()


__all__ = ["PasteOutcome", "RemoteFileClipboard", "TransferCallable"]
