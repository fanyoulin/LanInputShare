from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_helper_pyinstaller_target_is_minimal_and_headless() -> None:
    spec = (ROOT / "packaging" / "lan-input-share-input-helper.spec").read_text(
        encoding="utf-8"
    )

    assert "lan_input_share\" / \"elevated\" / \"helper_main.py" in spec
    assert 'name="LanInputShareInputHelper"' in spec
    assert "console=False" in spec
    for excluded in ("PySide6", "lan_input_share.clipboard", "lan_input_share.network"):
        assert excluded in spec


def test_build_packages_and_hashes_both_executables() -> None:
    script = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")

    helper_build = script.index("lan-input-share-input-helper.spec")
    gui_build = script.index("lan-input-share.spec")
    assert helper_build < gui_build
    assert "LanInputShareInputHelper.exe --smoke-test" not in script
    assert "LanInputShareInputHelper.exe" in script
    assert "SHA256SUMS.txt" in script
    assert "release-manifest.txt" in script
