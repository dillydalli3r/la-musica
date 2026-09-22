# la musica — Optimization & Grading Specification

This is the contract the app is held to, derived from the current
implementation. It is written so a user can check the app against it: every
requirement names the check id, script id or config key it maps to.

Sources of truth (change these and this document is wrong until it is updated):

| What | Where |
| --- | --- |
| Check ids, labels, defaults | `server/tags_registry.py` `CHECK_LABELS` (labels) + `mlo/config.py` `DEFAULT_CONFIG` (the keys and their defaults); `web/src/pages/GradingPage.tsx` `GROUPS` + `CHECK_DESC` |
| What each check asserts | `mlo/grader.py` (`_grade_album`, `grade_artist`, `run_grade_library`) |
| The presets | `web/src/pages/GradingPage.tsx` `applyPreset` |
| Script ids, titles, order | `mlo/cli.py` `SCRIPTS`, `server/script_runners.py` `RUNNERS`, `mlo/config.py` `DEFAULT_RUN_ALL_ORDER`, `server/imports.py` `DEFAULT_CHAIN` |
| Tag families and writers | `mlo/audio.py` `TAG_MAP`, `server/tags_registry.py` `TAG_FAMILY` / `TAG_WRITER` |
| Audit evidence | `mlo/audit.py`, `mlo/discs.py` |

