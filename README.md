# la musica

**la musica** (formerly Music Library Optimizer) — a modern, self-hosted web
app that *manages, optimizes, audits, grades and plays* your music library.
Built on the proven `mlo` engine with a React UI: playback of music **and**
music videos (with karaoke-synced lyrics), manual + smart playlists,
favorites, MusicBrainz / LRCLIB / RateYourMusic integration, an offline
player cache, multi-format export, and a Soulseek client with an automatic
MusicBrainz-driven importer.

All app state (config, playlists, favourites, the beets library, the
Soulseek config) lives in a single hidden `.mlo` folder inside your music
directory — app state in `.mlo/data`, with downloads (`.mlo/downloads`) and
trash (`.mlo/trash`) beside it — one folder to back up or carry between
machines.

## Highlights

- **Home** — a sidebar landing page that loads album recommendations from
  your own taste (most-collected artists + most-tagged genres, resolved
  against MusicBrainz release groups you don't own yet) alongside recently
  added, best-graded, rediscover and favourite shelves, with a skeleton
  loading state.
- **Wishes** — save any MusicBrainz release to the library *without*
  downloading it. A background worker re-searches Soulseek for every open
  wish on a configurable interval and auto-imports a release the moment a
  verified rip appears; wishes already present in the library (a manual
  download) resolve themselves. An auto-import search that ends with nothing
  usable can be handed to that same worker instead of failing. See *Wishes*
  below.
- **Library explorer** — artists → albums → tracks with live grade/audit
  badges, search, "fail only" filter, selectable rows with bulk tag tools,
  and sortable/resizable columns (year, title, grade, audit, genre,
  advisory, duration, bitrate, dynamic range, …). Music-video files are
  first-class tracks.
- **Artist / Album / Track pages** — grading and auditing detail, identity
  links (MusicBrainz + RateYourMusic logo buttons on each link's own
  metadata row), the link paste-editor, album + per-track cover upload and
  online cover search, manual tag editing, per-track video tag editing, and
  a full lyrics editor (synced/word-synced ELRC, translations,
  transliterations, hotkeys).
- **Player** — persistent player bar (queue, drag-reorder, shuffle,
  repeat-one, speed, sleep timer, ReplayGain, visualizer, volume shared
  app-wide) plus a **fullscreen player** with animated karaoke lyrics,
  queue and display options. Music videos play fullscreen with
  auto-hiding chrome, correct aspect ratio (no cropping), and every codec
  the bundled ffmpeg can probe (incompatible ones are transparently
  transcoded to fragmented MP4). Browser-fullscreen with a two-stage Esc.
- **Offline cache** — "Download" caches tracks (and video streams) in a
  service-worker media cache; cached tracks keep playing with the backend
  down. "Export" is the real file-saving path.
- **Playlists** — manual playlists (drag-reorder, favorites, .m3u8
  export/import) and **smart playlists** driven by saved grade/audit/tag
  filters. Playlist pages look and behave exactly like album pages, with a
  2×2 mosaic cover built from the first four tracks.
- **Export** — export tracks, albums, artists, playlists or the whole
  library to MP3 (VBR/CBR presets + custom bitrates), Opus, Vorbis, WAV or
  bit-exact FLAC copies, with a size estimate and drive-fit warning.
  Cover art and identity tags travel with the files.
- **Favorites** — liked tracks / albums / artists / playlists, consistent
  with the library views (ctrl-click a track title anywhere to open its
  track page for editing; the player bar title opens it too).
- **MusicBrainz browser** — the sidebar's *MusicBrainz* entry (and Enter in
  the global search box) opens a full MusicBrainz browser: search releases /
  artists / recordings, drill into release groups and releases, and add any
  release to *Wishes* — or match it against the library. Pasting a
  musicbrainz.org link or a bare MBID anywhere jumps straight to that entity.
