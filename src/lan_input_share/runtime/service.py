from __future__ import annotations

import asyncio
import base64
import ctypes
import logging
import shutil
import ssl
import time
import uuid
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

from lan_input_share.clipboard.coordinator import ClipboardCoordinator
from lan_input_share.clipboard.models import (
    BroadcastClipboard,
    ClipboardImage,
    ClipboardItem,
    ClipboardKind,
    WriteLocalClipboard,
)
from lan_input_share.clipboard.win32_clipboard import (
    ClipboardBusyError,
    Win32ClipboardWatcher,
)
from lan_input_share.config.models import AppConfig, PeerTarget
from lan_input_share.config.paths import AppPaths
from lan_input_share.core.models import (
    DeviceInfo,
    DeviceRole,
    Endpoint,
    ScreenInfo,
    ScreenPosition,
)
from lan_input_share.input.events import KeyEvent
from lan_input_share.input.win32_cursor import Win32CursorController
from lan_input_share.input.win32_hook import Win32InputHook
from lan_input_share.input.win32_injector import Win32InputInjector
from lan_input_share.logging_setup import sanitize_transfer_error
from lan_input_share.network.adapters import list_adapters, selected_ipv4
from lan_input_share.network.backoff import ExponentialBackoff
from lan_input_share.network.connection import (
    SessionCallbacks,
    SessionConnectError,
    SessionListener,
    connect_session,
)
from lan_input_share.network.session import (
    CloseReason,
    ControlSession,
    SessionState,
)
from lan_input_share.protocol.messages import (
    ClipboardMessage,
    FileAbortMessage,
    FileDataMessage,
    FileOfferMessage,
    FilePasteDecisionMessage,
    FilePasteProbeMessage,
    FileRequestMessage,
    JsonValue,
    WireMessage,
)
from lan_input_share.routing.controller import RoutingController
from lan_input_share.routing.layout import CrossLayout
from lan_input_share.runtime.client import ClientRuntime
from lan_input_share.runtime.controller import ControllerRuntime
from lan_input_share.security.identity import DeviceIdentity, PeerHello
from lan_input_share.security.pairing import (
    PairingCoordinator,
    PairingDecisionCallback,
    PairingRequest,
)
from lan_input_share.security.tls import create_server_ssl_context
from lan_input_share.security.trust import TrustStore
from lan_input_share.transfer.control_stream import QueuedTransferStream
from lan_input_share.transfer.destination import (
    DestinationTransferWorkspace,
    DestinationWorkspace,
)
from lan_input_share.transfer.manifest import EntryKind, FileManifest, build_manifest
from lan_input_share.transfer.preflight import (
    PasteDecision,
    PasteDecisionResult,
    PastePreflightService,
    PasteProbe,
    filter_manifest,
)
from lan_input_share.transfer.protocol import TransferProgress
from lan_input_share.transfer.receiver import FileReceiver
from lan_input_share.transfer.sender import FileSender
from lan_input_share.transfer.staging import StagingArea, TransferWorkspace
from lan_input_share.transfer.status import FileTransferStatus, TransferState
from lan_input_share.transfer.windows_destination import (
    DestinationKind,
    ExplorerDestinationResolver,
    ResolvedDestination,
)
from lan_input_share.transfer.wire import manifest_from_payload, manifest_to_payload

_LOGGER = logging.getLogger("lan_input_share.runtime")

type StatusCallback = Callable[[str], None]
type SlotStatusCallback = Callable[[ScreenPosition, str, str], None]
type TransferStatusCallback = Callable[[FileTransferStatus], None]
type OverwriteDecider = Callable[[OverwritePromptRequest], Awaitable[PasteDecision]]

_FILE_MARKER_PREFIX = "lan-input-share-files:"
_FILE_RATE_LIMIT_BYTES_PER_SECOND = 4 * 1024 * 1024
_FILE_NETWORK_UTILIZATION = 0.75
_CLIPBOARD_IMAGE_CHUNK_SIZE = 32 * 1024
_MAX_CLIPBOARD_IMAGE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _FileOffer:
    manifest: FileManifest
    source_id: str


@dataclass(slots=True)
class _ImageReceiveState:
    transfer_id: str
    sender_id: str
    origin_id: str
    generation: str
    sequence: int
    digest: str
    image_format: str
    raw_size: int
    compressed_size: int
    compressed: bytearray


@dataclass(frozen=True, slots=True)
class OverwritePromptRequest:
    transfer_id: str
    conflict_count: int
    conflict_preview: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.transfer_id or self.conflict_count <= 0:
            raise ValueError("覆盖确认请求无效")
        if len(self.conflict_preview) > 5:
            raise ValueError("覆盖确认预览最多包含 5 条")


class DestinationResolver(Protocol):
    async def resolve_foreground(self) -> ResolvedDestination: ...


class _PastePhase(StrEnum):
    OFFERED = "offered"
    PROBING = "probing"
    AWAITING_DECISION = "awaiting_decision"
    APPROVED = "approved"
    TRANSFERRING = "transferring"
    TERMINAL = "terminal"


@dataclass(slots=True)
class _PasteOperation:
    transfer_id: str
    source_id: str
    target_id: str
    manifest: FileManifest
    phase: _PastePhase = _PastePhase.OFFERED

    def transition(self, phase: _PastePhase) -> None:
        allowed = {
            _PastePhase.OFFERED: {_PastePhase.PROBING, _PastePhase.TERMINAL},
            _PastePhase.PROBING: {
                _PastePhase.AWAITING_DECISION,
                _PastePhase.APPROVED,
                _PastePhase.TERMINAL,
            },
            _PastePhase.AWAITING_DECISION: {
                _PastePhase.APPROVED,
                _PastePhase.TERMINAL,
            },
            _PastePhase.APPROVED: {_PastePhase.TRANSFERRING, _PastePhase.TERMINAL},
            _PastePhase.TRANSFERRING: {_PastePhase.TERMINAL},
            _PastePhase.TERMINAL: set(),
        }
        if phase not in allowed[self.phase]:
            raise RuntimeError(f"文件粘贴状态转换无效：{self.phase} -> {phase}")
        self.phase = phase


@dataclass(frozen=True, slots=True)
class _TargetPreflight:
    source_id: str
    target_id: str
    manifest: FileManifest
    destination: ResolvedDestination
    result: PasteDecisionResult


@dataclass(slots=True)
class _TransferProgressState:
    transfer_id: str
    current_file: str
    total_bytes: int
    started_at: float
    bytes_transferred: int = 0
    shown: bool = False
    last_emit_at: float = 0.0
    timer: asyncio.Task[None] | None = None


