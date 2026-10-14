from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from lan_input_share.input.events import (
    InputEvent,
    KeyEvent,
    MouseButton,
    MouseButtonEvent,
    MouseMoveEvent,
    MouseWheelEvent,
)

_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_SCANCODE = 0x0008
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_XDOWN = 0x0080
_MOUSEEVENTF_XUP = 0x0100
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x1000
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_XBUTTON1 = 0x0001
_XBUTTON2 = 0x0002
_VK_RSHIFT = 0xA1
_RIGHT_SHIFT_SCAN_CODE = 0x36

_ULONG_PTR = wintypes.WPARAM


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", _INPUT_UNION)]


class Win32InputInjector:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("输入注入只支持 Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SendInput.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(_INPUT),
            ctypes.c_int,
        ]
        self._user32.SendInput.restype = wintypes.UINT

    def inject(self, event: InputEvent) -> None:
        native = self._translate(event)
        if self._user32.SendInput(1, ctypes.byref(native), ctypes.sizeof(_INPUT)) != 1:
            raise ctypes.WinError(ctypes.get_last_error())

    def release_all(self, events: tuple[InputEvent, ...]) -> None:
        for event in events:
            self.inject(event)

    def _translate(self, event: InputEvent) -> _INPUT:
        if isinstance(event, KeyEvent):
            flags = _KEYEVENTF_SCANCODE if event.scan_code else 0
            if event.is_extended and not (
                event.virtual_key == _VK_RSHIFT
                or event.scan_code == _RIGHT_SHIFT_SCAN_CODE
            ):
                flags |= _KEYEVENTF_EXTENDEDKEY
            if not event.is_down:
                flags |= _KEYEVENTF_KEYUP
            keyboard = _KEYBDINPUT(
                0 if event.scan_code else event.virtual_key,
                event.scan_code,
                flags,
                0,
                0,
            )
            return _INPUT(type=_INPUT_KEYBOARD, ki=keyboard)
        if isinstance(event, MouseMoveEvent):
            x_origin = self._user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
            y_origin = self._user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
            width = max(1, self._user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN) - 1)
            height = max(1, self._user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN) - 1)
            x = round((event.position.x - x_origin) * 65535 / width)
            y = round((event.position.y - y_origin) * 65535 / height)
            mouse = _MOUSEINPUT(
                x,
                y,
                0,
                _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK,
                0,
                0,
            )
            return _INPUT(type=_INPUT_MOUSE, mi=mouse)
        if isinstance(event, MouseButtonEvent):
            flag, data = _button_flag(event.button, event.is_down)
            return _INPUT(type=_INPUT_MOUSE, mi=_MOUSEINPUT(0, 0, data, flag, 0, 0))
        if isinstance(event, MouseWheelEvent):
            flag = _MOUSEEVENTF_HWHEEL if event.horizontal else _MOUSEEVENTF_WHEEL
            data = ctypes.c_uint32(event.delta).value
            return _INPUT(type=_INPUT_MOUSE, mi=_MOUSEINPUT(0, 0, data, flag, 0, 0))
        raise TypeError(f"不支持的输入事件：{type(event).__name__}")


def _button_flag(button: MouseButton, is_down: bool) -> tuple[int, int]:
    mapping = {
        (MouseButton.LEFT, True): (_MOUSEEVENTF_LEFTDOWN, 0),
        (MouseButton.LEFT, False): (_MOUSEEVENTF_LEFTUP, 0),
        (MouseButton.RIGHT, True): (_MOUSEEVENTF_RIGHTDOWN, 0),
        (MouseButton.RIGHT, False): (_MOUSEEVENTF_RIGHTUP, 0),
        (MouseButton.MIDDLE, True): (_MOUSEEVENTF_MIDDLEDOWN, 0),
        (MouseButton.MIDDLE, False): (_MOUSEEVENTF_MIDDLEUP, 0),
        (MouseButton.X1, True): (_MOUSEEVENTF_XDOWN, _XBUTTON1),
        (MouseButton.X1, False): (_MOUSEEVENTF_XUP, _XBUTTON1),
        (MouseButton.X2, True): (_MOUSEEVENTF_XDOWN, _XBUTTON2),
        (MouseButton.X2, False): (_MOUSEEVENTF_XUP, _XBUTTON2),
    }
    return mapping[(button, is_down)]


__all__ = ["Win32InputInjector"]
