# la musica — Desktop shell

Tauri v2 (Rust) client for the React UI. The desktop install has TWO server
stories:

* **Local (default).** The shell bundles the app's own backend (`mlo-server`,
  a frozen Python service built from `pyinstaller/mlo-server.spec` on each OS
  at release time), spawns it on a free loopback port from 8011 up, serves the
  same React UI from THAT origin, and supervises the child. No Docker, no
  remote host: a la musica install runs entirely on this machine.
* **Remote (the classic mode).** The shell serves the built web app from the
  webview and talks HTTP to a server you point it at — the Docker container
  (see the [main README](../README.md#quick-start)), another machine on
  the LAN, or a Tailscale name. It never starts, adopts or stops that server.

## Targets

| Target | Build command | Output |
| --- | --- | --- |
| Windows 10/11 (x64) | `npm run build` on Windows | `.msi`, NSIS `.exe` |
| macOS 11+ (Intel/ARM) | `npm run build` on macOS | `.app`, `.dmg` |
| Linux (x64) | `npm run build` on Linux | `.deb`, `.AppImage` |

The shell is desktop-only: one crate builds these three installs. Each ships
the local backend: `tools/stage_desktop_bundle.py` stages the frozen
PyInstaller tree (`dist/mlo-server`, which itself carries the built SPA at
`_internal/web/dist` and the `mlo-audio` helper at `_internal/`) into
`desktop/bundle/mlo-server`, and the build maps that tree into the bundle as a
Tauri RESOURCE — so the installer places it at `<resource_dir>/mlo-server`,
which is where the shell spawns it from. `npm run build` in `desktop/` stages
and then builds with `src-tauri/tauri.bundle.conf.json`; CI stages per-OS and
passes the same map inline (see "Bundle config" below for why it is not in a
config file). Everything the shell adds around the webview — the tray icon, the
autostart registry, the folder picker, hide-on-close, the local backend — lives
in `src/lib.rs` for these targets.

### Windows install, uninstall and AppData

The NSIS installer is a **per-user** install: the shell and the staged backend
land in `%LOCALAPPDATA%\la musica` (`<install>\mlo-server` is the resource tree
the shell spawns). The shell's own per-user state lives in that same folder —
`config.json`, `shell.json`, `mlo-server.log`, and, until a music folder is
chosen, `server/data/auth.db` — because `backend_launcher`
redirects `mlo.paths.SCRIPT_DIR` **and `LEGACY_DATA_DIR`** there before the
engine is imported (the legacy dir is derived from `SCRIPT_DIR` at import time,
so naming only `SCRIPT_DIR` left fresh-install state inside the installed
backend tree). None of that state is in the installer's file list, and the NSIS
uninstaller deletes only the files it installed (its final `RMDir "$INSTDIR"`
is non-recursive), so **uninstalling removes the app and leaves the user's data
in AppData**; a reinstall reads it back.

The backend is a *child* of the shell, Windows does not end a child with its
parent, and Tauri's installer only looks for `mlo-desktop.exe`. So
`src-tauri/installer-hooks.nsh` (wired through `bundle.windows.nsis.
installerHooks`) runs before every install and uninstall: it stops the shell
first — a live supervisor respawns a killed backend within seconds — then the
backend, and polls until both are gone. Without that wait the uninstaller met
locked `python312.dll`/`*.pyd` files and left the backend tree, a running
server, behind in AppData. The uninstall hook then removes
`<install>\mlo-server` recursively: the template's per-file list only knows the
files the *current* build installed, so a bundle whose hashed assets or Python
version changed left the previous build's copies behind and the final,
non-recursive `RMDir` could not empty the folder.

### The external toolchain (flac, ffmpeg, oxipng, …)

The installers ship the *app*, not the tools: the Dependencies step installs
them into `<music>/.mlo/tools` (falling back to the per-user data dir while no
music folder is configured yet — a packaged app folder is read-only on macOS
and under `/usr/lib` on Linux, which is why the launcher redirects both it and
`mlo.paths.DEPS_DIR` before anything imports the engine). What each platform
can install there is decided in one place, `mlo/fetchdeps.py`:

| Platform | How a tool arrives |
|---|---|
| Windows | downloaded: every tool has a pinned Windows build |
| Linux (`LINUX_BINARIES` / `LINUX_PACKAGES`) | downloaded where upstream ships a Linux build (oxipng, fpcalc, rsgain, libjxl, libjpeg-turbo, AudioAuditor), otherwise the distro package, named in the row |
| macOS (`BREW_PACKAGES`) | nothing is downloaded: the row names the Homebrew formula (`brew install flac`) and the app then FINDS the result |

That last half is not free: a GUI-launched macOS app inherits launchd's
`PATH` (`/usr/bin:/bin:/usr/sbin:/sbin`), where Homebrew does not live — not
for detection, and not for spawning a tool by name either.
`backend_launcher/_augment_gui_path` prepends `/opt/homebrew/bin`,
`/usr/local/bin` and `/opt/local/bin` (only ones that exist) so the app sees
what the user's own shell sees. `tools/check_desktop_deps.py` runs the packed
backend on each OS in CI and presses the real button: Windows and Linux prove
`oxipng` lands on disk and reads `ok`, macOS proves every brew-backed row says
`brew install <formula>` and that no row names an apt package.

## How it works (desktop)

- **Window** shows the built React app. In local mode it is served by the
  shell's own backend at `http://127.0.0.1:<port>` — the SAME origin the API
  answers on, which is what makes the HttpOnly session cookie work in the
  webview (a cross-origin cookie is dropped by browser rules, which is why the
  remote-mode shell has to ride everything on the `token` query instead). The
  window opens *visible*: its first run is the app's own setup screen, which
  is no use behind a tray icon nobody has been told about.
- **The first run asks.** A shell that has never been told which backend to
  use shows the app's own build with two options: **use the built-in backend**
  (this app runs its own server — nothing to install) or **connect to a server
  you run** (the Docker container, a laptop, a home server). The answer is
  written to `shell.json` before anything is spawned or navigated, so a shell
  that already answered is never asked again
  (`web/src/pages/BackendChoice.tsx` → `choose_backend`). The tray's "Use the
  built-in backend" checkbox is the same question, reachable later: a page
  served by a REMOTE server cannot call the shell at all, so that item is the
  way back without editing `shell.json` by hand.

  Three rules keep that screen from becoming a trap, each one a bug that
  shipped:

  * the SHELL decides whether to ask, not the page. `mlo.clientSetup` lives in
    the webview's localStorage, and WebView2 keys that profile by the app
    IDENTIFIER — two shells of la musica share it — so a flag written by
    another install cannot hide the question from a shell that was never asked
    it (`App.tsx` reads `mode === "unset"` first);
  * the boot splash (`/splash.html`, part of the built SPA) has no navigation
    of its own, so every path that will not end in a running server leaves it
    explicitly (`backend_handle::show_app_page`, by moving the document — the
    window's configured `url` is applied after `setup()` returns, so a
    `navigate()` from there is overwritten), and a failed local start emits
    `stopped` with no origin so the screen can say so;
  * `shell_backend_choice` carries the live `status`
    (`running`/`starting`/`stopped`) beside the mode, because entering local
    mode reloads the page: the fresh document has missed the `mlo-backend`
    events, and without the status it fell through to the client wizard's
    server-ADDRESS step for as long as the backend took to boot.
- **One rail across the setup screens.** The backend question, the shell's
  client wizard (`ClientSetup.tsx`) and the app's own first run
  (`SetupPage.tsx`) draw the same header, frame and step rail
  (`components/SetupRail.tsx`), so answering the question reads as step 1 of
  the flow that follows rather than as a differently built page.
- **Server**: either the shell's child (`mlo-server`, local mode) or the
  address the user configured (remote mode). In local mode the shell finds a
  free loopback port from 8011 up (never 8000 — that is the live install),
  spawns the bundled backend with `MLO_SERVER_HOST/PORT` and the shell's
  per-user data dir set, waits for `/api/health`, then points the webview at
  it and tells the page (`mlo-backend` event) the backend is the shell's own.
  The per-user data dir (`%LOCALAPPDATA%/la musica` on Windows,
  `~/Library/Application Support/la musica` on macOS, `~/.local/share/la
  musica` on Linux) holds the server's own config before a music folder is
  chosen; after the setup wizard, app state lives in
  `<music>/.mlo/data/config.json` exactly as in every other install, so a
  music folder can move between the container and the desktop app without
  losing the RYM cookie or caches. In remote mode the client setup
  wizard (`web/src/pages/ClientSetup.tsx`) probes `${address}/api/health` with
  a 3 s deadline as before; the address is saved per device
  (`localStorage: mlo.server`) and changeable from Settings → Security.
- **One shell per machine**: a second launch — the login auto-start plus a
  click on the icon is the ordinary way to get two — brings the running window
  forward instead of starting anything. Two shells in local mode would mean
  two backends writing the same `<music>/.mlo/data`, two servers on
  8011/8012 and two tray icons (`tauri-plugin-single-instance`, keyed on the
  bundle identifier).
- **Tray icon**: the window lives in the tray while it is closed ("Open la
  musica", "Auto-start on login", "Keep backend running after quit",
  "Backend: running…", "Exit la musica"); closing the window hides it again,
  and Quit ends the shell. Quitting the shell normally stops the local backend
  it spawned — the backend is that shell's child — unless "Keep backend
  running after quit" is checked, which leaves it up for browsers on the
  network. A remote server is never stopped: it belongs to whoever runs it.
- **Native folder picker**: `pick_folder` Tauri command, used by the import
  wizard via `invoke` to pick a source folder. The music folder itself is
  decided at startup (`MLO_MUSIC_FOLDER`, or `music_folder` in
  `config.json`) and stays read-only in Settings — Settings has no picker.
  The setup wizard's folder step calls the shell's `set_music_folder` command
  so the next launch spawns the backend already knowing the library.
- **Notifications**: `tauri-plugin-notification`, registered on every desktop
  target, used by the web UI to raise an OS notification for the events the
  server publishes on the socket (`lib/notify.ts`) — a finished import, an
  import left short of something, a script run, a grade, a newer release — so
  the news reaches the user with the window in the background (the bell's
  panel lists them all).

## Bundle config

The top-level `identifier` (`com.musiclibraryoptimizer.lamusica`) is the
bundle id the desktop builds use. The old `com.musiclibraryoptimizer.app` is
the shape tauri-cli warns about on every build, because an identifier ending in
`.app` reads as the bundle extension; nothing rejects it, it is just the
default-shaped mistake.

The desktop bundles carry **one resource** — the staged backend tree, mapped by
`bundle.resources` to `<resource_dir>/mlo-server`, which is where `backend.rs`
looks for the process it spawns. It is declared where the staging happens, not
in a config file: `desktop/package.json`'s `build` passes
`--config src-tauri/tauri.bundle.conf.json`, and CI passes the same map inline.
The reason is that `tauri-build` validates resource paths on every compile, so a
resource in `tauri.conf.json` would make a plain `cargo check` fail in a
checkout that never staged a 300 MB backend ("resource path ... doesn't
exist") — which is every fresh clone and the `ci/desktop` leg.

The `src-tauri/Info.plist` sits beside `tauri.conf.json` and Tauri merges it
into the generated macOS `.app` plist; its one job is the App Transport
Security exemption described below.

## Plain-http servers: what is shipped, and what you have to do

A self-hosted la musica server is plain `http` on an address only you know: a
LAN IP, a Tailscale/MagicDNS name, or `127.0.0.1:8000` for the Docker container
running on the same machine. The server ships no certificate and offers no TLS,
so the macOS build has to be told to allow cleartext, or the app cannot reach
*any* server:

- **macOS** — `src-tauri/Info.plist` sets
  `NSAppTransportSecurity > NSAllowsArbitraryLoadsInWebContent` **and** the
  blanket `NSAllowsArbitraryLoads`. Tauri merges that file into the generated
  macOS `.app` plist; without those keys App Transport Security blocks every
  `http://` and `ws://` request the webview makes — fetch and the event
  WebSocket alike. The scoped web-content key keeps the exemption inside the
  page's own process, while the shell's native calls (there are none: `lib.rs`
  carries no HTTP client) would stay under full ATS. The blanket key is kept as
  the coarse exemption for a system that reads no scoped key at all (macOS
  10.11); on every macOS this app can run on it is the scoped key that decides
  and the blanket key is inert. Windows and Linux need no such file: their
  webviews allow plain `http` by default.

What that asks of your network: a client needs to be able to reach the
server's address — same Wi-Fi/LAN, or the Tailscale/VPN client running on it —
and the server must be listening on a reachable address rather than the
loopback default (`server_host` in the server's `config.json`, `127.0.0.1` out
of the box), because a server bound to loopback is invisible on the network.
Then type that address on the app's login screen (with the port) and sign in
with the auth password. An install that is reachable over `https` through a
reverse proxy works too — the cleartext allowance only widens what is
permitted, it does not require plain http.

## Development

```bash
# from the repo root — the frozen backend the desktop install spawns
cargo build --release --manifest-path rust/Cargo.toml   # mlo-audio, zero crate deps
python -m PyInstaller pyinstaller/mlo-server.spec       # dist/mlo-server (carries web/dist + the helper)

cd web && npm install && npm run build                  # web/dist, the Tauri frontendDist
cd ../desktop
npm install
npm run dev        # vite dev UI + tauri window
npm run build      # stages desktop/bundle/mlo-server, then NSIS/msi on Windows
```

The shell runs that backend itself (`Local backend`, the default): it picks a
free loopback port from 8011 up, spawns `mlo-server`, and points the window at
it. `npm run build` refuses to bundle without a staged backend, so the freeze
step above is not optional for a release build. Pointing the app at a server
you run yourself (`docker compose up -d` in the repo root, or
`python -m uvicorn server.main:app --host 127.0.0.1 --port 8000` for a source
checkout) is the other mode — Settings → Security; the wizard asks for the
address when a shell has no backend of its own.

## CI

- `.github/workflows/desktop.yml` — Windows/macOS/Linux bundles, uploaded per
  platform.
- `ci.yml` runs `cargo check` for this crate on every push, so the crate graph
  cannot rot unnoticed.

## Icons

Icons come from `desktop/icon-source.png` through Tauri's own `tauri icon` —
`npx tauri icon icon-source.png` in `desktop/`, wrapped by
`python tools/make_tauri_icons.py` so the old entry point keeps working. That
one run writes the desktop set (`32x32`, `128x128`, `128x128@2x`, `icon.png`,
`icon.icns`, `icon.ico`, plus the Windows Store logos) into
`src-tauri/icons/`.