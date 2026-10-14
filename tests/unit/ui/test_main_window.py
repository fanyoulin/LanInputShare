from __future__ import annotations

import pytest
from PySide6.QtWidgets import QGroupBox, QMessageBox, QPushButton

from lan_input_share.core.models import DeviceRole, ScreenPosition
from lan_input_share.ui.close_behavior import CloseChoice, build_close_dialog
from lan_input_share.ui.cross_layout_editor import CrossLayoutEditor
from lan_input_share.ui.main_window import MainWindow
from lan_input_share.ui.resources import application_icon
from lan_input_share.ui.view_model import AppViewModel, PairingView


class FakeRuntime:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.shutdowns = 0
        self.close_choices: list[CloseChoice | None] = []
        self.configured = []

    def cancel_transfer(self, transfer_id: str) -> None:
        self.cancelled.append(transfer_id)

    def shutdown(self) -> None:
        self.shutdowns += 1

    def remember_close_choice(self, choice: CloseChoice | None) -> None:
        self.close_choices.append(choice)

    def configure(self, config) -> None:  # type: ignore[no-untyped-def]
        self.configured.append(config)


@pytest.fixture(autouse=True)
def close_windows_without_blocking(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "lan_input_share.ui.main_window.ask_close_choice",
        lambda _parent: (CloseChoice.QUIT, False),
    )


def test_pairing_dialog_displays_device_name_and_short_fingerprint(
    qtbot, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, title, text, _buttons: captured.append((title, text))
        or QMessageBox.StandardButton.No,
    )
    view_model = AppViewModel(FakeRuntime())
    window = MainWindow(view_model)
    qtbot.addWidget(window)
    window.hide()

    view_model.show_pairing(PairingView("Laptop", "ABCD EFGH IJKL MNOP", "request-1"))

    assert captured
    assert window.isVisible()
    assert "Laptop" in captured[0][1]
    assert "ABCD EFGH IJKL MNOP" in captured[0][1]


def test_main_window_no_longer_contains_file_transfer_controls(qtbot) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow(AppViewModel(FakeRuntime()))
    qtbot.addWidget(window)

    assert window.findChild(QPushButton, "cancelTransferButton") is None
    assert all(box.title() != "文件传输" for box in window.findChildren(QGroupBox))


def test_main_window_uses_shared_application_icon(qtbot) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow(AppViewModel(FakeRuntime()))
    qtbot.addWidget(window)

    assert not window.windowIcon().isNull()
    assert window.windowIcon().cacheKey() == application_icon().cacheKey()


def test_selecting_visible_role_and_adapter_enables_start_button(qtbot) -> None:  # type: ignore[no-untyped-def]
    view_model = AppViewModel(FakeRuntime())
    window = MainWindow(view_model)
    qtbot.addWidget(window)

    window.name_edit.setText("MainPC")
    window.role_combo.setCurrentIndex(1)
    window.adapter_combo.setCurrentIndex(1)

    assert window.start_button.isEnabled()


def test_controller_shows_cross_editor_and_client_hides_position_fields(qtbot) -> None:  # type: ignore[no-untyped-def]
    view_model = AppViewModel(FakeRuntime())
    window = MainWindow(view_model)
    qtbot.addWidget(window)
    window.show()

    window.role_combo.setCurrentIndex(window.role_combo.findData(DeviceRole.CONTROLLER))
    assert window.cross_editor.isVisible()
    window.cross_editor.set_local_position(ScreenPosition.CENTER)

    window.role_combo.setCurrentIndex(window.role_combo.findData(DeviceRole.CLIENT))
    assert not window.cross_editor.isVisible()
    assert window.client_settings_box.isVisible()
    assert window.findChild(CrossLayoutEditor, "crossLayoutEditor") is not None


def test_moving_connected_peer_applies_layout_without_global_save(qtbot) -> None:  # type: ignore[no-untyped-def]
    runtime = FakeRuntime()
    view_model = AppViewModel(runtime)
    view_model.set_device_name("Desk")
    view_model.set_role(DeviceRole.CONTROLLER)
    view_model.set_adapter("auto")
    view_model.set_local_position(ScreenPosition.CENTER)
    view_model.set_peer(ScreenPosition.LEFT, "192.168.110.157", 24870)
    window = MainWindow(view_model)
    qtbot.addWidget(window)

    window.cross_editor.slot_button(ScreenPosition.LEFT).click()
    combo = window.cross_editor.position_combo
    combo.setCurrentIndex(combo.findData(ScreenPosition.RIGHT))
    window.cross_editor.save_peer_button.click()

    assert len(runtime.configured) == 1
    assert runtime.configured[0].peers[0].position is ScreenPosition.RIGHT


def test_close_dialog_has_only_minimize_and_quit_buttons(qtbot) -> None:  # type: ignore[no-untyped-def]
    dialog = build_close_dialog(None)
    qtbot.addWidget(dialog)

    assert {button.text() for button in dialog.buttons() if not button.isHidden()} == {
        "最小化到托盘",
        "退出程序",
    }


def test_close_dialog_titlebar_x_is_cancel(qtbot) -> None:  # type: ignore[no-untyped-def]
    dialog = build_close_dialog(None)
    qtbot.addWidget(dialog)
    dialog.show()

    assert dialog.escapeButton() is not None
    assert dialog.escapeButton().isHidden()
    assert dialog.close()
    assert dialog.clickedButton() is dialog.escapeButton()


def test_window_close_can_minimize_to_tray_without_shutdown(
    qtbot, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    runtime = FakeRuntime()
    window = MainWindow(AppViewModel(runtime))
    qtbot.addWidget(window)
    window.show()
    monkeypatch.setattr(
        "lan_input_share.ui.main_window.ask_close_choice",
        lambda _parent: (CloseChoice.MINIMIZE_TO_TRAY, False),
    )

    window.close()

    assert not window.isVisible()
    assert runtime.shutdowns == 0


def test_window_stays_visible_when_tray_is_unavailable(
    qtbot, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    runtime = FakeRuntime()
    window = MainWindow(
        AppViewModel(runtime), can_minimize_to_tray=lambda: False
    )
    qtbot.addWidget(window)
    window.show()
    monkeypatch.setattr(
        "lan_input_share.ui.main_window.ask_close_choice",
        lambda _parent: (CloseChoice.MINIMIZE_TO_TRAY, False),
    )
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, text: warnings.append((title, text)),
    )

    window.close()

    assert window.isVisible()
    assert runtime.shutdowns == 0
    assert warnings and "托盘" in warnings[0][1]


def test_dialog_x_cancels_main_window_close(qtbot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runtime = FakeRuntime()
    window = MainWindow(AppViewModel(runtime))
    qtbot.addWidget(window)
    window.show()
    monkeypatch.setattr(
        "lan_input_share.ui.main_window.ask_close_choice",
        lambda _parent: (None, False),
    )

    window.close()

    assert window.isVisible()
    assert runtime.shutdowns == 0


def test_quit_choice_uses_safe_shutdown_and_can_be_remembered(
    qtbot, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    runtime = FakeRuntime()
    window = MainWindow(AppViewModel(runtime))
    qtbot.addWidget(window)
    window.show()
    monkeypatch.setattr(
        "lan_input_share.ui.main_window.ask_close_choice",
        lambda _parent: (CloseChoice.QUIT, True),
    )

    window.close()

    assert runtime.close_choices == [CloseChoice.QUIT]
    assert runtime.shutdowns == 1
