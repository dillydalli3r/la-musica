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
import re
from pathlib import Path
ROOT = Path(SPECPATH).resolve().parent

from PyInstaller.utils.hooks import collect_all


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

# pip travels INSIDE this backend. The desktop install has no separate Python,
# so the packages the Dependencies page installs are fetched by THIS build
# acting as its own interpreter (`mlo-server --mlo-python -m pip …`, see
# backend_launcher/__main__.py and mlo/fetchdeps._pip_python). collect_all also
# carries pip's data files — the distlib launcher executables pip writes
# console scripts with — which a hidden-import list alone would drop.
pip_datas, pip_binaries, pip_hiddenimports = collect_all("pip")
hiddenimports += pip_hiddenimports

# Script runners reached ONLY by NAME. The two script tables — the server's
# (server/script_runners.RUNNERS) and the terminal's (mlo/cli.build_script_runners)
# — name a runner's module as a STRING and resolve it with `__import__`, which
# PyInstaller's analysis cannot follow: it finds an import only where one is
# written. Every other module in those tables happens to be imported normally
# somewhere else, so the gap stayed invisible until `mlo.taghygiene` (script 23,
# Optimize tags) — the first with no other importer — shipped a desktop backend
# whose /api/run answered "runner 23 not available" for a menu entry it still
# offered. Read the tables HERE, so a runner added to them is bundled without a
# second edit: the assert is what keeps a parse that silently stopped matching
# from looking like a complete list.
_TABLE_FILES = (ROOT / "server" / "script_runners.py", ROOT / "mlo" / "cli.py")
_NAMED_RUNNERS = sorted({name for path in _TABLE_FILES
                         for name in re.findall(r'"((?:mlo|server)\.[a-z_0-9]+)"',
                                                path.read_text(encoding="utf-8"))})
assert len(_NAMED_RUNNERS) >= 10, f"script tables parsed to {_NAMED_RUNNERS}"
hiddenimports += _NAMED_RUNNERS

extra_datasets = list(pip_datas)

# The beets plugin (`mloplugin`) is loaded by NAME from a beets `pluginpath`,
# never imported by this server, so PyInstaller's analysis cannot see it. Pack
# it beside the sources: server/beetscfg.PLUGIN_DIR resolves to
# <_MEIPASS>/server/beets in a frozen build, and beets imports mloplugin from
# there. Without it a desktop beets import starts and dies on a missing plugin.
beets_plugin = ROOT / "server" / "beets"
if beets_plugin.is_dir():
    extra_datasets.append((str(beets_plugin), os.path.join("server", "beets")))

web_dist = os.environ.get("MLO_WEB_DIST") or str(ROOT / "web" / "dist")
if os.path.isdir(web_dist):
    extra_datasets.append((str(web_dist), os.path.join("web", "dist")))

# The native analysis helper (rust/, `mlo-audio`) travels INSIDE this backend,
# so the desktop install keeps one resource directory instead of three and the
# launcher can name it from `_MEIPASS` (see backend_launcher/__main__.py). A
# build with no helper still packages: mlo.dr falls back to the numpy block
# math, so a missing helper is slower, not broken.
helper = os.environ.get("MLO_AUDIO_BIN") or str(
    ROOT / "rust" / "target" / "release" / ("mlo-audio.exe" if os.name == "nt" else "mlo-audio"))
helper_binaries = [(helper, ".")] if os.path.isfile(helper) else []
helper_binaries += pip_binaries

a = Analysis(
    [str(ROOT / "backend_launcher" / "__main__.py")],
    pathex=[str(ROOT), str(ROOT / "server")],
    binaries=helper_binaries,
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