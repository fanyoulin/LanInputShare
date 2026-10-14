from __future__ import annotations

from enum import StrEnum

from lan_input_share.input.events import KeyEvent

_VK_CONTROL = 0x11
_VK_ALT = 0x12
_VK_ESCAPE = 0x1B
_VK_1 = 0x31
_VK_2 = 0x32
_VK_3 = 0x33


class HotkeyAction(StrEnum):
    SELECT_DEVICE_1 = "select_device_1"
    SELECT_DEVICE_2 = "select_device_2"
    SELECT_DEVICE_3 = "select_device_3"
    RETURN_LOCAL = "return_local"


class HotkeyMatcher:
    def __init__(self) -> None:
        self._pressed: set[int] = set()
        self._fired: set[int] = set()

    def feed(self, event: KeyEvent) -> HotkeyAction | None:
        key = event.virtual_key
        if not event.is_down:
            self._pressed.discard(key)
            self._fired.discard(key)
            return None
        self._pressed.add(key)
        if key in self._fired:
            return None
        if _VK_CONTROL not in self._pressed or _VK_ALT not in self._pressed:
            return None
        actions = {
            _VK_1: HotkeyAction.SELECT_DEVICE_1,
            _VK_2: HotkeyAction.SELECT_DEVICE_2,
            _VK_3: HotkeyAction.SELECT_DEVICE_3,
            _VK_ESCAPE: HotkeyAction.RETURN_LOCAL,
        }
        action = actions.get(key)
        if action is not None:
            self._fired.add(key)
        return action


__all__ = ["HotkeyAction", "HotkeyMatcher"]
