# la musica 3.23.0 — the equalizer, and the four reports behind it

Four issues (#49–#52) plus the two things you asked for while they were open
(short release-facing docs, and the spectrum strip following the same contrast
rules as the lyrics). Everything below was reproduced, changed, and pinned.

## #52 — an equalizer that plays, not just exports

The app could already read **Equalizer APO / Peace** profiles — it baked them
into an export. Now the same profiles apply to what you PLAY.

- **Sidebar → MAINTAIN → Equalizer.** Presets (`flat`, `bass_shelf`,
  `presence`, `night`), everything you have imported, and a curve you build
  band by band.
- **AutoEq search.** Type a headphone model and import the measurement's own
  correction: the project's index is 6 290 models (cached for a month, so a
  search is not a megabyte per keystroke) and the import is one file fetch —
  `Sennheiser HD 600`, `Moondrop Aria`, ~10 bands each, preamp included.
- **Visual and interactive.** The curve is drawn from the browser's OWN
  `getFrequencyResponse` (the same maths the player runs), every band has a
  draggable handle, and Fc / Gain / Q are typeable with the exact value you
  typed (clamped, committed on Enter or blur). Per-band on/off, add/remove,
  a preamp slider, **Save** / **Save as** / **Revert**, delete.
- **Live.** Edits are audible while you drag; nothing is stored until Save.
- **One key, every client**: `playback_eq_profile` (default `""` = off, so an
  upgrade sounds exactly as before). The graph is ReplayGain gain → EQ preamp →
  the profile's bands → the analyser, so the meters and the ambience read the
  equalized signal. Verified in a live page: 11 nodes (1 preamp gain at 0.676 =
  −3.4 dB, 10 biquads at the profile's own frequencies/gains), with the
  analyser still reading signal afterwards.

One honest difference, stated in the page as well: a peaking band, a pass and a
notch render identically to an export's ffmpeg chain; a SHELF's width does not
(Equalizer APO's custom slope reaches ffmpeg as a Q, and a WebAudio shelf is
fixed-slope).

## #49 — the walk moves on, the noise stops, the numbers are true

**It would not move to the next release.** A release group whose best edition
was found but REFUSED (`score 60 is below the required 100`, a `.log` that
does not reach `soulseek_auto_log_min_score`, a verification that failed) ended
on the sentence *"Every candidate was rejected (…)"* — which the retry policy
classified as a TRANSIENT failure, the one classification that STOPS a walk.
The wish was re-marked with its backoff, the walk restarted at edition 1, and
the row sat on *"next: Release 1 of 5"* with *"Retrying in 28m 55s"* for ever.
A refusal is now its own outcome (`rejected`): the candidate is spent, the walk
advances to the next ranked edition exactly as it does after a miss, and a walk
whose every edition was refused rests in **Background** without spending a
not-found attempt. Covered by `tools/test_wishes_pipeline.py` driving a
three-edition wish through the real worker: every edition asked in ranking
order, the walk's own record moving with it, `not_found` still zero.

**The notification for it is gone.** A settled job that fills a wish is ONE
step of that wish's search, and one spent step is not an outcome: it announces
nothing (`soulseek_auto._wish_keeps_looking`). The ends that are real —
`wish_failed`, `wish_not_found` — still arrive, and a job with no wish behind
it (an interactive search, a bulk add) keeps its own `download_failed`.

**Every notification reaches every client.** Two fixes, one rule:
`OS_KINDS` and `PUSH_KINDS` are now ONE set — an outcome worth interrupting an
open app for is worth waking a closed one for — which added `import_done`
(pushed but never popped before), `watch.new_release` and `storage_pruned`
(neither). And `?since=` now replays from a DURABLE log beside the app state
(newest 400 frames), not only the 100-event memory ring: a desktop or mobile
shell cannot be woken by Web Push at all (no service worker in a Tauri
webview), so the replay on reopen is the only way it hears about an import that
finished overnight — and it used to lose everything across a restart.

**Peers/files told the truth.** The search box derived `responseCount` /
`fileCount` from slskd's search STATE, which only settles once a search has
ENDED — hence "0 peer(s), 0 file(s)" beside a list of hits (and a live transfer
at 11 MB/s). And the auto-import progress SUMMED its counters across the query
templates it asks in parallel, counting the same peer once per template (15
"responses" for 3 peers). Both now describe the list they travel with, and a
peer is counted once — pinned in `tools/test_soulseek_candidates.py`.

## #51 — the player opens in the window, and its text stops blocking

**Entering the fullscreen player no longer seizes the screen.** Clicking the
album cover (or the bar's own glyph, or `F`) mounts the viewer — `fixed inset-0`
over the app — without calling `requestFullscreen`. Real browser fullscreen is
its own button in the viewer's top bar, and a transition this pane asked for no
longer closes the viewer.

**The dark block under the lyrics is gone.** The glyph shadow was a tight
near-opaque core (3px, 0.92) plus a halo; on a bright field (a red cover reads
bright to the eye while its average luminance sits under the ink flip) that
core merged between glyphs into a slab of uniform dark pixels. It is now ALL
halo — four low-alpha stops, the tightest 0.42 — so what crosses a bright cover
is depth, not a plate. Verified against a white cover: `.np-shade-light`, no
tight core, text legible with no panel.

**The spectrum strip follows the same table.** Drawn with the app's accent it
was a white strip on a white cover. `<Visualizer>` now takes the ink polarity
(`ink.viz`): near-black bars over a bright cover, near-white over a dark one —
verified by sampling the canvas (bar luminance 8 on a white cover) — while the
docked sidebar keeps the accent, because its surface IS the app's own.

**The meters latch on.** Every media element has its own WebAudio graph, and
the strip read the LAST one attached — often the idle half of the gapless pair,
whose spectrum is zeros, which is why the visualizer "sometimes just didn't
work". It now reads the element that is actually PLAYING, resumes a context the
browser suspended (or WebKit "interrupted") *before* answering, retries a
WebAudio failure after a cooldown instead of latching the meters off for the
session, and ignores an element that has left the document.

**Volume readout** is one typographic unit now: the digits and the `%` are the
same 10px tabular mono at the same opacity, instead of a 9px sign at 60 %
parked in the box's padding.

## #50 — the details menus fit, and they can download and export

The row's "…" menu had `overflow-y-auto` and a `max-h-[70vh]` — and was still
cut off, because it was positioned `absolute` under a trigger near the bottom of
a shell that is `h-dvh overflow-hidden`: the lower half was UNREACHABLE, not
merely clipped. `Popover`'s fixed mode now caps the panel to the room its own
trigger leaves (measured, `100dvh`), `OverflowMenu` defaults to it, and the
panel scrolls inside the window with the app's own thin scrollbar. Measured on
a bottom row: panel 499.5px tall, bottom 8px above the fold, 159px of scroll to
reach the last entry.

And the menu now carries **Download for offline playback** (the one
implementation, shared with the Download button) and **Export…** — as does an
album's own details readout, which previously offered neither while a track's
did. The album page's header pair is unchanged; this is the same pair, one
level down.

## Docs and smaller things

- `README.md` is 412 lines instead of 1 287 (and the repo description is one
  sentence instead of 316 characters). Nothing usable was dropped: every
  command, path, port, env var, config key, default and IP-caveat is still
  there; what went was the essay.
- `docs/OPTIMIZATION-GRADING-SPEC.md` gains rules **R214–R220** (the refused
  candidate, the silent walk step, the durable notification log, the metric
  counters, the details menus, the playback equalizer, the AutoEq import),
  **R52e/R52f** (the in-window viewer; the meters following the sound), two
  amendments (R151's transient list no longer lumps a refusal in with an
  outage; R52c's shadow is all-halo and the strip takes the ink) and three
  honest limits.

## After the release: the player's own rough edges

Eleven things found and fixed on the lyrics pane, the fullscreen block and the
player bar once this release's own code was on disk. None of them adds a
setting.

- **The lyrics pane stopped looking frozen.** A wheel or a touch parked
  auto-follow for **6 s**, and while it was parked NOTHING moved — the hold
  suppressed the line-change step too, so the song's line could change three
  times over a pane that sat still. `HOLD_MS` (`web/src/lib/lyrScroll.ts`) is
  **1 200 ms**: a finger drag re-arms it on every event, so it never fights a
  gesture in progress, and a long line picks itself up in place the moment you
  stop instead of waiting for the next line change.
- **A line cut in half by the pane's edge dissolves instead.** `.lyr-fade`
  masks the top and bottom 26 px of BOTH lyric scrollers — the right-docked
  sidebar and the fullscreen pane. It is a mask, not an overlay: the reading
  surface still paints no panel, tint or gradient of its own (R52c), and the
  pane's own pads keep the first and last lines clear of the edges.
- **Zoom and offset are on the words.** The fullscreen lyrics pane's footer now
  carries both lyric controls, rendered only while the pane is open, in the
  ink's own tone at 60 % (100 % on hover or keyboard focus, 40 % while the
  lyrics are stale). `LyricZoom` and `LyricOffset` take the surface's colour
  (`text-current`) instead of a fixed grey, so ONE control reads right in the
  sidebar, in the options popover and over the artwork.
- **The "Click to seek" tooltips are gone** from both lyric surfaces. A line
  still seeks when you click it.
- **A long title drifts instead of truncating.** The player bar's marquee is
  its own component now (`ScrollingText.tsx`, out of `PlayerBar.tsx`), and the
  fullscreen player uses that same one for its TITLE — which used to be cut at
  "…" mid-word — and for its album·artist row. It measures the overflow (a
  web-font swap, a badge appearing, a window resize all re-measure), and a
  title that fits never moves.
- **Album · Artist is one row** in the fullscreen block; the two stacked rows
  it used to draw read as two unrelated lines.
- **The fullscreen star row takes the ink.** `StarRating`'s two colours are
  parameters now, and this row passes the player's own ink — a zinc-600 outline
  and an accent fill both blended into a bright cover, which is the failure
  R52c's polarity exists to avoid.
- **The bar's centre cannot be overlapped.** The desktop grid is
  `minmax(0,1fr) auto minmax(0,1fr)` instead of `1fr auto 1fr`: a bare `1fr`
  carries an `auto` minimum, so at high browser zoom the flanks could not
  shrink and the "up next" / duration readouts ran over the centred seek row.
  They truncate now.
- **The spectrum strip repaints when its BOX or the display's pixel ratio
  changes.** The canvas resize check compares the backing store
  (`clientWidth`/`clientHeight` × `devicePixelRatio`, DPR capped at 2) instead
  of the CSS width alone — a height or DPR change used to leave the old bitmap
  for the browser to stretch into the new box: the reported "two offset rows of
  bars".
- **A slider's dot and its outer ring are one shape.** The ring is the thumb's
  own `box-shadow`, and the thumb no longer transitions `transform`, so the
  ring cannot scale on a hover animation after the dot has already jumped to
  the pointer — the reported "the dot and the outer ring move at different
  times", worst mid-drag.
- **The star-rating facet says what it counts.** `RATED_NOTE` — the sentence
  printed in the Filter menu — reads AND: an album counts as rated only when
  its own folder rating is set AND every track of it is rated, so a half-rated
  album belongs in "Unrated" (the reported "only one track counted"). Spec
  R105's own wording is that same sentence.
- **A typed number box gives the value back on Escape.** It used to restore the
  text and then blur — and the blur committed the very value Escape was
  abandoning, so Escape wrote what Enter writes.
- **A curve that cannot be built can no longer take the sound with it.** The
  equalizer is spliced into the player's own graph, and installing one tears the
  old chain out first: when the build then failed, the gain node was left
  connected to nothing — silence — instead of the plain path the function
  promises.
- **An import opens what it stored.** A profile's id is its name, so importing
  a name again REPLACES that profile; the page then opened its own older copy
  of the row (same id), showing the previous bands — and Save wrote those back
  over the import.
- **A menu that opens upward is capped by the room above it.** The details
  menus measure the space their trigger leaves; a top-placed panel was bounded
  by the space *below* it, so it could still run off the top of the window.
- **The notification log cannot drop a frame under load.** Its compaction is a
  whole-file rewrite; an event published while it ran could be rewritten away.
  The append and the rewrite now share one lock.
- `docs/OPTIMIZATION-GRADING-SPEC.md` gains **R52g** (the lyric pane: the
  shorter hold, the dissolving edges, the controls on the words, the tooltip
  that is gone, the star row's ink) and **R221–R225** (the drifting titles, the
  one-row album·artist pair, the bar's grid, the strip's backing store, the
  slider's ring). R216 (the log's lock), R218 (the cap on the side the panel
  opens), R219 (Escape reverts; a failed install restores plain playback) and
  R220 (an import opens the row it stored) carry this pass.

## Upgrading

Nothing to do. `playback_eq_profile` defaults to `""` — no equalizer, exactly
what you hear today — and the Equalizer page is where you turn it on. The
notification catch-up starts from the next event (a fresh install asks "from
now"; so does an upgrade whose client has no stored position).
