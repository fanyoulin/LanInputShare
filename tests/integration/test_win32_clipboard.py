from __future__ import annotations

import sys
import threading

import pytest

from lan_input_share.clipboard.win32_clipboard import Win32ClipboardWatcher

pytestmark = [
    pytest.mark.windows_integration,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only clipboard APIs"),
]


def test_unicode_text_round_trip_and_change_notification() -> None:
    watcher = Win32ClipboardWatcher()
    changed = threading.Event()
    original = watcher.read_unicode_text()
    try:
        watcher.start(changed.set)
        watcher.write_unicode_text("中文 clipboard 🌍", "integration-marker")

        assert changed.wait(timeout=2)
        assert watcher.read_unicode_text() == "中文 clipboard 🌍"
        assert watcher.read_marker() == "integration-marker"
    finally:
        watcher.stop()
        watcher.write_unicode_text(original, "restore")
