# Build prompt — `la musica` TUI, native Rust

Hand this file to a coding agent as the whole brief. Everything it needs is
here: the domain, the on-disk contract, the feature list, the rules that decide
verdicts, and the acceptance tests. Where it names a behaviour, that behaviour
is required, not illustrative.

---

## 0. Mission

Build **`mlo`** (working name; binary `mlo`, crate `mlo-tui`): a single-binary
Rust terminal application and scriptable CLI that owns a music library end to
end — **acquire, identify, tag, organize, analyse, grade, optimize, play** — with
the same features as the `la musica` web/desktop app, re-implemented natively in
Rust. No Python, no Node, no server, no browser. One binary you can drop on a
machine, point at a folder, and run.

Deliverables:

1. `mlo` — TUI (default when stdout is a TTY) and CLI (`mlo <command>` when it is
   not, or with `--cli`). Same engine, same config, same results.
2. `mlo.toml` — config, plus a `library.json`-compatible state dir so an
   existing `la musica` library keeps working (see §3.6).
3. Tests (§14) that run headless with no network and no audio device.

Non-goals: a GUI, a web server, mobile, multi-user/remote streaming. A TUI and a
scriptable CLI are the whole surface.

---

## 1. Non-negotiables

These override convenience everywhere else in this document.

1. **Native Rust for the work.** Decoding, tag reading/writing, fingerprint
   handling, ReplayGain/DR, BPM/key, image work, archive handling and the
   MusicBrainz/AcoustID clients are Rust code paths. External binaries are
   allowed only where no credible crate exists — `fpcalc`-compatible
   fingerprinting (§8.3) and a `.rar` extractor (§7.2) — and both must degrade
   with a named reason, never a crash.
2. **Nothing is ever deleted.** Every removal moves the file/folder to
   `<music>/.mlo/trash/<user>/<timestamp>/` with an origin manifest that can put
   it back. There is exactly one trash implementation and every destructive
   operation goes through it.
3. **Every write is atomic.** Temp file beside the destination, flush, fsync,
   one rename. A killed process leaves the old file or the new one, never a
   half-written one. This applies to tags, sidecars, the database and the config.
4. **Never invent metadata.** A value that no source stated is not written. A
   provider answer is written as stated (including `0`); an inference from the
   audio (mood, energy, key, BPM) is written only by the script that computes it.
5. **Deterministic paths.** One album yields exactly one path, always. Multi-
   value `RELEASECOUNTRY`/`LABEL`/`MEDIA` keep their first value.
6. **A verdict is binary.** PASS or FAIL. No letters, no partial grades. An
   issue that is displayed must cost a failed check — a green row with a problem
   under it is a bug (§9.2).
7. **Offline is a normal state.** With no network the app still browses, plays,
   edits tags, organizes and grades from local data; every network feature
   reports *unavailable* with the reason. With the *service* down (§10) the app
   still starts, still plays what is cached, and still queues.

---

## 2. Domain model

```
Library            the music folder the user configured (one per config)
Artist             a folder directly under <music>/Artists
Album              a folder directly under an artist folder
Track              an audio file inside an album folder
Release            the MusicBrainz release an album is an instance of
Disc               a CD1/Disc 2 folder inside an album; VIDEO_TS/BDMV is a
                   disc STRUCTURE to remux, not a disc folder
Tool               an external program the app can install and/or use
Script             one of the 24 named passes (appendix A)
Run                one execution of a script over a scope
Job                a long operation with progress, cancellable, resumable
```

Scopes: a **track**, an **album**, an **artist**, a **selection**, or the
**library**. Every script, every grade and every job takes one of these; a
library-wide pass is never a side effect of an album action.

---

## 3. On-disk contract

### 3.1 Canonical layout

```
<music>/Artists/<Artist>/<Album>/<files>
```

- An artist folder holds album folders, plus the artist's own
  `artist.jpg`/`artist.png` and `description.txt` — nothing else.
- An album folder holds audio, `cover.*`, `description.txt`, and sidecars
  (`.lrc`, `.cue`, `.log`, `.accurip`), plus optional disc folders.
- `<music>/.mlo/` is the app's own state and never library content:
  `data/` (index, caches, reports), `tools/` (installed tools),
  `downloads/`, `incomplete/`, `trash/`.

### 3.2 Naming template (default, overridable)

```
%albumartist% [%musicbrainz_albumartistid%]/$if(%releasetype%,[%releasetype%] ,)$if(%originaldate%,%originaldate% - ,)$if(%date%,%date% - ,)%album% {$if(%releasecountry%,%releasecountry%)$if(%media%,$if(%releasecountry%, - ,)%media%)$if(%catalognumber%,$if(%media%, - ,$if(%releasecountry%, - ,))%catalognumber%)}$if(%label%, [%label%])$if(%musicbrainz_albumid%, [%musicbrainz_albumid%])$if(%musicbrainz_releasegroupid%, [%musicbrainz_releasegroupid%])/%discnumber%-$num(%tracknumber%,2) %title%$if(%musicbrainz_trackid%, [%musicbrainz_trackid%])$if(%musicbrainz_releasegroupid%, [%musicbrainz_releasegroupid%])
```