- **Import** — drag & drop uploads or a watched import folder: MusicBrainz
  release matching, cascading genre import, LRCLIB lyrics fetch, advisory
  ratings, then automatic organize into the naming-script layout.
- **Soulseek** — managed slskd instance (autostart, shares = music folder),
  search & download UI with a live status dot in the sidebar, and an
  **auto-importer** that searches releases by catalog number / artist +
  album, verifies rip logs (minimum logchecker score) and download
  completeness, then imports and organizes the album automatically.
  The query list runs most-specific-first — catalog number, then artist +
  album — and stops the moment one query returns a folder that is both
  complete and lossless, instead of waiting out every template. Each query
  gets a **search window** (`soulseek_auto_search_wait`, Settings →
  *Auto-import*, default 15 s) counted as quiet time since the network's
  last response, and a transfer that moves no bytes for 3 min is abandoned
  rather than tying up the job. While a job runs, the Soulseek page shows
  live per-query progress: elapsed against the window, plus response and
  file counts.
  Search returns every codec the network offers, grouped per shared folder
  and ordered CD rip (log + cue) → lossless → free slot / shortest queue,
  with All / CD rips (log + cue) / Lossless / Lossy chips plus a codec
  filter built from what the results actually contain. The search box keeps
  your last 10 queries in a recent-searches list under it. From any result
  group, **Browse** opens that peer's whole share tree — filterable and
  paged — where a folder can be queued as-is or handed to the
  auto-importer; a browsed folder needs no MusicBrainz release, because its
  audio files become the track list.
  Lossless means the
  files' extensions — M4A/MP4 count as lossless only when the peer reports
  a bit depth, since those containers hold ALAC or AAC. Downloading a
  folder with no lossless audio asks for confirmation first. slskd
  publishes a search's file responses only after the search has ended, so
  the UI polls and shows live response/file counts while it aggregates
  (up to 180 s). Imports convert any lossless source (WAV, AIFF, APE, WV,
  SHN, TTA; ALAC in MP4) into the codec set by `lossless_target_codec`
  *before* the files are named and graded — the same conversion script 3
  applies library-wide. When the search window closes without a usable
  candidate, the job parks and offers to move it to *Wishes* rather than
  reporting the old "no candidate folder contained every track" failure
  (`soulseek_auto_wish_prompt`, default on); see *Wishes* below. The page
  toasts every terminal auto-import state, including "added to wishes", and
  every wish that flips to **Imported** or **Failed**. The *Downloads* tab
  buckets transfers into active, queued, completed and failed, with
  per-transfer **Cancel**, **Retry** on the failed ones and **Clear finished**
  to empty the history.
  The sidebar dot is green when logged into the
  Soulseek network (tooltip names the account), amber when slskd runs but
  isn't logged in (tooltip carries the daemon's own error, e.g.
  `INVALIDPASS`), red when another app's slskd holds the web port, and
  hidden when slskd isn't running.
- **Grading** — a configurable battery of ~45 checks per album (tags,
  encoder identity, naming + capitalization, lowercase extensions, links,
  covers, CUE/log/AccurateRip, lyrics formatting, file categories…). Every
  check can be toggled on the Grading page; the verdict shows as a red/green
  dot on every album and track with the failed checks itemized.
- **Desktop + Web** — served by FastAPI (browser or Docker); a Tauri v2
  desktop shell lives in `desktop/`.

## Optimization: the 15 scripts

Run All executes a configurable order (default shown in parentheses where
it differs). Every script can run individually, on selected albums, or be
forced to redo work.

