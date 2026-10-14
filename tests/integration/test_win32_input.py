from __future__ import annotations

import ctypes
import subprocess
import sys
import time

import pytest

from lan_input_share.input.events import KeyEvent, Suppression
from lan_input_share.input.win32_cursor import Win32CursorController
from lan_input_share.input.win32_hook import Win32InputHook
from lan_input_share.input.win32_injector import Win32InputInjector

pytestmark = [
    pytest.mark.windows_integration,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only input APIs"),
]


def test_cursor_restore_is_idempotent_after_disconnect_and_shutdown() -> None:
    cursor = Win32CursorController()
    original = cursor.position()
    try:
        cursor.confine_for_remote()
        cursor.restore_local()
        cursor.restore_local()
        assert cursor.position() == original
    finally:
        cursor.restore_local()
        cursor.set_position(original)


def test_send_input_accepts_a_harmless_key_release() -> None:
    script = """
from lan_input_share.input.events import KeyEvent
from lan_input_share.input.win32_injector import Win32InputInjector

Win32InputInjector().inject(
    KeyEvent(scan_code=0, virtual_key=0x87, is_down=False)
)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


def test_right_shift_normalizes_an_erroneous_extended_key_flag() -> None:
    injector = Win32InputInjector()
    right_shift = KeyEvent(
        scan_code=0x36,
        virtual_key=0xA1,
        is_down=True,
        is_extended=True,
    )
    injector.inject(
        KeyEvent(scan_code=0x36, virtual_key=0xA1, is_down=False)
    )
    try:
        injector.inject(right_shift)
        time.sleep(0.03)

        assert ctypes.windll.user32.GetAsyncKeyState(0xA1) & 0x8000
    finally:
        injector.inject(
            KeyEvent(scan_code=0x36, virtual_key=0xA1, is_down=False)
        )


def test_native_hook_filters_injected_input_and_uninstalls_cleanly() -> None:
    seen: list[object] = []
    hook = Win32InputHook()
    injector = Win32InputInjector()
    try:
        hook.start(lambda event: seen.append(event) or Suppression.PASS_THROUGH)
        injector.inject(KeyEvent(scan_code=0x76, virtual_key=0x87, is_down=False))
        time.sleep(0.05)
    finally:
        hook.stop()

    assert not any(
        isinstance(event, KeyEvent) and event.virtual_key == 0x87 for event in seen
    )
