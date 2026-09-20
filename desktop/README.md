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

A phone has no Python to spawn, so the build carries one. The interpreter is
**embedded**: `src/mobile_backend.rs` links CPython (Python-Apple-support's
`Python.framework` on iOS, the python.org Android embeddable package on Android),
points it at the runtime tree staged by `tools/mobile/bundle.py`, and runs
`server.main:app` *inside this process*.

- **Why in-process and not a child process.** On iOS the sandbox gives an app no
  way to execute a second program at all. On Android the Python documentation is
  explicit that embedding is "the only way you can use Python on Android" —
  there is no `python` executable and no console to run one in. So one
  implementation covers both, and the difference is only where the runtime lives.
- **What it keeps from the desktop path.** The same guarantees, because it is the
  same code: the port is probed before anything starts, a listener that answers
  `/api/health` as ours is *adopted* rather than duplicated, a foreign listener
  on :8000 is neither adopted nor killed, and the app asks the backend to exit
  through the env-gated `/api/shutdown` on the way out. There is no child
  process to kill (the backend *is* this process), so nothing can be orphaned.
- **The webview is told the address.** `backend_info` (a mobile-only Tauri
  command) answers `{state, url, reason, embedded, tools, missing, keepalive}`,
  the same object is emitted as the `mlo-backend` event whenever it changes, and
  `window.__MLO_BACKEND_URL__` is set on the main webview. `state` is one of
  `starting`, `hosting`, `unavailable` (with the sentence to show the user in
  `reason`) or `busy` (something that is not us holds the port).
- **A build that cannot host a backend is still a working client.** If the
  runtime was never staged — or was staged without `pydantic-core`, which has no
  wheel on either platform — the staged `runtime.json` says so, the shell answers
  `unavailable` with that reason, and the app installs, opens and lets the user
  point at a server. That is the *only* difference between the two kinds of
  build: neither one fails at launch.
- **Notifications work the same**, so a phone can be told that a wish was found
  or an import is ready.
- **Capabilities** are split by platform: `capabilities/default.json` is
  desktop-only (folder picker + notifications), `capabilities/mobile.json`
  gives Android/iOS the core commands and notifications but no dialog
  permission, since there is no folder to pick. The shell's own commands need no
  permission entry (Tauri only ACL-checks app commands when the app ships its own
  ACL manifest, and this one does not).

### What an iOS-local backend cannot do

iOS cannot execute another program, so every feature the backend implements by
running a tool is unavailable there. The app reports this rather than failing on
first use (`/api/capabilities` on the backend, `backend_info` in the shell), and
the list is worth stating plainly:

- **Transcoding and lossless conversion** — ffmpeg/flac (`mlo/flac.py`), and with
  them the video remux pass (`mlo/remux.py`), since both are subprocesses.
- **Integrity and disc verification** — `flac -t` / `ffmpeg -f null` (`mlo/audit.py`),
  AccurateRip and the CD CRC32 check (`mlo/accurip.py`, `mlo/discs.py`).
- **Loudness work** — ReplayGain measurement and Dynamic Range tags
  (`mlo/loudness.py`), which need `rsgain` or `simple-dr-meter` + ffmpeg.
- **AcoustID import matching** — needs `fpcalc` (`mlo/acoustid.py`); the import
  pipeline falls back to title/artist search.
- **Beets, slskd/Soulseek, yt-dlp's ffmpeg post-processing, CUETools, php,
  Logchecker, AudioAuditor, oxipng/cjxl/jpegtran** — all external programs.
