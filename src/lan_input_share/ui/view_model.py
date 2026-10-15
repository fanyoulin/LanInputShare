from __future__ import annotations

import asyncio
import inspect
import os
import socket
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import PurePosixPath
from threading import Lock
from typing import Any

from PySide6.QtCore import QObject, Signal

from lan_input_share.config.models import AppConfig, PeerTarget
from lan_input_share.core.models import DeviceRole, ScreenPosition
from lan_input_share.elevated.models import HelperState, HelperStatus
from lan_input_share.logging_setup import sanitize_transfer_error
from lan_input_share.routing.layout import CrossLayout
from lan_input_share.runtime.service import OverwritePromptRequest
from lan_input_share.security.pairing import PairingRequest
from lan_input_share.transfer.preflight import PasteDecision
from lan_input_share.transfer.status import FileTransferStatus, TransferState
from lan_input_share.ui.close_behavior import CloseChoice, ClosePreference


@dataclass(frozen=True, slots=True)
class PairingView:
    device_name: str
    short_fingerprint: str
    request_id: str


@dataclass(frozen=True, slots=True)
class OverwritePromptView:
    transfer_id: str
    conflict_count: int
    conflict_preview: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.transfer_id or self.conflict_count <= 0:
            raise ValueError("覆盖确认视图无效")
        if len(self.conflict_preview) > 5:
            raise ValueError("覆盖确认预览最多包含 5 条")


@dataclass(frozen=True, slots=True)
class SlotView:
    position: ScreenPosition
    label: str
    host: str
    port: int
    device_name: str = ""
    status: str = "unconfigured"


@dataclass(frozen=True, slots=True)
class ConnectionStatusView:
    source_ip: str
    target_ip: str
    adapter: str
    latency_ms: float
    tls: str

    def display_text(self) -> str:
        return (
            f"源 IP: {self.source_ip}  → 目标 IP: {self.target_ip}\n"
            f"网卡: {self.adapter} | 延迟: {self.latency_ms:.1f} ms | TLS: {self.tls}"
        )


@dataclass(frozen=True, slots=True)
class TransferView:
    transfer_id: str
    state: TransferState
    file_name: str
    bytes_transferred: int
    total_bytes: int
    percent: int
    bytes_per_second: float
    error: str | None = None
    can_cancel: bool = True


@dataclass(frozen=True, slots=True)
class HelperStatusView:
    mode: HelperState
    text: str
    can_enable: bool
    can_repair: bool
    can_remove: bool


