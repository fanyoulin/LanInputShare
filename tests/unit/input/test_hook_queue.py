from __future__ import annotations

from lan_input_share.input.events import (
    KeyEvent,
    MouseButton,
    MouseButtonEvent,
    MouseMoveEvent,
    MouseWheelEvent,
    Point,
    Suppression,
)
from lan_input_share.input.win32_hook import HookEventQueue, Win32InputHook


def test_injected_events_are_filtered_and_never_requeued() -> None:
    seen: list[object] = []
    hook = Win32InputHook(install_native=False)
    hook.start(lambda event: seen.append(event) or Suppression.SUPPRESS)
    hook.set_remote_active(True)

    result = hook.dispatch_for_test(
        KeyEvent(scan_code=30, virtual_key=65, is_down=True, injected=True)
    )

    assert result is Suppression.PASS_THROUGH
    assert seen == []
    hook.stop()


def test_queue_overflow_coalesces_mouse_moves_but_keeps_keys_buttons_and_wheel() -> None:
    queue = HookEventQueue(capacity=3)
    queue.put(MouseMoveEvent(Point(10, 10)))
    queue.put(KeyEvent(scan_code=30, virtual_key=65, is_down=True))
    queue.put(MouseMoveEvent(Point(20, 20)))
    queue.put(MouseButtonEvent(MouseButton.LEFT, is_down=True))
    queue.put(MouseWheelEvent(delta=120))
    queue.put(MouseMoveEvent(Point(30, 30)))

    events = queue.drain()

    assert KeyEvent(scan_code=30, virtual_key=65, is_down=True) in events
    assert MouseButtonEvent(MouseButton.LEFT, is_down=True) in events
    assert MouseWheelEvent(delta=120) in events
    assert [event for event in events if isinstance(event, MouseMoveEvent)] == [
        MouseMoveEvent(Point(30, 30))
    ]


def test_remote_mouse_moves_are_not_coalesced_before_delta_conversion() -> None:
    queue = HookEventQueue(capacity=3)

    queue.put(MouseMoveEvent(Point(101, 100)), coalesce_mouse_moves=False)
    queue.put(MouseMoveEvent(Point(102, 100)), coalesce_mouse_moves=False)

    assert queue.drain() == (
        MouseMoveEvent(Point(101, 100)),
        MouseMoveEvent(Point(102, 100)),
    )


def test_emergency_hotkey_is_never_suppressed_from_local_recovery_path() -> None:
    hook = Win32InputHook(install_native=False)
    hook.start(lambda _event: Suppression.SUPPRESS)
    hook.set_remote_active(True)

    assert hook.dispatch_for_test(KeyEvent(29, 0x11, True)) is Suppression.SUPPRESS
    assert hook.dispatch_for_test(KeyEvent(56, 0x12, True)) is Suppression.SUPPRESS
    assert hook.dispatch_for_test(KeyEvent(1, 0x1B, True)) is Suppression.PASS_THROUGH
    assert hook.remote_active is False
    hook.stop()


def test_release_for_input_pressed_locally_passes_through_after_remote_switch() -> None:
    hook = Win32InputHook(install_native=False)
    hook.start(lambda _event: Suppression.SUPPRESS)

    assert hook.dispatch_for_test(KeyEvent(29, 0x11, True)) is Suppression.PASS_THROUGH
    assert (
        hook.dispatch_for_test(MouseButtonEvent(MouseButton.LEFT, True))
        is Suppression.PASS_THROUGH
    )
    hook.set_remote_active(True)

    assert hook.dispatch_for_test(KeyEvent(29, 0x11, False)) is Suppression.PASS_THROUGH
    assert (
        hook.dispatch_for_test(MouseButtonEvent(MouseButton.LEFT, False))
        is Suppression.PASS_THROUGH
    )
    assert hook.dispatch_for_test(KeyEvent(30, 65, True)) is Suppression.SUPPRESS
    assert hook.dispatch_for_test(KeyEvent(30, 65, False)) is Suppression.SUPPRESS
    hook.stop()


def test_explicit_local_suppression_can_intercept_cross_device_paste() -> None:
    hook = Win32InputHook(install_native=False)
    hook.start(lambda _event: Suppression.SUPPRESS_ALWAYS)

    assert hook.dispatch_for_test(KeyEvent(47, 0x56, True)) is Suppression.SUPPRESS
    assert hook.remote_active is False
    hook.stop()
