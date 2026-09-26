# la musica 4.2.0 — the twelve audits

4.2.0 works every open issue in the tracker — #58 through #69 — and closes them
with the behaviour written into
[`docs/OPTIMIZATION-GRADING-SPEC.md`](../OPTIMIZATION-GRADING-SPEC.md) as
R299–R316 (plus amendments to R151, R155, R269, R270, R275, R288, R289 and R296
where the old text no longer described the app). Every change is pinned by a case in
the suite that already owns its area: **128 Python suites, 0 failures**, and the
browser checks below were re-run against a scratch server on the owner's
machine.

## The player remembers, and a jump can be taken back (#62, #69)

Reload the page mid-track and the queue, the track and the second it was on come
back — paused, at that second, without counting a second play. The state lives in
`localStorage["mlo.player.state.v1"]` and the position is written on a coalesced
timer, never per `timeupdate`.

- **`gapless_playback` (ON)** is the setting the issue asked for: the idle audio
  element preloads the next sequential track and the hand-over plays it without a
  load step; off, every track takes the normal path.
- **Ctrl+Z / Cmd+Z undoes the last jump** — a mis-clicked seek, a ±10 s skip, a
  lyric-line jump (≥2 s, last five, same track only, no button anywhere).
- **The OS gets a real control set**: `seekbackward`/`seekforward` are registered
  (±10 s, honouring the platform's `seekOffset`) instead of being declared
  unsupported — which is why the lock screen drew ⟲10 / 10⟳ with nothing behind
  them — and `mediaSession.setPositionState` publishes duration and position so
  the scrubber has a timeline.

`tools/check_player_state.cjs` (30/30) and `tools/check_os_stop_resume.cjs`
(24/24) drive all of it in a real browser.

## Aliases are names (#59)

MusicBrainz aliases are imported (`TITLEALIAS` / `ARTISTALIAS` / `ALBUMALIAS`,
plus `-<locale>` when MB states the language, one list value per language), shown
in the library with the original beside them (`宇多田ヒカル (Hikaru Utada)`),
matched by the search bar and the query builder, used as a fallback by the
advisory and lyrics lookups (original first, alias only when the original states
nothing), and published to LRCLIB as a second submission per alias name.
`grade_check_alias_needed` fails a track whose title is written in a non-Latin
script and carries no alias.

## Grading gets teeth (#61)

- **A `COMMENT` that carries anything fails** — and script 10 clears it, gated by
  `strip_unknown_tags` exactly like the excess-tag strip. Dead `KEEP_VORBIS_KEYS`
  (which advertised COMMENT as keep-forever) is gone.
- **AcoustID is required, key or no key**: every non-video track needs the
  `ACOUSTID_ID` + `ACOUSTID_FINGERPRINT` pair, with or without
  `acoustid_api_key` — script 21 creates the pair locally with `fpcalc`; only
  *submitting* needs the user key. The check used to fire only when a file
  already carried half a pair, and the reader missed the spelling beets writes
  (`TXXX:Acoustid Id`) — which is the "the app says I have no AcoustID when I do"
  report.
- **70 checks**, up from 68: the new alias rule and a `grade_check_flac_md5` row
  the spec had never listed.

## The importer: one chain, honest notices, no video "best release" (#67, #68)

- **One chain per album.** The download's own finish and the import queue both
  check the `job_locks` claim first, so the scripts run once (the owner's "the
  scripts seem to run twice").
- **A notice means the work ended** — a finished download and the end of a whole
  import, never a started transfer, a download still being verified, one album of
  a bulk run or a chain still running. They name the album (artist, album, year),
  never a folder, and only those two kinds reach a closed device.
- **A release that needs a music video to be complete is never the default
  pick** (audio-only candidates rank above it, at both the Soulseek candidate
  layer and `mlo/release_choice`).
- **The queue's groups are exclusive** (the owner's Ænima, shown as Verifying AND
  Completed): a running item is only In progress, Completed only when the
  download truly finished, and finished rows clear themselves when the next
  import starts (`soulseek_clear_completed_on_import`, ON).
- **The sharing card carries the guide the issue asked for** — the three links
  that have to line up, why a client on your own network cannot prove any of it,
  and the Windows firewall command for the listen port. Shared-history rows name
  the album, peer, file, time and size instead of a `p2p/<uuid>/<uuid>` path.
- The import's step text is the album-scoped one: a purely audio album is no
  longer announced as `Remux videos (MKV) + 20 more`, and a step that does not
  happen for this album is not published.

## Faster where it was slow (#60)

Measured with `tools/perf_pipeline.py` (which times every script through the
app's own runner): whole-run wall clock **215.9 s → 190.2 s**, and the local
subset (no network scripts) **137.7 s → 111.0 s**. Per change, measured:

| Change | Before | After |
| --- | --- | --- |
| `/api/run` cache invalidation (one album) | whole-library `invalidate_all` | the run's own targets |
| 9 album-walking scripts, library-wide chain (300 albums) | 193.5 ms of walks | 0.026 ms (walks → 0) |
| replaygain.json, 400 measurements | store 5198 ms · read 432 ms | 26.3 ms + a 5.3 ms debounced write · 26.5 ms |
| `fpcalc` on one unchanged file, two passes | 2 runs | 1 run |
| Export of 3 files (codec `copy`) | 6 container parses | 3 |
| Layout's "is there audio below this folder" | 1046 µs | 277 µs (same answer) |
| LRCLIB instrumental lookups (per album) | serial, 0.4 s spacing | pooled, 4 tracks in 0.40 s |
| The bar's cover on the play path (74 px slot, 1400 px master, 20 Mbit/s + 20 ms emulated Wi-Fi) | 3.13 MB · artwork at **1230 ms** (the master fetched twice) | 12 KB · artwork at **98 ms**, one request |

The script chain also stops offering a script that cannot apply: an audio-only
album no longer runs (or advertises) the video remux.

- **The cover arrives with the play press, not a second after it.** Every
  surface that draws a cover smaller than the master now asks the server for
  that width (`GET /api/cover?w=`, bucketed to 160/320/640/1200) instead of
  pulling the 1200–3000 px file and letting the browser shrink it: the bar's
  74 px thumb and the queue rows share the 160 bucket, the fullscreen picture
  and its blurred ambient layer share the 640 one. The shrink is encoded once
  and kept under `<music>/.mlo/data/cover_thumbs`, keyed by the cover file's
  own stat — so a replaced cover is a new entry, never a stale hit — and a
  sized answer carries `private, max-age=300`, which is what makes the next
  track of the same album, the reopened pane and the OS media session cost no
  round trip at all (the URL a cover WRITE reports carries the new bytes
  immediately). The next track's cover also rides along with the gapless
  preload — fetched while the current one plays, not after the hand-over.
  Pinned by `tools/test_cover_preview.py` (the sized route: drawn size, its
  own ETag/304, one encode per width, no upscale, a replaced file is not
  served from cache) and `tools/check_player_state.cjs` 33/33 (the bar asks
  for a thumbnail, a row shares that URL, and a repeat play touches the
  network zero times).

## The UI fits the device (#63, #64, #65, #66)

- **Now Playing bar and fullscreen player** carry the track's ORIGINAL release
  year (`ORIGINALDATE`, `DATE` fallback), no element resizes its neighbours any
  more (the ReplayGain readout and the UP NEXT value hold their space), long
  artist/album/title text drifts through the existing `ScrollingText`, and the
  star rating sits after the bitrate readout.
- **Bar and Library open on the verdict, and the verdict waits for the import to
  finish**: a mid-import album is left out of `GET /api/grades/summary` (the same
  `job_locks` claim the queue reads) and the banner says how many were left out
  instead of reporting the tags its own chain is still filling.
- **The lyrics pane's zoom and offset controls moved into the pane's corner**,
  the docked sidebar no longer covers the run/progress row, and a reader-made
  jump in the lyrics lands in the same frame instead of gliding from the line
  they skipped past. The background visualizer's columns now land on whole device
  pixels, so no stale edge column trails in from the sides.
- **The MB artist page's discography is four foldable sections** — Album, EP,
  Single, then one count-ordered bucket with a searchable menu for every other
  type — instead of twenty stacked blocks.
- **Tables fit their data**: per-column pixel floors replace the wrapping title
  cell (which could be squeezed to 0 px), the playlist page's 700 px floor is
  `md`-only, the phone drawer and top bar survive 320 px, and a progress row
  degrades instead of overflowing.
- **The name column takes the table's free width**: the album page's tracklist,
  the library's Tracks view and the export preview dropped their pinned title
  widths for `md:w-auto`, so the column absorbs what the fixed columns leave —
  measured at 1440: 400 px of a 1200 px table, where the free width used to be
  shared out over every column in proportion to its width and the name came out
  311 with a long title wrapping beside 900 px of room. Its floor travels as a
  zero-height box inside the header cell instead of a `width`, because a fixed
  layout ignores a cell's `min-width` (measured: a 280 px ask came out 66 px
  wide with a 0 px name) and `min-w-max` only counts the widths the columns
  really declare.
- **A download's badge names where its files came from**: a `Digital Media`
  release wears its source beside the medium (`Digital · Bandcamp`, the release
  countries still after it, `Digital · Bandcamp · US, CA` on the page), built by
  the one helper the album card and the page's own chip both call — and when no
  file states a source, the app's own default (`Digital`) repeats the medium and
  is dropped rather than printed twice.
- **The Artists tab no longer scrolls under a short list**: `table-layout:
  fixed` makes the sum of a table's columns its floor, so four stored drag
  widths — every one inside the handle's own 40-900 range — drew a 1420 px table
  in a 1200 px box, and the sanitizer alone could not stop it (each value was
  legal). The stored map is now brought inside the columns' own floors plus the
  box's free room, and a hostile `mlo-colw-*` map (zeros, a string, an array,
  `null`, a legacy key shape) draws the table's data columns and covers instead
  of collapsing them. `tools/check_library_az.mjs` (90/90) and
  `tools/check_library_tables.cjs` pin all three.

