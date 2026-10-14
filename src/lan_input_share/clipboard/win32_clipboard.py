from __future__ import annotations

import ctypes
import struct
import sys
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import Protocol, TypeVar

from lan_input_share.clipboard.models import ClipboardImage

if sys.platform == "win32":
    import pywintypes  # type: ignore[import-untyped]
    import win32api  # type: ignore[import-untyped]
    import win32clipboard  # type: ignore[import-untyped]
    import win32con  # type: ignore[import-untyped]
    import win32gui  # type: ignore[import-untyped]
    _CLIPBOARD_OPEN_ERRORS = (OSError, pywintypes.error)
else:
    _CLIPBOARD_OPEN_ERRORS = (OSError,)

_T = TypeVar("_T")
_WM_CLIPBOARDUPDATE = 0x031D
_GHND = 0x0042
_CF_DIBV5 = 17


class ClipboardBusyError(RuntimeError):
    """Raised when another process keeps the Windows clipboard locked."""


class ClipboardBackend(Protocol):
    def open(self) -> None: ...

    def close(self) -> None: ...

    def read_text(self) -> str: ...

    def write_text(self, text: str, marker: str) -> None: ...

    def read_marker(self) -> str: ...

    def read_image(self) -> ClipboardImage | None: ...

    def write_image(self, image: ClipboardImage, marker: str) -> None: ...

    def write_files(self, paths: tuple[Path, ...], marker: str) -> None: ...

    def read_files(self) -> tuple[Path, ...]: ...


class _NativeClipboardBackend:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("系统剪贴板只支持 Windows")
        self._marker_format = int(
            win32clipboard.RegisterClipboardFormat("LanInputShare.Marker.v1")
        )
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self._kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        self._kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalLock.restype = wintypes.LPVOID
        self._kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalUnlock.restype = wintypes.BOOL
        self._kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalFree.restype = wintypes.HGLOBAL
        self._user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self._user32.SetClipboardData.restype = wintypes.HANDLE

    def open(self) -> None:
        win32clipboard.OpenClipboard()

    def close(self) -> None:
        win32clipboard.CloseClipboard()

    def read_text(self) -> str:
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return ""
        value = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return str(value)

    def write_text(self, text: str, marker: str) -> None:
        win32clipboard.EmptyClipboard()
        self._set_clipboard_bytes(
            win32con.CF_UNICODETEXT,
            text.encode("utf-16le") + b"\x00\x00",
        )
        self._set_clipboard_bytes(
            self._marker_format,
            marker.encode("utf-8") + b"\x00",
        )

    def read_marker(self) -> str:
        if not win32clipboard.IsClipboardFormatAvailable(self._marker_format):
            return ""
        value = win32clipboard.GetClipboardData(self._marker_format)
        if isinstance(value, bytes):
            return value.rstrip(b"\x00").decode("utf-8")
        return str(value)

    def read_image(self) -> ClipboardImage | None:
        for format_id, name in (
            (_CF_DIBV5, "dibv5"),
            (win32con.CF_DIB, "dib"),
        ):
            if not win32clipboard.IsClipboardFormatAvailable(format_id):
                continue
            value = win32clipboard.GetClipboardData(format_id)
            if isinstance(value, (bytes, bytearray, memoryview)):
                data = bytes(value)
                if data:
                    return ClipboardImage(name, data)
        return None

    def write_image(self, image: ClipboardImage, marker: str) -> None:
        format_id = _CF_DIBV5 if image.format == "dibv5" else win32con.CF_DIB
        win32clipboard.EmptyClipboard()
        self._set_clipboard_bytes(format_id, image.data)
        self._set_clipboard_bytes(
            self._marker_format,
            marker.encode("utf-8") + b"\x00",
        )

    def write_files(self, paths: tuple[Path, ...], marker: str) -> None:
        win32clipboard.EmptyClipboard()
        names = "\x00".join(str(path.resolve()) for path in paths) + "\x00\x00"
        dropfiles = struct.pack("<IiiII", 20, 0, 0, 0, 1) + names.encode("utf-16le")
        self._set_clipboard_bytes(win32con.CF_HDROP, dropfiles)
        self._set_clipboard_bytes(
            self._marker_format,
            marker.encode("utf-8") + b"\x00",
        )

    def read_files(self) -> tuple[Path, ...]:
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
            return ()
        value = win32clipboard.GetClipboardData(win32con.CF_HDROP)
        return tuple(Path(item) for item in value)

    def _set_clipboard_bytes(self, format_id: int, data: bytes) -> None:
        handle = self._kernel32.GlobalAlloc(_GHND, len(data))
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

        windows_owns_handle = False
        try:
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                ctypes.memmove(pointer, data, len(data))
            finally:
                ctypes.set_last_error(0)
                unlocked = self._kernel32.GlobalUnlock(handle)
                if not unlocked and ctypes.get_last_error() != 0:
                    raise ctypes.WinError(ctypes.get_last_error())

            if not self._user32.SetClipboardData(format_id, handle):
                raise ctypes.WinError(ctypes.get_last_error())
            windows_owns_handle = True
        finally:
            if not windows_owns_handle:
                self._kernel32.GlobalFree(handle)


