# la musica 4.5.0 — the pages that stop rebuilding, the alias that has to earn its tag

Five changes, each of them a measurement: a page load no longer waits for a
walk, a tag write is only a write when the value changes, alias tags are graded
in BOTH directions, a disc subfolder is one album, and LRCLIB's proof-of-work
leaves the request thread.

## Pages: served from memory, re-derived behind them

The library tree every page's first paint asks for was a hard 60-second TTL
with a **blocking** rebuild — the request that found it expired paid the whole
walk and every album row in it. On the owner's install (170 files, bind-mounted
library, Docker Desktop on Windows, warm reads 40-60 ms) that showed up as
**0.9 s, 5.5 s and 14.5 s rebuilds**, twice inside one minute of otherwise warm
reads. Now:

- `tagcache.get_library` serves the cached tree as it stands when it is merely
  old OR when a write marked it dirty, and rebuilds on a background thread
  (single-flight, stamped after the build). The only blocking build left is a
  cold process's first paint.
- The tree is **warmed at startup** (`_lifespan`'s `library-warm`, beside
  4.4.1's `storage-warm`): 11.3 s cold walk → **0.13 s** on a 320-file scratch
  library.
- **The library-state stamp is per album.** `tagindex.dir_signature` used to
  append one library-wide stamp of the four app-state stores, and the import
  chain rewrites those once per album (Audit library, AccurateRip, Process
  images) — so every one of those writes made EVERY album's row unreachable.
  That is where the 14.5 s rebuilds came from. `tagindex.state_stamp(album_dir,
  cfg)` reads each store's own share: entries under the album, entries the
  album sits under, and the whole file only for a store whose records state no
  path at all.
- **An in-app write drops only what it wrote.** 36 of the 45
  `tagcache.invalidate_all()` call sites are now `invalidate_album(...)` over
  the folders the operation actually touched; the 9 that stay global are a
  settings save (it changes the meaning of every cached entry), the explicit
  Refresh, and the "the affected set is unknown" fallback.

Measured after the change on a 320-file library: a genre import that rewrote 8
files was followed by `/api/library` in **0.068 s** (was 2.7-14.5 s), and the
album page answers in 8-46 ms.

## Writes are changes

A container write is a whole-file rewrite on FLAC/MP3/MP4, and the genre import
set the tag unconditionally: **every run rewrote every track of the album**
(8 tracks: 2.0 s) and moved their mtimes — which is what made the pages after
an import re-read the album. `mlo.autotag.genre_plan` is now the one decision
(it canonicalizes and caps through the shared `normalize_genres`, then compares
against what the CONTAINER holds verbatim, so another tagger's `"; "`-joined
value is still rewritten into repeated fields), `genre_apply` the one applier,
and `server.main._write_album_genres` runs ONE pass per file — the second loop
that re-opened every file it had just written is gone — in parallel across
files. Verified over HTTP: a second identical `/api/genres/import` reports
`updated: 0` and moves **0** mtimes; the first writes 8 files in 40 ms.

## Alias tags: written only where needed, graded both ways (issue #75)

A Latin name never gets an alias tag again. `server.integrations.alias_required`
(a name needs one only when the configured `locale` cannot read its script) and
`alias_for` (still the one picker) gate the writes, so `mlo.autotag` writes AT
MOST ONE alias value per entity — the whole per-locale fan-out
(`ARTISTALIAS-JA` on Radiohead) is gone. Grading now fails in BOTH directions:
`grade_check_alias_needed` (extended to `ALBUMALIAS` and made locale-aware)
fails a name that needs an alias and has none, and the new
`grade_check_alias_excess` fails one that has an alias it does not need, a tag
spelled for another locale, a second spelling of the same alias, or an alias
that just repeats the name. The strip pass (script 3 / script 10) clears
exactly what the excess check reports, the import can stamp what it should, and
the spec's R16a/R16b carry the rule (README's alias bullet too).

## A disc subfolder is one album

`mlo.discs` owns the ONE disc vocabulary (`CD1`, `Disc 2`, `Disk1`, `Volume 1`,
`DVD 2`, `CD1 [FLAC]`, a bare `1`/`2` only as part of a numbered set starting at
1) — the search's candidate trees, the download mover and the layout scan all
call it, and no module keeps its own disc regex. A folder whose album-bearing
children are its discs is ONE album (a stray log/cue folder or art rides along
instead of splitting off), `server.soulseek.disc_parent` is that rule once (the
two nested copies are gone), and a file's DISCNUMBER is recorded fill-only from
its own path before the naming script runs — so an untagged `CD1/`+`CD2/`
release organizes into the flat `1-01 …` shape with the discs intact, instead
of collapsing onto one set of names. Pinned by `tools/test_soulseek_api.py`
with fail-first evidence (before: the release read as THREE albums and the
numbered rip as TWO).

