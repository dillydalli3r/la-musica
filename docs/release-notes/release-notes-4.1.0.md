# la musica 4.1.0 — the phone plays, and the app uses the screen

4.1.0 is the release the iOS playback report asked for, plus the round of UI and
pipeline work that came with it: every page uses the window it is given, the
ambience follows the beat instead of drifting behind it, the library is
searchable by letter, the discography opens on albums, and the Soulseek pipeline
searches better, waits less and can be stopped when you want it stopped.

## iOS: the audio session was stopping itself

"Pressing play on tracks just makes them pause immediately" is a precise
symptom, and 4.0.3's own change was the cause. That release gave the audio
session a lifecycle: the category was re-applied and the session ACTIVATED on
every start of playback, then DEACTIVATED — with `NotifyOthersOnDeactivation` —
on every stop.

Both halves break a webview's playback:

* `setCategory:mode:options:` on a session that is already ACTIVE is Apple's
  documented "may interrupt audio playback". The app is always in exactly that
  state when the call arrives, because the web player writes its `playing`
  state the moment a row is pressed — before the element has started — so the
  shell's category call landed while the webview's own `<audio>` was starting
  into the same session. The interruption stops the element, the element fires
  `pause`, the player reads that as "the track ended by itself". Press play, get
  a pause.
* Deactivating on stop is the same fault from the other side. A pause that
  arrives in the same second as a start — a track change, a refused load, the
  OS pausing the element — handed the session back mid-startup, and the play
  that followed began in a session that had just been taken away.

The new rule is one sentence: **the category is taken once, at setup, while the
session is inactive, and the session is activated when playback begins and never
handed back while the app lives.** Activating an already-active session is a
no-op, so a start can no longer interrupt a start. The OS notifications
(background, becoming active, an interruption ending with `ShouldResume`) still
re-assert it, and a restart of the audio server — the one moment nothing is
playing into the session — is the only runtime path that re-takes the category.

The audible trade is stated honestly: another player this app interrupted does
not resume by itself the moment you pause la musica. That is what paying for a
session that cannot be pulled out from under a starting track looks like.

## iOS: the other audio bugs in that path

* **The volume slider works on iOS now.** `HTMLMediaElement.volume` is
  read-only there — assigning it is dropped without an error — so a slider that
  only wrote that property did nothing at all on a phone. Every element the
  player routes through the WebAudio graph now carries its own volume stage, and
  `applyVolume` puts the level there, pinning `el.volume` to unity so the two
  stages never multiply. Elements without a graph keep the plain property.
* **The audio graph is unlocked by a gesture.** A context created outside a user
  gesture starts suspended on iOS, and a track routed into a suspended graph is
  a track playing silently. The first touch, key press or click anywhere in the
  app resumes the shared context, and the context's own state changes (an
  interruption ending, the app coming back) re-ask as well.
* **The player stops lying about what it is doing.** The element's `play` event
  now writes the playing state as its `pause` event already cleared it: a track
  change pauses the outgoing element on the way in, and until this release that
  could leave the bar — and the iOS session bridge, which follows the same state
  — saying "paused" about a track that was playing. A REFUSED `play()` and a
  stream that fails to load are reported instead of swallowed
  (`play().catch(() => {})` was every call site), and coming back to the app no
  longer declares a slow start dead: the reconcile only clears the state of an
  element that is really paused mid-track.

## iOS: cleartext media, and the local network

`NSAllowsArbitraryLoadsInWebContent` covers what the *web content process*
fetches. The bytes of an `<audio>`/`<video>` element are loaded by WebKit's
media stack, which reads the blanket `NSAllowsArbitraryLoads` key — so the
plist now sets both, and `NSLocalNetworkUsageDescription` gives iOS 14+
something to show when it asks for local-network access (without it the
permission cannot be requested at all, so a LAN server is unreachable while a
VPN address still works). The CI job that reads the built `.app` back verifies
all three, and `tools/check_ios_ipa.py` verifies them in the shipped IPA.

