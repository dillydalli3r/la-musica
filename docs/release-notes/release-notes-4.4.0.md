# la musica 4.4.0 — the level that decides, the lines that leave the corners, and pages that stop re-reading the library

Five reports from a real install, and one of them was two numbers:

```
/api/storage        8.80 s    (170 audio files, ~15 albums, in the container)
/api/grades/summary 5.59 s    (the first load after the 60 s cache lapsed)
/api/library        0.058 s   (warm)
```

Nothing about that library is large. What it *is* is a bind mount: the app runs
in the container with `F:/media/music` at `/music`, so every `stat`, every
`scandir` and every `open` crosses the hypervisor's filesystem — which is why
"some sort of indexing" was the right instinct: the fix is to stop touching the
files on every page.

## Pages stop re-reading the library (issue #70)

**The whole tag/grade layer is a file that outlives the process now**
(`server/tagindex.py`). `build_album` is the expensive half of every page
(mutagen open + Vorbis-comment copy per track, the embedded-cover decode, the
integrity evidence read, the naming/path checks, the grade verdicts derived
from all of it) and it was recomputed per request and thrown away per restart.
A row is stored under the album's own identity — every entry of the album
folder as `relpath|mtime_ns|size`, plus a stamp of the app-state stores whose
content reaches a payload, plus the WHOLE config — and served only when that
identity matches exactly. Nothing is trusted to a timestamp: a rewritten file,
an added or deleted one, a changed setting or a rewritten audit evidence store
all miss. Every writer already calls `tagcache.invalidate_*` (~60 call sites),
and those hooks now drop the index rows too, so an in-app write is never served
stale; the file is an optimization and never a source of truth — an unreadable
or locked database falls back to building.

**The caches that were there now actually hold.** `tagcache.get_library` built
the tree *outside* its lock, so N first-paint requests each built the whole
library: a 6-request cold burst against a 3,120-file library built it **seven
times, 89.8 s of wall clock**; it is single-flight now, and the same burst is
**one build, 2.05 s**. The album and artist routes (`GET /api/album`,
`GET /api/artist`) were re-grading the album they had just shown, and are
memoized until the next write. `GET /api/storage` walked six directory trees on
every request while `StorageCard` polls it every 60 seconds — it is a 60-second
memo refreshed **behind** the request now (stale-while-revalidate), so a poll
never waits for a walk. Single-file ffprobe probes are memoized on the file's
stat (an album page asks for its videos on every visit), and the cover-colour
computation is keyed on the cover's stat.

Measured on a 204-album / 3,120-file scratch library (cold process, index
present — i.e. every restart except the first):

| endpoint | before | after |
|---|---|---|
| `GET /api/library` | 38,486 ms | 1,178 ms (**32.7x**) |
| `GET /api/home` | 7,929 ms | 723 ms (11.0x) |
| `GET /api/grades/summary` | 6,739 ms | 632 ms (10.7x) |
| `GET /api/artist` | 975 ms | 104 ms (9.4x) |
| `GET /api/album` | 189 ms | 56 ms (3.4x) |
| `GET /api/storage` (warm) | one walk per poll | 71 / 22 / 14 ms |
| `GET /api/videos/scan` (repeat) | 500 ms | 5 ms |

The number that transfers to the owner's install is the file-touch count,
because that is what the mount charges for: one library build touched
**15,735 `stat`s, 430 `scandir`s and 10,257 `open`s (6,400 media-file opens,
6,244 `AudioFile` constructions)**; a restart with the index present does
**0 `stat`, 430 `scandir`, 20 `open`s and 0 media-file opens in 663 ms** — the
scandirs are the library walk that finds the albums. The first run after the
upgrade still pays one full build (there is no index yet), and a run that
rewrites the evidence store invalidates once before it settles.

## The album pages draw in Firefox (issue #71)

`table { table-layout: fixed }` plus a definite width plus
`min-width: max-content` — the idiom `TABLE_FIT` and `ALBUM_TRACK_MIN_W` used —
is not the same declaration in the three engines. Gecko's intrinsic pass for
that pair returns an unconstrained sentinel: in a five-element document,
`table-layout: fixed; width: <any definite width>; min-width: max-content`
measures **17,895,698 px**, with `width: auto` it measures the floor (738 px),
so the runaway is the pair and not the columns. Chromium computes the same
declaration correctly; WebKit does not implement the floor at all — the name
column measured **188 px in an 860 px wrapper whose floor is 280 px**, and 0 px
at 620 px, which is the crushed column the floor exists to prevent.

