from __future__ import annotations

import ctypes
import re
import threading
import time
from typing import Protocol, cast

import pywintypes  # type: ignore[import-untyped]
import win32con  # type: ignore[import-untyped]
import win32file  # type: ignore[import-untyped]
import win32pipe  # type: ignore[import-untyped]
import win32security  # type: ignore[import-untyped]

from lan_input_share.elevated.protocol import MAX_HELPER_FRAME_BYTES

_PIPE_PREFIX = r"\\.\pipe\LanInputShare.Input"
_SID_PATTERN = re.compile(r"^S-(?:\d+-)+\d+$", re.IGNORECASE)
_CLOSED_ERRORS = {6, 109, 232, 233, 995}


class PipeClosedError(ConnectionError):
    pass


class PipeBackend(Protocol):
    def create_server(self, name: str, sddl: str) -> object: ...

    def connect_server(self, handle: object) -> None: ...

    def open_client(self, name: str, timeout: float) -> object: ...

    def read(self, handle: object, size: int) -> bytes: ...

    def available(self, handle: object) -> int: ...

    def write(self, handle: object, data: bytes) -> None: ...

    def cancel(self, handle: object) -> None: ...

    def close(self, handle: object) -> None: ...

    def get_client_pid(self, handle: object) -> int: ...


def pipe_name_for(user_sid: str, session_id: int) -> str:
    sid = _normalized_sid(user_sid)
    if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id < 0:
        raise ValueError("Windows 会话 ID 无效")
    return f"{_PIPE_PREFIX}.{sid}.{session_id}"


def pipe_sddl_for(user_sid: str) -> str:
    sid = _normalized_sid(user_sid)
    return f"D:P(A;;GA;;;SY)(A;;GA;;;{sid})"


class PipeConnection:
    def __init__(
        self,
        handle: object,
        backend: PipeBackend,
        *,
        client_pid: int = 0,
    ) -> None:
        self._handle: object | None = handle
        self._backend = backend
        self._lock = threading.Lock()
        self.client_pid = client_pid

    def read(self, size: int = MAX_HELPER_FRAME_BYTES + 4) -> bytes:
        handle = self._require_handle()
        try:
            return self._backend.read(handle, size)
        except BaseException as exc:
            raise _translate_closed(exc) from exc

    def write(self, data: bytes) -> None:
        handle = self._require_handle()
        try:
            self._backend.write(handle, data)
        except BaseException as exc:
            raise _translate_closed(exc) from exc

    def available(self) -> int:
        handle = self._require_handle()
        try:
            return self._backend.available(handle)
        except BaseException as exc:
            raise _translate_closed(exc) from exc

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
        if handle is None:
            return
        self._backend.cancel(handle)
        self._backend.close(handle)

    def _require_handle(self) -> object:
        with self._lock:
            if self._handle is None:
                raise PipeClosedError("命名管道已关闭")
            return self._handle


class NamedPipeServer:
    def __init__(
        self,
        name: str,
        user_sid: str,
        *,
        backend: PipeBackend | None = None,
    ) -> None:
        self._name = name
        self._sddl = pipe_sddl_for(user_sid)
        self._backend = backend or Win32PipeBackend()
        self._lock = threading.Lock()
        self._accept_handle: object | None = None
        self._closed = False

    def accept(self) -> PipeConnection:
        with self._lock:
            if self._closed:
                raise PipeClosedError("命名管道服务已关闭")
            handle = self._backend.create_server(self._name, self._sddl)
            self._accept_handle = handle
        try:
            self._backend.connect_server(handle)
            client_pid = self._backend.get_client_pid(handle)
            return PipeConnection(handle, self._backend, client_pid=client_pid)
        except BaseException as exc:
            self._backend.close(handle)
            raise _translate_closed(exc) from exc
        finally:
            with self._lock:
                if self._accept_handle is handle:
                    self._accept_handle = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            handle = self._accept_handle
        if handle is not None:
            self._backend.cancel(handle)


class NamedPipeClient:
    def __init__(self, name: str, *, backend: PipeBackend | None = None) -> None:
        self._name = name
        self._backend = backend or Win32PipeBackend()

    def connect(self, timeout: float) -> PipeConnection:
        if timeout < 0:
            raise ValueError("命名管道连接超时不能为负数")
        try:
            handle = self._backend.open_client(self._name, timeout)
        except BaseException as exc:
            raise _translate_closed(exc) from exc
        return PipeConnection(handle, self._backend)


class Win32PipeBackend:
    def create_server(self, name: str, sddl: str) -> object:
        security = pywintypes.SECURITY_ATTRIBUTES()
        security.SECURITY_DESCRIPTOR = (
            win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                sddl, win32security.SDDL_REVISION_1
            )
        )
        return win32pipe.CreateNamedPipe(
            name,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            win32pipe.PIPE_UNLIMITED_INSTANCES,
            MAX_HELPER_FRAME_BYTES + 4,
            MAX_HELPER_FRAME_BYTES + 4,
            0,
            security,
        )

    def connect_server(self, handle: object) -> None:
        try:
            win32pipe.ConnectNamedPipe(handle, None)
        except pywintypes.error as exc:
            if exc.winerror != 535:
                raise

    def open_client(self, name: str, timeout: float) -> object:
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                win32pipe.WaitNamedPipe(name, round(remaining * 1000))
                break
            except pywintypes.error as exc:
                if exc.winerror != 2 or remaining <= 0:
                    raise
                time.sleep(min(0.01, remaining))
        return win32file.CreateFile(
            name,
            win32con.GENERIC_READ | win32con.GENERIC_WRITE,
            0,
            None,
            win32con.OPEN_EXISTING,
            0,
            None,
        )

    def read(self, handle: object, size: int) -> bytes:
        _status, data = win32file.ReadFile(handle, size)
        return bytes(data)

    def available(self, handle: object) -> int:
        _data, available, _remaining = win32pipe.PeekNamedPipe(handle, 0)
        return int(available)

    def write(self, handle: object, data: bytes) -> None:
        win32file.WriteFile(handle, data)

    def cancel(self, handle: object) -> None:
        try:
            ctypes.windll.kernel32.CancelIoEx(int(cast(int, handle)), None)
        except (OSError, TypeError, ValueError):
            return

    def close(self, handle: object) -> None:
        try:
            win32file.CloseHandle(handle)
        except pywintypes.error as exc:
            if exc.winerror not in _CLOSED_ERRORS:
                raise

    def get_client_pid(self, handle: object) -> int:
        return int(win32pipe.GetNamedPipeClientProcessId(handle))


def _normalized_sid(user_sid: str) -> str:
    sid = user_sid.strip().upper()
    if not _SID_PATTERN.fullmatch(sid):
        raise ValueError("Windows 用户 SID 无效")
    return sid


def _translate_closed(exc: BaseException) -> BaseException:
    if isinstance(exc, PipeClosedError):
        return exc
    winerror = getattr(exc, "winerror", None)
    if winerror in _CLOSED_ERRORS:
        return PipeClosedError("命名管道已关闭")
    return exc


__all__ = [
    "NamedPipeClient",
    "NamedPipeServer",
    "PipeClosedError",
    "PipeConnection",
    "Win32PipeBackend",
    "pipe_name_for",
    "pipe_sddl_for",
]
