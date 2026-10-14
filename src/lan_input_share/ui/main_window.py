from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lan_input_share.core.models import DeviceRole
from lan_input_share.network.adapters import list_adapters, selected_ipv4
from lan_input_share.ui.close_behavior import CloseChoice, ask_close_choice
from lan_input_share.ui.cross_layout_editor import CrossLayoutEditor
from lan_input_share.ui.resources import application_icon
from lan_input_share.ui.view_model import (
    AppViewModel,
    PairingView,
    SlotView,
)


class MainWindow(QMainWindow):
    def __init__(
        self,
        view_model: AppViewModel,
        *,
        can_minimize_to_tray: Callable[[], bool] | None = None,
        minimized_to_tray: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._view_model = view_model
        self._can_minimize_to_tray = can_minimize_to_tray or (lambda: True)
        self._minimized_to_tray = minimized_to_tray or (lambda: None)
        self.setWindowTitle("局域网键鼠共享")
        self.setWindowIcon(application_icon())
        self.resize(640, 560)
        root = QWidget(self)
        layout = QVBoxLayout(root)

        settings = QGroupBox("本机设置")
        form = QFormLayout(settings)
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("deviceNameEdit")
        self.role_combo = QComboBox()
        self.role_combo.addItem("请选择角色", None)
        self.role_combo.addItem("主控", DeviceRole.CONTROLLER)
        self.role_combo.addItem("客户端", DeviceRole.CLIENT)
        self.adapter_combo = QComboBox()
        self.adapter_combo.setEditable(True)
        self.adapter_combo.addItem("", "")
        self.adapter_combo.addItem("自动选择物理网卡", "auto")
        form.addRow("设备名称", self.name_edit)
        form.addRow("角色", self.role_combo)
        form.addRow("网卡", self.adapter_combo)
        layout.addWidget(settings)

        self.controller_settings_box = QGroupBox("主控屏幕布局")
        controller_layout = QVBoxLayout(self.controller_settings_box)
        controller_layout.addWidget(QLabel("选择本机位置，再点击槽位填写客户端 IP"))
        self.cross_editor = CrossLayoutEditor()
        controller_layout.addWidget(self.cross_editor)
        layout.addWidget(self.controller_settings_box)

        self.client_settings_box = QGroupBox("客户端监听")
        client_layout = QFormLayout(self.client_settings_box)
        self.client_ip_label = QLabel()
        self.client_ip_label.setObjectName("clientLanIpLabel")
        self.client_ip_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.client_port_label = QLabel("24870")
        self.bound_controller_label = QLabel(
            "已信任主控可自动连接；新主控首次连接时将在本机确认"
        )
        self.bound_controller_label.setWordWrap(True)
        client_layout.addRow("本机局域网 IP", self.client_ip_label)
        client_layout.addRow("监听端口", self.client_port_label)
        client_layout.addRow("连接确认", self.bound_controller_label)
        layout.addWidget(self.client_settings_box)

        self.status_label = QLabel(view_model.connection_status_text)
        self.status_label.setObjectName("connectionStatusLabel")
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.start_button = QPushButton("保存并启动")
        self.start_button.setObjectName("startButton")
        self.start_button.setEnabled(view_model.can_start)
        diagnostics = QPushButton("导出诊断")
        return_local = QPushButton("返回主控")
        buttons.addWidget(self.start_button)
        buttons.addWidget(return_local)
        buttons.addWidget(diagnostics)
        layout.addLayout(buttons)
        self.setCentralWidget(root)

        self.name_edit.setText(view_model.device_name)
        if view_model.role is not None:
            self.role_combo.setCurrentIndex(self.role_combo.findData(view_model.role))
        adapter_index = self.adapter_combo.findData(view_model.adapter_id)
        if adapter_index >= 0:
            self.adapter_combo.setCurrentIndex(adapter_index)
        else:
            self.adapter_combo.setEditText(view_model.adapter_id)
        if view_model.local_position is not None:
            self.cross_editor.set_local_position(view_model.local_position)
        for peer in view_model.peers:
            self.cross_editor.set_peer(peer.position, peer.host, peer.port)

        self.name_edit.textChanged.connect(view_model.set_device_name)
        self.role_combo.currentIndexChanged.connect(self._role_changed)
        self.adapter_combo.currentIndexChanged.connect(self._adapter_changed)
        self.adapter_combo.editTextChanged.connect(self._adapter_text_changed)
        self.cross_editor.layout_changed.connect(self._cross_layout_changed)
        self.cross_editor.peer_moved.connect(view_model.apply_layout_immediately)
        self.cross_editor.validation_failed.connect(
            lambda message: QMessageBox.warning(
                self, "屏幕排列无效", message
            )
        )
        self.start_button.clicked.connect(view_model.apply_settings)
        diagnostics.clicked.connect(view_model.request_diagnostics)
        return_local.clicked.connect(view_model.return_local)
        view_model.validation_changed.connect(self.start_button.setEnabled)
        view_model.connection_status_changed.connect(self.status_label.setText)
        view_model.pairing_requested.connect(self._show_pairing)
        view_model.slot_status_changed.connect(self._update_slot)
        self._update_role_panels(view_model.role)
        self._refresh_client_ip()

    def restore(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:
        choice = self._view_model.close_preference.remembered
        remember = False
        if choice is None:
            choice, remember = ask_close_choice(self)
        if choice is None:
            event.ignore()
            return
        if remember:
            self._view_model.remember_close_choice(choice)
        if choice is CloseChoice.MINIMIZE_TO_TRAY:
            if not self._can_minimize_to_tray():
                QMessageBox.warning(
                    self,
                    "无法最小化到托盘",
                    "系统托盘图标当前不可用，窗口将保持打开。",
                )
                self.restore()
                event.ignore()
                return
            self.hide()
            self._minimized_to_tray()
            event.ignore()
            return
        self._view_model.shutdown()
        event.accept()

    def _role_changed(self, index: int) -> None:
        value = self.role_combo.itemData(index)
        try:
            role = DeviceRole(str(value))
        except ValueError:
            role = None
        self._view_model.set_role(role)
        self._update_role_panels(role)
        self._refresh_client_ip()

    def _adapter_changed(self, index: int) -> None:
        value = self.adapter_combo.itemData(index)
        self._view_model.set_adapter(str(value or self.adapter_combo.currentText()))
        self._refresh_client_ip()

    def _adapter_text_changed(self, text: str) -> None:
        index = self.adapter_combo.currentIndex()
        if index < 0 or self.adapter_combo.itemText(index) != text:
            self._view_model.set_adapter(text)
        self._refresh_client_ip()

    def _refresh_client_ip(self) -> None:
        adapter_id = self._view_model.adapter_id
        manual_id = None if adapter_id in {"", "auto"} else adapter_id
        address = selected_ipv4(list_adapters(), manual_id)
        self.client_ip_label.setText(
            address or "未检测到可用的局域网 IP"
        )

    def _cross_layout_changed(self) -> None:
        try:
            self._view_model.set_local_position(self.cross_editor.local_position)
            current = {peer.position for peer in self._view_model.peers}
            desired = set(self.cross_editor.peers)
            for position in current - desired:
                self._view_model.remove_peer(position)
            for position, (host, port) in self.cross_editor.peers.items():
                self._view_model.set_peer(position, host, port)
        except ValueError as exc:
            QMessageBox.warning(self, "屏幕排列无效", str(exc))

    def _update_role_panels(self, role: DeviceRole | None) -> None:
        self.controller_settings_box.setVisible(role is DeviceRole.CONTROLLER)
        self.client_settings_box.setVisible(role is DeviceRole.CLIENT)

    def _show_pairing(self, value: object) -> None:
        if not isinstance(value, PairingView):
            return
        self.restore()
        answer = QMessageBox.question(
            self,
            "确认设备配对",
            f"设备：{value.device_name}\n证书指纹：{value.short_fingerprint}\n\n是否信任？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        self._view_model.answer_pairing(
            value.request_id, answer is QMessageBox.StandardButton.Yes
        )

    def _update_slot(self, value: object) -> None:
        if not isinstance(value, SlotView):
            return
        self.cross_editor.set_slot_status(
            value.position, value.device_name, value.status
        )

__all__ = ["MainWindow"]