class Win32ClipboardWatcher:
    def __init__(
        self,
        *,
        backend: ClipboardBackend | None = None,
        retry_attempts: int = 5,
        retry_delay: float = 0.02,
    ) -> None:
        if retry_attempts <= 0 or retry_delay < 0:
            raise ValueError("剪贴板重试参数无效")
        self._backend = backend or _NativeClipboardBackend()
        self._retry_attempts = retry_attempts
        self._retry_delay = retry_delay
        self._operation_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._window_handle = 0
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._callback: Callable[[], None] | None = None

    def start(self, callback: Callable[[], None]) -> None:
        if self._thread is not None:
            raise RuntimeError("剪贴板监听器已经启动")
        if sys.platform != "win32":
            raise RuntimeError("剪贴板监听只支持 Windows")
        self._callback = callback
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._message_thread,
            name="lan-input-share-clipboard",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=3):
            raise TimeoutError("启动剪贴板监听器超时")
        if self._startup_error is not None:
            raise RuntimeError("启动剪贴板监听器失败") from self._startup_error

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        if self._window_handle:
            win32gui.PostMessage(self._window_handle, win32con.WM_CLOSE, 0, 0)
        thread.join(timeout=3)
        self._thread = None
        self._window_handle = 0
        self._callback = None

    def read_unicode_text(self) -> str:
        return self._with_open(self._backend.read_text)

    def write_unicode_text(self, text: str, marker: str) -> None:
        self._with_open(lambda: self._backend.write_text(text, marker))

    def read_marker(self) -> str:
        return self._with_open(self._backend.read_marker)

    def read_image(self) -> ClipboardImage | None:
        return self._with_open(self._backend.read_image)

    def write_image(self, image: ClipboardImage, marker: str) -> None:
        self._with_open(lambda: self._backend.write_image(image, marker))

    def write_file_drop(self, paths: tuple[Path, ...], marker: str) -> None:
        if not paths or any(not path.exists() for path in paths):
            raise ValueError("CF_HDROP 只能写入存在的本地路径")
        self._with_open(lambda: self._backend.write_files(paths, marker))

    def read_file_drop(self) -> tuple[Path, ...]:
        return self._with_open(self._backend.read_files)

    def _with_open(self, operation: Callable[[], _T]) -> _T:
        with self._operation_lock:
            return self._with_open_locked(operation)

    def _with_open_locked(self, operation: Callable[[], _T]) -> _T:
        last_error: BaseException | None = None
        for attempt in range(self._retry_attempts):
            opened = False
            try:
                self._backend.open()
                opened = True
                return operation()
            except _CLIPBOARD_OPEN_ERRORS as exc:
                last_error = exc
                if attempt + 1 < self._retry_attempts:
                    time.sleep(self._retry_delay)
            finally:
                if opened:
                    try:
                        self._backend.close()
                    except _CLIPBOARD_OPEN_ERRORS as exc:
                        if not _is_clipboard_not_open(exc):
                            raise
        raise ClipboardBusyError(
            f"Windows 剪贴板被占用，重试 {self._retry_attempts} 次后仍失败"
        ) from last_error

    def _message_thread(self) -> None:
        try:
            class_name = f"LanInputShareClipboard_{uuid.uuid4().hex}"

            def window_proc(handle: int, message: int, wparam: int, lparam: int) -> int:
                if message == _WM_CLIPBOARDUPDATE:
                    callback = self._callback
                    if callback is not None:
                        with suppress(Exception):
                            callback()
                    return 0
                if message == win32con.WM_CLOSE:
                    win32gui.DestroyWindow(handle)
                    return 0
                if message == win32con.WM_DESTROY:
                    ctypes.windll.user32.RemoveClipboardFormatListener(handle)
                    win32gui.PostQuitMessage(0)
                    return 0
                return int(win32gui.DefWindowProc(handle, message, wparam, lparam))

            window_class = win32gui.WNDCLASS()
            window_class.hInstance = win32api.GetModuleHandle(None)
            window_class.lpszClassName = class_name
            window_class.lpfnWndProc = window_proc
            atom = win32gui.RegisterClass(window_class)
            self._window_handle = int(
                win32gui.CreateWindowEx(
                    0,
                    atom,
                    class_name,
                    0,
                    0,
                    0,
                    0,
                    0,
                    win32con.HWND_MESSAGE,
                    0,
                    window_class.hInstance,
                    None,
                )
            )
            if not ctypes.windll.user32.AddClipboardFormatListener(self._window_handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._ready.set()
            win32gui.PumpMessages()
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()


def _is_clipboard_not_open(error: BaseException) -> bool:
    winerror = getattr(error, "winerror", None)
    return winerror == 1418 or (bool(error.args) and error.args[0] == 1418)


__all__ = ["ClipboardBusyError", "Win32ClipboardWatcher"]
