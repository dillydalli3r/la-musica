# la musica — Desktop shell

Tauri v2 (Rust) wrapper around the React UI and Python backend.

## Targets

| Target | Build command | Output |
| --- | --- | --- |
| Windows 10/11 (x64) | `npm run build` on Windows | `.msi`, NSIS `.exe` |
| macOS 11+ (Intel/ARM) | `npm run build` on macOS | `.app`, `.dmg` |
| Linux (x64) | `npm run build` on Linux | `.deb`, `.AppImage` |
| Android 7.0+ (API 24) | `npx tauri android build --apk --debug` | `.apk` (debug-signed, installable) |
| iOS 14+ | `npx tauri ios build --target aarch64 --no-sign` | unsigned `.app` → `.ipa` |

All five share one crate. Everything that only makes sense with a Python
process and a desktop shell — backend spawn, tray icon, autostart registry,
hide-on-close — sits behind `#[cfg(desktop)]` in `src/lib.rs`, so the mobile
builds compile without it instead of carrying dead desktop code. Tauri's own
build script defines `desktop`/`mobile`, so the split follows the target.

## How it works (desktop)

- **Window** shows the built React app (`../web/dist`, built by
  `beforeBuildCommand`).
- **Backend**: on startup the shell spawns the FastAPI backend on
  `127.0.0.1:8000` and stops it when the app is quit. Resolution order:
  1. Bundled `mlo-server.exe` next to the app binary (PyInstaller one-file
     build — optional, for fully standalone installers)
  2. `python -m uvicorn server.main:app` from the repo checkout, when that
     checkout actually contains `server/main.py`
  Without either, the shell shows a "backend not found" dialog instead of
  spawning a `python` that has nothing to run.
