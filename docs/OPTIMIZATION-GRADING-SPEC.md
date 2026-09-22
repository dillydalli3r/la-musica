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
| Advisory ratings | `mlo/advisory.py` (the ladder and the AI rubric), `mlo/advisory_words.py` (the lexicon and its mild tier) |
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
- **R1a — an issue the verdict lists is an issue the verdict charges.** Every
  entry in an album's `issues` costs at least one failed check, so the dot, the
  percentage and the *N problems to fix* list can never disagree: an album
  cannot be a `PASS` while it displays a problem. A readout that names something
  the grade would not charge — the CD verdict's *nothing established … evidence* line
  (§5, R26) — is charged as its own check rather than left as a note beside a green
  verdict, and the artist rollup carries its own `pass` flag (`failed == 0`)
  instead of deriving one from the ROUNDED `grade_pct`, which at a few thousand
  checks rounds one failure up to `100.0`.
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
  artist folder that holds NO album folder at all (`ARTIST_EMPTY`): a folder
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
| `ARTIST_IMAGE_MISSING`, `ARTIST_IMAGE_CORRUPT`, `ARTIST_IMAGE_FORMAT`, `ARTIST_IMAGE_OVERSIZED`, `ARTIST_IMAGE_ASPECT`, `ARTIST_IMAGE_UPSCALED`, `ARTIST_DESCRIPTION_MISSING`, `ARTIST_FOLDER_MISSING`, `ARTIST_EMPTY` | artist-folder failures (script 19 clears the image ones). `ARTIST_EMPTY` is an artist folder holding NO album folder — only the artist's own image/description: the artist is not in the library, so the folder is not a graded artist. Script 20 reports it and the Optimization page can remove it to the Trash |
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
rather than kept as a second hand-written list. The exception set is **empty
today**: it held 20 (Scan library layout) while that runner walked the whole
music folder and wrote ONE report about the library, which an import would have
re-scanned once per album and then overwritten with a partial scan. Script 20
now scopes BOTH its scan and its fixes to `targets` when a run names them (and
stores no report for a scoped run, so a one-album pass can never become "the
last scan" the Library page warns from), which is what lets the import chain run
it per album — after 14 (beets has put the folder in its canonical place) and
before 4 (so the grade reads the fixed layout). A library-wide Run All still
gets the whole-folder pass and the stored report. `import_auto_scripts` (ON) off
still means "run nothing after import". **Every path that finishes an import
runs this chain and no wider one** — the bulk queue, the Soulseek import, and
the wizard's Finish step, whose script boxes ARE the chain (the ticked ids it
runs, and `Run the import chain` beside them) rather than the library-wide Run
All order; unticking a box is the user's own override for that one album. A
script the configured chain leaves out keeps its box, unticked, so nothing the
step used to offer became unreachable.
**R10 — a failing script is reported, never fatal**: the chain carries on and
per-script results are returned (`server/script_runners.py`).
**R10a — a script's report counts each file ONCE.** Every file a script looked
at leaves with exactly one verdict in its stats — written, skipped or failed —
so `scanned == modified + skipped + errors` holds for the run and a file can
never be reported as both written and skipped. Script 10 counted a file it had
just formatted as *skipped* as well, which made a pass that changed nothing look
like a pass that did something; a tag write that fails is now reported with its
file (`stats["errors"]`) instead of only bumping a counter. Script 12 (every
analysed file is scanned, whether or not the write changed anything) and script
18 (a published track is scanned) follow the same rule, and **a missing tool is
said out loud**: script 7 with `write_replaygain_tags` on and no `rsgain` reports
the missing tool as an error instead of passing a DR-only run that wrote no
`REPLAYGAIN_*` tag at all. The rule covers the same three-way split every runner
publishes, including the ones that write nothing until they do.

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
| 9 | AccurateRip | CUETools `.accurip` generation and verification; an existing file is regenerated only when a track's **audio** changed (each track's FLAC audio-md5, recorded per `.accurip` — a tag write no longer looks like a re-rip) | writes `CD-N.accurip` | no | **yes** (AccurateRip DB) |
| 10 | Format all | Final canonical pass: `.accurip`/`.cue`/`.lrc`/tag trim, the canonical tag-value spelling (`mlo/tagtext.py`) + embedded-cover policy | tags, sidecars, embedded art | **yes** (strips tags outside the allowlist) | no |
| 11 | Remux videos (MKV) | Any video container → MKV, video copied bit-exact when possible, audio to FLAC, chapters kept | video files | **yes** when `video_remove_original` (ON) | no |
| 12 | Key & BPM | librosa key/tempo analysis (every file it analyses is counted as scanned, changed or not) | `INITIALKEY`, `BPM` | no | no |
| 13 | Fetch lyrics | The configured synced-lyrics chain into `lyrics_format` | `LYRICS`/`UNSYNCEDLYRICS`, `.lrc` | no | **yes** |
| 14 | Beets tagging | Managed beets import with the naming script, work/movement tags | identity/release tags, file paths | **yes** (moves/renames, overwrites identity tags) | **yes** (MusicBrainz) |
| 15 | Release tracklist | Writes `.mlo_expected.json` from the release's own tracklist | adds a manifest file | no | **yes** (MusicBrainz) |
| 16 | Mood & Energy | The mood classifier alone | `MOOD`, `ENERGY` | no | no |
| 17 | Lyrics transliterate (AI) | Romanization/translation tags and sidecars, re-synced at `lrc_sync_level`; the per-track work runs through the worker pool (one track's chunk requests used to be paid one after another) | `TRANSLITERATION-*`, `TRANSLATION-*`, sidecars | no | **yes** (configured AI endpoint) |
| 18 | Publish lyrics (LRCLIB) | Submits missing lyrics to the community database (every examined track counts as scanned, published included) | nothing locally | no (external side effect) | **yes** (LRCLIB) |
| 19 | Optimize artist images | Re-fits `Artists/<Artist>/artist.*` to `artist_image_aspect` / `artist_image_target_size`, re-encodes as `artist.jpg`/`artist.png` | the artist image in place (only when it has to move) | re-encodes in place; never deletes | no |
| 20 | Scan library layout | The music folder's shape against `<music>/Artists/<Artist>/<Album>/…`: audio at the root or in an artist folder, stray files, unexpected folders, empty albums, `wrong_case` rows. FIXES the unambiguous three when `layout_apply` (ON) is set — a wrong-case name is renamed, audio outside an album folder is moved into the one its tags name, an album-less artist folder goes to the Trash — and reports the rest as left alone, with the reason. Writes ONE report describing the whole library (plus a `fixes` list) to `<music>/.mlo/data/`, which the Library page warns from; scoped to `targets` when a run names them, and library-wide when it does not (R9) | one report file + the renamed/moved paths | `layout_apply` | no |
| 21 | Fix AcoustID pairs | Completes an INCOMPLETE `ACOUSTID_ID`/`ACOUSTID_FINGERPRINT` pair — the failure `Missing ACOUSTID_FINGERPRINT (incomplete AcoustID pair)`, which had no fixer before. An id already on the file has its fingerprint recomputed locally; the reverse half needs a lookup and is counted, never invented | `ACOUSTID_ID`, `ACOUSTID_FINGERPRINT` | no | only when the id half must be looked up |

**R11 — force flags are the only way to redo work.** Each script has one, and it
is what makes the script look at a file it has already processed:
`force_lyrics` (1), `force_cue` (2), `force_reencode_flac` (3), `force_reencode_images`
(5), `force_audit` (6), `force_dr_replaygain` (7), `force_auto_tag` (8),
`force_accurip` (9), `force_audiometa` (12), `force_mood` (16), `force_xlit` (17),
`force_publish` (18), `force_tracklist` (15). Grade (4) needs none — it re-reads.
Scan library layout (20) carries `layout_apply`, the ONE key that turns work OFF
instead of forcing a redo: the scan always reports, and the key is what lets it
rename and move (see §2's row 20). A supplied force dict is authoritative AND
complete — every flag it does not name is cleared — so the *Re-run & overwrite*
menu sends a COMPLETE selection: a saved one is completed with the defaults
(`web/src/lib/force.ts::loadForceSel`), which is what keeps a switch added later
from being silently off for everyone who had ever opened the menu. A caller that
sends a partial dict gets the authoritative reading: the flags it names are set
and the rest are off. The menu on any selection sets exactly these keys.
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
**R78 — a run's progress is written ONCE, to both surfaces that show it.** The
header bar (a WebSocket push from `mlo.stats.progress_hook`) and MAINTAIN → In
progress (a 2 s poll of `server.job_locks`) are two transports for one fact, so
every frame goes through `job_locks.publish`: the row shows the same step text
("#10/21 · Key & BPM"), the same fractional position and the same `<at>/<of>`
pair the bar draws. They used to be written by different code at different
moments — the bar from the runner's own ticks for the CURRENT script, the row
once per FINISHED script — so the same run read as two different steps, and a
single-script run showed a live bar over a row that said only "working" until it
was over. A step is announced for every script the chain reaches, including one
that was skipped or unavailable.
**R78a — a strip never draws one producer's numbers under another's name.** The
wizard's own progress strip (`web/src/pages/ImportWizard.tsx`) shows the action
the user started and the relay frames above, and those two can disagree while a
run is starting. So the strip follows the frames of the action that is RUNNING:
an action that counts its own steps (the metadata rows, the per-album lyrics
fetch) draws its own counts; an action whose numbers the engine publishes draws
the relay's — the run's own text as the label, the `<at>/<of>` pair every frame
of a chained run carries as the readout, its fraction as the bar — and while an
import stage is still what is running, says so under the STAGE's own name. What
it may never draw is an import stage's percentage under the chain's label, or a
frame that was already on screen when the action began (the last finished
producer's report): those leave the bar indeterminate under the action's own
label, with the clock moving. Both of the wizard's chain bars (the strip and the
Finish step's own) draw from that one reading, so they cannot disagree.
`tools/test_chain_bar.py` pins it against the frames a real chain publishes.
**R79 — the worker budget is what the WHOLE run costs.** `worker_limit`
(Settings → Performance → "Worker threads", 0 = count them from the CPU) bounds
the worker pools (`mlo.stats.worker_count`) and, through
`mlo.stats.tool_threads`, the thread count each worker's native tool is given:
cjxl's `--num_threads`, the ffmpeg decode fallback's `-threads`. A pool of 2
encoders each claiming every core was still a 16-thread run — which a container
turns into CPU throttling, i.e. a slower run, not a faster one.
Nested pools DIVIDE the same budget instead of multiplying it: the CD checksum
decoders take one album's share of the audit pool (`worker_limit=2` used to start
eight of them, one constant 4 per album), and a copy of a chain's own parallelism
is never added on top. **A pool is sized by the ITEMS it has**, not by the
container they came in: Auto tagging's mood/energy stage decodes one track at a
time and is pooled per TRACK (script 16's shape), so a single-album import uses
the budget it was given instead of one lane per album — 15 tracks that used to be
analysed strictly one after another now take what two lanes can carry.
**R80 — an auto-updater restart cannot tear a file.** The bundled watchtower
service replaces this container's image whenever a release appears, so `SIGTERM`
— and `SIGKILL` once `stop_grace_period` runs out — can land at any moment,
including inside a write. Every writer therefore goes through `mlo.atomic` (temp
file beside the destination, fsync, ONE `os.replace`), and a file a killed run
leaves behind is the old file or the new one, never a half-written one. The last
writer that could not say that was `rsgain`: `rsgain easy` rewrites a track's
tags IN PLACE (TagLib has no other mode), so ReplayGain is now measured with a
scan and stored by the app's own writer (R43). `server.interrupt_recovery` then
sweeps this app's own leftover temp files, reconciles a framework album whose
audio arrived and reports the jobs a shutdown had to abandon, so the next start
says what happened instead of guessing. A chain also stops at a **script
boundary** once the shutdown has begun: uvicorn waits for the run's background
task BEFORE the lifespan teardown, so a chain that ignored the flag held the
container open past its stop grace (measured: a 13 s chain delayed `docker stop`
by itself, leaving the app's own 120 s wait and its journal unreachable) — it now
names the scripts it did not run and lets the container go.

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
| `grade_check_log_checksum` | Log checksum valid | ON | a log checksum that IS PRESENT must verify (`LOG_CHECKSUM`); one that is ABSENT is not required and costs nothing — XLD, EAC before v1.0 and a 1.0+ log whose `Log checksum` line is gone are judged by their per-track CRCs alone (R30) |
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
| `grade_check_tag_case` | Tags — canonical value case | ON | `MEDIA`, `SOURCE`, `RELEASETYPE`, `RELEASESTATUS`, `AUDIT`, `RELEASECOUNTRY`, `SCRIPT` and `MOOD` hold the canonical spelling `mlo/tagtext.py` writes (`TAG_CASE`), and every name in `GENRE` holds the form every genre writer ends on — `mlo/genres.py::display_name` of the name's canonical spelling (`GENRE_CASE`, e.g. `metal` → `Metal`). GENRE is deliberately NOT in `CANONICAL_CASE`: it is an open, multi-value tag whose canonical form is per name, not a closed vocabulary. Free text — `TITLE`, `ALBUM`, `ARTIST`, `LABEL`, lyrics — is never touched |
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
  `Copy CRC` equals the track's decoded PCM AND any EAC SHA256 the `.log`
  carries verifies (a log that carries none — pre-1.0 EAC, XLD, a stripped line
  — is *unsupported*/*missing*, and per R30 that is not required: nothing
  claimed, nothing refuted, the disc is judged by its CRCs); **(3) AccurateRip**
  — the `.accurip` verdict is REAL. A leg that FAILS makes the verdict `FAKE`
  and the run log names the leg and its reason. A leg that cannot be EVALUATED
  at all leaves the tag untouched — no REAL, no FAKE — and the run prints
  `missing '<name>' evidence` with the reason, so "we could not check" is never
  reported as "your rip is bad".
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
  are regenerated when the disc's **audio** changed, so a re-ripped disc cannot
  inherit its neighbour's verdict — and a tag write cannot make it look like a
  re-rip. The identity is each track's own number: FLAC's STREAMINFO audio-md5
  (`flac -t`'s own MD5), which a lossless re-encode keeps (the PCM is the same,
  so the CRCs are too) and a re-rip, a trim or a level change does not. What each
  `.accurip` was built from is kept in `<music>/.mlo/data/accurip_evidence.json`
  (this library's, like R22's audit evidence); a track whose container has no
  such md5 falls back to the older "any track newer than the `.accurip`" rule, so
  nothing is ever judged current on a weaker test than before. The first run
  after this check changes regenerates once, to record the identities.
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
  verdict is shown as it is rather than guessed. A missing leg is a **failed
  check** — charged once for the album, however many legs and tracks it covers —
  because the readout is rendered as a problem to fix and R1a forbids a pass
  beside a listed problem: an album whose AccurateRip leg has no `.accurip`
  behind it reads `FAIL` with that leg named, and the check clears when script 9
  establishes the leg or `audit_require_accuraterip` is switched off. The
  sentence the album lists is *nothing established the CD verdict's `<leg>`
  evidence for N track(s)* — the word is `evidence`, not `leg`: a reader
  hovering a row has no reason to know the app's internal name for a component
  of the CD verdict, and the reason clause that follows already names the
  artefact that is missing. A leg whose own gate is switched off, or whose half
  of the gate claims nothing (R30's absent log checksum), contributes **no
  state** and is not charged. The evidence-satisfied reading survives only where
  it was written for: a track carrying NO stamped verdict whose own evidence (a
  verifying `.log` checksum or a REAL `.accurip`) proves the rip is reported
  `REAL`, with `audit_verified` naming which source proved it. The readout
  applies with `grade_check_audit` off, too.
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
  `grade_check_log_checksum`. For the latter the rule is **present ⇒ must
  verify, absent ⇒ not required**: a checksum the log carries and that does not
  verify fails the disc (`LOG_CHECKSUM`, and the `checksums` evidence of the CD
  verdict), while a log that carries none claims nothing and refutes nothing, so
  it is judged by its per-track CRCs alone. That covers every way a log can
  arrive without one — XLD, EAC older than 1.0, and a 1.0+ log whose
  `==== Log checksum … ====` line is gone. `mlo.discs.check_log_checksum` still
  reports the distinction (`unsupported` vs `missing`) so the run log can say
  which case a log is in, and the `missing` case is logged as a warning; neither
  is charged. Requiring an absent checksum is what failed an honest 2008 rip:
  its log was written by a version that had no checksum to write, and "the
  machine could not check" must not read as "your rip is bad" (R2). A
  collection that wants the old strictness turns
  `grade_check_log_checksum`/`audit_verify_log_checksum` off; with every
  checksum that IS present still verified, both keys remain the escape hatch for
  a collection whose logs must not be trusted at all.

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
import step, the wizard and the *Fetch / refresh advisory rating* action) and
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
**The asymmetry is deliberate**: the UNATTENDED import path is fill-only —
`server.imports.finish_album` calls `fetch_advisories(..., force=False)`, and
that is the only caller that does — because nothing was pressed there, while
EVERY action a user presses sends `force: true` (the tag menu's *Fetch / refresh
advisory rating*, the wizard's *Auto-import advisory for all tracks*, the
metadata review's and the track page's *Check advisory + instrumental*, all
through `checkTrackValues`). So each surface a user presses offers exactly ONE
advisory action, and none of them is fill-only. Even a forced re-rate rewrites
only with evidence: the invented `advisory_fallback` never overwrites a stored
rating.

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
- **R39a — the source list ships complete, and the ask stops when a track is
  full.** `genre_sources` is a PRIORITY list: `GENRE_SOURCES`
  (`server/integrations.py`) is the shipped order — RateYourMusic, MusicBrainz,
  ListenBrainz, iTunes, Last.fm, TheAudioDB, Wikidata, Bandcamp, Discogs,
  Deezer, Spotify — and `mlo.config.DEFAULT_CONFIG["genre_sources"]` IS that
  list (`tools/test_genres.py` asserts the two are equal), and `normalize_config`
  treats every order this app ever shipped as "never customized" — the four
  older chains and the 11-source order that preceded the two-source one
  (`LEGACY_DEFAULT_GENRE_SOURCES`, `LEGACY_GENRE_SOURCES`) — so an untouched
  install follows the current default while a list the user edited is kept
  exactly as saved. The sources are asked in that order until every track holds
  what the writer would write (`_genre_complete` — the early stop): at
  `mb_genre_count = 2` one specific genre plus its derived family IS the track's
  answer, so the sources below the one that supplied it are never asked; a
  source that would only repeat the answer must not be paid a request for it,
  and the ones below are fallbacks rather than a second opinion. The surfaces
  offer ONE `Import genres` action with the source tray beside it: the tray
  lists every source the app knows, ticks the enabled ones, says what each one
  provides, and carries the reset back to the shipped default. An IMPORT runs
  this chain by itself (`server.imports.finish_album`'s genres step, whose
  release is the one the import resolved — no script is needed for it, and
  script 8 only trims what it wrote): the unattended download therefore lands
  with the genres its sources can supply, and a Genres family the user kept for
  themselves is both left alone AND reported as a gap, so the prompt still says
  what is waiting.
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
- **R41b — every label and reason the UI renders reads as a sentence, and a
  genre name inside one uses the app's OWN spelling of it.** A reason line on a
  Discover row, a recommendation row or an album card starts with a capital
  letter (`Genre: …`, `Sounds like …`, `More from …`, `More release groups by …`,
  `Similar to …`, `Chart #1 …`, `Most listened this month (… )`, `Same genre:`,
  `Same family:`, `Same mood:`, `Same artist:`, `Energy 45 near 60`,
  `Both 2007`), and the genre a line names is rendered through
  `mlo.genres.display_name` — the same Title Case the stored tag and the
  Discover genre list use (`server.discover._genre_display`, which leaves a
  provider's compound label such as `Rap/Hip Hop` as published), so a line can
  never read `Genre: alternative rock` beside a list that says `Alternative
  Rock`. What is SENT as a search or seed parameter keeps its own spelling, and
  machine-facing strings (query keys, JSON field names, log lines) are not
  touched: this is about the words a person reads. `server.recommend._reasons`
  and `server.discover`'s reason builders are the two places they are written;
  the `basis` chip that says what a shelf was seeded from follows the same rule
  (`Library genres: Shoegaze, Dream Pop · Top artists: …`).

- **R92 — genres fall back LEVEL BY LEVEL, and the level that answered is never
  hidden.** MusicBrainz states a genre at four levels — the recording (per
  track), the release, the release group and the artist — and the app asks all
  four, merging them in exactly that order (`integrations.genre_cascade`): a
  track whose own recording carries genres keeps them first, and everything it
  does not state is filled from the release, then the release GROUP, then the
  ARTIST. So a MusicBrainz genre is usable even when the specific track has
  none, which is the case that matters on a young catalogue. `genre_cascade`
  reports the four levels' availability (`levels`) and, per track, which level
  answered (`source`) and which were used (`levels_used`), and the genre chain's
  MusicBrainz source resolves the same way (recording → release → release group
  → artist, `_genre_source_answers`). Nothing is invented: an empty cascade is
  an empty answer, not a guessed genre.
- **R93 — every source answers at the finest level it has.** The chain
  (`integrations.genre_chain`, `mlo.config["genre_sources"]`) asks its sources
  in the configured priority order and stops once the writer's policy can write
  a COMPLETE list, so the entries below the answering one are fallbacks, not a
  second opinion. Each source's own level is fixed and documented in
  `GENRE_SOURCES`: rateyourmusic per track where its page states one, else
  album, then artist; musicbrainz per recording, then release, then release
  group, then artist; listenbrainz per recording, then release group, then
  artist; itunes per-track `primaryGenreName`; lastfm
  `track.getTopTags` → `artist.getTopTags`; theaudiodb per track;
  wikidata on the recording entity; bandcamp, discogs and deezer at ALBUM level
  (those services state no finer one) and spotify at ARTIST level. A source
  marked per-track that cannot answer per track answers nothing rather than
  passing an album's list off as a track's.

### 7.3 ReplayGain and dynamic range

- **R42** — the ReplayGain family is complete or absent: a file carrying any of
  `REPLAYGAIN_TRACK_GAIN`, `REPLAYGAIN_TRACK_PEAK`, `REPLAYGAIN_ALBUM_GAIN`,
  `REPLAYGAIN_ALBUM_PEAK` must carry all four; a file with none is never graded
  for them (`grade_check_replaygain`, ON; `replaygain_analyze_missing` lets the
  player measure on the fly instead).
- **R43** — script 7 writes the album gain/peak and the track gain/peak for FLAC
  and MP4 alike, using the ReplayGain 2.0 reference of **−18 LUFS**;
  `replaygain_skip_existing` (ON) leaves already-tagged files alone unless
  `force_dr_replaygain` is set. **rsgain measures; the app writes.** The scan is
  `rsgain custom -s s -a -O` — one album per call, because `custom -a` averages
  everything it is handed into ONE album row — and the four tags are stored
  through the app's atomic writer, with the text rsgain prints (gain as
  `<n.nn> dB`, peak as the linear value, verified character for character against
  the tags `rsgain easy` stores). `rsgain easy` is never pointed at a library
  file: it writes in place, which a SIGKILL mid-write can tear (R80).
  **The peak is the SAMPLE peak**, which is what rsgain measures and what the
  ecosystem's readers expect: the
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
- **R52a — both lyric surfaces carry the same size control.** `−`, a typeable
  percentage and `+`, in 5 % steps between **85 %** and **160 %** (100 % is the
  surface's own base size) — one shared control (`web/src/components/LyricZoom.tsx`)
  so the sidebar and the fullscreen player cannot drift. A typed value is
  clamped to the bounds and rounded to a whole percent, never snapped to a step,
  and each surface keeps its OWN value (`mlo.lyrzoom.sidebar.v1` for the
  sidebar, `mlo.np.lyrzoom.v2` for the player): resizing one must not
  re-lay-out the other.
- **R52b — the lyrics pane is the reader's, and its control lives in the
  player's own control row.** The fullscreen pane is shown and hidden by ONE
  toggle — the microphone, beside the queue / visualizer / options buttons
  (`web/src/components/NowPlayingView.tsx`) — never by a control drawn over the
  album art. The sidebar pane keeps the same concept: the player bar's
  microphone button, same icon, same pressed state
  (`web/src/components/PlayerBar.tsx`). The toggle is drawn only while the
  current TRACK carries lyrics (a control that cannot do anything is hidden,
  not rendered inert) and the choice is remembered per device
  (`mlo.np.lyrics`, like the other display picks). Show and hide are seamless:
  the pane stays MOUNTED and only its box animates (the app's 300 ms base
  motion step, as the player's other transitions use), so the reader's scroll
  position, the zoom and the active-line emphasis survive the toggle, the cover
  glides back to the middle of the row instead of jumping, and nothing is ever
  painted on top of the artwork. Pinned by
  `tools/check_fullscreen_player.cjs`.
- **R52c — the fullscreen player paints no panel on the artwork; its text sits
  on a veil.** Every floating surface of the player — the lyrics pane, the
  metadata block, the queue drawer and the player's own popovers — draws a VEIL
  rather than a background: the same polarity tint the ink table picks, under a
  backdrop blur, at low alpha and dissolving at its edges (`np-veil`,
  `np-veil-dark`/`np-veil-light`, `np-veil-pane`, `np-veil-pill`,
  `np-veil-panel` in `web/src/index.css`; the polarity is stamped once on the
  fullscreen root), so the picture stays visible behind the text and no
  hard-edged slab is drawn over it. The pane keeps its glyph shadow for the dark
  polarity, and R56c's keyboard `:focus-visible` rings are untouched.
  Measured, not eyeballed: `tools/check_np_metadata_contrast.cjs` (every
  metadata tier ≥ 4.5:1, 3:1 for the title, against the veil it now sits on) and
  `tools/check_fullscreen_player.cjs` (nothing painted over the art, at every
  window size it tries).

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
  picked that exact image. **The album's reference cover is the release GROUP's**
  (`coverartarchive.org/release-group/<rg>/front-500`): it is the image the
  finder shows beside the candidates, the wizard's Links/Covers preview, and what
  the policy's first rule prefers — a `/release/<id>/front` is one edition's own
  sleeve and ranks below even a name-searched row. Both are asked when both ids
  are known, and the group's cover being absent falls back to the release's.
- **R56c — a cover is never framed by a decorative border in the UI.** No
  border, ring or outline is drawn around a cover wherever it appears — the
  album grid, the library and list rows, the fullscreen player, the album
  header, the cover pickers, the menus. Covers keep their rounding, their
  placeholder background and their elevation shadow (`shadow-lg` /
  `shadow-2xl` are a drop shadow, not a frame). Two things are deliberately NOT
  that frame: the keyboard-only `:focus-visible` ring on whatever a keyboard
  user focuses (`web/src/index.css`, kept — without it there is nothing to see
  where they are), and a picker's own selection highlight, which must be
  transparent at rest so nothing is drawn until it is earned. Pinned by
  `tools/check_fullscreen_player.cjs` (the computed border/ring/outline of the
  player's art and of an album-grid card, plus the focus ring on the card's
  cover link).
- **R56d — a cover WRITE stores the image at the URL it was given, or nothing.**
  `POST /api/cover/fromurl` and the import chain's cover step fetch exactly the
  picked/chosen URL (`server.main._cover_url_bytes(..., substitute=False)` over
  `server.artcache.fetch_art`): when that URL cannot be fetched the request
  fails with its own sentence and no file is written. No other provider may
  answer in its place — an image neither the user nor the ranking chose is how a
  WRONG cover lands in the library, which is what happened when a row's URL was
  replaced by the album edit page's own release-group art. The single repair
  allowed is the same picture: an Apple storefront URL that answers HTTP 200
  with an EMPTY body is fetched as the same artwork's largest `image/thumb`
  transform (`server.integrations._artwork_big`, `source` "applemusic"), before
  any provider tier — and that repair is the only tier a write may reach. The
  cache is keyed, and only read, by THE URL THAT ANSWERED: an entry never holds
  a picture its own URL does not serve, so a fallback fetched for one album can
  never be served (or written) as another album's cover; entries from the older
  format, which could hold one, are not read at all. Display paths may still
  substitute (`substitute=True`), and the finder hands a row's OWN artist/title
  as the identity that fallback may be asked about — never the open album's
  release group. Pinned by `tools/test_artcache.py` (the keying, the repair, the
  write's refusal to substitute, the format stamp) and `tools/test_covers.py`
  (the autonomous step writes the winner's own bytes, and writes nothing when
  its URL refuses).

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

### 7.7 Advisory rating (`ITUNESADVISORY`)

`ITUNESADVISORY` is `0` (not explicit), `1` (explicit) or `2` (a clean/edited
edition); an absent tag is *unrated*, never 0. The value is decided by the ladder
in `mlo/advisory.py::decide_advisory`, which asks for evidence in a fixed order
and records which stage answered (`source`), plus the words it saw (`hits`).

- **R61 — the providers are a source, and the merge rule is `1 > 0 > 2`.**
  `server/integrations.py::resolve_advisory_route` asks Deezer/Spotify (ISRC),
  Apple, Discogs and YouTube, and `merge_advisory` settles what they said: a
  stated 1 beats everything, then a stated 0, then a clean edition's 2.
- **R62 — the AI judges the SONG, not its vocabulary.** With
  `advisory_ai_classify` (ON) and a provider configured, the model is asked once
  per track and fed the track's own lyrics, read off the file (embedded first,
  else the `.lrc` sidecar — the read `mlo/lyrics_publish.py::local_lyrics`
  does). Its rubric is the song's subject and tone, not a keyword count: `1` is
  excessive profanity, a slur or a very strong word, or graphic
  sex/violence/drug use; a mild word in passing — a lone `ass`, `damn` or
  `hell`, an idiom, a quoted word, a word ordinary in another language — is `0`.
  The lyrics may be in ANY language or script, and the model must judge them in
  that language rather than answering 3 because they are not English. Its answer
  is a SOURCE in R61's merge: it overrules a stated 0 or 2 only when it READ the
  words, is recorded in the reply's per-source map either way, and `3` (or an
  unparseable reply) falls through the ladder. Provenance ids: `ai-lyrics` (the
  words were read) and `ai` (they were not).
- **R62a — the reasoning effort is the user's, and `Max` degrades instead of
  failing.** `ai_effort` (default **high**) is what every AI call sends as
  `reasoning_effort` — R62's advisory judge, script 17's transforms, the
  connection check — and the genre ranking has its own `ai_genre_effort` (same
  default), which the same client reads in its place before the call
  (`server/genre_ai.py`). Both take `minimal`, `low`, `medium`, `high` or
  `max`: `minimal` sends NO effort field at all (no thinking, the fastest
  answer), any other value sends itself and then retries without the field, and
  `max` is a LADDER — `max`, then `high`, then no field — because not every
  OpenAI-compatible endpoint knows the word: dropping a rung reaches "the
  highest this provider accepts" rather than the provider's own default
  (`server/ai.py::ai_chat`). A value outside the five reads as `high`.
- **R63 — the word scan is the last resort, and its mild tier decides nothing.**
  With the AI off or silent, `mlo/advisory_words.py` scans the lyrics: only a hit
  from the lexicon's STRONG set makes a track `1`. The MILD tier (`ass`,
  `asses`, `arse`, `culo`, …) is reported in `hits` and never decisive, so a
  lyric whose only hits are mild is `0` like any clean track. Matching is
  whole-token — `ass` never fires inside `class`, `grass` or `bass` — with leet,
  censored (`f***ing`) and drawn-out spellings seen through, and LRC scaffolding,
  timestamps, section headers and provider credit lines are stripped before the
  scan. A track with no lyrics states nothing: the scan never reads silence as
  clean.
- **R64 — an instrumental is settled first, and the fallback is the user's.**
  `INSTRUMENTAL=1` with `auto_zero_advisory_for_instrumental` (ON) is `0` before
  anything is asked, and costs no AI call. When every stage above was silent,
  `advisory_fallback` decides: `0` (shipped), `2`, or `none` to write nothing at
  all. An invented fallback value never overwrites a rating a file already
  carries — only evidence lowers a rating.
- **R65 — a stated 0 is escalateable, one way.** A provider's 0 is not final
  (Deezer's `explicit_lyrics: false` also covers "not classified", Apple's
  `notExplicit` is the master's own flag), so the stages that read the words run
  as explicit-only signals and a STRONG hit turns the 0 into `1` with a source
  naming the signal (`lyrics-scan (escalated)`, `ai-lyrics (escalated)`). A mild
  hit is not a contradiction: the stated 0 stands, credited to its provider. The
  scan never turns a stated 1, or a clean edition's 2, into anything else.

### 7.8 Tool dependencies and update detection

The Dependencies page — and the setup wizard's copy of the same rows — answers
three different questions about each external tool and keeps them apart:
`state` says whether what is installed is behind the publisher's newest release,
`install_kind` says who installs it on this host, and `action` says what the
row's own button offers. `mlo/fetchdeps.py::dependency_rows` is the single
source of truth for all of them (`GET /api/dependencies`), so the page, the CLI
table and the auto-update worker cannot disagree.

- **R66 — a row that is behind says so, whoever installs it.** `state` is `ok`,
  `update` or `missing`; `update` means the upstream release the check found is
  NEWER than what is installed (`update_available`), and a tool the distro
  provides is still behind when its package is. A failed upstream check never
  becomes a state of its own: the rate-limited lookup leaves the row on the
  status its pinned target gives it and the note carries the failure
  (`upstream check failed: …`) — a wall of red for a transient 403 would be
  worse than no check at all. `state` is never forced back to `ok` because this
  app cannot fetch the tool, and the header's *N update(s) available* counts
  exactly the rows whose chip is amber — one predicate, so the count and the
  table cannot disagree.
- **R67 — what can be done is a separate fact.** `install_kind` is `deps` (the
  installer fetches a pinned Windows build, a native Linux build, a `.deb` or a
  pip package into the tools folder), `system` (the OS package manager owns the
  tool) or `unsupported` (no build for this platform), and `action` is
  `install` / `update` / `upgrade` / `none`. A `deps` row installs and updates
  in-app, with the row's own button; a `system` row behind upstream offers
  `upgrade`, whose `upgrade_command` is the exact package-manager command for
  this host (`apt-get install --only-upgrade <pkg>`, built from
  `LINUX_PACKAGES`) — copied, never executed, because the app does not drive a
  package manager. `Copy command` is that row's alone: a row this host can
  download never offers a command instead of its button, and a tool with no
  install path and no command shows neither.
- **R67a — the tools live with the library, and the old folder is still read.**
  Every install goes to `<music>/.mlo/tools` (`mlo.paths.tools_dir`, derived
  from the music folder on every call — a music-folder change must not leave a
  stale path behind, which is what made a module-level constant the wrong shape
  for it). Detection and the installer's "is it already installed?" question
  read that folder AND the pre-move `<app folder>/.dependencies`
  (`mlo.paths.legacy_tools_dir`), so a tool installed before the move is never
  reported as missing; only the first is ever written, and the row's note says
  when a copy came from the old one. On Linux, `deps` is also what `libjxl` and
  `libjpeg_turbo` are: upstream's build for them is a `.tar.lz` (libjxl, fully
  static, x86-64 only — it publishes no ARM asset at all, so an ARM host keeps
  the distro package) and a `.deb` (libjpeg-turbo, one per architecture),
  unpacked in app without dpkg or 7-Zip (`fetchdeps._extract_archive`). A `.deb`
  whose binaries name their shared library by an absolute RPATH travels with
  that library and is run through a launcher, so an install can never land a
  tool the host cannot start.
- **R68 — the page's own action is always on screen.** Install/Update-all is
  sticky (it does not scroll away with the first rows) and is disabled only when
  there is nothing this host can install, with the reason in its title. It never
  disappears, and it never offers a row this host cannot install.
- **R69 — detection matches what the installer writes.** A tool installed into
  the tools folder must be what detection reports (`mlo/tools.py`'s per-tool
  `_exe` field map, native Linux builds included), or an update the installer
  performed would be invisible, the row would keep reading the PATH copy, and
  its amber chip could never clear.

---

### 7.9 Export processing and destination

An export is a copy of the library, so nothing it does may reach back into the
library files — and what it does to the EXPORTED copies has to be the thing the
user asked for, in the order they asked for it. `server/exporter.py` owns both.

- **R70 — the destination is the client's or the server's, and the client's is a
  zip.** `export_target` is `zip` (the client downloads one archive) or `server`
  (a folder the machine running this app can see, chosen with the drive picker).
  The zip target stages the export under `<music>/.mlo/data/export_zip/<id>/`,
  packs it with whatever the run wrote (the `.m3u8` playlists only when
  `playlists` is on — OFF by default, see R75 — and the manifest when
  `export_manifest` is), answers
  `zip: {id, name, bytes, files, url}` and serves it from
  `GET /api/export/zip/{id}` with `Content-Disposition: attachment`; one archive
  is kept at a time and a new export replaces it. `prune` is meaningless for a
  zip and is reported as ignored rather than silently pruning the staging folder.
- **R71 — ReplayGain has two modes and they are not the same thing.**
  `export_replaygain_mode` is `off`, `tags` (measure with
  `ebur128=peak=sample` + `astats` and write `REPLAYGAIN_*`) or `apply` (rewrite
  the audio so the files are level). `apply` uses the ALBUM gain when the
  selection covers a whole album — that is what keeps the album's internal
  balance — and the track gain otherwise, rides the gain in the SAME encode
  (`volume=<gain>dB`), and strips `REPLAYGAIN_*` from the output, because a
  player would otherwise apply the gain twice. A track that would clip after the
  gain is reported, never silently distorted.
- **R72 — an equalizer profile is the user's own file, and its losses are
  named.** `export_eq_profile` selects a built-in preset or a profile imported
  from **Equalizer APO / Peace EQ** text (`mlo/eq.py`): `Preamp:`, `Filter N:
  ON|OFF PK|LS|HS|LP|HP|BP|NO|LSC|HSC Fc … Gain … Q …`, `GraphicEQ:` band lists, free
  field order, optional units, case-insensitive keywords. OFF filters are
  skipped; a line with no equivalent (`Include:`, unknown constructs) is IGNORED
  and REPORTED in `unsupported` rather than dropped, while a BAND line that
  cannot be read is an ERROR naming its attribute or its line, and an import of
  it is REFUSED whole — a profile missing the band that failed to parse is not
  the curve the user asked for. Profiles live in
  `<music>/.mlo/data/eq/` with a sanitized id and a 64 KiB cap.
- **R73 — the chain order is ReplayGain gain → EQ preamp → EQ filters →
  encoder**, and processing requires a real codec: a copied stream cannot be
  filtered, so `copy` with `apply` or an EQ profile fails with one message that
  the endpoint and the UI share (`_PROCESSING_NEEDS_CODEC`). A profile that
  cannot be found — or cannot be READ, a file with a band line this app refuses
  — fails the track naming it, never a silent export without the curve the user
  selected.
- **R74 — a processing change is not "the same export".** The skip/duplicate
  decision carries a processing signature (`replaygain=apply eq=<id>`), so
  re-exporting with a different curve re-encodes instead of being skipped as
  identical to the previous run.
- **R75 — an export carries AUDIO; everything it leaves behind is reported.**
  An album export writes no `.accurip`, `.log`, `.cue`, `.txt`, `.jpg` or
  `.m3u8` file: the cover travels EMBEDDED in each exported file
  (`embed_covers`, ON) and the rip's evidence stays in the library where the
  audit, the grading and the log's own checksum read it. Two switches keep the
  old behaviour available and both are OFF by default — `export_sidecars`
  (mirror `cover.*`/`description.txt`/artist image/`.lrc`/`.cue`/`.log`) and
  `export_playlists` (the per-album `.m3u8` plus `all.m3u8`) — and the run
  result reports what did not travel in `excluded` (one row per file: album,
  name, `kind` ∈ cover / playlist / sidecar / evidence / unknown, and the
  reason), with `excluded_counts`, `excluded_total` and the sentence
  `excluded_note` that also goes to the run log. Nothing is dropped in silence:
  an unexpected `.nfo`/`.md5`/`.sfv`/`.pdf`/`Thumbs.db`, a `.bak` nobody
  anticipated and a stray subfolder are all classified and counted. The
  `export_manifest` key (OFF) still writes `checksums.sha256` listing every
  written file, so a copied library can be proven intact at the other end.
  **A PLAYLIST export is not an album export**: `server.playlists.export_m3u8`
  still writes `.m3u8` (the Playlists page's *Download .m3u8*), and
  `sidecars`/`playlists` ON still write the album's own files for a device that
  wants them.
- **R100 — every filename the app writes obeys ONE rule, and it is
  `mlo.naming.sanitize_segment`.** A character a filesystem refuses —
  `< > : " / \ | ? *`, an ASCII control character (0x01–0x1F; NUL is left alone
  because it can never reach a file and `mlo.grader`'s `UNKNOWN_RELEASE_TYPE`
  sentinel is spelled with it), a trailing dot or space, and the reserved
  device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`, with
  or without an extension, any case) — becomes `_`. Replacement, never
  deletion or transliteration; ONE `_` per character, so `A***B` is `A___B`;
  and `sanitize_segment(sanitize_segment(x)) == sanitize_segment(x)`, so a
  second organize or export of an already-named library is a no-op rather than
  a rename. `sanitize_path` applies the same rule per name and keeps `/` as
  STRUCTURE; `_run` applies it to every substituted tag value, so a `/` inside
  a TAG ("AC/DC" in a TITLE) becomes `_` and can never invent a directory
  level. The subfolder under the export root (`safe_subfolder`), the CUE
  sheet's own name (`re_safe_filename`), the Soulseek import folder
  (`_safe_component`) and the organizer (`eval_script` → `sanitize_path`) all
  call this one function: a second spelling of the character set is how a path
  the app WROTE stops being a path the app can FIND again.
- **R101 — the tags keep the truth; only the NAME on disk changes.** A `TITLE`
  of `AC/DC` is written to the file as `AC_DC.flac` and the tag inside stays
  `AC/DC`, byte for byte: the library's data is the evidence, and sanitisation
  is a naming rule, never a data rule. `.log` and `.cue` CONTENT is never
  rewritten by sanitisation (a `.log`'s checksum is verified over its raw bytes
  — see R-§3), and a sidecar that is copied is copied verbatim.
- **R102 — the audit and the grading match by the same rule.**
  `mlo.naming.name_key` is applied to BOTH sides wherever a name recorded by
  another program meets a name on disk: the graded CUE check
  (`grade_check_cue_files`, which no longer reports a file the app itself
  renamed as missing), the CUE repair and disc resolution
  (`mlo/discs._norm_name`, `fix_cue_filenames`, `rename_cues_for_discs`), and
  the `.accurip` generator's cue→disc and cue→WAV maps (`mlo/accurip`).
  `mlo.naming.cue_ref_names` gives every spelling a `FILE "…"` reference may
  denote — the whole reference keyed through the rule, and its leaf — because a
  `/` in a reference is usually a directory part and occasionally a character
  the app wrote as `_`; splitting on the separator first is how
  `01. AC/DC - Theme.flac` used to become `DC - Theme.flac` and match nothing.
  Log→track attribution itself is numeric (`_track_num_of`), so it never
  depended on spellings.
- **R96 — the folder structure is the library's own shape, and a custom one is
  the same grammar.** `export_structure` is `albumartist_album_disc` (shipped),
  `album`, `flat`, `mirror`, or `custom`; a custom one evaluates the user's own
  script from `export_structure_script`. Both the shipped layout and the custom
  one ARE naming scripts (`mlo/naming`: `%field%` substitution, `$if()`, `/` for
  folders), so presets and custom share one evaluator, one vocabulary and one
  validator — a `%field%` or `$function` the grammar does not implement is
  refused, never silently evaluated to "". `GET /api/export/structures` serves
  the menu's keys and labels and that vocabulary (the page renders THIS, so the
  dropdown cannot offer what a run would refuse), and
  `POST /api/export/structure/preview` evaluates a typed script against a sample
  track and returns the same sentence the run refuses with. The shipped
  structure writes ALBUMARTIST, never the track's own ARTIST (a compilation is
  ONE folder, not one per track), and the library's own file name —
  `%discnumber%-$num(%tracknumber%,2) %title%`, i.e. `1-01 Title`, the disc
  number written for a SINGLE-disc album too, because that is what
  `mlo.naming`'s default script produces and an export must read like the
  library it was copied from. A structure that names no path — empty, unknown,
  or a selection whose tags it cannot use — is refused with a sentence BEFORE
  anything is written (the endpoint answers 400); a script that names nothing
  for one file fails that file with its own message rather than dropping it into
  the export root. The layout this replaced (`artist_album`, whose label
  promised "Artist / Album / 01 - Title") is MIGRATED in `mlo/config.py` rather
  than kept selectable: the key means "the tree this app ships", so a saved
  value moves to the new layout and the key can never name a tree the app does
  not write. (`artist_album_disc`, advertised by Settings for years without ever
  being implemented, migrates there too.)
- **R97 — an export configuration is the Export page's form under a name.**
  A saved config is ONE JSON file per name at
  `<music>/.mlo/data/export_configs/<slug>.json`, named by the same
  name→one-safe-path-segment rule the imported EQ profiles use
  (`mlo.paths.slug_name`) and implemented in `server/exportconfigs.py`. The
  stored shape is `{"name": "<what the user typed>", "config": {…}}`, and
  `config` is exactly what `POST /api/export` takes, key for key, so a loaded
  config can be posted unchanged. What it holds is what decides what the export
  IS: the destination (`target`, `dest`, `subfolder`), `codec`, `quality`,
  `structure` and `structure_script`, the cover options (`embed_covers`,
  `embed_cover_jpeg_quality`, `embed_cover_resolution`), `id3v2`/`id3v1`,
  `replaygain_mode`, `clean_tags`, `playlists`, `sidecars`, `manifest`,
  `verify`, `prune`, `workers`, how lyrics travel (`lyrics`), the equalizer
  profile **by id** (`eq_profile`)
  — so a load points at the profile itself rather than at a copy of its curve —
  and `source_kind`, the Export page's source tab, stored verbatim for
  whichever surface has tabs. What it deliberately does NOT hold: the
  selection (which playlist, which albums/artists/tracks are ticked) — data,
  not configuration, because a config carrying paths would export something
  else after the library moved — and the page's filter box, which is a view
  aid. Its keys are whitelisted from the exporter's own tables
  (`server.exporter.FORM_FIELDS` minus `paths`, plus `EXPORT_DEFAULTS`), and
  the enumerated values a run would refuse — an unknown codec, folder
  structure, export target or ReplayGain mode, or a profile id that could name
  a file outside the profile folder — are refused at SAVE time with the run's
  own sentence (the folder structure uses `exporter.structure_error`). Saving
  under a name that already exists REPLACES that config; the response's
  `replaced` flag says which happened. The endpoints are `GET
  /api/export/configs` (list, newest first), `POST /api/export/configs` (save:
  `{name, config}`), `GET /api/export/configs/{id}` (load) and `DELETE
  /api/export/configs/{id}` (drop); an unknown id is a 404 and a path-shaped
  one a 400. Every row, list and load alike, carries `eq_profile`, `eq_missing`
  and `eq_problem`: a config outlives the profile it was saved with, so a
  profile that has been deleted or renamed is REPORTED — list rows are marked,
  the load says "its equalizer profile '…' is gone — pick another profile
  before exporting", and the run refuses that profile — never a silent
  fallback to another curve.
- **R98 — the EQ profiles the app accepts are Equalizer APO / Peace files,
  both shapes, and their approximations are stated.** ONE parser
  (`mlo/eq.py`) reads both. The first shape is the Equalizer APO / Peace text:
  `Preamp: -6.5 dB`, `Filter N: ON|OFF PK|LS|HS|LP|HP|BP|NO|LSC|HSC Fc … Gain
  … Q …` (or `BW …` instead of `Q …`, converted with APO's own BW→Q relation; a
  missing Q takes the type's standard width), `GraphicEQ: 25 0; 40 0.5; …` band
  lists — free field order, optional `Hz`/`dB` units, case-insensitive
  keywords. `Filter N: ON None` is APO's empty slot and is skipped, not treated
  as a band. The second is Peace's `FilterCurve:` export: ONE line with no
  trailing newline, `FilterCurve:f0="10" f1="11.7" … v0="0.03" v1="0.043" …` —
  `fN` the frequency of point N and `vN` the value there, paired BY INDEX,
  because there is no separate gain list — followed by the curve's meta
  attributes (`FilterLength`, `InterpolateLin`, `InterpolationMethod`,
  optionally `Preamp`). The points are read from the file: a real Peace export
  runs 10 Hz → 18.9 kHz on no clean geometric ladder (50 points, ~5% off a
  geometric ladder), so no fixed frequency table is assumed. The bytes are
  decoded per their own encoding — UTF-8 with or without a BOM, UTF-16 when its
  BOM says so, otherwise the Windows code page — and CRLF and CRLF-less lines
  both parse. A `FilterCurve` is a CONVOLUTION curve while the app's model is a
  chain of ffmpeg biquads, so the mapping is stated in the import result rather
  than implied: one peaking filter per point (never a subset), each band's Q
  taken from its own neighbours' spacing (so a denser ladder gets narrower
  bands, not the octave-wide Q 1.41 the `GraphicEQ` conversion uses), the curve
  exact AT the file's points and an approximation between them, the file's own
  interpolation (`InterpolateLin=0` with `InterpolationMethod="B-spline"`, or
  `InterpolateLin=1` for straight lines) stated as approximated — never
  silently flattened to straight lines or dropped — and `FilterLength` (the
  convolution's tap count, i.e. a linear-phase FIR) reported because
  minimum-phase peaking bands do not carry that phase response. Two kinds of
  bad input are treated differently. A line with no equivalent (`Include:`,
  `Convolution:`, `Device:`, a Peace banner, an unknown `FilterCurve`
  attribute) is IGNORED and REPORTED in `unsupported`; a BAND line that cannot
  be read (an unknown filter type, a non-numeric frequency, gain, Q or BW, a
  `GraphicEQ` group that is not one `frequency gain` pair, a `FilterCurve` `vN`
  with no `fN` or the reverse, a non-numeric `vN`) is an ERROR naming the
  attribute or the line number, and the import of that file is REFUSED whole —
  a profile missing the band that failed to parse is a different curve, which
  for audio is worse than a refusal. A file whose lines are all ignorable
  imports as an explicitly EMPTY profile (`empty: true`, and its result says it
  exports the audio unchanged), never as a flat curve. Profiles live in
  `<music>/.mlo/data/eq/<slug>.txt` with a 64 KiB cap; presets and imported
  profiles share ONE id space (`eq_profile` names one of them), and a profile
  an export names that cannot be found or cannot be read fails the run naming
  it — never a silent export without the curve.

- **R99 — lyrics are the user's choice, and the export defaults to the
  library's own.** `export_lyrics` (and the run option `lyrics`) is `embedded`
  (the `LYRICS` tag inside the file), `lrc` (a `.lrc` beside the exported file)
  or `both`; the shipped value is `""`, which means "whatever the library
  keeps" — `lyrics_format` — so an export writes lyrics the way the app itself
  does until the user says otherwise, and the two settings cannot silently
  disagree (`server.exporter.lyrics_mode`; the Export page's default comes from
  that same resolver). The text and its form come from `mlo.lyrics`, the one
  code path that already writes lyrics for the library: the sidecar leg reads
  the source's own lyrics (its `.lrc` when that holds lyrics, else its tag,
  `read_lyrics`), canonicalises them with the formatter the format pass uses,
  and writes them under the exported track's OWN name (`write_lyrics_sidecar` —
  the same name rule, so nothing here invents a second sanitiser); a source
  whose lyrics live in a FILE has them embedded when the mode wants the tag, so
  no mode can lose them. A `.lrc` is never inherited from the source's tag list:
  when the mode writes lyrics as a file, the lyrics tags are DROPPED from the
  exported file (`_LYRICS_TAGS`) — on the transcode path through the tag write
  and on a byte copy through an explicit strip — because a file carrying both
  is a player showing a second, stale copy. In the audit, a source `.lrc` whose
  track IS in the selection is output when the mode writes `.lrc`, and is
  reported as `lyrics` (the one non-audio kind the run itself can account for)
  when it is not; the `.lrc` of a track outside the selection is always
  reported. The run result carries `lyrics_mode` and the number of `.lrc`
  files it wrote.

### 7.10 YouTube, cookies and the Soulseek port

- **R76 — the app's yt-dlp calls honour ONE cookie setting.**
  `youtube_cookies_mode` is `none`, `file` (the jar at
  `<music>/.mlo/data/cookies.txt`, saved by pasting or dropping a Netscape
  `cookies.txt` in Settings → Videos) or `browser` (`youtube_cookies_browser`,
  e.g. `chrome`). The setting reaches BOTH yt-dlp paths (its Python API and the
  vendored binary) at all three call sites — the search, the video download and
  the YouTube-captions fetch — because an age-gated video is exactly the kind
  that needs a signed-in jar for any of them. The jar is validated as a Netscape
  cookie file on write, capped at 512 KiB, and reported back with its cookie
  count and domains.
- **R81 — the RateYourMusic credential can be imported from a cookies.txt.**
  RYM has no API: `rym_cookie` is the user's own signed-in session cookie, and
  RYM's `session` cookie is HttpOnly — no script and no "copy the Cookie header"
  from devtools can ever see it, so a browser extension's Netscape `cookies.txt`
  export is the only way most users can hand it over at all. Settings →
  Discovery therefore takes the file (pasted or dropped) as well as the manual
  header paste: `POST /api/rym/cookies` parses it with the SAME parser the
  yt-dlp jar uses (`server/api_youtube.parse_cookie_file` — one file-or-junk
  rule for both), keeps ONLY the cookies whose host is `rateyourmusic.com` or a
  subdomain of it (the export carries every site the profile holds), and writes
  the survivors into `rym_cookie` in file order, as the exact
  `name=value; name=value` string the manual box accepts, through the app's own
  config writer — so `_rym_cookie`/`_rym_cookiejar` pick it up unchanged, the
  box and the import cannot disagree, and a restart keeps it. `rym_cookie`
  remains the ONE place the credential lives (no second file, no second config
  key). A file with no `rateyourmusic.com` cookie stores NOTHING and says why
  (a signed-out tab's export must not cost a working session); junk and an
  oversize body are refused before any write; and no route — `GET
  /api/rym/cookies` included — ever returns, logs or shows a cookie VALUE: the
  panel is told the cookie NAMES, the count, whether `session` is among them
  (without it RYM answers as a guest) and warnings as sentences.
- **R82 — the download queue has exactly two limits, and the overflow WAITS.**
  `soulseek_search_concurrency` (3) is how many RELEASES run at once and
  `soulseek_candidate_slots` (3) is how many candidates of one release download
  at once; both are enforced by the app itself, never delegated to slskd. A
  release that arrives at the ceiling is NOT refused: it takes its place in the
  pipeline's waiting queue (the Queue tab's **Waiting** group, one row per
  release, with its 1-based `position` and its `waiting` flag on the polled
  payload), is cancellable there without ever starting, and starts by itself
  from the finish path of whichever release frees the slot
  (`soulseek_auto._finish → _start_next`) — so it never depends on a second
  user action. `POST /api/soulseek/downloads/cancel` takes `ids` (the
  `pipeline:<key>` / `job:<id>` rows of one selection) and answers `cancelled`
  + `ids` + `missed`; `POST /api/soulseek/downloads/clear` with
  `scope: "queued"` is **Clear all**: it removes exactly the queued/waiting
  releases, needs no running slskd, and leaves a RUNNING release alone (that is
  a per-row cancel). slskd's own `soulseek_download_slots` is the OUTER ceiling
  on the transfers this product creates and is shipped as the product itself
  (3 × 3 = 9); a config whose slots are narrower than its other two settings
  gets the per-release batch narrowed to fit (`slots ÷ releases`,
  `soulseek_auto._batch_width`), so the app never asks slskd for more than it
  will serve. The Queue tab reads all three numbers back in its header.
- **R77 — the Soulseek port check states what it proves.** `GET
  /api/soulseek/port-check` returns five rows — `listen` (a real TCP connect
  plus a bind test), `mapping` (what the router itself lists, with its own words
  and the lease), `address` (the LAN address the mapping points at vs the WAN
  address the gateway states, so CGNAT is named as CGNAT), `self-connect`
  (refused ⇒ `unknown`, never `fail`: a router without hairpinning refuses it
  while the port may still be open) and `network` (slskd's signed-in state) —
  each carrying `proves` and `cannot`. A definitive "open to the internet"
  answer needs a probe from OUTSIDE the network, which this app does not ship,
  and the payload says so. Nothing runs on its own: the probe is fired by the
  page's *Test port* action.

### 7.11 MusicBrainz browsing and the watch dialog

- **R83 — the release-type picker is chips, and it holds every type
  MusicBrainz can state.** The watch dialog
  (`web/src/components/WatchDialog.tsx`, feeding `server/artist_watch.py`)
  draws the five primary types — `album`, `ep`, `single`, `broadcast`, `other`
  — and the eleven secondary ones — `compilation`, `soundtrack`, `spokenword`,
  `interview`, `audiobook`, `live`, `remix`, `dj-mix`, `mixtape/street`, `demo`,
  `field recording` — each a toggle carrying its own `aria-pressed` state and
  MusicBrainz's own capitalization. Nothing is paged through: there is no "load
  more" and no hidden tail. What the dialog ticks is what the watch stores, as a
  CLOSED vocabulary compared case-insensitively — a name MusicBrainz does not
  publish is dropped rather than kept as a filter that could never match
  anything. The artist pages themselves keep loading their release lists as the
  reader scrolls, and the fallback button stays for the two cases where the
  observer cannot run (no `IntersectionObserver`, or a stalled fetch).

### 7.12 What enters the library: the edition, the source, and the name in your language

- **R84 — one deterministic policy decides which edition is fetched.** The nine
  tiers of `mlo/release_choice.py`, in the order they are scored
  (`_TIER_NAMES`): release **status** (official → promotion → bootleg — an
  unofficial edition is chosen only when nothing official exists), the
  configured **medium** order (`auto_import_medium_order`: CD, then the other
  physical media, digital last), the **box-set** rule (an edition carrying
  DVD/Blu-ray media, or one disc after another, sorts below the album's own
  CD/digital media, so a 3-CD anniversary box no longer outranks the plain CD it
  contains), the **disc-versus-re-encode** rule (R85), the **track count** (an
  edition short of the release group's own count is penalised), the **release
  date** — the group's own `first-release-date` is the reference, the penalty
  for distance is strictly increasing in the gap and NEVER flat (two reissues a
  decade apart are never a tie, which is what a linear term that reached zero
  at a nine-year gap let happen: a live "The Dark Side of the Moon" browse came
  back as a 2016 reissue over the 1988 CD, and the album folder was named 2016)
  and an edition that states its date in FULL (`YYYY-MM-DD`) beats one stating
  only its month or year, because the folder is named after that date — the
  **clean/edited-edition** rule (`prefer_original_edition`: the
  original beats a later reissue unless the later one is materially more
  complete), the **plain-release** rule (a plain release beats a disambiguated
  one) and **prefer_release_country**, which only ever breaks a tie. The order is
  fixed and total: equal scores are broken by MusicBrainz's own listing order,
  never by chance, and `_deciding_reason` names the tier that decided
  (`"the disc-versus-re-encode rule"`). The SAME module serves the release-group
  page's ranking, `group_targets`, `resolve_release`, `auto_import_targets`,
  `pick_releases`, the artist watch and `GET /api/mb/release-choice`, so a page
  and the downloader cannot disagree about which edition "this album" means.
- **R85 — a compressed derivative of a disc sorts below the disc's own streams**
  (`prefer_disc_streams`, shipped **ON**; Settings → Import & tags). A `BDRip`,
  a `DVDRip` or an `x264` re-encode is somebody's lossy derivative of a source
  that usually still exists — a remux, a full disc — so it ranks below it. The
  markers, read from the same two fields the clean-edition rule reads (the
  release title and MusicBrainz's disambiguation comment), are `bdrip`, `brrip`,
  `dvdrip`/`dvd-rip`, `webrip`, `web-dl`, `hdtv`, `hdtvrip`, `x264`, `x265`,
  `xvid`, `divx`, `microhd`, `halfcd`/`half-cd`, `re-encode`/`re-encoded` and
  `compressed`. Deliberately NOT markers: `remux`, `bdmv`, `dvd`, `blu-ray`
  (those name the disc ITSELF — what wins) and codec names such as `h264` or
  `hevc`, which a remux carries just as well. With the setting **off** the tier
  scores every candidate the same, so the other eight decide exactly as they did
  before the rule existed. It is not a grade key: it decides which file the
  grade is computed on, and the same switch decides the disc-folder case of
  §7.13.
- **R86 — a music-video release published as Digital Media is fetched from
  YouTube, inside the same auto-import job.** `server.soulseek_auto` routes by
  the release itself (`acquisition_route`, reading `video_tracks` — the
  recordings' own MusicBrainz `video` flag, carried on each track of the payload
  `server/integrations.release_lookup` returns): video recordings on Digital
  Media take the YouTube branch, video recordings on a DISC (what
  `mlo.release_choice.is_video_format` classifies: DVD, Blu-ray, VHS, Video CD,
  LaserDisc…) and EVERY audio release keep the Soulseek path byte for byte, and
  an unstated or unknown medium is never guessed at. The route is read before
  the "slskd is not running" precondition, so a YouTube release needs no
  Soulseek at all, and the branch starts after the album-folder claim, re-check
  and queue row the Soulseek path already had. One `youtube.best_candidate`
  search per track (the length filter and the lyric/cover/tribute rejection),
  downloaded into `<downloads>/YouTube/<Artist - Album>`, renamed to
  `<disc>-<NN> <title>.<ext>` — the shape `_parse_trackno` reads disc and
  position back out of, and the naming script writes itself; nothing is named
  after an upload title. Then the SAME `_import` the Soulseek path calls: MB
  stamping, `MEDIA=Digital Media`, `SOURCE=YouTube` (the closed vocabulary of
  `mlo.tagtext.SOURCE_VALUES`, so the Digital Media SOURCE rule of §3 is
  satisfied rather than dodged), the naming script and the configured
  post-import chain in the background. A collection that is a dozen separate
  uploads imports from what came back — every missing track is named in the log
  and counted in the job's result — and a run that finds NOTHING ends on the
  same wish offer an empty search does (`source="youtube"`), never as a silent
  success. YouTube disabled (`youtube_enabled`) or yt-dlp missing fails the job
  with that sentence, not with an empty album.
- **R87 — the alias in your locale is what the app shows beside a MusicBrainz
  name.** `locale` (default `en`; the old `beets_locale` is migrated into it, so
  one setting now serves both) drives `server.integrations.alias_for`, whose
  ladder is exact — an alias whose `locale` equals the setting, `primary` first
  — then a COUSIN locale (MusicBrainz separates a script with a hyphen, `ja-Latn`,
  and a region with an underscore, `en_PH`; a reader who asked for `ja` wants the
  romanization and one who asked for `en` wants `en_PH`, but only when the exact
  locale has nothing), then the entity's `primary` alias whatever its locale, and
  last any alias whose name actually differs from the entity's own (the
  Japanese/Chinese/Korean case). An alias flagged as a search hint is never
  shown, an alias identical to the name is not repeated in parentheses,
  and an alias must be at least as READABLE as the name it annotates:
  the ladder's answer is dropped when the name is already written in the
  reader's script and the alias is not (`_reads_natively` — an English
  reader is never shown `Radiohead (レディオヘッド)` because MusicBrainz
  marks that alias primary, while a Japanese reader is), and the mirror
  holds, so a Japanese name is not romanized for a `ja` reader. An alias
  in the reader's own script always passes. The alias rides along in the MusicBrainz request that was already
  being made — no second call, ever — and the browser, entity and release-group
  pages and the credits panel show it (`宇多田ヒカル (Hikaru Utada)`), while the
  SAME setting is what translates non-Latin names for the Soulseek searches and
  the beets import.
- **R95 — "Add to library" records the request, and a request is not the
  album.** Four things follow, and the library, the queue and the wish store
  have to agree about all of them (`server/api_add.py`, `server/pending_albums.py`,
  `server/api_queue.py`, `server/wishes.py`, `server/interrupt_recovery.py`):

  * **The button answers before MusicBrainz does.** A request that already
    carries the title and the artist has given everything a framework album and
    a wish need, so the folder, the marker and the wish are written from the
    request (`pending_albums.create_from_request`), the reply says
    `"background": true` + `"resolving": true`, and the release lookup
    (`integrations.auto_import_targets` + the rest of the add) runs on a daemon
    thread (`api_add._prepare_add`). A caller that gave only an id (a bare MBID
    or URL) keeps the synchronous resolution — there is nothing to name a folder
    with until MusicBrainz answers — and its reply says `"resolving": true` for
    the same reason: the server, not the caller, named the release. The one
    difference the user sees is the queue row: while the identity is being
    resolved it carries `pending_albums.STAGE_RESOLVING`
    (`searching_musicbrainz`), which the queue view draws as *Searching
    MusicBrainz…* — a wish in that state is waiting for the SERVER, and calling
    it `queued` would read as a download nothing has searched for. The flag is
    cleared when the lookup lands, and a marker whose lookup never landed is
    believed only for `pending_albums.RESOLVING_MAX_AGE`, after which the row
    falls back to the wish's own state.
  * **A framework album is never the album.** A folder holding a
    `.mlo_pending.json` marker and NO audio is a REQUEST on disk: its marker
    carries the release's MBIDs, so `wishes.owned_mbids` and
    `wishes.reconcile_with_library` refuse it (`pending_albums.is_placeholder`)
    — counting it as "already in your library" is what left a wish terminal,
    nothing searching it and an empty album standing in the library for ever —
    and `POST /api/wishes/{id}/import` refuses to aim an import at it (409, the
    same sentence as any un-downloaded wish). Only a folder with audio satisfies
    "already in your library".
  * **A fresh add is a fresh request.** Re-adding a release whose wish has ENDED
    (`not_found`, a spent `failed`, or `imported` with no audio anywhere) re-arms
    it (`wishes.rearm`: counters and backoff cleared, due now) before the reply
    claims the search has started; a wish whose album really IS here answers
    "It is already in your library." instead, with no framework album created
    for it. An add that names its release instead of identifying it (no MBID,
    `title` + `artist`, from a streaming recommendation) searches MusicBrainz
    once: a hit continues as an ordinary add (`matched: true`), and no hit
    records a NAME-keyed wish (`name:<artist> — <album>`, `matched: false`,
    `by_name: true`) the queue's own name search fills — never an id, and never
    a framework album nothing could tie an import back to.
  * **One release is one album folder, and an import ends the placeholder.** An
    import that lands somewhere else (its own name disagreed with the naming
    script, or it was aimed at a folder by hand) is tied back to the placeholder
    by release identity — `pending_albums.adopt_root` asks the release's own
    wish after its own folder scan, and `pending_albums.clear_if_filled` takes
    the placeholder down when the album really arrived elsewhere
    (`_drop_other_placeholder`, matched by `imports._album_mbids`, never by
    name) — and the startup sweep (`server.interrupt_recovery`) re-arms a
    framework album whose wish has STOPPED while its release is nowhere in the
    library, and removes the placeholder whose album IS there.

### 7.13 Disc rips: a DVD or Blu-ray structure is one title, not a pile of parts

- **R88 — a disc structure is recognized, its feature is never guessed, and
  what it produces is a bit-exact remux.** A folder holding `VIDEO_TS/` (or a
  loose `VTS_nn_m.VOB` title set) is a DVD rip; a folder holding `BDMV/` (or a
  `BDMV/STREAM/` directory) is a Blu-ray one; both are read by
  `mlo/videodisc.py`, which knows the grammar and nothing else — the parts of a
  title set are `VTS_nn_1.VOB`, `VTS_nn_2.VOB`, … and the part index starts at
  1, because `VTS_nn_0.VOB` is the set's MENU and is never the feature. A
  Blu-ray's titles are NOT its file names: the `.mpls` playlist says which clips
  form which title and in what order, each play item carrying the clip's own in
  and out time, and a playlist that cannot be parsed is refused rather than
  guessed at — including when only SOME of a disc's playlists parse, because a
  title the disc states and this code cannot read may be the feature. An `.iso`
  is recognized only to say so: nothing in this app reads inside a disc image
  (a Blu-ray one is usually AACS-encrypted), so it is asked about, never
  opened. A single `.vob`/`.mpg`/`.m2ts` with no structure around it is an
  ordinary video file and keeps the ordinary single-file path.

- **The main feature is the longest title, and an unclear one is a QUESTION,
  not a coin toss.** Durations come from the structure itself where it states
  them (a Blu-ray's play items) and from one `ffprobe` per part for a DVD, so a
  disc is picked without decoding a frame. The app refuses, and asks, when: the
  structure is an `.iso`; a playlist or a part cannot be read; a usable title has
  no measurable duration; the runner-up is within `max(30 s, 5%)` of the longest
  (both durations are named in the question); or a Blu-ray playlist replays a
  clip twice or plays only PART of one (the concat demuxer could not reproduce
  that title, so the app asks instead of shipping something else). A refusal
  touches nothing on disk. The question is stored as one row per album — the
  same shape the wizard's family questions use, so the notification bell,
  `GET /api/import/prompts` and the queue's "Needs you" row show it — and
  `prompts` RE-DERIVES it from the structure itself, so it stands while the app
  would still refuse and withdraws itself once the user has resolved the disc.

- **The remux is the existing one, fed one input.** The chosen streams go into a
  generated `ffconcat` list (system temp dir, absolute paths) and reach ffmpeg
  as a single input, through the same `mlo.remux.remux_video` every other video
  uses: video copied bit-exact, lossless audio to FLAC, lossy audio copied,
  captions always mapped, chapters off, then the same ffprobe verification
  (video present, audio/subtitle counts equal, duration within 0.5%) and the
  same H.264 fallback gate (`video_reencode_incompatible`). Nothing is
  re-encoded on its own, so script 11 never CREATES a compressed derivative of a
  disc stream. The output is ONE MKV beside the structure, named after the
  folder that holds it (`<Album>/<Album>.mkv` for `<Album>/VIDEO_TS/`), and the
  consumed streams are removed only after that remux verified
  (`video_remove_original`); other title sets' parts and the IFOs are left where
  they are, and a re-run is idempotent (an existing MKV of the same duration is
  the disc's, and its leftover streams are swept).

- **The disc's own streams win over a derivative shipped beside them**
  (`prefer_disc_streams`, ON, R85): the "700 MB rip" a release ships next to its
  `VIDEO_TS` folder is left where it is, never remuxed as if it were the
  feature, and never claimed to be a disc (that rule is unconditional — a lone
  re-encode is an ordinary video file on the ordinary path). With the setting
  OFF, the disc's special casing goes with it: those files take the ordinary
  per-file path and the log says why. The layout agrees about all of this: a
  confirmed structure is not reported as a stray folder, while a folder merely
  NAMED `VIDEO_TS` still is.

### 7.14 What an import promises: one release, one album folder, one run

- **R89 — a release is imported ONCE, into ONE album folder, and its chain is
  scoped to that album.** The queue reports "two separate releases" when any of
  these leaks, so all three are rules:

  * **The destination is identity-checked.** An import moves the album to
    `<library root>/<Artist - Album>` (`_import_dest`). When that path is
    already taken the question is WHICH album holds it: the SAME release — its
    own `MUSICBRAINZ_ALBUMID`/`MUSICBRAINZ_RELEASEGROUPID` tags, or the ids a
    framework album's `.mlo_pending.json` marker was created with
    (`imports._album_mbids`, which reads the marker exactly because a framework
    album has no tags yet) — **raises** instead of importing ("already in your
    library"), because re-downloading a release the library already holds is
    what left a second copy of one album beside the first. A DIFFERENT album
    that happens to share the name keeps the `(2)` escape, and the log says so:
    a silent `(2)` is the bug, not the escape.
  * **A release cannot start twice.** `start_job` refuses a release already
    waiting in the bulk queue (`_queued_keys`, under the queue lock) and one
    already in the library (`wishes.owned_mbids`), and it RE-CHECKS the running
    set inside the same critical section that registers the job
    (`_running_keys_locked` under `_lock`): two requests arriving together — a
    double press, the wishes worker and a manual grab — used to both find the
    release "not running" and register two jobs, which the album-folder claim
    only made WAIT, after which the second searched, downloaded and imported the
    same album again.
  * **Two jobs heading for one folder serialize, and the chain stays in its
    album.** `job_locks` holds the folder `_import` names (before any `(2)`
    suffix) for the job's whole life — search, download, verify, import — so two
    editions of one album cannot move their files in at once; and the chain runs
    as `script_runners.run_chain(targets=[album])`, which sets `cfg["targets"]`
    for every script, so nothing in an import ever walks the library (`mlo/cli`'s
    own Run All is the explicit, user-started library-wide path and is not what
    an import runs).

- **R94 — an album-scoped action never waits on an unrelated album.** A script
  run claims the paths it is about to work on, in the same registry a delete, a
  move or a tag write claims against (`server.job_locks`), so two chains over
  DIFFERENT albums run at the same time — two imports of two releases, an
  import and a user-started run — while two over the SAME album refuse or queue
  exactly as one process-wide run lock used to make them. What a refusal says
  is the claim's own sentence ("`<album>` is in use by Import Album (job-4) —
  wait for it to finish, then retry"), so the user is told which album is busy
  and who has it, not that "a script run is already in progress". A queueing
  caller (an import, which must not skip its chain) waits for that album's
  claim instead of the whole process. A library-wide Run All is the one run
  that touches everything: it claims the library root (`<music folder>/Artists`,
  or the music folder itself while that does not exist yet), so it blocks every
  scoped run and is blocked by any — the honest reading of "this run rewrites
  whatever it finds". The UI's header bar still follows ONE run at a time (the
  one in flight longest, so the line never interleaves two albums' numbers);
  every run's own row in MAINTAIN → In progress shows its own progress
  regardless of who holds the bar.

- **R94a — a run that waits says so, and the user's own press never waits.**
  The reported bug was a press of `Run the import chain` that sat there: an
  album another job was already finishing (the import that put it there, a
  script run on it) made the press queue — silently, for as long as that run
  took, and then it ran the very same chain over the album again. Two rules
  come out of it, and they are the two halves of one sentence: a caller that
  QUEUES must say what it waits for, and a caller with someone at the keyboard
  must not queue at all.
  * **A queued run is visible.** `script_runners.run_chain` resolves its job and
    answers `job_locks.holder` BEFORE it claims anything, so a run that has to
    wait registers its own row, joins the header bar and publishes
    `waiting_text(path, holder)`'s sentence ("`waiting for Import Album —
    <album> is in use`") on both surfaces. Before this, a queued run had no row
    of its own (MAINTAIN listed only the job it waited behind, under that job's
    numbers) and the bar kept the other run's last frame, which is exactly what
    the owner read as "nothing happens for ages". The wait itself is unchanged:
    an import still waits for that album's claim rather than skipping its chain,
    and a timed-out or refused wait releases the row it made, so no ghost is
    left in MAINTAIN. What waits: the AUTONOMOUS paths — the bulk queue, the
    one-click downloads import, the Soulseek importer, a wish or a watch landing
    (`server.imports.finish_album`'s `wait`, default ON).
  * **The user's own press is answered at once.** `POST /api/import/finish` is
    the wizard's `Run the import chain` button, so it passes `wait=False`:
    `server.imports._refuse_if_held` asks the album **before** the import does
    any of its own work and answers 409 with the claim's own sentence
    ("`<album>` is in use by Import Album (job-4) — wait for it to finish, then
    retry") when another job is already finishing it — the same answer
    `/api/run` gives a double-pressed run. Asking first is also what keeps the
    press's own tag-writing steps off an album in use, and it is why the chain
    is never started twice over one album (queueing behind that job used to
    re-run the whole chain the moment it ended). A batch where only SOME albums
    are busy still runs the free ones, and reports each refused one in its own
    album entry; 409 is for a request that started nothing at all.
  * **The steps an import takes before its chain are named.** The links, the
    genres, the advisories, the instrumentals, the metadata and the cover art
    are network work that happens before the first script — measured at 5.4 s on
    a throwaway album, and longer on a real one — and none of it is a script, so
    nothing published anything: the press read as having done nothing until the
    chain's first frame arrived. `server.imports._phase` announces each of them
    through `job_locks.publish`, so the row and the bar show the phase
    (`0/0`, i.e. indeterminate, with the phase's own text and NO step pair: it
    is not a step of the chain and must never be drawn as one). The bar is left
    alone while a chain owns it — one line cannot honestly show two producers —
    and the row is the running job's, which is the import's own when it runs
    inside one.

### 7.15 Notifications and the player's own immediacy

- **R90 — every import announces itself, and the switches are yours.**
  `server/imports.finish_album` is the single call every import path makes (the
  wizard's finish, the downloads panel, the sequential import queue, the bulk
  queue, the Soulseek auto-importer and the wish/artist-watch pipeline behind
  it), and it emits two events through `server.events`: `import_started` on the
  way in and `import_done` where every path ends (`_report_gaps`), carrying the
  chain's own one-line summary (`chain_summary`). Both are switchable
  (`notify_import_start` / `notify_import_done`, Settings → Notifications, ON by
  default), like the download phase's `notify_soulseek_download_start` and
  `notify_download_done` and the add-time `album_pending`; a kind with no key in
  `events._notify_configured` is unconditional by design (an outcome the user
  must be able to see). Notifications go to the persisted tray for every kind,
  and an OS notification for the kinds in `notifications.ts`'s `OS_KINDS`.
- **R91 — selecting a track silences the outgoing one at once.** The player's
  load effect (`web/src/components/PlayerBar.tsx`) pauses the ACTIVE element the
  moment the index changes, before the new source is fetched and decoded — a
  switch used to leave the old track playing until the new one was ready, which
  reads as "nothing happened". Skipped when a gapless swap already started the
  next track on the other element, where pausing would cut the song that just
  began.

## 8. Recommended runbook

Nothing here is a substitute for the app's own Dependencies page: run it first
and install what the platform supports.

1. **Before touching anything** — set the music folder, then script **20 (Scan
   library layout)** and script **4 (Grade)**. The grade is read-only and tells
   you what is missing; script 20 walks the canonical
   `<music>/Artists/<Artist>/<Album>/…` shape and, with `layout_apply` (ON),
   fixes the three findings that have exactly one answer — a wrong-case name is
   renamed, audio outside any album folder is moved into the one its own tags
   name, an album-less artist folder goes to the Trash — while everything else
   (stray files, unexpected folders, empty albums) is reported and left alone.
   Set `layout_apply` off for a report-only pass. Script 20 writes that one
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
   transforms, 8 writes mood/energy/genre/advisory, 5 normalizes images, 19
   re-fits the artist image, 6 audits, 7 measures DR/ReplayGain, 9 writes
   `.accurip`, 12 writes key/BPM, 16
   is the standalone mood pass, 10 is the final canonical pass, 20 puts the
   library's shape right, 21 completes any half-written AcoustID pair and 4
   grades.
   An **import** runs the same list (R9): script 20 is scoped to the album just
   imported, so the layout it fixes and reports on is that album's, and the
   grade at the end of the chain reads the fixed folder.
4. **Re-run only what failed.** Every script is idempotent by default: it skips
   files that already carry the work, so a second *Run All* is safe and cheap.
   To redo a specific thing use its force flag (§2, R11) — that is the only way
   a script revisits work it has done.
   **A run has one scope, and it is the same for every script in it**: a
   library-wide run (the Optimize page's *Run All*, the CLI) makes every script
   discover the whole library for itself, and a targeted run (a selection, the
   wizard's *Run ticked scripts*) makes every script work only on those
   targets. A script must never be handed an empty target list and left to
   report "nothing to do" — a run that changed nothing must be able to say why
   in terms of the files it looked at, not in terms of a scope it never had.

Safe to re-run at any time: **4** (read-only) and **20** (idempotent — a library
already in the canonical shape has nothing left to fix), 2, 1, 5, 6, 7, 8,
9, 10, 12, 13, 15, 16, 17, 19 (it never upscales and leaves a conforming image
byte for byte alone), 21 (it acts only on a file holding half a pair).
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
- `grade_check_audit` (ON — `STRICT_DEFAULT_KEYS`) and `audit_cd_require_both` /
  `audit_verify_*` decide how much audit machinery runs; on a Docker or Linux
  server the AccurateRip, audit and logchecker paths run through the image's own
  runtimes (`mono-runtime` for CUETools, `php-cli` for the Logchecker phar), and
  `GET /api/capabilities` names what a given host cannot do.

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
| `genre_autofill` / `genre_sources` | ON / `[rateyourmusic, musicbrainz, listenbrainz, itunes, lastfm, theaudiodb, wikidata, bandcamp, discogs, deezer, spotify]` | which writers can satisfy the genre checks. The list is a PRIORITY list, asked in order and stopped as soon as a track's list is complete, and the shipped default is every source the app knows (R39a) |
| `mood_enabled` / `mood_source` | ON / `hybrid` | whether script 8/16 writes `MOOD`/`ENERGY` at all |
| `ai_effort` / `ai_genre_effort` | `high` / `high` | the reasoning budget every AI call sends, and the genre ranking's own: `minimal` (no thinking field), `low`, `medium`, `high`, `max` — `max` reaches a provider's ceiling by ladder (`max` → `high` → no field, R62a) |
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
| `audit_verify_log_checksum` | ON | whether a log's own EAC SHA256 is read at all: when on, a checksum the log CARRIES must verify or the disc fails, and one that is absent is reported (warning) but not required — 'unsupported' (XLD, EAC before 1.0) and 'missing' (a 1.0+ log with the line stripped) are indistinguishable to grading, only to the warning (see R30) |
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

Two keys deliberately do **not** change a verdict on their own:
`grade_check_accuraterip` (AUDIT-only, R5) and `show_sidecar_files` —
deliberately NOT in the table above: it only makes the viewer list a file's
sidecar siblings (`cue`/`log`/`lrc`/`.accurip`) and compute their grades, and it
adds no check. The ACQUISITION keys are not grade keys either — they choose
which file a verdict is later computed on, never the verdict itself:
`prefer_disc_streams` and the other release-choice keys (R84/R85),
`soulseek_auto_*`, `wishes_*`, `youtube_*` (R76), and `locale` (R87).

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
- Platform decides what is possible, and the app installs what it can rather
  than assuming a Windows host: CUETools ships Windows binaries and runs under
  the **mono** runtime (`LINUX_BINARIES.cuetools` + `LINUX_RUNNERS`, and the
  Docker image installs `mono-runtime`), AudioAuditor has native Linux builds,
  and the Logchecker phar runs on the image's `php-cli` — so AccurateRip
  generation, the log grade and the audit all work on a Linux server. What a
  given host cannot do is reported by `GET /api/capabilities` and shown on the
  Dependencies page as a row with the reason and, where the OS package manager
  owns the tool, the exact command that installs or upgrades it — never as a
  failed check. libjxl and libjpeg-turbo are installs of their own as well —
  upstream's static `.tar.lz` and its `.deb` are unpacked in app, so the tools
  left as a `system` row on Linux are the ones the distro really owns (flac,
  ffmpeg, and rsgain or fpcalc on an architecture upstream publishes no build
  for, plus libjxl on ARM, where no asset of any kind exists): each is graded
  normally when the distro provides it, and its row says what upstream has and
  what the package manager would install.
