from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from lan_input_share.transfer.manifest import EntryKind, FileEntry, FileManifest


def _file(path: str, size: int = 1) -> FileEntry:
    return FileEntry(PurePosixPath(path), size, 0, EntryKind.FILE)


def _directory(path: str) -> FileEntry:
    return FileEntry(PurePosixPath(path), 0, 0, EntryKind.DIRECTORY)


def _manifest(*entries: FileEntry) -> FileManifest:
    return FileManifest("transfer-a", entries)


def test_probe_and_decision_payloads_round_trip_without_target_path() -> None:
    from lan_input_share.transfer.preflight import (
        PasteDecision,
        PasteDecisionResult,
        PasteProbe,
    )

    manifest = _manifest(_directory("folder"), _file("folder/item.txt", 3))
    probe = PasteProbe("transfer-a", "source-a", "target-a", manifest)
    decision = PasteDecisionResult(
        "transfer-a",
        PasteDecision.SKIP,
        (PurePosixPath("folder/item.txt"),),
        False,
    )

    probe_payload = probe.to_payload()
    decision_payload = decision.to_payload()

    assert PasteProbe.from_payload(probe_payload) == probe
    assert PasteDecisionResult.from_payload(decision_payload) == decision
    assert "C:\\" not in repr(probe_payload)
    assert "destination" not in probe_payload


def test_probe_payload_rejects_target_absolute_path() -> None:
    from lan_input_share.transfer.preflight import PasteProbe

    payload = PasteProbe(
        "transfer-a",
        "source-a",
        "target-a",
        _manifest(_file("item.txt")),
    ).to_payload()
    payload["destination_path"] = "C:/Users/private/Desktop"

    with pytest.raises(ValueError, match="目标路径|字段"):
        PasteProbe.from_payload(payload)


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"decision": "overwrite", "skipped_paths": [], "compatibility": False}, "ID"),
        (
            {
                "transfer_id": "transfer-a",
                "decision": "unknown",
                "skipped_paths": [],
                "compatibility": False,
            },
            "决定",
        ),
        (
            {
                "transfer_id": "transfer-a",
                "decision": "skip",
                "skipped_paths": ["C:/private/file.txt"],
                "compatibility": False,
            },
            "相对",
        ),
        (
            {
                "transfer_id": "transfer-a",
                "decision": "skip",
                "skipped_paths": ["../private/file.txt"],
                "compatibility": False,
            },
            "越界",
        ),
    ],
)
def test_decision_payload_rejects_untrusted_fields(
    payload: object, message: str
) -> None:
    from lan_input_share.transfer.preflight import PasteDecisionResult

    with pytest.raises(ValueError, match=message):
        PasteDecisionResult.from_payload(payload)


def test_scan_detects_case_insensitive_file_conflict(tmp_path: Path) -> None:
    from lan_input_share.transfer.preflight import ConflictKind, PastePreflightService

    (tmp_path / "Report.TXT").write_text("old", encoding="utf-8")

    result = PastePreflightService().scan(tmp_path, _manifest(_file("report.txt", 8)))

    assert result.required_bytes == 8
    assert [(item.relative_path.as_posix(), item.kind) for item in result.conflicts] == [
        ("report.txt", ConflictKind.EXISTING_FILE)
    ]


def test_scan_does_not_unicode_normalize_distinct_names(tmp_path: Path) -> None:
    from lan_input_share.transfer.preflight import PastePreflightService

    (tmp_path / "é.txt").write_text("old", encoding="utf-8")

    result = PastePreflightService().scan(tmp_path, _manifest(_file("e\u0301.txt")))

    assert result.conflicts == ()


def test_scan_merges_directories_and_detects_nested_files(tmp_path: Path) -> None:
    from lan_input_share.transfer.preflight import ConflictKind, PastePreflightService

    folder = tmp_path / "Folder"
    folder.mkdir()
    (folder / "old.bin").write_bytes(b"old")
    manifest = _manifest(
        _directory("folder"),
        _file("folder/old.bin", 4),
        _file("folder/new.bin", 5),
    )

    result = PastePreflightService().scan(tmp_path, manifest)

    assert result.required_bytes == 9
    assert len(result.conflicts) == 1
    assert result.conflicts[0].relative_path == PurePosixPath("folder/old.bin")
    assert result.conflicts[0].kind is ConflictKind.EXISTING_FILE


@pytest.mark.parametrize(
    "existing_directory, source_kind",
    [(True, EntryKind.FILE), (False, EntryKind.DIRECTORY)],
)
def test_scan_reports_file_directory_type_conflict(
    tmp_path: Path,
    existing_directory: bool,
    source_kind: EntryKind,
) -> None:
    from lan_input_share.transfer.preflight import ConflictKind, PastePreflightService

    target = tmp_path / "item"
    if existing_directory:
        target.mkdir()
    else:
        target.write_text("old", encoding="utf-8")
    source = _directory("item") if source_kind is EntryKind.DIRECTORY else _file("item")

    result = PastePreflightService().scan(tmp_path, _manifest(source))

    assert result.conflicts[0].kind is ConflictKind.TYPE_MISMATCH


def test_filter_manifest_skips_type_conflict_subtree_and_preserves_sources(
    tmp_path: Path,
) -> None:
    from lan_input_share.transfer.preflight import filter_manifest

    kept_source = tmp_path / "keep.txt"
    skipped_source = tmp_path / "skip.txt"
    manifest = FileManifest(
        "transfer-a",
        (
            _directory("blocked"),
            _file("blocked/child.txt", 2),
            _directory("kept"),
            _file("kept/keep.txt", 3),
        ),
        {
            PurePosixPath("blocked/child.txt"): skipped_source,
            PurePosixPath("kept/keep.txt"): kept_source,
        },
    )

    filtered = filter_manifest(manifest, (PurePosixPath("blocked"),))

    assert [entry.relative_path.as_posix() for entry in filtered.entries] == [
        "kept",
        "kept/keep.txt",
    ]
    assert filtered.source_paths == {PurePosixPath("kept/keep.txt"): kept_source}
    assert filtered.total_size == 3


def test_filter_manifest_can_return_empty_manifest() -> None:
    from lan_input_share.transfer.preflight import filter_manifest

    manifest = _manifest(_file("only.txt", 7))

    filtered = filter_manifest(manifest, (PurePosixPath("only.txt"),))

    assert filtered.transfer_id == manifest.transfer_id
    assert filtered.entries == ()
    assert filtered.total_size == 0


def test_preflight_result_bounds_conflict_preview() -> None:
    from lan_input_share.transfer.preflight import (
        Conflict,
        ConflictKind,
        PreflightResult,
    )

    conflicts = tuple(
        Conflict(PurePosixPath(f"file-{index}.txt"), ConflictKind.EXISTING_FILE)
        for index in range(10_000)
    )

    result = PreflightResult(conflicts, 10_000)

    assert result.conflict_count == 10_000
    assert len(result.conflict_preview) == 5
    assert result.conflict_preview[-1].relative_path == PurePosixPath("file-4.txt")
