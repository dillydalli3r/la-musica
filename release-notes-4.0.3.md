# la musica 4.0.3 — the phone keeps its voice

4.0.3 is the finish on the two iOS reports 4.0.2 was meant to close, plus the
correction that makes synced lyrics line up with what you actually hear.

## "Audio still stops when I tab out" — the other half of the session

4.0.2 put the app's audio in the playback category and activated the session at
launch. The report came back sharper than before: *"audio still stops playing
from the app when it tabs out… if I pause / play again the audio works, then
doesn't work after entering and exiting the app again."*

That pause/play detail is the diagnosis. The audio is decoded by WebKit's web
content process, not by the app's own process, and a page that is merely playing
audio is a case iOS has to be told about on every transition — WebKit stops a
page it can no longer justify keeping alive. A fresh `play()` in the foreground
is what re-armed it, which is why the sound came back exactly then and died
again on the next trip out.

Two things fix it, and both are in this build:

* **The window disables WebKit's background throttling**
  (`"backgroundThrottling": "disabled"` in `desktop/src-tauri/tauri.conf.json`).
  Tauri maps that onto WebKit's `WKPreferences.inactiveSchedulingPolicy = .none`
  (public API, iOS 17+ / macOS 14+), the setting that exists for exactly this
  case: long-running audio in a backgrounded web app. Every WKWebView host in
  the field ends up here — the Cordova background-audio threads solve it the
  same way — and on the desktop it means the app's audio also survives a hidden
  window.
* **The session has a lifecycle now, not one launch-time activation**
  (`desktop/src-tauri/src/ios_audio.rs`). The category is still set at setup,
  but playback is what activates the session, a stop hands it back with
  `NotifyOthersOnDeactivation` (so whatever the app interrupted resumes), and
  four OS notifications re-assert it wherever iOS takes a backgrounded app's
  session away: going to the background and becoming active again, an
  interruption ending with `ShouldResume` — when it ends *without* that option
  the session belongs to whatever took it, and the app lets it go rather than
  fighting for it — and a restart of the audio server.

Apple's own reason for activating on playback instead of at launch is part of
the same change: a non-mixable playback session activated the moment an app
starts interrupts whatever you were listening to before you asked this app to
play anything.

## The star, kept pressable

The star is `MPRemoteCommandCenter.likeCommand`, and its `enabled` bit is shared
with the system's own now-playing plumbing — the same bit WebKit rewrites when
it refreshes the command set for the webview's now-playing card. A star that a
state push cannot turn back on is a star that vanishes mid-album, so the shell
now re-asserts `enabled` on every state push (`set_liked`) and again whenever
the app becomes active, or the audio server restarts (`ios_like::refresh`,
called by the session module). The fill state was already the right call —
`MPFeedbackCommand.active` is read/write, which is what MediaPlayer's own header
says, so the app's like lands on the OS star as a filled star — and pressing it
still runs the app's *one* like writer (`useFav`, the same one every heart in
the UI uses), with the state pushed back afterwards.

## Synced lyrics follow the sound, not the decoder

Playback is routed through the WebAudio graph (ReplayGain → equalizer →
visualizer → speakers), and that graph has a real output delay: the element's
`currentTime` says where the *decoder* is, while the buffer the speakers are
playing was handed to the device `baseLatency + outputLatency` ago. A lyric pane
driven straight off `currentTime` therefore runs ahead of what you hear — the
"audio in general is de-synced from what the app displays for synced lyrics"
report — so the one clock both lyric surfaces read now subtracts that: zero for
an element with no graph, capped at half a second so a nonsense reading cannot
throw a pane a verse off. The per-track offset control is unchanged on top of
it, and `tools/check_lyric_clock.mjs` (7 checks, run by CI's web job) pins the
arithmetic case by case.

## The fullscreen player's phone layouts

The fullscreen player now decides its layout from what the track can actually
show (`npLyricsMode`): synced lyrics, plain lyrics, a plain text this install
refuses (which gets the red-cross "Plain" mark in the metadata row instead of an
empty pane), an instrumental — even when lyrics are stored — and no lyrics at
all. The pane and its toggle are drawn only where the pane can be filled, so no
state of the control promises something that cannot appear, and a track with
nothing drawable gets the phone's full composition back: cover, title, album ·
artist, rating, transport, seek and the visualizer, centred in the viewport. The
transport row is one line at every width: the queue button used to wrap onto a
row of its own at phone widths, and the favourite heart now stands on that row —
where the owner went looking for it — instead of alone in the bottom-left corner.

## The build itself is checked, not just the build log

The iOS modules cannot be compiled on a Windows or Linux desk at all (the target
needs Apple's SDK), so "the mobile job went green" and "the IPA the owner
installs carries the fix" used to be the same sentence. They are two now:

* `tools/check_ios_ipa.py <ipa-or-url>` opens a **built** `.ipa`, reads
  `UIBackgroundModes` and the ATS exemption back out of its `Info.plist` (the
  merge in `tauri-build` is what decides them, not the source), and looks for
  the Objective-C names the modules use at runtime — the audio session class,
  the playback category and mode, `NSNotificationCenter` for the session's
  lifecycle observers, `MPRemoteCommandCenter` for the star, and `mlo-ios-like`,
  the event a star press is handed to the webview with. On 4.0.2 it fails on
  `NSNotificationCenter`, exactly as it should; on this release's build it
  passes.
* `tools/check_lyric_clock.mjs` runs in CI beside the other browser-side checks.

Spec `docs/OPTIMIZATION-GRADING-SPEC.md`: R265 rewritten for the session's
lifecycle, R265a added for the background-throttling statement, R251 extended
with the star's re-assert, and R266 added for the audible lyric clock.
