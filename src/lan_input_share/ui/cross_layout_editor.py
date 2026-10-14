from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from lan_input_share.config.models import ConfigError, PeerTarget
from lan_input_share.core.models import ScreenPosition

_POSITION_LABELS = {
    ScreenPosition.TOP: "上",
    ScreenPosition.BOTTOM: "下",
    ScreenPosition.LEFT: "左",
    ScreenPosition.CENTER: "中",
    ScreenPosition.RIGHT: "右",
}

_STATUS_LABELS = {
    "connecting": "连接中",
    "connected": "已连接",
    "retrying": "重试中",
    "unreachable": "无法连接",
    "authorization_required": "等待客户端确认",
    "busy": "客户端使用中",
    "rejected": "连接被拒绝",
    "identity_mismatch": "设备身份不匹配",
}


class CrossLayoutEditor(QWidget):
    layout_changed = Signal()
    peer_moved = Signal()
    validation_failed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("crossLayoutEditor")
        self._local_position = ScreenPosition.CENTER
        self._peers: dict[ScreenPosition, tuple[str, int]] = {}
        self._slot_details: dict[ScreenPosition, tuple[str, str]] = {}
        self._selected_position = ScreenPosition.LEFT
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        local_row = QHBoxLayout()
        local_row.addWidget(QLabel("本机位置"))
        self.local_combo = QComboBox()
        for position in (
            ScreenPosition.CENTER,
            ScreenPosition.TOP,
            ScreenPosition.BOTTOM,
            ScreenPosition.LEFT,
            ScreenPosition.RIGHT,
        ):
            self.local_combo.addItem(_POSITION_LABELS[position], position)
        local_row.addWidget(self.local_combo)
        local_row.addStretch(1)
        root.addLayout(local_row)

        cross = QGridLayout()
        self._buttons: dict[ScreenPosition, QPushButton] = {}
        coordinates = {
            ScreenPosition.TOP: (0, 1),
            ScreenPosition.LEFT: (1, 0),
            ScreenPosition.CENTER: (1, 1),
            ScreenPosition.RIGHT: (1, 2),
            ScreenPosition.BOTTOM: (2, 1),
        }
        for position, (row, column) in coordinates.items():
            button = QPushButton()
            button.setCheckable(True)
            button.setMinimumSize(90, 38)
            button.clicked.connect(
                lambda _checked=False, value=position: self._select_slot(value)
            )
            self._buttons[position] = button
            cross.addWidget(button, row, column)
        root.addLayout(cross)

        peer_row = QHBoxLayout()
        peer_row.addWidget(QLabel("位置"))
        self.position_combo = QComboBox()
        self.position_combo.setObjectName("peerPositionCombo")
        for position in (
            ScreenPosition.TOP,
            ScreenPosition.BOTTOM,
            ScreenPosition.LEFT,
            ScreenPosition.CENTER,
            ScreenPosition.RIGHT,
        ):
            self.position_combo.addItem(_POSITION_LABELS[position], position)
        peer_row.addWidget(self.position_combo)
        peer_row.addWidget(QLabel("客户端 IP"))
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("例如 192.168.1.20")
        self.host_edit.setMinimumWidth(120)
        peer_row.addWidget(self.host_edit, 1)
        peer_row.addWidget(QLabel("端口"))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(24870)
        self.port_spin.setMinimumWidth(110)
        self.port_spin.setMaximumWidth(130)
        peer_row.addWidget(self.port_spin)
        self.save_peer_button = QPushButton("设置")
        self.remove_peer_button = QPushButton("移除")
        peer_row.addWidget(self.save_peer_button)
        peer_row.addWidget(self.remove_peer_button)
        root.addLayout(peer_row)

        self.local_combo.currentIndexChanged.connect(self._local_combo_changed)
        self.save_peer_button.clicked.connect(self._save_selected_peer)
        self.remove_peer_button.clicked.connect(self._remove_selected_peer)
        self._refresh()
        self._select_slot(self._selected_position)

    @property
    def local_position(self) -> ScreenPosition:
        return self._local_position

    @property
    def peers(self) -> dict[ScreenPosition, tuple[str, int]]:
        return dict(self._peers)

    def slot_button(self, position: ScreenPosition) -> QPushButton:
        return self._buttons[position]

    def set_local_position(self, position: ScreenPosition) -> None:
        if position in self._peers:
            raise ValueError("本机位置已被客户端占用")
        self._local_position = position
        index = self.local_combo.findData(position)
        if index >= 0 and index != self.local_combo.currentIndex():
            self.local_combo.blockSignals(True)
            self.local_combo.setCurrentIndex(index)
            self.local_combo.blockSignals(False)
        self._refresh()
        self.layout_changed.emit()

    def set_peer(self, position: ScreenPosition, host: str, port: int) -> None:
        if position is self._local_position:
            raise ValueError("客户端不能占用本机位置")
        if position not in self._peers and len(self._peers) >= 2:
            raise ValueError("最多配置两个客户端")
        target = PeerTarget(position, host, port)
        self._peers[position] = (target.host, target.port)
        self._refresh()
        self.layout_changed.emit()

    def remove_peer(self, position: ScreenPosition) -> None:
        if self._peers.pop(position, None) is not None:
            self._slot_details.pop(position, None)
            self._refresh()
            self.layout_changed.emit()

    def set_slot_status(
        self, position: ScreenPosition, device_name: str, status: str
    ) -> None:
        self._slot_details[position] = (device_name, status)
        self._refresh()

    def _local_combo_changed(self, index: int) -> None:
        try:
            value = ScreenPosition(str(self.local_combo.itemData(index)))
            self.set_local_position(value)
        except (TypeError, ValueError) as exc:
            self.validation_failed.emit(str(exc))
            current = self.local_combo.findData(self._local_position)
            self.local_combo.blockSignals(True)
            self.local_combo.setCurrentIndex(current)
            self.local_combo.blockSignals(False)

    def _select_slot(self, position: ScreenPosition) -> None:
        self._selected_position = position
        for candidate, button in self._buttons.items():
            button.setChecked(candidate is position)
        peer = self._peers.get(position)
        position_index = self.position_combo.findData(position)
        if position_index >= 0:
            self.position_combo.setCurrentIndex(position_index)
        self.host_edit.setText("" if peer is None else peer[0])
        self.port_spin.setValue(24870 if peer is None else peer[1])
        editable = position is not self._local_position
        self.host_edit.setEnabled(editable)
        self.port_spin.setEnabled(editable)
        self.save_peer_button.setEnabled(editable)
        self.remove_peer_button.setEnabled(editable and peer is not None)

    def _save_selected_peer(self) -> None:
        try:
            target = ScreenPosition(str(self.position_combo.currentData()))
        except ValueError:
            self.validation_failed.emit("请选择客户端位置")
            return
        source = self._selected_position
        moving = source in self._peers and target is not source
        original_peer = self._peers.get(source)
        original_details = self._slot_details.get(source)
        try:
            if target is self._local_position:
                raise ValueError("客户端不能占用本机位置")
            if moving and target in self._peers:
                raise ValueError("目标位置已被其他客户端占用")
            if moving:
                self._peers.pop(source)
                details = self._slot_details.pop(source, None)
                if details is not None:
                    self._slot_details[target] = details
            self.set_peer(
                target,
                self.host_edit.text(),
                self.port_spin.value(),
            )
        except (ConfigError, ValueError) as exc:
            if moving and original_peer is not None:
                self._peers.pop(target, None)
                self._slot_details.pop(target, None)
                self._peers[source] = original_peer
                if original_details is not None:
                    self._slot_details[source] = original_details
                self._refresh()
            self.validation_failed.emit(str(exc))
            return
        self._select_slot(target)
        if moving:
            self.peer_moved.emit()

    def _remove_selected_peer(self) -> None:
        self.remove_peer(self._selected_position)
        self._select_slot(self._selected_position)

    def _refresh(self) -> None:
        for position, button in self._buttons.items():
            lines = [_POSITION_LABELS[position]]
            if position is self._local_position:
                lines.append("本机")
            elif position in self._peers:
                lines.append(self._peers[position][0])
                details = self._slot_details.get(position)
                if details is not None:
                    status = _STATUS_LABELS.get(details[1], details[1])
                    lines.append(f"{details[0]} · {status}")
            button.setText("\n".join(lines))


__all__ = ["CrossLayoutEditor"]
