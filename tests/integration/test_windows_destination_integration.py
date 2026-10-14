from __future__ import annotations

from pathlib import Path

import pytest

from lan_input_share.transfer.windows_destination import (
    DestinationKind,
    ExplorerDestinationResolver,
)


@pytest.mark.windows_integration
@pytest.mark.asyncio
async def test_current_foreground_explorer_resolves_existing_directory() -> None:
    resolver = ExplorerDestinationResolver()

    result = await resolver.resolve_foreground()

    if result.kind is DestinationKind.COMPATIBILITY:
        pytest.skip(f"foreground is not a physical Explorer folder: {result.reason}")
    assert isinstance(result.path, Path)
    assert result.path.is_dir()
