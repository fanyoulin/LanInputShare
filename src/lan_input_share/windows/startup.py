from __future__ import annotations

import subprocess
import winreg
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "LanInputShare"


class Registry(Protocol):
    def set_current_user(self, key: str, name: str, value: str) -> None: ...

    def get_current_user(self, key: str, name: str) -> str | None: ...

    def delete_current_user(self, key: str, name: str) -> None: ...


class _WinRegistry:
    def set_current_user(self, key: str, name: str, value: str) -> None:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, key, access=winreg.KEY_SET_VALUE
        ) as handle:
            winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)

    def get_current_user(self, key: str, name: str) -> str | None:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key, access=winreg.KEY_QUERY_VALUE
            ) as handle:
                value, _kind = winreg.QueryValueEx(handle, name)
                return str(value)
        except FileNotFoundError:
            return None

    def delete_current_user(self, key: str, name: str) -> None:
        with suppress(FileNotFoundError), winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key, access=winreg.KEY_SET_VALUE
        ) as handle:
            winreg.DeleteValue(handle, name)


@dataclass(frozen=True, slots=True)
class StartupStatus:
    enabled: bool
    command: str | None


class StartupManager:
    def __init__(self, *, registry: Registry | None = None) -> None:
        self._registry = registry or _WinRegistry()

    def enable(self, command: Sequence[str]) -> None:
        if not command:
            raise ValueError("开机启动命令不能为空")
        self._registry.set_current_user(
            _RUN_KEY, _VALUE_NAME, subprocess.list2cmdline(list(command))
        )

    def disable(self) -> None:
        self._registry.delete_current_user(_RUN_KEY, _VALUE_NAME)

    def status(self) -> StartupStatus:
        command = self._registry.get_current_user(_RUN_KEY, _VALUE_NAME)
        return StartupStatus(command is not None, command)


__all__ = ["StartupManager", "StartupStatus"]
