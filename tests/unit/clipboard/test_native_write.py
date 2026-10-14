from __future__ import annotations

import ctypes
from dataclasses import dataclass
from pathlib import Path

import pytest

import lan_input_share.clipboard.win32_clipboard as clipboard_module


@dataclass(frozen=True)
class ImageFixture:
    format: str
    data: bytes


class NativeCall:
    def __init__(self, callback):  # type: ignore[no-untyped-def]
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):  # type: ignore[no-untyped-def]
        return self.callback(*args)


class FakeKernel32:
    def __init__(self) -> None:
        self.buffers: dict[int, ctypes.Array[ctypes.c_char]] = {}
        self.sizes: dict[int, int] = {}
        self.freed: list[int] = []
        self.GlobalAlloc = NativeCall(self._global_alloc)
        self.GlobalLock = NativeCall(lambda handle: handle)
        self.GlobalUnlock = NativeCall(lambda _handle: True)
        self.GlobalFree = NativeCall(self._global_free)

    def _global_alloc(self, _flags: int, size: int) -> int:
        buffer = ctypes.create_string_buffer(size)
        handle = ctypes.addressof(buffer)
        self.buffers[handle] = buffer
        self.sizes[handle] = size
        return handle

    def _global_free(self, handle: int) -> int:
        self.freed.append(handle)
        self.buffers.pop(handle, None)
        self.sizes.pop(handle, None)
        return 0


class FakeUser32:
    def __init__(self, kernel32: FakeKernel32) -> None:
        self.kernel32 = kernel32
        self.writes: list[tuple[int, bytes]] = []
        self.SetClipboardData = NativeCall(self._set_clipboard_data)

    def _set_clipboard_data(self, format_id: int, handle: int) -> int:
        size = self.kernel32.sizes[handle]
        self.writes.append((format_id, ctypes.string_at(handle, size)))
        return handle


def test_native_clipboard_write_transfers_movable_global_memory_to_windows(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    kernel32 = FakeKernel32()
    user32 = FakeUser32(kernel32)
    monkeypatch.setattr(
        clipboard_module.ctypes,
        "WinDLL",
        lambda name, **_kwargs: kernel32 if name == "kernel32" else user32,
    )
    monkeypatch.setattr(
        clipboard_module.win32clipboard,
        "RegisterClipboardFormat",
        lambda _name: 0xC123,
    )
    monkeypatch.setattr(clipboard_module.win32clipboard, "EmptyClipboard", lambda: None)
    monkeypatch.setattr(
        clipboard_module.win32clipboard,
        "SetClipboardData",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("pywin32 must not own clipboard write buffers")
        ),
    )

    backend = clipboard_module._NativeClipboardBackend()
    backend.write_text("客户端稳定", "marker-1")

    assert user32.writes == [
        (
            clipboard_module.win32con.CF_UNICODETEXT,
            "客户端稳定".encode("utf-16le") + b"\x00\x00",
        ),
        (0xC123, b"marker-1\x00"),
    ]
    assert kernel32.freed == []


def test_native_file_drop_uses_owned_global_memory(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    kernel32 = FakeKernel32()
    user32 = FakeUser32(kernel32)
    monkeypatch.setattr(
        clipboard_module.ctypes,
        "WinDLL",
        lambda name, **_kwargs: kernel32 if name == "kernel32" else user32,
    )
    monkeypatch.setattr(
        clipboard_module.win32clipboard,
        "RegisterClipboardFormat",
        lambda _name: 0xC123,
    )
    monkeypatch.setattr(clipboard_module.win32clipboard, "EmptyClipboard", lambda: None)
    copied_file = tmp_path / "文件.zip"
    copied_file.write_bytes(b"content")

    backend = clipboard_module._NativeClipboardBackend()
    backend.write_files((copied_file,), "marker-2")

    drop_data = user32.writes[0][1]
    assert user32.writes[0][0] == clipboard_module.win32con.CF_HDROP
    assert drop_data[:20] == clipboard_module.struct.pack("<IiiII", 20, 0, 0, 0, 1)
    assert drop_data[20:] == (str(copied_file.resolve()) + "\x00\x00").encode(
        "utf-16le"
    )
    assert user32.writes[1] == (0xC123, b"marker-2\x00")
    assert kernel32.freed == []


def test_native_clipboard_reads_dibv5_screenshot_before_legacy_dib(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    kernel32 = FakeKernel32()
    user32 = FakeUser32(kernel32)
    monkeypatch.setattr(
        clipboard_module.ctypes,
        "WinDLL",
        lambda name, **_kwargs: kernel32 if name == "kernel32" else user32,
    )
    monkeypatch.setattr(
        clipboard_module.win32clipboard,
        "RegisterClipboardFormat",
        lambda _name: 0xC123,
    )
    monkeypatch.setattr(
        clipboard_module.win32clipboard,
        "IsClipboardFormatAvailable",
        lambda format_id: format_id in (17, clipboard_module.win32con.CF_DIB),
    )
    monkeypatch.setattr(
        clipboard_module.win32clipboard,
        "GetClipboardData",
        lambda format_id: b"dibv5-screenshot" if format_id == 17 else b"legacy-dib",
    )

    backend = clipboard_module._NativeClipboardBackend()

    image = backend.read_image()

    assert (image.format, image.data) == ("dibv5", b"dibv5-screenshot")


def test_native_clipboard_writes_screenshot_format_and_loop_marker(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    kernel32 = FakeKernel32()
    user32 = FakeUser32(kernel32)
    monkeypatch.setattr(
        clipboard_module.ctypes,
        "WinDLL",
        lambda name, **_kwargs: kernel32 if name == "kernel32" else user32,
    )
    monkeypatch.setattr(
        clipboard_module.win32clipboard,
        "RegisterClipboardFormat",
        lambda _name: 0xC123,
    )
    monkeypatch.setattr(clipboard_module.win32clipboard, "EmptyClipboard", lambda: None)

    backend = clipboard_module._NativeClipboardBackend()
    backend.write_image(ImageFixture("dib", b"raw-screenshot"), "image-marker")

    assert user32.writes == [
        (clipboard_module.win32con.CF_DIB, b"raw-screenshot"),
        (0xC123, b"image-marker\x00"),
    ]
    assert kernel32.freed == []


def test_native_clipboard_write_frees_memory_when_windows_rejects_it(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    kernel32 = FakeKernel32()
    user32 = FakeUser32(kernel32)
    user32.SetClipboardData = NativeCall(lambda _format_id, _handle: 0)
    monkeypatch.setattr(
        clipboard_module.ctypes,
        "WinDLL",
        lambda name, **_kwargs: kernel32 if name == "kernel32" else user32,
    )
    monkeypatch.setattr(
        clipboard_module.win32clipboard,
        "RegisterClipboardFormat",
        lambda _name: 0xC123,
    )
    monkeypatch.setattr(clipboard_module.win32clipboard, "EmptyClipboard", lambda: None)

    backend = clipboard_module._NativeClipboardBackend()
    ctypes.set_last_error(5)

    with pytest.raises(OSError):
        backend.write_text("failure", "marker")

    assert len(kernel32.freed) == 1
