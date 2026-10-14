from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lan_input_share.transfer.status import TransferState
from lan_input_share.ui.view_model import TransferView


class FileTransferPopup(QWidget):
    def __init__(
        self,
        cancel_transfer: Callable[[str], None],
        *,
        terminal_close_ms: int = 800,
        failure_close_ms: int = 5_000,
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint,
        )
        if terminal_close_ms < 0 or failure_close_ms < 0:
            raise ValueError("传输浮窗关闭时间不能为负数")
        self._cancel_transfer = cancel_transfer
        self._terminal_close_ms = terminal_close_ms
        self._failure_close_ms = failure_close_ms
        self._transfer_id: str | None = None
        self._positioned_transfer_id: str | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        self.setObjectName("fileTransferPopup")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        self.file_label = QLabel()
        self.file_label.setObjectName("transferFileLabel")
        self.detail_label = QLabel()
        self.detail_label.setObjectName("transferDetailLabel")
        self.status_label = QLabel()
        self.status_label.setObjectName("transferStatusLabel")
        self.progress = QProgressBar()
        self.progress.setObjectName("transferProgressBar")
        self.progress.setRange(0, 100)
        self.cancel_button = QPushButton("取消传输")
        self.cancel_button.setObjectName("cancelTransferButton")
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self.cancel_button)
        layout.addWidget(self.file_label)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.progress)
        layout.addWidget(self.status_label)
        layout.addLayout(button_row)

        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self.hide)
        self.cancel_button.clicked.connect(self._cancel_current)

    def update_transfer(self, value: TransferView) -> None:
        is_new = value.transfer_id != self._transfer_id
        if is_new:
            self._close_timer.stop()
            self._transfer_id = value.transfer_id
        elif value.state is TransferState.ACTIVE:
            self._close_timer.stop()

        self.file_label.setText(value.file_name or "文件传输")
        self.detail_label.setText(
            f"{_format_bytes(value.bytes_transferred)} / "
            f"{_format_bytes(value.total_bytes)}"
        )
        self.progress.setValue(value.percent)
        self.cancel_button.setEnabled(value.can_cancel)
        speed = f"{_format_bytes(value.bytes_per_second)}/s"
        if value.state is TransferState.ACTIVE:
            status = speed
        elif value.state is TransferState.COMPLETE:
            status = f"传输完成  ·  {speed}"
        elif value.state is TransferState.CANCELLED:
            status = "已取消"
        else:
            status = f"{value.error or '传输失败'}  ·  {speed}"
        self.status_label.setText(status)

        if is_new or self._positioned_transfer_id != value.transfer_id:
            self.adjustSize()
            self._position_on_cursor_screen()
            self._positioned_transfer_id = value.transfer_id
        self.show()
        if value.state is not TransferState.ACTIVE:
            delay = (
                self._failure_close_ms
                if value.state is TransferState.FAILED
                else self._terminal_close_ms
            )
            self._close_timer.start(delay)

    def _cancel_current(self) -> None:
        if self._transfer_id is not None and self.cancel_button.isEnabled():
            self._cancel_transfer(self._transfer_id)

    def _position_on_cursor_screen(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        x = available.x() + (available.width() - self.width()) // 2
        y = available.y() + 16
        self.move(x, y)


def _format_bytes(value: int | float) -> str:
    amount = max(0.0, float(value))
    units = ("B", "KB", "MB", "GB", "TB")
    unit = units[0]
    for candidate in units:
        unit = candidate
        if amount < 1024 or candidate == units[-1]:
            break
        amount /= 1024
    return f"{int(amount)} B" if unit == "B" else f"{amount:.1f} {unit}"


__all__ = ["FileTransferPopup"]