| # | Script | What it does |
| --- | --- | --- |
| 1 | Format lyrics | Canonical embedded LYRICS / .lrc (padding, blank lines, zero-timestamp rule, Enhanced/Extended LRC word-sync tags) |
| 2 | Format CUEs | Canonical CUE text, FILE-line fixes, CD-N sheet renaming |
| 3 | Optimize FLACs | Re-encode at target level, strip padding/CUESHEET/APPLICATION, remove tags outside the canonical set, convert every other lossless source (WAV, AIFF, APE, WV, SHN, TTA; ALAC in MP4) to the codec set by `lossless_target_codec` losslessly (default FLAC, alternative ALAC — Settings → *FLACs & lossless sources*); `lossless_remove_original` decides whether the pre-conversion file survives a verified conversion |
| 4 | Grade | The full grading battery below |
| 5 | Process images | Covers resized/cropped (default 1200×1200 JPEG q90; per-format size targets for JPEG/PNG/JXL, configurable), JPEG/PNG/JXL optimization, optional JPEG XL conversion |
| 6 | Audit library | AudioAuditor detectors (silence, DR, peaks, LUFS, BPM, MQA, fake stereo…) + CD .log CRC verification → AUDIT tag |
| 7 | DR & ReplayGain | rsgain + simple-dr-meter (album gain, FLAC and MP4 alike) |
| 8 | Auto tagging | ITUNESADVISORY normalization, INSTRUMENTAL-from-lyrics |
| 9 | AccurateRip | .accurip generation via CUETools, checksum verification |
| 10 | Format All | Final canonical pass: accurip/cue/lrc/tag trim + **embedded cover policy** |
| 11 | Remux videos | Any video container (VOB/AVI/WMV/TS/MOV/FLV…) → MKV, video copied bit-exact when possible, every audio stream re-encoded to FLAC, subtitles copied |
| 12 | Key & BPM | librosa-backed INITIALKEY + BPM (musical/camelot/openkey notation) |
| 13 | Fetch lyrics | LRCLIB download into the configured format (embedded / .lrc / both) |
| 14 | Beets tagging | Managed beets (Picard parity) with the naming script, genre import, work/movement tags |
| 15 | Lyrics xlit / translate (AI) | Any OpenAI-compatible endpoint: romanization (TRANSLITERATION-*) and translation (TRANSLATION-*) tags + `<lang>.lrc` sidecars, cached per track |

### Embedded covers (new in 2.1.0)

**By default the optimizer does not embed cover art — it removes it.** Audio
files stay lean; covers live on disk as `cover.*` (and per-track sidecars),
which every player can read. Settings → *Embedded covers* flips the policy:

- **Embed covers into audio files** (off by default). When on, script 10
  embeds the album cover into every track (FLAC picture, MP3 APIC, MP4
  `covr`, OGG/Opus `METADATA_BLOCK_PICTURE`), replacing any existing art;
  script 3 stops stripping the FLAC PICTURE block.
- **Embedded JPEG quality** — applies only when the embedded image is a
  JPEG (PNG/lossless embeds ignore it).
- **Embedded cover max resolution** — longest side in px, downscaled with
  the aspect ratio preserved; 0 keeps the cover file's own size.

The pass is idempotent — it only rewrites files whose embedded art actually
changes.

### Cover compression

Covers are normalized by script 5 (Settings → *Images*):

- **Resize** to `cover_target_size` px (default 1200) when
  `cover_resize_enabled` is on, and **crop to square** when
  `cover_crop_enabled` is on. `cover_force_exact_size` crops regardless of
  aspect deviation, so the output is exactly *target*×*target*.
- **Re-encode** as JPEG at `cover_jpeg_quality` (default 90 %). Other images
  keep their own quality setting; covers are the only ones re-encoded at 90.
- **Per-format size targets** — `cover_jpeg_target_size`,
  `cover_png_target_size` and `cover_jxl_target_size` override the global
  target for that one format; 0 means "use the global target".

### Filenames, capitalization and extensions (new in 2.1.0)

The app now enforces canonical file naming in all three ways:

- **Check** — the organizer's preview and the naming-script grading compare
  every track's full path (folders included) against the configured naming
  script.
- **Grade** — new checks: *Path capitalization* (a path that matches the
  naming script except for letter case — `TOXICITY` vs `Toxicity` — fails)
  and *Lowercase extensions* (`01 - Song.FLAC` fails). Both toggleable on
  the Grading page, both on by default.
