from __future__ import annotations

import ntpath
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePath, PurePosixPath
from typing import cast

from lan_input_share.protocol.messages import JsonValue
from lan_input_share.transfer.manifest import (
    EntryKind,
    FileManifest,
    validate_relative_path,
    windows_path_key,
)
from lan_input_share.transfer.wire import manifest_from_payload, manifest_to_payload


class PasteDecision(StrEnum):
    OVERWRITE = "overwrite"
    SKIP = "skip"
    CANCEL = "cancel"


class ConflictKind(StrEnum):
    EXISTING_FILE = "existing_file"
    TYPE_MISMATCH = "type_mismatch"


@dataclass(frozen=True, slots=True)
class Conflict:
    relative_path: PurePosixPath
    kind: ConflictKind


@dataclass(frozen=True, slots=True)
class PreflightResult:
    conflicts: tuple[Conflict, ...]
    required_bytes: int

    def __post_init__(self) -> None:
        if self.required_bytes < 0:
            raise ValueError("预检所需字节数不能为负数")

    @property
    def conflict_count(self) -> int:
        return len(self.conflicts)

    @property
    def conflict_preview(self) -> tuple[Conflict, ...]:
        return self.conflicts[:5]


@dataclass(frozen=True, slots=True)
class PasteProbe:
    transfer_id: str
    source_id: str
    target_id: str
    manifest: FileManifest

    def __post_init__(self) -> None:
        if not self.transfer_id or not self.source_id or not self.target_id:
            raise ValueError("粘贴探测缺少 ID")
        if self.manifest.transfer_id != self.transfer_id:
            raise ValueError("粘贴探测与文件清单 ID 不一致")

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "transfer_id": self.transfer_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "manifest": manifest_to_payload(self.manifest),
        }

    @classmethod
    def from_payload(cls, payload: object) -> PasteProbe:
        mapping = _mapping(payload, "粘贴探测必须是对象")
        _require_keys(
            mapping,
            {"transfer_id", "source_id", "target_id", "manifest"},
            "粘贴探测包含未知字段或目标路径",
        )
        transfer_id = _required_id(mapping.get("transfer_id"), "传输")
        source_id = _required_id(mapping.get("source_id"), "源设备")
        target_id = _required_id(mapping.get("target_id"), "目标设备")
        manifest = manifest_from_payload(mapping.get("manifest"))
        return cls(transfer_id, source_id, target_id, manifest)


@dataclass(frozen=True, slots=True)
class PasteDecisionResult:
    transfer_id: str
    decision: PasteDecision
    skipped_paths: tuple[PurePosixPath, ...]
    compatibility: bool

    def __post_init__(self) -> None:
        if not self.transfer_id:
            raise ValueError("粘贴决定缺少传输 ID")
        for path in self.skipped_paths:
            validate_relative_path(path)

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "transfer_id": self.transfer_id,
            "decision": self.decision.value,
            "skipped_paths": [path.as_posix() for path in self.skipped_paths],
            "compatibility": self.compatibility,
        }

    @classmethod
    def from_payload(cls, payload: object) -> PasteDecisionResult:
        mapping = _mapping(payload, "粘贴决定必须是对象")
        _require_keys(
            mapping,
            {"transfer_id", "decision", "skipped_paths", "compatibility"},
            "粘贴决定包含未知字段或目标路径",
        )
        transfer_id = _required_id(mapping.get("transfer_id"), "传输")
        raw_decision = mapping.get("decision")
        if not isinstance(raw_decision, str):
            raise ValueError("粘贴决定无效")
        try:
            decision = PasteDecision(raw_decision)
        except ValueError as exc:
            raise ValueError("粘贴决定无效") from exc
        raw_skipped = mapping.get("skipped_paths")
        if not isinstance(raw_skipped, list) or not all(
            isinstance(path, str) and path for path in raw_skipped
        ):
            raise ValueError("粘贴决定的跳过路径无效")
        compatibility = mapping.get("compatibility")
        if not isinstance(compatibility, bool):
            raise ValueError("粘贴决定的兼容标志无效")
        skipped_paths = tuple(PurePosixPath(path) for path in raw_skipped)
        return cls(transfer_id, decision, skipped_paths, compatibility)


class PastePreflightService:
    def scan(self, destination: Path, manifest: FileManifest) -> PreflightResult:
        if not destination.is_dir():
            raise ValueError("目标目录不存在")
        conflicts: list[Conflict] = []
        for entry in manifest.entries:
            existing = _find_existing(destination, entry.relative_path)
            if existing is None:
                continue
            existing_kind = (
                EntryKind.DIRECTORY if existing.is_dir() else EntryKind.FILE
            )
            if existing_kind is not entry.kind:
                conflicts.append(
                    Conflict(
                        PurePosixPath(entry.relative_path.as_posix()),
                        ConflictKind.TYPE_MISMATCH,
                    )
                )
            elif entry.kind is EntryKind.FILE:
                conflicts.append(
                    Conflict(
                        PurePosixPath(entry.relative_path.as_posix()),
                        ConflictKind.EXISTING_FILE,
                    )
                )
        return PreflightResult(tuple(conflicts), manifest.total_size)


def filter_manifest(
    manifest: FileManifest,
    skipped_paths: tuple[PurePosixPath, ...],
) -> FileManifest:
    for path in skipped_paths:
        validate_relative_path(path)
    manifest_keys = {windows_path_key(entry.relative_path) for entry in manifest.entries}
    unknown = [path for path in skipped_paths if windows_path_key(path) not in manifest_keys]
    if unknown:
        raise ValueError("跳过路径不属于文件清单")
    kept_entries = tuple(
        entry for entry in manifest.entries if not _is_skipped(entry.relative_path, skipped_paths)
    )
    kept_sources = {
        path: source
        for path, source in manifest.source_paths.items()
        if not _is_skipped(path, skipped_paths)
    }
    return FileManifest(manifest.transfer_id, kept_entries, kept_sources)


def _find_existing(root: Path, relative_path: PurePath) -> Path | None:
    current = root
    for part in relative_path.parts:
        if not current.is_dir():
            return None
        matches = {
            ntpath.normcase(child.name): child
            for child in current.iterdir()
        }
        found = matches.get(ntpath.normcase(part))
        if found is None:
            return None
        current = found
    return current


def _is_skipped(path: PurePath, skipped_paths: tuple[PurePosixPath, ...]) -> bool:
    path_parts = tuple(ntpath.normcase(part) for part in path.parts)
    return any(
        path_parts[: len(skipped.parts)]
        == tuple(ntpath.normcase(part) for part in skipped.parts)
        for skipped in skipped_paths
    )


def _mapping(value: object, message: str) -> dict[object, object]:
    if not isinstance(value, dict):
        raise ValueError(message)
    return cast(dict[object, object], value)


def _required_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"粘贴消息缺少{label} ID")
    return value


def _require_keys(
    mapping: dict[object, object], expected: set[str], message: str
) -> None:
    if set(mapping) - expected:
        raise ValueError(message)


__all__ = [
    "Conflict",
    "ConflictKind",
    "PasteDecision",
    "PasteDecisionResult",
    "PastePreflightService",
    "PasteProbe",
    "PreflightResult",
    "filter_manifest",
]
