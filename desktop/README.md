# la musica — Desktop shell

Tauri v2 (Rust) client for the React UI: the same app the browser gets, pointed
at a la musica server you run yourself — the Docker container (see the [main
README](../README.md#docker-recommended)), on this machine, on the LAN or
behind a Tailscale name.

## Targets

| Target | Build command | Output |
| --- | --- | --- |
| Windows 10/11 (x64) | `npm run build` on Windows | `.msi`, NSIS `.exe` |
| macOS 11+ (Intel/ARM) | `npm run build` on macOS | `.app`, `.dmg` |
| Linux (x64) | `npm run build` on Linux | `.deb`, `.AppImage` |
| Android 7.0+ (API 24) | `npx tauri android build --apk --debug` | `.apk` (debug-signed, installable) |
| iOS 14+ | `npx tauri ios build --target aarch64 --no-sign` | unsigned `.app` → `.ipa` |

All five share one crate, and all five are clients: none of them starts, adopts
or stops a backend. Everything that only makes sense in a desktop shell — the
tray icon, the autostart registry, the folder picker, hide-on-close — sits
behind `#[cfg(desktop)]` in `src/lib.rs`, so the mobile builds compile without
it instead of carrying dead desktop code. Tauri's own build script defines
`desktop`/`mobile`, so the split follows the target.

## How it works (desktop)

- **Window** shows the built React app (`../web/dist`, built by
  `beforeBuildCommand`), and opens *visible*: its first run is the setup
  wizard's server-address screen, which is no use behind a tray icon nobody has
  been told about.
- **Server**: the address this client was set up with, and nothing else. The
  shell carries no backend, no Python and no port of its own. The wizard
  (`web/src/pages/ClientSetup.tsx`: server address → sign-in → notifications)
  probes `${address}/api/health` with a 3 s deadline and refuses to continue
  until a real la musica server answered, so "start your container" is a
  message in the wizard instead of an app full of failed requests; the address
  is saved per device (`localStorage: mlo.server`) and changeable later from
  Settings → Security. A configured client whose server stops answering lands on
  the sign-in screen with that address field.
- **Tray icon**: the window lives in the tray while it is closed ("Open la
  musica", "Auto-start on login", "Exit la musica"); closing the window hides
  it again, and Quit ends the shell. Nothing else happens on the way out — the
  server belongs to whoever runs its container, so quitting a client never
  stops anyone's server, and this shell has no child process to kill.
- **Native folder picker**: `pick_folder` Tauri command, used by the import
  wizard via `invoke` to pick a source folder. The music folder itself is
  decided at startup (`MLO_MUSIC_FOLDER`, or `music_folder` in
  `config.json`) and is read-only in Settings — Settings has no picker.
- **Notifications**: `tauri-plugin-notification`, registered on every target,
  used by the web UI for "wish found", "download done" and "import ready".

## How it works (mobile)

Android and iOS build the same client from the same crate. The OS owns the
window and there is no tray, so the shell shows it once at startup and never
touches it again; and it carries **no Python and no backend** — a phone points
at the same server every other client does, which is the only way it can have a
library at all (the wizard asks for the address on first run).

What a client can offer is the *server's* answer, not the app's:
`GET /api/capabilities` derives it from what the server can actually run (is a
subprocess possible, is ffmpeg/flac/fpcalc/rsgain present), so a container with
a full toolchain and one on a NAS report different lists. The Dependencies page
and the setup wizards read that report and say a feature is unavailable with
the reason, instead of offering an Install button that cannot succeed.

- **Notifications work the same** while the app is open — and the socket is why
  the desktop tray exists at all: a hidden window keeps its connection alive, so
  it is still told when a wish lands or an import finishes. What the shells do
  **not** have is remote push: a Tauri webview registers no service worker, so a
  CLOSED shell cannot be woken. Push belongs to the browser and to the installed
  PWA — on iPhone and iPad that means la musica on the Home Screen, iOS 16.4 or
  newer — and Settings → Notifications says exactly that per client instead of
  offering a switch that could not work (`spec R204`).
- **Capabilities** are split by platform: `capabilities/default.json` is
  desktop-only (folder picker + notifications), `capabilities/mobile.json`
  gives Android/iOS the core commands and notifications but no dialog
  permission, since there is no folder to pick. The mobile shell's one app
  command — `set_now_playing_liked`, the iOS star's state (see below) — needs no
  entry in either file, and neither does the desktop shell's `pick_folder`: Tauri
  checks the ACL for *plugin* commands, and for the app's own commands only when
  the crate ships an ACL manifest (`tauri-build` writes one from a
  `permissions/` tree, which this crate has never had). `core:default` is what
  the web side actually needs from the shell — including `event:listen`, which
  is how the star's press reaches the web UI.
- **Background audio** stays declared (`UIBackgroundModes: [audio]` in
  `Info.plist`): music playing with the screen locked is what a music client is
  for. `mobile.yml` now reads that key — and the ATS exemption below — back out
  of the built `.app`, so a build that lost either one fails instead of shipping
  an app that goes quiet when backgrounded. It used to carry a second job —
  keeping an embedded backend alive with a silent session — and that job, with
  its sideload-only caveat, went away with the backend.
  The mode is the PERMISSION, not the fact: until `src/ios_audio.rs` existed the
  app never configured an `AVAudioSession` category, and the default
  (`soloAmbient`) is muted the moment the app stops being frontmost — the
  owner's own 4.0.0 report, "audio is muted when app is unfocused". That module
  puts the session in `AVAudioSessionCategoryPlayback` (default mode, no
  options) and owns its whole lifecycle — see the next section; with the plist
  key it is what makes the mode true.

### The audio session on iOS

The star above needs a now-playing session to be drawn on, and the audio itself
needs the session to keep playing once the app is not in front — one module,
`src-tauri/src/ios_audio.rs`, owns both, and it is compiled for **iOS only** for
the same reason the star is (Android plays through its own audio path; the
desktop targets have no `AVAudioSession`).

1. **Setup** (`ios_audio::configure`, from the mobile shell's setup): put the
   session in `AVAudioSessionCategoryPlayback` with the default mode and no
   options — the framework's own exported constant, not a copied string, and
   AVFAudio is linked explicitly because a framework that is not loaded has no
   classes to look up. The category is set here, while the session is inactive —
   the one moment a category change is free — and the session is deliberately
   NOT activated here, because Apple's guidance is to activate when playback
   begins ("to ensure that you won't prematurely interrupt any other background
   audio") — an activation at launch does exactly that to whatever the user was
   listening to.
2. **Play** (`set_playback_active`, driven by `web/src/lib/iosAudio.ts` from the
   player's own `playing` state): playback activates the session, and that is
   ALL that happens. Activating an already-active session is a no-op, so a start
   can never interrupt a start — which is the 4.0.4 correction. 4.0.3 also
   re-applied the category and deactivated the session on every stop, and both
   halves broke playback on a real phone: `setCategory:` on an ACTIVE session is
   Apple's documented "may interrupt audio playback" (and the web player writes
   `playing` before the element has started, so the call landed exactly there),
   while a pause arriving in the same second as a start handed the session back
   mid-startup. The owner's report was the shape of it — "pressing play on
   tracks just makes them pause immediately".
3. **The transitions** (`ios_audio::register`): the OS notifications re-assert
   the session where iOS takes it from a backgrounded app — going to the
   background and becoming active again (both only while playing, and both a
   no-op when the session is already up), `AVAudioSessionInterruption` ending
   with `ShouldResume` (without that option the session belongs to whatever took
   it, and the next press of play is what takes it back), and
   `AVAudioSessionMediaServicesWereReset` — the ONE transition besides setup
   that re-takes the category, because the audio server restarted and nothing is
   playing into the session at that moment.
4. **The backgrounded webview** (`tauri.conf.json`): the category and
   `UIBackgroundModes: [audio]` are the app's half of the promise; the web
   content process that decodes the audio is WebKit's, and WebKit stops a page
   it can no longer justify keeping. The window therefore carries
   `"backgroundThrottling": "disabled"`, which wry maps onto WebKit's
   `WKPreferences.inactiveSchedulingPolicy = .none` (public API, iOS 17+ /
   macOS 14+) — long-running audio in a backgrounded hybrid app is the case that
   setting exists for. Older systems keep WebKit's default.
5. **The keep-alive** (`ios_audio::sync_keep_alive`, the owner's 4.1.0 report —
   *"audio just cuts out after tabbing out of the app"*, with the OS star
   behaving as if unwired): the background-audio mode is a grant for the APP,
   and what iOS keeps running is a process that is PRODUCING audio. This app's
   audio is decoded by WebKit's web content process, so this one configures a
   session and then renders silence into it — and a backgrounded app with no
   playback of its own is suspended like any other, taking the music, the star's
   command handler and the star's press handling with it. While the web player
   says it is playing AND the app is in the background, this process therefore
   plays half a second of generated 16-bit dither (`keepalive_wav`, ±1 LSB,
   about −90 dBFS) on a looping `AVAudioPlayer` at unity volume, on that same
   playback session: inaudible under any master, and deliberately not digital
   silence, which a platform may discount as "no audio". The state this whole
   module is in — category, background, playing, keep-alive running or not, and
   why — is readable from inside the app through `ios_audio_state` (Settings →
   Downloads & playback → Playback diagnostics), because none of it is
   observable from a development box. It starts at
   `DidEnterBackground` and when playback starts while already backgrounded
   (the lock-screen play button has no app-state notification to ride on), and
   stops the moment either half goes away. `MPNowPlayingInfoCenter` is still
   left alone — the webview's Media Session remains the only writer of what the
   lock screen shows.

### The Now Playing star on iOS

iOS draws the Now Playing module (Control Center, the lock screen, CarPlay) from
whatever owns the audio session, and the star in it is MediaPlayer's
`MPRemoteCommandCenter.likeCommand`. This app's playback is HTML5 `<audio>` in
the webview, whose Media Session API covers play/pause/previous/next/seek and
nothing else — there is no like action to register there — so the star is the
one part of "mark this track as a favourite" that must be native.
`src-tauri/src/ios_like.rs` is that part, and it is compiled for **iOS only**:
the split elsewhere in this crate is Tauri's own `desktop`/`mobile`, but Android
drives its media notification from the webview's Media Session alone, and a
`cfg(mobile)` star would have dragged Android into a MediaPlayer framework it
does not have. The wiring is four steps:

1. **Setup** (`ios_like::register`, from the mobile shell's setup): look
   `MPRemoteCommandCenter` up dynamically, enable `likeCommand` (the star has to
   be pressable *before* anything has been said about the track, or the first
   press — the one that likes an unliked track — has nothing to hit), pin
   `active` to NO (nothing is liked yet), pin `dislikeCommand` inactive (this app
   has no dislike concept) and attach one handler with
   `addTargetWithHandler:`. MediaPlayer is linked explicitly, because a framework
   that is not loaded has no classes to look up. Its `enabled` bit is re-asserted
   on every state push, whenever playback begins (`set_playing` — the moment
   WebKit publishes its OWN remote-command set from the web content process,
   which is transport-only), and again whenever the app becomes active
   (`ios_like::refresh`, called by the audio-session module): the same bit is
   written by the system's now-playing plumbing, and a star a state push cannot
   turn back on is a star that vanishes mid-album.
2. **The star is pressed**: the handler emits the `mlo-ios-like` event to the
   webview and answers `MPRemoteCommandHandlerStatusSuccess`. The shell writes
   no like itself — and because it cannot tell a delivered event from a dropped
   one, it REMEMBERS the press until the web answers with a state push of its
   own (that is what `set_now_playing_liked` means), re-sending it the next time
   the app is active (`ios_like::refresh`). A webview parked behind the lock
   screen is exactly where a Tauri event goes missing, and that is where this
   star is used.
3. **The web toggles**: `web/src/lib/iosFavs.ts`, mounted by the player bar,
   listens for that event and calls the app's one like writer —
   `useFav`/`api.likeToggle`, i.e. the same optimistic update, the same query
   invalidation and the same query keys every heart in the UI uses, so all of
   them flip together. Outside the Tauri shell the module is inert
   (`IN_TAURI` gates both wires), and inside it every failure — no such event,
   no such command in an older shell — is swallowed: a favourite must never
   break playback.
4. **The state goes back**: the same module calls the `set_now_playing_liked`
   command whenever the current track or its liked state changes, which on iOS
   sets `MPFeedbackCommand.active` — the OS's "the user already likes this item"
   (`MPFeedbackCommand.h`), i.e. a **filled** star when the track is favourited
   and a hollow one when it is not. The command is registered on every target
   with an empty body off iOS, so the web UI calls it unconditionally.

What the star cannot do: the app has no dislike or bookmark, so only the like
command is ever activated; and the star belongs to the OS's module, so it
appears only while a now-playing session exists at all — i.e. while this
webview owns one (playing, or paused mid-track) — and a session is a *playback*
session only because `src/ios_audio.rs` says so: before that module the app had
the plist's background mode but no category, so the module had no card to be
drawn on (the owner's "the like button still isn't on ios" was the same bug as
the muted audio — spec R265). Hand the session to another app and the module,
star included, goes with it. `MPNowPlayingInfoCenter` is left untouched because
the webview's own Media Session metadata is what the OS reads for
title/artist/album/artwork; `localizedTitle`/`localizedShortTitle` are left
untouched so the OS's already-localised wording is used instead of an English
string hard-coded in the shell.

What is verified here and what is not: `cargo check` proves the crate still
builds for the non-iOS targets with both modules excluded, and the objc2/block2
API in them was written against those crates' vendored sources (the
`&DynBlock<dyn Fn(…) -> _>` argument shape, `Option<Retained<_>>` returns,
`msg_send!`'s encoding rules — `None::<&mut AnyObject>` is how an out-parameter
like `NSError**` is passed, since raw pointers are not `Encode`, which is also
why the audio category is read from AVFAudio's exported `NSString *const` rather
than built) — but compiling for iOS and watching the star on a device both need
Xcode, which is not installed on this machine, so neither has been run locally.
`mobile.yml` (macOS) is the first build that type-checks these files, and the ARTIFACT is checked too: `tools/check_ios_ipa.py <ipa-or-url>` opens a
built `.ipa`, reads `UIBackgroundModes` and the ATS key back out of its
`Info.plist`, and looks for the Objective-C names the modules use at runtime
(`AVAudioSession`, the playback category and mode, `NSNotificationCenter`,
`MPRemoteCommandCenter`, `mlo-ios-like`) in the app binary — so "the mobile job
went green" and "the IPA the owner installs carries the fix" are two separate
facts, both checked. A device is still the only thing that can show the star
filling on a press.

## Bundle config

`bundle.iOS.minimumSystemVersion` 14.0, `bundle.iOS.bundleVersion` 4.1.4,
`bundle.iOS.infoPlist` and `bundle.android.minSdkVersion` 24 in
`tauri.conf.json`. The Android package name and the iOS bundle id both come from
the top-level `identifier` (`com.musiclibraryoptimizer.lamusica` — the old
`com.musiclibraryoptimizer.app` is the shape tauri-cli warns about on every
build, because an identifier ending in `.app` reads as the bundle extension;
nothing rejects it, it is just the default-shaped mistake).

The bundle carries **no resources and no native frameworks** any more:
`bundle.resources` and `bundle.iOS.frameworks` named the staged Python tree and
its `Python.xcframework`, and both went away with the embedded backend. A
mobile build is the webview and the React app, nothing else — a phone with no
reachable server shows the wizard's address screen, and once it has one it is
the same client the desktop build is.

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
LAN IP, a Tailscale/MagicDNS name, or `127.0.0.1:8000` for the Docker container
running on the same machine. The server ships no certificate and offers no TLS,
so both Apple platforms have to be told to allow cleartext, or the app cannot
reach *any* server:

- **iOS and macOS** — `src-tauri/Info.plist` sets
  `NSAppTransportSecurity > NSAllowsArbitraryLoadsInWebContent` **and** the
  blanket `NSAllowsArbitraryLoads`. Tauri merges that file into the generated
  iOS `Info.plist` at `tauri ios build` time — `bundle.iOS.infoPlist` names it,
  and it is the last plist merged, so what it says wins — and into the macOS
  `.app`; without it App Transport Security blocks every `http://` and `ws://`
  request the webview makes — fetch, WebSocket, audio and
  video playback alike. The web-content key alone is not enough for the MEDIA:
  an `<audio>`/`<video>` element's bytes are loaded by WebKit's media stack,
  which reads the blanket key, so a build with only the first could open the
  whole app and then fail every single track ("pressing play just pauses it
  immediately"). The same file carries
  `NSLocalNetworkUsageDescription` — iOS 14+ asks before an app may talk to
  devices on the local network and cannot even prompt without a reason to show,
  so a server on the LAN would be unreachable while a Tailscale address worked.
  The shell's own native calls are still held to full ATS (there are none:
  `lib.rs` carries no HTTP client). The mobile CI job reads all three keys back
  out of the built `.app`, and `tools/check_ios_ipa.py` out of the shipped IPA.
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
  store. It carries **arm64-v8a only** — the workflow pins `abiList`/`targetList`
  to that ABI in the generated `gradle.properties`, and builds with
  `--target aarch64`, so the project and the build agree on one slice. A
  universal client APK would claim four and be four times the download for
  phones nobody is installing on. That is a deliberate choice: a release
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
cd web && npm install && npm run build   # web/dist, the Tauri frontendDist
cd ../desktop
npm install
npm run dev        # vite dev UI + tauri window
npm run build      # release bundle (NSIS/msi on Windows)
```

Nothing starts a backend: run the server yourself (`docker compose up -d` in
the repo root, or `python -m uvicorn server.main:app --host 127.0.0.1 --port
8000` for a source checkout) and point the window at it — the wizard asks for
the address on first run, and Settings → Security changes it afterwards. The
web UI detects the Tauri webview (`window.__TAURI_INTERNALS__`) only to know it
is a client that must be told where the server is; it never assumes
`127.0.0.1:8000`.

## CI

- `.github/workflows/desktop.yml` — Windows/macOS/Linux bundles, uploaded per
  platform.
- `.github/workflows/mobile.yml` — Android debug APK and unsigned iOS IPA. Both
  jobs generate the native project, install the app icons, and build; neither
  stages a runtime or a CLI toolchain into the app any more, because the app is
  a client (`grep` the workflow for `bundle.py` and you will find nothing).
- `ci.yml` runs `cargo check` for this crate on every push, so the crate graph
  cannot rot unnoticed. Note that `cargo check --target
  aarch64-apple-ios` needs Xcode on the *host* (objc2's build script runs
  `xcrun`), so the iOS half of the shell is only type-checked by the macOS CI
  job.

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
