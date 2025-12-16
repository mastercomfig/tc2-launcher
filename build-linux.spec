# -*- mode: python ; coding: utf-8 -*-

options = [
    ("X utf8", None, "OPTION"),
    ("hash_seed=0", None, "OPTION"),
    ("O", None, "OPTION"),
]

block_cipher = None
added_files = [
    ("./gui", "gui"),
]

a = Analysis(
    ["./tc2_launcher/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=added_files,
    hiddenimports=["clr"],
    hookspath=["hooks"],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    options,
    name="TC2Launcher-linux-qt",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version="version.rc",
)
