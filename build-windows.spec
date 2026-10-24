# -*- mode: python ; coding: utf-8 -*-

import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--debug", action="store_true")
args = parser.parse_args()

options = [
    ("X utf8", None, "OPTION"),
    ("hash_seed=0", None, "OPTION"),
    ("O", None, "OPTION"),
]

keep_file = ".\\data\\.keep"

block_cipher = None
added_files = [
    (".\\gui", "gui"),
    (keep_file, "webview/lib/runtimes/win-arm64/native"),
    (keep_file, "webview/lib/runtimes/win-x86/native"),
]

a = Analysis(
    [".\\tc2_launcher\\__main__.py"],
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

allowed_binaries = []
for binary in a.binaries:
    name, path, typecode = binary
    if typecode == "BINARY":
        if "webview" in path:
            if "WebBrowserInterop" in path:
                continue
            if any(test in path for test in ["win-x86", "win-arm64"]):
                continue
        if "clr_loader" in path:
            if "x86" in path:
                continue
        if "Windows Performance Toolkit" in path:
            continue
        if "api-ms-win-" in name:
            continue
        if "system32" in path:
            continue
        if "ucrtbase" in name:
            continue
        if "VCRUNTIME" in name:
            continue
    allowed_binaries.append(binary)

a.binaries = allowed_binaries

allowed_datas = []
for data in a.datas:
    name, path, typecode = data
    if typecode == "DATA":
        if "webview" in path:
            if ".jar" in name:
                continue
    allowed_datas.append(data)

a.datas = allowed_datas

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    options,
    name="TC2Launcher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version="version.rc",
    icon=[".\\gui\\favicon.ico"],
)

if args.debug:
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="TC2Launcher",
    )
