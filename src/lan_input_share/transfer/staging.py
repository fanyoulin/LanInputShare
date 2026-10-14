from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from lan_input_share.transfer.manifest import EntryKind, FileEntry, FileManifest


class StagingError(RuntimeError):
    """Raised when a transfer cannot be staged safely."""


@dataclass(slots=True)
class TransferWorkspace:
    manifest: FileManifest
    path: Path
    created_at: float
    active: bool = True
    clipboard_referenced: bool = False


class StagingArea:
    def __init__(self, root: Path, *, retention_seconds: float = 24 * 60 * 60) -> None:
        if retention_seconds <= 0:
            raise ValueError("暂存保留时间必须为正数")
        self._root = root
        self._retention_seconds = retention_seconds
        self._workspaces: dict[str, TransferWorkspace] = {}
        self._current: TransferWorkspace | None = None

    def reserve(
        self, manifest: FileManifest, available_bytes: int
    ) -> TransferWorkspace:
        if manifest.total_size > available_bytes:
            raise StagingError(
                f"暂存磁盘空间不足：需要 {manifest.total_size}，可用 {available_bytes}"
            )
        if manifest.transfer_id in self._workspaces:
            raise StagingError("传输暂存区已经存在")
        workspace_path = self._safe_workspace_path(manifest.transfer_id)
        if workspace_path.exists():
            raise StagingError("传输暂存目录已经存在")
        workspace_path.mkdir(parents=True)
        workspace = TransferWorkspace(manifest, workspace_path, time.time())
        try:
            for entry in manifest.entries:
                if entry.kind is EntryKind.DIRECTORY:
                    self._final_path(entry, workspace).mkdir(parents=True, exist_ok=True)
        except BaseException:
            shutil.rmtree(workspace_path, ignore_errors=True)
            raise
        self._workspaces[manifest.transfer_id] = workspace
        self._current = workspace
        return workspace

    def part_path(
        self, entry: FileEntry, workspace: TransferWorkspace | None = None
    ) -> Path:
        selected = self._select(workspace)
        if entry.kind is not EntryKind.FILE:
            raise StagingError("目录条目没有 .part 文件")
        final = self._final_path(entry, selected)
        part = final.with_name(final.name + ".part")
        self._assert_contained(part, selected.path)
        return part

    def commit(
        self,
        entry: FileEntry,
        digest: str,
        workspace: TransferWorkspace | None = None,
    ) -> Path:
        selected = self._select(workspace)
        part = self.part_path(entry, selected)
        if not part.is_file():
            raise StagingError(f"暂存分块不存在：{entry.relative_path.as_posix()}")
        actual_size = part.stat().st_size
        if actual_size != entry.size:
            raise StagingError(f"文件大小不匹配：预期 {entry.size}，实际 {actual_size}")
        actual_digest = _sha256(part)
        if actual_digest.casefold() != digest.casefold():
            raise StagingError("文件 SHA-256 校验失败")
        final = self._final_path(entry, selected)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, final)
        os.utime(final, ns=(entry.mtime_ns, entry.mtime_ns))
        return final

    def cleanup(self, now: float | None = None) -> tuple[str, ...]:
        current_time = time.time() if now is None else now
        removed: list[str] = []
        for transfer_id, workspace in tuple(self._workspaces.items()):
            expired = current_time - workspace.created_at >= self._retention_seconds
            if not expired or workspace.active or workspace.clipboard_referenced:
                continue
            self._assert_contained(workspace.path, self._root)
            shutil.rmtree(workspace.path, ignore_errors=False)
            self._workspaces.pop(transfer_id)
            if self._current is workspace:
                self._current = None
            removed.append(transfer_id)
        return tuple(sorted(removed))

    def cleanup_orphans(
        self,
        now: float | None = None,
        *,
        preserve: set[str] | None = None,
    ) -> tuple[str, ...]:
        if not self._root.exists():
            return ()
        current_time = time.time() if now is None else now
        preserved = preserve or set()
        removed: list[str] = []
        for child in self._root.iterdir():
            if (
                not child.is_dir()
                or child.name in preserved
                or child.name in self._workspaces
            ):
                continue
            try:
                self._assert_contained(child, self._root)
            except StagingError:
                continue
            if current_time - child.stat().st_mtime < self._retention_seconds:
                continue
            shutil.rmtree(child, ignore_errors=False)
            removed.append(child.name)
        return tuple(sorted(removed))

    def _select(self, workspace: TransferWorkspace | None) -> TransferWorkspace:
        selected = workspace or self._current
        if selected is None or selected.manifest.transfer_id not in self._workspaces:
            raise StagingError("没有有效的传输暂存区")
        return selected

    def _final_path(self, entry: FileEntry, workspace: TransferWorkspace) -> Path:
        if entry not in workspace.manifest.entries:
            raise StagingError("文件条目不属于该传输")
        destination = workspace.path.joinpath(*entry.relative_path.parts)
        self._assert_contained(destination, workspace.path)
        return destination

    def _safe_workspace_path(self, transfer_id: str) -> Path:
        if not transfer_id or transfer_id in (".", "..") or any(
            separator in transfer_id for separator in ("/", "\\")
        ):
            raise StagingError("传输 ID 不能包含路径")
        destination = self._root / transfer_id
        self._assert_contained(destination, self._root)
        return destination

    @staticmethod
    def _assert_contained(path: Path, root: Path) -> None:
        resolved_root = root.resolve(strict=False)
        resolved_path = path.resolve(strict=False)
        if not resolved_path.is_relative_to(resolved_root):
            raise StagingError("暂存路径越界")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = ["StagingArea", "StagingError", "TransferWorkspace"]
