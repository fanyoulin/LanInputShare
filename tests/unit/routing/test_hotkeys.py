from lan_input_share.input.events import KeyEvent
from lan_input_share.routing.hotkeys import HotkeyAction, HotkeyMatcher


def test_ctrl_alt_escape_always_returns_local() -> None:
    matcher = HotkeyMatcher()

    assert matcher.feed(KeyEvent(29, 0x11, True)) is None
    assert matcher.feed(KeyEvent(56, 0x12, True)) is None
    assert matcher.feed(KeyEvent(1, 0x1B, True)) is HotkeyAction.RETURN_LOCAL


def test_ctrl_alt_number_selects_layout_slot_once_until_key_up() -> None:
    matcher = HotkeyMatcher()
    matcher.feed(KeyEvent(29, 0x11, True))
    matcher.feed(KeyEvent(56, 0x12, True))

    assert matcher.feed(KeyEvent(2, 0x31, True)) is HotkeyAction.SELECT_DEVICE_1
    assert matcher.feed(KeyEvent(2, 0x31, True)) is None
    assert matcher.feed(KeyEvent(2, 0x31, False)) is None
    assert matcher.feed(KeyEvent(2, 0x31, True)) is HotkeyAction.SELECT_DEVICE_1
