from __future__ import annotations

import argparse
import asyncio
import ctypes
import faulthandler
import inspect
import json
import socket
import sys
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import CancelledError, Future
from contextlib import suppress
from dataclasses import asdict, replace
from pathlib import Path
from typing import Protocol

from lan_input_share import __version__
from lan_input_share.config.models import AppConfig, ConfigError
from lan_input_share.config.paths import AppPaths
from lan_input_share.config.store import ConfigStore
from lan_input_share.core.models import ScreenPosition
from lan_input_share.logging_setup import configure_logging
from lan_input_share.network.adapters import classify_adapter, list_adapters
from lan_input_share.runtime.service import NetworkRoleService, OverwritePromptRequest
from lan_input_share.security.pairing import PairingRequest
from lan_input_share.transfer.preflight import PasteDecision
from lan_input_share.transfer.status import FileTransferStatus
from lan_input_share.ui.close_behavior import CloseChoice, ClosePreference
from lan_input_share.windows.input_helper_install import (
    INPUT_HELPER_EXE_NAME,
    InputHelperManager,
)
from lan_input_share.windows.privilege import is_elevated

_ERROR_ALREADY_EXISTS = 183
_WINDOW_TITLE = "局域网键鼠共享"


class AsyncResource(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class SafetyRecovery(Protocol):
    async def restore_local_input(self) -> None: ...


class InstanceGuard(Protocol):
    def acquire(self) -> bool: ...

    def focus_existing(self) -> None: ...

    def release(self) -> None: ...


class ApplicationCoordinator:
    """Starts resources in order and always restores input before teardown."""

    def __init__(
        self,
        resources: Sequence[AsyncResource],
        safety: SafetyRecovery,
        instance_guard: InstanceGuard,
    ) -> None:
        self._resources = tuple(resources)
        self._safety = safety
        self._guard = instance_guard
        self._started: list[AsyncResource] = []
        self._acquired = False

    async def start(self) -> bool:
        if not self._guard.acquire():
            self._guard.focus_existing()
            return False
        self._acquired = True
        try:
            for resource in self._resources:
                await resource.start()
                self._started.append(resource)
            return True
        except BaseException:
            await self._recover_and_stop()
            raise

    async def stop(self) -> None:
        await self._recover_and_stop()

    async def _recover_and_stop(self) -> None:
        await self._safety.restore_local_input()
        for resource in reversed(self._started):
            with suppress(Exception):
                await resource.stop()
        self._started.clear()
        if self._acquired:
            self._guard.release()
            self._acquired = False


class SingleInstanceGuard:
    def __init__(
        self,
        name: str = "Local\\LanInputShare",
        *,
        window_activator: Callable[[str], None] | None = None,
    ) -> None:
        self._name = name
        self._handle: int | None = None
        self._window_activator = window_activator

    def acquire(self) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, self._name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = int(handle)
        return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS

    def focus_existing(self) -> None:
        if self._window_activator is not None:
            self._window_activator(_WINDOW_TITLE)
            return
        try:
            import win32con  # type: ignore[import-untyped]
            import win32gui  # type: ignore[import-untyped]
        except ImportError:
            return

        def visit(handle: int, _extra: object) -> None:
            if win32gui.GetWindowText(handle) == _WINDOW_TITLE:
                win32gui.ShowWindow(handle, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(handle)

        win32gui.EnumWindows(visit, None)

    def release(self) -> None:
        if self._handle is not None:
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None


class _NoopSafety:
    async def restore_local_input(self) -> None:
        return


class _ConfiguredRuntime:
    def __init__(
        self,
        store: ConfigStore,
        paths: AppPaths,
        *,
        status: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._paths = paths
        self._paused = False
        self._status = status or (lambda _message: None)
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._service_lock = asyncio.Lock()
        self._thread = threading.Thread(
            target=self._run_loop, name="lan-input-share-runtime", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=3):
            raise TimeoutError("启动后台运行时超时")
        self._service: NetworkRoleService | None = None
        self._pairing_callback: Callable[[PairingRequest], Future[bool]] | None = None
        self._slot_status_callback: (
            Callable[[ScreenPosition, str, str], None] | None
        ) = None
        self._transfer_status_callback: (
            Callable[[FileTransferStatus], None] | None
        ) = None
        self._overwrite_callback: (
            Callable[[OverwritePromptRequest], Future[PasteDecision]] | None
        ) = None

    def set_status_callback(self, callback: Callable[[str], None]) -> None:
        self._status = callback

    def set_pairing_callback(
        self, callback: Callable[[PairingRequest], Future[bool]]
    ) -> None:
        self._pairing_callback = callback

    def set_slot_status_callback(
        self, callback: Callable[[ScreenPosition, str, str], None]
    ) -> None:
        self._slot_status_callback = callback

    def set_transfer_status_callback(
        self, callback: Callable[[FileTransferStatus], None]
    ) -> None:
        self._transfer_status_callback = callback

    def set_overwrite_callback(
        self,
        callback: Callable[[OverwritePromptRequest], Future[PasteDecision]],
    ) -> None:
        self._overwrite_callback = callback

    def configure(self, config: AppConfig) -> None:
        self._store.save(config)
        self._submit(self._restart(config))

    def start(self, config: AppConfig) -> None:
        self._submit(self._restart(config))

    def pause(self) -> None:
        self._paused = True
        if self._service is not None:
            self._submit(self._service.pause())

    def resume(self) -> None:
        self._paused = False
        if self._service is not None:
            self._submit(self._service.resume())

    def return_local(self) -> None:
        if self._service is not None:
            self._submit(self._service.return_local())

    def cancel_transfer(self, transfer_id: str) -> None:
        if self._service is not None:
            self._submit(self._service.cancel_transfer(transfer_id))

    def remember_close_choice(self, choice: CloseChoice | None) -> None:
        try:
            config = self._store.load()
        except ConfigError:
            return
        self._store.save(
            replace(config, close_preference=ClosePreference(choice))
        )

    def shutdown(self) -> None:
        future = asyncio.run_coroutine_threadsafe(self._stop_service(), self._loop)
        with suppress(Exception):
            future.result(timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def _submit(self, coroutine: object) -> None:
        if not inspect.isawaitable(coroutine):
            raise TypeError("后台任务必须可等待")
        future = asyncio.run_coroutine_threadsafe(_await_object(coroutine), self._loop)
        future.add_done_callback(self._task_done)

    def _task_done(self, future: Future[None]) -> None:
        try:
            future.result()
        except CancelledError:
            return
        except Exception as exc:
            self._status(f"后台运行失败：{exc}")

    async def _restart(self, config: AppConfig) -> None:
        async with self._service_lock:
            await self._stop_service_unlocked()
            service = NetworkRoleService(
                config,
                self._paths,
                status=self._status,
                slot_status=self._slot_status,
                pairing_decider=self._decide_pairing,
                transfer_status=self._transfer_status,
                overwrite_decider=self._decide_overwrite,
            )
            try:
                await service.start()
            except BaseException:
                await service.stop()
                raise
            self._service = service

    async def _decide_pairing(self, request: PairingRequest) -> bool:
        callback = self._pairing_callback
        if callback is None:
            return False
        return await asyncio.wrap_future(callback(request))

    async def _decide_overwrite(
        self, request: OverwritePromptRequest
    ) -> PasteDecision:
        callback = self._overwrite_callback
        if callback is None:
            return PasteDecision.CANCEL
        return await asyncio.wrap_future(callback(request))

    def _slot_status(
        self, position: ScreenPosition, device_name: str, status: str
    ) -> None:
        callback = self._slot_status_callback
        if callback is not None:
            callback(position, device_name, status)

    def _transfer_status(self, status: FileTransferStatus) -> None:
        callback = self._transfer_status_callback
        if callback is not None:
            callback(status)

    async def _stop_service(self) -> None:
        async with self._service_lock:
            await self._stop_service_unlocked()

    async def _stop_service_unlocked(self) -> None:
        service = self._service
        self._service = None
        if service is not None:
            await service.stop()


def diagnose() -> dict[str, object]:
    paths = AppPaths.from_environment()
    adapters = list_adapters()
    return {
        "ok": True,
        "mode": "read_only",
        "version": __version__,
        "python": sys.version.split()[0],
        "hostname": socket.gethostname(),
        "paths": {
            "root": str(paths.root),
            "config": str(paths.config_file),
            "logs": str(paths.log_dir),
        },
        "adapters": [
            asdict(adapter) | {"classification": classify_adapter(adapter).value}
            for adapter in adapters
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="LanInputShare")
    parser.add_argument("--diagnose", action="store_true", help="输出只读诊断并退出")
    parser.add_argument("--minimized", action="store_true", help="仅显示托盘图标")
    parser.add_argument("--gui-smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--install-input-helper", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--remove-input-helper", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--input-helper-status", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    maintenance = sum(
        bool(value)
        for value in (
            arguments.install_input_helper,
            arguments.remove_input_helper,
            arguments.input_helper_status,
        )
    )
    if maintenance > 1:
        parser.error("input-helper maintenance options are mutually exclusive")
    if maintenance:
        return _run_input_helper_maintenance(arguments)
    if arguments.diagnose:
        print(json.dumps(diagnose(), ensure_ascii=False, indent=2))
        return 0
    if arguments.gui_smoke_test:
        return _run_gui_smoke_test()
    return _run_gui(minimized=bool(arguments.minimized))


def _current_executable() -> Path:
    executable = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
    return Path(executable).resolve()


def _run_input_helper_maintenance(arguments: argparse.Namespace) -> int:
    main_exe = _current_executable()
    helper_source = main_exe.with_name(INPUT_HELPER_EXE_NAME)
    manager = InputHelperManager()
    if arguments.input_helper_status:
        status = manager.status(main_exe, helper_source)
        print(
            json.dumps(
                {
                    "state": status.state.value,
                    "installed_path": (
                        str(status.installed_path) if status.installed_path else None
                    ),
                    "detail": status.detail,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if not is_elevated():
        print("安装或移除高权限输入助手需要管理员权限", file=sys.stderr)
        return 2
    if arguments.install_input_helper:
        manager.install(main_exe, helper_source)
        manager.start()
    else:
        manager.remove()
    return 0


def _run_gui_smoke_test() -> int:
    from PySide6.QtWidgets import QApplication

    from lan_input_share.ui.resources import application_icon

    existing_application = QApplication.instance()
    application = (
        existing_application
        if isinstance(existing_application, QApplication)
        else QApplication([sys.argv[0]])
    )
    application.setWindowIcon(application_icon())
    return 0


def _run_gui(*, minimized: bool) -> int:
    from PySide6.QtWidgets import QApplication

    from lan_input_share.ui.main_window import MainWindow
    from lan_input_share.ui.overwrite_dialog import OverwriteDialog
    from lan_input_share.ui.resources import application_icon
    from lan_input_share.ui.transfer_popup import FileTransferPopup
    from lan_input_share.ui.tray import TrayController
    from lan_input_share.ui.view_model import AppViewModel, OverwritePromptView

    guard = SingleInstanceGuard()
    if not guard.acquire():
        guard.focus_existing()
        guard.release()
        return 0
    paths = AppPaths.from_environment()
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    native_crash_log = (paths.log_dir / "native-crash.log").open(
        "a", encoding="utf-8", buffering=1
    )
    native_crash_log.write("\n=== LanInputShare process started ===\n")
    faulthandler.enable(file=native_crash_log, all_threads=True)
    configure_logging(paths.log_dir)
    store = ConfigStore(paths.config_file)
    runtime = _ConfiguredRuntime(store, paths)
    existing_application = QApplication.instance()
    application = (
        existing_application
        if isinstance(existing_application, QApplication)
        else QApplication([sys.argv[0]])
    )
    application.setApplicationName("LanInputShare")
    application.setWindowIcon(application_icon())
    application.setQuitOnLastWindowClosed(False)
    view_model = AppViewModel(runtime)
    transfer_popup = FileTransferPopup(runtime.cancel_transfer)
    runtime.set_status_callback(view_model.show_status)
    runtime.set_pairing_callback(view_model.decide_pairing)
    runtime.set_overwrite_callback(view_model.decide_overwrite)
    runtime.set_slot_status_callback(view_model.update_slot_status)
    runtime.set_transfer_status_callback(view_model.update_transfer_status)
    view_model.transfer_changed.connect(transfer_popup.update_transfer)
    try:
        config = store.load()
    except ConfigError:
        config = None
    if config is not None:
        view_model.load_config(config)
        runtime.start(config)
    tray = TrayController(view_model)
    tray_visible = tray.show()
    window = MainWindow(
        view_model,
        can_minimize_to_tray=tray.is_available,
        minimized_to_tray=tray.show_minimized_message,
    )
    overwrite_dialogs: dict[str, OverwriteDialog] = {}

    def show_overwrite(value: object) -> None:
        if not isinstance(value, OverwritePromptView):
            return
        dialog = OverwriteDialog(value, window)
        overwrite_dialogs[value.transfer_id] = dialog
        dialog.answered.connect(view_model.answer_overwrite)

        def remove_dialog() -> None:
            overwrite_dialogs.pop(value.transfer_id, None)
            dialog.deleteLater()

        dialog.finished.connect(lambda _result: remove_dialog())
        dialog.open()

    view_model.overwrite_requested.connect(show_overwrite)
    tray.settings_action.triggered.connect(window.restore)
    tray.restore_requested.connect(window.restore)
    view_model.quit_requested.connect(application.quit)
    if not minimized or not tray_visible:
        window.show()
    try:
        return int(application.exec())
    finally:
        view_model.cancel_pending_overwrites()
        transfer_popup.hide()
        tray.hide()
        runtime.shutdown()
        guard.release()
        faulthandler.disable()
        native_crash_log.close()


async def _await_object(value: object) -> None:
    if inspect.isawaitable(value):
        await value


__all__ = [
    "ApplicationCoordinator",
    "SingleInstanceGuard",
    "diagnose",
    "main",
]