The floor is now stated as `width: max-content; min-width: 100%` (`.table-fit`)
— the same demand, computed identically by all three — with
`.table-fit td { max-width: 0 }` at `md`+ so the floor is read off the header
row (what Chromium always did), and `useFittedWidths` pins inline widths while
it measures so the fit cannot collapse a stored column.

`tools/check_firefox_album.cjs` (new) drives the album tracklist in **Firefox,
Chromium and WebKit** and compares the geometry: on the fixed tree **59/59**,
with the pre-fix constants flipped back it is **48/60 (12 failures)**.

## A press on a lyric line no longer flashes the line it left (issue #72)

4.3.3 made a press *seed* the pane's clock with the pressed line's own timecode
so the emphasis lands with the press. What the seed did not survive is the very
next frame: the 60 fps tick overwrites it with the **audible** clock
(`currentTime` minus the WebAudio output latency), which the seek has not
caught up with yet, so `activeLineRange` walked back to the previous line for
that window — and because the press drops the emphasis transition for 600 ms,
it read as a hard cut. Measured in the browser: the stray frame is always the
line BEFORE the pressed one, 24-32 ms in, in **both** panes.

The pane's clock now has a floor for 400 ms after a press (`lyricHold` in
`web/src/lib/lyrScroll.ts`, applied after the 20 Hz quantize in both panes,
because the quantize alone can sit 25 ms below the line's own timestamp). It
self-expires in wall time, so a seek the element refuses cannot park the pane.

`tools/check_lyric_press.cjs` gained the settle-window assertion (every frame
in 0-600 ms must show the pressed line) and now launches Chromium with
`--disable-frame-rate-limit`: headless rAF delivered ~5 frames/s on the
fullscreen player's blurred surfaces, which is why the flash had never been
caught — with the flag it is 618 rAF/s. Fail-first on the unpatched tree:
1/106, 1/239, 1/156, 2/197 frames strayed (always the previous line). After:
167-402 frames in the window, every one on the pressed line, in both panes.

## The fullscreen background stops drawing lines out of its corners (issue #73)

Two layers were drawing straight lines, and neither was 8-bit banding of a soft
gradient — which is the only thing the grain layer can dither — so the lines
survived every opacity and every grain change (proved: identical with grain on
and off).

* `.amb-sweep` is a **conic** gradient, and a conic gradient's isophotes are
  straight rays from its own centre: its alpha ramp quantized into a fan of
  1-level ribs every 4-13 px, and the angle singularity in the middle measured
  **21.9 levels on a white cover** (14.9 mid-grey, 4.9 in the composite where
  every other layer measures at most 2.1). It is blurred past the rib pitch now
  (`filter: blur(30px)`): the fan becomes a ramp, the centre a soft spot, and
  the band survives (41 levels of angular span before, 39 after).
* `.amb-cover`'s `blur-3xl` samples nothing past its own box, so each edge of
  the blurred artwork ramped 34 % → 0 over the last ~64 px INSIDE the frame —
  quantized into a ladder of **21 straight 1-level ribs per edge, 1440 px long,
  meeting in the corners**. The layer is laid out 6 rem past the frame
  (`inset: -6rem`) so the whole ramp falls outside it: 21 jumps → 0, and 0.07
  levels of range over the same 160 px.

`tools/check_viz_corners.cjs` (new) samples the rendered pixels: 10 assertions,
including the whole-frame and centre-box step scans and the strip's own
baseline rows. Screenshots land in `.pi/shots-viz-corners/`.

## Scripts: the level decides, a run fills, and a forced pass writes only what changed (issue #74)

