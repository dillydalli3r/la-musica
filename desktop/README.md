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
  (see the [main README](../README.md#docker-recommended)), another machine on
  the LAN, or a Tailscale name. It never starts, adopts or stops that server.

## Targets

| Target | Build command | Output |
| --- | --- | --- |
| Windows 10/11 (x64) | `npm run build` on Windows | `.msi`, NSIS `.exe` |
| macOS 11+ (Intel/ARM) | `npm run build` on macOS | `.app`, `.dmg` |
| Linux (x64) | `npm run build` on Linux | `.deb`, `.AppImage` |
| Android 7.0+ (API 24) | `npx tauri android build --apk --debug` | `.apk` (debug-signed, installable) |
| iOS 14+ | `npx tauri ios build --target aarch64 --no-sign` | unsigned `.app` → `.ipa` |

All five share one crate. The three desktop builds additionally ship the local
backend: `tools/stage_desktop_bundle.py` stages the frozen PyInstaller tree
(`dist/mlo-server`, which itself carries the built SPA at `_internal/web/dist`
and the `mlo-audio` helper at `_internal/`) into `desktop/bundle/mlo-server`,
and the build maps that tree into the bundle as a Tauri RESOURCE — so the
installer places it at `<resource_dir>/mlo-server`, which is where the shell
spawns it from. `npm run build` in `desktop/` stages and then builds with
`src-tauri/tauri.bundle.conf.json`; CI stages per-OS and passes the same map
inline (see "Bundle config" below for why it is not in a config file). The
mobile builds have no Python and no backend — a phone points at the same server
every other client uses. Everything that only makes sense in a desktop shell —
the tray icon, the autostart registry, the folder picker, hide-on-close, the
local backend — sits behind `#[cfg(desktop)]` in `src/lib.rs`, so the mobile
builds compile without it instead of carrying dead desktop code. Tauri's own
build script defines `desktop`/`mobile`, so the split follows the target.

### Windows install, uninstall and AppData

The NSIS installer is a **per-user** install: the shell and the staged backend
land in `%LOCALAPPDATA%\la musica` (`<install>\mlo-server` is the resource tree
the shell spawns). The shell's own per-user state lives in that same folder —
`config.json`, `shell.json`, `mlo-server.log`, and, until a music folder is
chosen, `server/data/{auth.db,playlists.db}` — because `backend_launcher`
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
  * the boot splash (`web/public/splash.html`) has no navigation of its own, so
    every path that will not end in a running server leaves it explicitly
    (`backend_handle::show_app_page`, by moving the document — the window's
    configured `url` is applied after `setup()` returns, so a `navigate()` from
    there is overwritten), and a failed local start emits `stopped` with no
    origin so the screen can say so;
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
  losing the RYM cookie, playlists or caches. In remote mode the client setup
  wizard (`web/src/pages/ClientSetup.tsx`) probes `${address}/api/health` with
  a 3 s deadline as before; the address is saved per device
  (`localStorage: mlo.server`) and changeable from Settings → Security.
- **One shell per machine**: a second launch — the login auto-start plus a
  click on the icon is the ordinary way to get two — brings the running window
  forward instead of starting anything. Two shells in local mode would mean
  two backends writing the same `<music>/.mlo/data`, two servers on
  8011/8012 and two tray icons (`tauri-plugin-single-instance`, keyed on the
  bundle identifier; desktop only, a phone app gets one process from its OS).
- **Tray icon**: the window lives in the tray while it is closed ("Open la
  musica", "Auto-start on login", "Keep backend running after quit",
  "Backend: running…", "Exit la musica"); closing the window hides it again,
  and Quit ends the shell. Quitting the shell normally stops the local backend
  it spawned — the backend is that shell's child — unless "Keep backend
  running after quit" is checked, which leaves it up for phones and browsers.
  A remote server is never stopped: it belongs to whoever runs it.
- **Native folder picker**: `pick_folder` Tauri command, used by the import
  wizard via `invoke` to pick a source folder. The music folder itself is
  decided at startup (`MLO_MUSIC_FOLDER`, or `music_folder` in
  `config.json`) and stays read-only in Settings — Settings has no picker.
  The setup wizard's folder step calls the shell's `set_music_folder` command
  so the next launch spawns the backend already knowing the library.
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
   playing into the session at that moment. The two teardown notifications (an
   interruption beginning, the media server resetting) also RELEASE the
   keep-alive's player: iOS stopped this process's render when it took the
   session, and a handle to a player the OS has discarded is worse than none —
   `start_keep_alive` refuses to build one while a player is stored, so the app
   would render nothing again for the rest of its life and be suspended the next
   time it was backgrounded. The render is rebuilt where it belongs: an
   interruption ending with `ShouldResume`, the media server's reset, and the
   app becoming active all re-render if the app is backgrounded AND the web
   player still says it is playing. (`keep_alive_platform_stops` in the readout
   counts those teardowns, so "the keep-alive was running and then the music
   stopped" can be read as a number rather than guessed.)
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
   playing session: inaudible under any master, and deliberately not digital
   silence, which a platform may discount as "no audio". The state this whole
   module is in — category, background, playing, keep-alive running or not, and
   why — is readable from inside the app through `ios_audio_state` (Settings →
   Downloads & playback → Playback diagnostics), because none of it is
   observable from a development box: `session_category_taken` and
   `session_activate_last` are the two answers the session's write calls gave
   (there is no public getter for the session's active bit, so "it refused the
   category" and "it refused to activate" are only knowable as the answer we got
   when we asked), `keep_alive_platform_stops` counts the platform teardowns
   above, and `app_heartbeat` says whether `app_process_worst_gap_s` was
   measured at all — a `0.0` without a ticking heartbeat means "nobody looked",
   not "the process was never frozen". It starts at
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
does not have. The wiring is five steps:

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
   turn back on is a star that vanishes mid-album. The star's FILL is replayed
   in the same place, from the web's OWN last answer (`set_liked` remembers it):
   that re-assert lands exactly where the system rebuilds the command set, so
   without the replay a liked track's star came back hollow the next time the
   app was backgrounded and reopened — the app's hearts saying liked and the OS
   drawing otherwise, which is the shape of "the like button does not work". The
   web stays the only writer; what is replayed is its last word, updated in the
   same main-thread breath as the write, so it cannot be stale — and a push
   racing it lands after it and wins.
2. **The star is pressed**: the handler emits the `mlo-ios-like` event to the
   webview — carrying the press's NUMBER — and answers
   `MPRemoteCommandHandlerStatusSuccess`. The shell writes no like itself — and
   because it cannot tell a delivered event from a dropped one, it REMEMBERS the
   press until the web answers with a state push of its own (that is what
   `set_now_playing_liked` means), re-sending it the next time the app is active
   (`ios_like::refresh`). A webview parked behind the lock screen is exactly
   where a Tauri event goes missing, and that is where this star is used. It is
   not always a DEAF webview, though: WKWebView queues the JavaScript it is
   handed while the web content process is suspended and runs it when the
   process wakes, so the original hand-over and the re-delivery can both arrive.
   The number is what makes that safe — `web/src/lib/iosFavs.ts` drops a copy of
   a press it has already handled, and records the drop as a
   `like-press-duplicate` row in the playback report — because two toggles of
   the one like endpoint land exactly where the user started, which is
   indistinguishable from a star that does nothing.
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
5. **It is readable from inside the app**: `ios_like::command_state_rows` adds
   `now_playing_like_enabled` (is the star pressable at this instant),
   `now_playing_like_active` (is it drawn FILLED — MediaPlayer's own `active`
   bit, read through the `isActive` getter its header declares),
   `now_playing_like_web_state` (what the web last pushed) and
   `now_playing_like_press_pending` (a press still owed to a webview that has
   not answered) to the same Settings → Downloads & playback → Playback
   diagnostics list as the audio-session rows. `active` beside `web_state` is
   the mismatch that used to be undiagnosable: "the app thinks the track is
   liked and the OS draws it hollow" is now a row pair, not an argument.

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
than built), with the MediaPlayer side read off Apple's own headers
(`MPRemoteCommandCenter.h`'s `likeCommand`/`dislikeCommand` as `MPFeedbackCommand
*` and `skip*Command` as `MPSkipIntervalCommand *`, and `MPRemoteCommand.h`'s
`@property (nonatomic, assign, getter = isActive) BOOL active;`) — but compiling
for iOS and watching the star on a device both need
Xcode, which is not installed on this machine, so neither has been run locally.
`mobile.yml` (macOS) is the first build that type-checks these files, and the ARTIFACT is checked too: `tools/check_ios_ipa.py <ipa-or-url>` opens a
built `.ipa`, reads `UIBackgroundModes`, all three ATS keys and the
local-network reason back out of its `Info.plist`, and looks for the
Objective-C names the modules use at runtime (`AVAudioSession`, the playback
category and mode, the media-server-reset notification, `AVAudioPlayer`,
`NSTimer`, `NSNotificationCenter`, `MPRemoteCommandCenter`, `sharedCommandCenter`,
`likeCommand`, `dislikeCommand`, `addTargetWithHandler:`, `skipForwardCommand`,
`mlo-ios-like`, and every readout row name) in the app binary — so "the mobile
job went green" and "the IPA the owner installs carries the fix" are two
separate facts, both checked. Each of those assertions is also stated as an
app-level truth in this file, which is the contract `tools/check_ios_ipa.py`
serves: a new iOS-side constant is asserted in the artifact only once it is
written down here as behaviour. `tools/test_sidestore_source.py` builds a
synthetic IPA and deletes each invariant on its own, so an assertion that could
not fail is caught as a failure rather than trusted. A device is still the only
thing that can show the star filling on a press.

**What the shell guarantees, and what the webview does** — the split, in one
place, because every report about iOS playback lands on the seam:

- The SHELL (`ios_audio.rs`) owns the audio session: the `playback` category and
  its activation, the keep-alive render that keeps the app process alive in the
  background, and the notifications that put the session back. It owns the star
  (`ios_like.rs`): the command's existence, its `enabled` and its fill — but
  never the like itself, which it hands to the page and takes back as state.
- The WEBVIEW owns the playback: the `<audio>` element, the Media Session
  metadata the lock screen shows (`title`/artist/album/artwork), and the
  transport handlers for play/pause/previous/next/seek
  (`PlayerBar.tsx`'s `navigator.mediaSession` effect) — plus the single like
  store, and the push that keeps the star's fill in step with it
  (`lib/iosFavs.ts`). A transport button that does nothing while the app is
  backgrounded is a webview/WebKit question, not a shell one; a track that
  refuses to load at all is an ATS question (the media key above).
- NEITHER writes the other's state: the page never calls `AVAudioSession`, and
  the shell never writes a like or a now-playing title.

**Checking it on a device** (the order that answers the most with one build):

1. Settings → Downloads & playback → *Playback diagnostics*, and copy the
   report. `session_category_taken: accepted` + `session_activate_last: accepted`
   while a track plays is the session half; `keep_alive_running: yes` with
   `app_in_background: yes` while the app is behind the lock screen is the
   keep-alive half; `app_process_worst_gap_s` in the seconds with
   `app_heartbeat: ticking` is the app process being frozen anyway (a real
   suspension, not a guess).
2. Play a track, lock the phone, and confirm the music continues and the lock
   screen shows the track's own metadata. Then press the star there: the report
   gains a `like-press` row (`iosFavs`), the heart in the app is filled when the
   app comes back, and the rows read `now_playing_like_active: yes` with
   `now_playing_like_web_state: liked`. A `like-press-duplicate` row with no
   second toggle is the re-delivery path working as designed.
3. Take a phone call (or trigger Siri) with the music playing in the
   background, then hang up: the music should still be there and
   `keep_alive_platform_stops` should have risen by one — that count rising is
   the tear-down/re-render pair above doing its job, and a `keep_alive_running:
   no` beside it, while backgrounded and playing, is the transition that never
   came.
4. Leave the app backgrounded for a few minutes and reopen it: `web_player_says_
   playing`, `keep_alive_running` and the star's fill should all still agree. A
   hollow star with `now_playing_like_web_state: liked` is the OS's own rebuild
   winning — the replay in step 1 of the star section above is what should
   prevent it, and a report showing it is how that gets a next fix rather than a
   next release.

## Bundle config

`bundle.iOS.minimumSystemVersion` 14.0, `bundle.iOS.bundleVersion` 5.3.0,
`bundle.iOS.infoPlist` and `bundle.android.minSdkVersion` 24 in
`tauri.conf.json`. The Android package name and the iOS bundle id both come from
the top-level `identifier` (`com.musiclibraryoptimizer.lamusica` — the old
`com.musiclibraryoptimizer.app` is the shape tauri-cli warns about on every
build, because an identifier ending in `.app` reads as the bundle extension;
nothing rejects it, it is just the default-shaped mistake).

The desktop bundles carry **one resource** — the staged backend tree, mapped by
`bundle.resources` to `<resource_dir>/mlo-server`, which is where `backend.rs`
looks for the process it spawns. It is declared where the staging happens, not
in a config file: `desktop/package.json`'s `build` passes
`--config src-tauri/tauri.bundle.conf.json`, and CI passes the same map inline.
Two reasons. `tauri-build` validates resource paths on every compile, so a
resource in `tauri.conf.json` would make a plain `cargo check` fail in a
checkout that never staged a 300 MB backend ("resource path ... doesn't
exist") — which is every fresh clone and the `ci/desktop` leg. And
`npx tauri android build` / `tauri ios build` read `tauri.conf.json` too: a
phone has neither a backend to ship nor a checkout that built one, so a
resource there would fail or bloat every mobile build.

The mobile bundle carries no resources and no native frameworks: this is the
webview and the React app, nothing else. A phone with no reachable server shows
the wizard's address screen, and once it has one it is the same client the
desktop build is.

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
  `NSAppTransportSecurity > NSAllowsArbitraryLoadsInWebContent` **and**
  `NSAllowsArbitraryLoadsForMedia`, plus the blanket `NSAllowsArbitraryLoads`.
  Tauri merges that file into the generated
  iOS `Info.plist` at `tauri ios build` time — `bundle.iOS.infoPlist` names it,
  and it is the last plist merged, so what it says wins — and into the macOS
  `.app`; without those keys App Transport Security blocks every `http://` and
  `ws://` request the webview makes — fetch, WebSocket, audio and
  video playback alike. **Two scoped keys, not one, and the reason is a rule of
  Apple's that reads backwards:** on iOS 10+ and macOS 10.12+ the blanket
  `NSAllowsArbitraryLoads` is *ignored* — treated as NO — the moment any scoped
  key is present ("In iOS 10 and later and in macOS 10.12 and later, the value of
  the `NSAllowsArbitraryLoads` key is ignored—and the default value of NO used
  instead—if any of the following keys are present:
  `NSAllowsArbitraryLoadsForMedia`, `NSAllowsArbitraryLoadsInWebContent`,
  `NSAllowsLocalNetworking`"). The web-content key covers what the page's own
  process fetches (the document, its scripts, its XHRs); an
  `<audio>`/`<video>` element's bytes are loaded by AVFoundation, in a process of
  its own, and read the MEDIA key instead. A build with the web-content key
  alone therefore loaded the whole app and then had every single track refused
  by ATS with nothing the page could see — "pressing play just pauses it
  immediately" — which is the shape of the owner's report this file's media key
  answers. The blanket key is kept as the coarse exemption for a system that
  reads no scoped key at all (iOS 9 / macOS 10.11); on every OS this app can run
  on it is inert, and it is the two scoped keys that decide. The same file carries
  `NSLocalNetworkUsageDescription` — iOS 14+ asks before an app may talk to
  devices on the local network and cannot even prompt without a reason to show,
  so a server on the LAN would be unreachable while a Tailscale address worked.
  The shell's own native calls are still held to full ATS (there are none:
  `lib.rs` carries no HTTP client). The mobile CI job reads the plist keys back
  out of the built `.app` (the web-content and blanket ATS keys, and the
  local-network reason), and `tools/check_ios_ipa.py` reads all four back out of
  the shipped IPA — the MEDIA key included, which is the one a release most
  needs: its absence changes nothing a build log or a launch would show, and
  every track is refused behind it.
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
checkout) is the other mode — Settings → Security, and the same client the
mobile builds are; the wizard asks for the address when a shell has no backend
of its own.

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