class _ControlMessageWriter:
    def __init__(
        self,
        send: Callable[[bytes], Awaitable[None]],
        *,
        bytes_per_second: int | None = None,
        utilization: float = _FILE_NETWORK_UTILIZATION,
        clock: Callable[[], float] = time.perf_counter,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if bytes_per_second is not None and bytes_per_second <= 0:
            raise ValueError("文件传输限速必须大于零")
        if not 0 < utilization <= 1:
            raise ValueError("文件传输网络占用比例无效")
        self._send = send
        self._bytes_per_second = bytes_per_second
        self._utilization = utilization
        self._clock = clock
        self._sleeper = sleeper

    async def write(self, data: bytes) -> None:
        started_at = self._clock()
        await self._send(data)
        if self._bytes_per_second is None:
            return
        send_elapsed = max(self._clock() - started_at, 0.0)
        rate_delay = max(
            len(data) / self._bytes_per_second - send_elapsed,
            0.0,
        )
        reserve_delay = send_elapsed * (
            (1.0 - self._utilization) / self._utilization
        )
        delay = max(rate_delay, reserve_delay)
        if delay > 0:
            await self._sleeper(delay)

    async def drain(self) -> None:
        await asyncio.sleep(0)


@dataclass(frozen=True, slots=True)
class RoleNetworkPlan:
    listener: Endpoint | None
    targets: tuple[PeerTarget, ...]


def build_role_network_plan(config: AppConfig, bind_ip: str | None) -> RoleNetworkPlan:
    if config.role is DeviceRole.CLIENT:
        return RoleNetworkPlan(Endpoint(bind_ip or "0.0.0.0", config.port), ())
    return RoleNetworkPlan(None, config.peers)


def reachable_connected_positions(
    local_position: ScreenPosition,
    connected_positions: set[ScreenPosition],
) -> set[ScreenPosition]:
    if local_position is ScreenPosition.CENTER:
        return set(connected_positions)
    if ScreenPosition.CENTER not in connected_positions:
        return set()
    return set(connected_positions)


class NetworkRoleService:
    """Runnable keyboard/mouse service for one configured controller or client."""

    def __init__(
        self,
        config: AppConfig,
        paths: AppPaths,
        *,
        status: StatusCallback | None = None,
        slot_status: SlotStatusCallback | None = None,
        pairing_decider: PairingDecisionCallback | None = None,
        pairing_timeout: float = 30.0,
        transfer_status: TransferStatusCallback | None = None,
        transfer_progress_delay: float = 1.0,
        transfer_progress_interval: float = 0.1,
        overwrite_decider: OverwriteDecider | None = None,
        destination_resolver: DestinationResolver | None = None,
        overwrite_timeout: float = 300.0,
    ) -> None:
        if transfer_progress_delay < 0 or transfer_progress_interval < 0:
            raise ValueError("文件传输进度时间参数不能为负数")
        if overwrite_timeout <= 0:
            raise ValueError("覆盖确认超时必须为正数")
        self._config = config
        self._paths = paths
        self._status = status or (lambda _message: None)
        self._slot_status = slot_status or (
            lambda _position, _device_name, _status: None
        )
        self._pairing_decider = pairing_decider or _reject_pairing
        self._pairing_timeout = pairing_timeout
        self._transfer_status = transfer_status or (lambda _status: None)
        self._transfer_progress_delay = transfer_progress_delay
        self._transfer_progress_interval = transfer_progress_interval
        self._overwrite_decider = overwrite_decider or _cancel_overwrite
        self._destination_resolver = destination_resolver or ExplorerDestinationResolver()
        self._overwrite_timeout = overwrite_timeout
        self._preflight = PastePreflightService()
        self._identity: DeviceIdentity | None = None
        self._local_device: DeviceInfo | None = None
        self._listener: SessionListener | None = None
        self._controller_runtime: ControllerRuntime | None = None
        self._client_runtime: ClientRuntime | None = None
        self._client_session: ControlSession | None = None
        self._controller_sessions: dict[PeerTarget, ControlSession] = {}
        self._pairing: PairingCoordinator | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clipboard: Win32ClipboardWatcher | None = None
        self._clipboard_coordinator: ClipboardCoordinator | None = None
        self._clipboard_generation = uuid.uuid4().hex
        self._clipboard_sequence = 0
        self._wire_sequence = 1_000_000
        self._trust_store = TrustStore(paths.root / "trusted-devices.json")
        self._file_offer: _FileOffer | None = None
        self._image_receivers: dict[str, _ImageReceiveState] = {}
        self._local_file_manifests: dict[str, FileManifest] = {}
        self._file_requests: dict[str, tuple[str, str]] = {}
        self._paste_operations: dict[str, _PasteOperation] = {}
        self._paste_tasks: dict[str, asyncio.Task[None]] = {}
        self._paste_probe_tasks: dict[str, asyncio.Task[None]] = {}
        self._paste_decision_waiters: dict[
            str, asyncio.Future[PasteDecisionResult]
        ] = {}
        self._target_preflights: dict[str, _TargetPreflight] = {}
        self._target_paste_transfer_id: str | None = None
        self._resolved_destinations: dict[str, ResolvedDestination] = {}
        self._file_streams: dict[str, QueuedTransferStream] = {}
        self._file_receive_tasks: dict[str, asyncio.Task[None]] = {}
        self._file_send_tasks: dict[str, asyncio.Task[None]] = {}
        self._file_senders: dict[str, FileSender] = {}
        self._file_receivers: dict[str, FileReceiver] = {}
        self._transfer_progress: dict[str, _TransferProgressState] = {}
        self._completed_file_targets: set[tuple[str, str]] = set()
        self._staging = StagingArea(
            paths.staging_dir,
            retention_seconds=config.staging_retention_hours * 60 * 60,
        )
        self._staging_workspaces: dict[str, TransferWorkspace] = {}
        self._destination_workspaces: dict[str, DestinationWorkspace] = {}
        self._referenced_transfer_id: str | None = None

    async def start(self) -> None:
        self._stop.clear()
        self._loop = asyncio.get_running_loop()
        self._identity = DeviceIdentity.load_or_create(
            self._paths, self._config.device_name
        )
        self._pairing = PairingCoordinator(self._identity, self._trust_store)
        self._local_device = DeviceInfo(
            self._identity.device_id,
            self._config.device_name,
            self._config.role,
            _primary_screen(),
        )
        self._clipboard_coordinator = ClipboardCoordinator(
            self._identity.device_id,
            forward_remote=self._config.role is DeviceRole.CONTROLLER,
        )
        self._clipboard = Win32ClipboardWatcher()
        self._clipboard.start(self._clipboard_changed_from_windows)
        self._paths.staging_dir.mkdir(parents=True, exist_ok=True)
        try:
            marker = self._clipboard.read_marker()
        except ClipboardBusyError:
            marker = ""
        preserve: set[str] = set()
        if marker.startswith(_FILE_MARKER_PREFIX):
            transfer_id = marker.removeprefix(_FILE_MARKER_PREFIX)
            if transfer_id:
                self._referenced_transfer_id = transfer_id
                preserve.add(transfer_id)
        self._staging.cleanup_orphans(preserve=preserve)
        plan = build_role_network_plan(self._config, _selected_bind_ip(self._config))
        if plan.listener is not None:
            await self._start_client_listener(plan.listener)
        else:
            await self._start_controller_connections(plan.targets)

    async def stop(self) -> None:
        self._stop.set()
        if self._clipboard is not None:
            self._clipboard.stop()
            self._clipboard = None
        for stream in self._file_streams.values():
            stream.abort("服务已停止")
        for waiter in self._paste_decision_waiters.values():
            if not waiter.done():
                waiter.cancel()
        paste_tasks = tuple(self._paste_tasks.values())
        for task in paste_tasks:
            task.cancel()
        if paste_tasks:
            await asyncio.gather(*paste_tasks, return_exceptions=True)
        self._paste_tasks.clear()
        await self._cancel_paste_probe_tasks()
        self._paste_decision_waiters.clear()
        self._paste_operations.clear()
        self._target_preflights.clear()
        self._target_paste_transfer_id = None
        self._resolved_destinations.clear()
        self._destination_workspaces.clear()
        transfer_tasks = [
            *self._file_receive_tasks.values(),
            *self._file_send_tasks.values(),
        ]
        for task in transfer_tasks:
            task.cancel()
        if transfer_tasks:
            await asyncio.gather(*transfer_tasks, return_exceptions=True)
        self._file_streams.clear()
        self._image_receivers.clear()
        self._file_receive_tasks.clear()
        self._file_send_tasks.clear()
        self._file_senders.clear()
        self._file_receivers.clear()
        for progress in self._transfer_progress.values():
            if progress.timer is not None:
                progress.timer.cancel()
        self._transfer_progress.clear()
        if self._controller_runtime is not None:
            await self._controller_runtime.stop()
            self._controller_runtime = None
        if self._client_runtime is not None:
            await self._client_runtime.stop()
            self._client_runtime = None
        sessions = [*self._controller_sessions.values()]
        if self._client_session is not None:
            sessions.append(self._client_session)
        await asyncio.gather(
            *(session.close(CloseReason.LOCAL_REQUEST) for session in sessions),
            return_exceptions=True,
        )
        self._controller_sessions.clear()
        self._client_session = None
        if self._listener is not None:
            await self._listener.stop()
            self._listener = None
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def pause(self) -> None:
        if self._controller_runtime is not None:
            await self._controller_runtime.pause()

    async def resume(self) -> None:
        if self._controller_runtime is not None:
            await self._controller_runtime.resume()

    async def return_local(self) -> None:
        if self._controller_runtime is not None:
            await self._controller_runtime.return_local()

    async def cancel_transfer(self, transfer_id: str) -> None:
        sender = self._file_senders.get(transfer_id)
        receiver = self._file_receivers.get(transfer_id)
        if sender is not None:
            sender.cancel()
        if receiver is not None:
            receiver.cancel()
        stream = self._file_streams.get(transfer_id)
        if stream is not None:
            stream.abort("用户取消")
        self._finish_transfer(
            transfer_id,
            success=False,
            error="用户取消",
            cancelled=True,
        )
        await self._broadcast_file_abort(transfer_id, "用户取消")

    async def _cancel_pastes_for_device(self, device_id: str, reason: str) -> None:
        transfer_ids = {
            transfer_id
            for transfer_id, operation in self._paste_operations.items()
            if device_id in (operation.source_id, operation.target_id)
        }
        transfer_ids.update(
            transfer_id
            for transfer_id, preflight in self._target_preflights.items()
            if device_id in (preflight.source_id, preflight.target_id)
        )
        for transfer_id in transfer_ids:
            sender = self._file_senders.get(transfer_id)
            receiver = self._file_receivers.get(transfer_id)
            if sender is not None:
                sender.cancel()
            if receiver is not None:
                receiver.cancel()
            stream = self._file_streams.get(transfer_id)
            if stream is not None:
                stream.abort(reason)
            self._finish_transfer(transfer_id, success=False, error=reason)
            self._terminate_paste(transfer_id)
            await self._broadcast_file_abort(transfer_id, reason)

    async def _start_client_listener(self, endpoint: Endpoint) -> None:
        assert self._identity is not None and self._local_device is not None
        self._listener = SessionListener(
            self._local_device,
            self._allow_inbound_controller,
            ssl_context=create_server_ssl_context(self._identity),
            local_certificate_der=self._identity.certificate_der,
            certificate_authenticator=self._verify_inbound_controller,
            certificate_approver=self._approve_inbound_controller,
            challenge_signer=self._identity.sign,
        )
        bound = await self._listener.start(endpoint, self._accept_controller_session)
        self._status(f"客户端监听 {bound.host}:{bound.port}")

    async def _start_controller_connections(
        self, targets: tuple[PeerTarget, ...]
    ) -> None:
        await self._rebuild_controller_runtime()
        for target in targets:
            self._safe_slot_status(target.position, "", "connecting")
            self._tasks.append(
                asyncio.create_task(self._supervise_controller_connection(target))
            )

    async def _supervise_controller_connection(self, target: PeerTarget) -> None:
        while not self._stop.is_set():
            try:
                await self._controller_connection_loop(target)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._safe_status(
                    f"客户端 {target.host} 连接任务异常，正在重启连接任务："
                    f"{sanitize_transfer_error(str(exc))}"
                )
            if not self._stop.is_set():
                await asyncio.sleep(0.5)

    async def _rebuild_controller_runtime(
        self,
    ) -> None:
        assert self._local_device is not None
        if self._controller_runtime is not None:
            await self._controller_runtime.stop()
        devices = {self._local_device.device_id: self._local_device}
        positions: dict[ScreenPosition, str] = {}
        assert self._config.local_position is not None
        positions[self._config.local_position] = self._local_device.device_id
        reachable = reachable_connected_positions(
            self._config.local_position,
            {target.position for target in self._controller_sessions},
        )
        sessions = tuple(
            (target, session)
            for target, session in self._controller_sessions.items()
            if target.position in reachable
        )
        for target, session in sessions:
            if session.peer is not None:
                devices[session.peer.device_id] = session.peer
                positions[target.position] = session.peer.device_id
        routing = RoutingController(
            self._local_device.device_id, devices, CrossLayout(positions)
        )
        runtime = ControllerRuntime(
            routing,
            Win32InputHook(),
            Win32CursorController(),
            message_handler=self._controller_message,
            paste_handler=self._intercept_file_paste,
        )
        for _target, session in sessions:
            runtime.attach_session(session)
        await runtime.start()
        self._controller_runtime = runtime
        self._status(f"已连接客户端：{len(sessions)}")

    async def _controller_connection_loop(self, target: PeerTarget) -> None:
        assert self._local_device is not None and self._identity is not None
        backoff = ExponentialBackoff(initial=0.5, maximum=5.0)
        while not self._stop.is_set():
            session: ControlSession | None = None
            attached = False
            try:
                local_ip = _selected_bind_ip(self._config)
                session = await connect_session(
                    Endpoint(target.host, target.port),
                    self._local_device,
                    self._identity,
                    self._outbound_callbacks(target),
                    local_ip,
                    handshake_timeout=self._pairing_timeout + 5.0,
                )
                self._controller_sessions[target] = session
                attached = True
                await self._rebuild_controller_runtime()
                assert session.peer is not None
                self._safe_slot_status(
                    target.position, session.peer.name, "connected"
                )
                self._safe_status(f"已连接客户端 {target.host}:{target.port}")
                backoff.reset()
                await session.wait_closed()
            except SessionConnectError as exc:
                if exc.reason is CloseReason.AUTHENTICATION_FAILED:
                    status = "authorization_required"
                    message = (
                        f"客户端 {target.host} 尚未授权："
                        "请在客户端确认连接；若没有弹窗，请检查设备身份"
                    )
                elif exc.reason is CloseReason.ROLE_CONFLICT:
                    status = "busy"
                    message = f"客户端 {target.host} 正在被其他主控使用"
                else:
                    status = "rejected"
                    message = f"客户端 {target.host} 拒绝连接：{exc}"
                self._safe_slot_status(target.position, "", status)
                self._safe_status(message)
            except (OSError, ConnectionError, TimeoutError, ssl.SSLError) as exc:
                self._safe_slot_status(target.position, "", "unreachable")
                self._safe_status(f"连接客户端 {target.host} 失败：{exc}")
            except Exception as exc:
                self._safe_slot_status(target.position, "", "unreachable")
                self._safe_status(
                    f"连接客户端 {target.host} 运行错误："
                    f"{sanitize_transfer_error(str(exc))}"
                )
            finally:
                if attached:
                    self._controller_sessions.pop(target, None)
                    if session is not None and session.peer is not None:
                        try:
                            await self._cancel_pastes_for_device(
                                session.peer.device_id, "设备连接已断开"
                            )
                        except Exception as exc:
                            self._safe_status(
                                "断线清理失败："
                                f"{sanitize_transfer_error(str(exc))}"
                            )
                    if not self._stop.is_set():
                        try:
                            await self._rebuild_controller_runtime()
                        except Exception as exc:
                            self._safe_status(
                                "输入运行器重建失败："
                                f"{sanitize_transfer_error(str(exc))}"
                            )
            if not self._stop.is_set():
                self._safe_slot_status(target.position, "", "retrying")
                await asyncio.sleep(backoff.next_delay())

    def _safe_status(self, message: str) -> None:
        try:
            self._status(message)
        except Exception:
            _LOGGER.exception("status callback failed")

    def _safe_slot_status(
        self, position: ScreenPosition, device_name: str, status: str
    ) -> None:
        try:
            self._slot_status(position, device_name, status)
        except Exception:
            _LOGGER.exception("slot status callback failed")

    async def _accept_controller_session(self, session: ControlSession) -> None:
        previous = self._client_session
        if previous is not None and previous.state not in (
            SessionState.CLOSING,
            SessionState.CLOSED,
        ) and not self._is_same_controller_reconnect(previous, session.peer):
            await session.close(CloseReason.ROLE_CONFLICT)
            return
        if previous is not None:
            stale_runtime = self._client_runtime
            self._client_session = None
            self._client_runtime = None
            if previous.state not in (SessionState.CLOSING, SessionState.CLOSED):
                try:
                    await previous.close(CloseReason.LOCAL_REQUEST)
                except Exception as exc:
                    self._status(
                        f"旧连接关闭失败：{sanitize_transfer_error(str(exc))}"
                    )
            if stale_runtime is not None:
                try:
                    await stale_runtime.stop()
                except Exception as exc:
                    self._status(
                        f"旧连接清理失败：{sanitize_transfer_error(str(exc))}"
                    )
        self._client_session = session
        runtime = ClientRuntime(
            session,
            Win32InputInjector(),
            message_handler=self._client_message,
        )
        self._client_runtime = runtime
        await runtime.start()
        assert session.peer is not None
        self._status(f"已连接主控 {session.peer.name}")
        self._tasks.append(asyncio.create_task(self._watch_client_session(session)))

    async def _watch_client_session(self, session: ControlSession) -> None:
        await session.wait_closed()
        if self._client_session is not session:
            return
        runtime = self._client_runtime
        self._client_session = None
        self._client_runtime = None
        cleanup_errors: list[Exception] = []
        if session.peer is not None:
            try:
                await self._cancel_pastes_for_device(
                    session.peer.device_id, "主控连接已断开"
                )
            except Exception as exc:
                cleanup_errors.append(exc)
        try:
            await self._cancel_paste_probe_tasks()
        except Exception as exc:
            cleanup_errors.append(exc)
        if runtime is not None:
            try:
                await runtime.stop()
            except Exception as exc:
                cleanup_errors.append(exc)
        for cleanup_error in cleanup_errors:
            self._status(
                f"断线清理失败：{sanitize_transfer_error(str(cleanup_error))}"
            )
        self._status("主控已断开")

    def _allow_inbound_controller(self, peer: DeviceInfo) -> bool | CloseReason:
        if peer.role is not DeviceRole.CONTROLLER:
            return False
        existing = self._client_session
        if existing is None or existing.state in (
            SessionState.CLOSING,
            SessionState.CLOSED,
        ):
            return True
        if self._is_same_controller_reconnect(existing, peer):
            return True
        return CloseReason.ROLE_CONFLICT

    @staticmethod
    def _is_same_controller_reconnect(
        existing: ControlSession, peer: DeviceInfo | None
    ) -> bool:
        return (
            peer is not None
            and existing.peer is not None
            and existing.peer.device_id == peer.device_id
        )

    def _verify_inbound_controller(
        self, peer: DeviceInfo, certificate_der: bytes
    ) -> bool:
        known = next(
            (
                item
                for item in self._trust_store.trusted_devices()
                if item.device_id == peer.device_id
            ),
            None,
        )
        if known is not None:
            return self._trust_store.verify(peer.device_id, certificate_der)
        return self._trust_store.find_by_certificate(certificate_der) is None

    async def _approve_inbound_controller(
        self, peer: DeviceInfo, certificate_der: bytes
    ) -> None:
        if self._trust_store.verify(peer.device_id, certificate_der):
            return
        assert self._pairing is not None
        approved = await self._pairing.decide_controller(
            PeerHello(peer.device_id, peer.name, certificate_der),
            self._pairing_decider,
            timeout=self._pairing_timeout,
        )
        if not approved:
            raise PermissionError("用户拒绝了主控配对")
        self._status(f"已信任主控：{peer.name}")

    def _outbound_callbacks(self, target: PeerTarget) -> SessionCallbacks:
        endpoint_key = f"{target.host}:{target.port}"

        def authenticate(peer: DeviceInfo) -> bool:
            return (
                peer.role is DeviceRole.CLIENT
                and (
                    target.expected_device_id is None
                    or target.expected_device_id == peer.device_id
                )
            )

        def verify(peer: DeviceInfo, certificate_der: bytes) -> bool:
            if self._trust_store.verify_endpoint(
                endpoint_key, peer.device_id, certificate_der
            ):
                return True
            endpoint_known = self._trust_store.endpoint_binding(endpoint_key)
            if endpoint_known is not None:
                return False
            existing = next(
                (
                    item
                    for item in self._trust_store.trusted_devices()
                    if item.device_id == peer.device_id
                ),
                None,
            )
            return existing is None or self._trust_store.verify(
                peer.device_id, certificate_der
            )

        def approve(peer: DeviceInfo, certificate_der: bytes) -> None:
            self._trust_store.approve_endpoint(
                endpoint_key, peer.device_id, certificate_der
            )

        return SessionCallbacks(authenticate, verify, approve)

    def _clipboard_changed_from_windows(self) -> None:
        loop = self._loop
        if loop is None or self._clipboard is None or self._identity is None:
            return
        try:
            marker = self._clipboard.read_marker()
            file_paths = self._clipboard.read_file_drop()
        except ClipboardBusyError:
            return
        referenced = self._referenced_transfer_id
        if referenced is not None and marker != f"{_FILE_MARKER_PREFIX}{referenced}":
            workspace = self._staging_workspaces.get(referenced)
            if workspace is not None:
                workspace.clipboard_referenced = False
            self._referenced_transfer_id = None
            self._staging.cleanup()
            self._staging.cleanup_orphans()
        if marker.startswith(_FILE_MARKER_PREFIX):
            return
        if file_paths:
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._handle_local_files(file_paths))
            )
            return
        image_reader = getattr(self._clipboard, "read_image", None)
        try:
            image = image_reader() if callable(image_reader) else None
        except ClipboardBusyError:
            return
        if image is not None:
            image_item = _image_item_from_marker(marker, image)
            if image_item is None:
                self._clipboard_sequence += 1
                image_item = ClipboardItem.image(
                    self._identity.device_id,
                    self._clipboard_sequence,
                    ClipboardImage(image.format, image.data),
                    generation=self._clipboard_generation,
                )
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._handle_local_clipboard(image_item))
            )
            return
        try:
            text = self._clipboard.read_unicode_text()
        except ClipboardBusyError:
            return
        self._file_offer = None
        self._completed_file_targets.clear()
        item = _item_from_marker(marker, text)
        if item is None:
            self._clipboard_sequence += 1
            item = ClipboardItem.text(
                self._identity.device_id,
                self._clipboard_sequence,
                text,
                generation=self._clipboard_generation,
            )
        loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._handle_local_clipboard(item))
        )

    async def _handle_local_clipboard(self, item: ClipboardItem) -> None:
        coordinator = self._clipboard_coordinator
        if coordinator is None:
            return
        await self._apply_clipboard_actions(coordinator.local_changed(item))

    async def _controller_message(self, _device_id: str, message: WireMessage) -> None:
        if isinstance(message, FileOfferMessage):
            await self._receive_file_offer(_device_id, message)
            return
        if isinstance(message, FileDataMessage):
            await self._receive_file_data(_device_id, message)
            return
        if isinstance(message, FileAbortMessage):
            await self._receive_file_abort(_device_id, message)
            return
        if isinstance(message, FileRequestMessage):
            await self._receive_file_request(_device_id, message)
            return
        if isinstance(message, FilePasteProbeMessage):
            return
        if isinstance(message, FilePasteDecisionMessage):
            await self._receive_file_paste_decision(_device_id, message)
            return
        if not isinstance(message, ClipboardMessage):
            return
        self._file_offer = None
        self._completed_file_targets.clear()
        await self._receive_clipboard_message(_device_id, message)

    async def _client_message(self, message: WireMessage) -> None:
        assert self._client_session is not None and self._client_session.peer is not None
        controller_id = self._client_session.peer.device_id
        if isinstance(message, FileOfferMessage):
            await self._receive_file_offer(controller_id, message)
            return
        if isinstance(message, FileRequestMessage):
            await self._receive_file_request(controller_id, message)
            return
        if isinstance(message, FileDataMessage):
            await self._receive_file_data(controller_id, message)
            return
        if isinstance(message, FileAbortMessage):
            await self._receive_file_abort(controller_id, message)
            return
        if isinstance(message, FilePasteProbeMessage):
            self._spawn_file_paste_probe(controller_id, message)
            return
        if isinstance(message, FilePasteDecisionMessage):
            return
        if not isinstance(message, ClipboardMessage):
            return
        self._file_offer = None
        self._completed_file_targets.clear()
        await self._receive_clipboard_message(controller_id, message)

    async def _receive_clipboard_message(
        self, sender_id: str, message: ClipboardMessage
    ) -> None:
        if message.payload.get("kind") == ClipboardKind.IMAGE.value:
            state = _image_receive_state(sender_id, message)
            if state is None:
                return
            for transfer_id, existing in tuple(self._image_receivers.items()):
                if (
                    existing.origin_id == state.origin_id
                    and existing.generation == state.generation
                    and existing.sequence < state.sequence
                ):
                    self._image_receivers.pop(transfer_id, None)
            self._image_receivers[state.transfer_id] = state
            return
        item = _clipboard_item_from_message(message)
        coordinator = self._clipboard_coordinator
        if item is not None and coordinator is not None:
            await self._apply_clipboard_actions(coordinator.remote_received(item))

    async def _apply_clipboard_actions(self, actions: tuple[object, ...]) -> None:
        for action in actions:
            if isinstance(action, WriteLocalClipboard):
                if (
                    self._clipboard is not None
                    and action.item.kind is ClipboardKind.TEXT
                    and isinstance(action.item.payload, str)
                ):
                    self._clipboard.write_unicode_text(
                        action.item.payload, action.item.marker
                    )
                elif (
                    self._clipboard is not None
                    and action.item.kind is ClipboardKind.IMAGE
                    and isinstance(action.item.payload, ClipboardImage)
                ):
                    self._clipboard.write_image(action.item.payload, action.item.marker)
            elif isinstance(action, BroadcastClipboard):
                await self._broadcast_clipboard(action)

    async def _broadcast_clipboard(self, action: BroadcastClipboard) -> None:
        sessions: tuple[ControlSession, ...]
        if self._config.role is DeviceRole.CONTROLLER:
            sessions = tuple(self._controller_sessions.values())
        else:
            sessions = () if self._client_session is None else (self._client_session,)
        active_sessions = tuple(
            session
            for session in sessions
            if session.state is SessionState.ACTIVE
            and session.peer is not None
            and session.peer.device_id != action.exclude_origin_id
        )
        if (
            action.item.kind is ClipboardKind.IMAGE
            and isinstance(action.item.payload, ClipboardImage)
        ):
            if len(action.item.payload.data) > _MAX_CLIPBOARD_IMAGE_BYTES:
                self._status("截图过大，未同步到其他设备")
                return
            compressed = await asyncio.to_thread(zlib.compress, action.item.payload.data)
            if len(compressed) > _MAX_CLIPBOARD_IMAGE_BYTES:
                self._status("截图过大，未同步到其他设备")
                return
            for session in active_sessions:
                await self._send_clipboard_image(session, action.item, compressed)
            return
        wire_payload: JsonValue
        if isinstance(action.item.payload, str):
            wire_payload = action.item.payload
        elif isinstance(action.item.payload, tuple):
            wire_payload = list(action.item.payload)
        else:
            return
        for session in active_sessions:
            await session.send(
                ClipboardMessage(
                    session.session_id,
                    self._wire_sequence,
                    {
                        "origin_id": action.item.origin_id,
                        "generation": action.item.generation,
                        "sequence": action.item.sequence,
                        "digest": action.item.digest,
                        "kind": action.item.kind.value,
                        "payload": wire_payload,
                    },
                )
            )
            self._wire_sequence += 1

    async def _send_clipboard_image(
        self,
        session: ControlSession,
        item: ClipboardItem,
        compressed: bytes,
    ) -> None:
        assert isinstance(item.payload, ClipboardImage)
        transfer_id = uuid.uuid4().hex
        await session.send(
            ClipboardMessage(
                session.session_id,
                self._wire_sequence,
                {
                    "origin_id": item.origin_id,
                    "generation": item.generation,
                    "sequence": item.sequence,
                    "digest": item.digest,
                    "kind": item.kind.value,
                    "payload": {
                        "transfer_id": transfer_id,
                        "format": item.payload.format,
                        "codec": "zlib",
                        "raw_size": len(item.payload.data),
                        "compressed_size": len(compressed),
                    },
                },
            )
        )
        self._wire_sequence += 1
        offset = 0

        async def send_chunk(data: bytes) -> None:
            nonlocal offset
            final = offset + len(data) == len(compressed)
            await self._send_file_message(
                session,
                FileDataMessage,
                {
                    "channel": "clipboard_image",
                    "transfer_id": transfer_id,
                    "offset": offset,
                    "final": final,
                    "data": base64.b64encode(data).decode("ascii"),
                },
            )
            offset += len(data)

        writer = _ControlMessageWriter(
            send_chunk,
            bytes_per_second=_FILE_RATE_LIMIT_BYTES_PER_SECOND,
        )
        for start in range(0, len(compressed), _CLIPBOARD_IMAGE_CHUNK_SIZE):
            await writer.write(compressed[start : start + _CLIPBOARD_IMAGE_CHUNK_SIZE])

    async def _handle_local_files(self, paths: tuple[Path, ...]) -> None:
        assert self._identity is not None
        try:
            manifest = build_manifest(paths)
        except (OSError, ValueError):
            self._status("无法读取复制的文件")
            return
        self._local_file_manifests.clear()
        self._local_file_manifests[manifest.transfer_id] = manifest
        self._file_offer = _FileOffer(manifest, self._identity.device_id)
        self._completed_file_targets.clear()
        await self._advertise_file_offer(self._file_offer)

    async def _advertise_file_offer(
        self, offer: _FileOffer, *, exclude_device_id: str | None = None
    ) -> None:
        payload: dict[str, JsonValue] = {
            "source_id": offer.source_id,
            "manifest": manifest_to_payload(offer.manifest),
            "cut": False,
        }
        for session in self._file_sessions(exclude_device_id):
            await self._send_file_message(session, FileOfferMessage, payload)

    async def _receive_file_offer(
        self, sender_id: str, message: FileOfferMessage
    ) -> None:
        source_id = message.payload.get("source_id")
        raw_manifest = message.payload.get("manifest")
        if not isinstance(source_id, str):
            return
        if self._config.role is DeviceRole.CONTROLLER and source_id != sender_id:
            return
        try:
            manifest = manifest_from_payload(raw_manifest)
        except (ValueError, TypeError):
            return
        offer = _FileOffer(manifest, source_id)
        self._file_offer = offer
        self._completed_file_targets.clear()
        if self._config.role is DeviceRole.CONTROLLER:
            await self._advertise_file_offer(offer, exclude_device_id=sender_id)

    def _intercept_file_paste(self, target_id: str) -> bool:
        offer = self._file_offer
        loop = self._loop
        if (
            offer is None
            or loop is None
            or target_id == offer.source_id
            or (offer.manifest.transfer_id, target_id) in self._completed_file_targets
        ):
            return False
        transfer_id = offer.manifest.transfer_id
        if transfer_id not in self._file_requests:
            self._file_requests[transfer_id] = (offer.source_id, target_id)
            loop.call_soon_threadsafe(lambda: self._spawn_paste_request(offer, target_id))
        return True

    def _spawn_paste_request(self, offer: _FileOffer, target_id: str) -> None:
        transfer_id = offer.manifest.transfer_id
        task = asyncio.create_task(self._request_file_paste(offer, target_id))
        self._paste_tasks[transfer_id] = task
        task.add_done_callback(lambda _task: self._paste_tasks.pop(transfer_id, None))

    async def _request_file_paste(self, offer: _FileOffer, target_id: str) -> None:
        assert self._identity is not None
        transfer_id = offer.manifest.transfer_id
        operation = _PasteOperation(
            transfer_id, offer.source_id, target_id, offer.manifest
        )
        self._paste_operations[transfer_id] = operation
        self._file_requests[transfer_id] = (offer.source_id, target_id)
        try:
            operation.transition(_PastePhase.PROBING)
            if target_id == self._identity.device_id:
                decision = await self._prepare_target_preflight(
                    PasteProbe(transfer_id, offer.source_id, target_id, offer.manifest)
                )
            else:
                target_session = self._session_for_device(target_id)
                if target_session is None:
                    raise ConnectionError("目标设备未连接")
                waiter: asyncio.Future[PasteDecisionResult] = (
                    asyncio.get_running_loop().create_future()
                )
                self._paste_decision_waiters[transfer_id] = waiter
                operation.transition(_PastePhase.AWAITING_DECISION)
                await self._send_file_message(
                    target_session,
                    FilePasteProbeMessage,
                    PasteProbe(
                        transfer_id, offer.source_id, target_id, offer.manifest
                    ).to_payload(),
                )
                try:
                    decision = await asyncio.wait_for(
                        waiter, timeout=self._overwrite_timeout
                    )
                except TimeoutError:
                    decision = PasteDecisionResult(
                        transfer_id, PasteDecision.CANCEL, (), False
                    )
                finally:
                    self._paste_decision_waiters.pop(transfer_id, None)
            await self._apply_paste_decision(operation, decision)
        except asyncio.CancelledError:
            self._terminate_paste(transfer_id)
            raise
        except (ConnectionError, RuntimeError, OSError, ValueError) as exc:
            self._terminate_paste(transfer_id)
            category = sanitize_transfer_error(str(exc))
            self._status(f"文件传输无法开始：{category}")
            await self._broadcast_file_abort(transfer_id, category)

    async def _prepare_target_preflight(
        self, probe: PasteProbe
    ) -> PasteDecisionResult:
        transfer_id = probe.transfer_id
        busy = self._target_paste_transfer_id
        if busy is not None and busy != transfer_id:
            return PasteDecisionResult(transfer_id, PasteDecision.CANCEL, (), False)
        self._target_paste_transfer_id = transfer_id
        try:
            destination = await self._destination_resolver.resolve_foreground()
        except Exception:
            self._release_target_preflight(transfer_id)
            return PasteDecisionResult(
                transfer_id, PasteDecision.CANCEL, (), False
            )
        if destination.kind is DestinationKind.COMPATIBILITY:
            decision = PasteDecisionResult(
                transfer_id, PasteDecision.OVERWRITE, (), True
            )
            self._target_preflights[transfer_id] = _TargetPreflight(
                probe.source_id,
                probe.target_id,
                probe.manifest,
                destination,
                decision,
            )
            return decision
        assert destination.path is not None
        try:
            result = self._preflight.scan(destination.path, probe.manifest)
        except (OSError, RuntimeError, ValueError):
            self._release_target_preflight(transfer_id)
            return PasteDecisionResult(
                transfer_id, PasteDecision.CANCEL, (), False
            )
        if result.conflict_count == 0:
            choice = PasteDecision.OVERWRITE
        else:
            request = OverwritePromptRequest(
                transfer_id,
                result.conflict_count,
                tuple(
                    conflict.relative_path.as_posix()
                    for conflict in result.conflict_preview
                ),
            )
            try:
                choice = await asyncio.wait_for(
                    self._overwrite_decider(request), timeout=self._overwrite_timeout
                )
            except TimeoutError:
                choice = PasteDecision.CANCEL
        skipped = (
            tuple(conflict.relative_path for conflict in result.conflicts)
            if choice is PasteDecision.SKIP
            else ()
        )
        decision = PasteDecisionResult(transfer_id, choice, skipped, False)
        if choice is PasteDecision.CANCEL:
            self._release_target_preflight(transfer_id)
        else:
            self._resolved_destinations[transfer_id] = destination
            self._target_preflights[transfer_id] = _TargetPreflight(
                probe.source_id,
                probe.target_id,
                probe.manifest,
                destination,
                decision,
            )
        return decision

    async def _receive_file_paste_probe(
        self, _sender_id: str, message: FilePasteProbeMessage
    ) -> None:
        assert self._identity is not None
        try:
            probe = PasteProbe.from_payload(message.payload)
        except (TypeError, ValueError):
            return
        if probe.target_id != self._identity.device_id:
            return
        decision = await self._prepare_target_preflight(probe)
        session = self._client_session
        if session is not None:
            await self._send_file_message(
                session, FilePasteDecisionMessage, decision.to_payload()
            )

    def _spawn_file_paste_probe(
        self, sender_id: str, message: FilePasteProbeMessage
    ) -> None:
        transfer_id = message.payload.get("transfer_id")
        if not isinstance(transfer_id, str) or transfer_id in self._paste_probe_tasks:
            return
        task = asyncio.create_task(
            self._run_file_paste_probe(sender_id, message, transfer_id)
        )
        self._paste_probe_tasks[transfer_id] = task
        task.add_done_callback(
            lambda completed: self._discard_paste_probe_task(
                transfer_id, completed
            )
        )

    async def _run_file_paste_probe(
        self,
        sender_id: str,
        message: FilePasteProbeMessage,
        transfer_id: str,
    ) -> None:
        try:
            await self._receive_file_paste_probe(sender_id, message)
        except asyncio.CancelledError:
            self._release_target_preflight(transfer_id)
            raise
        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
            self._release_target_preflight(transfer_id)
            self._status(
                f"文件传输无法开始：{sanitize_transfer_error(str(exc))}"
            )

    def _discard_paste_probe_task(
        self, transfer_id: str, completed: asyncio.Task[None]
    ) -> None:
        if self._paste_probe_tasks.get(transfer_id) is completed:
            self._paste_probe_tasks.pop(transfer_id, None)

    async def _cancel_paste_probe_tasks(self) -> None:
        tasks = tuple(self._paste_probe_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._paste_probe_tasks.clear()

    async def _receive_file_paste_decision(
        self, sender_id: str, message: FilePasteDecisionMessage
    ) -> None:
        try:
            decision = PasteDecisionResult.from_payload(message.payload)
        except (TypeError, ValueError):
            return
        operation = self._paste_operations.get(decision.transfer_id)
        waiter = self._paste_decision_waiters.get(decision.transfer_id)
        if (
            operation is None
            or waiter is None
            or sender_id != operation.target_id
            or waiter.done()
        ):
            return
        waiter.set_result(decision)

    async def _apply_paste_decision(
        self, operation: _PasteOperation, decision: PasteDecisionResult
    ) -> None:
        if decision.transfer_id != operation.transfer_id:
            raise ValueError("粘贴决定与传输 ID 不一致")
        if decision.decision is PasteDecision.CANCEL:
            self._terminate_paste(operation.transfer_id)
            await self._broadcast_file_abort(
                operation.transfer_id, "文件粘贴已取消"
            )
            return
        operation.transition(_PastePhase.APPROVED)
        filtered = filter_manifest(operation.manifest, decision.skipped_paths)
        if not filtered.entries:
            self._completed_file_targets.add(
                (operation.transfer_id, operation.target_id)
            )
            self._terminate_paste(operation.transfer_id)
            await self._broadcast_file_abort(
                operation.transfer_id, "没有需要传输的文件"
            )
            return
        payload: dict[str, JsonValue] = {
            "action": "start",
            "transfer_id": operation.transfer_id,
            "source_id": operation.source_id,
            "target_id": operation.target_id,
            "manifest": manifest_to_payload(filtered),
            "skipped_paths": [path.as_posix() for path in decision.skipped_paths],
            "compatibility": decision.compatibility,
        }
        assert self._identity is not None
        local_source_manifest: FileManifest | None = None
        if operation.source_id == self._identity.device_id:
            original = self._local_file_manifests.get(operation.transfer_id)
            if original is None:
                raise RuntimeError("源文件清单已失效")
            local_source_manifest = filter_manifest(
                original, decision.skipped_paths
            )
            if local_source_manifest.entries != filtered.entries:
                raise RuntimeError("过滤后的文件清单不一致")
        if operation.target_id == self._identity.device_id:
            await self._start_file_receiver(
                filtered, operation.source_id, operation.target_id
            )
        else:
            target_session = self._session_for_device(operation.target_id)
            if target_session is None:
                raise ConnectionError("目标设备未连接")
            await self._send_file_message(
                target_session, FileRequestMessage, payload
            )
        if operation.source_id == self._identity.device_id:
            assert local_source_manifest is not None
            await self._start_file_sender(
                local_source_manifest,
                operation.source_id,
                operation.target_id,
            )
        else:
            source_session = self._session_for_device(operation.source_id)
            if source_session is None:
                raise ConnectionError("源设备未连接")
            await self._send_file_message(
                source_session, FileRequestMessage, payload
            )
        operation.transition(_PastePhase.TRANSFERRING)

    def _release_target_preflight(self, transfer_id: str) -> None:
        self._target_preflights.pop(transfer_id, None)
        self._resolved_destinations.pop(transfer_id, None)
        if self._target_paste_transfer_id == transfer_id:
            self._target_paste_transfer_id = None

    def _terminate_paste(self, transfer_id: str) -> None:
        operation = self._paste_operations.pop(transfer_id, None)
        if operation is not None and operation.phase is not _PastePhase.TERMINAL:
            operation.transition(_PastePhase.TERMINAL)
        waiter = self._paste_decision_waiters.pop(transfer_id, None)
        if waiter is not None and not waiter.done():
            waiter.cancel()
        self._file_requests.pop(transfer_id, None)
        self._release_target_preflight(transfer_id)

    async def _receive_file_request(
        self, sender_id: str, message: FileRequestMessage
    ) -> None:
        action = message.payload.get("action")
        transfer_id = message.payload.get("transfer_id")
        source_id = message.payload.get("source_id")
        target_id = message.payload.get("target_id")
        if not all(isinstance(value, str) for value in (transfer_id, source_id, target_id)):
            return
        assert isinstance(transfer_id, str)
        assert isinstance(source_id, str)
        assert isinstance(target_id, str)
        if action == "complete" and self._config.role is DeviceRole.CONTROLLER:
            if sender_id == target_id:
                self._completed_file_targets.add((transfer_id, target_id))
                self._terminate_paste(transfer_id)
            return
        if action != "start" or self._config.role is DeviceRole.CONTROLLER:
            return
        assert self._identity is not None
        try:
            manifest = manifest_from_payload(message.payload.get("manifest"))
        except (ValueError, TypeError):
            return
        if manifest.transfer_id != transfer_id:
            return
        raw_skipped = message.payload.get("skipped_paths", [])
        compatibility = message.payload.get("compatibility", True)
        if (
            not isinstance(raw_skipped, list)
            or not all(isinstance(path, str) and path for path in raw_skipped)
            or not isinstance(compatibility, bool)
        ):
            return
        skipped_paths = tuple(
            PurePosixPath(path) for path in cast(list[str], raw_skipped)
        )
        try:
            if self._identity.device_id == target_id:
                target_preflight = self._target_preflights.get(transfer_id)
                if (
                    target_preflight is None
                    or target_preflight.source_id != source_id
                    or target_preflight.target_id != target_id
                    or target_preflight.result.compatibility != compatibility
                    or target_preflight.result.skipped_paths != skipped_paths
                ):
                    await self._broadcast_file_abort(
                        transfer_id, "目标预检状态已失效"
                    )
                    return
                expected_manifest = filter_manifest(
                    target_preflight.manifest, skipped_paths
                )
                if expected_manifest.entries != manifest.entries:
                    await self._broadcast_file_abort(
                        transfer_id, "目标预检清单与启动清单不一致"
                    )
                    return
                await self._start_file_receiver(manifest, source_id, target_id)
            if self._identity.device_id == source_id:
                local_manifest = self._local_file_manifests.get(transfer_id)
                if local_manifest is None:
                    await self._broadcast_file_abort(transfer_id, "源文件清单已失效")
                    return
                filtered = filter_manifest(local_manifest, skipped_paths)
                if filtered.entries != manifest.entries:
                    await self._broadcast_file_abort(transfer_id, "过滤后的文件清单不一致")
                    return
                await self._start_file_sender(filtered, source_id, target_id)
        except (OSError, RuntimeError, ValueError) as exc:
            await self._broadcast_file_abort(transfer_id, str(exc))

    async def _start_file_sender(
        self, manifest: FileManifest, source_id: str, target_id: str
    ) -> None:
        transfer_id = manifest.transfer_id
        if transfer_id in self._file_send_tasks:
            return

        async def run() -> None:
            sender = FileSender(
                session_id=transfer_id,
                source_device_id=source_id,
                chunk_size=32 * 1024,
            )
            self._file_senders[transfer_id] = sender
            writer = _ControlMessageWriter(
                lambda data: self._emit_file_data(
                    transfer_id, source_id, target_id, data
                ),
                bytes_per_second=(
                    None
                    if self._identity is not None
                    and target_id == self._identity.device_id
                    else _FILE_RATE_LIMIT_BYTES_PER_SECOND
                ),
            )
            result = await sender.send(manifest, writer)
            if not result.success:
                await self._broadcast_file_abort(
                    transfer_id, result.error or "文件发送失败"
                )

        task = asyncio.create_task(run())
        self._file_send_tasks[transfer_id] = task
        task.add_done_callback(
            lambda _task: (
                self._file_send_tasks.pop(transfer_id, None),
                self._file_senders.pop(transfer_id, None),
            )
        )

    async def _start_file_receiver(
        self, manifest: FileManifest, source_id: str, target_id: str
    ) -> None:
        transfer_id = manifest.transfer_id
        if transfer_id in self._file_receive_tasks:
            return
        self._begin_transfer(manifest)
        target_preflight = self._target_preflights.get(transfer_id)
        direct = (
            target_preflight is not None
            and not target_preflight.result.compatibility
            and target_preflight.destination.kind is DestinationKind.DIRECT
        )
        if direct:
            assert target_preflight is not None
            destination = target_preflight.destination.path
            assert destination is not None
            direct_workspace = (
                DestinationTransferWorkspace.reserve(
                    destination, manifest, target_preflight.result
                )
            )
            workspace: TransferWorkspace | DestinationWorkspace = direct_workspace
            self._destination_workspaces[transfer_id] = direct_workspace
        else:
            free_bytes = shutil.disk_usage(self._paths.staging_dir).free
            staging_workspace = self._staging.reserve(manifest, free_bytes)
            workspace = staging_workspace
            self._staging_workspaces[transfer_id] = staging_workspace
        stream = QueuedTransferStream()
        self._file_streams[transfer_id] = stream

        async def run() -> None:
            receiver = FileReceiver(
                None if direct else self._staging,
                expected_session_id=transfer_id,
            )
            self._file_receivers[transfer_id] = receiver
            result = await receiver.receive(
                manifest,
                stream,
                workspace,
                progress=self._report_transfer_progress,
            )
            self._file_streams.pop(transfer_id, None)
            self._finish_transfer(
                transfer_id,
                success=result.success,
                bytes_transferred=result.bytes_transferred,
                error=sanitize_transfer_error(result.error)
                if not result.success
                else None,
            )
            if not result.success:
                await self._broadcast_file_abort(
                    transfer_id, result.error or "文件接收失败"
                )
                return
            try:
                if direct:
                    self._completed_file_targets.add((transfer_id, target_id))
                    await self._notify_file_complete(
                        transfer_id, source_id, target_id
                    )
                    self._status(f"文件传输完成：{result.files_completed} 个文件")
                    return
                if self._clipboard is None:
                    raise RuntimeError("系统剪贴板已停止")
                assert isinstance(workspace, TransferWorkspace)
                marker = f"{_FILE_MARKER_PREFIX}{transfer_id}"
                self._clipboard.write_file_drop(result.paths, marker)
                workspace.clipboard_referenced = True
                self._referenced_transfer_id = transfer_id
                self._completed_file_targets.add((transfer_id, target_id))
                await self._notify_file_complete(transfer_id, source_id, target_id)
                self._replay_paste()
                self._status(f"文件传输完成：{result.files_completed} 个文件")
            except (ClipboardBusyError, OSError, RuntimeError, ValueError) as exc:
                await self._broadcast_file_abort(transfer_id, str(exc))

        task = asyncio.create_task(run())
        self._file_receive_tasks[transfer_id] = task
        task.add_done_callback(
            lambda _task: (
                self._file_receive_tasks.pop(transfer_id, None),
                self._file_receivers.pop(transfer_id, None),
                self._destination_workspaces.pop(transfer_id, None),
            )
        )

    def _begin_transfer(self, manifest: FileManifest) -> None:
        transfer_id = manifest.transfer_id
        if transfer_id in self._transfer_progress:
            return
        first_file = next(
            (
                entry.relative_path.as_posix()
                for entry in manifest.entries
                if entry.kind is EntryKind.FILE
            ),
            "",
        )
        loop = asyncio.get_running_loop()
        progress = _TransferProgressState(
            transfer_id,
            first_file,
            manifest.total_size,
            loop.time(),
        )
        self._transfer_progress[transfer_id] = progress
        progress.timer = asyncio.create_task(self._show_transfer_after_delay(transfer_id))

    async def _show_transfer_after_delay(self, transfer_id: str) -> None:
        try:
            await asyncio.sleep(self._transfer_progress_delay)
        except asyncio.CancelledError:
            return
        progress = self._transfer_progress.get(transfer_id)
        if progress is None:
            return
        progress.shown = True
        progress.last_emit_at = asyncio.get_running_loop().time()
        self._emit_transfer_status(progress, TransferState.ACTIVE)

    def _report_transfer_progress(self, update: TransferProgress) -> None:
        progress = self._transfer_progress.get(update.transfer_id)
        if progress is None:
            return
        progress.current_file = update.relative_path
        progress.bytes_transferred = update.bytes_transferred
        if not progress.shown:
            return
        now = asyncio.get_running_loop().time()
        if now - progress.last_emit_at < self._transfer_progress_interval:
            return
        progress.last_emit_at = now
        self._emit_transfer_status(progress, TransferState.ACTIVE)

    def _finish_transfer(
        self,
        transfer_id: str,
        *,
        success: bool,
        bytes_transferred: int | None = None,
        error: str | None = None,
        cancelled: bool = False,
    ) -> None:
        progress = self._transfer_progress.pop(transfer_id, None)
        if progress is None:
            return
        if bytes_transferred is not None:
            progress.bytes_transferred = bytes_transferred
        timer = progress.timer
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        elapsed = asyncio.get_running_loop().time() - progress.started_at
        if not progress.shown and elapsed < self._transfer_progress_delay:
            return
        progress.shown = True
        state = (
            TransferState.CANCELLED
            if cancelled
            else TransferState.COMPLETE
            if success
            else TransferState.FAILED
        )
        if success:
            progress.bytes_transferred = progress.total_bytes
        self._emit_transfer_status(progress, state, error=error)

    def _emit_transfer_status(
        self,
        progress: _TransferProgressState,
        state: TransferState,
        *,
        error: str | None = None,
    ) -> None:
        elapsed = max(
            asyncio.get_running_loop().time() - progress.started_at,
            0.001,
        )
        self._transfer_status(
            FileTransferStatus(
                progress.transfer_id,
                progress.current_file,
                progress.bytes_transferred,
                progress.total_bytes,
                progress.bytes_transferred / elapsed,
                state,
                error,
            )
        )

    async def _emit_file_data(
        self, transfer_id: str, source_id: str, target_id: str, data: bytes
    ) -> None:
        assert self._identity is not None
        if target_id == self._identity.device_id:
            stream = self._file_streams.get(transfer_id)
            if stream is None:
                raise ConnectionError("目标文件接收器不存在")
            stream.feed(data)
            return
        session = self._session_for_device(target_id)
        if session is None and self._config.role is DeviceRole.CLIENT:
            session = self._client_session
        if session is None:
            raise ConnectionError("目标设备连接已断开")
        await self._send_file_message(
            session,
            FileDataMessage,
            {
                "transfer_id": transfer_id,
                "source_id": source_id,
                "target_id": target_id,
                "data": base64.b64encode(data).decode("ascii"),
            },
        )

    async def _receive_file_data(
        self, sender_id: str, message: FileDataMessage
    ) -> None:
        if message.payload.get("channel") == "clipboard_image":
            await self._receive_clipboard_image_data(sender_id, message)
            return
        transfer_id = message.payload.get("transfer_id")
        source_id = message.payload.get("source_id")
        target_id = message.payload.get("target_id")
        encoded = message.payload.get("data")
        if not all(
            isinstance(value, str)
            for value in (transfer_id, source_id, target_id, encoded)
        ):
            return
        assert isinstance(transfer_id, str)
        assert isinstance(source_id, str)
        assert isinstance(target_id, str)
        assert isinstance(encoded, str)
        if self._config.role is DeviceRole.CONTROLLER and sender_id != source_id:
            return
        if (
            self._config.role is DeviceRole.CONTROLLER
            and self._file_requests.get(transfer_id) != (source_id, target_id)
        ):
            return
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            await self._broadcast_file_abort(transfer_id, "文件分块编码无效")
            return
        assert self._identity is not None
        if target_id == self._identity.device_id:
            stream = self._file_streams.get(transfer_id)
            if stream is not None:
                stream.feed(data)
            return
        if self._config.role is DeviceRole.CONTROLLER:
            session = self._session_for_device(target_id)
            if session is not None:
                await self._send_file_message(session, FileDataMessage, message.payload)

    async def _receive_clipboard_image_data(
        self, sender_id: str, message: FileDataMessage
    ) -> None:
        transfer_id = message.payload.get("transfer_id")
        offset = message.payload.get("offset")
        final = message.payload.get("final")
        encoded = message.payload.get("data")
        if (
            not isinstance(transfer_id, str)
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or not isinstance(final, bool)
            or not isinstance(encoded, str)
        ):
            return
        state = self._image_receivers.get(transfer_id)
        if state is None or state.sender_id != sender_id or offset != len(state.compressed):
            return
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            self._image_receivers.pop(transfer_id, None)
            return
        if len(state.compressed) + len(data) > state.compressed_size:
            self._image_receivers.pop(transfer_id, None)
            return
        state.compressed.extend(data)
        if not final:
            return
        self._image_receivers.pop(transfer_id, None)
        if len(state.compressed) != state.compressed_size:
            return
        try:
            raw = await asyncio.to_thread(
                _decompress_clipboard_image,
                bytes(state.compressed),
                state.raw_size,
            )
            item = ClipboardItem.image(
                state.origin_id,
                state.sequence,
                ClipboardImage(state.image_format, raw),
                generation=state.generation,
            )
        except (ValueError, zlib.error):
            return
        if item.digest != state.digest:
            return
        coordinator = self._clipboard_coordinator
        if coordinator is not None:
            await self._apply_clipboard_actions(coordinator.remote_received(item))

    async def _broadcast_file_abort(self, transfer_id: str, reason: str) -> None:
        reason = sanitize_transfer_error(reason)
        stream = self._file_streams.get(transfer_id)
        if stream is not None:
            stream.abort(reason)
        payload: dict[str, JsonValue] = {
            "transfer_id": transfer_id,
            "reason": reason,
        }
        for session in self._file_sessions():
            await self._send_file_message(session, FileAbortMessage, payload)

    async def _receive_file_abort(
        self, _sender_id: str, message: FileAbortMessage
    ) -> None:
        transfer_id = message.payload.get("transfer_id")
        reason = message.payload.get("reason")
        if not isinstance(transfer_id, str) or not isinstance(reason, str):
            return
        reason = sanitize_transfer_error(reason)
        stream = self._file_streams.get(transfer_id)
        if stream is not None:
            stream.abort(reason)
        sender = self._file_senders.get(transfer_id)
        receiver = self._file_receivers.get(transfer_id)
        if sender is not None:
            sender.cancel()
        if receiver is not None:
            receiver.cancel()
        self._finish_transfer(
            transfer_id,
            success=False,
            error=reason,
            cancelled=reason == "用户取消",
        )
        self._terminate_paste(transfer_id)
        self._status(f"文件传输失败：{reason}")
        if self._config.role is DeviceRole.CONTROLLER:
            safe_payload: dict[str, JsonValue] = {
                "transfer_id": transfer_id,
                "reason": reason,
            }
            for session in self._file_sessions(exclude_device_id=_sender_id):
                await self._send_file_message(
                    session, FileAbortMessage, safe_payload
                )

    async def _notify_file_complete(
        self, transfer_id: str, source_id: str, target_id: str
    ) -> None:
        self._terminate_paste(transfer_id)
        if self._config.role is DeviceRole.CLIENT and self._client_session is not None:
            await self._send_file_message(
                self._client_session,
                FileRequestMessage,
                {
                    "action": "complete",
                    "transfer_id": transfer_id,
                    "source_id": source_id,
                    "target_id": target_id,
                },
            )

    def _replay_paste(self) -> None:
        injector = Win32InputInjector()
        injector.inject(KeyEvent(29, 0x11, True))
        injector.inject(KeyEvent(47, 0x56, True))
        injector.inject(KeyEvent(47, 0x56, False))
        injector.inject(KeyEvent(29, 0x11, False))

    def _file_sessions(
        self, exclude_device_id: str | None = None
    ) -> tuple[ControlSession, ...]:
        sessions = (
            tuple(self._controller_sessions.values())
            if self._config.role is DeviceRole.CONTROLLER
            else (() if self._client_session is None else (self._client_session,))
        )
        return tuple(
            session
            for session in sessions
            if session.state is SessionState.ACTIVE
            and session.peer is not None
            and session.peer.device_id != exclude_device_id
        )

    def _session_for_device(self, device_id: str) -> ControlSession | None:
        for session in self._file_sessions():
            if session.peer is not None and session.peer.device_id == device_id:
                return session
        return None

    async def _send_file_message(
        self,
        session: ControlSession,
        message_class: type[WireMessage],
        payload: dict[str, JsonValue],
    ) -> None:
        await session.send(
            message_class(session.session_id, self._wire_sequence, payload)
        )
        self._wire_sequence += 1


async def _reject_pairing(_request: PairingRequest) -> bool:
    return False


async def _cancel_overwrite(_request: OverwritePromptRequest) -> PasteDecision:
    return PasteDecision.CANCEL


def _primary_screen() -> ScreenInfo:
    user32 = ctypes.windll.user32
    return ScreenInfo(int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))