class AppViewModel(QObject):
    validation_changed = Signal(bool)
    connection_status_changed = Signal(str)
    pairing_requested = Signal(object)
    pairing_answered = Signal(str, bool)
    overwrite_requested = Signal(object)
    slot_status_changed = Signal(object)
    transfer_changed = Signal(object)
    helper_status_changed = Signal(object)
    paused_changed = Signal(bool)
    diagnostics_requested = Signal()
    startup_requested = Signal(bool)
    elevation_requested = Signal()
    firewall_requested = Signal()
    quit_requested = Signal()

    def __init__(self, runtime: object) -> None:
        super().__init__()
        self._runtime = runtime
        self._device_name = _default_device_name()
        self._role: DeviceRole | None = None
        self._adapter_id = "auto"
        self._local_position: ScreenPosition | None = None
        self._peers: dict[ScreenPosition, PeerTarget] = {}
        self._pending_pairings: dict[str, Future[bool]] = {}
        self._pending_overwrites: dict[str, Future[PasteDecision]] = {}
        self._overwrite_lock = Lock()
        self._close_preference = ClosePreference()
        self._paused = False
        self._connection_status_text = "尚未连接"
        self._transfer: TransferView | None = None
        self._helper_status = _helper_status_view(
            HelperStatus(HelperState.NOT_INSTALLED, "")
        )

    @property
    def can_start(self) -> bool:
        base_valid = bool(
            self._device_name.strip() and self._role and self._adapter_id.strip()
        )
        if not base_valid:
            return False
        if self._role is DeviceRole.CLIENT:
            return True
        return self._layout_is_connected()

    @property
    def connection_status_text(self) -> str:
        return self._connection_status_text

    @property
    def transfer(self) -> TransferView | None:
        return self._transfer

    @property
    def helper_status(self) -> HelperStatusView:
        return self._helper_status

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def role(self) -> DeviceRole | None:
        return self._role

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    @property
    def manual_ip(self) -> str:
        return ""

    @property
    def layout(self) -> tuple[str, ...]:
        return ()

    @property
    def local_position(self) -> ScreenPosition | None:
        return self._local_position

    @property
    def peers(self) -> tuple[PeerTarget, ...]:
        return tuple(self._peers.values())

    @property
    def close_preference(self) -> ClosePreference:
        return self._close_preference

    def load_config(self, config: AppConfig) -> None:
        self._device_name = config.device_name
        self._role = config.role
        self._adapter_id = config.adapter_id or "auto"
        self._local_position = config.local_position
        self._peers = {peer.position: peer for peer in config.peers}
        self._close_preference = config.close_preference
        self._emit_validation()

    def set_device_name(self, name: str) -> None:
        self._device_name = name.strip()
        self._emit_validation()

    def set_role(self, role: DeviceRole | None) -> None:
        self._role = role
        if role is DeviceRole.CLIENT:
            self._local_position = None
            self._peers.clear()
        elif role is DeviceRole.CONTROLLER and self._local_position is None:
            self._local_position = ScreenPosition.CENTER
        self._emit_validation()

    def set_adapter(self, adapter_id: str) -> None:
        self._adapter_id = adapter_id.strip()
        self._emit_validation()

    def set_manual_ip(self, address: str) -> None:
        _ = address

    def set_layout(self, device_ids: tuple[str, ...]) -> None:
        _ = device_ids

    def set_local_position(self, position: ScreenPosition) -> None:
        if position in self._peers:
            raise ValueError("本机位置已被客户端占用")
        self._local_position = position
        self._emit_validation()

    def set_peer(self, position: ScreenPosition, host: str, port: int) -> None:
        if position is self._local_position:
            raise ValueError("客户端不能占用本机位置")
        if position not in self._peers and len(self._peers) >= 2:
            raise ValueError("最多配置两个客户端")
        target = PeerTarget(position, host, port)
        for existing_position, existing in self._peers.items():
            if existing_position is not position and existing.host == target.host:
                raise ValueError("客户端 IP 不能重复")
        self._peers[position] = target
        self._emit_validation()

    def remove_peer(self, position: ScreenPosition) -> None:
        self._peers.pop(position, None)
        self._emit_validation()

    def show_pairing(self, pairing: PairingView) -> None:
        self.pairing_requested.emit(pairing)

    def decide_pairing(self, request: PairingRequest) -> Future[bool]:
        decision: Future[bool] = Future()
        self._pending_pairings[request.request_id] = decision
        self.show_pairing(
            PairingView(
                request.peer.device_name,
                request.short_fingerprint,
                request.request_id,
            )
        )
        return decision

    def answer_pairing(self, request_id: str, approved: bool) -> None:
        self.pairing_answered.emit(request_id, approved)
        pending = self._pending_pairings.pop(request_id, None)
        if pending is not None and not pending.done():
            pending.set_result(approved)
            return
        method = "approve_pairing" if approved else "reject_pairing"
        self._call_runtime(method, request_id)

    def decide_overwrite(
        self, request: OverwritePromptRequest
    ) -> Future[PasteDecision]:
        decision: Future[PasteDecision] = Future()
        with self._overwrite_lock:
            if self._pending_overwrites:
                decision.set_result(PasteDecision.CANCEL)
                return decision
            self._pending_overwrites[request.transfer_id] = decision
        decision.add_done_callback(
            lambda completed: self._discard_overwrite(
                request.transfer_id, completed
            )
        )
        self.overwrite_requested.emit(
            OverwritePromptView(
                request.transfer_id,
                request.conflict_count,
                request.conflict_preview,
            )
        )
        return decision

    def answer_overwrite(
        self, transfer_id: str, decision: PasteDecision
    ) -> None:
        with self._overwrite_lock:
            pending = self._pending_overwrites.pop(transfer_id, None)
        if pending is not None and not pending.done():
            pending.set_result(decision)

    def cancel_pending_overwrites(self) -> None:
        with self._overwrite_lock:
            pending = tuple(self._pending_overwrites.values())
            self._pending_overwrites.clear()
        for decision in pending:
            if not decision.done():
                decision.set_result(PasteDecision.CANCEL)

    def update_connection_status(self, status: ConnectionStatusView) -> None:
        self._connection_status_text = status.display_text()
        self.connection_status_changed.emit(self._connection_status_text)

    def show_status(self, message: str) -> None:
        self._connection_status_text = message
        self.connection_status_changed.emit(message)

    def update_transfer(self, transfer: TransferView) -> None:
        if not 0 <= transfer.percent <= 100:
            raise ValueError("传输进度必须在 0 到 100 之间")
        self._transfer = transfer
        self.transfer_changed.emit(transfer)

    def update_transfer_status(self, status: FileTransferStatus) -> None:
        total = max(0, status.total_bytes)
        transferred = min(max(0, status.bytes_transferred), total)
        percent = 100 if status.state is TransferState.COMPLETE else (
            int(transferred * 100 / total) if total else 0
        )
        self.update_transfer(
            TransferView(
                transfer_id=status.transfer_id,
                state=status.state,
                file_name=PurePosixPath(status.relative_path).name,
                bytes_transferred=transferred,
                total_bytes=total,
                percent=percent,
                bytes_per_second=max(0.0, status.bytes_per_second),
                error=sanitize_transfer_error(status.error)
                if status.state is TransferState.FAILED
                else None,
                can_cancel=status.state is TransferState.ACTIVE,
            )
        )

    def update_helper_status(self, status: HelperStatus) -> None:
        self._helper_status = _helper_status_view(status)
        self.helper_status_changed.emit(self._helper_status)

    def enable_input_helper(self) -> None:
        self._call_runtime("enable_input_helper")

    def repair_input_helper(self) -> None:
        self._call_runtime("repair_input_helper")

    def remove_input_helper(self) -> None:
        self._call_runtime("remove_input_helper")

    def pause(self) -> None:
        if self._paused:
            return
        self._paused = True
        self._call_runtime("pause")
        self.paused_changed.emit(True)

    def resume(self) -> None:
        if not self._paused:
            return
        self._paused = False
        self._call_runtime("resume")
        self.paused_changed.emit(False)

    def return_local(self) -> None:
        self._call_runtime("return_local")

    def cancel_transfer(self) -> None:
        if self._transfer is not None:
            self._call_runtime("cancel_transfer", self._transfer.transfer_id)

    def request_diagnostics(self) -> None:
        self.diagnostics_requested.emit()

    def request_startup(self, enabled: bool) -> None:
        self.startup_requested.emit(enabled)

    def request_elevation(self) -> None:
        self.elevation_requested.emit()

    def request_firewall(self) -> None:
        self.firewall_requested.emit()

    def shutdown(self) -> None:
        self.cancel_pending_overwrites()
        self._call_runtime("shutdown")
        self.quit_requested.emit()

    def remember_close_choice(self, choice: CloseChoice | None) -> None:
        self._close_preference = ClosePreference(choice)
        self._call_runtime("remember_close_choice", choice)

    def apply_settings(self) -> None:
        if not self.can_start or self._role is None:
            if self._role is DeviceRole.CONTROLLER and not self._layout_is_connected():
                raise ValueError("屏幕布局不连通，请通过中间位置连接设备")
            raise ValueError("设备名称、角色和网卡未填写完整")
        config = AppConfig(
            version=2,
            device_name=self._device_name,
            role=self._role,
            adapter_id=None if self._adapter_id == "auto" else self._adapter_id,
            bind_ip=None,
            port=24870,
            local_position=self._local_position,
            peers=self.peers,
        )
        self._call_runtime("configure", config)

    def apply_layout_immediately(self) -> None:
        self.return_local()
        self.apply_settings()

    def update_slot(self, slot: SlotView) -> None:
        self.slot_status_changed.emit(slot)

    def update_slot_status(
        self, position: ScreenPosition, device_name: str, status: str
    ) -> None:
        peer = self._peers.get(position)
        self.update_slot(
            SlotView(
                position=position,
                label=position.value,
                host="" if peer is None else peer.host,
                port=24870 if peer is None else peer.port,
                device_name=device_name,
                status=status,
            )
        )

    def _layout_is_connected(self) -> bool:
        if self._local_position is None:
            return False
        positions = {self._local_position: "local"}
        positions.update(
            {position: f"peer:{position.value}" for position in self._peers}
        )
        try:
            CrossLayout(positions).validate_connected()
        except ValueError:
            return False
        return True

    def _emit_validation(self) -> None:
        self.validation_changed.emit(self.can_start)

    def _discard_overwrite(
        self, transfer_id: str, completed: Future[PasteDecision]
    ) -> None:
        with self._overwrite_lock:
            if self._pending_overwrites.get(transfer_id) is completed:
                self._pending_overwrites.pop(transfer_id, None)

    def _call_runtime(self, method_name: str, *args: object) -> None:
        method = getattr(self._runtime, method_name, None)
        if not callable(method):
            return
        result = cast_callable(method)(*args)
        if not inspect.isawaitable(result):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_await_result(result))
        else:
            asyncio.ensure_future(result, loop=loop)


