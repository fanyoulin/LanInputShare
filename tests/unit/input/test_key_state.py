from lan_input_share.input.events import KeyEvent, MouseButton, MouseButtonEvent
from lan_input_share.input.key_state import PressedInputState


def test_key_state_emits_key_up_for_each_pressed_key_on_disconnect() -> None:
    state = PressedInputState()
    state.apply(KeyEvent(scan_code=30, virtual_key=65, is_down=True))
    state.apply(KeyEvent(scan_code=48, virtual_key=66, is_down=True, is_extended=True))
    state.apply(KeyEvent(scan_code=30, virtual_key=65, is_down=True))

    releases = state.release_events()

    assert releases == (
        KeyEvent(scan_code=30, virtual_key=65, is_down=False),
        KeyEvent(scan_code=48, virtual_key=66, is_down=False, is_extended=True),
    )
    state.clear()
    assert state.release_events() == ()


def test_mouse_button_is_released_on_target_switch() -> None:
    state = PressedInputState()
    state.apply(MouseButtonEvent(MouseButton.LEFT, is_down=True))
    state.apply(MouseButtonEvent(MouseButton.RIGHT, is_down=True))
    state.apply(MouseButtonEvent(MouseButton.LEFT, is_down=False))

    assert state.release_events() == (
        MouseButtonEvent(MouseButton.RIGHT, is_down=False),
    )
