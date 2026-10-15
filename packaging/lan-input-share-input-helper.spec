# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path.cwd()
icon_path = project_root / "assets" / "lan-input-share.ico"

a = Analysis(
    [str(project_root / "src" / "lan_input_share" / "elevated" / "helper_main.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[
        "pywintypes",
        "win32con",
        "win32file",
        "win32pipe",
        "win32security",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6",
        "lan_input_share.clipboard",
        "lan_input_share.network",
        "lan_input_share.routing",
        "lan_input_share.runtime",
        "lan_input_share.transfer",
        "lan_input_share.ui",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="LanInputShareInputHelper",
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
