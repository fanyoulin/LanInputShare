from __future__ import annotations

import ctypes
import sys

_ERROR_ACCESS_DENIED = 5
_PER_MONITOR_AWARE_V2 = -4


def enable_per_monitor_v2() -> bool:
    """Use physical pixels for Win32 cursor and SendInput coordinates."""
    if sys.platform != "win32":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    setter = user32.SetProcessDpiAwarenessContext
    setter.argtypes = [ctypes.c_void_p]
    setter.restype = ctypes.c_bool
    if setter(ctypes.c_void_p(_PER_MONITOR_AWARE_V2)):
        return True
    error = ctypes.get_last_error()
    if error in (0, _ERROR_ACCESS_DENIED):
        return False
    raise ctypes.WinError(error)


__all__ = ["enable_per_monitor_v2"]
