from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol

from lan_input_share.elevated.client import HelperUnavailableError
from lan_input_share.elevated.models import HelperState, HelperStatus
from lan_input_share.input.events import InputEvent
from lan_input_share.input.key_state import PressedInputState


class InputRoute(Protocol):
    def inject(self, event: InputEvent) -> None: ...

    def release_all(self, events: tuple[InputEvent, ...]) -> None: ...


class HelperInputRoute(InputRoute, Protocol):
    @property
    def status(self) -> HelperStatus: ...

    def add_status_callback(self, callback: Callable[[HelperStatus], None]) -> None: ...


class StaticHelperInput:
    def __init__(self, status: HelperStatus) -> None:
        self._status = status

    @property
    def status(self) -> HelperStatus:
        return self._status

    def add_status_callback(self, callback: Callable[[HelperStatus], None]) -> None:
        del callback

    def inject(self, event: InputEvent) -> None:
        del event
        raise HelperUnavailableError(self._status.detail)

    def release_all(self, events: tuple[InputEvent, ...]) -> None:
        del events

    def start(self) -> None:
        return

    def stop(self) -> None:
        return


_LOCAL_STATES = {
    HelperState.NOT_INSTALLED,
    HelperState.UNAVAILABLE,
    HelperState.REPAIR_REQUIRED,
    HelperState.DISABLED,
}


class RoutedInputInjector:
    def __init__(
        self,
        local: InputRoute,
        helper: HelperInputRoute,
        *,
        status_callback: Callable[[HelperStatus], None] | None = None,
    ) -> None:
        self._local = local
        self._helper = helper
        self._state = helper.status.state
        self._pressed = PressedInputState()
        self._lock = threading.RLock()
        self._status_callback = status_callback or (lambda _status: None)
        helper.add_status_callback(self._on_helper_status)
        self._status_callback(helper.status)

    @property
    def helper_status(self) -> HelperStatus:
        return self._helper.status

    def inject(self, event: InputEvent) -> None:
        with self._lock:
            route = self._route_for(self._state)
            if route is None:
                return
            try:
                route.inject(event)
            except HelperUnavailableError:
                if route is not self._helper:
                    raise
                self._release_held(self._helper)
                self._state = HelperState.UNAVAILABLE
                self._local.inject(event)
            self._pressed.apply(event)

    def release_all(self, events: tuple[InputEvent, ...]) -> None:
        del events
        with self._lock:
            releases = self._pressed.release_events()
            if not releases:
                return
            route = self._route_for(self._state)
            if route is not None:
                try:
                    route.release_all(releases)
                except HelperUnavailableError:
                    if route is not self._helper:
                        raise
            self._pressed.clear()

    def _on_helper_status(self, status: HelperStatus) -> None:
        with self._lock:
            old_route = self._route_for(self._state)
            new_route = self._route_for(status.state)
            if old_route is not new_route:
                self._release_held(old_route)
            self._state = status.state
        self._status_callback(status)

    def _release_held(self, route: InputRoute | None) -> None:
        releases = self._pressed.release_events()
        if releases and route is not None:
            with suppress(HelperUnavailableError):
                route.release_all(releases)
        self._pressed.clear()

    def _route_for(self, state: HelperState) -> InputRoute | None:
        if state is HelperState.RUNNING:
            return self._helper
        if state in _LOCAL_STATES:
            return self._local
        return None


__all__ = ["RoutedInputInjector", "StaticHelperInput"]