- **Optimize** — organize applies the naming script's exact capitalization
  and lowercases every extension it touches (audio files, same-stem
  sidecars, and leftover files like covers/logs). Case-only renames work on
  case-insensitive filesystems too.

## Home & recommendations (new in 2.2.0)

The sidebar opens on **Home**: library stats plus shelves of albums.

- **Recommended for you** — seeded from the artists you collect most and the
  genres tagged most across your tracks; each seed is resolved against
  MusicBrainz release groups you don't already own. Results are TTL-cached
  and never re-hit the network on repeat views.
- **Recently added**, **Best graded**, **Rediscover** (random library slice)
  and **Favorites** — all owned albums, click-through to the album page.
- Recommendations that aren't in the library link to the MusicBrainz browser,
  where a click saves them as a wish.

Settings → *Home* controls whether MusicBrainz recommendations are included
and how many albums each shelf shows.

## Wishes — releases that aren't downloadable *yet* (new in 2.2.0)

Some albums simply aren't on Soulseek right now. A **wish** records a
MusicBrainz release identity without downloading anything, so it can be
filled in automatically later:

1. Open a release in the MusicBrainz browser and press **Add to wishes** (or
   paste a release ID/URL under Soulseek → *Wishes*).
2. A background worker re-searches Soulseek for every open wish on the
   configured interval (default every 6 h), running the same
   find → verify-logs → download → audit → import pipeline as the one-shot
   auto-importer. Auto-import ranks lossless folders ahead of lossy ones:
   when no lossless candidate turns up, an interactive run parks and asks
   *Only lossy copies found — download anyway?* on the Soulseek page, while
   a wish-filling run never takes a lossy copy — it leaves the wish open
   for a lossless match.
3. When a verified copy is found the release is imported and organized, and
   the wish flips to **Imported**. Wishes whose release already exists in the
   library (e.g. a manual download) are reconciled too — the **Sync library**
   button does this on demand.
4. Each wish has **Search now**, per-wish notes and attempt tracking; the
   whole list shares one searchable activity log.

Settings → *Wishes* controls the master switch, interval, per-wish attempt
cap and auto-import.

New in 2.3.0, the end of a fruitless search: an auto-import job whose search
window closes with **no usable candidate** parks instead of failing and asks
whether to move the job to wishes. Accepting creates a wish carrying the same
search queries, so the background worker keeps looking with no further input;
declining keeps the old *no candidate folder contained every track* failure.
The prompt appears after the configured time — the search window
(`soulseek_auto_search_wait`, default 15 s) plus the 45 s grace tail, about a
minute by default — and there is deliberately no second timer.
`soulseek_auto_wish_prompt` (default on) switches the prompt off; searches the
wishes worker itself started never prompt.

Completion is notified by toast: an auto-import job reaching a terminal state
(including *added to wishes*), and a wish flipping to **Imported** or
**Failed**.

## Grading — what the checks cover

Every check is toggleable on the Grading page, which also offers
**Strict / Balanced / Relaxed** presets, a live check filter, and
enable-all / disable-all bulk actions.

- **Tracks & albums** — unreadable files, required per-track and album-level
  tags, encoder identity tags, naming-script match, path capitalization,
  lowercase extensions, INITIALKEY + BPM, excess tags, media/source tags and
  their consistency, instrumental/lyrics consistency, disallowed file types,
  stray images, un-remuxed videos, disc folder naming, CD requirements
  (.log exact match, .cue, FLAC lossless, CRC checksums).
- **Auditing** — AUDIT tag presence, log checksum validity, AccurateRip
  verification, log grade within a configurable threshold.
- **Identity links** — the MusicBrainz release (or release group) and the
  RateYourMusic release page must be tagged. Artist/recording-level links
  remain optional.
