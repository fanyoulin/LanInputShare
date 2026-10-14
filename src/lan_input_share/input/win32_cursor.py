from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from lan_input_share.input.events import Point


class Win32CursorController:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("光标控制只支持 Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self._user32.GetCursorPos.restype = wintypes.BOOL
        self._user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self._user32.SetCursorPos.restype = wintypes.BOOL
        self._user32.ClipCursor.argtypes = [ctypes.POINTER(wintypes.RECT)]
        self._user32.ClipCursor.restype = wintypes.BOOL
        self._user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self._user32.GetSystemMetrics.restype = ctypes.c_int
        self._user32.ShowCursor.argtypes = [wintypes.BOOL]
        self._user32.ShowCursor.restype = ctypes.c_int
        self._confined = False
        self._restore_position: Point | None = None
        self._remote_anchor: Point | None = None
        self._hide_calls = 0

    def position(self) -> Point:
        point = wintypes.POINT()
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            raise ctypes.WinError(ctypes.get_last_error())
        return Point(point.x, point.y)

    def set_position(self, point: Point) -> None:
        if not self._user32.SetCursorPos(point.x, point.y):
            raise ctypes.WinError(ctypes.get_last_error())

    def confine_for_remote(self) -> Point:
        if not self._confined:
            self._restore_position = self.position()
        if not self._user32.ClipCursor(None):
            raise ctypes.WinError(ctypes.get_last_error())
        center = Point(
            self._user32.GetSystemMetrics(0) // 2,
            self._user32.GetSystemMetrics(1) // 2,
        )
        self.set_position(center)
        self._remote_anchor = center
        if not self._confined:
            while True:
                self._hide_calls += 1
                if self._user32.ShowCursor(False) < 0:
                    break
        self._confined = True
        return center

    def recenter_remote(self) -> None:
        if self._confined and self._remote_anchor is not None:
            self.set_position(self._remote_anchor)

    def restore_local(self, position: Point | None = None) -> None:
        if not self._user32.ClipCursor(None):
            raise ctypes.WinError(ctypes.get_last_error())
        restore_position = position if position is not None else self._restore_position
        if restore_position is not None:
            self.set_position(restore_position)
        while self._hide_calls:
            self._user32.ShowCursor(True)
            self._hide_calls -= 1
        self._confined = False
        self._restore_position = None
        self._remote_anchor = None


__all__ = ["Win32CursorController"]
