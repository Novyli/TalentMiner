# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules


root = Path(SPECPATH)
webview_datas, webview_binaries, webview_hidden = collect_all("webview")
hidden_imports = webview_hidden + collect_submodules("uvicorn") + [
    "aiohttp",
    "fitz",
    "multipart",
    "networkx",
]
mac_icon = str(root / "assets" / "gqi-talent-radar.icns")
windows_icon = str(root / "assets" / "gqi-talent-radar.ico")

a = Analysis(
    [str(root / "desktop.py")],
    pathex=[str(root)],
    binaries=webview_binaries,
    datas=[(str(root / "static"), "static")] + webview_datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="GQI Talent Radar",
        icon=mac_icon,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="GQI Talent Radar",
    )
    app = BUNDLE(
        coll,
        name="GQI Talent Radar.app",
        icon=mac_icon,
        bundle_identifier="com.gqi.talentradar",
        info_plist={
            "CFBundleDisplayName": "GQI Talent Radar",
            "CFBundleName": "GQI Talent Radar",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="GQITalentRadar",
        icon=windows_icon,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
    )
