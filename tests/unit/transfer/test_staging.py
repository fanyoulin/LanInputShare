from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path, PurePath

import pytest

from lan_input_share.transfer.manifest import EntryKind, FileEntry, FileManifest
from lan_input_share.transfer.staging import StagingArea, StagingError


def manifest(transfer_id: str = "transfer-1", size: int = 4) -> FileManifest:
    return FileManifest(
        transfer_id,
        (FileEntry(PurePath("folder", "file.bin"), size, 123, EntryKind.FILE),),
    )


def test_reserve_rejects_insufficient_disk_space_without_creating_parts(
    tmp_path: Path,
) -> None:
    staging = StagingArea(tmp_path / "staging")

    with pytest.raises(StagingError, match="空间不足"):
        staging.reserve(manifest(size=100), available_bytes=99)

    assert not (tmp_path / "staging" / "transfer-1").exists()


def test_cleanup_keeps_active_and_clipboard_referenced_workspaces(tmp_path: Path) -> None:
    staging = StagingArea(tmp_path / "staging", retention_seconds=10)
    active = staging.reserve(manifest("active"), available_bytes=100)
    referenced = staging.reserve(manifest("referenced"), available_bytes=100)
    expired = staging.reserve(manifest("expired"), available_bytes=100)
    active.created_at = referenced.created_at = expired.created_at = 0
    active.active = True
    referenced.active = False
    referenced.clipboard_referenced = True
    expired.active = False

    removed = staging.cleanup(now=11)

    assert removed == ("expired",)
    assert active.path.exists()
    assert referenced.path.exists()
    assert not expired.path.exists()


def test_cleanup_orphans_removes_only_expired_unreferenced_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "staging"
    expired = root / "expired"
    preserved = root / "preserved"
    fresh = root / "fresh"
    for folder in (expired, preserved, fresh):
        folder.mkdir(parents=True)
    old = 1_000.0
    os.utime(expired, (old, old))
    os.utime(preserved, (old, old))
    area = StagingArea(root, retention_seconds=100)

    removed = area.cleanup_orphans(now=1_200.0, preserve={"preserved"})

    assert removed == ("expired",)
    assert preserved.exists()
    assert fresh.exists()


def test_cleanup_orphans_ignores_directory_resolving_outside_staging(
    tmp_path: Path,
) -> None:
    root = tmp_path / "staging"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    redirected = root / "redirected"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(redirected), str(outside)],
        check=True,
        capture_output=True,
    )
    area = StagingArea(root, retention_seconds=100)

    removed = area.cleanup_orphans(now=1_200.0)

    assert removed == ()
    assert redirected.exists()
    assert outside.exists()


def test_commit_requires_expected_size_and_sha256(tmp_path: Path) -> None:
    staging = StagingArea(tmp_path / "staging")
    workspace = staging.reserve(manifest(size=4), available_bytes=100)
    entry = workspace.manifest.entries[0]
    part = staging.part_path(entry, workspace)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"data")

    with pytest.raises(StagingError, match="SHA-256"):
        staging.commit(entry, "0" * 64, workspace)
    final = staging.commit(entry, hashlib.sha256(b"data").hexdigest(), workspace)

    assert final.read_bytes() == b"data"
    assert not part.exists()


def test_commit_rejects_wrong_size_without_renaming(tmp_path: Path) -> None:
    staging = StagingArea(tmp_path / "staging")
    workspace = staging.reserve(manifest(size=5), available_bytes=100)
    entry = workspace.manifest.entries[0]
    part = staging.part_path(entry, workspace)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"data")

    with pytest.raises(StagingError, match="大小"):
        staging.commit(entry, hashlib.sha256(b"data").hexdigest(), workspace)

    assert part.exists()
