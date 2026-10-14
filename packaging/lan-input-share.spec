# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path.cwd()
icon_path = project_root / "assets" / "lan-input-share.ico"

a = Analysis(
    [str(project_root / "src" / "lan_input_share" / "__main__.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=[(str(icon_path), "assets")],
    hiddenimports=[
        "pythoncom",
        "pywintypes",
        "win32api",
        "win32clipboard",
        "win32com.client",
        "win32com.shell",
        "win32com.shell.shell",
        "win32com.shell.shellcon",
        "win32con",
        "win32gui",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Codex ships document tooling (including Poppler) on PATH.  PyInstaller may
# mistake those unrelated DLLs for Qt transitive dependencies, producing a
# package where Qt6Core fails with WinError 127 on a clean launch.
def _is_codex_poppler_binary(entry):
    source = str(entry[1]).lower().replace("/", "\\")
    return "\\codex-runtimes\\" in source and "\\native\\poppler\\" in source


a.binaries = [entry for entry in a.binaries if not _is_codex_poppler_binary(entry)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LanInputShare",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="LanInputShare",
)