The media routes also answer CORS for ANY origin now (`/api/stream`,
`/api/videos/stream`): a phone's `<audio>` is fetched with
`crossorigin="anonymous"` because the visualizer, the equalizer and ReplayGain
read that stream through a WebAudio graph, and the origin the MEDIA loader
states is WebKit's business — it can be `null` when the bytes are pulled by the
media process. No match, no load: the app is completely reachable, every API
call works, and pressing play does nothing. Both routes are read-only and
authenticated by the session token in the URL, and `*` (rather than echoing the
caller) means no browser can pair that response with credentials.

## The star remembers a press from the lock screen

The star is `MPRemoteCommandCenter.likeCommand`, and the case it exists for —
pressing it on the lock screen — is exactly the case where the webview may be
parked and a Tauri event is dropped on the floor. A press is now remembered in
the shell until the web UI answers with a state push of its own, and re-sent the
next time the app is active: one press, one like. `enabled` is re-asserted on
the same transitions, as before.

## The background reacts to the music, and stops banding

`--amb` is still the smoothed level, and it now has a partner: `--amb-pulse`,
the part of each tick that arrived ABOVE a slow (~1.5 s) follower — a kick, a
snare, a hit — amplified, decayed per tick (so a busy passage cannot turn it
into a flicker), and applied to the colour-field layer with a ~0.1 s transition
of its own. The level's own window widened (8 dB → 12 dB), so quiet and loud
passages move the backdrop more than they used to.

The faint horizontal lines across the ambience were 8-bit banding, and the
existing grain could not fix them: `overlay` blending barely registers over the
dark half of the field. The grain is drawn twice now — the same turbulence tile
at two sizes, one `overlay` and one `screen` — which dithers both ends and
cannot beat into a pattern of its own.

## The app uses the screen

Every page shell was capped at 1152 px, which stranded ~400 px of a 1568 px
window (and much more on a desktop monitor). Pages cap at 1600 px now — the
width Browse and Export already used — and the Home dashboard refreshes itself
every minute instead of waiting to be pressed (a plain poll of the server's
cached payload, so it costs one small GET; a hidden tab stops asking).

Home and the Library also stop shouting "All checks pass": that state is a small
green dot beside the page title, with the tooltip to match. Anything that needs
saying is still written out — the strip below the header draws the warnings
exactly as it did.

## Library: search, and the alphabet

The Library toolbar carries a search field and an A–Z menu beside the view
switcher. The field filters whatever view is showing (albums, artists, tracks),
case- and accent-insensitively; the menu lists A–Z and `#` with the count of
matches per letter and filters to one letter. Both compose with the current
sort, with group-by-artist, and with each other, and an empty result says so
instead of drawing an empty list. On the album page the recommended shelf wraps
into as many rows as it has cards instead of clipping the last one at the screen
edge.

## MusicBrainz artist pages: the discography you came for

Release-group types are ordered Album, EP, Single first — then everything else,
most-populated first — and a compound label is ranked by its primary type, so
"Album + Live" sorts with the albums. Previously the page led with whatever
MusicBrainz happened to serve first, which for an artist with 49 live albums and
10 studio ones meant opening on "Album + Live".

## Genres: capitalised where you read them, correct where they are derived

Genre chips print the reader's form ("progressive rock" → "Progressive Rock",
`r&b` → "R&B") while the stored values, the filter buckets and the
`genre:"…"` query keep the lower-case form they match on. The derivation behind
them was fixed in the same pass: the keyword rule carried "wave" as an
electronic word, so a new wave track's family was *electronic* — the report was
a Talking Heads-era row reading "Electronic; New Wave". New wave is rock, no
wave is experimental and new romantic is pop; the synth-driven waves stay where
they were, and the genre called "wave" is electronic.

## Soulseek: searched by MBID, spaced by an hour, and stoppable