Notation: **ON**/**off** is the shipped `DEFAULT_CONFIG` value of a check.
Requirements below are numbered `R<n>`; every one is falsifiable by running the
Grade script (4) over an album and reading the report.

---

## 1. Verdict semantics

- **R1 — a verdict is binary.** An album is `PASS` only when every enabled check
  on every track, file and album slot passed; otherwise `FAIL` with the failed
  checks itemized. There are no partial grades and no letters
  (`mlo/grader.py:run_grade_library`, `grade_dist`).
- **R2 — the score is a check count, not a percentage of files.**
  `total_checks` counts every enabled assertion evaluated,
  `pass_count = max(0, total_checks - failed_checks)`, and the report prints
  `pass_count/total_checks`. `grade_verbose` (ON) controls the per-track detail.
- **R3 — a check that raises fails loudly.** An internal error is reported as
  *could not be evaluated*, counted in `total_checks` and failed, so the
  percentage can never be inflated by a check that silently disappeared.
- **R4 — checks that do not apply are not counted.** A disabled check, a
  `VIDEO_SKIP_TAGS` tag on a music video, an opt-in family with no member tags
  present, an artist folder's album-level checks and an album with no
  MusicBrainz release id are all excluded from *both* sides of the fraction.
- **R5 — the run's summary is grade-only, and says so.** `albums_passed` /
  `albums_failed` come from `grade_dist`; an album that passes every check while
  its live audit verdict is FAKE/Mix is counted in `albums_audit_failed` and the
  library badges it `FAIL` on the Audit column. AccurateRip never costs a grade
  point by itself (§3, *Auditing*).
- **R6 — empty folders are graded, not skipped.** A folder with no audio track
  anywhere beneath it (including one that holds only a `cover.*`, `.cue`, `.log`,
  `.lrc` or `.accurip`) is reported as `EMPTY_FOLDER` with one failed check, so
  an album whose audio is gone cannot hide from the counts.
- **R7 — artist folders have their own grade.** `grade_artist()` evaluates
  `grade_check_artist_image` and `grade_check_artist_description`, and fails an
  artist folder that holds NO album folder at all (`EMPTY_ARTIST`): a folder
  carrying only the artist's own image and description is not an artist in this
  library — nothing of theirs is here — so it cannot pass as one. With both
  checks switched off an artist that DOES hold an album reports 100 % and
  `pass: true` (nothing graded is nothing failed). An absent artist folder is
  `ARTIST_FOLDER_MISSING`.
- **R7a — the artist image is judged on its decoded pixels**, never on its name
  or suffix: Pillow reads the stored file, and every issue names the numbers it
  judged. OVERSIZED fails (`image_policy()`'s ceiling: `artist_image_target_size`,
  else the 2000 px `DEFAULT_MAX_SIDE`); a ratio further than 2 %
  (`ASPECT_TOLERANCE`) from `artist_image_aspect` fails, naming both ratios and
  the delta; a file that does not decode fails as `ARTIST_IMAGE_CORRUPT`; a
  decodable image in a container the library does not read fails as
  `ARTIST_IMAGE_FORMAT`; and a file larger than the size `save_image` recorded
  writing it fails as `ARTIST_IMAGE_UPSCALED` (its detail is interpolated).
  **Undersized is accepted** — it lands in the result's `notes` with the shortfall
  and never fails, because nothing in the pipeline upscales and failing it would
  fail the folder permanently. Script 19 (`Optimize artist images`) is the pass
  that clears every one of these.

### Issue codes

An issue is `{code, label, where, reason}` for album-level and artist problems
(`reason` is the sentence naming the numbers) and a code list
(`["GENRE_COUNT", …]`) per track. The codes that exist today:

| Code | Means |
| --- | --- |
| `UNREADABLE` | the file could not be opened/decoded |
| `TITLE`, `ARTIST`, `ALBUM`, `ALBUMARTIST`, `DATE`, `TRACKNUMBER`, `DISCNUMBER`, `GENRE`, `MOOD`, `ENERGY`, `ITUNESADVISORY`, `INSTRUMENTAL`, `DYNAMIC RANGE`, `REPLAYGAIN_*`, `INITIALKEY`, `BPM`, `MEDIA`, `SOURCE`, `ENCODER_*`, `ACOUSTID_ID`, `ACOUSTID_FINGERPRINT`, `LOG_GRADE` | the tag (or its presence check) failed; the tag's own name is the code |
| `MOOD_MISSING`, `ENERGY_MISSING`, `GENRE_MISSING` | the per-tag presence checks (`TAG_PRESENCE_CHECKS`) |
| `GENRE_COUNT`, `GENRE_ORDER`, `GENRE_VOCAB`, `GENRE_CASE` | genre count, arrangement, vocabulary, and the spelling every writer produces (`metal` → `Metal`) |
| `TAGS` | excess tags |
| `PATH`, `PATH_CASE` | naming-script mismatch / case-only mismatch |
| `LYRICS` | lyrics missing, wrongly formatted, or present on an instrumental |
| `XLIT_MISSING`, `XLIT_UNNEEDED` | a needed transform is absent / an unneeded one is stored |
| `MB_LINK`, `RYM_LINK` | a required identity link is missing |
| `COVER` | cover missing or failing the size/square rules |
| `CRC`, `CRC_MISMATCH` | a track is not covered by its disc's `.log` CRC / its CRC does not match |
| `CD_FORMAT` | a CD track is not 16-bit/44.1 kHz FLAC |
| `LOG_CHECKSUM` | the rip log's EAC SHA256 does not verify |
| `AUDIT` | the audit tag is missing or not REAL (with `grade_check_audit` on) |
| `EMPTY_FOLDER`, `EXPECTED_TRACKS_MISSING` | folder/release-level failures |
| `ARTIST_IMAGE_MISSING`, `ARTIST_IMAGE_CORRUPT`, `ARTIST_IMAGE_FORMAT`, `ARTIST_IMAGE_OVERSIZED`, `ARTIST_IMAGE_ASPECT`, `ARTIST_IMAGE_UPSCALED`, `ARTIST_DESCRIPTION_MISSING`, `ARTIST_FOLDER_MISSING`, `EMPTY_ARTIST` | artist-folder failures (script 19 clears the image ones). `EMPTY_ARTIST` is an artist folder holding NO album folder — only the artist's own image/description: the artist is not in the library, so the folder is not a graded artist. Script 20 reports it and the Optimization page can remove it to the Trash |
| `ARTIST_IMAGE_UNDERSIZED` | informational note on an artist image below `artist_image_target_size` — reported, never failing |

---

## 2. The 21 optimization scripts

Ids, titles and the shipped order are `mlo/cli.py:SCRIPTS` and
`mlo/config.py:DEFAULT_RUN_ALL_ORDER`; the runners are
`server/script_runners.py:RUNNERS` (that table is what `/api/run` and the import
chain both call).

**R8 — Run All runs `run_all_order`**, shipped as
`[11, 3, 14, 15, 2, 1, 13, 18, 17, 8, 5, 19, 6, 7, 9, 12, 16, 10, 20, 21, 4]`:
everything that moves a file first, everything that reads it last. A saved order
is honoured as saved (ids outside 1–21 are dropped; legacy 8/9-id orders are
migrated).
**R9 — the import chain is DERIVED from the run order, minus a declared
exception.** `import_scripts` replaces it outright; an empty list means the
default, which is `DEFAULT_RUN_ALL_ORDER` minus `LIBRARY_WIDE_SCRIPTS` — one
list, so a script added to Run All cannot go missing from an import, and the
scripts an import deliberately does not run are named as data with their reason
rather than kept as a second hand-written list. Today the set holds exactly one
id: **20 (Scan library layout)**, whose runner walks the whole music folder and
writes ONE report about the whole library (the Library page warns from that
stored report) — running it once per imported album would re-walk the library
for every import and overwrite the report with a partial scan. Script 21 is
per-album and does run on import. `import_auto_scripts` (ON) off still means
"run nothing after import".
**R10 — a failing script is reported, never fatal**: the chain carries on and
per-script results are returned (`server/script_runners.py`).

| # | Title | What it does | Changes | Destructive | Network |
| --- | --- | --- | --- | --- | --- |
| 1 | Format lyrics | Canonical embedded `LYRICS`/`.lrc`: timestamps, padding, blank lines, zero-timestamp rule, enhanced/extended LRC; normalizes album `MEDIA`/`SOURCE` | tags, `.lrc` sidecars | deletes an emptied sidecar when lyrics are embedded | no |
| 2 | Format CUEs | Canonical CUE text, `FILE`-line fixes, `CD-N` sheet renaming | `.cue`/`.log` names and contents | renames and rewrites sidecars | no |
| 3 | Optimize FLACs | Re-encode at `library_codec_quality` (FLAC `-0`..`-8`), strip padding/CUESHEET/APPLICATION and tags outside the canonical set; convert files that are not the `library_codec` target yet (what `library_codec_optimize` permits) | audio bytes, `ENCODER_*`, file extension | **yes** — the converted original moves to the app's trash when `lossless_remove_original` (ON) | no |
| 4 | Grade | The full battery in §3 | nothing (read-only) | no | no |
| 5 | Process images | Resize/crop covers to `cover_target_size`, per-format targets, JPEG/PNG/JXL optimization, `cover.*` rename | image files in place | re-encodes in place | no |
| 6 | Audit library | AudioAuditor detectors (spectral/DSP) + CD `.log` CRC verification, log scoring | `AUDIT`, `LOG_GRADE`, `LOG_CRC`, `INTEGRITY` | no | no |
| 7 | DR & ReplayGain | in-process loudness-war DR (`mlo/dr.py`) + `rsgain` ReplayGain 2.0 | `DYNAMIC RANGE`, `ALBUM DYNAMIC RANGE`, the four `REPLAYGAIN_*` | no | no |
| 8 | Auto tagging | `ITUNESADVISORY`, `ALBUMITUNESADVISORY`, `INSTRUMENTAL`, `MOOD`, `ENERGY`, `GENRE`, plus empty MusicBrainz identity/date completion | those tags | no | optional (advisory/genre providers) |
| 9 | AccurateRip | CUETools `.accurip` generation and verification | writes `CD-N.accurip` | no | **yes** (AccurateRip DB) |
| 10 | Format all | Final canonical pass: `.accurip`/`.cue`/`.lrc`/tag trim, the canonical tag-value spelling (`mlo/tagtext.py`) + embedded-cover policy | tags, sidecars, embedded art | **yes** (strips tags outside the allowlist) | no |
| 11 | Remux videos (MKV) | Any video container → MKV, video copied bit-exact when possible, audio to FLAC, chapters kept | video files | **yes** when `video_remove_original` (ON) | no |
| 12 | Key & BPM | librosa key/tempo analysis | `INITIALKEY`, `BPM` | no | no |
| 13 | Fetch lyrics | The configured synced-lyrics chain into `lyrics_format` | `LYRICS`/`UNSYNCEDLYRICS`, `.lrc` | no | **yes** |
| 14 | Beets tagging | Managed beets import with the naming script, work/movement tags | identity/release tags, file paths | **yes** (moves/renames, overwrites identity tags) | **yes** (MusicBrainz) |
| 15 | Release tracklist | Writes `.mlo_expected.json` from the release's own tracklist | adds a manifest file | no | **yes** (MusicBrainz) |
| 16 | Mood & Energy | The mood classifier alone | `MOOD`, `ENERGY` | no | no |
| 17 | Lyrics transliterate (AI) | Romanization/translation tags and sidecars, re-synced at `lrc_sync_level` | `TRANSLITERATION-*`, `TRANSLATION-*`, sidecars | no | **yes** (configured AI endpoint) |
| 18 | Publish lyrics (LRCLIB) | Submits missing lyrics to the community database | nothing locally | no (external side effect) | **yes** (LRCLIB) |
| 19 | Optimize artist images | Re-fits `Artists/<Artist>/artist.*` to `artist_image_aspect` / `artist_image_target_size`, re-encodes as `artist.jpg`/`artist.png` | the artist image in place (only when it has to move) | re-encodes in place; never deletes | no |
| 20 | Scan library layout | The music folder's shape against `<music>/Artists/<Artist>/<Album>/…`: audio at the root or in an artist folder, stray files, unexpected folders, empty albums, `wrong_case` rows. Read-only — it moves nothing. Writes ONE report describing the whole library to `<music>/.mlo/data/`, which the Library page warns from; it ignores `targets` on purpose and is therefore not run by an import (R9) | one report file | no | no |
| 21 | Fix AcoustID pairs | Completes an INCOMPLETE `ACOUSTID_ID`/`ACOUSTID_FINGERPRINT` pair — the failure `Missing ACOUSTID_FINGERPRINT (incomplete AcoustID pair)`, which had no fixer before. An id already on the file has its fingerprint recomputed locally; the reverse half needs a lookup and is counted, never invented | `ACOUSTID_ID`, `ACOUSTID_FINGERPRINT` | no | only when the id half must be looked up |

**R11 — force flags are the only way to redo work.** Each script has one, and it
is what makes the script look at a file it has already processed:
`force_lyrics` (1), `force_cue` (2), `force_reencode_flac` (3), `force_reencode_images`
(5), `force_audit` (6), `force_dr_replaygain` (7), `force_auto_tag` (8),
`force_accurip` (9), `force_audiometa` (12), `force_mood` (16), `force_xlit` (17),
`force_publish` (18), `force_tracklist` (15). Grade (4) and Scan library layout (20) need none — both
re-read. The *Re-run & overwrite* menu on any selection sets exactly these keys.
**R12 — a switched-off feature skips its script** instead of running it as a
no-op: `dr_replaygain_enabled` (7), `audiometa_enabled` (12), `mood_enabled` (16),
`lyrics_xlit_enabled` / `lyrics_translate_enabled` (17), `lrclib_auto_publish`
(18), `acoustid_enabled` (21 — the same switch the AcoustID lookup itself
refuses on, so a run says WHY it did nothing instead of reporting an empty
pass). Scripts 9/10/11/12/13/16/17/18 whose module is missing are reported
unavailable rather than silently passing.
**R13 — scripts clean up after themselves**: folders a run emptied are pruned
bottom-up (never a folder that holds anything, never the music root), and the run
reports how many were removed.

---

## 3. Grading checks

68 keys exist; **every one of them ships ON**, checks and file categories
alike. A fresh install grades strictly without anyone pressing a preset: the
two that used to ship off (`grade_check_audit`, `grade_include_other`) are
named in `mlo/config.py::STRICT_DEFAULT_KEYS` so the change is visible rather
than implied. Every check is
toggleable on the Grading page; a check the registry knows and the page does not
group still renders (section *Other checks*).

### Tracks & albums

| Check id | Label | Default | Asserts |
| --- | --- | --- | --- |
| `grade_check_unreadable` | Unreadable files | ON | every audio file opens and decodes (`UNREADABLE`) |
| `grade_check_missing_tags` | Required tags | ON | every `PER_TRACK_TAGS` entry is present and non-empty: `TITLE`, `ARTIST`, `ALBUM`, `ALBUMARTIST`, `DATE`, `TRACKNUMBER`, `DISCNUMBER` (only when the album really has several discs), `GENRE`, `MOOD`, `ENERGY`, `ITUNESADVISORY`, `DYNAMIC RANGE`, `INSTRUMENTAL` (ReplayGain is graded by its own opt-in check) |
| `grade_check_album_tags` | Album-level tags | ON | `ALBUMITUNESADVISORY` and `ALBUM DYNAMIC RANGE` are present |
| `grade_check_mood` | Mood tag present | ON | `MOOD` exists (`MOOD_MISSING`) |
| `grade_check_energy` | Energy tag present | ON | `ENERGY` (0-100) exists (`ENERGY_MISSING`) |
| `grade_check_genre` | Genre tag present | ON | `GENRE` exists (`GENRE_MISSING`) |
| `grade_check_genre_count` | Genre count per track | ON | at most `mb_genre_count` genres (default 2, max 3) — a ceiling, never a quota (`GENRE_COUNT`) |
| `grade_check_genre_order` | Genre order (family first) | ON | the family slot, if present, is FIRST and no genre repeats (`GENRE_ORDER`) |
| `grade_check_genre_vocab` | Genre vocabulary | ON | every name is one MusicBrainz publishes (`GENRE_VOCAB`); grading never rewrites the tag |
| `grade_check_replaygain` | ReplayGain tags present | ON | opt-in per file: any `REPLAYGAIN_*` tag means all four must exist |
| `grade_check_acoustid` | AcoustID tags present | ON | opt-in pair: `ACOUSTID_ID` and `ACOUSTID_FINGERPRINT` together |
| `grade_check_encoder` | Encoder identity | ON | the `ENCODER_*` markers switched on in `encoder_tags` are present (covers included while `reencode_images` is on) |
| `grade_check_naming` | Naming script match | ON | the full relative path equals the evaluated `naming_script`; full and 8-char MBIDs both accepted (`PATH`) |
| `grade_check_filename_case` | Path capitalization | ON | letter case matches the script exactly (`PATH_CASE`) |
| `grade_check_ext_case` | Lowercase extensions | ON | no `.FLAC`-style extension in the folder |
| `grade_check_key_bpm` | Key & BPM | ON | `INITIALKEY` (in `audiometa_key_notation`) and `BPM` exist |
| `grade_check_excess_tags` | Excess tags | ON | no tag outside `mlo.grader.TAG_ALLOWLIST` (`TAGS`); gated as well on `strip_unknown_tags` |
| `grade_check_media` | Media type | ON | `MEDIA` exists, is in `KNOWN_MEDIA`, and is uniform across the album (`MEDIA`) |
| `grade_check_source` | Source tag | ON | `MEDIA=digital media` requires a non-empty, uniform `SOURCE`; any other medium must NOT carry one (`SOURCE`) |
| `grade_check_instrumental` | Instrumental consistency | ON | `INSTRUMENTAL=1` tracks carry no lyrics; `INSTRUMENTAL=0` tracks are graded for lyrics |
| `grade_check_disallowed` | Disallowed file types | ON | no file whose category is switched off in `grade_include_*` |
| `grade_check_extra_images` | Stray images | ON | no image that is neither `cover.*` nor a per-track sidecar |
| `grade_check_empty_folders` | Empty folders | ON | no audio-less folder (`EMPTY_FOLDER`) |
| `grade_check_expected_tracks` | Release tracklist manifest | ON | an album carrying a MusicBrainz release id has a non-empty `.mlo_expected.json` (`EXPECTED_TRACKS_MISSING`) |
| `grade_check_album_description` | Album description stored | ON | `<album>/description.txt` exists and is non-blank |
| `grade_check_raw_video` | Raw videos | ON | no un-remuxed video container (`.vob`/`.avi`/`.wmv`/`.ts`…) |
| `grade_check_lossless_source` | Lossless sources | ON | no uncompressed lossless source (`.wav`/`.aif`/`.aiff`/`.ape`/`.wv`/`.shn`/`.tta`) is left in the library. The check **stands down** (adds no check at all) when the conversion pass would never touch one: the target is itself one of those containers (`library_codec` = `wav`/`aiff`) or nothing is converted (`library_codec`/`library_codec_optimize` = `keep`). The issue names the target: *"… (script 3 converts them to FLAC)"* |
| `grade_check_disc_naming` | Disc rip-sheet naming | ON | a CD's `.log`/`.cue`/`.accurip` follow `discs_rename_pattern` (`CD-{n}`) |
| `grade_check_cd_log` | CD — .log present | ON | every CD disc has an exact-match, non-empty `.log` |
| `grade_check_cd_cue` | CD — .cue present | ON | every CD disc has a `.cue` |
| `grade_check_cd_format` | CD — lossless format | ON | a CD track is 16-bit/44.1 kHz (`CD_FORMAT`; FLAC is what the shipped target produces). A file that **is** the configured *lossy* target is exempt and not counted — the CD-DA stream is gone once the album was deliberately converted, and Opus resamples to 48 kHz by design |
| `grade_check_crc` | CRC checksums | ON | every track is covered by a per-track CRC in its **own disc's** `.log` (`CRC`) and that CRC equals the decoded PCM's CRC-32 (`CRC_MISMATCH`); lossy or undecodable files are judged on coverage alone |

### Artist

| Check id | Label | Default | Asserts |
| --- | --- | --- | --- |
| `grade_check_artist_image` | Artist image stored | ON | the artist folder holds `artist.jpg`/`artist.png` within `artist_image_aspect` (±2 %) and under the size ceiling, in the library's format and decodable — see R7a (`ARTIST_IMAGE_MISSING` / `_CORRUPT` / `_FORMAT` / `_OVERSIZED` / `_ASPECT` / `_UPSCALED`; `ARTIST_IMAGE_UNDERSIZED` is a note) |
| `grade_check_artist_description` | Artist description stored | ON | the artist folder holds a non-blank `description.txt` (`ARTIST_DESCRIPTION_MISSING`) |

### Auditing

| Check id | Label | Default | Asserts |
| --- | --- | --- | --- |
| `grade_check_audit` | Require audit tag | ON | the track's audit verdict is REAL — missing or non-REAL fails (`AUDIT`). It ships **on** now: with the CD verdict decided by the rip's own evidence (§5, R21) an unaudited library is a library nobody has checked, which is the thing this check exists to say |
| `grade_check_log_checksum` | Log checksum valid | ON | the rip log's EAC SHA256 verifies; a log that states none while `audit_verify_log_checksum` is on fails (`LOG_CHECKSUM`); XLD logs and EAC logs from BEFORE v1.0 pass (nothing claimed, nothing refuted — v1.0b1 is the release that introduced the checksum, and the version is read from the log's OWN header, so a modern EAC log with its checksum line removed is still a real tamper signal and still fails). The log file itself is never written, repaired or stripped: editing a log to make its checksum pass would destroy the only thing the checksum proves |
| `grade_check_accuraterip` | AccurateRip verified (audit only) | ON | a `.accurip` whose verdict is not REAL marks the album's audit FAKE (and the track red). Together with `audit_require_accuraterip` it is what can turn the audit verdict FAKE; **it never adds a grade point** |
| `grade_check_log_grade` | Log grade present & in range | ON | `LOG_GRADE` exists, is an integer 0-100 and is at least `grade_log_score_threshold` (default 100; 0 disables the threshold) |

### Identity links, covers, formatting, lyrics, categories

| Check id | Label | Default | Asserts |
| --- | --- | --- | --- |
| `grade_check_mb_links` | MusicBrainz release link | ON | `MUSICBRAINZ_ALBUMID` (or a release-group id) is tagged (`MB_LINK`) |
| `grade_check_rym_links` | RateYourMusic release link | ON | `RATEYOURMUSIC_ALBUM` is tagged (`RYM_LINK`) |
| `grade_check_cover` | Cover art | ON | the album has a cover (`cover.jpg`/`jpeg`/`png`/`jxl`) meeting the size rules (`COVER`) |
| `grade_check_cover_crop` | Cover aspect ratio (squareness) | ON | `|w/h − 1| ≤ cover_crop_threshold` (an aspect test, not crop detection) |
| `grade_check_sidecar_cover` | Per-track sidecar covers | ON | per-track covers meet the same rules |
| `grade_check_tag_spaces` | Tags — no padding | ON | no leading/trailing space or tab, and no run of 2+ internal spaces, in a single-line tag value (a value carrying a newline is never judged — its whitespace is text) |
| `grade_check_tag_case` | Tag value capitalisation | ON | `MEDIA`, `SOURCE`, `RELEASETYPE`, `RELEASESTATUS`, `AUDIT`, `RELEASECOUNTRY`, `SCRIPT` and `MOOD` hold the canonical spelling `mlo/tagtext.py` writes (`TAG_CASE`), and every name in `GENRE` holds the form every genre writer ends on — `mlo/genres.py::display_name` of the name's canonical spelling (`GENRE_CASE`, e.g. `metal` → `Metal`). GENRE is deliberately NOT in `CANONICAL_CASE`: it is an open, multi-value tag whose canonical form is per name, not a closed vocabulary. Free text — `TITLE`, `ALBUM`, `ARTIST`, `LABEL`, lyrics — is never touched |
| `grade_check_tag_blank_lines` | Tags — no blank lines | ON | no blank line inside a tag value (`LYRICS` exempt) |
| `grade_check_lyrics_spaces` | Lyrics — no padding | ON | no leading/trailing space on a lyric line |
| `grade_check_lyrics_blank_lines` | Lyrics — blank line rules | ON | blank lines match the formatter's canonical output |
| `grade_check_lyrics_zero` | Lyrics — zero timestamp rule | ON | the `[00:00.00]` leader follows `lrc_add_zero_timestamp` / `lrc_zero_timestamp_blank` / `lrc_zero_timestamp_target` |
| `grade_check_lyrics_format` | Lyrics — canonical formatting | ON | re-running the formatter would change nothing (timestamps at `lrc_timestamp_precision`, `lrc_strip_metadata`, `lrc_collapse_blank_lines`, no merged timestamps) |
| `grade_check_cue_spaces` / `grade_check_cue_blank_lines` / `grade_check_cue_format` | CUE — no padding / no blank lines / canonical formatting | ON | CUE lines are trimmed, blank lines absent, and the sheet is byte-equivalent to the canonical formatter's output (`keep_empty_cue_lines`, `keep_other_cue_lines`, `cue_file_type`, `append_final_newline`) |
| `grade_check_accurip_format` | `.accurip` — canonical formatting | ON | each line trimmed, outer blank lines handled (`keep_empty_accurip_lines`) |
| `grade_check_cue_files` | CUE — referenced files exist | ON | every `FILE` line names a file that is in the album |
| `grade_check_lyrics` | Lyrics present | ON | every non-instrumental track has lyrics (embedded and/or `.lrc`, per `lyrics_format`) |
| `grade_check_lyrics_lang_tags` | Transform language tags | ON | `TRANSLATION-EN`, `TRANSLITERATION-JA-LATN`-style names, never the bare legacy ones |
| `grade_check_xlit_transliteration` | Transliteration — needed, never extra | ON | `mlo.lyrics_xlit.xlit_needs` says a transliteration is required and it exists, or is not required and none is stored (`XLIT_MISSING` / `XLIT_UNNEEDED`) |
| `grade_check_xlit_translation` | Translation — needed, never extra | ON | same, against the reader's language (`lyrics_translation_langs`, first entry) |
| `grade_include_music` | Audio tracks | ON | the audio files themselves participate in grading |
| `grade_include_cover` | Cover art | ON | `cover.*` images participate |
| `grade_include_description` | Album description | ON | `description.txt` is the app's own file category, not a stray file |
| `grade_include_cue` / `grade_include_log` / `grade_include_lrc` / `grade_include_accurip` | CUE sheets / Log files / LRC lyrics / AccurateRip files | ON | those sidecars participate |
| `grade_include_video` | Remuxed videos | ON | MKV/MP4 music videos participate |
| `grade_include_other` | Other files | ON | unclassified files (`.txt`, `.pdf`, `.m3u`, …) participate |

**R14 — `VIDEO_SKIP_TAGS` never apply to a music video**: `REPLAYGAIN_*` and
`DYNAMIC RANGE` are not written into video containers by any script, so they are
not graded there. Videos are still graded on tags, links, naming and format.
**R15 — the excess-tag vocabulary is one predicate.** `tag_key_allowed()`
(which reads `TAG_ALLOWLIST` = `TAG_MAP` + encoder markers + `BEETS_TAGS` +
`BEETS_ID3_FRAMES`) is used by the grade *and* by the Optimize/Format All strip
pass, so a strip can never leave what the grade flags or delete what it needs.
**R15a — the metadata import is complete, and complete means allowlisted.**
Every MusicBrainz field with a home in the container's tag system is written
(§6), and every one of them goes into that SAME predicate — so a credit the app
wrote is never reported as an excess tag and never stripped by script 10, while
a genuinely foreign tag still is. A field with no home is not invented under an
ad-hoc key; the writer reports what it could not place.
**R16 — ReplayGain and AcoustID are opt-in families** (R42): absence is never a
failure, a half-written set always is.
**R17 — CD vs Digital Media vs other.** `_is_cd()` is exactly `MEDIA == "cd"`
(case-insensitive); the CUE/LOG/AccurateRip/CRC/`LOG_GRADE` expectations are
gated on it. `MEDIA == "digital media"` requires `SOURCE`. Any other value in
`KNOWN_MEDIA` is graded like Digital Media without the `SOURCE` requirement, and a
value outside `KNOWN_MEDIA` fails `grade_check_media`. Two interactions follow
from the library target (`library_codec`): `grade_check_lossless_source` stands
down when the target is itself an uncompressed container (`wav`/`aiff`) or the
library is kept as it is, and `grade_check_cd_format` exempts a file that already
**is** the configured lossy target. `library_codec` = `wav`/`aiff` also makes
those extensions library audio (`mlo/paths.py:AUDIO_EXTS`), so such a file is a
graded, taggable track rather than an invisible one.

---

## 4. Presets

The three presets are one-click starting points on the Grading page; they edit
the local config copy and only take effect on **Save** (`POST /api/config`).
**R18 — the SHIPPED defaults ARE Strict**: every `grade_check_*` and every
`grade_include_*` key ships `true` (68 of 68), so a fresh install grades
strictly with nobody pressing anything. The two that used to ship off —
`grade_check_audit` and `grade_include_other` — are named in
`mlo/config.py::STRICT_DEFAULT_KEYS`, so the change is a readable fact rather
than an implied one. **R18a — Strict** is therefore the identity preset (load
the defaults and set every `grade_check_*` true): it is what a fresh install
already has, and pressing it on an edited config restores it.
**R19 — Balanced** is the pre-strict set: the defaults with `grade_check_audit`
and `grade_include_other` off — the one-click way back to the old behaviour for
a collection nobody has audited.
**R20 — Relaxed** loads the defaults and then switches these 18 keys **off**:
`grade_check_tag_spaces`, `grade_check_tag_case`, `grade_check_lyrics_spaces`,
`grade_check_cue_spaces`, `grade_check_cover_crop`, `grade_check_lyrics_zero`,
`grade_check_tag_blank_lines`, `grade_check_lyrics_blank_lines`,
`grade_check_cue_blank_lines`, `grade_check_filename_case`,
`grade_check_ext_case`, `grade_check_excess_tags`, `grade_check_mb_links`,
`grade_check_rym_links`, `grade_check_replaygain`,
`grade_check_album_description`, `grade_check_artist_image`,
`grade_check_artist_description`.

---

## 5. Audit workflow, rip evidence and overrides

The audit verdict is the one grade input that is *derived*, and the order of
evidence matters.

- **R21 — a CD's verdict is its rip's OWN evidence, and only that.** For
  `MEDIA=CD`, script 6 writes `AUDIT=REAL` exactly when every enabled leg
  passes: **(1) log score** — the disc's rip-log score (fresh from Logchecker,
  else the `LOG_GRADE` the tracks already carry) is at least
  `audit_log_score_threshold`; **(2) checksums** — the `.log`'s per-track
  `Copy CRC` equals the track's decoded PCM AND the `.log`'s own EAC SHA256
  verifies (a pre-1.0 EAC or XLD log is *unsupported*, which passes — nothing
  claimed, nothing refuted); **(3) AccurateRip** — the `.accurip` verdict is
  REAL. A leg that FAILS makes the verdict `FAKE` and the run log names the leg
  and its reason. A leg that cannot be EVALUATED at all leaves the tag untouched
  — no REAL, no FAKE — and the run prints `missing leg '<name>'` with the
  reason, so "we could not check" is never reported as "your rip is bad".
  **AudioAuditor never decides a CD in either direction**: its read is kept as
  evidence and a disagreement is logged as a warning
  (*AudioAuditor reports …, the rip's own evidence decides a CD*). It remains
  the verdict for every non-CD file, where there is nothing else to go on.
  `AUDIOAUDITOR_OVERRIDE` still wins over all of it (R25).
- **R22 — a verdict needs evidence, and is bound to its file.** Script 6 writes
  a verdict only where something verified it; a missing tool, a timeout or an
  `info`-only answer leaves the tag untouched and reports the file as
  *not verified*. The verdict is stamped with the file's size and mtime in
  `<music>/.mlo/data/audit_evidence.json`, and a file whose stamp no longer
  matches is re-audited.
- **R23 — `.accurip` files are per disc** (`CD-1.accurip`, `CD-2.accurip`) and
  are regenerated when a track is newer than the log, so a re-ripped disc cannot
  inherit its neighbour's verdict.
- **R24 — the AUDIT tag vocabulary is `REAL` / `FAKE`**; the album-level summary
  is `REAL`, `FAKE` or `Mix` (`summarize_audits`: FAKE wins, a uniform REAL
  passes through, anything else is Mix) and `None` when no track carries one.
- **R25 — `AUDIOAUDITOR_OVERRIDE` wins.** The track page's REAL/FAKE selector
  writes it; when it is set, per-track verdicts and the album's all-REAL gate
  both agree with it, and a forced re-audit reproduces the user's call instead of
  erasing it. *Auto* clears the tag and hands the track back to the detectors.
- **R26 — the readout and the written verdict are the same rule.** For a CD the
  live readout (library, album and track pages) computes the same three legs
  R21 does: all three pass → `REAL`, any leg fails → `FAKE` with the leg named,
  and a leg that could not be evaluated is named as *missing* and the stored
  verdict is shown as it is rather than guessed — one absence never costs two
  checks, because each leg's own artefact already has its own graded check
  (`LOG_GRADE`, `CRC` / `LOG_CHECKSUM`, `AccurateRip`). The
  evidence-satisfied reading survives only where it was written for: a track
  carrying NO stamped verdict whose own evidence (a verifying `.log` checksum or
  a REAL `.accurip`) proves the rip is reported `REAL`, with
  `audit_verified` naming which source proved it. The readout applies with
  `grade_check_audit` off, too.
- **R27 — the log's own documentation never overrules a verified rip.** A log
  with no verifiable EAC SHA256, one the tool cannot score, or one below
  `audit_log_score_threshold` is reported as such, but a track whose CRC just
  matched is exempt from those *log-file* gates. The grade has its own checksum
  checks (R30) that stand on their own terms.
- **R28 — `audit_cd_require_both`** (ON) decides whether AudioAuditor is run over
  `MEDIA=CD` at all; it can no longer downgrade a verified disc.
- **R29 — an unverified log-checker is not a bad rip.** A missed log-checker in
  a container is reported as unavailable, never as a failed rip
  (`audit_fail_on_unscorable_log`, ON, applies only where a scorer exists).
- **R30 — the rip's checksums are graded on their own terms**, independently of
  `grade_check_audit`: `grade_check_crc` (coverage plus CRC-32 equality) and
  `grade_check_log_checksum` (the EAC SHA256 verifies, or states none while
  `audit_verify_log_checksum` is on — but a log whose own header names an EAC
  version older than 1.0 is judged by neither: it predates the checksum, so
  nothing is claimed and nothing is refuted). Both can be switched off for a
  collection whose logs predate EAC checksums.

## 6. Tag families and what writes them

Five families group the tags the registry knows (`server/tags_registry.py`
`FAMILIES`): **identity**, **release**, **audio**, **lyrics**, **provenance**.
The default writer of everything else is *Beets tagging (14) · import*.

| Tag | Family | Written by | Graded by |
| --- | --- | --- | --- |
| `TITLE`, `ARTIST`, `ALBUM`, `ALBUMARTIST`, `TRACKNUMBER`, `DISCNUMBER`, `DATE` | identity | Beets tagging (14) · import | `grade_check_missing_tags` |
| `GENRE` | identity | Auto tagging (8) · genre import · Format all (10) trims | `grade_check_genre`, `_genre_count`, `_genre_order`, `_genre_vocab` |
| `MEDIA`, `SOURCE` | release | Format lyrics (1) · media/source normalization | `grade_check_media`, `grade_check_source` |
| `ITUNESADVISORY` | identity | Auto tagging (8) · advisory fetch | `grade_check_missing_tags` |
| `ALBUMITUNESADVISORY` | release | Auto tagging (8) · advisory fetch | `grade_check_album_tags` |
| `INSTRUMENTAL` | identity | Auto tagging (8) · instrumental fetch | `grade_check_missing_tags`, `grade_check_instrumental` |
| `MOOD`, `ENERGY` | audio | Auto tagging (8) · Mood & Energy (16) | `grade_check_mood`, `grade_check_energy` |
| `BPM`, `INITIALKEY` | audio | Key & BPM (12) | `grade_check_key_bpm` |
| `DYNAMIC RANGE` | audio | DR & ReplayGain (7) | `grade_check_missing_tags` (never on video) |
| `ALBUM DYNAMIC RANGE` | audio | DR & ReplayGain (7) | `grade_check_album_tags` |
| `REPLAYGAIN_TRACK_GAIN` / `_PEAK`, `REPLAYGAIN_ALBUM_GAIN` / `_PEAK` | audio | DR & ReplayGain (7) | `grade_check_replaygain` (opt-in family) |
| `AUDIT`, `LOG_GRADE`, `LOG_CRC`, `INTEGRITY` | provenance | Audit library (6) | `grade_check_audit`, `grade_check_log_grade`, `grade_check_excess_tags` |
| `AUDIO_MD5` | provenance | nothing — legacy, read only | `grade_check_excess_tags` |
| `AUDIOAUDITOR_OVERRIDE` | provenance | the track editor (manual) | `grade_check_audit` (wins over every derived verdict) |
| `LYRICS`, `UNSYNCEDLYRICS` | lyrics | Fetch lyrics (13) · lyrics editor | `grade_check_lyrics`, `_lyrics_format` |
| `TRANSLITERATION`, `TRANSLATION` | lyrics | Lyrics transliterate (AI) (17) | `grade_check_xlit_transliteration`, `_xlit_translation`, `_lyrics_lang_tags` |
| `ACOUSTID_ID`, `ACOUSTID_FINGERPRINT` | provenance | the import wizard's AcoustID apply (fingerprint match) — the PAIR in one save, verified by re-read — and Fix AcoustID pairs (21) for a file already holding half of one | `grade_check_acoustid` |
| `ENCODER_PROGRAM`, `ENCODER_QUALITY`, `ENCODER_VERSION` | provenance | Optimize FLACs (3) | `grade_check_encoder` |
| `MUSICBRAINZ_*`, `RATEYOURMUSIC_*`, `RELEASETYPE`, `CATALOGNUMBER`, `LABEL`, `BARCODE`, `ISRC`, `WORK`, `MOVEMENT`, … | release | Beets tagging (14) · import · MusicBrainz writes | `grade_check_album_tags`, `grade_check_mb_links`, `grade_check_rym_links`, `grade_check_naming` |
| `PERFORMER`, `PRODUCER`, `ENGINEER`, `MIXER`, `ARRANGER`, `DJMIXER`, `CONDUCTOR`, `WRITER`, `DIRECTOR`, `COMPOSERSORT`, `MUSICBRAINZ_COMPOSERID` | release | Beets tagging (14, `beets_credits`) · Auto tagging (8) — the release's own artist/recording/work relations, fetched in ONE request per album | `grade_check_excess_tags` (allowlisted, never foreign) |
| `ASIN`, `LANGUAGE`, `DISCSUBTITLE`, `LICENSE`, `ENCODEDBY` | release | Beets tagging (14) · Auto tagging (8) | `grade_check_excess_tags` |

Notes that are easy to get wrong: `MEDIA`/`SOURCE` belong to script 1, not to
script 8; `ACOUSTID_*` are written by the wizard's AcoustID apply — the
fingerprint/recording pair goes in with one save and is read back to prove it
landed, and a container the app cannot tag is reported per file — and by script
21, which completes a pair the file already holds HALF of (an id with no
fingerprint has the fingerprint recomputed locally; the reverse half needs a
lookup and is counted, never invented); `ENCODER_*` are written by the FLAC
optimizer (3) and, for images, by Process images (5).

A tag's VALUE is normalised on the way in as well (§7.6): the eight tags with a
closed value set hold the canonical spelling `mlo/tagtext.py` names, every
single-line value gets its spacing collapsed, and script 10 re-applies both over
an existing library. The writers' rules and the grader's `grade_check_tag_case`
/ `grade_check_tag_spaces` are the same functions, so the import can never
produce a value the grade would fail.

**The MusicBrainz import writes the release's metadata, not a subset of it**
(R15a). Every field MusicBrainz states that has a home in the container's tag
system is written — the identity and release tags, the whole credit set
(performer/producer/engineer/mixer/arranger/conductor/writer/director with their
roles, the composer id and sort name, from the release's artist, recording and
work relations, fetched in ONE request per album), `ASIN`, `LANGUAGE`,
`DISCSUBTITLE`, `LICENSE`, `BARCODE` and the full per-track `ISRC` list — and
each one is in the SAME `tag_key_allowed()` allowlist the excess-tags check and
the Format-All strip pass read, so a credit the app wrote is never reported as
foreign and never stripped. A field with no home in the container's tag system
(packaging, per-catalogue-entry labels, annotations) is not invented under an
ad-hoc key: it stays out, and the writer says which fields it could not place.

The two advisory tags answer to **different switches**, because different things
write them: `ITUNESADVISORY` to `advisory_auto_fetch` (the provider fetch — the
import step, the wizard and the *Fetch advisory rating* action) and
`ALBUMITUNESADVISORY` to script 8's *Auto Album Advisory* derivation
(`mlo/config.py::_TAG_WRITE_SWITCH`). The advisory fetch derives the album tag
too, with script 8's own rule, so a manual fetch never leaves it stale.

A fetch reports its provenance per track, and never invents one: per source the
STRONGEST answer wins (every ISRC the file or MusicBrainz states is asked, so a
later pressing's explicit answer is not lost to an earlier clean one), the
configured AI provider is a SOURCE of that merge — asked once per track and
ranked with the providers', so a stated 1 survives it, an AI 1 overrules a
stated 0 or 2, and an AI 2 never outranks a stated 0 — a
provider-stated 0 stays escalateable to 1 by a word-reading stage (the
configured AI, then the multilingual scan — the source says `(escalated)`), and
a track that already holds 0/1/2 is echoed back UNCHANGED with
`sources = ["existing-tag"]` unless the caller asks for a re-rate (`force`).
Even a forced re-rate rewrites only with evidence: the invented
`advisory_fallback` never overwrites a stored rating.

---

## 7. Quality bars

### 7.1 Naming, casing and extensions

- **R31** — the on-disk path (folders included) must equal the evaluated
  `naming_script`; the shipped script puts the artist MBID in the artist folder,
  the release id *and* release-group id in the album folder and the recording id
  in the file name, with `[Release type]`, both dates in full and a
  `{country - media - catalog}` brace group in which each segment appears only
  when the previous one is present.
- **R32** — `short_folder_names` truncates the UUIDs to 8 characters; grading
  accepts **both** spellings, so switching the flag does not fail a library by
  itself.
- **R33** — multi-value `RELEASECOUNTRY` / `LABEL` keep their **first** value, so
  one album always yields exactly one deterministic path.
- **R34** — `%releasetype%` is evaluated with the tag's own spelling
  (`Album; Live`); a missing tag with a cold cache is matched as a wildcard and
  reported as *Missing RELEASETYPE tag*, never as an invented path (no network
  call is made by the check).
- **R35** — letter case is part of the contract: `grade_check_filename_case`
  compares case-sensitively (a path differing only by case fails as `PATH_CASE`)
  and `grade_check_ext_case` requires lowercase extensions.
- **R36** — illegal characters (`< > : " \ | ? *`) become `_`, empty `[]`/`{}`
  groups left by omitted conditionals are removed, runs of whitespace collapse,
  and trailing dots/spaces are stripped.

### 7.2 Genre vocabulary, count and order

- **R37** — at most `mb_genre_count` genres per track (default **2**, hard
  ceiling `GENRE_COUNT_MAX = 3`) — a ceiling, never a quota: nothing is padded.
- **R38** — slot order: the **family first**, the specific genre(s) behind it
  (`Rock / Shoegaze`). A family in a later slot, or a repeated genre, fails as
  `GENRE_ORDER`.
- **R39** — every name must exist in MusicBrainz's 2 202-name list
  (`mlo/_genre_names.py`, canonicalized through `mlo/genre_vocab.py`); an alias
  table folds the spellings sources emit (`rnb` → `r&b`, `synthpop` →
  `synth-pop`). A name MusicBrainz does not publish is still stored (dropping
  what a source said is worse) and is what `GENRE_VOCAB` reports.
- **R40** — the family is *derived* from a curated 28-family table plus keyword
  rules, never asked of a model; a genre with no known family gets **no** family
  slot rather than a wrong one.
- **R41** — the stored value IS the display form, and it is title-cased with
  explicit exceptions (small connectives lowercased; `IDM`, `EDM`, `UK`, `R&B`,
  `DnB`, `DJ`, … upper-cased; each hyphenated chunk capitalized): every writer
  ends in `mlo/genres.py::display_name`, so a file holds `Rock; Shoegaze` and
  not MusicBrainz's own lowercase `rock`. **The display form is GRADED**: every
  name in the tag must equal `display_name` of its canonical spelling, and a
  lowercase name fails `grade_check_tag_case` as `GENRE_CASE` (naming the value
  and the spelling it should have) — script 10 rewrites it. The *vocabulary*
  comparison (`mlo/genre_vocab.py::canonical`, the grader's `GENRE_VOCAB` check,
  the alias table) folds case, so `Shoegaze` and `shoegaze` are the same genre
  to everything that judges the value; the case rule is about the value the
  file stores, not about which genre it is.

### 7.3 ReplayGain and dynamic range

- **R42** — the ReplayGain family is complete or absent: a file carrying any of
  `REPLAYGAIN_TRACK_GAIN`, `REPLAYGAIN_TRACK_PEAK`, `REPLAYGAIN_ALBUM_GAIN`,
  `REPLAYGAIN_ALBUM_PEAK` must carry all four; a file with none is never graded
  for them (`grade_check_replaygain`, ON; `replaygain_analyze_missing` lets the
  player measure on the fly instead).
- **R43** — script 7 writes the album gain/peak and the track gain/peak for FLAC
  and MP4 alike, using the ReplayGain 2.0 reference of **−18 LUFS**;
  `replaygain_skip_existing` (ON) leaves already-tagged files alone unless
  `force_dr_replaygain` is set. **The peak is the SAMPLE peak**, which is what
  rsgain (the writer) stores and what the ecosystem's readers expect: the
  on-demand measurement and the export writer must not measure true peak
  instead, or a file's own tag, its cached value and its export disagree about
  the same audio — measured at up to +39.6 % before this was pinned
  (`ebur128=peak=sample`, the sample-peak value read from `astats`).
  The GAIN is EBU R128 / ITU-R BS.1770 integrated loudness, verified within
  ±0.05 dB of rsgain's own number (the print resolution either implementation
  can produce).
- **R44** — DR expectations are the two tags the meter writes: `DYNAMIC RANGE`
  per track and `ALBUM DYNAMIC RANGE` per album (the meter's *Official DR value*,
  typically rendered `DR<n>`); they are graded through the required-tag sweep and
  the album-tag check, never on video files.

### 7.4 Lyrics format and sync

- **R45** — the storage target is `lyrics_format`: `EMBEDDED` (default), `LRC`
  or `BOTH`; a `.lrc` sidecar counts as lyrics only when real text survives
  stripping (a 0-byte file, a lone `[00:00.00]` stub or a metadata-only header
  is *absent*), and a sidecar shared by two same-stem files is credited to
  neither.
- **R46** — line timestamps are `[mm:ss.xx]` with `lrc_timestamp_precision`
  digits (2 or 3).
- **R47** — the canonical form is what the formatter produces: metadata stripped
  (`lrc_strip_metadata`), blank lines collapsed (`lrc_collapse_blank_lines`), no
  leading/trailing spaces, no merged double timestamps on one line
  (`_MERGED_TS_RE`; extended LRC's stacked timestamps are allowed only while
  `lrc_extended_enabled` is on).
- **R48** — the zero-timestamp rule is configuration, not taste:
  `lrc_add_zero_timestamp`, `lrc_zero_timestamp_blank` and
  `lrc_zero_timestamp_target` (`EMBEDDED` / `LRC` / `BOTH`).
- **R49** — word-level sync uses Enhanced LRC (`<mm:ss.xx>`) when
  `lrc_enhanced_enabled` / `lrc_enhanced_word_sync` are on, and
  `lrc_sync_level` (`LINE` / `WORD` / `SYLLABLE`) is what script 17 re-aligns to.
- **R50** — a transform is graded on whether it was *needed*: Latin-script
  lyrics must not carry a transliteration (`XLIT_UNNEEDED`) and non-Latin lyrics
  must (`XLIT_MISSING`); the same rule applies to translations against
  `lyrics_translation_langs`. Instrumentals and tracks without lyrics are never
  graded for transforms, and transform tags must name their language.
- **R51** — an answer without timestamps is discarded as if the provider had
  none; `lyrics_allow_plain` (off) is the only opt-in that lets plain text
  through. Automatic fetches only *write* a hit at a high confidence floor while
  the manual search keeps the loose one.
- **R52** — credits are not lyrics: the contributor block some providers return
  as the first line is dropped (becoming a blank line), and an instrumental is
  never given lyrics.

### 7.5 Covers

- **R53** — the canonical cover names are `cover.jpg`, `cover.jpeg`,
  `cover.png`, `cover.jxl`.
- **R54** — cover art is **not** embedded by default; `embed_covers` (off)
  flips script 10 to embed the album cover into every track (FLAC picture, MP3
  APIC, MP4 `covr`, OGG/Opus `METADATA_BLOCK_PICTURE`) at
  `embed_cover_jpeg_quality` / `embed_cover_resolution`.
- **R55** — the grade's cover rules are presence + size + squareness:
  `cover_target_size` (1200 default, per-format overrides
  `cover_jpeg_target_size` / `cover_png_target_size` / `cover_jxl_target_size`,
  0 = use the global), `cover_enforce_size`, `cover_enforce_square`,
  `grader_cover_size_tolerance_px` and the aspect test
  `grader_strict_square_threshold` / `cover_crop_threshold`.
- **R56** — per-track sidecar covers are graded under the same rules
  (`grade_check_sidecar_cover`), and any image that is neither the album cover
  nor a track sidecar fails `grade_check_extra_images`.
- **R56b** — cover CHOICE (`mlo/cover_choice.py`, the one policy the finder, the
  autonomous Covers step and Add-to-library all rank with) verifies every
  candidate against the album's OWN identity: a row whose stated artist/title
  contradicts the album is rejected (a karaoke/tribute or another album's
  release cannot win), a row stating a different track count is demoted, and —
  while `cover_resize_enabled` puts `cover_target_size` in force — an image
  whose size was never measured cannot be picked and the autonomous step refuses
  to store a below-target cover. A manual apply stays warning-only: the user
  picked that exact image.

### 7.6 Tag value spelling and spacing

- **R57** — a tag VALUE is written in the one canonical form its family has:
  `mlo/tagtext.py::canonical_value` resolves a closed-vocabulary tag (`MEDIA`,
  `SOURCE`, `RELEASETYPE`, `RELEASESTATUS`, `AUDIT`, `MOOD`) case-insensitively
  to the spelling `CANONICAL_VALUES` names, upper-cases a two-letter
  `RELEASECOUNTRY` code and gives `SCRIPT` its ISO 15924 casing (four letters,
  initial capital). A value the vocabulary does NOT know — a mood a person
  typed, a `SOURCE` that is really a video id, a release type MusicBrainz has
  since added — is returned unchanged rather than coerced into a wrong answer,
  which is what makes the rule idempotent. A multi-value tag is canonicalised
  per part and written as REPEATED container fields (`; `-joined on read):
  `RELEASECOUNTRY` carries every country the release's own events state, earliest
  first, and a file already holding one of them is completed rather than left
  short.
- **R58** — free text is untouched, byte for byte: `TITLE`, `ALBUM`, `ARTIST`,
  `ALBUMARTIST`, `LABEL`, `COMMENT` and the lyrics are somebody's words, and
  "AC/DC" and "k.d. lang" must survive a tag write. Only the tags in
  `CANONICAL_CASE` are looked at at all.
- **R59** — spacing is part of the value: a leading or trailing space/tab, or a
  run of two or more internal spaces, is wrong (`spacing_problem`) — fixed by
  the writers and by script 10, failed by `grade_check_tag_spaces`. A value
  carrying a newline is never judged and never collapsed: its whitespace is the
  text.
- **R60** — the rule is applied ON THE WRITE (`AudioFile.set_tag`,
  `set_any_tag`, `set_video_tags`), so the beets import, the import wizard, the
  auto-import chain, every script and a manual edit all land canonical; script
  10 (Format all) re-applies it over an existing library; and
  `grade_check_tag_case` fails a value the writers would have fixed. One rule
  in one place — the grader can never fail what a writer produces.

---

## 8. Recommended runbook

Nothing here is a substitute for the app's own Dependencies page: run it first
and install what the platform supports.

1. **Before touching anything** — set the music folder, then script **20 (Scan
   library layout)** and script **4 (Grade)**. Both are read-only: the grade
   tells you what is missing, and the layout report tells you where the
   canonical `<music>/Artists/<Artist>/<Album>/…` shape is not met (misplaced
   audio, stray files, empty folders, `wrong_case`). Script 20 writes that one
   report to `<music>/.mlo/data/` and the Library page warns from it, so the
   same facts are one click away from the album list.
2. **Fix the folders before the tags** — *Organize* (or script 14, whose beets
   config sets `move: yes`) applies `naming_script`. This is the destructive,
   path-changing step: run it when you are ready for every file to move, and
   expect the graders that compare paths (`grade_check_naming`,
   `grade_check_filename_case`) to keep failing until it is done.
3. **Run the pipeline** — Optimization → *Run All* (`run_all_order`). It is
   designed to be the whole job: 11 moves video containers first, 3 re-encodes
   lossless sources, 14 tags and organizes, 15 writes the tracklist manifest,
   2/1 canonicalize sidecars, 13/18 fetch and publish lyrics, 17 adds
   transforms, 8 writes mood/energy/genre/advisory, 5 normalizes images, 6
   audits, 7 measures DR/ReplayGain, 9 writes `.accurip`, 12 writes key/BPM, 16
   is the standalone mood pass, 10 is the final canonical pass, 20 reports the
   library's shape, 21 completes any half-written AcoustID pair and 4 grades.
   An **import** runs the same list minus 20 (R9): the layout report is about
   the whole library, so an import would only re-walk it.
4. **Re-run only what failed.** Every script is idempotent by default: it skips
   files that already carry the work, so a second *Run All* is safe and cheap.
   To redo a specific thing use its force flag (§2, R11) — that is the only way
   a script revisits work it has done.
   **A run has one scope, and it is the same for every script in it**: a
   library-wide run (the Optimize page's *Run All*, the CLI) makes every script
   discover the whole library for itself, and a targeted run (a selection, the
   wizard's *Run all scripts here*) makes every script work only on those
   targets. A script must never be handed an empty target list and left to
   report "nothing to do" — a run that changed nothing must be able to say why
   in terms of the files it looked at, not in terms of a scope it never had.

Safe to re-run at any time: **4** and **20** (both read-only), 2, 1, 5, 6, 7, 8,
9, 10, 12, 13, 15, 16, 17, 21 (it acts only on a file holding half a pair).
Re-running 3/11 only replaces files whose conversion/remux has not happened yet,
unless their force flags are set. **Needs a human decision**:

- `lossless_remove_original` (default **on**) — after a verified conversion,
  script 3 moves the original (WAV/AIFF/…) into the app's trash
  (`<music>/.mlo/trash/`, with its origin recorded), never out of existence:
  the lossless master stays restorable from the Trash page. Turn it off to keep
  both copies in place.
- `video_remove_original` (default **on**) — script 11 deletes the source
  container after a verified remux.
- `video_reencode_incompatible` (default **off**) — lossily re-encoding an
  incompatible video replaces the only copy, so it is opt-in.
- `embed_covers` (default off), `strip_unknown_tags` (on) and
  `metadata_review` (off) — the last stages candidates instead of writing them.
- **Script 18 publishes to LRCLIB**, a public database: it refuses when the
  database already answers for the recording, and `force_publish` overrides that
  refusal. Treat a `force_publish` run as an upload, not a local operation.
- `grade_check_audit` (off) and `audit_cd_require_both` / `audit_verify_*` decide
  how much audit machinery runs; on a Docker or Linux server, AccurateRip
  generation and the AudioAuditor/Logchecker path are unavailable by platform,
  and `GET /api/capabilities` says so.

---

## 9. Config keys that change a grade

Every key below is in `mlo/config.py` `DEFAULT_CONFIG`. The `grade_check_*` /
`grade_include_*` keys are the checks themselves (§3); the rest change what the
checks see or how they judge it.

| Key | Default | Effect on grading |
| --- | --- | --- |
| `grade_check_*` (59 keys) | all ON | switch one check on/off — every one ships on, including `grade_check_audit` (`mlo/config.py::STRICT_DEFAULT_KEYS`) |
| `grade_include_music`, `grade_include_cover`, `grade_include_description`, `grade_include_cue`, `grade_include_log`, `grade_include_lrc`, `grade_include_accurip`, `grade_include_video` | ON | a file category participates; off means its files are also "disallowed" for `grade_check_disallowed` |
| `grade_include_other` | ON | unclassified files participate |
| `grade_log_score_threshold` | 100 | minimum `LOG_GRADE` for `grade_check_log_grade` (0 disables the threshold) |
| `grade_verbose` | ON | per-track detail in the Grade report |
| `grader_cover_size_tolerance_px` | 0 | pixel tolerance on the cover size test |
| `grader_strict_square_threshold` | 0.0 | strict aspect tolerance |
| `cover_crop_threshold` | 0.0 | `grade_check_cover_crop` tolerance |
| `cover_enforce_size` / `cover_enforce_square` | ON | whether the cover's size/squareness is enforced at all |
| `cover_resize_enabled` / `cover_force_exact_size` | ON | whether the cover is expected to be the target size exactly |
| `cover_target_size`, `cover_jpeg_target_size`, `cover_png_target_size`, `cover_jxl_target_size` | 1200 / 0 / 0 / 0 | expected cover dimensions (0 = the global target) |
| `artist_image_aspect` / `artist_image_crop` | `1:1` / ON | the artist image's configured shape and whether it is enforced at all (off, or `cover_crop_enabled` off, means no aspect is graded; script 19 crops to the same value) |
| `artist_image_target_size` | 0 | the artist image's size ceiling (0 = the provider's native size, bounded by the 2000 px `mlo.artistdata.DEFAULT_MAX_SIDE`); only OVERSIZED fails, undersized is a note |
| `reencode_images` | ON | whether cover encoder tags are graded |
| `encoder_tags` | per-format map | which `ENCODER_*` markers `grade_check_encoder` requires (`ENCODER_QUALITY` / `ENCODER_VERSION` on, `ENCODER_PROGRAM` off, per format) |
| `strip_unknown_tags` | ON | whether `grade_check_excess_tags` reports junk tags |
| `mb_genre_count` | 2 (max 3) | `grade_check_genre_count` ceiling, and what script 8/10 trim to |
| `genre_autofill` / `genre_sources` | ON / `[rateyourmusic, musicbrainz]` | which writers can satisfy the genre checks |
| `mood_enabled` / `mood_source` | ON / `hybrid` | whether script 8/16 writes `MOOD`/`ENERGY` at all |
| `naming_script` | the shipped pattern | what `grade_check_naming` / `grade_check_filename_case` compare against |
| `short_folder_names` | off | 8-char ids (both spellings are accepted) |
| `music_folder` | — | the root the paths are compared against |
| `audiometa_key_notation` | `musical` | the notation `grade_check_key_bpm` accepts (`musical` / `camelot` / `openkey`) |
| `lyrics_format` | `EMBEDDED` | whether embedded lyrics, `.lrc` or both are required |
| `lyrics_allow_plain` | off | lets untimed lyrics count as lyrics |
| `lyrics_translation_langs` | `en` | the reader's language `grade_check_xlit_translation` is computed against |
| `optimize_lrc` / `optimize_embedded_lyrics` | ON | whether script 1 canonicalizes the form the format checks expect |
| `lrc_timestamp_precision` | 2 | digits in `[mm:ss.xx]` |
| `lrc_strip_metadata` / `lrc_collapse_blank_lines` | ON | canonical-form rules |
| `lrc_enhanced_enabled` / `lrc_enhanced_word_sync` / `lrc_sync_level` | ON / ON / `LINE` | word-sync expectations and what script 17 re-aligns to |
| `lrc_extended_enabled` | ON | whether stacked timestamps are allowed |
| `lrc_add_zero_timestamp` / `lrc_zero_timestamp_blank` / `lrc_zero_timestamp_target` | off / off / `BOTH` | the zero-timestamp rule |
| `append_final_newline`, `keep_empty_cue_lines`, `keep_other_cue_lines`, `cue_file_type`, `keep_empty_accurip_lines` | off/off/off/`WAVE`/off | the canonical form the CUE/`.accurip` checks compare against |
| `discs_rename_enabled` / `discs_rename_pattern` / `cue_fix_filenames` | ON / `CD-{n}` / ON | the disc-sheet naming and `FILE`-line rules |
| `audit_require_accuraterip` | ON | together with `grade_check_accuraterip`, whether a `.accurip` verdict can turn the album audit FAKE |
| `audit_verify_log_checksum` | ON | whether a log stating no EAC SHA256 fails `grade_check_log_checksum` (a log whose own header names an EAC version older than 1.0 is exempt either way — see R30) |
| `audit_check_cd_format` | ON | whether a `CD_FORMAT` failure turns the album audit FAKE |
| `audit_verify_cd_checksums` / `audit_integrity` / `audit_cd_require_both` | ON | what script 6 verifies on a CD rip |
| `audit_log_score_threshold` | 100 | the log score the audit accepts |
| `audit_fail_on_unscorable_log` | ON | whether an unscorable log fails where a scorer exists |
| `audit_thorough`, `audit_clipping`, `audit_scaled_clipping`, `audit_mqa`, `audit_ai`, `audit_fake_stereo`, `audit_silence`, `audit_dynamic_range`, `audit_true_peak`, `audit_lufs`, `audit_bpm`, `audit_cutoff_allow` | ON (`0` for the cutoff) | which AudioAuditor detectors run — they decide the verdict, not the grade directly |
| `write_audit_tag` / `write_log_grade` / `write_replaygain_tags` / `write_dynamic_range_tags` | ON | whether the scripts write the tags the checks require |
| `audio_tag_writes` | per-format map | per-filetype switches for the tag families (turning `ENERGY` off for one container removes it there) |
| `replaygain_skip_existing` / `force_dr_replaygain` | ON / off | whether script 7 revisits already-tagged files |
| `dr_replaygain_enabled` | ON | whether script 7 runs at all |
| `embed_covers` | off | whether cover art is embedded (script 10) instead of removed |
| `lossless_remove_original` / `video_remove_original` | ON | whether the pre-conversion/pre-remux file survives (the audio original goes to `<music>/.mlo/trash/`, restorable) |
| `library_codec` | `flac` | the codec script 3 (and every import) converts to — `flac`/`alac`/`wav`/`aiff`/`mp3`/`aac`/`ogg`/`opus`/`keep`. It changes grading: `grade_check_lossless_source` fails any uncompressed source (WAV/AIFF/APE/WV/SHN/TTA) still in the library, but **stands down** when the target is itself such a container or is `keep`, and `grade_check_cd_format` exempts a file that already is the configured lossy target. `wav`/`aiff` are library audio extensions (`mlo/paths.py:AUDIO_EXTS`) |
| `library_codec_quality` / `library_codec_bitrate` / `library_codec_args` | 5 / 0 / `""` | the conversion's compression level (FLAC `-0`..`-8`), its lossy rate (`0` = the codec's own default) and extra encoder arguments |
| `library_codec_optimize` | `lossless_to_lossy` | what script 3 may convert: a lossless source to the target (`lossless_to_lossy`), lossy sources too (`all`), or nothing (`keep`) |
| `lrclib_auto_publish` / `force_publish` | ON / off | whether script 18 publishes, and whether it overrides LRCLIB's refusal |
| `run_all_order` / `import_scripts` / `import_auto_scripts` | see R8 / R9 | what runs, and in which order |

Two keys in that table deliberately do **not** change a verdict on their own:
`grade_check_accuraterip` (AUDIT-only, R5) and `show_sidecar_files` (whether
the viewer computes per-file sidecar grades; it adds no check).

---

## 10. Honest limits of this spec

- The check **count** is 68 today; the registry derives it from
  `DEFAULT_CONFIG`, so a new check appears on the Grading page the day it exists
  even if this document has not caught up. The registry raises when a claim here
  points at a key the config does not hold.
- **Every external tool the engine drives stops at Windows' 260-character
  MAX_PATH** (they open files through the MSVC CRT), and a library named by the
  shipped script reaches that on its own — the artist folder carries an id, the
  album folder three more plus the dates and the media. `mlo/subproc.tool_path`
  is what bridges a longer path (the volume's 8.3 alias, else a temporary
  junction in `%TEMP%\mlo-longpath`), and it is applied to every `run_tool`
  argv. When it cannot bridge, the tool sees a path it cannot open and reports
  *it* — so a step that "found nothing" or "could not decode" on a long-path
  library is a bridge failure, not an empty library.
- Detection is heuristic where the evidence is: AudioAuditor's spectral
  detectors can disagree with a provably intact rip, which is why a verified CD
  rip outranks them (R21) and why `AUDIOAUDITOR_OVERRIDE` exists (R25).
- Grading never rewrites a tag. Every failure names the script that fixes it
  (`run organize`, `run Auto tagging (8)`, `run Audit Library`, …) and the Grade
  script stays read-only.
- Platform decides what is possible: AccurateRip generation, the Logchecker
  grade and the AudioAuditor audit need Windows-only tools, so a Docker/Linux
  server reports those checks as unavailable rather than failed
  (`GET /api/capabilities`).
