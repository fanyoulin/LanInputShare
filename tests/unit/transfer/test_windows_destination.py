from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest


class FakeForeground:
    def __init__(self, handle: int | None, class_name: str = "CabinetWClass") -> None:
        self.handle = handle
        self.class_name = class_name
        self.calls = 0

    def capture(self):  # type: ignore[no-untyped-def]
        from lan_input_share.transfer.windows_destination import ForegroundWindow

        self.calls += 1
        if self.handle is None:
            return None
        return ForegroundWindow(self.handle, self.class_name)


class FakeShell:
    def __init__(self, paths: dict[int, str | None], *, error: Exception | None = None) -> None:
        self.paths = paths
        self.error = error
        self.calls: list[int] = []
        self.thread_id: int | None = None

    def resolve(self, window):  # type: ignore[no-untyped-def]
        self.calls.append(window.handle)
        self.thread_id = threading.get_ident()
        if self.error is not None:
            raise self.error
        return self.paths.get(window.handle)


@pytest.mark.asyncio
async def test_resolves_foreground_explorer_directory_off_event_loop(
    tmp_path: Path,
) -> None:
    from lan_input_share.transfer.windows_destination import (
        DestinationKind,
        ExplorerDestinationResolver,
    )

    foreground = FakeForeground(101)
    shell = FakeShell({101: str(tmp_path)})
    event_loop_thread = threading.get_ident()

    result = await ExplorerDestinationResolver(foreground, shell).resolve_foreground()

    assert result.kind is DestinationKind.DIRECT
    assert result.path == tmp_path.resolve()
    assert result.reason is None
    assert foreground.calls == 1
    assert shell.calls == [101]
    assert shell.thread_id != event_loop_thread


@pytest.mark.asyncio
async def test_binds_only_the_captured_explorer_window(tmp_path: Path) -> None:
    from lan_input_share.transfer.windows_destination import (
        DestinationKind,
        ExplorerDestinationResolver,
    )

    selected = tmp_path / "selected"
    other = tmp_path / "other"
    selected.mkdir()
    other.mkdir()
    foreground = FakeForeground(202)
    shell = FakeShell({201: str(other), 202: str(selected)})

    result = await ExplorerDestinationResolver(foreground, shell).resolve_foreground()

    assert result.kind is DestinationKind.DIRECT
    assert result.path == selected.resolve()
    assert shell.calls == [202]


@pytest.mark.asyncio
async def test_non_explorer_foreground_uses_compatibility_without_shell_query() -> None:
    from lan_input_share.transfer.windows_destination import (
        DestinationKind,
        ExplorerDestinationResolver,
    )

    shell = FakeShell({303: "C:/should-not-be-read"})

    result = await ExplorerDestinationResolver(
        FakeForeground(303, "Notepad"), shell
    ).resolve_foreground()

    assert result.kind is DestinationKind.COMPATIBILITY
    assert result.path is None
    assert result.reason == "not_explorer"
    assert shell.calls == []


@pytest.mark.asyncio
async def test_desktop_window_resolves_to_physical_desktop(tmp_path: Path) -> None:
    from lan_input_share.transfer.windows_destination import (
        DestinationKind,
        ExplorerDestinationResolver,
    )

    shell = FakeShell({404: str(tmp_path)})

    result = await ExplorerDestinationResolver(
        FakeForeground(404, "WorkerW"), shell
    ).resolve_foreground()

    assert result.kind is DestinationKind.DIRECT
    assert result.path == tmp_path.resolve()


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", [None, "", "::{20D04FE0-3AEA-1069-A2D8-08002B30309D}"])
async def test_virtual_or_missing_shell_location_uses_compatibility(
    candidate: str | None,
) -> None:
    from lan_input_share.transfer.windows_destination import (
        DestinationKind,
        ExplorerDestinationResolver,
    )

    result = await ExplorerDestinationResolver(
        FakeForeground(505), FakeShell({505: candidate})
    ).resolve_foreground()

    assert result.kind is DestinationKind.COMPATIBILITY
    assert result.path is None
    assert result.reason == "virtual_location"


@pytest.mark.asyncio
@pytest.mark.parametrize("make_file", [False, True])
async def test_non_directory_physical_location_uses_compatibility(
    tmp_path: Path,
    make_file: bool,
) -> None:
    from lan_input_share.transfer.windows_destination import (
        DestinationKind,
        ExplorerDestinationResolver,
    )

    candidate = tmp_path / "candidate"
    if make_file:
        candidate.write_text("not a directory", encoding="utf-8")

    result = await ExplorerDestinationResolver(
        FakeForeground(606), FakeShell({606: str(candidate)})
    ).resolve_foreground()

    assert result.kind is DestinationKind.COMPATIBILITY
    assert result.path is None
    assert result.reason == "not_directory"


@pytest.mark.asyncio
async def test_shell_failure_is_sanitized_and_uses_compatibility(tmp_path: Path) -> None:
    from lan_input_share.transfer.windows_destination import (
        DestinationKind,
        ExplorerDestinationResolver,
    )

    private_path = tmp_path / "private"
    shell = FakeShell({}, error=OSError(f"cannot open {private_path}"))

    result = await ExplorerDestinationResolver(
        FakeForeground(707), shell
    ).resolve_foreground()

    assert result.kind is DestinationKind.COMPATIBILITY
    assert result.reason == "shell_error"
    assert str(private_path) not in repr(result)


@pytest.mark.asyncio
async def test_missing_foreground_window_uses_compatibility() -> None:
    from lan_input_share.transfer.windows_destination import (
        DestinationKind,
        ExplorerDestinationResolver,
    )

    result = await ExplorerDestinationResolver(
        FakeForeground(None), FakeShell({})
    ).resolve_foreground()

    assert result.kind is DestinationKind.COMPATIBILITY
    assert result.reason == "window_unavailable"


def test_resolved_destination_rejects_inconsistent_state(tmp_path: Path) -> None:
    from lan_input_share.transfer.windows_destination import (
        DestinationKind,
        ResolvedDestination,
    )

    with pytest.raises(ValueError, match="直接"):
        ResolvedDestination(DestinationKind.DIRECT, None, None)
    with pytest.raises(ValueError, match="兼容"):
        ResolvedDestination(DestinationKind.COMPATIBILITY, tmp_path, None)


def test_resolver_can_be_called_from_plain_asyncio(tmp_path: Path) -> None:
    from lan_input_share.transfer.windows_destination import ExplorerDestinationResolver

    result = asyncio.run(
        ExplorerDestinationResolver(
            FakeForeground(808), FakeShell({808: str(tmp_path)})
        ).resolve_foreground()
    )

    assert result.path == tmp_path.resolve()