- **Covers** — presence, size/square/crop rules (tolerances configurable),
  per-track sidecar covers under the same rules.
- **Strict formatting** — tag padding/blank lines, lyrics canonical form,
  CUE canonical form.
- **Lyrics** — presence, transliteration/translation (only when AI is
  configured and the script needs it).
- **File categories** — which file types participate in grading at all
  (music, covers, CUE, log, LRC, accurip, videos, other).

### AudioAuditor override

A track's audit verdict can be forced from the track page: **REAL** / **FAKE**
writes the `AUDIOAUDITOR_OVERRIDE` tag, while *Auto* clears it and hands the
track back to AudioAuditor. The override wins over every derived verdict — it
is applied last, so the album-level all-real gate agrees with it, and a forced
re-audit reproduces the user's call instead of erasing it.

## Library layout — a read-only report

`GET /api/library/layout`, surfaced on the **Optimization** page, walks the
whole music folder — root, `Artists/`, every artist folder, every album
folder — and reports where the canonical
`<music>/Artists/<Artist>/<Album>/<files>` shape is not met: misplaced audio,
unexpected folders and subfolders, empty album folders, stray files (including
a file sitting directly in `Artists/`), hidden folders, and leftovers from the
old `.mlo_data` layout.

The scan never moves, renames or deletes anything; acting on the report is
what **Organize** and the scripts are for.

New in 2.3.0 it also reports **`wrong_case`** — an artist folder, album folder
or file name whose stored capitalization differs from the naming script's
expectation. The comparison is case-sensitive, which works because
`os.listdir` returns the stored casing even on Windows' case-insensitive
filesystem. Each row hints at running Organize; the scanner itself never
renames.

## Cover finder

The album/track online cover search is a meta-search over the musichoarders
providers with selectable sources and a storefront **region**
(`cover_country`, default `us`). The chosen source list and region can be saved
as the default (`cover_sources`), so the next search starts from the same
choices. `GET /api/cover/search` runs the search against the catalogue,
`GET /api/cover/sources` lists the selectable sources, regions and the saved
defaults, and `POST /api/cover/fromurl` saves a chosen result to disk.

## Downloads viewer

Releases downloaded to the staging folder `<music>/.mlo/downloads` are listed
by `GET /api/downloads` (newest first); `POST /api/downloads/import` moves
entries into the library as albums and `POST /api/downloads/delete` removes
them. The Soulseek page's *Downloads* tab shows the same entries as transfers
bucketed into active, queued, completed and failed.

## Getting started

```bash
# backend
python -m pip install -r server/requirements.txt
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000

# frontend (dev, http://localhost:5173 proxies /api to :8000)
cd web && npm install && npm run dev
```

Production build is served by the backend automatically (`web/dist`):

```bash
cd web && npm run build
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

Set your music folder in **Settings** (or point `MLO_MUSIC_FOLDER` at it).
Install the external toolchain from Settings → Dependencies (ffmpeg, flac,
libjxl, oxipng, rsgain, AudioAuditor, Logchecker, CUETools, librosa, beets,
slskd). The UI walks you through the first-run setup.

## Docker

```bash
docker compose up --build
# open http://localhost:8000 — mount your music under ./music
```

The image bundles the React build and the audio/image toolchain
(`ffmpeg`, `flac`, `libjxl`, `jpegtran`). `oxipng` is installed when the
base image provides it and otherwise fetched at runtime from Settings →
Dependencies (a silent `|| true` in the Dockerfile keeps the build alive).

## Architecture

```
web/         React 19 + TypeScript + Tailwind UI (Vite, service-worker media cache)
server/      FastAPI backend: library payload, playlists, integrations,
             import, streaming (direct + on-the-fly transcode), export,
             organize, WebSocket progress
mlo/         core engine: grader, audit, flac, images, lyrics, cue,
             accurip, loudness, autotag, remux, naming, discs, stats
