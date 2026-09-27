# la musica 4.3.3 — the columns that fit, the line that carries, and a submission LRCLIB accepts

Four reports from a real install, one of them outright broken.

## The album page was the one page with no width

Every page in the app is `mx-auto max-w-[1600px]` — Home, Library, Artist,
Downloads, Favorites, Grading, the import wizard — and **AlbumPage and
TrackPage were the two without it**. The tracklist was therefore the only
surface that stretched to the window: measured at 2560 px, the table was
**2320 px wide with the name column alone taking 1712 of it**, which is the
report — "Columns in the app are way to long, atleast on album pages, also rows
seem to wide. To be perfectly clear, columns should auto-fit to the space on
screen."

The name column is deliberately the row's one flexible column (R313, from the
opposite report: a title wrapping while free width sat beside it), so the page
width is the right lever — the free width is now the page's, and the page is
the same 1600 px the Library's tables get: **1552 / 944 at 1920 and 2560**.

The genre column's floor was the other half. 96 px cannot hold a genre this app
itself writes — `"Rock; Garage Rock"` is **148 px** at the table's font — so it
wrapped onto three lines and set every track row to **80 px** (the cover cell
sets 52). The floor is 160 now, measured the way the other floors are (widest
value + a few px of slack), and a track row is 52 px again.

## An instrumental track wears no lyrics verdict

The player hides an instrumental's stored words (`npLyricsMode`: the state wins
over the text), but two surfaces graded them anyway: a track tagged
`INSTRUMENTAL=1` that still carried a stored plain text was reported with the
full failing vocabulary — the red cross and *"this track should hold a synced
version"* — on the track page and in the stored readout, a demand no
instrumental can satisfy. Both now say what the app does instead
("Instrumental", "instrumental — stored lyrics stay hidden"), the derivation
lives once (`Badges.isInstrumental`) for the player, the sidebar, the page and
the readout, and the player's own refused-plain mark waits for the track's OWN
payload (`!staleLyrics`), so a track change cannot wear the previous track's
lyrics state while the next one loads.

The other reading of that report is unchanged and worth stating: the `× Plain`
mark on a track whose **file is not instrumental** is the install's own policy
(`lyrics_allow_plain` off) shown on the row that carries the track's other
marks — it names the setting in its tooltip, and turning that setting on is what
takes it away.

## A press on a lyric line carries the reader

Pressing a lyric line snapped the pane to it and the emphasis followed a frame
later: *"it kinda teleports to it … for like one frame it shows the previous
line then cuts to the next one very quickly and looks jarring."* The pane now
**glides** to the pressed line — the one reader-made move where a glide is what
was asked for — while the emphasis lands in the press's own commit (both panes
seed the 60 fps clock with the target time), and the seek that press causes is
recognised as the press instead of re-snapping the pane through `markJump`. A
scrub on the bar, an offset step and a zoom change still land in the same frame
(`lyricMove`); the clock's own advance still glides.

## LRC submission to LRCLIB works again

The client spoke LRCLIB's **obsolete** contract: artist / track / album /
duration in the query string, and no publish token. Today's `POST /api/publish`
wants the metadata in the JSON BODY and a fresh, single-use `X-Publish-Token`
obtained from `POST /api/request-challenge` — a proof of work whose nonce is the
smallest `n` with `sha256(prefix + n) <= target` (~2²⁴ attempts, ~15-25 s of one
process here). Without either half the submission is refused, which is why the
button never worked.

`mlo/lyrics_providers.py` now solves the challenge (LRCGET's rule, the one
LRCLIB's docs point at; bounded by `PUBLISH_SOLVE_DEADLINE`), sends the token
and the body, and reports LRCLIB's own answer (`201`, `400
IncorrectPublishTokenError`, a 429 rate limit, a 409 duplicate from an older
server) instead of a generic failure. The `User-Agent` carries the app's name,
version and link, read from `mlo.__version__` so a release cannot leave a stale
copy behind. A synced text submitted alone has its plain half derived
(`mlo/lyrics_publish.to_plain`), as the script and the editor already did. The
web call's timeout is 180 s — the solve is real CPU, not a slow network.

## Verified

- `python tools/check_versions.py v4.3.3` — all 10 copies agree.
- `tools/test_lyrics_publish.py` (rewritten for the current wire format: the
  smallest nonce, the equality boundary, an unsatisfiable target at its
  deadline, the token header, the body fields, no query string, the refusal
  wording) and the new `tools/check_lrclib_publish.py`, which drives the real
  `POST /api/lyrics/publish` against a stub enforcing LRCLIB's rule and
  verifying the submitted token itself.
- `tools/check_lyric_press.cjs` (new): the pane's biggest single-frame step is
  a fraction of the move (it is 100 % on the old build — the teleport), the
  emphasised line is the pressed one within a frame, and the pressed line lands
  on the pane's anchor lane.
- `tools/check_lyrics_kind.mjs` gained three cases (track page, readout) that
  fail on the old code; `tools/check_lyrscroll.cjs` 14/14; `npx tsc --noEmit`
  and `npm run build` clean.
- `tools/check_library_tables.cjs` holds the geometry: a 2560 px window may not
  widen the tracklist past the page width, the name column must be exactly what
  the wrapper has left after every other cell, and a one-line row keeps a
  one-line row's height. One case in that file — "the migrated list replaces
  the old key" — fails identically on the pre-4.3.3 tree (reproduced with this
  release's changes stashed); it is unrelated to this release and left as it
  was found.
- 127 of the 128 `tools/test_*.py` suites pass locally in one serial run. The
  exception is `tools/test_export_audio.py`'s EQ-band assertion, which fails on
  THIS host's ffmpeg (8.1.2, gyan full build) and not in CI: the band meter
  reports the same loudness before and after the EQ (+0.00 dB) where CI's
  ffmpeg measures +4.20 dB, and this release touches nothing in the
  export / EQ / metering path. CI — which gates the release — runs that suite
  against the distro ffmpeg.

R325 (the LRCLIB submission), R326 (a press carries the reader), R327 (the page
width and the measured genre floor) and R328 (an instrumental wears no lyrics
verdict) are in `docs/OPTIMIZATION-GRADING-SPEC.md`.
