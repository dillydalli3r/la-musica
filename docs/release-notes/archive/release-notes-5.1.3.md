# la musica 5.1.3 - the uninstall lets go of what it was holding

Uninstalling the Windows desktop app left `%LOCALAPPDATA%\la musica\mlo-server`
behind — not a folder of dead files, but a running backend, with the shell
it belonged to already gone. 5.1.3 stops the backend before the uninstaller
deletes anything, removes the backend tree whole, and moves the app's own
fresh-install state out of the tree the uninstaller is allowed to delete.

## Why the folder survived

The shell spawns the backend it ships (`mlo-server.exe`) as a child process,
and **Windows does not end a child when its parent exits**. Tauri's installer
looks for `mlo-desktop.exe` and nothing else, so the uninstaller closed the
shell, ran its delete list, and found `mlo-server.exe` alive with
`python312.dll`, `_multiarray_umath.pyd`, ucrt and the rest of its mapped
modules locked — none of them could be deleted, and the folder could not be
emptied.

`desktop/src-tauri/installer-hooks.nsh` now runs before install and uninstall
(wired through `bundle.windows.nsis.installerHooks`). It stops the **shell
first** — a live supervisor respawns a killed backend within a few seconds —
then the backend, and **polls until both are really gone**. A kill-and-return
is not enough: the uninstaller starts deleting the moment the hook returns, and
a process still tearing down keeps its DLLs locked, so those files survived
even though the kill had been issued. The uninstall hook also removes
`<install>\mlo-server` **recursively**: the template's per-file delete list
only knows the files *this* build installed, so a bundle whose hashed assets or
Python version changed had been leaving the previous build's copies behind and
the final, non-recursive `RMDir` could never empty the folder.

## Your data was never in that folder

The uninstaller is allowed to delete `<install>\mlo-server`, and nothing else.
The app's own state lives **beside** it in `%LOCALAPPDATA%\la musica`:
`config.json`, `shell.json`, `mlo-server.log`, `server/data` with `auth.db`,
`playlists.db` and the beets library, and `.dependencies` with the tools you
installed. None of it is in the installer's file list, so a reinstall reads it
back — the music folder, the wishes, the tools.

One case did hide state inside the backend tree: the launcher redirected
`mlo.paths` — `SCRIPT_DIR`, `CONFIG_FILE`, `DEPS_DIR` — but not
`LEGACY_DATA_DIR`, which `mlo.paths` derives from `SCRIPT_DIR` at import time.
A fresh install that had never set a music folder therefore wrote `auth.db` and
`playlists.db` into `<install>\mlo-server\server\data`, exactly where the
uninstall deletes. `backend_launcher._redirect_engine_home` now redirects that
too, so the whole state story is one folder that survives.

## Proved on a real build

A real 5.1.2-era NSIS build was installed, the bundled backend started from the
installed tree, and the app uninstalled silently while it ran: **0 files left
under `mlo-server`**, no `mlo-server.exe` in `tasklist`, the registry entry and
both shortcuts gone — and `config.json` byte-identical, with `shell.json`,
`mlo-server.log` and `mobile/` intact. A non-frozen launcher run started
against a scratch `MLO_APP_DATA_DIR` writes `server/data/auth.db` under that
folder, pinned by a new check in `tools/test_platform_guards.py`.