desktop/     Tauri v2 desktop shell
tools/       test-library generator and test suites
```
## API overview (selected)

| Endpoint | Purpose |
| --- | --- |
| `GET /api/library` | tag-rich library tree (grades, audits, tags, tech info; gzipped) |
| `GET /api/library/layout` | read-only layout scan: misplaced audio, unexpected folders, empty albums, stray files, hidden folders, `wrong_case` |
| `GET /api/home` | Home page: stats, recommendations, recent/top/favorite shelves |
| `GET/POST/PATCH/DELETE /api/wishes` | release wishlist CRUD; `POST …/{id}/search`, `…/search-all`, `…/reconcile` |
| `GET /api/album` `GET /api/artist` | entity details |
| `GET /api/stream` `GET /api/videos/stream` | audio/video streaming (Range; `?transcode=1` pipes fragmented MP4) |
| `GET /api/videos/meta` | codec probe deciding direct play vs transcode |
| `GET /api/tags` | per-track tag/lyrics/cover read view |
| `POST /api/tags/bulk` `POST /api/videos/tag` | bulk tag surgery; music-video tag writes |
| `POST /api/lyrics/embed` `POST /api/lyrics/write` | embedded LYRICS / .lrc sidecar writes |
| `POST /api/run` | run any of scripts 1–15 on targets |
| `POST /api/organize` | apply the naming script (dry-run supported) |
| `POST /api/export` | multi-format export with codec/bitrate config |
| `GET/POST /api/playlists…` | manual + smart playlists, .m3u8 |
| `GET /api/mb/release?mbid=…` `GET /api/mb/release-genres?mbid=…` | MusicBrainz release + genre cascade |
| `POST /api/mb/match` `POST /api/mb/assign` | track/disc matching, MB/RYM/genre/advisory writes |
| `GET /api/lyrics/*` `POST /api/lyrics/write` | LRCLIB proxy + LRC sidecar write |
| `POST /api/cover` | album cover upload (`?track=` writes per-track sidecar covers) |
| `GET /api/cover/search` `GET /api/cover/sources` | cover meta-search (sources + storefront region) and the selectable catalogue / saved defaults |
| `POST /api/cover/fromurl` | save a chosen search result as the cover |
| `GET /api/downloads` `POST /api/downloads/import` `POST /api/downloads/delete` | staged `.mlo/downloads` entries: list newest-first, import as albums, delete |
| `POST /api/import/upload` `…/commit` | upload + link assignment |
| `POST /api/lyrics/ai` `POST /api/lyrics/ai/lines` | AI lyric transforms (cleanup, repair, translate, transliterate; line-aligned for the player) |
| `WS /ws/progress` | live progress |

## Tests

```bash
python tools/make_test_library.py   # synthetic library for end-to-end runs
python tools/test_remux.py          # video remux suite (VOB/MKV/AVI/WebM fixtures)
python tools/test_script_menus.py   # every script menu agrees (numbers, labels,
                                    # Run All order, force switches) — the gate
                                    # for adding a script anywhere
python tools/smoke_api.py           # route smoke test against a running backend
                                    # (python tools/smoke_api.py http://127.0.0.1:8000)
```

The browser-side check (`tools/check_menus.cjs` — every sidebar entry and
route renders with no page errors) needs a running backend and Playwright
(`npm i -D playwright`).

The other `tools/test_*.py` suites cover config migration, CUE disc renaming,
grading paths, Home shelves, lyrics merge/repair, the Soulseek client and the
layout scanner's capitalization reporting (`test_layout_case.py`).
Run them all before a release. The frontend gate is `cd web && npx tsc -b &&
npx oxlint && npm run build`. CI (`.github/workflows/ci.yml`) runs the Python
suites and that frontend gate on every push and pull request; the suites that
need a live backend (the browser check and `smoke_api.py`) are manual.

---

Legacy v1 (Tkinter app, CLI, PyInstaller/Inno packaging) is archived on the
`archive/legacy-v1.7` branch.