Rules that must hold:

1. The path (folders included) equals the evaluated template.
2. `short_folder_names` truncates each UUID group to 8 characters; **grading
   accepts both spellings**, and switching the flag never fails a library by
   itself.
3. Letter case is part of the contract: the folder depth is compared
   case-sensitively against the template's spelling; a case-only difference is
   `PATH_CASE`. File extensions are lowercase.
4. Illegal characters (`< > : " \ | ? *`) become `_`; empty `[]`/`{}` groups left
   by omitted conditionals are removed; whitespace runs collapse; trailing dots
   and spaces are stripped.
5. A missing tag with a cold cache is a wildcard and is reported as *Missing
   X tag*, never as an invented path — and never triggers a network call by
   itself.

### 3.3 Sidecars

| Name | Meaning |
|---|---|
| `cover.<ext>` / `folder.<ext>` | album cover (front) |
| `description.txt` | album or artist description (UTF-8) |
| `artist.jpg` / `artist.png` | artist image (artist folder only) |
| `*.lrc` | lyrics, synced or plain |
| `*.cue` | CD layout, written in one canonical form |
| `*.log` | rip log (EAC/XLD), graded and checksummed |
| `*.accurip` | AccurateRip evidence |
| `.mlo_expected.json` | release tracklist manifest (missing/extra tracks) |

A numbered copy of a sidecar (`description (2).txt`) is its own finding: renamed
when the canonical name is absent, moved to Trash when it is present.

### 3.4 State

- Config: `mlo.toml` (see §12.3); an existing `config.json` from `la musica` is
  read and migrated on first run, never rewritten in place.
- Index: SQLite at `<music>/.mlo/data/index.db` (schema §12.2).
- Layout report: `<music>/.mlo/data/layout-report.json` — ONE report describing
  the whole library, written only by a library-wide scan.
- Trash manifests: `<music>/.mlo/trash/<user>/<stamp>/manifest.json`.
- Logs: `<state>/logs/mlo.log`, rotating, plus a per-run journal.

### 3.5 Interrupt recovery

On start: sweep the app's own temp files, reconcile albums whose audio arrived
without a finishing pass, report abandoned jobs by name. Never guess: say what
was found and what was done.

### 3.6 Compatibility with an existing `la musica` library

Read, do not convert: `.mlo/data/config.json`, `auth.db` (ignore),
`beets-library.db` (ignore), `events.jsonl` (ignore) and the `.mlo/tools`
folders. The canonical layout and the sidecar names above are the contract; a
library written by the other app must scan, grade and optimize identically.

---

## 4. Feature set A — library and layout

### 4.1 Scan and index

One walk builds the index, with per-file mtime+size reuse from the last walk.
The index carries: artist, album, track, path, tags, durations, hashes, sidecar
presence, artist image/description presence, grade results, layout findings.
Concurrent edits by other tools are detected by mtime and re-read, never trusted
from the cache.

### 4.2 Layout audit (script 20)

ONE scan answers every surface (the TUI panel, the CLI `mlo layout`, the stored
report). Findings, with the fix the apply may perform:

| Kind | Meaning | Apply |
|---|---|---|
| `audio_at_root` | audio file directly in `<music>` | move into the album its tags name |
| `audio_in_artists` | audio directly in `Artists/` | move into its album folder |
| `audio_in_artist` | audio directly in an artist folder | move into its album folder |
| `unexpected_folder` | foreign folder in `<music>` root | Trash **only** if it holds no audio |
| `unexpected_subfolder` | folder inside an album that is neither a disc folder nor holds audio | Trash |
| `empty_album` | album folder with no audio anywhere beneath | Trash |
| `empty_artist` | artist folder with no album folder **and no sibling for the same artist** | Trash |
| `split_artist` | **two folders that name one artist** (see §4.3) | merge (see §4.3) |
| `wrong_case` | name differs from the naming script by case only | rename |
| `sidecar_copy` | duplicate/numbered sidecar | rename or Trash (§3.3) |
| `stray_file` | non-audio, non-sidecar file inside an album | Trash |
| `stray_in_artists` | non-audio file directly in `Artists/` | Trash |
| `hidden_folder` | hidden folder inside `Artists/` | report only |
| `legacy_state_file` | leftover of the old `.mlo_data` layout | Trash |

Rules:

1. **Re-derive every removal at the move.** A row's reason is asked again when
   the fix runs, never trusted from the report: the path must be inside the
   music folder (never the folder itself, never `Artists/`, never `.mlo`), and a
   folder that gained audio since the scan is refused and reported instead.
