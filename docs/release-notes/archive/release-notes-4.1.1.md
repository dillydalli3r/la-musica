# la musica 4.1.1 — the phone keeps playing, and the buckets agree with the vocab

4.1.1 is the five reports from a session with 4.1.0, each traced back to the line
that caused it: the iOS app had no audio of its own to be kept alive for, the
Genres page filed a genre by how its name is spelled, the Dependencies header
never scrolled away, and a working `yt-dlp` install could be reported — and
deleted — as a failure.

## iOS: the app process was producing no audio at all

"Audio just cuts out after tabbing out of the app" survived 4.0.3's session
lifecycle and 4.1.0's scheduling policy, because both were about the wrong
process. iOS's audio background mode is a grant to the **app**, and what the
runtime keeps running is a process that is *playing something* — while the music
is decoded by WebKit's **web content process**. The shell configured a playback
session and then rendered silence into it, so at the one moment iOS asks "is
this app playing?", the only truthful answer from that process was no, and a
backgrounded app with no playback of its own is suspended like any other. That
is also what made the OS star look unwired: a suspended app runs no
remote-command handler (R288).

* **The keep-alive.** While the web player says it is playing AND the app is in
  the background, `desktop/src-tauri/src/ios_audio.rs` renders half a second of
  generated 16-bit silence (`silence_wav`) through a looping `AVAudioPlayer` at
  unity volume, on the same playback session — inaudible by construction, real
  output as far as the session is concerned. It starts at `DidEnterBackground`
  and when playback starts while already backgrounded (the lock-screen play
  button has no app-state notification to ride on), and stops the moment either
  half goes away: nothing renders in the foreground, so the cost is bounded to
  the time the app is out of sight and the user is listening.
  `MPNowPlayingInfoCenter` is still untouched — the webview's Media Session
  remains the only source of what the lock screen shows.
* **The graph iOS parks mid-track comes back.** A suspended/interrupted
  `AudioContext` used to be resumed only while the page was *visible*, which
  refused to undo exactly the suspension a backgrounded, still-playing track
  suffers. `web/src/lib/analyser.ts` now also resumes it while the page is
  hidden if one of the app's own elements is really playing — and only then.
* **The star is re-armed where it can be lost.** WebKit publishes its own remote
  command set from the content process
  (`RemoteCommandListenerCocoa::updateSupportedCommands` →
  `MRMediaRemoteSetSupportedCommands`, transport commands only), so
  `likeCommand`'s `enabled` bit is now re-asserted on playback start as well as
  on every state push and every app transition.

## The Genres page files a genre by what it is

The owner's card `OTHER` held the chip `New Wave` while `METAL` and `ROCK`
bucketed right. `server/integrations.py:genre_category` was a second genre
taxonomy that keyword-matched the raw name and never consulted the vocabulary —
and `"new wave"` reads as the keyword `"wave"`, so a rock movement was filed by
its spelling. It now buckets the **derived family** (`mlo.genre_vocab.parent_of`)
first and falls back to the name only for a genre the vocabulary cannot place,
which makes the page and the app's own family derivation one answer.

`New Wave → Rock`, `"Electronic; New Wave"` (the endpoint's own split) →
Electronic + Rock, `Shoegaze` / `Alternative Rock` / `Garage Rock` → Rock,
`Dark Wave` → Electronic, `Polka` → Other. The buckets are unchanged, the
keyword table is unchanged, and the composition of the two taxonomies (all 28
families) is pinned in `tools/test_genre_vocab.py`.

## Dependencies: the header scrolls, and a landed install is not an error

* **The header no longer pins.** The page's `PageHeader` carried `sticky` and
  sat over fifteen scrolling rows with the counts line behind its blurred strip.
  It scrolls away with the table now; Grading is the only page whose header has
  to stay put.
* **A pip install is judged by what the detector finds, not by pip's run.** The
  owner's report — "dependency installs work, but yt-dlp succeeds with an
  'error'" — is a class, and three checks carried it:
  * pip's **exit status** decided the install, and a non-zero exit also
    `rmtree`'d the folder. pip writes the package and its `.dist-info` first and
    its console script last, so a bind mount that refuses that last write
    (`ERROR: … OSError: [Errno 5] Input/output error: '.../bin/yt-dlp'`) left a
    complete, importable package that the app reported as a failure and deleted.
  * the **`landed` probe** guessed the import name *and* assumed a package
    directory: `eac-logchecker` installs `eac_logchecker.py`, a module, so a
    finished install read as "pip install failed" — and was deleted too.
  * the **prune** after a landed install ran bare, and `os.listdir(tools_dir())`
    on the Windows bind mount raising EIO was the one post-install failure that
    leaves the folder intact: `[Errno 5] Input/output error`, `ok: false`, tool
    still working — the report's exact shape.

  The verdict is now the app's own detector in the folder just written
  (`tools.pip_import_present` — one shared rule with the detector — plus
  `python_pkg_version` matching the version that was asked for). pip's non-zero
  exit is logged, pruning is housekeeping that never fails a landed install, and
  a run that landed **nothing** still raises with pip's own last line, which is
  what a genuine failure, an unreachable release and an unwritable folder all
  look like.

## Under the hood

* Spec rules **R288** (the app process has to be producing audio itself) and
  **R69a** (a pip install is judged by the detector, never by pip's run), with
  R265a and R271 amended to say what the last report changed.
* `tools/check_ios_ipa.py` now requires `AVAudioPlayer` in the shipped binary,
  beside the session and star names it already read out of the IPA.
* `README.md` and `desktop/README.md` carry the iOS audio story as it is now.

## Verified, and what could not be

* iOS: the keep-alive's WAV generator was compiled and run standalone and its
  output read back by `ffprobe` (`pcm_s16le`, 44.1 kHz, mono, 16 bit,
  0.500000 s, 44 144 B); both Rust modules parse clean; the IPA checker was
  exercised against synthetic IPAs that do and do not carry the keep-alive.
  What a Windows desk cannot do is compile the Apple-target modules (`cargo
  check` for iOS needs Apple's SDK) or feel the playback on a device — that is
  the macOS CI job and the owner's phone.
* Genres: `tools/test_genre_vocab.py`, `test_genres.py`, `test_genre_format.py`.
* Dependencies: `tools/test_dep_updates.py` (with the new cases, which fail
  seven ways against the pre-fix code), `test_platform_guards.py`,
  `test_video_pipeline.py`, `test_script_optimizations.py`.
* Web: `tsc -b`, `npm run build`, oxlint; the AudioContext resume driven in a
  real browser (hidden + playing resumes, hidden + paused stays parked); the
  Dependencies header measured with Playwright against a scratch server.
