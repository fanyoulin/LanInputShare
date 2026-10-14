from __future__ import annotations

import ctypes
import sys
import threading
from collections import deque
from collections.abc import Callable
from ctypes import wintypes

from lan_input_share.input.events import (
    InputEvent,
    KeyEvent,
    MouseButton,
    MouseButtonEvent,
    MouseMoveEvent,
    MouseWheelEvent,
    Point,
    Suppression,
)

_WH_KEYBOARD_LL = 13
_WH_MOUSE_LL = 14
_HC_ACTION = 0
_WM_QUIT = 0x0012
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_RBUTTONDOWN = 0x0204
_WM_RBUTTONUP = 0x0205
_WM_MBUTTONDOWN = 0x0207
_WM_MBUTTONUP = 0x0208
_WM_MOUSEWHEEL = 0x020A
_WM_XBUTTONDOWN = 0x020B
_WM_XBUTTONUP = 0x020C
_WM_MOUSEHWHEEL = 0x020E
_LLKHF_EXTENDED = 0x01
_LLKHF_INJECTED = 0x10
_LLMHF_INJECTED = 0x01
_VK_CONTROL = 0x11
_VK_MENU = 0x12
_VK_ESCAPE = 0x1B
_XBUTTON1 = 0x0001

_LRESULT = wintypes.LPARAM
_HOOKPROC = ctypes.WINFUNCTYPE(
    _LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class HookEventQueue:
    """Non-blocking hook queue that drops stale moves, never discrete input."""

    def __init__(self, capacity: int = 512) -> None:
        if capacity <= 0:
            raise ValueError("capacity 必须为正数")
        self._capacity = capacity
        self._items: deque[InputEvent] = deque()
        self._lock = threading.Lock()

    def put(self, event: InputEvent, *, coalesce_mouse_moves: bool = True) -> None:
        with self._lock:
            if isinstance(event, MouseMoveEvent) and coalesce_mouse_moves:
                for index in range(len(self._items) - 1, -1, -1):
                    if isinstance(self._items[index], MouseMoveEvent):
                        self._items[index] = event
                        return
            if isinstance(event, MouseMoveEvent) and len(self._items) >= self._capacity:
                return
            self._items.append(event)

    def get_nowait(self) -> InputEvent:
        with self._lock:
            if not self._items:
                raise IndexError("输入队列为空")
            return self._items.popleft()

    def drain(self) -> tuple[InputEvent, ...]:
        with self._lock:
            result = tuple(self._items)
            self._items.clear()
            return result


class Win32InputHook:
    def __init__(self, *, install_native: bool = True) -> None:
        if install_native and sys.platform != "win32":
            raise RuntimeError("全局输入钩子只支持 Windows")
        self._install_native = install_native
        self._callback: Callable[[InputEvent], Suppression] | None = None
        self._remote_active = False
        self._control_down = False
        self._alt_down = False
        self._locally_pressed_keys: set[tuple[int, int, bool]] = set()
        self._locally_pressed_buttons: set[MouseButton] = set()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._keyboard_proc: object | None = None
        self._mouse_proc: object | None = None

    @property
    def remote_active(self) -> bool:
        return self._remote_active

    def start(self, callback: Callable[[InputEvent], Suppression]) -> None:
        if self._callback is not None:
            raise RuntimeError("输入钩子已经启动")
        self._callback = callback
        if not self._install_native:
            return
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._hook_thread,
            name="lan-input-share-hooks",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=3):
            self._callback = None
            raise TimeoutError("安装 Windows 输入钩子超时")
        if self._startup_error is not None:
            error = self._startup_error
            self._callback = None
            raise RuntimeError("安装 Windows 输入钩子失败") from error

    def stop(self) -> None:
        self._remote_active = False
        if self._thread is not None and self._thread.is_alive():
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
            self._thread.join(timeout=3)
        self._thread = None
        self._thread_id = 0
        self._callback = None
        self._control_down = False
        self._alt_down = False
        self._locally_pressed_keys.clear()
        self._locally_pressed_buttons.clear()

    def set_remote_active(self, active: bool) -> None:
        self._remote_active = active

    def dispatch_for_test(self, event: InputEvent) -> Suppression:
        return self._dispatch(event)

    def _dispatch(self, event: InputEvent) -> Suppression:
        if event.injected:
            return Suppression.PASS_THROUGH
        local_release = False
        if isinstance(event, KeyEvent):
            key = (event.scan_code, event.virtual_key, event.is_extended)
            local_release = not event.is_down and key in self._locally_pressed_keys
            if event.virtual_key == _VK_CONTROL:
                self._control_down = event.is_down
            elif event.virtual_key == _VK_MENU:
                self._alt_down = event.is_down
            if (
                event.virtual_key == _VK_ESCAPE
                and event.is_down
                and self._control_down
                and self._alt_down
            ):
                self._remote_active = False
                callback = self._callback
                if callback is not None:
                    callback(event)
                return Suppression.PASS_THROUGH
        elif isinstance(event, MouseButtonEvent):
            local_release = (
                not event.is_down and event.button in self._locally_pressed_buttons
            )
        callback = self._callback
        if callback is None:
            return Suppression.PASS_THROUGH
        requested = callback(event)
        if requested is Suppression.SUPPRESS_ALWAYS:
            return Suppression.SUPPRESS
        if local_release:
            self._forget_local_press(event)
            return Suppression.PASS_THROUGH
        if not self._remote_active:
            self._remember_local_press(event)
        if self._remote_active and requested is Suppression.SUPPRESS:
            return Suppression.SUPPRESS
        return Suppression.PASS_THROUGH

    def _remember_local_press(self, event: InputEvent) -> None:
        if isinstance(event, KeyEvent):
            key = (event.scan_code, event.virtual_key, event.is_extended)
            if event.is_down:
                self._locally_pressed_keys.add(key)
            else:
                self._locally_pressed_keys.discard(key)
        elif isinstance(event, MouseButtonEvent):
            if event.is_down:
                self._locally_pressed_buttons.add(event.button)
            else:
                self._locally_pressed_buttons.discard(event.button)

    def _forget_local_press(self, event: InputEvent) -> None:
        if isinstance(event, KeyEvent):
            self._locally_pressed_keys.discard(
                (event.scan_code, event.virtual_key, event.is_extended)
            )
        elif isinstance(event, MouseButtonEvent):
            self._locally_pressed_buttons.discard(event.button)

    def _hook_thread(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._thread_id = int(kernel32.GetCurrentThreadId())
        keyboard_hook = wintypes.HHOOK()
        mouse_hook = wintypes.HHOOK()
        try:
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int,
                _HOOKPROC,
                wintypes.HINSTANCE,
                wintypes.DWORD,
            ]
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
            user32.UnhookWindowsHookEx.restype = wintypes.BOOL
            user32.CallNextHookEx.argtypes = [
                wintypes.HHOOK,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.CallNextHookEx.restype = _LRESULT

            def keyboard_proc(
                code: int, message: wintypes.WPARAM, data_pointer: wintypes.LPARAM
            ) -> int:
                if code == _HC_ACTION:
                    native = ctypes.cast(
                        data_pointer, ctypes.POINTER(_KBDLLHOOKSTRUCT)
                    ).contents
                    event = KeyEvent(
                        scan_code=int(native.scanCode),
                        virtual_key=int(native.vkCode),
                        is_down=int(message) in (_WM_KEYDOWN, _WM_SYSKEYDOWN),
                        is_extended=bool(native.flags & _LLKHF_EXTENDED),
                        injected=bool(native.flags & _LLKHF_INJECTED),
                    )
                    if self._dispatch(event) is Suppression.SUPPRESS:
                        return 1
                return int(user32.CallNextHookEx(None, code, message, data_pointer))

            def mouse_proc(
                code: int, message: wintypes.WPARAM, data_pointer: wintypes.LPARAM
            ) -> int:
                if code == _HC_ACTION:
                    native = ctypes.cast(
                        data_pointer, ctypes.POINTER(_MSLLHOOKSTRUCT)
                    ).contents
                    event = _mouse_event(int(message), native)
                    if event is not None and self._dispatch(event) is Suppression.SUPPRESS:
                        return 1
                return int(user32.CallNextHookEx(None, code, message, data_pointer))

            keyboard_callback = _HOOKPROC(keyboard_proc)
            mouse_callback = _HOOKPROC(mouse_proc)
            self._keyboard_proc = keyboard_callback
            self._mouse_proc = mouse_callback
            module = kernel32.GetModuleHandleW(None)
            keyboard_hook = user32.SetWindowsHookExW(
                _WH_KEYBOARD_LL, keyboard_callback, module, 0
            )
            mouse_hook = user32.SetWindowsHookExW(
                _WH_MOUSE_LL, mouse_callback, module, 0
            )
            if not keyboard_hook or not mouse_hook:
                raise ctypes.WinError(ctypes.get_last_error())
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
        finally:
            if keyboard_hook:
                user32.UnhookWindowsHookEx(keyboard_hook)
            if mouse_hook:
                user32.UnhookWindowsHookEx(mouse_hook)
            self._keyboard_proc = None
            self._mouse_proc = None


def _mouse_event(message: int, native: _MSLLHOOKSTRUCT) -> InputEvent | None:
    injected = bool(native.flags & _LLMHF_INJECTED)
    if message == _WM_MOUSEMOVE:
        return MouseMoveEvent(Point(native.pt.x, native.pt.y), injected=injected)
    buttons = {
        _WM_LBUTTONDOWN: (MouseButton.LEFT, True),
        _WM_LBUTTONUP: (MouseButton.LEFT, False),
        _WM_RBUTTONDOWN: (MouseButton.RIGHT, True),
        _WM_RBUTTONUP: (MouseButton.RIGHT, False),
        _WM_MBUTTONDOWN: (MouseButton.MIDDLE, True),
        _WM_MBUTTONUP: (MouseButton.MIDDLE, False),
    }
    if message in buttons:
        button, is_down = buttons[message]
        return MouseButtonEvent(button, is_down, injected=injected)
    if message in (_WM_XBUTTONDOWN, _WM_XBUTTONUP):
        x_button = (int(native.mouseData) >> 16) & 0xFFFF
        button = MouseButton.X1 if x_button == _XBUTTON1 else MouseButton.X2
        return MouseButtonEvent(button, message == _WM_XBUTTONDOWN, injected=injected)
    if message in (_WM_MOUSEWHEEL, _WM_MOUSEHWHEEL):
        delta = ctypes.c_int16((int(native.mouseData) >> 16) & 0xFFFF).value
        return MouseWheelEvent(
            delta,
            horizontal=message == _WM_MOUSEHWHEEL,
            injected=injected,
        )
    return None


__all__ = ["HookEventQueue", "Win32InputHook"]