2. **Two things are never removed**: a foreign root folder that holds audio, and
   a hidden folder inside `Artists/`.
3. **A scoped run** (an import, an album action) confines both the scan and the
   fixes to the named targets and **stores no report**, so a one-album pass can
   never become "the last scan of the library".
4. `layout_apply` off makes the whole pass a report.

### 4.3 Split artist folders — required behaviour

An artist's folder is named `<name> [<artist MBID>]`. The same artist can end up
with two folders — `<name>` and `<name> [<mbid>]`, or two spellings differing
only by case — when one pass wrote artefacts through a name lookup and another
wrote albums from the release metadata. Required:

1. The scan reports **`split_artist`**, naming both folders and saying which one
   holds albums.
2. **`empty_artist` does not fire** for the half that holds no albums when its
   sibling names the same artist, and its offered fix is **not** Trash.
3. The fix is a **merge**: the artist-level artefacts (`artist.jpg`,
   `artist.png`, `description.txt`) move into the folder that holds the albums
   (or, when neither holds albums, into the MBID-named folder), the emptied
   folder is removed to the Trash, and the two halves are never merged at album
   level without a human: if **both** folders hold albums, the finding is
   reported with **no** fix and the row says so.
4. Streams must not be able to observe the intermediate state: move artefacts
   first, then the folder.

### 4.4 Artist artefacts — required behaviour

1. `artist.jpg` (or `artist.png`) and `description.txt` live in the artist
   folder **that holds the albums**; every writer resolves the artist folder by
   name **with** the MBID suffix applied and stripped, case-insensitively, and
   prefers an existing folder over creating one.
2. A missing artist image or artist description **fails grading** (§9.4) — in
   the artist row, the artist page, and the library verdict.
3. The image policy is shared by the writer, the grader and script 19: one
   aspect (`artist_image_aspect`, default `1:1`, ±2 % tolerance), one size
   ceiling (`artist_image_target_size`, `0` = provider-native, capped by a
   2000 px hard ceiling). Undersized is a note, never a failure. Upscaled after
   this app wrote it is a failure.

---

## 5. Feature set B — import

`mlo import <path|archive|folder|file>` runs the pipeline; each step is also a
CLI subcommand and a TUI action. The whole chain is resumable and cancellable,
and an interrupted import reports what was done.

1. **Acquire.** A folder, a set of files, a single file, or an archive
   (`.zip`, `.7z`, `.tar.gz`/`.tar.bz2`/`.tar.xz`, `.rar` when an extractor is
   present). Extract into `.mlo/incomplete/<job>/`, keeping the tree.
2. **Detect the shape.** One album per folder tree; a video disc structure
   (`VIDEO_TS`/`BDMV`) is one title; a folder of loose tracks is one album.
   Refuse to guess when two releases are mixed — report and ask.
3. **Identify.** AcoustID fingerprint → MusicBrainz recording → release; or a
   direct MusicBrainz search (artist + album + track count + durations). The
   match is presented with its confidence and the evidence, and is applied only
   on acceptance (interactive) or at/above a configured threshold (batch).
4. **Write metadata** (§6): the release's full metadata, not a subset — the
   identity, release, credit and link tags, `BARCODE`, `ASIN`, `LANGUAGE`,
   `DISCSUBTITLE`, `LICENSE`, per-track `ISRC`, and the work/relation credits,
   fetched in ONE request per album. Anything with no home in the container's
   tag system is reported as unplaced, never invented under an ad-hoc key.
5. **Artefacts.** Cover art (front), album `description.txt`, artist
   `artist.jpg`/`artist.png`, artist `description.txt` — each fetched from the
   provider chain, each written only into the folder §4.4 names.
6. **Lyrics** (script 13): synced first (`.lrc` with timestamps), then plain;
   `LYRICS`/`UNSYNCEDLYRICS` and the sidecar agree.
7. **Advisory + instrumental** (§6.5): from the provider chain with provenance
   per track; the unattended path is fill-only, a user-pressed action always
   re-rates.
8. **Run the import chain** (§7.4) — layout **last**, grade last of all.
9. **Report**: files written, values filled vs skipped, per-script results,
   leftovers, and the grade of what landed.

---

## 6. Feature set C — tagging

### 6.1 Containers

FLAC (Vorbis comments), Ogg Vorbis, Opus, MP3 (ID3v2.4), M4A/ALAC (MP4 atoms),
WAV/AIFF (ID3 where supported), and MKV/MP4 for music video (tag the container,
never the video stream). A container that cannot carry a tag is reported per
file with the reason.

### 6.2 Families

Five families group every tag the app knows. Anything outside them is foreign
and fails the excess-tags check unless it is on the allowlist.