## Mobile (#58)

The shipping mobile clients are the Tauri shells around this web UI, and the iOS
side had three real defects:

- **The ATS media exemption was inert**: `NSAllowsArbitraryLoads` is IGNORED on
  iOS 10+ once any scoped key is present, so the media loader's own key
  (`NSAllowsArbitraryLoadsForMedia`) is now set and asserted in the built `.app`
  and IPA — a build with only the blanket key loads the page and refuses every
  track.
- **The star cannot double-toggle**: the lock-screen press carries a number and
  the page drops a re-delivery it already handled.
- **The keep-alive player is released** on an interruption or a media-services
  reset instead of leaving a dead handle that made it a permanent no-op after the
  first phone call, and the playback diagnostics grew the rows that made all
  three observable (`session_category_taken`, `session_activate_last`,
  `keep_alive_platform_stops`, the star's three-way state).

The unshipped, three-versions-stale native Flutter client (`flutter/`, built by
no workflow and referenced by no doc) is deleted — the Tauri shell and the
browser are the clients this app ships.

## Everything else

- **Auto-import acts on a good find in seconds.** A search's window is the quiet
  time before slskd ENDS it, and slskd serves a search's responses only once it
  has ended — so the walker's 60 s window meant nothing could be read for 60 s,
  and a perfect folder found at t≈2 s was first readable at t≈60 s. Each
  candidate's search now runs two passes: every configured template is asked
  with the new **`soulseek_search_fast_seconds`** (5 s), and the moment ONE
  complete lossless folder is readable its download is enqueued and the job
  moves on — a transfer already started is never cancelled for a marginally
  better copy that turns up later. `soulseek_search_timeout_seconds` is now the
  TOP-UP window: spent only when the fast pass found nothing usable, it keeps
  reading the searches that are still running at slskd (rather than cancelling
  them) before the walk moves on to the next ranked edition. Searching several
  releases at once is unchanged and pinned: a job sitting on a peer's queue does
  not hold another release's search. R151 rewritten to match.
- `server/api_queue.py`'s stages and the Soulseek page's groups now come from one
  predicate; `share-reachable.yml`'s outside check is named in the sharing guide.
- The README was condensed from 401 lines to ~140 with every command, default,
  key and limit kept.

## Verified

- `python tools/check_versions.py v4.2.0` — all 10 copies agree.
- All 128 `tools/test_*.py` suites, 0 failures; `npx tsc --noEmit -p
  tsconfig.app.json` and `npm run build` clean.
- Browser checks against a scratch server on 8011 with a synthetic library:
  `check_player_state.cjs` 30/30 · `check_os_stop_resume.cjs` 24/24 ·
  `check_fullscreen_player.cjs` · `check_np_metadata_contrast.cjs` ·
  `check_library_tables.cjs` · `check_library_az.mjs` 56/56 ·
  `check_lyrscroll.cjs` 14/14 · `check_lyric_clock.mjs` · `check_release_choice.mjs`
  · `check_queue_view.mjs` (53 checks) · `check_menus.cjs` · `check_sidebar.cjs`.
- The iOS changes are proven by reading, a Rust parse check and `cargo check`
  (which cannot compile the `#[cfg(target_os = "ios")]` modules on this box) plus
  the extended `tools/check_ios_ipa.py` against a synthetic IPA — **on-device
  behaviour is not verified here** and the mobile CI job is the first place the
  plist assertions run for real.
