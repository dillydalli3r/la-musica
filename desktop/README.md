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

Icons regenerate with `python tools/make_tauri_icons.py`, which writes the
desktop set (png/ico/icns) from `desktop/icon-source.png`. The committed
`src-tauri/icons/ios/` and `src-tauri/icons/android/` folders are the mobile
icon sets for those same sources.