| Family | Tags |
|---|---|
| identity | `TITLE` `ARTIST` `ALBUM` `ALBUMARTIST` `TRACKNUMBER` `DISCNUMBER` `DATE` `TITLEALIAS` `ARTISTALIAS` `GENRE` `ITUNESADVISORY` `INSTRUMENTAL` `COMMENT` |
| release | `ALBUMALIAS` `MEDIA` `SOURCE` `ALBUMITUNESADVISORY` `RELEASETYPE` `LABEL` `CATALOGNUMBER` `BARCODE` `ASIN` `LANGUAGE` `DISCSUBTITLE` `LICENSE` `ENCODEDBY` `ISRC` `MUSICBRAINZ_*` `RATEYOURMUSIC_*` `WORK` `MOVEMENT` credits (`PERFORMER` `PRODUCER` `ENGINEER` `MIXER` `ARRANGER` `DJMIXER` `CONDUCTOR` `WRITER` `DIRECTOR` `COMPOSERSORT` `MUSICBRAINZ_COMPOSERID`) |
| audio | `MOOD` `ENERGY` `BPM` `INITIALKEY` `DYNAMIC RANGE` `ALBUM DYNAMIC RANGE` `REPLAYGAIN_TRACK_GAIN` `REPLAYGAIN_TRACK_PEAK` `REPLAYGAIN_ALBUM_GAIN` `REPLAYGAIN_ALBUM_PEAK` |
| lyrics | `LYRICS` `UNSYNCEDLYRICS` `TRANSLITERATION` `TRANSLATION` |
| provenance | `AUDIT` `LOG_GRADE` `LOG_CRC` `INTEGRITY` `AUDIO_MD5` (legacy, read-only) `AUDIOAUDITOR_OVERRIDE` `ACOUSTID_ID` `ACOUSTID_FINGERPRINT` `ENCODER_PROGRAM` `ENCODER_QUALITY` `ENCODER_VERSION` |

Alias tags accept a locale suffix (`TITLEALIAS_JA`); the allowlist covers bare
and suffixed spellings, so a legitimate alias is never reported as foreign and
never stripped.

### 6.3 Normalization (on write AND on grade)

- The eight tags with a closed value set hold the canonical spelling
  (`MEDIA`/`SOURCE` normalization lives here, not in auto-tagging).
- One spacing policy: collapse runs, trim, no blank-only values.
- `COMMENT` must be empty: a non-empty `COMMENT` fails (`COMMENT`) and scripts
  3/10/23 clear it.
- Multi-value fields are repeated container fields where the container allows
  it; `"; "`-joined where it does not. Existing values are **completed**, never
  truncated.
- The writer's normalization and the grader's `tag_case`/`tag_spaces` checks are
  the same functions, so an import can never produce a value the grade fails.

### 6.4 Writers

Every tag has exactly one default writer (appendix B). A script never writes
another script's family. A manual edit wins over any derived value, and
`AUDIOAUDITOR_OVERRIDE` wins over every derived audit verdict.

### 6.5 Advisory ladder

Strongest source wins per track; every ISRC the file or MusicBrainz states is
asked; a stated value is final (a stated `0` is final); a track nobody stated
anything about is decided by: instrumental → 0, then the configured AI, then
`advisory_fallback`. Unattended imports are fill-only; every user-pressed action
forces a re-rate (which still never overwrites a stored rating with the invented
fallback).

---

## 7. Feature set D — optimization scripts

### 7.1 The table

25 passes, ids 1–26 (there is no 18). Appendix A is the authoritative table; the TUI
shows it as a menu, the CLI as `mlo run <id|name> [--scope …]`, and both read one
list. Scripts whose feature has its own switch (`dr_replaygain_enabled`,
`audiometa_enabled`, `mood_enabled`, `lyrics_xlit_enabled`,
`lyrics_translate_enabled`, `acoustid_enabled`, `strip_unknown_tags`,
`web_ratings_enabled`) are skipped with a named reason when the switch is off,
not run as no-ops.

### 7.2 Archive and video handling

- Archives as in §5.1a; `.rar` via an extractor when present, otherwise a named
  refusal.
- Video: remux to MKV with audio re-encoded to FLAC (script 11), using
  Symphonia for decode and a pure-Rust MKV writer (`matroska`/`ebml` crates).
  A raw `VIDEO_TS`/`BDMV` structure is one title: pick the main feature by
  duration, keep every audio track that is not a duplicate language.

### 7.3 Pooling and progress

A pool is sized by **items**, not containers: an analysis pooled per track uses
the budget on a single-album run. Every script reports each file once
(ok/skip/fail), progress is cancellable, and a failing script is reported and
never fatal — the chain carries on.

### 7.4 Run order and the import chain

- Run All order:
  `[11, 3, 14, 15, 2, 1, 13, 17, 8, 24, 5, 19, 6, 7, 9, 12, 16, 10, 23, 20, 21, 4]`
  — everything that moves a file first, everything that reads it last.
- The import chain is **derived** from that order minus the library-wide-only
  scripts (today: none — every script is scope-aware). `import_scripts` replaces
  it outright; an empty list means the default.