- **Anything requiring a writable path outside the app** — iOS gives an app its
  own container and nothing else, so the music folder has to live inside it
  (the Files app's "On My iPhone/la musica" directory is reachable there).
- **Serving while suspended** — see the keep-alive section below: iOS suspends
  the app, and a suspended app answers nothing.

What does work on-device and in-process is the substance of the app: tag reading
and writing (mutagen), scanning, the library API, streaming and range requests,
playlists, favourites, search, grading, recommendations, cover art (Pillow has
an iOS wheel) and the whole web UI.

## Bundling a mobile backend

`tools/mobile/` assembles what a mobile build ships. The scripts run on any host
for everything except the cross-builds, and they are idempotent: re-running with
the same inputs does nothing, `--force` restages.

```bash
# iOS: the framework + standard library + site-packages + the backend's sources.
python tools/mobile/bundle.py ios [--wheels DIR] [--build-missing-wheels]

# Android: the same tree as one payload in the native-lib directory, plus the
# CLI tools Android can actually execute.
python tools/mobile/bundle.py android [--wheels DIR] [--tools DIR]

# Prove a built artifact really carries the runtime (this is what CI asserts).
python tools/mobile/bundle.py verify --apk app-release.apk --require-tools
python tools/mobile/bundle.py verify --ipa la-musica.ipa

# The Android tools (ffmpeg/flac/metaflac for aarch64), built from pinned
# sources with the NDK — needs Linux/macOS + ANDROID_HOME.
bash tools/mobile/build_android_tools.sh /tmp/mlo-tools
```

Where things land, and why there rather than somewhere simpler:

| | iOS | Android |
| --- | --- | --- |
| interpreter | `Python.xcframework`, linked and embedded via `bundle.iOS.frameworks` | `libpython3.14.so` + its `*_python.so` externals in `jniLibs/arm64-v8a/` |
| standard library + site-packages | `resources/mobile/ios/python/**` → `<app>/assets/mobile/python/` (real files: iOS `resource_dir()` is a path) | one gzipped tar (`libmlopy.so`) in the same native-lib directory, unpacked to the app's data dir on first launch |
| backend sources | copied out of the read-only bundle into the app's data dir on first launch (the backend writes `config.json` next to its own code) | used in place (already writable) |
| CLI tools | none — iOS cannot run them | `libffmpeg.so`, `libflac.so`, … beside the interpreter; the shell exports `MLO_BUNDLED_TOOLS` and `mlo/tools.py` resolves them before `.dependencies` and `PATH` |
| capability manifest | `runtime.json` inside the python tree, written by the pipeline from what it actually installed | the same file inside the payload |

Android's split is forced by the platform, not chosen: APK **assets have no
filesystem path** (an app reads them through the Java AssetManager, and Tauri's
`resource_dir()` there is the pseudo-URI `asset://localhost/`), while CPython
needs real files for `import`; and AGP only packages files named `lib*.so` from
`jniLibs`. The native-lib directory is the one place that is both packaged and
executable, so the interpreter, its libraries, the payload and the tools all
live there.

`gen/android` is generated and never committed, so the parts that touch it are
applied in CI — `.github/workflows/mobile.yml` runs
`python tools/mobile/android/inject.py --project src-tauri/gen/android
--jni-libs resources/mobile/android/jniLibs/arm64-v8a` after `tauri android
init`, which copies the staged libraries in, pins the build to `arm64-v8a`
(the only ABI the bundle has a runtime for), turns on
`jniLibs.useLegacyPackaging` (without it the libraries are loaded straight out of
the APK and have no path to read or execute), and adds the keep-alive service.
`tools/mobile/bundle.py verify --apk` is the assertion that all of it survived
into the artifact.

### The wheel that does not exist

`pydantic-core` has no iOS **or** Android wheel on PyPI (FastAPI cannot import
without it, so there is no such thing as a working backend without it), and
Pillow has no Android wheel either. BeeWare's
[mobile-wheels](https://beeware.org/mobile-wheels/) index says the same thing and
recommends **cibuildwheel**, which builds for both mobile platforms. So the
pipeline builds it once per platform, caching the wheel:

```bash
pip install cibuildwheel
python tools/mobile/bundle.py ios --wheels desktop/src-tauri/target/mobile-wheels/ios \
    --build-missing-wheels
```

That runs `cibuildwheel --platform ios --archs arm64_iphoneos pydantic-core-<pin>`
(or `--platform android --archs arm64_v8a` with `ANDROID_HOME` and the NDK set).
**The iOS half needs macOS with Xcode, and the Android half needs a POSIX host
with the Android SDK/NDK and java** — neither can run on Windows, which is why
the CI job does it and why an iOS build cannot be produced from Windows at all.
Without `--wheels`, the pipeline fails with that message rather than staging a
bundle that would install and then fail on first use.

### Hosting while the app is in the background

Both platforms suspend a backgrounded app, and a suspended app serves nothing
(the UI reconnects when it comes back; it must not treat that as a dead server).
The shell's *keep hosting in the background* setting — `set_backend_keepalive`,
stored as a marker file in the app's data directory because it has to survive a
relaunch — asks the platform to keep the process alive anyway:

- **Android**: a foreground service
  (`tools/mobile/android/MloKeepAlive.kt`, type `specialUse` — hosting a library
  for other devices is none of the specific types Android 14 requires, and
  `dataSync` is both unfitting and time-limited on Android 15). It shows an
  ongoing notification, follows the marker file on a five-second poll so a live
  toggle takes effect without a restart, and `START_STICKY` which is the closest
  Android gets to "keep hosting". The user should exempt the app from battery
  optimisation.
- **iOS**: `UIBackgroundModes: [audio]` in `Info.plist` plus an active playback
  audio session. While music plays — the app is a music player, so this is the
  honest half — the process keeps running and the backend keeps serving. With
  nothing playing, the setting holds a *silent* session open: that works, it costs
  battery, and Apple's review guidance names silent audio kept alive for its own
  sake as abuse, so it is **off by default, opt-in, and only defensible because
  this build is sideloaded**. An App Store submission must not ship it turned on.
  There is no other mechanism: iOS grants background execution for a fixed list
  of modes, and running a server is not one of them.

### Bundle config

`bundle.iOS.minimumSystemVersion` 14.0, `bundle.iOS.bundleVersion` 3.1.5,
`bundle.iOS.infoPlist`, `bundle.iOS.frameworks` (the staged
`Python.xcframework`) and `bundle.android.minSdkVersion` 24 in
`tauri.conf.json`. The Android package name and the iOS bundle id both come from
the top-level `identifier` (`com.musiclibraryoptimizer.lamusica` — the old
`com.musiclibraryoptimizer.app` is the shape tauri-cli warns about on every
build, because an identifier ending in `.app` reads as the bundle extension;
nothing rejects it, it is just the default-shaped mistake).

`bundle.resources` carries the iOS Python tree (`resources/mobile/ios/python/**`
→ `<app>/assets/mobile/python/`). That key is global — Tauri v2 has no
per-platform resources for iOS — so a *desktop* bundle built while the iOS
runtime happens to be staged would carry a copy of it; nothing stages it except
`bundle.py ios`, and desktop CI never runs that. The one file that is committed
is `resources/mobile/ios/python/runtime.json`, the "no runtime staged" manifest,
so a build that was never staged answers the wizard honestly instead of failing.
The xcframework and the staged trees are gitignored
(`resources/mobile/.gitignore`).

The iOS `Info.plist` is `src-tauri/Info.plist`, named by `bundle.iOS.infoPlist`
so the merge is a repo decision rather than the CLI's auto-detection. Tauri
merges it into the macOS `.app` too (the same file also carries the App
Transport Security exemption described below). What it states about the app:
`CFBundleDisplayName`/`CFBundleName` "la musica",
`ITSAppUsesNonExemptEncryption` false, `UIBackgroundModes: [audio]`, and the
orientation sets — iPhone portrait + both landscapes, iPad all four.
`CFBundleVersion` is `bundle.iOS.bundleVersion`, deliberately stated:
`CFBundleShortVersionString` is the marketing version (`tauri.conf.json`
`version`, which must match `mlo/__init__.py`), and the build number is what
changes when the *same* version is rebuilt for a re-upload or a re-sideload.

Mobile builds need the native projects, which are **generated, never
committed**: `npx tauri android init` (Android SDK, NDK, JDK 17) and
`npx tauri ios init` (Xcode, CocoaPods) write them into `src-tauri/gen/`. CI
does exactly that, so `.github/workflows/mobile.yml` is the reference for the
toolchain each target needs; a local build needs the same SDK/NDK or Xcode
installed first — and, for iOS, a Mac: `tauri ios build` runs Xcode.

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
  store. It carries **arm64-v8a only** — `tools/mobile/android/inject.py` pins
  `abiList=arm64-v8a` in the generated `gradle.properties`, because that is the
  one ABI the bundled Python exists for; a universal APK would claim four and
  carry a runtime for one, and the other three would fail to link. The file is
  still large (the interpreter, its standard library, the backend's sources and
  the bundled ffmpeg/flac add up), and a release (non-debug) build loses the
  debug symbols on top of that plus ~45 MB of uncompressed native libraries.
  That is a deliberate choice: a release
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
- `.github/workflows/mobile.yml` — Android debug APK and unsigned iOS IPA. Both
  jobs stage the runtime before they build and assert it afterwards:
  - a `wheels` job builds `pydantic-core` for both platforms with cibuildwheel
    (macOS for iOS, Linux for Android) and caches it under
    `desktop/src-tauri/target/mobile-wheels/<platform>/<package>/` keyed on the
    pin, so only the first run pays for it;
  - `bundle.py ios` / `bundle.py android --tools <dir>` stage the runtime (the
    Android job also builds ffmpeg/flac with `build_android_tools.sh`);
  - `bundle.py verify --apk/--ipa` reads the finished artifact back and fails
    the job if the interpreter, the payload or the site-packages tree is not
    inside it.
- `ci.yml` runs `cargo check` for this crate on every push, so the crate graph
  cannot rot unnoticed. Note that `cargo check --target
  aarch64-apple-ios` needs Xcode on the *host* (objc2's build script runs
  `xcrun`), so the iOS half of the shell is only type-checked by the macOS CI
  job.

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
