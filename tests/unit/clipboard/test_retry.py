from __future__ import annotations

import threading

import pytest

from lan_input_share.clipboard.win32_clipboard import (
    ClipboardBusyError,
    Win32ClipboardWatcher,
)


class BusyBackend:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.open_calls = 0
        self.closed = 0
        self.text = ""

    def open(self) -> None:
        self.open_calls += 1
        if self.open_calls <= self.failures:
            raise OSError("clipboard busy")

    def close(self) -> None:
        self.closed += 1

    def read_text(self) -> str:
        return self.text

    def write_text(self, text: str, marker: str) -> None:
        _ = marker
        self.text = text


class ClipboardNotOpenError(OSError):
    winerror = 1418


class AlreadyClosedBackend(BusyBackend):
    def close(self) -> None:
        self.closed += 1
        raise ClipboardNotOpenError("clipboard was already closed")


class ConcurrentTrackingBackend(BusyBackend):
    def __init__(self) -> None:
        super().__init__(failures=0)
        self.first_opened = threading.Event()
        self.release_reads = threading.Event()
        self.overlapped = threading.Event()
        self._state_lock = threading.Lock()
        self._active = 0

    def open(self) -> None:
        with self._state_lock:
            self._active += 1
            if self._active > 1:
                self.overlapped.set()
            else:
                self.first_opened.set()

    def close(self) -> None:
        with self._state_lock:
            self._active -= 1
        super().close()

    def read_text(self) -> str:
        if not self.release_reads.wait(timeout=2):
            raise TimeoutError("test did not release clipboard read")
        return "serialized"


def test_clipboard_busy_retries_with_bound_and_reports_failure() -> None:
    backend = BusyBackend(failures=5)
    watcher = Win32ClipboardWatcher(backend=backend, retry_attempts=3, retry_delay=0)

    with pytest.raises(ClipboardBusyError, match="3"):
        watcher.read_unicode_text()

    assert backend.open_calls == 3
    assert backend.closed == 0


def test_clipboard_busy_then_succeeds_and_always_closes() -> None:
    backend = BusyBackend(failures=2)
    backend.text = "恢复成功"
    watcher = Win32ClipboardWatcher(backend=backend, retry_attempts=4, retry_delay=0)

    assert watcher.read_unicode_text() == "恢复成功"
    assert backend.open_calls == 3
    assert backend.closed == 1


def test_already_closed_race_does_not_discard_successful_read() -> None:
    backend = AlreadyClosedBackend(failures=0)
    backend.text = "读取完成"
    watcher = Win32ClipboardWatcher(backend=backend, retry_attempts=1, retry_delay=0)

    assert watcher.read_unicode_text() == "读取完成"
    assert backend.closed == 1


def test_clipboard_operations_are_serialized_across_threads() -> None:
    backend = ConcurrentTrackingBackend()
    watcher = Win32ClipboardWatcher(backend=backend, retry_attempts=1, retry_delay=0)
    errors: list[BaseException] = []

    def read() -> None:
        try:
            watcher.read_unicode_text()
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=read)
    second = threading.Thread(target=read)
    first.start()
    assert backend.first_opened.wait(timeout=1)
    second.start()
    overlapped = backend.overlapped.wait(timeout=0.2)
    backend.release_reads.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not overlapped
    assert errors == []
    assert backend.closed == 2
