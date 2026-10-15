from __future__ import annotations

import ctypes
import subprocess
from collections.abc import Sequence


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_elevated(argv: Sequence[str]) -> None:
    if not argv:
        raise ValueError("管理员重启参数不能为空")
    executable = str(argv[0])
    parameters = subprocess.list2cmdline(list(argv[1:]))
    _shell_execute_elevated(executable, parameters)


def request_helper_install(executable: str) -> None:
    relaunch_elevated((executable, "--install-input-helper"))


def request_helper_remove(executable: str) -> None:
    relaunch_elevated((executable, "--remove-input-helper"))


def _shell_execute_elevated(executable: str, parameters: str) -> None:
    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        executable,
        parameters or None,
        None,
        1,
    )
    if result <= 32:
        raise ctypes.WinError(result)


__all__ = [
    "is_elevated",
    "relaunch_elevated",
    "request_helper_install",
    "request_helper_remove",
]
