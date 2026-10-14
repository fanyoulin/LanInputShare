from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PIL import Image

EXPECTED_SIZES = {
    (16, 16),
    (20, 20),
    (24, 24),
    (32, 32),
    (48, 48),
    (64, 64),
    (128, 128),
    (256, 256),
}


def test_application_ico_contains_all_required_sizes() -> None:
    path = Path(__file__).parents[3] / "assets" / "lan-input-share.ico"

    assert path.is_file()
    with Image.open(path) as icon:
        assert icon.ico.sizes() == EXPECTED_SIZES


def test_application_icon_is_cached_and_non_null(qtbot) -> None:  # type: ignore[no-untyped-def]
    from lan_input_share.ui.resources import application_icon

    _ = qtbot
    application_icon.cache_clear()
    first = application_icon()
    second = application_icon()

    assert not first.isNull()
    assert first.cacheKey() == second.cacheKey()


def test_application_icon_loads_from_pyinstaller_meipass(
    qtbot, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from lan_input_share.ui.resources import application_icon

    _ = qtbot
    bundled = tmp_path / "assets"
    bundled.mkdir()
    source = Path(__file__).parents[3] / "assets" / "lan-input-share.ico"
    shutil.copy2(source, bundled / source.name)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    application_icon.cache_clear()

    assert not application_icon().isNull()
    application_icon.cache_clear()


def test_missing_resource_uses_non_null_fallback(qtbot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import lan_input_share.ui.resources as resources

    _ = qtbot
    monkeypatch.setattr(resources, "_icon_path", lambda: Path("missing.ico"))
    resources.application_icon.cache_clear()

    assert not resources.application_icon().isNull()
    resources.application_icon.cache_clear()
