# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the desktop shell's private backend (`mlo-server`).
#
# Build:  python -m PyInstaller pyinstaller/mlo-server.spec
# Output: dist/mlo-server/  (onedir: the exe plus _internal/ — the shell
#         bundles that whole tree, so Windows/macOS/Linux each build it once).
#
# Packs the built SPA (web/dist, or MLO_WEB_DIST) at _internal/web/dist —
# `server.main`'s own ROOT/web/dist, exactly where a source checkout keeps it,
# and the same origin as the API (the session cookie's home). Nothing is
# patched at runtime.
import os
from pathlib import Path
ROOT = Path(SPECPATH).resolve().parent


block_cipher = None

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "multipart",
    "websockets.legacy",
    "websockets.server",
    "websockets.legacy.server",
]

extra_datasets = []
web_dist = os.environ.get("MLO_WEB_DIST") or str(ROOT / "web" / "dist")
if os.path.isdir(web_dist):
    extra_datasets.append((str(web_dist), os.path.join("web", "dist")))

a = Analysis(
    [str(ROOT / "backend_launcher" / "__main__.py")],
    pathex=[str(ROOT), str(ROOT / "server")],
    binaries=[],
    datas=extra_datasets,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mlo-server",
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="mlo-server",
)