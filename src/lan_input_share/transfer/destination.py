from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePath

from lan_input_share.transfer.manifest import EntryKind, FileEntry, FileManifest
from lan_input_share.transfer.preflight import PasteDecision, PasteDecisionResult

_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class DestinationError(RuntimeError):
    """Raised when a direct destination transfer cannot remain transactional."""


@dataclass(frozen=True, slots=True)
class _InitialTarget:
    exists: bool
    kind: EntryKind | None


@dataclass
class DestinationWorkspace:
    destination: Path
    manifest: FileManifest
    decision: PasteDecisionResult
    path: Path
    created_at: float
    active: bool = True
    recovery_required: bool = False
    _initial: dict[PurePath, _InitialTarget] = field(default_factory=dict)
    _verified: set[PurePath] = field(default_factory=set)
    _actions: list[tuple[str, Path, Path | None]] = field(default_factory=list)

    @property
    def _new_root(self) -> Path:
        return self.path / "new"

    @property
    def _backup_root(self) -> Path:
        return self.path / "backup"

    @property
    def _journal_path(self) -> Path:
        return self.path / "commit.jsonl"

    def part_path(self, entry: FileEntry) -> Path:
        self._require_entry(entry)
        if entry.kind is not EntryKind.FILE:
            raise DestinationError("目录条目没有 .part 文件")
        staged = self._new_root.joinpath(*entry.relative_path.parts)
        part = staged.with_name(staged.name + ".part")
        self._assert_workspace_contained(part)
        return part

    def verify_and_stage(self, entry: FileEntry, digest: str) -> Path:
        part = self.part_path(entry)
        if not part.is_file():
            raise DestinationError("目标工作区分块不存在")
        actual_size = part.stat().st_size
        if actual_size != entry.size:
            raise DestinationError(
                f"文件大小不匹配：预期 {entry.size}，实际 {actual_size}"
            )
        if _sha256(part).casefold() != digest.casefold():
            raise DestinationError("文件 SHA-256 校验失败")
        staged = self._new_root.joinpath(*entry.relative_path.parts)
        self._assert_workspace_contained(staged)
        staged.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, staged)
        os.utime(staged, ns=(entry.mtime_ns, entry.mtime_ns))
        self._verified.add(entry.relative_path)
        return staged

    def commit(self) -> tuple[Path, ...]:
        files = tuple(
            entry for entry in self.manifest.entries if entry.kind is EntryKind.FILE
        )
        if any(entry.relative_path not in self._verified for entry in files):
            raise DestinationError("并非所有文件都已完成校验")
        try:
            self._validate_commit_snapshot()
            directories = sorted(
                (
                    entry
                    for entry in self.manifest.entries
                    if entry.kind is EntryKind.DIRECTORY
                ),
                key=lambda entry: len(entry.relative_path.parts),
            )
            for entry in directories:
                final = self._safe_final(entry.relative_path)
                if final.exists() and final.is_dir():
                    continue
                if final.exists():
                    self._backup_existing(entry, final)
                self._record("mkdir", final)
                final.mkdir(parents=True, exist_ok=False)
                self._actions.append(("mkdir", final, None))
            for entry in files:
                final = self._safe_final(entry.relative_path)
                if final.exists():
                    self._backup_existing(entry, final)
                staged = self._new_root.joinpath(*entry.relative_path.parts)
                if not staged.is_file():
                    raise DestinationError("已校验文件在提交前丢失")
                final.parent.mkdir(parents=True, exist_ok=True)
                self._record("install", final, staged)
                self._replace(staged, final)
                self._actions.append(("install", final, staged))
                os.utime(final, ns=(entry.mtime_ns, entry.mtime_ns))
            paths = self._top_level_paths()
            self.active = False
            self._actions.clear()
            self.cleanup()
            return paths
        except (OSError, RuntimeError, ValueError) as exc:
            try:
                self.rollback()
            except DestinationError:
                raise DestinationError(
                    "目标提交失败且回滚不完整，已保留恢复材料"
                ) from exc
            self.cleanup()
            if isinstance(exc, DestinationError):
                raise
            raise DestinationError("目标提交失败，原文件已恢复") from exc

    def rollback(self) -> None:
        try:
            for action, final, auxiliary in reversed(self._actions):
                if action == "install":
                    _remove_path(final)
                elif action == "mkdir":
                    try:
                        final.rmdir()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        if any(final.iterdir()):
                            continue
                        raise
                elif action == "backup":
                    assert auxiliary is not None
                    _remove_path(final)
                    final.parent.mkdir(parents=True, exist_ok=True)
                    if auxiliary.exists():
                        self._replace(auxiliary, final)
            self._actions.clear()
            self.active = False
        except (OSError, RuntimeError, ValueError) as exc:
            self.recovery_required = True
            self.active = False
            with suppress(OSError):
                (self.path / "RECOVERY_REQUIRED.txt").write_text(
                    "自动回滚未完成，请保留此目录并人工恢复。",
                    encoding="utf-8",
                )
            raise DestinationError("回滚不完整") from exc

    def cleanup(self) -> None:
        if self.recovery_required or not self.path.exists():
            return
        self._assert_workspace_contained(self.path)
        shutil.rmtree(self.path, ignore_errors=False)

    def _validate_commit_snapshot(self) -> None:
        if not self.destination.exists() or not self.destination.is_dir():
            raise DestinationError("目标目录已消失")
        if self.destination.resolve(strict=True) != self.destination:
            raise DestinationError("目标目录已变化")
        for entry in self.manifest.entries:
            final = self._safe_final(entry.relative_path)
            initial = self._initial[entry.relative_path]
            exists = final.exists()
            if exists and not initial.exists:
                raise DestinationError("预检后出现未经授权的新增冲突")
            if initial.exists and not exists:
                raise DestinationError("预检后的目标内容已变化")
            if initial.exists and exists:
                kind = EntryKind.DIRECTORY if final.is_dir() else EntryKind.FILE
                if kind is not initial.kind:
                    raise DestinationError("预检后的目标类型已变化")

    def _backup_existing(self, entry: FileEntry, final: Path) -> None:
        backup = self._backup_root.joinpath(*entry.relative_path.parts)
        self._assert_workspace_contained(backup)
        backup.parent.mkdir(parents=True, exist_ok=True)
        self._record("backup", final, backup)
        self._replace(final, backup)
        self._actions.append(("backup", final, backup))

    def _safe_final(self, relative_path: PurePath) -> Path:
        final = self.destination.joinpath(*relative_path.parts)
        resolved_destination = self.destination.resolve(strict=True)
        if resolved_destination != self.destination:
            raise DestinationError("目标目录已变化")
        current = self.destination
        for part in relative_path.parts:
            current = current / part
            if current.exists() and _is_reparse_point(current):
                raise DestinationError("目标路径包含链接或重解析点")
        existing_parent = final if final.exists() and final.is_dir() else final.parent
        while not existing_parent.exists() and existing_parent != self.destination:
            existing_parent = existing_parent.parent
        resolved_parent = existing_parent.resolve(strict=True)
        if not resolved_parent.is_relative_to(resolved_destination):
            raise DestinationError("目标路径越界")
        return final

    def _assert_workspace_contained(self, path: Path) -> None:
        if not path.resolve(strict=False).is_relative_to(self.path.resolve(strict=False)):
            raise DestinationError("目标工作区路径越界")

    def _require_entry(self, entry: FileEntry) -> None:
        if entry not in self.manifest.entries:
            raise DestinationError("文件条目不属于该传输")

    def _record(self, action: str, final: Path, auxiliary: Path | None = None) -> None:
        record = {
            "action": action,
            "relative": final.relative_to(self.destination).as_posix(),
            "auxiliary": None
            if auxiliary is None
            else auxiliary.relative_to(self.path).as_posix(),
        }
        with self._journal_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _replace(self, source: Path, target: Path) -> None:
        os.replace(source, target)

    def _top_level_paths(self) -> tuple[Path, ...]:
        names: list[str] = []
        for entry in self.manifest.entries:
            root = entry.relative_path.parts[0]
            if root not in names:
                names.append(root)
        return tuple(self.destination / name for name in names)


