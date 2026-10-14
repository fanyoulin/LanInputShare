from __future__ import annotations

import ctypes
import hashlib
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from lan_input_share.transfer.destination import (
    DestinationError,
    DestinationTransferWorkspace,
)
from lan_input_share.transfer.manifest import EntryKind, FileEntry, FileManifest
from lan_input_share.transfer.preflight import PasteDecision, PasteDecisionResult


def make_manifest(
    *entries: tuple[str, int, EntryKind], transfer_id: str = "transfer-1"
) -> FileManifest:
    return FileManifest(
        transfer_id,
        tuple(
            FileEntry(PurePosixPath(path), size, 123, kind)
            for path, size, kind in entries
        ),
    )


def overwrite_decision(manifest: FileManifest) -> PasteDecisionResult:
    return PasteDecisionResult(
        manifest.transfer_id, PasteDecision.OVERWRITE, (), False
    )


def test_reserve_creates_hidden_same_volume_workspace_and_commits_new_file(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    manifest = make_manifest(
        ("folder", 0, EntryKind.DIRECTORY),
        ("folder/a.bin", 4, EntryKind.FILE),
    )

    workspace = DestinationTransferWorkspace.reserve(
        destination, manifest, overwrite_decision(manifest)
    )
    entry = manifest.entries[1]
    part = workspace.part_path(entry)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"data")
    workspace.verify_and_stage(entry, hashlib.sha256(b"data").hexdigest())
    paths = workspace.commit()

    assert workspace.path.parent == destination
    assert workspace.path.name == ".LanInputShare-transfer-1.part"
    assert paths == (destination / "folder",)
    assert (destination / "folder" / "a.bin").read_bytes() == b"data"
    assert not workspace.path.exists()


