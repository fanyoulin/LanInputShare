from __future__ import annotations

from lan_input_share.input import win32_cursor
from lan_input_share.input.events import Point


class NativeCall:
    def __init__(self, callback):  # type: ignore[no-untyped-def]
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):  # type: ignore[no-untyped-def]
        return self.callback(*args)


class FakeUser32:
    def __init__(self) -> None:
        self.point = Point(25, 40)
        self.cursor_counter = 0
        self.show_calls: list[bool] = []
        self.GetCursorPos = NativeCall(self._get_cursor_pos)
        self.SetCursorPos = NativeCall(self._set_cursor_pos)
        self.ClipCursor = NativeCall(lambda _rect: True)
        self.GetSystemMetrics = NativeCall(
            lambda metric: 1920 if metric == 0 else 1080
        )
        self.ShowCursor = NativeCall(self._show_cursor)

    def _get_cursor_pos(self, pointer):  # type: ignore[no-untyped-def]
        pointer._obj.x = self.point.x
        pointer._obj.y = self.point.y
        return True

    def _set_cursor_pos(self, x: int, y: int) -> bool:
        self.point = Point(x, y)
        return True

    def _show_cursor(self, visible: bool) -> int:
        self.show_calls.append(bool(visible))
        self.cursor_counter += 1 if visible else -1
        return self.cursor_counter


def test_local_cursor_is_hidden_only_while_remote_is_active(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeUser32()
    monkeypatch.setattr(win32_cursor.ctypes, "WinDLL", lambda *_args, **_kwargs: fake)
    cursor = win32_cursor.Win32CursorController()

    anchor = cursor.confine_for_remote()
    assert anchor == Point(960, 540)
    assert fake.show_calls == [False]
    assert fake.cursor_counter < 0

    cursor.restore_local()
    assert fake.show_calls == [False, True]
    assert fake.cursor_counter >= 0
    assert cursor.position() == Point(25, 40)


def test_edge_return_restores_cursor_to_mapped_position(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeUser32()
    monkeypatch.setattr(win32_cursor.ctypes, "WinDLL", lambda *_args, **_kwargs: fake)
    cursor = win32_cursor.Win32CursorController()
    anchor = cursor.confine_for_remote()
    assert anchor == Point(960, 540)

    cursor.restore_local(Point(1919, 540))

    assert cursor.position() == Point(1919, 540)
