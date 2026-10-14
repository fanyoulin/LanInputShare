from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from lan_input_share.ui.resources import application_icon
from lan_input_share.ui.view_model import AppViewModel


class TrayController(QObject):
    restore_requested = Signal()

    def __init__(self, view_model: AppViewModel, icon: QIcon | None = None) -> None:
        super().__init__()
        self._view_model = view_model
        tray_icon = icon
        if tray_icon is None or tray_icon.isNull():
            tray_icon = application_icon()
        self.tray = QSystemTrayIcon(tray_icon, self)
        self.menu = QMenu()
        self.pause_action = QAction("暂停共享", self.menu)
        self.pause_action.setCheckable(True)
        self.return_local_action = QAction("返回主控", self.menu)
        self.settings_action = QAction("打开设置", self.menu)
        self.diagnostics_action = QAction("导出诊断", self.menu)
        self.quit_action = QAction("退出", self.menu)
        self.menu.addAction(self.pause_action)
        self.menu.addAction(self.return_local_action)
        self.menu.addSeparator()
        self.menu.addAction(self.settings_action)
        self.menu.addAction(self.diagnostics_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.tray.setContextMenu(self.menu)
        self.tray.setToolTip("局域网键鼠共享")
        self.pause_action.triggered.connect(self._toggle_pause)
        self.return_local_action.triggered.connect(view_model.return_local)
        self.diagnostics_action.triggered.connect(view_model.request_diagnostics)
        self.quit_action.triggered.connect(view_model.shutdown)
        self.tray.activated.connect(self._on_activated)
        view_model.paused_changed.connect(self.pause_action.setChecked)

    def show(self) -> bool:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        self.tray.show()
        QApplication.processEvents()
        return self.tray.isVisible()

    def hide(self) -> None:
        self.tray.hide()

    def is_available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable() and self.tray.isVisible()

    def show_minimized_message(self) -> None:
        if self.is_available():
            self.tray.showMessage(
                "局域网键鼠共享",
                "程序仍在运行，单击托盘图标可恢复窗口。",
                self.tray.icon(),
                2500,
            )

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.restore_requested.emit()

    def _toggle_pause(self, checked: bool) -> None:
        if checked:
            self._view_model.pause()
        else:
            self._view_model.resume()

__all__ = ["TrayController"]
