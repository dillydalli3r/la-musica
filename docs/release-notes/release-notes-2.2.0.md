# la musica 2.2.0

An import now ends with the release's own tags, MusicBrainz freshness is a
first-class feature, Home is for listening, and a drop anywhere opens the
import wizard.

## An import's last word is the release

The owner's report: an album tagged `12" Vinyl` was re-pointed at its CD
release in **Import & Link**, the chain ran with **Force**, and MEDIA stayed
Vinyl. The link writer only wrote the link; the release identity was an
import-only step the wizard's Finish/Run all path never reached.

- **Saving a link now stamps the release too.** `POST /api/import/commit`
  takes the release payload the wizard already fetched and writes the release
  identity with it — MEDIA, label, catalogue number, country and the per-track
  ids — in the same write. No second MusicBrainz request.
- **Every import refreshes at the END, forced or not.** `finish_album` ends by
  re-reading the release the album *names* and stamping it (MEDIA is the one
  value a refresh may replace; everything else stays fill-only, so a typed
  catalogue number survives). The wizard's own **Run all** / **Done** paths go
  through the new `POST /api/import/refresh`, so the same rule covers a run
  that never touches the import pipeline. An album with no MusicBrainz link
  says *skipped* with the reason — no release is invented.
- Verified on a scratch album: a copy tagged `12" Vinyl` and pointed at a US CD
  release came out `MEDIA=CD`, `MUSICBRAINZ_ALBUMID` = the CD release, and a
  hand-typed `CATALOGNUMBER` stayed the user's.

## MAINTAIN → Tags: MusicBrainz freshness, in the files

- Every writer that lands MusicBrainz data (script 8's per-track pass, the
  import's release stamp, a scheduled refresh) writes
  **`MUSICBRAINZ_UPDATED`** — ISO-8601 UTC — on the file. No SQL table: a moved
  or restored library keeps its history.
- An album's age is its **oldest** track's stamp; one missing stamp makes the
  whole album due, and a refresh updates the WHOLE album (the release is read
  once and every file is stamped).
- The sidebar has a **Tags** page under Maintain: library totals, per-artist
  rollups, per-album rows that expand to every track's own pull date, an
  on-demand **Refresh due** / **All linked**, and per-album **Refresh**.
- **Fast by construction, which matters on a large library.** Each file is
  opened once and gets its identity values and its pull stamp in ONE container
  save; a file already stamped within the hour with nothing to change is not
  rewritten at all; and finishing a job does not rebuild the library tree — the
  page takes the job's fresh stamps from memory while the per-album caches mark
  the tree for its normal stale-while-revalidate pass. Measured on a scratch
  album: 0.17 s first refresh of 3 tracks, 3 ms for an immediate repeat, Tags
  read 0.24 s. The queue is bounded by `tag_refresh_workers`.
- Scheduling is opt-in: `tag_refresh_enabled` (off by default),
  `tag_refresh_days` (7), `tag_refresh_workers` (4) and
  `tag_refresh_interval_minutes` (60). The scheduler never starts the whole
  library at once: albums go through a bounded queue (workers, default 4),
  each under the library's own per-album claim, and a second job is never
  started beside a running one. Album pages carry a chip with the oldest pull
  and a *due* mark.

## Home is a listener's page

Home is now built from what people listen to:

- **Most played tracks / albums / artists / playlists** (the play store; a play
  now records which playlist its queue came from).
- **Genres** and **MOOD** lanes, ranked by plays, each with a few records.
- **Your stars**: rated tracks, albums and artists.
- Then recently added, favourites, podcasts and a rediscovery slice.

The maintenance shelves left it — no grade strip, needs-attention, pending
albums or most-collected-artists; the Library, Grading and In-progress pages
own those questions. A shelf with nothing real behind it is not drawn.

## Browse is gone; smart playlists live in Playlists

The sidebar has no Browse entry. The Playlists page's **Build rules…** opens the
same query builder and saves the same `{conditions, match}` spec a smart
playlist stores, so one query model serves the rule editor and the playlist
page.

## Less at once, and playback stays smooth

- A play press queues a **window** (40 back / 260 ahead) instead of mapping the
  whole library: three copies of the library per click, including the persisted
  restore record, which now also stores a window. Measured on a 368-track
  scratch library: the tracks table renders 301 rows, a click persists a
  261-row queue (~70 KB), and the click settles in well under a second.
- The player warms the **next two** queue rows through the same
  `playbackSource` the audible load uses, so a skip or a gapless handover
  starts on bytes already fetched. Scripts and tag refreshes hold per-album
  claims and write through the existing shared-read/atomic-swap path, so a
  write cannot interrupt the song that is playing.

## A drop anywhere is an import

Files, folders (nested included) and archives dropped on **any** page are read
during the drop event (the browser API goes stale afterwards), carried across
the route change, and handed to the wizard's own album detection. The desktop
shell's OS drop reports paths and takes the same route. A "Drop to import"
overlay says what will happen.

## Recommendations only when they make sense

A candidate now qualifies on a **strong listening signal** — a shared specific
genre, the same MOOD, energy within 25, or the same artist — never on era or a
broad family alone; a score floor catches pairs several weak terms agree on.
Fewer than two qualifying rows means the shelf is **empty** rather than padded.
Queue extensions ask for their own minimum, so one good row still keeps the
music going.

## Also

- The Library card's disambiguation comment no longer truncates the artist
  name, and "Recommended (Local)" is simply "Recommended" (2.1.1 carried these;
  this release keeps them).
- All script force switches stay in step with the script registry (16
  one-shot flags, pinned by `tools/test_script_menus.py`).
- New suite `tools/test_tag_refresh.py` covers the stamp arithmetic, album
  rollup, due rule, queue/claim behaviour, the scheduler's off state, and the
  writer. `tools/test_home.py` now pins the listener payload.

Contract: spec **§7.86 / R447–R453**. Version 2.2.0 across all twelve copies.