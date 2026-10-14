from __future__ import annotations

import os
import subprocess
from pathlib import Path, PurePath

import pytest

from lan_input_share.transfer.manifest import (
    EntryKind,
    FileEntry,
    FileManifest,
    build_manifest,
)
from lan_input_share.transfer.wire import manifest_from_payload, manifest_to_payload


def test_manifest_preserves_multiple_roots_without_name_collision(tmp_path: Path) -> None:
    first = tmp_path / "first" / "photos"
    second = tmp_path / "second" / "photos"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "a.txt").write_text("a", encoding="utf-8")
    (second / "b.txt").write_text("bb", encoding="utf-8")

    manifest = build_manifest([first, second])
    paths = {entry.relative_path.as_posix() for entry in manifest.entries}

    assert "photos/a.txt" in paths
    assert "photos (2)/b.txt" in paths
    assert manifest.total_size == 3


def test_manifest_does_not_follow_junction_or_symlink_cycle(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "safe.txt").write_text("safe", encoding="utf-8")
    link = root / "cycle"
    try:
        os.symlink(root, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("creating symlinks is unavailable")
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(root)],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            pytest.skip("creating a Windows junction is unavailable")

    manifest = build_manifest([root])

    assert {entry.relative_path.as_posix() for entry in manifest.entries} == {
        "root",
        "root/safe.txt",
    }


def test_manifest_rejects_relative_path_escape() -> None:
    with pytest.raises(ValueError, match="越界"):
        FileEntry(PurePath("..", "secret.txt"), 1, 0, EntryKind.FILE)
    with pytest.raises(ValueError, match="相对"):
        FileEntry(PurePath("C:\\secret.txt"), 1, 0, EntryKind.FILE)


@pytest.mark.parametrize(
    "relative_path",
    [
        "CON",
        "con.txt",
        "folder/PRN.bin",
        "folder/name. ",
        "folder/file:name.txt",
    ],
)
def test_manifest_rejects_windows_reserved_names(relative_path: str) -> None:
    with pytest.raises(ValueError, match="Windows|保留|无效"):
        FileEntry(PurePath(relative_path), 1, 0, EntryKind.FILE)


def test_manifest_keeps_distinct_unicode_normalizations() -> None:
    manifest = FileManifest(
        "transfer",
        (
            FileEntry(PurePath("é.txt"), 1, 0, EntryKind.FILE),
            FileEntry(PurePath("e\u0301.txt"), 1, 0, EntryKind.FILE),
        ),
    )

    assert len(manifest.entries) == 2


def test_wire_manifest_round_trip_never_exposes_absolute_source_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "private" / "archive.zip"
    source.parent.mkdir()
    source.write_bytes(b"zip")
    local = build_manifest([source])

    payload = manifest_to_payload(local)
    remote = manifest_from_payload(payload)

    assert str(tmp_path) not in repr(payload)
    assert remote.transfer_id == local.transfer_id
    assert remote.entries == local.entries
    assert remote.source_paths == {}


def test_wire_manifest_rejects_path_escape() -> None:
    payload = {
        "transfer_id": "transfer",
        "entries": [
            {"relative_path": "../secret.txt", "size": 1, "mtime_ns": 0, "kind": "file"}
        ],
    }

    with pytest.raises(ValueError, match="越界"):
        manifest_from_payload(payload)