def cast_callable(value: Callable[..., Any]) -> Callable[..., Any]:
    return value


async def _await_result(value: Awaitable[Any]) -> None:
    await value


def _format_bytes(value: int | float) -> str:
    amount = max(0.0, float(value))
    units = ("B", "KB", "MB", "GB", "TB")
    unit = units[0]
    for candidate in units:
        unit = candidate
        if amount < 1024 or candidate == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{int(amount)} B"
    return f"{amount:.1f} {unit}"


def _default_device_name() -> str:
    return (os.environ.get("COMPUTERNAME") or socket.gethostname()).strip()


def _helper_status_view(status: HelperStatus) -> HelperStatusView:
    state = status.state
    detail = status.detail.strip()
    if state in {HelperState.NOT_INSTALLED, HelperState.DISABLED}:
        return HelperStatusView(
            state,
            "未启用（当前使用标准控制）",
            True,
            False,
            False,
        )
    if state is HelperState.RUNNING:
        return HelperStatusView(
            state,
            "增强控制可用",
            False,
            False,
            True,
        )
    if state is HelperState.SECURE_DESKTOP:
        return HelperStatusView(
            state,
            "客户端处于 UAC 安全桌面，需在客户端本机确认",
            False,
            False,
            True,
        )
    if state is HelperState.REPAIR_REQUIRED:
        text = "增强控制需要修复"
        if detail:
            text += f"：{detail}"
        return HelperStatusView(state, text, False, True, True)
    text = "增强控制暂不可用"
    if detail:
        text += f"：{detail}"
    return HelperStatusView(state, text, False, True, True)


__all__ = [
    "AppViewModel",
    "ConnectionStatusView",
    "HelperStatusView",
    "OverwritePromptView",
    "PairingView",
    "SlotView",
    "TransferView",
]
