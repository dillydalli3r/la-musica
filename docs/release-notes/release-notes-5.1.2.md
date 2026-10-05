# la musica 5.1.2 - the desktop install finishes

5.1.1 made every installer start. 5.1.2 is where the install button *inside*
the Windows desktop app finishes: three tools could never install there, and
the app told you so with `Install finished with 3 failure(s): libjpeg-turbo,
librosa, beets`. They install now — on a machine with no Python and no 7-Zip,
which is most Windows machines — and the first-run screens stopped wearing the
library behind them.

## Why those three could never install

- **libjpeg-turbo** is the one tool upstream publishes for Windows only as an
  NSIS installer, and their installer *refuses to run a second time* — "An
  existing version of the libjpeg-turbo SDK for Visual C++ 64-bit … is already
  installed. Please uninstall it first", exit code 2, nothing written. The app
  needs 7-Zip to unpack NSIS without running it, and a Windows box usually has
  neither. The Windows desktop bundle now carries 7-Zip (`7z.exe`/`7z.dll`, the
  full build — a bare `7za` cannot read NSIS), `mlo.archives.find_7z` prefers
  the app's own copy over PATH, and the installer extracts `bin/jpegtran.exe`
  and the DLLs beside it instead of executing the vendor's installer at all.
- **librosa and beets** failed with `vendored Python packages need a Python
  interpreter on PATH`. A packaged desktop app has no interpreter, and it must
  not borrow one: `pip install --target` records wheels for *that* Python's
  platform and version, and the app then imports them with its own — a
  `cp312-win_amd64` numpy written by a PATH python 3.12 cannot be loaded by a
  frozen 3.13 server, so the folder lands and detection refuses it for both
  hosts. The frozen backend is now its own interpreter: `mlo-server
  --mlo-python` runs `-m module`, `-c` or a script with the runtime it already
  ships, `pip` rides inside the PyInstaller build, and both
  `mlo.fetchdeps._pip_python` and `server.beetscfg._python` use it. One shim
  makes pip work there at all — its vendored distlib maps a package's loader
  *type* to a resource finder and has never seen PyInstaller's, so the plain
  file-system finder is registered against it (`backend_launcher/__main__.py`).
- **beets runs, not just installs.** The same `--mlo-python` entry point is
  what `beet import` is spawned through, and the beets plugin
  (`server/beets/mloplugin.py`, loaded by name from a `pluginpath`) is packed
  with the backend, so a desktop beets import starts.

Verified on the packaged backend with PATH cut to `System32` — no `python`, no
`scoop` shims, no 7-Zip — against a tools folder seeded with the container's
Linux installs: all three now report `ok`, and the host-tagged pip folders
(`librosa v1.0.0-win-amd64-cp313`) sit beside the container's, the rule 5.1.1
already documented.

## Setup never shows the library

- **The wizard renders bare.** While setup is unfinished — and when it is
  re-run from Settings — `/setup` is rendered outside the shell, so there is no
  sidebar, no top bar, no player bar and no live event socket painting
  library progress or notifications over the screens you are answering.
- **The shell no longer flashes first.** The first-run gate is "the config says
  `first_run_done` is false", and while the config request was still in flight
  that check was false, so the full shell (sidebar, player, library home)
  mounted for a beat and was torn down for the wizard. The app now holds a
  setup frame while the config answers — an errored request still leaves the
  shell reachable.
- **Setup's own messages are visible.** Toasts were only rendered inside the
  shell, so a first-run wizard stored them and showed nothing — including
  `Install finished with N failure(s)`, the line that started this release. The
  toast stack is now rendered by every pre-shell screen too (backend chooser,
  client wizard, login/claim, setup), and the wizard's Tools step draws the
  Dependencies table with the page's own wrapper and width instead of a
  narrower copy.
- **The auth screen matches the flow behind it.** Login/claim drew a hand-
  rolled, narrower header; it now uses the same `PageHeader`, frame and width
  as the wizard it leads into, and its exit button reads "Close" on a re-run.

## The media flyout says "la musica"

Windows 11's now-playing flyout labelled a session from this app "unknown app".
It resolves a media session's AppUserModelID to a registered shell app, and
Tauri set none on the process — the installer had already stamped the bundle
identifier onto the shortcuts, but nothing on the running side used it. The
shell now calls `SetCurrentProcessExplicitAppUserModelID` with that identifier
before it starts (`desktop/src-tauri/src/main.rs`, raw `shell32` FFI — no new
dependency).

## Smaller

- **7-Zip is credited** (LGPL-2.1-or-later with the unRAR restriction; the
  bundled `License.txt` travels with the copy), in `THIRD-PARTY-NOTICES.md` and
  the in-app credits.
- **The contract says so**: `R69b` (a packaged install installs with its own
  interpreter and reads NSIS with its own 7-Zip) and `R107a` (setup is one
  surface and never wears the library) in
  `docs/OPTIMIZATION-GRADING-SPEC.md`.
- `python tools/check_desktop_deps.py` runs the packaged backend and proves a
  dependency install lands through it; the suite gate and the app's own
  browser check (`tools/check_backend_choice.mjs`) stay green.