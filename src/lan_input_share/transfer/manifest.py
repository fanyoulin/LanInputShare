from __future__ import annotations

import ntpath
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_INVALID_WINDOWS_CHARACTERS = frozenset('<>:"|?*')
_RESERVED_WINDOWS_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class EntryKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"


@dataclass(frozen=True, slots=True)
class FileEntry:
    relative_path: PurePath
    size: int
    mtime_ns: int
    kind: EntryKind

    def __post_init__(self) -> None:
        validate_relative_path(self.relative_path)
        if self.size < 0 or self.mtime_ns < 0:
            raise ValueError("文件大小和修改时间不能为负数")
        if self.kind is EntryKind.DIRECTORY and self.size != 0:
            raise ValueError("目录条目的大小必须为零")


@dataclass(frozen=True, slots=True)
class FileManifest:
    transfer_id: str
    entries: tuple[FileEntry, ...]
    source_paths: Mapping[PurePath, Path] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.transfer_id:
            raise ValueError("传输 ID 不能为空")
        paths = [windows_path_key(entry.relative_path) for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise ValueError("文件清单包含重复路径")

    @property
    def total_size(self) -> int:
        return sum(entry.size for entry in self.entries if entry.kind is EntryKind.FILE)


def build_manifest(paths: Sequence[Path]) -> FileManifest:
    if not paths:
        raise ValueError("至少选择一个文件或目录")
    entries: list[FileEntry] = []
    source_paths: dict[PurePath, Path] = {}
    used_roots: set[str] = set()
    for source in paths:
        source = source.resolve(strict=True)
        root_name = _unique_root_name(source.name, used_roots)
        _walk_source(source, PurePosixPath(root_name), entries, source_paths)
    return FileManifest(str(uuid.uuid4()), tuple(entries), source_paths)


def validate_relative_path(path: PurePath) -> None:
    windows_path = PureWindowsPath(path.as_posix())
    if (
        path.is_absolute()
        or path.drive
        or path.root
        or windows_path.is_absolute()
        or windows_path.drive
        or windows_path.root
    ):
        raise ValueError("清单路径必须是相对路径")
    if not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("清单相对路径越界或无效")
    for part in path.parts:
        if part != part.rstrip(" ."):
            raise ValueError("清单路径包含 Windows 无效名称")
        if any(character in _INVALID_WINDOWS_CHARACTERS for character in part):
            raise ValueError("清单路径包含 Windows 无效字符")
        if any(ord(character) < 32 for character in part):
            raise ValueError("清单路径包含 Windows 无效字符")
        reserved_base = part.split(".", 1)[0].upper()
        if reserved_base in _RESERVED_WINDOWS_NAMES:
            raise ValueError("清单路径包含 Windows 保留名称")


def windows_path_key(path: PurePath) -> str:
    """Return a Windows case-insensitive key without Unicode normalization."""
    return ntpath.normcase(path.as_posix())


def _walk_source(
    source: Path,
    relative: PurePosixPath,
    entries: list[FileEntry],
    source_paths: dict[PurePath, Path],
) -> None:
    stat_result = source.stat(follow_symlinks=False)
    if source.is_symlink() or _is_reparse_point(stat_result):
        return
    if source.is_file():
        entries.append(
            FileEntry(relative, stat_result.st_size, stat_result.st_mtime_ns, EntryKind.FILE)
        )
        source_paths[relative] = source
        return
    if not source.is_dir():
        return
    entries.append(FileEntry(relative, 0, stat_result.st_mtime_ns, EntryKind.DIRECTORY))
    with os.scandir(source) as iterator:
        children = sorted(iterator, key=lambda item: item.name.casefold())
    for child in children:
        child_path = Path(child.path)
        try:
            child_stat = child.stat(follow_symlinks=False)
        except OSError:
            continue
        if child.is_symlink() or _is_reparse_point(child_stat):
            continue
        _walk_source(child_path, relative / child.name, entries, source_paths)


def _is_reparse_point(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _unique_root_name(name: str, used: set[str]) -> str:
    candidate = name
    counter = 2
    while candidate.casefold() in used:
        candidate = f"{name} ({counter})"
        counter += 1
    used.add(candidate.casefold())
    return candidate


__all__ = [
    "EntryKind",
    "FileEntry",
    "FileManifest",
    "build_manifest",
    "validate_relative_path",
    "windows_path_key",
]