- **Script 20 (library layout) is in the import chain**, scoped to the imported
  album, and runs **after** beets/MusicBrainz has put the folder in its canonical
  place and **before** the grade. It is the last modifying script; only the grade
  (4) reads after it.
- Every path that finishes an import runs this chain and no wider one.

---

## 8. Feature set E — audio analysis (native)

### 8.1 Dynamic range (script 7)

TT-DR-compatible: per-channel RMS over the whole track and the 2nd percentile of
the 3-second RMS blocks; DR = peak dB − block RMS dB, rounded to a whole number,
reported per track into `DYNAMIC RANGE` and per album (median) into
`ALBUM DYNAMIC RANGE`. Video tracks are never given DR.

### 8.2 ReplayGain (script 7)

EBU R128 loudness (integrated, gated) and true peak per track, plus album gain
over the whole album's programme. Write `REPLAYGAIN_TRACK_GAIN/_PEAK` and
`REPLAYGAIN_ALBUM_GAIN/_PEAK` with this app's own writer (one atomic tag write),
never by letting a tool rewrite the file in place.

### 8.3 Fingerprint (scripts 21, 22)

`ACOUSTID_ID` + `ACOUSTID_FINGERPRINT` are a **pair**: written in one save,
read back to prove it landed. Fingerprints are Chromaprint-compatible; if no
crate provides the encoder, ship/require `fpcalc` and say so in `mlo doctor`.
Script 21 completes a half pair and creates the pair for a file that names its
recording without one (id from the file, fingerprint local; ask AcoustID only
when a half pair names no recording). Script 22 submits fingerprint+recording
pairs to AcoustID, opt-in.

### 8.4 BPM and key (script 12)

Tempo from an onset-strength autocorrelation over a 44.1 kHz mono mixdown;
key from a chromagram (constant-Q or FFT binning) correlated against Krumhansl–
Schmuckler profiles, written as `BPM` and `INITIALKEY` (standard 24-key
spelling, e.g. `C#m`). Both are estimates: script 12 is opt-in per config, and a
manual value is never overwritten by a re-run unless forced.

### 8.5 Mood and energy (script 16)

A shipped model, run locally (no network): a Rust ONNX runtime binding is
acceptable as an optional feature, and the model must be fetched/pinned by
`mlo tools install mood-model`. Without the model the script reports *unavailable
(model not installed)*; it never invents values, and it never writes a
low-confidence guess over a stored value.

---

## 9. Feature set F — grading

### 9.1 Semantics

1. **Binary.** An album passes only when every enabled check on every track,
   file and album slot passed.
2. **Counted.** `total_checks` counts every enabled assertion actually
   evaluated; `pass_count = total_checks - failed_checks`; the report prints
   `pass_count/total_checks`. A check that raises is reported as *could not be
   evaluated* and counted as failed.
3. **Not applicable is not counted** on either side (disabled check, video skip
   tag, empty opt-in family, artist-level check on an album).
4. **Empty folders are graded, not skipped** (`EMPTY_FOLDER`, one failed check).
5. **Every displayed issue is charged** (R1a). The dot, the percentage and the
   problem list can never disagree.

### 9.2 Layout findings are charged

Required change: **any library-layout finding on an album or artist is charged
as a failed check on that row** (`LAYOUT_<KIND>`, e.g. `LAYOUT_SPLIT_ARTIST`,
`LAYOUT_EMPTY_ALBUM`), so a library with a duplicate artist folder, a stray file
or a wrong-case path can never show a green verdict beside a layout problem.
Findings that concern the library as a whole (a stray at the root, a foreign
folder) are charged on an explicit `library` row that the TUI shows with the
same dot vocabulary.

### 9.3 Check registry

Every check is a key in one registry (the TUI's settings screen, the CLI's
`mlo config`, and the grader all read it), with a label, a default, and the
issue codes it can raise. Appendix C is the list; issue codes are stable
strings (`ARTIST_IMAGE_MISSING`, `PATH_CASE`, `COMMENT`, `EMPTY_FOLDER`,
`LAYOUT_*`, …) and the CLI prints them verbatim.

### 9.4 Artist grade

`grade_artist` evaluates, for the artist folder **that holds the albums**:

- `ACT_ARTIST_IMAGE` — `artist.jpg`/`artist.png` present, decodable, in a
  readable container, within the aspect tolerance and the size ceiling; a file
  larger than the size this app recorded writing it fails as *upscaled*;
  undersized is a note.
- `ACT_ARTIST_DESCRIPTION` — non-blank `description.txt`.

Both are **ON by default**, and a missing artefact **fails the artist** and the
library verdict. An artist folder holding no album and no sibling (§4.3) fails
as `ARTIST_EMPTY`; an absent folder is `ARTIST_FOLDER_MISSING`. With both checks
switched off, an artist that holds an album reports 100 % and passes.

