from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

_EXPLORER_WINDOW_CLASSES = frozenset({"CabinetWClass", "ExploreWClass"})
_DESKTOP_WINDOW_CLASSES = frozenset({"Progman", "WorkerW"})
_SUPPORTED_WINDOW_CLASSES = _EXPLORER_WINDOW_CLASSES | _DESKTOP_WINDOW_CLASSES


class DestinationKind(StrEnum):
    DIRECT = "direct"
    COMPATIBILITY = "compatibility"


@dataclass(frozen=True, slots=True)
class ForegroundWindow:
    handle: int
    class_name: str

    def __post_init__(self) -> None:
        if self.handle <= 0 or not self.class_name:
            raise ValueError("前台窗口信息无效")


@dataclass(frozen=True, slots=True)
class ResolvedDestination:
    kind: DestinationKind
    path: Path | None
    reason: str | None

    def __post_init__(self) -> None:
        if self.kind is DestinationKind.DIRECT:
            if self.path is None or self.reason is not None:
                raise ValueError("直接目标必须包含路径且不能包含兼容原因")
            return
        if self.path is not None or not self.reason:
            raise ValueError("兼容目标不能包含路径且必须包含原因")


class ForegroundWindowProvider(Protocol):
    def capture(self) -> ForegroundWindow | None: ...


class ShellWindowProvider(Protocol):
    def resolve(self, window: ForegroundWindow) -> str | None: ...


class Win32ForegroundWindowProvider:
    def capture(self) -> ForegroundWindow | None:
        import win32gui  # type: ignore[import-untyped]

        handle = int(win32gui.GetForegroundWindow())
        if handle <= 0:
            return None
        class_name = str(win32gui.GetClassName(handle))
        if not class_name:
            return None
        return ForegroundWindow(handle, class_name)


class Win32ShellWindowProvider:
    def resolve(self, window: ForegroundWindow) -> str | None:
        import pythoncom  # type: ignore[import-untyped]

        pythoncom.CoInitialize()
        try:
            if window.class_name in _DESKTOP_WINDOW_CLASSES:
                from win32com.shell import shell, shellcon  # type: ignore[import-untyped]

                return str(
                    shell.SHGetKnownFolderPath(shellcon.FOLDERID_Desktop, 0)
                )
            if window.class_name not in _EXPLORER_WINDOW_CLASSES:
                return None
            import win32com.client  # type: ignore[import-untyped]

            shell_application = win32com.client.Dispatch("Shell.Application")
            for shell_window in shell_application.Windows():
                try:
                    if int(shell_window.HWND) != window.handle:
                        continue
                    value = shell_window.Document.Folder.Self.Path
                    return str(value) if value else None
                except (AttributeError, TypeError, ValueError):
                    continue
            return None
        finally:
            pythoncom.CoUninitialize()


class ExplorerDestinationResolver:
    def __init__(
        self,
        foreground: ForegroundWindowProvider | None = None,
        shell: ShellWindowProvider | None = None,
    ) -> None:
        self._foreground = foreground or Win32ForegroundWindowProvider()
        self._shell = shell or Win32ShellWindowProvider()

    async def resolve_foreground(self) -> ResolvedDestination:
        try:
            window = self._foreground.capture()
        except (OSError, RuntimeError):
            return _compatibility("window_unavailable")
        if window is None:
            return _compatibility("window_unavailable")
        if window.class_name not in _SUPPORTED_WINDOW_CLASSES:
            return _compatibility("not_explorer")
        try:
            candidate = await asyncio.to_thread(self._shell.resolve, window)
        except Exception:
            return _compatibility("shell_error")
        if not candidate or candidate.startswith("::"):
            return _compatibility("virtual_location")
        try:
            path = Path(candidate).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return _compatibility("not_directory")
        if not path.is_dir():
            return _compatibility("not_directory")
        return ResolvedDestination(DestinationKind.DIRECT, path, None)


def _compatibility(reason: str) -> ResolvedDestination:
    return ResolvedDestination(DestinationKind.COMPATIBILITY, None, reason)


__all__ = [
    "DestinationKind",
    "ExplorerDestinationResolver",
    "ForegroundWindow",
    "ForegroundWindowProvider",
    "ResolvedDestination",
    "ShellWindowProvider",
    "Win32ForegroundWindowProvider",
    "Win32ShellWindowProvider",
]