def _selected_bind_ip(config: AppConfig) -> str | None:
    if config.bind_ip:
        return config.bind_ip
    return selected_ipv4(list_adapters(), config.adapter_id)


def _item_from_marker(marker: str, text: str) -> ClipboardItem | None:
    parts = marker.split(":")
    if len(parts) == 3:
        origin_id, raw_sequence, digest = parts
        generation = ""
    elif len(parts) == 4:
        origin_id, generation, raw_sequence, digest = parts
    else:
        return None
    try:
        item = ClipboardItem.text(
            origin_id,
            int(raw_sequence),
            text,
            generation=generation,
        )
    except ValueError:
        return None
    return item if item.digest == digest else None


def _image_item_from_marker(
    marker: str, image: ClipboardImage
) -> ClipboardItem | None:
    parts = marker.split(":")
    if len(parts) == 3:
        origin_id, raw_sequence, digest = parts
        generation = ""
    elif len(parts) == 4:
        origin_id, generation, raw_sequence, digest = parts
    else:
        return None
    try:
        item = ClipboardItem.image(
            origin_id,
            int(raw_sequence),
            ClipboardImage(image.format, image.data),
            generation=generation,
        )
    except ValueError:
        return None
    return item if item.digest == digest else None


def _image_receive_state(
    sender_id: str, message: ClipboardMessage
) -> _ImageReceiveState | None:
    payload = message.payload
    metadata = payload.get("payload")
    origin_id = payload.get("origin_id")
    generation = payload.get("generation", "")
    sequence = payload.get("sequence")
    digest = payload.get("digest")
    if (
        not isinstance(metadata, dict)
        or not isinstance(origin_id, str)
        or not origin_id
        or not isinstance(generation, str)
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 0
        or not isinstance(digest, str)
        or len(digest) != 64
    ):
        return None
    transfer_id = metadata.get("transfer_id")
    image_format = metadata.get("format")
    codec = metadata.get("codec")
    raw_size = metadata.get("raw_size")
    compressed_size = metadata.get("compressed_size")
    if (
        not isinstance(transfer_id, str)
        or not transfer_id
        or image_format not in {"dib", "dibv5"}
        or codec != "zlib"
        or not isinstance(raw_size, int)
        or isinstance(raw_size, bool)
        or not 0 < raw_size <= _MAX_CLIPBOARD_IMAGE_BYTES
        or not isinstance(compressed_size, int)
        or isinstance(compressed_size, bool)
        or not 0 < compressed_size <= _MAX_CLIPBOARD_IMAGE_BYTES
    ):
        return None
    assert isinstance(image_format, str)
    return _ImageReceiveState(
        transfer_id,
        sender_id,
        origin_id,
        generation,
        sequence,
        digest,
        image_format,
        raw_size,
        compressed_size,
        bytearray(),
    )


