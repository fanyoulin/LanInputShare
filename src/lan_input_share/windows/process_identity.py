from __future__ import annotations

import ctypes
import hashlib
from ctypes import wintypes
from pathlib import Path
from typing import Protocol

from lan_input_share.elevated.models import ClientIdentity

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_DESKTOP_READOBJECTS = 0x0001
_DESKTOP_SWITCHDESKTOP = 0x0100
_UOI_NAME = 2


class ProcessIdentityApi(Protocol):
    def process_path(self, pid: int) -> Path: ...

    def process_user_sid(self, pid: int) -> str: ...

    def process_session_id(self, pid: int) -> int: ...

    def current_user_sid(self) -> str: ...

    def input_desktop_name(self) -> str | None: ...


def inspect_process_identity(
    pid: int, *, api: ProcessIdentityApi | None = None
) -> ClientIdentity:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("进程 ID 必须为正整数")
    active_api = api or Win32ProcessIdentityApi()
    path = active_api.process_path(pid).resolve(strict=True)
    return ClientIdentity(
        pid=pid,
        path=path,
        sha256=_sha256_file(path),
        user_sid=active_api.process_user_sid(pid),
        session_id=active_api.process_session_id(pid),
    )


def current_user_sid(*, api: ProcessIdentityApi | None = None) -> str:
    return (api or Win32ProcessIdentityApi()).current_user_sid()


def default_input_desktop_name(
    *, api: ProcessIdentityApi | None = None
) -> str | None:
    return (api or Win32ProcessIdentityApi()).input_desktop_name()


class Win32ProcessIdentityApi:
    def __init__(self) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._configure_signatures()

    def process_path(self, pid: int) -> Path:
        process = self._open_process(pid)
        try:
            size = wintypes.DWORD(32_768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not self._kernel32.QueryFullProcessImageNameW(
                process, 0, buffer, ctypes.byref(size)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            return Path(buffer.value)
        finally:
            self._kernel32.CloseHandle(process)

    def process_user_sid(self, pid: int) -> str:
        process = self._open_process(pid)
        try:
            return self._user_sid_from_process(process)
        finally:
            self._kernel32.CloseHandle(process)

    def process_session_id(self, pid: int) -> int:
        session_id = wintypes.DWORD()
        if not self._kernel32.ProcessIdToSessionId(pid, ctypes.byref(session_id)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(session_id.value)

    def current_user_sid(self) -> str:
        process = self._kernel32.GetCurrentProcess()
        return self._user_sid_from_process(process)

    def input_desktop_name(self) -> str | None:
        desktop = self._user32.OpenInputDesktop(
            0, False, _DESKTOP_READOBJECTS | _DESKTOP_SWITCHDESKTOP
        )
        if not desktop:
            return None
        try:
            needed = wintypes.DWORD()
            buffer = ctypes.create_unicode_buffer(256)
            if not self._user32.GetUserObjectInformationW(
                desktop,
                _UOI_NAME,
                buffer,
                ctypes.sizeof(buffer),
                ctypes.byref(needed),
            ):
                return None
            return buffer.value
        finally:
            self._user32.CloseDesktop(desktop)

    def _open_process(self, pid: int) -> int:
        handle = self._kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)

    def _user_sid_from_process(self, process: int) -> str:
        token = wintypes.HANDLE()
        if not self._advapi32.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            needed = wintypes.DWORD()
            self._advapi32.GetTokenInformation(
                token, _TOKEN_USER, None, 0, ctypes.byref(needed)
            )
            buffer = ctypes.create_string_buffer(needed.value)
            if not self._advapi32.GetTokenInformation(
                token,
                _TOKEN_USER,
                buffer,
                needed,
                ctypes.byref(needed),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
            if not sid_pointer:
                raise RuntimeError("Windows 令牌缺少用户 SID")
            sid_text = wintypes.LPWSTR()
            if not self._advapi32.ConvertSidToStringSidW(
                sid_pointer, ctypes.byref(sid_text)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return str(sid_text.value)
            finally:
                self._kernel32.LocalFree(sid_text)
        finally:
            self._kernel32.CloseHandle(token)

    def _configure_signatures(self) -> None:
        self._kernel32.GetCurrentProcess.argtypes = []
        self._kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self._kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.ProcessIdToSessionId.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL
        self._advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self._advapi32.OpenProcessToken.restype = wintypes.BOOL
        self._advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._advapi32.GetTokenInformation.restype = wintypes.BOOL
        self._advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        self._advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        self._user32.OpenInputDesktop.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._user32.OpenInputDesktop.restype = wintypes.HANDLE
        self._user32.GetUserObjectInformationW.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._user32.GetUserObjectInformationW.restype = wintypes.BOOL
        self._user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        self._user32.CloseDesktop.restype = wintypes.BOOL


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


__all__ = [
    "Win32ProcessIdentityApi",
    "current_user_sid",
    "default_input_desktop_name",
    "inspect_process_identity",
]
