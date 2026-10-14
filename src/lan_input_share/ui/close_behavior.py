from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QMessageBox, QWidget


class CloseChoice(StrEnum):
    MINIMIZE_TO_TRAY = "minimize_to_tray"
    QUIT = "quit"


@dataclass(frozen=True, slots=True)
class ClosePreference:
    remembered: CloseChoice | None = None


def build_close_dialog(parent: QWidget | None) -> QMessageBox:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QCheckBox, QMessageBox

    dialog = QMessageBox(parent)
    dialog.setWindowTitle("关闭程序")
    dialog.setText("请选择关闭窗口后的操作")
    dialog.setIcon(QMessageBox.Icon.Question)
    dialog.addButton("最小化到托盘", QMessageBox.ButtonRole.AcceptRole)
    dialog.addButton("退出程序", QMessageBox.ButtonRole.DestructiveRole)
    # QMessageBox disables its title-bar close button when it cannot determine
    # an escape action. Keep a hidden reject button so X/Esc remain a real
    # cancel operation without adding a third visible button.
    cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    cancel.hide()
    dialog.setEscapeButton(cancel)
    dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
    dialog.setCheckBox(QCheckBox("记住我的选择", dialog))
    return dialog


def ask_close_choice(parent: QWidget | None) -> tuple[CloseChoice | None, bool]:
    dialog = build_close_dialog(parent)
    dialog.exec()
    clicked = dialog.clickedButton()
    if clicked is None:
        return None, False
    choices = {
        "最小化到托盘": CloseChoice.MINIMIZE_TO_TRAY,
        "退出程序": CloseChoice.QUIT,
    }
    checkbox = dialog.checkBox()
    return choices.get(clicked.text()), bool(checkbox and checkbox.isChecked())


__all__ = [
    "CloseChoice",
    "ClosePreference",
    "ask_close_choice",
    "build_close_dialog",
]
