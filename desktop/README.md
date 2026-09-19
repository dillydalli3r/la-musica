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
- **Bundle config**: `bundle.iOS.minimumSystemVersion` 14.0,
  `bundle.iOS.bundleVersion` 3.1.2, `bundle.iOS.infoPlist` and
  `bundle.android.minSdkVersion` 24 in `tauri.conf.json`. The Android package
  name and the iOS bundle id both come from the top-level `identifier`
  (`com.musiclibraryoptimizer.lamusica` — the old
  `com.musiclibraryoptimizer.app` is the shape tauri-cli warns about on every
  build, because an identifier ending in `.app` reads as the bundle extension;
  nothing rejects it, it is just the default-shaped mistake). The
  Android permissions are declared by the generated project (`gen/android`),
  not in this config — Tauri v2 exposes no config key for them.

  The iOS `Info.plist` is `src-tauri/Info.plist`, named by
  `bundle.iOS.infoPlist` so the merge is a repo decision rather than the CLI's
  auto-detection. Tauri merges it into the macOS `.app` too (the same file
  also carries the App Transport Security exemption described below). What it
  states about the app: `CFBundleDisplayName`/`CFBundleName` "la musica",
  `ITSAppUsesNonExemptEncryption` false, and the orientation sets — iPhone
  portrait + both landscapes, iPad all four. `CFBundleVersion` is
  `bundle.iOS.bundleVersion`, deliberately stated: `CFBundleShortVersionString`
  is the marketing version (`tauri.conf.json` `version`, which must match
  `mlo/__init__.py`), and the build number is what changes when the *same*
  version is rebuilt for a re-upload or a re-sideload.

Mobile builds need the native projects, which are **generated, never
committed**: `npx tauri android init` (Android SDK, NDK, JDK 17) and
`npx tauri ios init` (Xcode, CocoaPods) write them into `src-tauri/gen/`. CI
does exactly that, so `.github/workflows/mobile.yml` is the reference for the
toolchain each target needs; a local build needs the same SDK/NDK or Xcode
installed first.

## Home-screen installs

The React UI is installable from any browser on the served address — the
desktop shell's own webview, a phone pointed at a server, and the plain web
build are the same files. `web/public/manifest.webmanifest` carries what a
home screen needs and `web/index.html` links it, plus the `apple-touch-icon`
iOS Safari reads instead (it ignores the manifest's `icons`):

- name/short name "la musica", `start_url`/`scope` `/`, `display` standalone.
- `theme_color` and `background_color` `#0a0a0c` — the `bg` token in
  `web/tailwind.config.js` and the `<meta name="theme-color">` in
  `web/index.html`, so the status bar and splash match the app.
- one icon, `/icon.png` at 512×512 (`web/public/icon.png`, drawn by
  `tools/make_icons.py`). Declared `purpose: "any"`, not `maskable`: the
  artwork is full-bleed, and a maskable declaration promises Android it can
  crop to a circle without eating the picture.
- three `shortcuts` — Favorites `/favorites`, Playlists `/playlists`, Search
  `/library` (all real routes in `web/src/App.tsx`).

**Only Android honours `shortcuts`.** Chrome/Android shows them on a
long-press of the installed icon. iOS Safari does not implement the manifest's
`shortcuts` at all, and the native equivalent — `UIApplicationShortcutItems`
in `Info.plist` for static items, or `UIApplication.shared.shortcutItems` from
Swift for dynamic ones, both surfaced by the same long-press — needs an app
delegate of our own in the generated Xcode project. This repo has none
(`tauri ios init` generates one and we never touch `src-tauri/gen/`), so an
iOS home-screen install gets the icon and no jump list, and `tauri.conf.json`
deliberately carries no shortcut config that would only ever apply to Android.

## Plain-http servers: what is shipped, and what you have to do

A self-hosted la musica server is plain `http` on an address only you know: a
LAN IP, a Tailscale/MagicDNS name, or `127.0.0.1:8000` for the desktop shell's
own backend. The server ships no certificate and offers no TLS, so both Apple
platforms have to be told to allow cleartext, or the app cannot reach *any*
server:

- **iOS and macOS** — `src-tauri/Info.plist` sets
  `NSAppTransportSecurity > NSAllowsArbitraryLoadsInWebContent`. Tauri merges
  that file into the generated iOS `Info.plist` at `tauri ios build` time —
  `bundle.iOS.infoPlist` names it, and it is the last plist merged, so what it
  says wins — and into the macOS `.app`; without it App Transport Security
  blocks every `http://` and `ws://` request the webview makes — fetch,
  WebSocket, audio and
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

  What the installed IPA shows comes from this repo, not from the CLI's
  templates: `CFBundleDisplayName`/`CFBundleName` "la musica",
  `CFBundleShortVersionString` from `tauri.conf.json` `version`,
  `CFBundleVersion` from `bundle.iOS.bundleVersion`, iPad/iPhone orientation,
  and `ITSAppUsesNonExemptEncryption` false — the last one matters here
  because a sideload tool re-signs the bundle, and an edit to a signed
  `Info.plist` is exactly what invalidates that signature, so the answer to
  the export-compliance question has to be inside the file we build.

The identifier changed this release (`com.musiclibraryoptimizer.lamusica`, a
build from before it used `…optimizer.app`), and both platforms key an update
on that one string — Android's package id is the identifier too. So an older
APK or IPA is left installed *beside* the new build, keeps its own icon, and
keeps whatever it stored locally (the service worker's caches, the saved
server address); the new install starts empty and asks for the server address
again. Nothing on the server is affected — the library, playlists and likes
live there, not in the app — so the cost is one manual uninstall of the old
icon.

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
