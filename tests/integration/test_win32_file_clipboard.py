from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lan_input_share.clipboard.win32_clipboard import Win32ClipboardWatcher

pytestmark = [
    pytest.mark.windows_integration,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only clipboard APIs"),
]


def test_verified_paths_round_trip_through_cf_hdrop(tmp_path: Path) -> None:
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("1", encoding="utf-8")
    second.write_text("2", encoding="utf-8")
    watcher = Win32ClipboardWatcher()
    original = watcher.read_unicode_text()
    try:
        watcher.write_file_drop((first, second), "files-marker")
        assert watcher.read_file_drop() == (first, second)
    finally:
        watcher.write_unicode_text(original, "restore")