### 9.5 Run summary

The run's summary is grade-only: `albums_passed`/`albums_failed` from the grade
distribution; a live audit verdict of FAKE/Mix is counted separately
(`albums_audit_failed`) and is the only other thing that badges FAIL.
AccurateRip never costs a grade point by itself.

---

## 10. Feature set G — external services and tools

### 10.1 Services

| Service | Used for |
|---|---|
| MusicBrainz | release/artist/recording search, the full metadata write, credits |
| AcoustID | fingerprint → recording, submission (opt-in) |
| Cover Art Archive | front cover, per-release and per-release-group |
| Discogs | release metadata, cover, ratings |
| RateYourMusic | public album/track ratings (`RATEYOURMUSIC_*`) |
| Apple Music / Deezer | artwork and descriptions when CAA has none |
| LRCLIB + the lyrics provider chain | synced then plain lyrics |
| YouTube (via yt-dlp or a native client) | music videos, downloads |
| slskd / local downloads | download sources the user configures |
| AudioAuditor, CUETools, Logchecker | audit, AccurateRip, rip-log scoring |

Rules: one HTTP client, per-service rate limits (MusicBrainz: 1 req/s,
identified User-Agent), an on-disk response cache with TTL (SQLite), and a
`mlo sources` health view. Every call degrades to *unavailable + reason*; a
timeout never aborts a batch job.

### 10.2 Tools

The app installs and detects: FLAC, libjxl, libjpeg-turbo, oxipng, AudioAuditor,
rsgain, ffmpeg, Logchecker, PHP, CUETools, librosa-equivalent analysis
(native in this app — the row exists only for compatibility), Chromaprint
(fpcalc), yt-dlp.

Rules:

1. Install into `<music>/.mlo/tools/<Name> v<version>[-<host tag>]/`; detect by
   running the tool **and** by the marker files the installer wrote.
2. A packaged install uses **its own** interpreter equivalent: anything that
   installs a wheel/plugin set must be validated by the very same runtime that
   will load it, and the check must read it back.
3. A tool that installs but then errors is reported with **its captured stderr
   and the command line that produced it** — never a bare "error" (this is the
   `librosa` class of report: a row must never say only "error").
4. `mlo tools doctor` prints, per tool: expected version, found version,
   install kind (download / system package / unsupported here), path, and the
   last failure with its log.

---

## 11. Feature set H — player

### 11.1 Playback

Queue, current track, repeat/shuffle, seek, volume. Decode with Symphonia,
output with `cpal` (or `rodio`). Gapless is not required; a decode failure skips
with a named reason.

### 11.2 Cache — required behaviour

1. **The queue and the current track are persisted** (index DB) on every change
   and restored on start; a restart resumes exactly what was queued and where.
2. **Track metadata is cached** (tags, duration, artwork bytes) so the queue
   renders and playback starts with the network down.
3. **Artwork is cached** beside the album (`cover.*` is the source of truth);
   the player never blocks on a fetch.
4. **The service being down is not an error state**: the app starts, browses the
   index, plays what is local, and shows *offline* with the reason and the last
   successful sync time. Nothing in the player path may require the network.

---

## 12. Architecture

### 12.1 Crates (choose equivalents only with a reason)

```
tui        ratatui, crossterm
cli        clap (derive), serde, serde_json, toml
tags       lofty (read/write), symphonia (decode), claxon (FLAC decode),
           flacenc (FLAC encode), md-5 (STREAMINFO md5), byteorder
audio      ebur128 (R128 loudness/true peak), rustfft (BPM/key), hound (WAV)
images     image, fast_image_resize, jxl-oxide (JXL decode);
           libjxl bindings behind a feature for JXL encode (else PNG fallback)
net        reqwest (rustls), tokio, http-cache (or a hand-rolled SQLite cache)
data       rusqlite (bundled), rayon, walkdir, regex, unicode-normalization
archives   zip, sevenz-rust, tar, flate2, bzip2, xz2
mkv        matroska/ebml crates
playback   symphonia + cpal
misc       anyhow/thiserror, tracing + tracing-appender, time/chrono,
           signal-hook, notify (watch), unicode-width
```

Optional features: `jxl-encode`, `mood-onnx` (ort), `fpcalc` (external),
`rar` (external). A build without them still passes every test in §14.

### 12.2 SQLite schema (minimum)

```
library(path PK, kind, artist, album, mtime, size, quick_hash, indexed_at)
tags(path, key, value)                     -- long tail, not columns
albums(id PK, artist, title, mb_release_id, mb_release_group_id, path, grade_pct, pass)
artists(name PK, path, mb_artist_id, grade_pass, has_image, has_description)
queue(pos PK, path, added_at)              -- §11.2
player_state(id=1, path, position_ms, volume, shuffle, repeat, updated_at)
cache(service, key PK, body, fetched_at, ttl_s)
jobs(id PK, kind, scope, state, started_at, finished_at, journal)
trash(id PK, original_path, trash_path, user, stamp, reason)
```

