from __future__ import annotations

from collections.abc import Callable

import pytest

from lan_input_share.elevated.client import HelperUnavailableError
from lan_input_share.elevated.models import HelperState, HelperStatus
from lan_input_share.input.events import KeyEvent
from lan_input_share.input.routed_injector import RoutedInputInjector


class FakeInjector:
    def __init__(self) -> None:
        self.injected: list[object] = []
        self.released: list[tuple[object, ...]] = []

    def inject(self, event: object) -> None:
        self.injected.append(event)

    def release_all(self, events: tuple[object, ...]) -> None:
        self.released.append(events)


class FakeHelper(FakeInjector):
    def __init__(self, state: HelperState) -> None:
        super().__init__()
        self.status = HelperStatus(state, state.value)
        self.callbacks: list[Callable[[HelperStatus], None]] = []
        self.fail_next = False

    def add_status_callback(self, callback: Callable[[HelperStatus], None]) -> None:
        self.callbacks.append(callback)

    def set_state(self, state: HelperState) -> None:
        self.status = HelperStatus(state, state.value)
        for callback in tuple(self.callbacks):
            callback(self.status)

    def inject(self, event: object) -> None:
        if self.fail_next:
            self.fail_next = False
            self.set_state(HelperState.UNAVAILABLE)
            raise HelperUnavailableError("gone")
        super().inject(event)


def test_running_helper_is_the_only_injection_route() -> None:
    local = FakeInjector()
    helper = FakeHelper(HelperState.RUNNING)
    routed = RoutedInputInjector(local, helper)
    event = KeyEvent(42, 0x10, True)

    routed.inject(event)

    assert helper.injected == [event]
    assert local.injected == []


@pytest.mark.parametrize(
    "state",
    [
        HelperState.NOT_INSTALLED,
        HelperState.UNAVAILABLE,
        HelperState.REPAIR_REQUIRED,
        HelperState.DISABLED,
    ],
)
def test_unavailable_helper_states_fall_back_locally(state: HelperState) -> None:
    local = FakeInjector()
    helper = FakeHelper(state)
    routed = RoutedInputInjector(local, helper)
    event = KeyEvent(42, 0x10, True)

    routed.inject(event)

    assert local.injected == [event]
    assert helper.injected == []


def test_secure_desktop_suppresses_input_without_local_fallback() -> None:
    local = FakeInjector()
    helper = FakeHelper(HelperState.SECURE_DESKTOP)
    routed = RoutedInputInjector(local, helper)

    routed.inject(KeyEvent(42, 0x10, True))

    assert local.injected == []
    assert helper.injected == []


def test_release_is_delivered_once_to_the_route_that_received_press() -> None:
    local = FakeInjector()
    helper = FakeHelper(HelperState.RUNNING)
    routed = RoutedInputInjector(local, helper)
    down = KeyEvent(42, 0x10, True)
    up = KeyEvent(42, 0x10, False)
    routed.inject(down)

    routed.release_all((up,))
    routed.release_all((up,))

    assert helper.released == [(up,)]
    assert local.released == []


def test_state_change_immediately_releases_held_input_on_previous_route() -> None:
    local = FakeInjector()
    helper = FakeHelper(HelperState.RUNNING)
    routed = RoutedInputInjector(local, helper)
    down = KeyEvent(42, 0x10, True)
    routed.inject(down)

    helper.set_state(HelperState.UNAVAILABLE)

    assert helper.released == [(KeyEvent(42, 0x10, False),)]
    assert local.released == []


def test_helper_failure_falls_back_current_event_without_duplicate_injection() -> None:
    local = FakeInjector()
    helper = FakeHelper(HelperState.RUNNING)
    routed = RoutedInputInjector(local, helper)
    helper.fail_next = True
    event = KeyEvent(42, 0x10, True)

    routed.inject(event)

    assert helper.injected == []
    assert local.injected == [event]
