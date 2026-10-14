from __future__ import annotations

from lan_input_share.ui.resources import application_icon
from lan_input_share.ui.tray import TrayController
from lan_input_share.ui.view_model import AppViewModel


class FakeRuntime:
    def __init__(self) -> None:
        self.paused = 0
        self.returned = 0

    def pause(self) -> None:
        self.paused += 1

    def return_local(self) -> None:
        self.returned += 1


def test_tray_pause_and_return_local_call_runtime_once(qtbot) -> None:  # type: ignore[no-untyped-def]
    runtime = FakeRuntime()
    view_model = AppViewModel(runtime)
    tray = TrayController(view_model)

    tray.pause_action.trigger()
    tray.return_local_action.trigger()

    assert runtime.paused == 1
    assert runtime.returned == 1
    assert not tray.tray.icon().isNull()


def test_tray_activation_restores_window(qtbot) -> None:  # type: ignore[no-untyped-def]
    view_model = AppViewModel(FakeRuntime())
    tray = TrayController(view_model)
    restored: list[bool] = []
    tray.restore_requested.connect(lambda: restored.append(True))

    tray._on_activated(tray.tray.ActivationReason.Trigger)

    assert restored == [True]


def test_tray_uses_shared_application_icon(qtbot) -> None:  # type: ignore[no-untyped-def]
    tray = TrayController(AppViewModel(FakeRuntime()))

    assert tray.tray.icon().cacheKey() == application_icon().cacheKey()