### 12.3 Config

`mlo.toml`, every key of the source app's `config.json` accepted on migration.

```toml
music_folder = "F:/Media/Music"
naming_script = "..."
short_folder_names = false
layout_apply = true
import_auto_scripts = true
import_scripts = []              # empty = derived default (§7.4)
artist_image_enabled = true
artist_image_aspect = "1:1"
artist_image_crop = true
artist_image_target_size = 0
artist_description_enabled = true
grade_check_artist_image = true
grade_check_artist_description = true
# …one key per check in appendix C, defaults ON except the opt-in families
[services]   musicbrainz_user_agent = "mlo-tui/0.1 ( contact )"
[player]     cache_dir = "<music>/.mlo/data/player"
```

### 12.4 Concurrency and jobs

Long work runs on a job thread pool; the TUI reads job state through a channel
and stays responsive; Ctrl-C cancels at a script boundary and the job reports
what it did not run. No work is done on the render thread.

---

## 13. TUI

Screens: **Library** (artists → albums → tracks, one table with a grade column),
**Album**, **Artist**, **Grade** (per-row check detail), **Layout** (the report
and Apply), **Scripts** (the 24, with scope), **Import** (wizard steps), **Tools**,
**Player** (queue + now playing), **Log**, **Settings**, **Trash**.

Rules:

1. One status line that always says what is running and what failed, with the
   reason attached.
2. Progress is per-item and cancellable; `Esc` cancels the running job, `q`
   quits, `?` help.
3. Every list filter has a keyboard path and every destructive action asks once,
   naming what will move to the Trash.
4. The layout panel groups findings by kind with the count, and Apply shows
   exactly which rows it will act on.

---

## 14. Acceptance tests (headless, no network, no audio device)

Fixtures: build a small library in a temp dir — two artists, three albums, one
wrong-case folder, one stray file, one numbered sidecar, one loose audio file,
one empty album, one artist folder with no albums, one `.mlo_data` leftover.

1. **Naming** — every generated path equals the evaluated template; both the
   full-UUID and the 8-char spelling grade as valid; a case-only difference
   fails as `PATH_CASE`.
2. **Scan** — the fixture yields exactly the expected findings, one row per
   finding, and a second scan of an unchanged tree reports none.
3. **Split artist** — `<name>` + `<name> [<mbid>]`: the scan reports
   `split_artist`; `empty_artist` does **not** fire; the merge moves
   `artist.jpg`/`description.txt` into the album-holding folder, trashes the
   empty half, and the artist then grades on its real artefacts.
4. **Artist artefacts** — an album-holding artist folder with no
   `artist.jpg`/`description.txt` **FAILS** with `ARTIST_IMAGE_MISSING` and
   `ARTIST_DESCRIPTION_MISSING`; both counts appear in the library verdict.
5. **Layout is charged** — an album next to a stray file FAILS with a `LAYOUT_*`
   check; removing the stray flips it to PASS; the dot, percentage and problem
   list agree in every case.
6. **Import chain** — the chain ends with layout (20) then grade (4); a scoped
   import stores no library-wide report; an interrupted import reports the
   scripts it did not run.
7. **Atomicity** — kill a writer mid-write; the target is the old bytes or the
   new bytes, and the next start recovers.
8. **Nothing deleted** — every fix moves to the Trash with a manifest, and
   `mlo trash restore <id>` puts back the exact original path.
9. **Offline** — with the network disabled: browsing, tag edits, layout, grade
   and playback of a local file all work; every network action reports
   *unavailable* with the reason.
10. **Service down** — with the player pointed at a dead service: the app
    starts, the queue is intact after a restart, cached metadata renders, and
    the status line says what is unavailable.
11. **Tools** — a tool that installs but cannot run reports its command line and
    captured stderr; no code path prints a bare "error" for a tool row.
12. **Grading semantics** — a check that raises counts as failed; a disabled
    check counts on neither side; an empty folder fails with `EMPTY_FOLDER`.

---

## Appendix A — the 25 scripts

