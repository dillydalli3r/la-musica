# la musica 5.4.0 - Windows draws the app's own media card

Windows 11's media flyout — the card in Quick Settings, the one a media key
raises, the one a headset's buttons talk to — used to label whatever la musica
was playing **"Unknown app"**. It says **la musica** now, with the app's own
icon, and it draws the track, the album art and a progress bar with it. The
buttons work, a media key on the keyboard reaches the player again, and the
queue's ⏮/⏭ appear when there is somewhere to step.

None of that was a label change: the session Windows was drawing belonged to
WebView2.

## Why it said "Unknown app", and why the shell has to publish the session

Windows resolves a media session's app name from its `SourceAppUserModelId`.
The session the webview publishes for the page's Media Session is created on a
hidden window of the **runtime's own** process, so Windows answers
`msedgewebview2.exe` — measured on the running app, with this app's AUMID
already set on the process and stamped on the installer's shortcuts by 5.2.0.
Windows resolves that id to no app at all and falls back to "Unknown app".

It is not the page's to fix, and not a switch away: no Chromium argument sets an
AUMID (there is no `--app-user-model-id`), the webview's process is not ours to
name, and the upstream request has been open and unworked since 2022
(MicrosoftEdge/WebView2Feedback#2236). That is exactly what
`release-notes-5.2.0.md` recorded when this report was closed as unfixable from
where it stood — it named owning the controls natively as the real fix, and
that is what this release does for the half that can be owned without moving
playback out of the webview.

The shell opens the session itself, the way Chromium opens one for its own
window: `ISystemMediaTransportControlsInterop::GetForWindow` on the app's main
window, from `lib.rs`'s setup hook (`desktop/src-tauri/src/win_media.rs`).
Windows then resolves the card through **this** process —
`com.musiclibraryoptimizer.lamusica`, which `main.rs` sets on the process and
which the installer's Start Menu shortcut already carried — and `main.rs` also
writes that id into the per-user AUMID registry key
(`HKCU\Software\Classes\AppUserModelId\…`) with the name and the app's icon, so
an unpackaged or dev build resolves its own name too.

The webview's own session is switched **off** in `tauri.conf.json`
(`additionalBrowserArgs`, `HardwareMediaKeyHandling`): two live sessions would
draw two cards for one song, one of them still anonymous. The switch was checked
both ways on a real Chromium before it was trusted — with it, a page still plays
and its OS card is gone.

## What the shell carries for the card

Metadata and transport used to be free, because the browser derived both from
the page's Media Session. Owning the session means carrying both across the
bridge, in the shape the iOS bridges already have:

* **metadata** — title, artist and album, plus the artwork **URL** the bar is
  already drawing. The URL, not bytes: the OS fetches the cover itself, the way
  it already did for `MediaMetadata.artwork`, so a track change costs one IPC
  message and no image; on loopback the server's login gate never asks a request
  from this machine to sign in.
* **state** — playing or paused, the position, the duration and the rate the
  card's progress bar is drawn from, and whether the queue can step either way
  (that is what enables ⏮/⏭). The position is bucketed to five seconds: Windows
  extrapolates from a position and a rate, so a per-second message would buy a
  bar that reads the same.
* **presses** — a button in the flyout, a media key or a headset arrives in the
  shell and leaves it as the `mlo-media-key` event. The page runs it through the
  **same** handlers it registers with `navigator.mediaSession` — one handler
  set, two doors — so a press cannot mean one thing on the OS surface and
  another in the lock screen. A scrub on the card's own progress bar seeks the
  playing element through the same `seekto` handler a car stereo uses.

Nothing else changes: macOS and Linux still draw the webview's own session, the
iOS audio session and its Now Playing star are untouched, and the browser build
never reaches any of this (the bridge is inert outside the Tauri shell).

## Smaller

* `AGENTS.md` gains the house rule this came from: the OS media card is the
  shell's, the webview's own session stays off, and both plus the bridge between
  them are one mechanism.
* `desktop/README.md` documents the card under "How it works (desktop)", next to
  the window and the backend bullets.
* The shell's new `set_now_playing` command is registered on every target and
  inert off Windows, exactly like the iOS commands, so the page calls it
  unconditionally; its permission is in both capability files and in
  `build.rs`'s command manifest.

## What proved it

* **the name, on screen**: a probe session published on the app's own id, and
  then the real app playing a scratch library, both read back from Windows'
  session list as `com.musiclibraryoptimizer.lamusica` — and a capture of Quick
  Settings showing the card as **la musica** with the album art and ⏮ ⏸ ⏭.
* **the resolution itself**, rather than the string: `shell:AppsFolder\com.musiclibraryoptimizer.lamusica`
  answers "la musica" while `shell:AppsFolder\msedgewebview2.exe` does not exist.
* **one card, not two**: with the app playing a scratch track through a debug
  shell, Windows listed exactly one session for it — our id, "Chop Suey!",
  "System of a Down", Playing — and no `msedgewebview2.exe` session beside it.
* **the buttons drive the app**: `TryTogglePlayPauseAsync` on that card (the
  call the flyout's button makes) returned true and the app's audio element
  went from playing at 0:09 to paused; a media key pressed on the keyboard
  reached the session's own button handler.
* **the flag, both ways**: `HardwareMediaKeyHandling` disabled → a Chromium
  page still plays and its session is gone; enabled → the session is back.
* **the suites**: `tools/check_player_state.cjs` 36/36,
  `tools/check_replaygain_player.cjs` 17/17, `tools/check_fullscreen_player.cjs`
  64/66 (the two failures are the phone-viewport passes and fail identically
  with this change stashed — pre-existing on this machine), plus
  `cargo check --all-targets`, `npx tsc -b`, `npx oxlint`, `npm run build` and
  `python tools/check_versions.py v5.4.0` (all 12 copies agree).

Not verified here: macOS and Linux, whose now-playing UI this release
deliberately leaves alone, and the card on a **packaged** Windows install — the
run above used a debug build of this tree, and the installer only adds the Start
Menu shortcut that already carried the id.