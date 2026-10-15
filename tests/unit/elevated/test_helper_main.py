from __future__ import annotations

import json
from pathlib import Path

import pytest

from lan_input_share.elevated.helper_main import helper_main, load_approved_client


def test_helper_smoke_test_needs_no_manifest() -> None:
    assert helper_main(["--smoke-test"]) == 0


def test_load_approved_client_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "approved-client.json"
    manifest.write_text(
        json.dumps(
            {
                "path": r"C:\Program Files\LanInputShare\LanInputShare.exe",
                "sha256": "A" * 64,
                "user_sid": "S-1-5-21-1",
                "protocol_version": 1,
            }
        ),
        encoding="utf-8",
    )

    approved = load_approved_client(manifest)

    assert approved.path == Path(r"C:\Program Files\LanInputShare\LanInputShare.exe")
    assert approved.sha256 == "A" * 64


def test_manifest_rejects_unknown_or_malformed_fields(tmp_path: Path) -> None:
    manifest = tmp_path / "approved-client.json"
    manifest.write_text(
        json.dumps(
            {
                "path": r"C:\LanInputShare.exe",
                "sha256": "bad",
                "user_sid": "S-1-5-21-1",
                "protocol_version": 1,
                "unexpected": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest"):
        load_approved_client(manifest)