| # | Name | What it does |
|---|---|---|
| 1 | Format lyrics | multi-format + `MEDIA`/`SOURCE` normalization |
| 2 | Format CUEs | CD-N rename + FILE/INDEX layout |
| 3 | Optimize FLACs | lossless re-encode (max compression, verify, `ENCODER_*`) |
| 4 | Grade | per-album tag/lyrics/cover report (§9) |
| 5 | Process images | JXL / lossless / JXL-back |
| 6 | Audit library | fake lossless / upscaled / MQA detection |
| 7 | DR & ReplayGain | in-process DR + ReplayGain tags |
| 8 | Auto tagging | advisory / instrumental / mood / energy / genre |
| 9 | AccurateRip | CUETools `.accurip` files |
| 10 | Format all | final pass: `.accurip` / `.cue` / `.lrc` / tags |
| 11 | Remux videos (MKV) | any video → MKV, audio → FLAC |
| 12 | Key & BPM | musical key + tempo tags |
| 13 | Fetch lyrics | synced then plain |
| 14 | Beets tagging | MusicBrainz release tagging (native here) |
| 15 | Release tracklist | `.mlo_expected.json` manifests |
| 16 | Mood & Energy | `MOOD`/`ENERGY` from the track's audio |
| 17 | Lyrics transliterate | `TRANSLITERATION`/`TRANSLATION` + sidecars |
| 19 | Optimize artist images | crop/resize artist artwork to policy |
| 20 | Optimize library layout | the layout report + fixes (§4.2) |
| 21 | Fix AcoustID pairs | complete or create `ACOUSTID_ID`/`_FINGERPRINT` |
| 22 | Submit fingerprints | give AcoustID the fingerprint + recording |
| 23 | Optimize tags | delete excess tags: junk names, a valued `COMMENT`, unneeded aliases |
| 24 | Web ratings | aggregated public album + track scores |
| 25 | Fetch artist images | missing artist photos from the provider chain (force re-fetches) |
| 26 | Fetch artist descriptions | missing artist biographies from the provider chain (force re-fetches) |

## Appendix B — default writers

| Tag | Writer |
|---|---|
| identity/release core, credits, links | import (MusicBrainz) |
| `MEDIA` `SOURCE` | script 1 |
| `GENRE` | script 8 / genre import; script 10 trims |
| `ITUNESADVISORY` `INSTRUMENTAL` | script 8 / advisory fetches |
| `ALBUMITUNESADVISORY` | script 8 |
| `MOOD` `ENERGY` | script 16 |
| `BPM` `INITIALKEY` | script 12 |
| `DYNAMIC RANGE` `ALBUM DYNAMIC RANGE` | script 7 |
| `REPLAYGAIN_*` | script 7 |
| `AUDIT` `LOG_GRADE` `LOG_CRC` `INTEGRITY` | script 6 |
| `ACOUSTID_*` | import wizard / script 21 |
| `ENCODER_*` | script 3 (script 5 for images) |
| `LYRICS` `UNSYNCEDLYRICS` | script 13 / editor |
| `TRANSLITERATION` `TRANSLATION` | script 17 |

## Appendix C — grading checks (keys; all default ON unless marked)

Missing/identity: `missing_tags`, `tag_case`, `tag_spaces`, `tag_blank_lines`,
`excess_tags`, `alias_needed`, `alias_excess`, `naming`, `filename_case`,
`ext_case`, `empty_folders`, `disallowed`, `unreadable`, `expected_tracks`.
Release: `album_tags`, `mb_links`, `rym_links`, `media`, `source`, `genre`,
`genre_count`, `genre_order`, `genre_vocab`, `naming`.
Audio: `key_bpm`, `mood`, `energy`, `replaygain` (opt-in family), `encoder`,
`flac_md5`, `cr` (`crc`), `cd_format`, `cd_cue`, `cd_log`, `log_grade`,
`log_checksum`, `accuraterip`, `accurip_format`, `lossless_source`, `audit`,
`instrumental`, `raw_video`.
Lyrics: `lyrics`, `lyrics_format`, `lyrics_spaces`, `lyrics_blank_lines`,
`lyrics_zero`, `lyrics_lang_tags`, `xlit_transliteration`, `xlit_translation`.
Covers: `cover`, `cover_crop`, `sidecar_cover`, `extra_images`.
AcoustID: `acoustid`. Album description: `album_description`.
Artist: `artist_image`, `artist_description` (**failing** when the artefact is
missing — §9.4).
Layout: `layout_*` — one key per finding kind in §4.2, all failing.

---

## Appendix D — output contract for the agent

Work in this order and stop at each gate:

1. Skeleton: CLI, config, SQLite, logging, atomic IO, trash. Gate: `mlo --help`,
   `mlo doctor`, tests 7 and 8 pass.
2. Scan/index/layout (§4). Gate: tests 2, 3, 5 pass on the fixture.
3. Tags read/write + families + normalization (§6). Gate: a round-trip test on
   every container in §6.1.
4. Grading (§9). Gate: tests 4, 12 pass.
5. Import + MusicBrainz/AcoustID clients (§5, §10.1). Gate: an offline mock
   server drives a full import end to end.
6. Analysis and scripts (§7, §8). Gate: DR/ReplayGain/BPM/key pinned to
   reference values on a fixed fixture.
7. TUI (§13) + player and cache (§11). Gate: tests 9, 10 pass.
8. Hardening: `mlo tools doctor`, archive matrix, video remux, optional
   features on/off.

At every gate: no `unwrap()` on user input, no panic in a job, a named reason for
every refusal, and a regression test for each bug fixed.