class DestinationTransferWorkspace:
    @staticmethod
    def reserve(
        destination: Path,
        manifest: FileManifest,
        decision: PasteDecisionResult,
    ) -> DestinationWorkspace:
        if decision.transfer_id != manifest.transfer_id:
            raise DestinationError("目标决定与文件清单 ID 不一致")
        if decision.decision is PasteDecision.CANCEL or decision.compatibility:
            raise DestinationError("该决定不能使用资源管理器直写工作区")
        try:
            resolved = destination.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise DestinationError("目标目录不存在") from exc
        if not resolved.is_dir() or _is_reparse_point(destination):
            raise DestinationError("目标目录不是安全的物理目录")
        if not manifest.transfer_id or any(
            separator in manifest.transfer_id for separator in ("/", "\\")
        ):
            raise DestinationError("传输 ID 不能包含路径")
        if shutil.disk_usage(resolved).free < manifest.total_size:
            raise DestinationError("目标磁盘空间不足")
        path = resolved / f".LanInputShare-{manifest.transfer_id}.part"
        if path.exists():
            raise DestinationError("目标传输工作区已经存在")
        initial: dict[PurePath, _InitialTarget] = {}
        probe = DestinationWorkspace(
            resolved,
            manifest,
            decision,
            path,
            __import__("time").time(),
        )
        for entry in manifest.entries:
            final = probe._safe_final(entry.relative_path)
            exists = final.exists()
            kind = (
                EntryKind.DIRECTORY if exists and final.is_dir() else EntryKind.FILE
            ) if exists else None
            initial[entry.relative_path] = _InitialTarget(exists, kind)
        try:
            path.mkdir()
            (path / "new").mkdir()
            (path / "backup").mkdir()
            _mark_hidden(path)
        except OSError as exc:
            shutil.rmtree(path, ignore_errors=True)
            raise DestinationError("无法创建目标传输工作区") from exc
        probe._initial = initial
        return probe


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _mark_hidden(path: Path) -> None:
    if sys.platform != "win32":
        return
    kernel32 = ctypes.windll.kernel32
    attributes = int(kernel32.GetFileAttributesW(str(path)))
    if attributes == -1 or not kernel32.SetFileAttributesW(
        str(path), attributes | _FILE_ATTRIBUTE_HIDDEN
    ):
        raise ctypes.WinError()


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


__all__ = [
    "DestinationError",
    "DestinationTransferWorkspace",
    "DestinationWorkspace",
]