def test_reserved_workspace_has_windows_hidden_attribute(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows hidden attributes are Windows-only")
    destination = tmp_path / "destination"
    destination.mkdir()
    manifest = make_manifest(("file.bin", 4, EntryKind.FILE))

    workspace = DestinationTransferWorkspace.reserve(
        destination, manifest, overwrite_decision(manifest)
    )

    kernel32 = ctypes.windll.kernel32
    kernel32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetFileAttributesW.restype = ctypes.c_uint32
    assert int(kernel32.GetFileAttributesW(str(workspace.path))) & 0x2
    workspace.cleanup()


def test_overwrite_directory_merge_keeps_unmentioned_target_files(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    folder = destination / "folder"
    folder.mkdir(parents=True)
    (folder / "a.bin").write_bytes(b"old")
    (folder / "extra.bin").write_bytes(b"keep")
    manifest = make_manifest(
        ("folder", 0, EntryKind.DIRECTORY),
        ("folder/a.bin", 3, EntryKind.FILE),
    )
    workspace = DestinationTransferWorkspace.reserve(
        destination, manifest, overwrite_decision(manifest)
    )
    entry = manifest.entries[1]
    part = workspace.part_path(entry)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"new")
    workspace.verify_and_stage(entry, hashlib.sha256(b"new").hexdigest())

    workspace.commit()

    assert (folder / "a.bin").read_bytes() == b"new"
    assert (folder / "extra.bin").read_bytes() == b"keep"


def test_preflight_after_new_conflict_aborts_without_touching_new_target(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    manifest = make_manifest(("new.bin", 4, EntryKind.FILE))
    workspace = DestinationTransferWorkspace.reserve(
        destination, manifest, overwrite_decision(manifest)
    )
    entry = manifest.entries[0]
    part = workspace.part_path(entry)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"data")
    workspace.verify_and_stage(entry, hashlib.sha256(b"data").hexdigest())
    (destination / "new.bin").write_bytes(b"appeared-late")

    with pytest.raises(DestinationError, match="新增冲突"):
        workspace.commit()

    assert (destination / "new.bin").read_bytes() == b"appeared-late"


def test_hash_mismatch_never_changes_existing_target(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    target = destination / "file.bin"
    target.write_bytes(b"old")
    manifest = make_manifest(("file.bin", 4, EntryKind.FILE))
    workspace = DestinationTransferWorkspace.reserve(
        destination, manifest, overwrite_decision(manifest)
    )
    part = workspace.part_path(manifest.entries[0])
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"data")

    with pytest.raises(DestinationError, match="SHA-256"):
        workspace.verify_and_stage(manifest.entries[0], "0" * 64)

    assert target.read_bytes() == b"old"


def test_insufficient_space_rejects_before_workspace_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    manifest = make_manifest(("large.bin", 100, EntryKind.FILE))
    monkeypatch.setattr(
        "lan_input_share.transfer.destination.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=99),
    )

    with pytest.raises(DestinationError, match="空间不足"):
        DestinationTransferWorkspace.reserve(
            destination, manifest, overwrite_decision(manifest)
        )

    assert not (destination / ".LanInputShare-transfer-1.part").exists()


def test_commit_failure_rolls_back_replaced_file_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    first = destination / "first.bin"
    second = destination / "second.bin"
    first.write_bytes(b"old-first")
    second.write_bytes(b"old-second")
    manifest = make_manifest(
        ("first.bin", 9, EntryKind.FILE),
        ("second.bin", 10, EntryKind.FILE),
    )
    workspace = DestinationTransferWorkspace.reserve(
        destination, manifest, overwrite_decision(manifest)
    )
    for entry, content in zip(
        manifest.entries, (b"new-first", b"new-second"), strict=True
    ):
        part = workspace.part_path(entry)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(content)
        workspace.verify_and_stage(entry, hashlib.sha256(content).hexdigest())
    original_replace = workspace._replace
    calls = 0

    def fail_during_second_file(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("injected commit failure")
        original_replace(source, target)

    monkeypatch.setattr(workspace, "_replace", fail_during_second_file)

    with pytest.raises(DestinationError, match="提交失败"):
        workspace.commit()

    assert first.read_bytes() == b"old-first"
    assert second.read_bytes() == b"old-second"


def test_file_replaces_existing_directory_only_after_backup(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    existing = destination / "item"
    existing.mkdir(parents=True)
    (existing / "old.txt").write_text("old", encoding="utf-8")
    manifest = make_manifest(("item", 4, EntryKind.FILE))
    workspace = DestinationTransferWorkspace.reserve(
        destination, manifest, overwrite_decision(manifest)
    )
    part = workspace.part_path(manifest.entries[0])
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"data")
    workspace.verify_and_stage(
        manifest.entries[0], hashlib.sha256(b"data").hexdigest()
    )

    workspace.commit()

    assert (destination / "item").is_file()
    assert (destination / "item").read_bytes() == b"data"


def test_rollback_failure_preserves_recovery_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    target = destination / "item.bin"
    target.write_bytes(b"old")
    manifest = make_manifest(("item.bin", 3, EntryKind.FILE))
    workspace = DestinationTransferWorkspace.reserve(
        destination, manifest, overwrite_decision(manifest)
    )
    part = workspace.part_path(manifest.entries[0])
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"new")
    workspace.verify_and_stage(
        manifest.entries[0], hashlib.sha256(b"new").hexdigest()
    )
    original_replace = workspace._replace
    calls = 0

    def fail_install_and_rollback(source: Path, target_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise OSError("injected persistent failure")
        original_replace(source, target_path)

    monkeypatch.setattr(workspace, "_replace", fail_install_and_rollback)

    with pytest.raises(DestinationError, match="回滚不完整"):
        workspace.commit()

    assert workspace.recovery_required
    assert workspace.path.exists()
    assert (workspace.path / "RECOVERY_REQUIRED.txt").exists()
    assert (workspace.path / "backup" / "item.bin").read_bytes() == b"old"


def test_reparse_parent_is_rejected_before_commit(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    outside = tmp_path / "outside"
    destination.mkdir()
    outside.mkdir()
    link = destination / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating directory symlinks requires Windows developer mode")
    manifest = make_manifest(("linked/file.bin", 4, EntryKind.FILE))

    with pytest.raises(DestinationError, match="重解析|链接"):
        DestinationTransferWorkspace.reserve(
            destination, manifest, overwrite_decision(manifest)
        )

    assert not (outside / "file.bin").exists()


def test_reparse_parent_detection_blocks_escape_without_os_symlink_privilege(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "destination"
    linked = destination / "linked"
    linked.mkdir(parents=True)
    manifest = make_manifest(("linked/file.bin", 4, EntryKind.FILE))
    monkeypatch.setattr(
        "lan_input_share.transfer.destination._is_reparse_point",
        lambda path: path.name == "linked",
    )

    with pytest.raises(DestinationError, match="重解析"):
        DestinationTransferWorkspace.reserve(
            destination, manifest, overwrite_decision(manifest)
        )