**The encoder LEVEL decides a re-encode; the encoder's VERSION does not.**
`ENCODER_VERSION` named the encoder *binary*, and the skip check compared it,
so every `flac.exe` / `oxipng` / `cjxl` upgrade re-encoded the whole library —
hours of CPU for a marker nothing reads once it matches. It is **off by default
for all four formats** since this release (Settings → Encoder Tags can turn it
back on per format), the compare is gated on the marker being enabled, and
`ENCODER_QUALITY` — the level — is what the checks ask about. A stored `true`
(the old default's own value, which every saved config holds) follows the new
default exactly once, keyed by `encoder_tags_version_default_moved`, so a
deliberate re-enable later stays. Disabled markers are removed from a file a
re-encode rewrites, so a later re-enable cannot compare a value written under
other settings. Pinned by `tools/test_script_optimizations.py`: a track at
`ENCODER_QUALITY=8` carrying an ancient `ENCODER_VERSION=0.0.1` is **not**
re-encoded (modified=0, markers unchanged), and the same file with the marker
turned on **is** (modified=1, quality rewritten to the target).

**A maintenance run fills metadata; it never replaces what a file already
holds.** Script 13 has no force flag any more — `force_lyrics` re-fetched every
provider for the whole library and overwrote the words the files already had,
which is the owner's "sometimes, I've had lyric tags overwritten". A run now
searches only the tracks that hold no words (embedded or a real `.lrc`
sidecar), re-tries what nobody could answer, and never replaces a stored text;
replacing one track's lyrics is the manual route's job
(`POST /api/lyrics/auto` with `force` — the `replace` parameter of
`fetch_one`). An `INSTRUMENTAL=1` track is never fetched by any caller: the file
states there are no words, so a hit could only be written and deleted again by
the very next pass. Verified over the app's own HTTP API: a forced run left a
stored lyric byte-identical, never touched the instrumental, and searched only
the track that had none.

**A forced pass writes only what changes.** Script 10 re-wrote every tag (and
every `.lrc` sidecar) it found under force — and with `flac_no_padding` on,
every tag write is a whole-file re-encode, so a forced *Run All* re-encoded the
library to store the same bytes. Both passes compare first now: the smoke run's
forced Format All reports "1 formatted, 2 already correct" where it rewrote all
three, and a py-suite check pins that a forced lyrics run leaves a
`LYRICS`-bearing file untouched.

**Script 3's skip path reads the file once, not twice** (one mutagen open now
answers the ENCODER markers AND the seektable state), and `_clean_flac_tags`
generalises the v1.4.2 "remove a disabled marker" rule to all three markers.

The scripts themselves were audited end to end with the repo's own harness:
`python tools/perf_pipeline.py --albums 12 --tracks 5 --build yes` runs all 22
runners through the app's own chain (RUNNERS_OK), reports every script's wall
clock (total **177.08 s**, of which **98.95 s** is the local half — scripts 14
beets 77.82 s and 8 Auto Tagging 92.10 s dominate and are network-bound) and
hashes the library it produced (`12e30df9cc58cfd7aae35084e6944a89c734c1db`,
64 files) so a future change can prove it did not alter what a run writes.

## Verified

- `python tools/check_versions.py v4.4.0` — all 10 copies agree, and the tag
  matches.
- **129 of the 129 `tools/test_*.py` suites pass** in one serial run.
  `tools/test_script_optimizations.py` 103 checks (the new "level only",
  "fills only", remux-memo and PNG-identity cases included); `tools/test_tagindex.py`
  (new, 36 checks: changed/added/deleted files miss, a config change builds
  instead of hitting, a real tag write is on the page the next time it is asked
  for); `tools/test_storage.py` and `tools/test_remux.py` (the memos are
  transparent); `tools/test_lyrics_aliases.py` 139 checks;
  `tools/test_import_corrections.py` (the route's `force` is `fetch_one`'s
  `replace`, the chain's is not); `tools/smoke_runners.py` RUNNERS_OK.
- `python tools/perf_pipeline.py --albums 12 --tracks 5 --build yes` — every
  runner through the app's own chain, with the table above and the output
  manifest `12e30df9cc58cfd7aae35084e6944a89c734c1db` (64 files).
- Browser checks against the built app (`npm run build`): `check_firefox_album.cjs`
  59/59 (Firefox + Chromium + WebKit), `check_lyric_press.cjs` all pass,
  `check_lyrscroll.cjs` 17/17, `check_viz_corners.cjs` 10/10.
- `cd web && npx tsc -b` and `npm run build` clean.
- Two suites carry pre-existing failures that are NOT this release's, each
  reproduced on the pristine tree (source AND built frontend) before this
  release was cut:
  * `tools/check_library_tables.cjs` — "the migrated list replaces the old key"
    and "Explicit matches the payload's own count" fail identically at HEAD
    (the first was already recorded as pre-existing in the 4.3.3 notes).
  * `tools/check_responsive.cjs` — 312/315 on a library whose tracks carry no
    lyrics (the track page's per-line controls legitimately do not exist there)
    and the `/discover` page cannot load without outbound network, which is the
    sandbox this was verified in.
  * `tools/test_plays.py` — the fixture's clock was pinned to a Monday
    (2026-09-21) while one assertion asked the wall clock for "the week that
    contains it", so it went red on 2026-09-28 for everyone, CI included. That
    one assertion now asks the window that contains the fixture (`now=NOW`);
    the fixture date and every other assertion are unchanged.

R329 (the level decides), R330 (a run fills) and R331 (a forced pass writes
only what changes) are in `docs/OPTIMIZATION-GRADING-SPEC.md`, beside R332 (the
ambience draws no straight lines the grain cannot dither) from the visualizer
work, R333 (the album page's column floor is a width every engine computes the
same) and R334 (a page load does not re-read the library, and a write is never
served stale).