**And a disc whose rows and files are numbered in different conventions now
lines up (R341).** A release numbered CONTINUOUSLY — disc 2 = tracks 14-26,
what the CD's TOC states and what this app writes as `2-14 …` — against a
manifest numbered PER DISC (disc 2 = tracks 1-13, what MusicBrainz states)
shares no `(disc, position)` key on that disc, so every row of the second disc
read "**not imported**" beside the files that were right there. On the owner's
own library (The Wall, US CD C2K 36183: 13 rows, 13 files) that was 13 false
"not imported" on disc 2 alone. `mlo.paths.expected_tracks_state` now takes the
album's audio as rows (disc, track number, TITLE) and aligns a disc's rows and
files BY ORDER when they share no position — and only then, and only when the
counts match and every title both sides state agrees, so a different tracklist
still reads as missing rather than being renamed into place. Measured: 13 of 13
present with the rule, 0 of 13 without it, 13 of 13 still missing when the
titles are bent.

**And the same convention mismatch in the rip log (R342).** EAC numbers a
*disc's* tracks 1..N, so `CD-2.log` states rows 1-13 for a disc whose files are
`2-14 … 2-26` — which graded as "Track not covered by .log CRC (unverifiable CD
rip)" on all 13 files beside a log that covered every one of them. `mlo.discs.
log_crc_map` is now the ONE matcher (the grader's CRC coverage and value pass,
and the audit's `verify_album_checksums`): by track number first, and by ORDER
only when the two share no number at all, guarded by equal counts and by the
log's own TOC playtime against the file's within `TOC_TOLERANCE_S`. Measured on
The Wall's own `CD-2.log`: **13 of 13 matched where the old rule matched 0**;
a partial overlap stays uncovered, and a TOC that names other tracks refuses
the alignment (both pinned in `tools/test_grading_paths.py`).

## The fingerprint never picks a release by itself

The wizard's automatic detect reads the album's **tags**; an AcoustID match is
a release GROUP with many editions, so it fills the release field only when
**Match from fingerprint** — the new button beside the field — is pressed, and
it then ends in the same `useAcoustidRelease` → `pickRelease` flow a pasted id
takes (one writer, and the accepted match files its `ACOUSTID_ID` /
`ACOUSTID_FINGERPRINT` pair). `import_acoustid_autofill` (Settings → Import,
**off**) is the opt-in that lets that match run automatically at the Links step;
unattended Soulseek auto-imports keep matching on their own under
`import_acoustid`. Spec: R340.

## LRCLIB's proof-of-work leaves the request thread

Every LRCLIB submission needs a fresh single-use token whose challenge is
sha256 in a tight loop over a 40-byte input — **which holds the GIL**: measured
here, six threads reach 1.13x the throughput of one, so script 18's own lanes
cannot overlap it and a 26-track album costs ~6.5 minutes whatever the lane
count (the owner watched "7/20 Publish Lyrics" for five). `PublishTokenPool`
solves each challenge on a spawn-context worker instead: on one saved set of
eight challenges, **120.4 s in-thread against 50.8 s through the pool**, and a
run's cost becomes max(hardest, sum / workers). No spawn (a frozen build
without `freeze_support`, a sandbox) leaves the old path in place; a one-off
manual publish still solves in its own thread.

## Small ones

- The import wizard's advisory step says "Re-rating 26 track(s)…" — the
  "asking the sources anyway" tail is gone.
- **Done** on the import-complete screen opens the album it finished (the
  duplicate "Open album" button is gone; a run with no album path still just
  closes the wizard).

## Verified

- `tools/test_*.py`: **all suites pass** (the sweep runs every one of them,
  serially, with a timeout).
- `tools/test_tagindex.py` gained the tree's serve-and-refresh behaviour, the
  in-app-write refresh, and the per-album state stamp.
- `npx tsc -b` clean; `python tools/check_versions.py v4.5.0` — all 10 copies.
- Live measurements on the owner's own install and on a 320-file scratch
  library, quoted above and in `docs/OPTIMIZATION-GRADING-SPEC.md`
  (R334/R338/R339, R325, R340, and the disc rules R335-R337).