- **Tray icon**: the window starts hidden and lives in the tray ("Open la
  musica", "Auto-start on login", "Exit (stop backend)"); closing the window
  hides it again. Quit stops the backend it spawned, and only force-kills a
  listener on :8000 that answers `/api/health` as ours.
- **Native folder picker**: `pick_folder` Tauri command, used by the import
  wizard via `invoke` to pick a source folder. The music folder itself is
  decided at startup (`MLO_MUSIC_FOLDER`, or `music_folder` in
  `config.json`) and is read-only in Settings — Settings has no picker.
- **Notifications**: `tauri-plugin-notification`, registered on every target,
  used by the web UI for "wish found", "download done" and "import ready".

## How it works (mobile)

A phone has no Python and no tray icon, so a mobile install is a **client** of
a backend running somewhere else:

- **The shell starts no backend.** It hosts the same React UI and nothing
  else.
- **The server address comes from the UI**: the login screen asks for the
  address of the server (LAN IP or a public URL) and the web app sends every
  API call there. Nothing in the shell needs to know it — which is why a
  mobile build has no server setting of its own.
- **Notifications work the same**, so a phone can be told that a wish was
  found or an import is ready by a backend it is configured against.
- **Capabilities** are split by platform: `capabilities/default.json` is
  desktop-only (folder picker + notifications), `capabilities/mobile.json`
  gives Android/iOS the core commands and notifications but no dialog
  permission, since there is no folder to pick.
- **Bundle config**: `bundle.iOS.minimumSystemVersion` 14.0 and
  `bundle.android.minSdkVersion` 24 in `tauri.conf.json`. The Android package
  name and the iOS bundle id both come from the top-level `identifier`; the
  Android permissions are declared by the generated project (`gen/android`),
  not in this config — Tauri v2 exposes no config key for them.

Mobile builds need the native projects, which are **generated, never
committed**: `npx tauri android init` (Android SDK, NDK, JDK 17) and
`npx tauri ios init` (Xcode, CocoaPods) write them into `src-tauri/gen/`. CI
does exactly that, so `.github/workflows/mobile.yml` is the reference for the
toolchain each target needs; a local build needs the same SDK/NDK or Xcode
installed first.

## Plain-http servers: what is shipped, and what you have to do

A self-hosted la musica server is plain `http` on an address only you know: a
LAN IP, a Tailscale/MagicDNS name, or `127.0.0.1:8000` for the desktop shell's
own backend. The server ships no certificate and offers no TLS, so both Apple
platforms have to be told to allow cleartext, or the app cannot reach *any*
server:

- **iOS and macOS** — `src-tauri/Info.plist` sets
  `NSAppTransportSecurity > NSAllowsArbitraryLoadsInWebContent`. Tauri merges
  that file into the generated iOS `Info.plist` at `tauri ios build` time (the
  `bundle.iOS.infoPlist` config key is the explicit form of the same thing),
  and into the macOS `.app`; without it App Transport Security blocks every
  `http://` and `ws://` request the webview makes — fetch, WebSocket, audio and
  video playback alike. The exemption covers web content only, so the shell's
  own native calls would still be held to full ATS (there are none: `lib.rs`
  carries no HTTP client).
- **Android** — there is no config key for the manifest's cleartext flag and
  the generated project is not committed, so the allowance is applied in CI
  right after `tauri android init` (`.github/workflows/mobile.yml`). The
  generated `app/build.gradle.kts` ships
  `manifestPlaceholders["usesCleartextTraffic"] = "false"` for every build type
  and `"true"` for `debug` only. **The APK CI publishes is a debug build, so it
  is already allowed**; a release APK built by hand needs the same flip before
  Gradle runs:

  ```bash
  cd desktop
  sed -i 's/\["usesCleartextTraffic"\] = "false"/["usesCleartextTraffic"] = "true"/' \
    src-tauri/gen/android/app/build.gradle.kts
  ```

What that asks of your network: the phone needs to be able to reach the
server's address — same Wi-Fi/LAN, or the Tailscale/VPN client running on the
phone — and the server must be listening on a reachable address rather than the
loopback default (`server_host` in the server's `config.json`, `127.0.0.1` out
of the box), because a server bound to loopback is invisible on the network.
Then type that address on the app's login screen (with the port) and sign in
with the auth password. An install that is reachable over `https` through a
reverse proxy works too — the cleartext allowance only widens what is
permitted, it does not require plain http.

## Mobile installs: what CI gives you

- **Android** — CI builds a **debug** APK (`--apk --debug`), which is signed
  with the SDK's debug keystore
  and therefore installs on any device that allows apps from outside the
  store. It is a *universal* build — every ABI tauri's generated project lists
  (arm64, armv7, x86_64, i686) — which is why the file is large (hundreds of
  MB); build one ABI by trimming `abiFilters` in
  `src-tauri/gen/android/app/build.gradle.kts` (or turn on Gradle's own ABI
  splits), and a release (non-debug) build loses the debug symbols on top of
  that. That is a deliberate choice: a release
  APK is only signed when `src-tauri/gen/android/keystore.properties` exists
  (a keystore generated locally, never committed), so a release build in CI
  would be an unsigned APK no phone accepts. To publish a signed release
  build, add that file as a CI secret, build with `--apk`, and `apksigner`
  with your own keystore.
- **iOS**: the IPA is an unsigned `.app` zipped into `Payload/`, which is what
  an `.ipa` is. Installing it needs an ad-hoc sideload tool (AltStore,
  Sideloadly, `ios-deploy`) that re-signs with a personal or team
  certificate. A store- or TestFlight-ready build needs an Apple Developer
  certificate, a provisioning profile and a team id (`APPLE_DEVELOPMENT_TEAM`
  or `bundle.iOS.developmentTeam`), none of which this repository carries —
  that step is a human's, with their own Apple account.

## Development

```bash
cd desktop
npm install
npm run dev        # vite dev UI + tauri window, spawns backend
npm run build      # release bundle (NSIS/msi on Windows)
```

The web UI detects the Tauri webview (`window.__TAURI_INTERNALS__`) and
switches API calls to `http://127.0.0.1:8000` automatically.

## CI

- `.github/workflows/desktop.yml` — Windows/macOS/Linux bundles, uploaded per
  platform.
- `.github/workflows/mobile.yml` — Android debug APK and unsigned iOS IPA.
- `ci.yml` runs `cargo check` for this crate on every push, so the crate graph
  cannot rot unnoticed.

## Standalone installers

To ship a fully standalone `.exe` without requiring Python:

1. Build the backend as a single file:
   `pyinstaller --onefile server/main.py --name mlo-server`
2. Place `mlo-server.exe` **next to the built app binary** (step 1 of the
   resolution order above). `tauri.conf.json` declares no bundle resources or
   external binaries, so a copy dropped into `src-tauri/resources/` is not
   packaged and will not be found.

## Icons

Icons come from `desktop/icon-source.png` through Tauri's own `tauri icon` —
`npx tauri icon icon-source.png` in `desktop/`, wrapped by
`python tools/make_tauri_icons.py` so the old entry point keeps working. That
one run writes every set: the desktop files (`32x32`, `128x128`, `128x128@2x`,
`icon.png`, `icon.icns`, `icon.ico`, plus the Windows Store logos) into
`src-tauri/icons/`, and the iOS `AppIcon-*.png` / Android `mipmap-*` sets into
`src-tauri/icons/{ios,android}` — or straight into `src-tauri/gen/` when the
generated native projects exist.

That destination rule is what the mobile builds hang on: `tauri android init` /
`tauri ios init` render **Tauri's own logo** into the generated project, and
the Xcode/Gradle build reads the app icon from there — never from
`bundle.icon`, which only feeds the desktop bundles. So the icon command has to
run *after* init, which is exactly what `.github/workflows/mobile.yml` does,
and it fails the build if the generated project still holds the template logo.
Delete `src-tauri/gen/` and re-run init plus the icon command if you changed
the artwork; `tauri icon` on its own cannot reach into a project that does not
exist yet.
