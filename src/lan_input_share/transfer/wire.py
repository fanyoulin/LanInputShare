from __future__ import annotations

from pathlib import PurePosixPath
from typing import cast

from lan_input_share.protocol.messages import JsonValue
from lan_input_share.transfer.manifest import EntryKind, FileEntry, FileManifest


def manifest_to_payload(manifest: FileManifest) -> dict[str, JsonValue]:
    return {
        "transfer_id": manifest.transfer_id,
        "entries": [
            {
                "relative_path": entry.relative_path.as_posix(),
                "size": entry.size,
                "mtime_ns": entry.mtime_ns,
                "kind": entry.kind.value,
            }
            for entry in manifest.entries
        ],
    }


def manifest_from_payload(payload: object) -> FileManifest:
    if not isinstance(payload, dict):
        raise ValueError("文件清单必须是对象")
    mapping = cast(dict[object, object], payload)
    transfer_id = mapping.get("transfer_id")
    raw_entries = mapping.get("entries")
    if not isinstance(transfer_id, str) or not transfer_id:
        raise ValueError("文件清单缺少传输 ID")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("文件清单条目无效")
    entries: list[FileEntry] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("文件清单条目必须是对象")
        entry = cast(dict[object, object], raw_entry)
        path = entry.get("relative_path")
        size = entry.get("size")
        mtime_ns = entry.get("mtime_ns")
        kind = entry.get("kind")
        if not isinstance(path, str) or not path:
            raise ValueError("文件清单相对路径无效")
        if not isinstance(size, int) or isinstance(size, bool):
            raise ValueError("文件清单大小无效")
        if not isinstance(mtime_ns, int) or isinstance(mtime_ns, bool):
            raise ValueError("文件清单时间无效")
        if not isinstance(kind, str):
            raise ValueError("文件清单类型无效")
        entries.append(
            FileEntry(PurePosixPath(path), size, mtime_ns, EntryKind(kind))
        )
    return FileManifest(transfer_id, tuple(entries))


__all__ = ["manifest_from_payload", "manifest_to_payload"]