* **A release is also searched by its MBIDs, by default**
  (`soulseek_auto_mbid_queries`): the default query set carries the release's own
  id, the recording ids of its first `soulseek_auto_mbid_tracks` tracks (4,
  1–10) and each of those tracks' own "artist title" — deduped, and posted in
  the SAME parallel batch, so the extra reach costs no extra wait. The tracklist
  comes from the payload the app already holds: no additional MusicBrainz
  request.
* **One track can be searched for on its own, by name or by pasting its MBID.**
  The Soulseek page's search box accepts a UUID, resolves it with the cached
  MusicBrainz client and runs each query as its own slskd search at once (one
  merged, deduped result list, one poll key, one stop); a library track's own
  menu offers "Search Soulseek for this track". Nothing goes to the wishes or
  the queue unless you download something.
* **Failed means given up.** A failed attempt whose wish the worker will search
  again by itself lives in the queue's **Background** section with the failure's
  own sentence and the next-search clock; a terminal failure keeps its Failed
  row, and a settled job never draws a second row for a wish that still exists.
  This is the owner's own report: an auto-import download that failed once while
  the app was still looking for it showed up under *Failed* as if the album had
  been given up on.
* **An hour between the searches of one release.** `wishes_interval_hours` ships
  as **1**: the gap is between two ATTEMPTS of the same release, and one attempt
  still walks every ranked candidate it may, each with its own bounded window.
* **The queue's finished sections clear themselves when a new run starts** — a
  job start, a bulk enqueue, a page download, an accepted prompt, the worker's
  own pass. Live work, background wishes, retryable failures and *Needs you*
  rows are left alone, and a job that finishes after the clear stays visible.
* **Cancel stops the network work at once.** The row's Cancel (and the
  Auto-import Stop) drops the job's outstanding slskd searches on the spot — the
  search loop asks the cancel flag before every poll and tears the searches down
  at slskd instead of waiting out the window — cancels its transfers, and either
  aborts the import at a safe point or says it will stop after the current step.
  The row is gone the moment the press lands, and a cancelled *Add to library*
  takes its framework album and its wish with it.
* **The searches themselves are tighter**: the per-candidate half-second
  "settle" sleep is gone from the worker's wait loop, and the wish's cadence is
  one number everywhere (`wishes_interval_hours`). The early exit that starts a
  download the moment a GOOD folder is visible was audited rather than relaxed,
  on purpose: the poll's predicate IS the scorer's own (`find_candidates` +
  complete + lossless), so a whole lossless folder already starts without
  waiting for the network to go quiet, and a folder the scorer would reject is
  still never started — the alternative would have been a second, laxer rule
  that could begin a download the pipeline would have thrown away.

## One port, one number

`docker-compose.yml` publishes `${MLO_SOULSEEK_LISTEN_PORT:-50000}` while slskd
listens on whatever `soulseek_listen_port` says — and a share peers can see the
SIZE of and never connect to is a forward pointing at a closed port, which is
the owner's "clients can detect the number of shared files" with "Requesting
file list…" forever. The variable now SEEDS the app's own setting at startup
(the same way `MLO_MUSIC_FOLDER` does), a save that contradicts the pin is
refused with the pin named, a change that gets through restarts the daemon, and
the image `EXPOSE`s both ports.

## Under the hood

* The player's `playing` state follows the element in both directions, and the
  visibility reconcile only clears an element that is really paused mid-track.
* `tools/check_ios_ipa.py` verifies the new plist keys in the shipped IPA, and
  the mobile CI job verifies them in the built `.app`.
* `GET /api/soulseek/shares` answers 503 ("slskd is not running") when the
  daemon is down, like its sibling transfer routes — it used to let the
  connection error escape as a 500, which reads as a broken app rather than as
  a service that needs starting.
* New spec rules R268–R287 in `docs/OPTIMIZATION-GRADING-SPEC.md` (§7.48 the
  phone's playback and the app's width, §7.49 the acquisition pipeline), each
  pinned by a suite or a browser check.
