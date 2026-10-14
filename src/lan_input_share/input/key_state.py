from __future__ import annotations

from lan_input_share.input.events import (
    InputEvent,
    KeyEvent,
    MouseButton,
    MouseButtonEvent,
)


class PressedInputState:
    def __init__(self) -> None:
        self._keys: dict[tuple[int, int, bool], KeyEvent] = {}
        self._buttons: dict[MouseButton, MouseButtonEvent] = {}

    def apply(self, event: InputEvent) -> None:
        if isinstance(event, KeyEvent):
            key = (event.scan_code, event.virtual_key, event.is_extended)
            if event.is_down:
                self._keys[key] = event
            else:
                self._keys.pop(key, None)
        elif isinstance(event, MouseButtonEvent):
            if event.is_down:
                self._buttons[event.button] = event
            else:
                self._buttons.pop(event.button, None)

    def release_events(self) -> tuple[InputEvent, ...]:
        keys = tuple(
            KeyEvent(
                scan_code=event.scan_code,
                virtual_key=event.virtual_key,
                is_down=False,
                is_extended=event.is_extended,
            )
            for event in self._keys.values()
        )
        buttons = tuple(
            MouseButtonEvent(button=event.button, is_down=False)
            for event in self._buttons.values()
        )
        return keys + buttons

    def clear(self) -> None:
        self._keys.clear()
        self._buttons.clear()


__all__ = ["PressedInputState"]