def _decompress_clipboard_image(compressed: bytes, expected_size: int) -> bytes:
    decompressor = zlib.decompressobj()
    raw = decompressor.decompress(compressed, expected_size + 1)
    if (
        len(raw) != expected_size
        or decompressor.unconsumed_tail
        or not decompressor.eof
        or decompressor.unused_data
    ):
        raise ValueError("截图压缩数据大小不匹配")
    return raw


def _clipboard_item_from_message(message: ClipboardMessage) -> ClipboardItem | None:
    payload = message.payload
    origin_id = payload.get("origin_id")
    generation = payload.get("generation", "")
    sequence = payload.get("sequence")
    digest = payload.get("digest")
    kind = payload.get("kind")
    text = payload.get("payload")
    if (
        not isinstance(origin_id, str)
        or not isinstance(generation, str)
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or not isinstance(digest, str)
        or kind != ClipboardKind.TEXT.value
        or not isinstance(text, str)
    ):
        return None
    try:
        item = ClipboardItem.text(
            origin_id,
            sequence,
            text,
            generation=generation,
        )
    except ValueError:
        return None
    return item if item.digest == digest else None


__all__ = [
    "NetworkRoleService",
    "OverwritePromptRequest",
    "RoleNetworkPlan",
    "build_role_network_plan",
    "reachable_connected_positions",
]
