from __future__ import annotations

import ctypes
import struct
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


def _opaque_32_bit_dib(*, v5: bool, alpha_mask: int = 0) -> bytes:
    header_size = 124 if v5 else 40
    header = bytearray(header_size)
    struct.pack_into("<IiiHHII", header, 0, header_size, 2, 1, 1, 32, 3, 8)
    if v5:
        struct.pack_into("<IIII", header, 40, 0x00FF0000, 0x0000FF00, 0x000000FF, alpha_mask)
    else:
        header.extend(struct.pack("<III", 0x00FF0000, 0x0000FF00, 0x000000FF))
    pixels = bytes((0, 0, 255, 255, 0, 255, 0, 255))
    return bytes(header) + (b"\x00" * 12 if v5 else b"") + pixels


def _expected_24_bit_dib() -> bytes:
    header = bytearray(40)
    struct.pack_into("<IiiHHII", header, 0, 40, 2, 1, 1, 24, 0, 8)
    return bytes(header) + bytes((0, 0, 255, 0, 255, 0, 0, 0))


def _enum_formats(*formats: int):  # type: ignore[no-untyped-def]
    def next_format(previous: int) -> int:
        if previous == 0:
            return formats[0] if formats else 0
        try:
            return formats[formats.index(previous) + 1]
        except (ValueError, IndexError):
            return 0

    return next_format


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
    monkeypatch.setattr(
        clipboard_module.win32clipboard, "EnumClipboardFormats", _enum_formats(17, 8)
    )

    backend = clipboard_module._NativeClipboardBackend()

    image = backend.read_image()

    assert (image.format, image.data) == ("dibv5", b"dibv5-screenshot")


@pytest.mark.parametrize("v5", [True, False])
def test_opaque_32_bit_clipboard_image_is_normalized_to_24_bit_dib(
    monkeypatch, v5: bool
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
    source_format = 17 if v5 else clipboard_module.win32con.CF_DIB
    source_data = _opaque_32_bit_dib(v5=v5)
    monkeypatch.setattr(
        clipboard_module.win32clipboard,
        "IsClipboardFormatAvailable",
        lambda format_id: format_id == source_format,
    )
    monkeypatch.setattr(
        clipboard_module.win32clipboard,
        "GetClipboardData",
        lambda format_id: source_data if format_id == source_format else None,
    )
    monkeypatch.setattr(
        clipboard_module.win32clipboard, "EnumClipboardFormats", _enum_formats(source_format)
    )

    backend = clipboard_module._NativeClipboardBackend()
    image = backend.read_image()

    assert image is not None
    assert (image.format, image.data) == ("dib", _expected_24_bit_dib())


def test_dibv5_with_real_alpha_mask_is_preserved(monkeypatch) -> None:  # type: ignore[no-untyped-def]
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
    source_data = _opaque_32_bit_dib(v5=True, alpha_mask=0xFF000000)
    monkeypatch.setattr(
        clipboard_module.win32clipboard,
        "IsClipboardFormatAvailable",
        lambda format_id: format_id == 17,
    )
    monkeypatch.setattr(
        clipboard_module.win32clipboard,
        "GetClipboardData",
        lambda format_id: source_data if format_id == 17 else None,
    )
    monkeypatch.setattr(
        clipboard_module.win32clipboard, "EnumClipboardFormats", _enum_formats(17)
    )

    image = clipboard_module._NativeClipboardBackend().read_image()

    assert image is not None
    assert (image.format, image.data) == ("dibv5", source_data)


def test_native_24_bit_dib_is_read_back_without_format_change(monkeypatch) -> None:  # type: ignore[no-untyped-def]
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
    dib = _expected_24_bit_dib()
    v5_header = bytearray(124)
    struct.pack_into("<IiiHHII", v5_header, 0, 124, 2, 1, 1, 24, 0, 8)
    v5 = bytes(v5_header) + dib[40:]
    monkeypatch.setattr(
        clipboard_module.win32clipboard,
        "IsClipboardFormatAvailable",
        lambda format_id: format_id in (17, clipboard_module.win32con.CF_DIB),
    )
    monkeypatch.setattr(
        clipboard_module.win32clipboard,
        "GetClipboardData",
        lambda format_id: v5 if format_id == 17 else dib,
    )
    monkeypatch.setattr(
        clipboard_module.win32clipboard, "EnumClipboardFormats", _enum_formats(8, 17)
    )

    image = clipboard_module._NativeClipboardBackend().read_image()

    assert image is not None
    assert (image.format, image.data) == ("dib", dib)


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
