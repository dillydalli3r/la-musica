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
| Advisory ratings | `mlo/advisory.py` (the ladder and the AI rubric) |
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
today**: it held 20 (Optimize library layout) while that runner walked the whole
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
runs) rather than the library-wide Run All order; unticking a box is the user's
own override for that one album. A script the configured chain leaves out keeps
its box, unticked, so nothing the step used to offer became unreachable. The
step has ONE run action (`Run ticked scripts`) and no re-run-the-chain button:
that press went back through `finish_album`, whose own steps re-fetch the
links, genres, cover art and advisory and EMPTY the arrived values of the four
families an import decides (`drop_arrived_values`) — it undid exactly the work
done by hand in the steps before it, on an album the owner had just finished.
Every script is still runnable per album from the album page, and Run All over
the library lives on the Optimization page.
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
| 20 | Optimize library layout | The music folder's shape against `<music>/Artists/<Artist>/<Album>/…`: audio at the root or in an artist folder, stray files, unexpected folders, empty albums, `wrong_case` rows. With `layout_apply` (ON) it SETTLES what the folder itself proves — a wrong-case name is renamed, audio outside an album folder is moved into the one its tags name, and what is excess goes to the Trash (a stray file, a folder inside an album that is neither a disc folder nor holds audio, an album folder with no audio, a foreign root folder holding no audio, an album-less artist folder, the `.mlo_*` leftovers) — and reports every other row with the reason it stayed, re-derived at the move (R185). Writes ONE report describing the whole library (plus a `fixes` list) to `<music>/.mlo/data/`, which the Library page warns from; scoped to `targets` when a run names them, and library-wide when it does not (R9) | one report file + the renamed/moved/removed paths | `layout_apply` (removals go to the Trash) | no |
| 21 | Fix AcoustID pairs | Completes an INCOMPLETE `ACOUSTID_ID`/`ACOUSTID_FINGERPRINT` pair — the failure `Missing ACOUSTID_FINGERPRINT (incomplete AcoustID pair)`, which had no fixer before. An id already on the file has its fingerprint recomputed locally; the reverse half needs a lookup and is counted, never invented | `ACOUSTID_ID`, `ACOUSTID_FINGERPRINT` | no | only when the id half must be looked up |

**R11 — force flags are the only way to redo work.** Each script has one, and it
is what makes the script look at a file it has already processed:
`force_lyrics` (1), `force_cue` (2), `force_reencode_flac` (3), `force_reencode_images`
(5), `force_audit` (6), `force_dr_replaygain` (7), `force_auto_tag` (8),
`force_accurip` (9), `force_audiometa` (12), `force_mood` (16), `force_xlit` (17),
`force_publish` (18), `force_tracklist` (15). Grade (4) needs none — it re-reads.
Optimize library layout (20) carries `layout_apply`, the ONE key that turns work OFF
instead of forcing a redo: the scan always reports, and the key is what lets it
rename, move and remove (see §2's row 20 and R185). A supplied force dict is authoritative AND
complete — every flag it does not name is cleared — so the *Re-run & overwrite*
menu sends a COMPLETE selection: a saved one is completed with the defaults
(`web/src/lib/force.ts::loadForceSel`), which is what keeps a switch added later
from being silently off for everyone who had ever opened the menu. A caller that
sends a partial dict gets the authoritative reading: the flags it names are set
and the rest are off. The menu on any selection sets exactly these keys.
The dict's KEYS are the short UI names above (`audit`, `flac`, `dr`) or a script
id (`"6"`, `[6]`); a config-key spelling is NOT a key — `_apply_force` does not
know `force_audit`, and because the pass is authoritative an unknown key is not
merely ignored but leaves that flag CLEARED, so a caller that spells a force as
its config key runs the script with the force it asked for turned off. The
*Re-run & overwrite* menu shipped exactly that bug for its four actions; the
short keys are the only accepted spelling, and
`tools/test_script_menus.py` refuses a caller that uses another one. A bare
`True`/`False` covers the whole CHAIN when it is applied with no script id (the
chain applies force once), which is what `run_chain(..., force=True)` means.
A caller that has no force selection of its own OMITS the dict rather than
sending `{}`: every import path — the wizard's chain re-run, the row menu, the
bulk queue, the Soulseek auto-importer — passes `force=None`, so the saved
switches apply, while `{}` would clear them all (`layout_apply` included, which
left an import's layout pass a read-only report on the album it had just
imported).
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
  *not verified*. The verdict is stamped with the file's size, mtime and
  **audio identity** in `<music>/.mlo/data/audit_evidence.json`, and a file the
  record no longer describes — neither its stamp nor its identity — is
  re-audited (R206).
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
foreign and never stripped. Every one of those fields is a LIST where the
release states several answers — two engineers, four performers, a code per
pressing — stored as repeated container fields (§7.6 R57a/R57c, `"; "`-joined
when a container can hold only one string, R57b) and COMPLETED rather than cut
short when the file already states some of them (R57d). A field with no home in
the container's tag system (packaging, per-catalogue-entry labels, annotations)
is not invented under an ad-hoc key: it stays out, and the writer says which
fields it could not place.

The two advisory tags answer to **different switches**, because different things
write them: `ITUNESADVISORY` to `advisory_auto_fetch` (the provider fetch — the
import step, the wizard and the *Fetch / refresh advisory rating* action) and
`ALBUMITUNESADVISORY` to script 8's *Auto Album Advisory* derivation
(`mlo/config.py::_TAG_WRITE_SWITCH`). The advisory fetch derives the album tag
too, with script 8's own rule, so a manual fetch never leaves it stale.

A fetch reports its provenance per track, and never invents one: per source the
STRONGEST answer wins (every ISRC the file or MusicBrainz states is asked, so a
later pressing's explicit answer is not lost to an earlier clean one), a source
that STATED a value is written as it stands — the AI is not asked to
second-guess it, and a stated 0 is final (R65) — a track NOBODY stated anything
about is decided by `mlo/advisory.py`'s ladder (an instrumental is 0, then the
configured AI, then `advisory_fallback`), and a track that already holds 0/1/2
is echoed back UNCHANGED with `sources = ["existing-tag"]` unless the caller
asks for a re-rate (`force`).
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

- **R219 — the equalizer applies to PLAYBACK, and it is the same profile
  store an export uses.** `playback_eq_profile` (one config key, "" = off — the
  curve is what the library sounds like, the same reason `replaygain_mode` is
  one key) names a built-in preset or an imported profile from
  `mlo.eq`/`<music>/.mlo/data/eq`, and the player installs it on its own
  WebAudio graph: `lib/eqNodes.buildEqChain` renders the profile's preamp as one
  GainNode followed by one BiquadFilterNode per active band, spliced between the
  ReplayGain gain and the analyser (`lib/analyser.applyEq`), so the meters and
  the ambience read the equalized signal and an element attached later (the
  gapless pair's other half, a video popout) inherits the same curve. The
  mapping is APO's own — PK → peaking, LS/HS/LSC/HSC → shelves, LP/HP/BP/NO →
  the matching pass/notch — so a band's frequency, gain and order are identical
  in the app and in an export; a SHELF's width is the one honest difference
  (APO's custom slope reaches ffmpeg as a Q, while a WebAudio shelf is
  fixed-slope), and it is stated in the page rather than hidden. The editor
  (`pages/EqualizerPage.tsx`, sidebar → MAINTAIN) lists the presets and the
  stored profiles, imports APO/Peace text or a dropped file, draws the response
  from the browser's own `getFrequencyResponse` (never a second implementation
  of the filter maths), gives every band a DRAGGABLE handle plus typeable Fc /
  Gain / Q boxes (clamped, committed on Enter or blur, the VolumePct pattern;
  Escape puts the stored value back and the blur it fires is not a commit)
  and a per-band on/off, and previews edits LIVE through the player's own chain
  — nothing is stored until Save, which writes the profile back as APO text
  through the one import endpoint (`POST /api/export/eq/import`), replacing a
  profile of the same name. A profile that cannot be built leaves plain
  playback alone even mid-install: `installEq`'s tear-down is undone when the
  build throws, because the element's audio reaches the speakers through that
  graph alone and a gain node left connected to nothing is silence. `tools/test_export_audio.py` keeps covering the
  parser, the store and the export chain the page shares.

- **R220 — AutoEq is a SEARCH, and an import is the profile store's own
  file.** `GET /api/eq/autoeq/search?q=` answers from the AutoEq project's own
  index (`results/INDEX.md`, ~6 300 measurements, cached beside the profiles for
  a month — a search box that downloads a megabyte per keystroke is unusable),
  ranking an exact model name, then a name the query starts, then where the
  first word lands, with EVERY word required. `POST /api/eq/autoeq/import`
  fetches ONE file by name — `<model> ParametricEQ.txt`, falling back to the
  `GraphicEQ` form for a model that has only that — so no GitHub API call (and
  no rate limit) is in the path, and the fetched text goes through the SAME
  parser and store as a pasted profile (`mlo.eq.autoeq_import` →
  `import_profile`), which is what makes an imported correction editable,
  exportable and applicable to playback like any other. An id that is not a
  plain results-relative path is refused with a 400 (`_autoeq_dir`), and a
  failed refresh still answers from the cache with the reason in `error` instead
  of emptying the list. The import hands the page the row the server stored
  and the page opens THAT row: a profile's id is its name's slug, so
  re-importing an existing name REPLACES that profile, and a look-up in the
  page's own catalogue would open the bands from before the import — and Save
  would then write those back over it.

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
- **R52c — the fullscreen player paints NO panel on the artwork, and its INK
  ANSWERS to the cover.** The lyrics pane, the metadata block (title / format
  line / album / artist), the transport and the top bar draw no background, no
  border, no backdrop blur and no halo of their own — a tinted rounded rectangle
  under the title and a gradient pane behind the lyrics read as grey boxes
  pasted over the cover, which is what they were reported as. Two mechanisms
  replaced them, and there is no third:
  * **the ink polarity** (`npInk`, `NowPlayingView.tsx`): the cover's own
    average colour (the server's `tagcache.cover_color`, the value every
    ambience layer is painted from) decides ONE of two tables — the light one
    (white / zinc-100 / zinc-300, `.np-shade`) for a cover at or below
    `NP_INK_FLIP` (0.42 relative luminance), the dark one (zinc-950 / 900 / 800,
    `.np-shade-light`) above it — and the choice covers every lyric surface,
    the metadata tiers, the karaoke syllables, the transport glyphs and the
    time readouts. A single ink cannot be AA on a field that spans rgb(96) to
    rgb(255) within one screen (a white cover's bloom core), which is why the
    polarity is decided at all; the glyph shadow flips with it, and that shadow
    is ALL halo — four low-alpha stops, the tightest 0.42 — never a tight
    near-opaque core: the 0.92/3px core of the first fix merged between
    glyphs into a slab of uniform dark pixels under the line on a BRIGHT
    field (a red cover reads bright to the eye while its average luminance
    sits under the flip, so the light table was picked), which is what was
    reported. Legibility is carried by the ink's own contrast and by the
    field's treatment above; the shadow only has to stop a glyph dissolving
    into a busy mid-tone;
  * **nothing behind a dark cover, a cover-tinted wash in the band above it, a
    cover-tinted lift above the flip**: at or below `NP_FIELD_AS_IS` (0.20
    relative luminance) the ambience is dark enough for the white table on
    every patch the text covers, so **no scrim is drawn at all** — the
    background is the artwork's own ambience. Above the flip the field is
    lifted by a full-bleed gradient built from the cover's own colour mixed
    toward white (never a grey), with no edge, rounding or blur — a scrim, not
    a panel. The band BETWEEN the two keeps the white table and DROPS the field
    instead: the same full-bleed mechanism, the cover's colour mixed toward
    near-black at 0.62. That band is the case the owner reported twice — a
    cover is a mixture (The Bends is a bright face on a dark frame), and the
    average the flip judges it by sits well below the field its ambience
    paints, because the orbs and the bloom are screen-blended and ADD light on
    top of it (0.24 average, 0.45 field, measured). White ink read 2.55–3.49:1
    there with four tiers under 3:1 — "the text mixes into the background" —
    and 6.98–15.02:1 on the same cover after the wash, across all 17 tiers the
    player draws (the top bar's queue line and `Up next` label, the title, the
    format line, the album and artist lines, the transport glyphs, the time
    readouts, the volume box and the lyric lines).
  * **the chrome follows the chosen table, never a fixed grey**: the top bar's
    icons and queue line, the transport glyphs, the seek readouts and
    `VolumePct` take their colour from the ink (`text-current` inside an
    `ink.chromeText` surface), so they move with the polarity. `VolumePct` draws
    on two surfaces — the fullscreen chrome and the player bar — and the fixed
    `zinc-500`/`zinc-600` it used to pin suited only the dark one (the volume
    box read 1.64:1 on a mid cover: invisible). Over a music video the top bar
    keeps light greys, because the picture is the field there and the ink's
    polarity says nothing about it.
  * **the frequency strip takes the same table**: `<Visualizer>`'s
    `ink` prop is the ink polarity (`ink.viz`), and it draws its bars in
    the table's own tones — near-black over a bright cover, near-white
    over a dark one — because a strip drawn with the app's accent
    (white) is a white strip on a white field: the same "text that blends
    into the background" the lyrics had. Left unset (the docked lyrics
    sidebar, whose surface IS the app's dark panel) the accent is right.
  The floating MENUS are the deliberate exception and keep their frosted veil
  (`np-veil` + `np-veil-dark` + `np-veil-panel`: the options popover, the queue
  drawer) — a menu is a menu, and its panel is how it reads as one. R56c's
  keyboard `:focus-visible` rings are untouched.
  The lift's strength has a FLOOR (0.46), and the floor is the point: what the
  dark table has to clear is the DIMMEST patch the text covers — the metadata
  block at the bottom, where the vignette bites — which is dark whatever the
  cover's average is. A curve starting at zero at the flip left exactly that
  band unreadable (measured: a `#b4b4b4` cover put the block's field at
  rgb(80), 2.5:1 for near-black ink); with the floor it reads 4.69:1.
  Measured, not eyeballed: `tools/check_np_metadata_contrast.cjs` samples the
  field the text actually sits on for FOUR covers (dark, mid-grey, a
  bright-grey one just above the flip, and white), asserts every metadata tier
  ≥ 4.5:1 and the title ≥ 3:1 on each — the boundary cover is the tightest at
  4.69:1 — asserts the ink FLIPPED with each cover, and asserts the block
  paints nothing of its own.
  `tools/check_fullscreen_player.cjs` asserts the structural half: no layer but
  the ambience and the chosen scrim paints over the art, and the metadata
  block's own computed style has no background, blur, shadow or border.
- **R52d — the lyric offset is a control on both lyric surfaces, and it is
  saved into the track's lyrics.** `−`, the pending shift in seconds, `+`, and a
  Save that appears once there is something to save (`LyricOffset.tsx`, shared
  by the fullscreen pane's options menu and the right-docked sidebar's header —
  one control, like `LyricZoom`, so the two surfaces cannot drift). The step is
  a tenth of a second and the pending range is ±10 s. While it is being dialled
  in the shift is LOCAL: the surfaces parse their lyrics with it
  (`parseLrc(text, shiftMs)` / `parsePlayerLrc(text, shiftMs)`), so the
  highlight follows the buttons with no round trip and no half-written file;
  Save posts it as a DELTA (`POST /api/lyrics/offset`, `{path, delta_ms}`) and
  renders the text the server stored. The write follows where the lyrics already
  live — the `.lrc` beside the track and/or its `LYRICS` tag, never a migration
  between the two (that is `lyrics_format`'s job, script 1) — gated by the same
  per-filetype LYRICS switch the format pass uses. What moves is the SYNC:
  every `[mm:ss.xx]` and every Enhanced `<mm:ss.xx>` stamp in the text, at the
  precision the file itself carries; a line with NO timestamp is returned
  untouched (plain text has no sync to move, and inventing one would turn an
  unsynced file into a wrong one), and a stamp that would land before the
  file's start clamps at zero instead of going negative. A pending shift belongs
  to the track it was dialled against and is dropped when the track changes
  (never carried onto the next one, and never written from a stale surface).
- **R162 — a lyrics search that finds NOTHING settles the track as
  INSTRUMENTAL.** An import fetches lyrics by itself (script 13 is in the
  configured chain, R89/R160), and the search is allowed to come back empty:
  when the whole configured provider chain has nothing for a track — no hit
  above the automatic confidence floor, or a hit with no text
  (`mlo/lyrics_fetch.fetch_one`) — the track is marked `INSTRUMENTAL=1` under
  the app's own source (`server.instrumental.lyrics_absent`, evidence key
  `lyrics-none`) instead of being left as a LYRICS grading failure that parks
  the album for a person. It is the app's documented rule and not an invented
  tag: the tag records what the app actually did (it asked every configured
  provider and none had the track), the note it writes says so
  ("no provider in the configured lyrics chain had this track and no source
  states vocals"), and the write goes through the same gate and the same
  `answers`/`evidence` shape the instrumental step's own writers use
  (`server.imports.fetch_instrumentals`, `/api/instrumental/fetch`), so the
  tag menu shows this value's provenance like any other.
  Three statements outrank the absence of lyrics, and each leaves the file
  exactly as it is: an `INSTRUMENTAL` tag already on the file (a provider's
  answer, the pipeline's own step, or the user's edit — the tag is the record);
  lyrics on the file (embedded text or a real `.lrc` sidecar: lyrics ARE
  vocals, so a track whose words merely were not re-fetched is not
  instrumental); and a cross-referenced source saying not-instrumental for this
  track (`server.instrumental.detect_instrumental` — LRCLIB's
  `instrumental: false`, Spotify's `instrumentalness`, a title that says
  otherwise), whose answer is then what is written when it says instrumental.
  Lyrics that ARE found change nothing: the fetch writes them, and the track is
  never touched by this rule. Gated by `instrumental_auto_fetch` (the app's own
  switch over deciding INSTRUMENTAL unattended) and by the per-filetype write
  gate — a switch the user turned off is never overruled to close a gap. The
  run counts what it settled (`Fetch lyrics` logs "marked instrumental (no
  lyrics found by any provider): N track(s)", and the run's stats carry
  `instrumental_count`) so "skipped: 12" cannot read as "twelve tracks nobody
  looked at".

- **R52e — the fullscreen player opens IN THE WINDOW, and the browser's own
  fullscreen is a separate, opt-in button.** Clicking the album art in the
  now-playing bar (and the bar's own fullscreen glyph, and the app-wide `F`)
  mounts the viewer, which is `fixed inset-0` and covers the app by itself — it
  does NOT call `requestFullscreen`. Taking the whole screen is a distinct
  control, rendered in the viewer's top bar (`Maximize2`/`Minimize2`, beside the
  visualizer and lyrics toggles) and remembered in its own state; the
  `fullscreenchange` listener still treats a browser-driven exit (Esc being
  swallowed by the browser is the common case) as "the user is done", but a
  transition this pane asked for — `fsOwn` — leaves the viewer up. What this
  fixes: entering the player seized the whole screen, which is not what "open
  the player" means, and an embedded host can refuse the request anyway.

- **R52f — the meters follow the SOUND, not the last attach.** Every media
  element carries its own WebAudio graph (`lib/analyser`), and the gapless
  `<audio>` pair means two of them exist with only one playing: the analyser the
  visualizer and the ambience read is therefore chosen from a registry of
  attached elements by "which one is actually playing" (falling back to the last
  attach), because reading the idle half returns an all-zero spectrum and both
  meters fell back to their synthetic animation while real audio played. The
  same read resumes a context the browser suspended or WebKit "interrupted"
  (tab backgrounded, a phone call) before it returns — a suspended context reads
  as zeros too — and a WebAudio failure is retried after a cooldown instead of
  latching the meters off for the session. An element that has left the document
  is skipped, so a video popout unmounted mid-track cannot answer for the app.

- **R52g — the lyric pane reads as a live surface: a nudge parks the follow for
  a little over a second, its edges dissolve, and its own controls ride on the
  words.** Five behaviours, one shared pane (`web/src/lib/lyrScroll.ts`,
  `LyricsSidebar.tsx`, `NowPlayingView.tsx` — the sidebar, the fullscreen
  player and the editor preview take their scrolling from the same module, so
  they cannot drift apart):
  * **The hold is `HOLD_MS` = 1 200 ms, not 6 000.** A wheel or a touch calls
    `takeOver()`, which stops the glider and parks the follow until
    `Date.now() + HOLD_MS`; a timer `HOLD_MS + 50` later re-kicks the pane while
    the element is playing, so the sung line is picked back up in place rather
    than waiting for the next line change — the hold suppresses the line-change
    step too. The 6 s this started as made the pane look BROKEN: a reader who
    nudged the wheel and then waited watched the song's line change three times
    while the pane sat still. A wheel's momentum is a few hundred milliseconds
    and a finger drag re-arms the hold on every event, so a second and a bit
    never fights a gesture in progress and the pane is alive the moment the
    reader stops.
  * **The pane's top and bottom edges dissolve (`.lyr-fade`, `index.css`).**
    Both scrollers carry it: a mask (`mask-image` and `-webkit-mask-image`)
    that is transparent at 0, opaque at 26 px, opaque at `calc(100% - 26px)`
    and transparent at 100 % — a line the pane's own box cuts through its
    middle is the one place a reading surface looks broken rather than alive.
    A MASK and not an overlay: nothing is painted, so R52c still holds and the
    reading surface has no panel, tint or gradient of its own. The first and
    last lines are never affected, because `LYRICS_PAD_TOP` / `LYRICS_PAD_BOTTOM`
    hold them a third of the pane away from either edge.
  * **The two lyric controls sit ON the words, quietly.** The fullscreen pane's
    footer row carries `LyricZoom` and `LyricOffset` themselves, not only the
    options popover's copies — nudging the sync or fitting the size to the room
    used to mean leaving the words to go and find them. The row is rendered
    only while the pane is OPEN (`paneOpen &&`: collapsed, there is nothing on
    screen to size or to shift), in the ink's own tone (`ink.chromeText`) at
    `opacity-60` (`40` while the lyrics are stale, `100` on hover /
    `focus-within`), with no background and no border of its own (R52c). Both
    controls take `text-current` plus an opacity instead of a hardcoded grey —
    `text-zinc-500` / `text-white` are gone from them — because ONE control is
    rendered on three surfaces: the sidebar's header, the options popover, and
    the artwork itself, where a fixed zinc glyph is R52c's grey-on-grey failure.
  * **A lyric line is not a tooltip.** `title="Click to seek"` is gone from
    both surfaces; the line still seeks on click (`renderLine`'s `onClick` →
    `p.onSeek(l.time)` and `centerLine(i)`), so the only thing lost is a hover
    bubble drawn over the words.
  * **The star row takes the ink too.** `StarRating`'s two colours are
    parameters now (`emptyClass`, `fillClass`; the app's own defaults are
    unchanged, `text-zinc-600` and `fill-current text-accent`), and the
    fullscreen row passes `text-current opacity-45` / `fill-current`: this row
    sits straight on the artwork, where a zinc-600 outline and an accent fill
    both blend into a bright cover. The outline keeps a little air, the filled
    halves take the ink at full strength — the polarity of R52c applied to a
    control.

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
- **R163 — the autonomous fetch asks the release group and ranks by that
  reference, and it derives the group id when only the release is tagged.** What
  R56b promises is only true if the query actually carries the group:
  `imports.cover_candidates` reads the album's own identity (`imports._album_mbids`
  — the `MUSICBRAINZ_RELEASEGROUPID`/`MUSICBRAINZ_ALBUMID` tags, or the ids a
  framework album's marker was created with) and passes BOTH ids to
  `integrations.cover_search`, which asks the Cover Art Archive by
  `release-group/<rg>` (labelled `release_cover: false` — the reference) and by
  `release/<id>` (labelled `true` — one edition's sleeve). An album that states
  a release but no group used to skip the group read outright ("no
  release-group id to ask about — only the release"), so the pick could only be
  an edition's sleeve or a name-searched row: `cover_candidates` now fills that
  gap with `integrations.release_lookup(album_id)`'s own `release_group_id`
  (cached MusicBrainz read; a failed lookup leaves the identity untouched and
  the run continues), and the ids are also what `staged_metadata` records, so a
  staged candidate set and the CAA read agree. Verified live for **OK Computer**
  (release group `b1392450-e666-3926-a536-22c65f834433`, no release id given):
  22 candidates ranked, the group's own front cover won —
  `coverartarchive.org/release/30702389-…/30730533321.jpg`, 1400×1400 JPEG,
  `release_cover: false`, score 0.9997 — above the Tidal/Apple 1400–4000 px rows
  (0.6624/0.6619) and with the karaoke/tribute rows (*Vitamin String Quartet*,
  *Mother Falcon*, *Molotov Cocktail Piano*) rejected by name; the sources
  report is part of the payload, so a search that could not ask the group says
  so.
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
- **R57a — a multi-value tag is stored as REPEATED container fields and joined
  on read with ONE separator, `"; "`.** `AudioFile.set_tag` writes a list as one
  Vorbis comment, one ID3 text frame / people pair or one MP4 atom PER VALUE
  (`mlo/audio.py`), never as one string holding several answers; `get_tag` and
  `all_tags` join those repeats back with `mlo/tagtext.py::_LIST_SEP` and
  `tag_values` hands the pieces back, so every container reads one list the same
  way. Only that exact separator means a list (`_LIST_SEP`'s own comment: a `;`
  inside a URL is a character, not a second value): `mlo/tagtext.py::join_list` /
  `split_list` are the one pair a writer joins or takes a list apart with, and no
  other module spells the separator itself. R57's per-part canonicalisation
  applies to each piece — in the container and in the joined copy alike.
- **R57b — a container that holds one string per key stores the list as that
  joined value, never as a Python repr.** A video file (MKV/webm/mov/vob/avi/…,
  the ffmpeg path) has a flat metadata block, so `mlo.audio.set_video_tags` —
  the ONE video writer, which `/api/mb/assign`, `/api/tags/bulk`,
  `/api/videos/tag`, the video pass of Mood & Energy and the DR pass all end in
  — writes a list as its `"; "`-joined value; `get_tag` returns that string and
  `tag_values` its parts. An MP4/M4V is NOT this case: mutagen writes repeated
  atoms there, like FLAC and MP3. Passing the list through `str()` used to put
  the repr in the file, so a two-engineer credit landed as `['a', 'b']` in an
  MKV while the same write to a FLAC produced two comments.
- **R57c — a credit/role tag is a LIST, and several people in one role are DATA.**
  `PERFORMER`, `PRODUCER`, `ENGINEER`, `MIXER`, `ARRANGER`, `DJMIXER`,
  `CONDUCTOR`, `REMIXER`, `DIRECTOR` and the work's `COMPOSER` / `LYRICIST` /
  `WRITER` / `MUSICBRAINZ_COMPOSERID` each hold EVERY credit the release states,
  one value per person (a performer's instrument stays in the value, in the
  `Name (instrument)` spelling), as do `ISRC` and `RELEASECOUNTRY`. Nothing may
  reduce such a tag to one value — not a container (R57b), not a writer
  (R57d), not a naming variable (R57e): a second engineer is a fact about the
  record, and dropping it is data loss, not tolerance.
- **R57d — a writer COMPLETES a short list rather than replacing it.**
  `mlo/autotag.py::write_mb_tags` is the one MusicBrainz writer on both paths
  (the Auto Tagging stage and the beets import plugin). For a tag whose answer is
  a LIST, a file that already states values KEEPS them, first and in its own
  order, and gains every value of the answer it does not already state
  (`_complete_list`, compared case-insensitively); a tag that is already complete
  writes nothing, which is what keeps a re-run — and the prescan's "nothing to
  fill" — the no-op they are. For a SCALAR answer the file's own value still
  wins (another pressing's label, ids another tagger wrote). The two rules that
  already existed are unchanged: the two dates are SHARPENED to MusicBrainz's
  fuller spelling (`mlo/naming.py::fuller_date`) and `RELEASECOUNTRY` is WIDENED
  to the release's whole set only when the file's codes are a strict subset of it
  (`mlo/autotag.py::_country_upgrade` — a country the release does not state is
  never added to the list).
- **R57e — `GENRE` is a list of names, and `%genre%` is that whole list on both
  sides of an import.** The tag holds one repeated field per name (R57a),
  broad-first per R38/R41, canonicalised and capped by the one genre policy. The
  naming variable is the `"; "`-joined list on the organizer's side
  (`mlo/naming.py::track_variables`, which passes `GENRE` through while
  `RELEASECOUNTRY` / `LABEL` are the two that reduce to their first value — R33)
  and on the import's side too: beets' own item field carries only mediafile's
  FIRST genre (`genre` is mediafile's `genres.single_field()`), so the beets
  plugin reads the file's own `GENRE` tag with the organizer's own reader
  (`server/beets/mloplugin.py::_item_genre`) and falls back to the item only for a
  file that states none. An import and the organizer therefore compute the same
  `%genre%`, and the same path from it.
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
in `mlo/advisory.py::decide_advisory`, which asks its sources in a fixed order
and records which stage answered (`source`).

- **R61 — the providers are a source, and the merge rule is `1 > 0 > 2`.**
  `server/integrations.py::resolve_advisory_route` asks Deezer/Spotify (ISRC),
  Apple, Discogs and YouTube, and `merge_advisory` settles what they said: a
  stated 1 beats everything, then a stated 0, then a clean edition's 2.
- **R61a — every ISRC is asked, by every ISRC source — the instrumental lookup
  included.** `server/integrations.py::_isrc_codes` is the ONE reader of "the
  ISRCs this track has" (a file's `"; "`-joined `ISRC` tag or a caller's own
  list, trimmed and deduplicated) and both ISRC consumers ask every code:
  the advisory ladder (Deezer, then Spotify when configured, for each code the
  file states plus each ISRC MusicBrainz holds for the recording) and the
  instrumental detection (`server/instrumental.py`, Spotify's audio-features,
  for each code the file states, in the tag's own order). A source asked more
  than once contributes its STRONGEST answer — `_strongest_advisory` for the
  ladder, "instrumental anywhere wins" for `server/instrumental.py`'s own merge
  rule — so a clean first pressing can never hide a later one. Taking the first
  code alone is exactly the miss this rule names: a recording is published in
  several territories under several codes (R57c).
- **R62 — the AI judges the SONG, not its vocabulary.** With
  `advisory_ai_classify` (ON) and a provider configured, the model is asked ONE
  question about a track no source stated anything about (R63's step 3) and fed
  the track's own lyrics, read off the file (embedded first, else the `.lrc`
  sidecar — the read `mlo/lyrics_publish.py::local_lyrics` does). Its rubric is
  the song's subject and tone, not a keyword count: `1` is excessive profanity,
  a slur or a very strong word, or graphic sex/violence/drug use; a mild word in
  passing — a lone `ass`, `damn` or `hell`, an idiom, a quoted word, a word
  ordinary in another language — is `0`. The lyrics may be in ANY language or
  script, and the model must judge them in that language rather than answering 3
  because they are not English. When it answers, it IS the value's source and is
  recorded in the reply's per-source map beside the providers (which said
  nothing); `3` (or an unparseable reply) falls through the ladder. Provenance
  ids: `ai-lyrics` (the words were read) and `ai` (they were not). It is never
  asked what a source already answered: its answer is not a second opinion, so
  a provider's value is left alone however the model would have voted.
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
- **R63 — the ladder, and the AI is asked ONLY when nothing else answered.**
  `mlo/advisory.py::decide_advisory` runs one order and stops at its first
  answer: (1) a source that STATED a value wins — it is written as it stands,
  with that source's provenance, and nothing else is asked about it; (2) an
  instrumental is `0` (R64); (3) the AI (R62), asked only now, when every source
  came up with nothing at all; (4) `advisory_fallback` (R64). The lyrics WORD
  SCAN is gone, out of the import and off every surface: the module that carried
  it (a multilingual lexicon, a mild tier inside it, the `hits` readout and the
  switch that gated the whole stage) was deleted, and a saved config that still
  carries that switch drops it when it is normalized. A word list is not the ear
  the rubric wants — R62's model already reads the same lyrics, in context and
  in any language — so nothing about the ladder asks for one any more.
- **R64 — an instrumental is settled before the AI, and the fallback is the
  user's.** `INSTRUMENTAL=1` with `auto_zero_advisory_for_instrumental` (ON) is
  `0` when no source stated a value, and costs no AI call — there are no words
  to read. When every step above was silent, `advisory_fallback` decides: `0`
  (shipped), `2`, or `none` to write nothing at all. An invented fallback value
  never overwrites a rating a file already carries — only evidence lowers a
  rating.
- **R65 — a stated value is FINAL.** Whatever a source stated is written as it
  stands and is never re-opened; a stated `0` above all. The sources do miss
  explicit content in their own direction (Deezer's `explicit_lyrics: false`
  also covers "not classified", Apple's `notExplicit` is the master's own flag),
  and the ladder used to escalate a stated 0 to `1` when a stage that read the
  words disagreed, reporting an `(escalated)` source instead of the provider.
  It does not any more: an answer that contradicts a source is exactly the
  second opinion the owner had removed, so the file's own words do not overturn
  a stated 0 either. The AI never overrules a source for the same reason — it is
  not asked about one.

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
  audit, the grading and the log's own checksum read it. WHICH families do
  travel is the user's file selection (R187); untouched, it is the tracks alone.
  Two switches keep the old behaviour available and both are OFF by default —
  `export_sidecars` (mirror `cover.*`/`description.txt`/artist image/`.lrc`/
  `.cue`/`.log` — R187's `LEGACY_SIDECAR_FAMILIES`) and
  `export_playlists` (the per-album `.m3u8` plus `all.m3u8`) — and the run
  result reports what did not travel in `excluded` (one row per file: album,
  name, `kind` — the file FAMILY of R187, one of `FILE_FAMILIES`' own keys — and
  the reason), with `excluded_counts`, `excluded_total` and the sentence
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

- **R187 — WHAT an export copies is a file selection, family by family.**
  `copy_files` (per run) / `export_copy_files` (saved default) is a list of the
  keys of `server/exporter.py`'s `FILE_FAMILIES` — `audio` (the tracks
  themselves), `cover` (the album's `cover.*` and the artist image), `lyrics`
  (`.lrc`), `cue` (`.cue`), `log` (`.log`/`.accurip`), `description`
  (`description.txt`, numbered copies included — `mlo.paths.album_sidecar_of`),
  `checksum` (`.md5`/`.sfv`/`.ffp`/`.torrent`), `text` (`.txt`/`.nfo`/`.url`/
  `.pdf`), `playlist` (`.m3u`/`.m3u8`/`.pls`/`.wpl`) and `other` (a non-audio
  file this app classifies as none of those) — and it is the ONE thing that
  decides what a run writes. One classifier (`_extra_kind`, over the extension
  table `_EXTRA_REASONS`, plus the cover names and the artist image it knows by
  NAME) and one predicate (`export_tracks._travels`) serve the menu, the copy
  pass (`_copy_siblings`) and the `excluded` report alike, so a file a run
  copies is never reported as left behind and a file it leaves is never copied:
  there is no per-surface or per-caller filter, and the families a run reports
  in `excluded_counts` are the very keys of the selection.
  `GET /api/export/files` serves the menu the Export page's checkboxes render
  (keys, labels and the one-line hint under each), so a tick a run would ignore
  cannot be drawn, exactly as the structure menu is served; the Export page's
  checkboxes and the per-page dialog's are that group of `Opt` rows, kept in
  table order so a saved config and a re-opened form read the way the menu does.
  The shipped default is the tracks ALONE (`EXPORT_DEFAULTS["copy_files"]`,
  `["audio"]`) — what an export has always written — and a run whose selection
  writes no tracks is coherent (the families it names land where the tracks
  would have gone) EXCEPT under `prune`: sync mode makes the destination match
  the tracks a run writes, so a selection that writes none is REFUSED rather
  than obeyed, because obeying it would delete the destination's audio.
  An explicit EMPTY selection is refused with a sentence naming the families
  (`copy_files_error`: a run that copies nothing would create the destination
  and write an empty tree while reporting success), answered as a 400 by
  `POST /api/export` before anything is written and refused at SAVE time by
  `server/exportconfigs` like every other enumerated value; an unknown family
  key is refused the same way. `/api/export/defaults` serves the RESOLVED
  selection (`exporter.copy_files`), so the form opens showing the files the
  next export would actually write. The per-track families follow the per-track
  rule the audit already used — `lyrics` copies the exported tracks' OWN `.lrc`
  files and never the `.lrc` of a track outside the selection — and a stray
  SUBFOLDER is reported and never walked or copied, exactly as `extra_files`
  promises. `sidecars` is the switch this replaced and is still honoured (per
  run, and as a saved default through `mlo/config.py`'s `export_sidecars`),
  resolving to `LEGACY_SIDECAR_FAMILIES` (audio + cover + lyrics + cue + log +
  description) through the same code path: a caller that sends nothing new — or
  only that boolean — gets the behaviour it had, with one honest widening,
  since the files the audit already classifies as those families now travel
  with them (an `Artist.jpg` sitting INSIDE an album folder, and a numbered
  `description (2).txt`).

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

- **R177 — an entity page's genre chips are drawn in the app's own
  capitalization, and the cascade keeps MusicBrainz's.** MusicBrainz publishes
  genre names lowercase (`shoegaze`, `art pop`) — a database convention, and
  the reason `server.integrations._genre_names` keeps the spelling it reads:
  that list is what the genre CASCADE hands the writers, and
  `mlo.genres.normalize_genres` capitalizes once, at the one place a tag is
  written. A page draws a CAPTION instead, so the four entity payloads that
  carry a chip row — the release header, `artist_identity` (whose `tags` ride
  the same row, so they are capitalized with the genres rather than leaving
  "Art Pop · britpop"), the release-group page and the recording page — pass
  their names through `_display_genres` → `mlo.genres.display_name`, the same
  function the tag writers, the grader's `grade_check_tag_case` and
  `server.discover`'s list already use. Nothing else moves: a release's
  per-TRACK genre rows and the cascade's `per_track`/`per_source` lists keep
  MusicBrainz's spelling (`tools/test_genres.py` pins the source order and the
  mixed spelling of that merge), identity is untouched — every comparison
  folds case — and the string a reader copies off a chip is the string a tag
  holds.

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

- **R150 — an acquisition walks its release group's ranked editions, best first,
  and the walk is FINITE.** "Add to library" on a release group resolves ONE
  edition through the release-choice policy and queues it (R84); the group's
  OTHER eligible editions are ranked behind it by that same policy, and that
  ordered list rides on the request the album was created from
  (`integrations.group_targets`' `candidates`, recorded on the wish by
  `server.pending_albums.create` → `wishes.set_candidates`). The search asks
  them in order — the best first, then the next — until one lands, and it does
  so INSIDE the one wish (`server.wishes_worker._run_one`): one release group is
  still ONE album folder (R142) and ONE queue row, never a row per edition. The
  list costs no request: its entries are the editions the release-group browse
  already returned, and no candidate costs a fresh search to decide what to try
  next. `mode: "best"` is what starts a walk (one target row carrying the ranked
  list); `mode: "all"` keeps its meaning — every eligible edition queued as its
  own album, each with itself as its only candidate, because the user asked for
  all of them.

  What "best" MEANS is `mlo/release_choice.py` and nothing else. The nine tiers,
  in the order they are scored (`_TIER_NAMES`, `_evaluate`):
  `status` (official → an unstated status → withdrawn/expired/cancelled →
  promotion → bootleg), `medium` (`auto_import_medium_order`, CD first, a format
  the order does not name last), `set` (an edition carrying DVD/Blu-ray media,
  or one disc after another, sorts below the album's own media), `compressed`
  (R85), `tracks` (short of the release group's own count is penalised),
  `date` (closest to the group's `first-release-date`; the penalty is strictly
  increasing in the gap and NEVER flat, and a fully-dated edition beats a
  year-only one from the same year), `edition` (a clean/edited edition sorts
  below the original while `prefer_original_edition` is on), `disambiguation`
  (a plain release beats a disambiguated one) and `country`
  (`prefer_release_country` — a TIE-BREAKER and nothing else, which is why it is
  last). The score is that tier tuple encoded positionally in base 8
  (`_score`), so a bigger score IS a better pick and no lower tier can ever
  outvote a higher one; equal scores are broken by the order MusicBrainz listed
  the editions in — never by chance — and `_deciding_reason` names the tier that
  decided. `rank_releases` returns EVERY edition in that order, and
  `group_targets` truncates the ROWS to the first while `mode` is "best" — the
  ranking itself is never truncated, which is exactly what the walk walks.

  There is ONE such policy. `integrations.ranked_releases`, `pick_releases`,
  `pick_release`, `resolve_release`, `group_targets`, `auto_import_targets`, the
  artist watch, the album page's ranking and `GET /api/mb/release-choice` all
  reach this module, so a page and the search that fills it cannot disagree about
  which edition "this album" is. The only other ranking in the acquisition path
  ranks a different thing: `soulseek_auto._rank` over `find_candidates` ranks the
  PEER FOLDERS of one already-chosen edition (which peer has the complete,
  lossless, log-verified copy), so a walk of five editions contains up to five
  of those — a downloads ranking inside a candidate, never a second opinion about
  which edition the album is.

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

- **R164 — a script that moves an album reports the folder the album is in
  WHEN THE SCRIPT RETURNS, and the chain follows it.** A script that takes an
  album's audio out of the folder the chain is pointed at knows where the album
  went, and `server.script_runners._follow_moved_targets` re-points every later
  script at that folder — one identity, one chain (R142). The report is
  `stats["moved_targets"]`, so it has to be taken AFTER everything the runner
  itself does to the album: script 14 moves the album into the library with
  beets and then re-applies MLO's naming script (`beets_organize_after`), and
  the two spellings are not the same path — measured on a real Creep EP import,
  beets wrote `…Creep {GB - 7243 8 80234 2 9} [Parlophone] [<release id>]` and
  the organize step renamed it to `…Creep {GB - CD - 7243 8 80234 2 9}
  [Parlophone] [<release id>] [<group id>]`, so `server.beetscfg` maps the
  pre-organize dirs through organize's own `album_root` report
  (`_organized_roots`) and hands the chain where the album actually ended up.
  A `moved_targets` frozen before that step named a folder that no longer
  existed, `_claimed_targets` dropped the claim as stale (it takes only folders
  that hold audio NOW — a stale path must not send the tail nowhere twice), the
  identity walk behind it (`_find_moved_album`, the fallback for a mover that
  reports nothing) could not recognise files the mover had renamed, and every
  script after the mover ran against the emptied staging folder: Format all
  reported "No files found to format.", Grade "No albums found.", both with
  zero stats and no error, while the import reported success. When neither
  answer exists, the target stays put and the chain says so out loud
  (`WARNING: no audio left in …`) rather than printing a cheerful "nothing to
  do" — but with the shipped chain it does not happen. Pinned by
  `tools/test_import_pipeline.py`.

- **R165 — the album's own files travel with the album.** Whatever takes the
  audio out of an album folder — script 14's beets import, an organize run,
  script 20's apply filing a loose track into its album — leaves the album's
  non-audio files behind unless something carries them, and the ones that matter
  are the files the pipeline itself just wrote: the cover the autonomous step
  fetched BEFORE the chain ran, the description beside it (`run_metadata_step`)
  and the expected-tracklist manifest (script 15). Measured on the real import:
  the album landed in its canonical folder holding only the FLACs while
  `cover.jpg`/`description.txt`/`.mlo_expected.json` stayed in the staging
  folder, the grade reported COVER on an album the import had just fetched
  artwork for, and the import was parked for a person (R160's one failure mode).
  One rule, one implementation: `mlo.layout.carry_album_files` moves every
  non-audio entry of the folder the album left into the folder it is in NOW, at
  the album root — and `mlo.layout.carry_track_files` does the same for a
  track's own companions (`01 - Song.jpg`/`01 - Song.lrc`, the organizer's stem
  rule) when the apply files it. A name the destination already holds is NEVER
  overwritten (that file stays where it is and the run says so), the move is
  `mlo.paths.move_path` — a rename, never a copy of the bytes — and the emptied
  folder is pruned, so no audio-less shell is left for the scan to report as a
  broken album. It is applied by the chain when it follows a move
  (`_follow_moved_targets`), by script 14's own artifact gather
  (`server.beetscfg._gather_orphaned_artifacts`, which decides only WHICH fresh
  folder is the album's — identity, since beets renames every file it imports),
  and by script 20's apply; `server.main.organize` already carries them itself
  (sidecars follow their track, leftovers follow the album to its new root), and
  that is the same rule for a user-started organize. What cannot be attributed
  is left alone and reported, never guessed at: a subfolder that still holds
  audio the mover did not take (beets refuses a file it cannot read), and a file
  whose name the album folder already holds. Pinned by
  `tools/test_import_pipeline.py` and `tools/test_layout_case.py`.

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
  that touches everything: it claims every folder it WALKS — the library root
  (`<music folder>/Artists`, or the music folder itself while that does not
  exist yet) plus every album the sweep finds filed elsewhere in the music
  folder, listed from the same `mlo.stats._find_albums` walk the run's own
  scripts make (R168) — so it blocks every scoped run and is blocked by any —
  the honest reading of "this run rewrites whatever it finds". The UI's header bar still follows ONE run at a time (the
  one in flight longest, so the line never interleaves two albums' numbers);
  every run's own row in MAINTAIN → In progress shows its own progress
  regardless of who holds the bar.

- **R94a — a run that waits says so, and the user's own press never waits.**
  The reported bug was a press of the import chain that sat there: an
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
    the press a person makes (the album page's tag-actions entry for a
    selection — the wizard's own chain button is gone, R9), so it passes
    `wait=False`, and
    every import — the press and the autonomous paths alike — claims the album
    it is finishing for the WHOLE call (`server.script_runners.claim_paths`,
    taken by `server.imports.finish_album` before its first tag write, R168):
    the press is refused at once with the claim's own sentence
    ("`<album>` is in use by Import Album (job-4) — wait for it to finish, then
    retry") when another job is already finishing that album — the same answer
    `/api/run` gives a double-pressed run. Claiming up front is also what keeps
    the press's own tag-writing steps off an album in use, and it is why the
    chain is never started twice over one album (queueing behind that job used
    to re-run the whole chain the moment it ended). A batch where only SOME
    albums are busy still runs the free ones, and reports each refused one in
    its own album entry; 409 is for a request that started nothing at all.
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

- **R168 — the claim follows the album, covers the WHOLE import, and a
  background chain inherits the job's.** One registry (`server.job_locks`) is
  what every run that rewrites an album holds against (`server.script_runners.
  claim_paths`), and four seams were letting an album be worked on while
  something else was already on it. Each is fixed at its seam, not per caller:
  * **An import claims the album for the whole import.** `finish_album` held the
    album only from its chain onwards, so the steps before the first script —
    dropping the arrived values, the links, the genres, the metadata, the cover
    art, all of it network work — ran unclaimed: two presses on one album wrote
    those files at the same time, and an auto-import's background chain was
    unlocked for its whole look-up phase. `server.imports.finish_album` now
    claims the album for the length of the call (the same `wait` rule and the
    same refusal sentence as the chain) and announces the import only once that
    claim is really its own; `_refuse_if_held` is gone, because the claim IS the
    refusal (R94a).
  * **A chain that MOVES the album keeps holding the album.**
    `job_locks.move(job, old, new)` re-points a claim when a script renames the
    folder (script 14's beets import, then the naming script): the album's NEW
    folder is claimed FIRST (waited for, never stolen — the album is
    mid-rewrite) and only then is the emptied one handed back, and the blocks
    that made the claim keep their accounting — a block's release of the old
    path lands on the new one, and the alias dies with the new folder's last
    reference, so a later, unrelated claim on the path the album left is never
    mistranslated. Before this the tail of the chain rewrote an album whose
    folder was unclaimed under its new name (a folder that has just appeared is
    exactly what a second import, a re-download or a fresh run claims) while the
    empty shell it left stayed "in use" until the run ended.
  * **An auto-import's background chain carries the job's claim — and the
    release's download folder with it.** `_start_import_chain` used a plain
    daemon thread, so the job's claim on the release's album folder
    (`_AlbumClaim`, taken before the search) was released by `_finish` the
    moment the download settled, while the chain still had every look-up and
    every script to run: a press on that album found no holder and started a
    second chain over the same files. The chain thread now takes a reference of
    the SAME job, so the album is never unclaimed across the hand-off and
    MAINTAIN keeps ONE row for the job; the folder the download came FROM is
    claimed for the chain's length as well, so a second import of it (the
    downloads page's one-click import, the bulk queue) queues instead of
    importing — or clearing — it underneath. `_account_metadata` (the artist
    image and the descriptions, written into the album) runs inside that claim
    too.
  * **A library-wide run claims what it walks.** The sweeps start at
    `config["music_folder"]` (`mlo.grader.run_grade_library`, `mlo.loudness`,
    `mlo.autotag`, the lyric fetch) while the claim was the library ROOT
    (`<music folder>/Artists`), so an album filed anywhere else in the music
    folder — by hand, by another tool, by an import that failed halfway — was
    rewritten by a Run All while nothing held it, and an album-scoped run could
    hold the very album the sweep was grading. `script_runners.held_paths`
    claims the root PLUS every album the walk finds outside it, listed from the
    same `mlo.stats._find_albums` walk the run's own scripts make, so the claim
    and the run cannot disagree about which albums are in the library; the
    `.mlo` state dirs are pruned from that walk, so a download or a trash entry
    is not "in use" merely because a sweep is going (R110a keeps pruning). A
    sweep with albums parked for a person claims the albums it was narrowed to
    (R163).
  * Pinned by `tools/test_job_locks.py`, one case per bullet — each one failing
    on the code before this rule: the claim following a moved album (and the
    old folder free), a sweep versus an album-scoped run over an album outside
    the library root, an auto-import's chain versus the user's press (refused,
    naming the holder), and the release's download folder claimed while its
    chain runs.

- **R160 — `automatic` finishes an import WITHOUT a person.** The shipped mode
  (`import_autonomy: "automatic"`, `mlo.import_policy`) means the pipeline does
  every step it can and then reports what is left; it never stops to ask. What
  it decides on its own, each through the family's own writer (the same entry
  point the manual option in `mlo.import_policy.FAMILIES` calls, so the two can
  never drift apart): the MusicBrainz/RateYourMusic **links**
  (`imports._stamp_release`/`stamp_rym_links`), the **cover** (`cover_candidates`
  → `mlo.cover_choice`, R163), the **genres** (`_stamp_release`), the
  **lyrics** — and, when the chain finds none, the **instrumental** mark that
  settles them (R162) — the **advisory** (`fetch_advisories`), the **artist
  image and the two descriptions** (`run_metadata_step`) and the
  **INSTRUMENTAL** tag itself (`fetch_instrumentals`). The chain then runs the
  configured scripts (R89), and `_report_gaps` reports what none of that could
  supply.
  A stop is legitimate only where the answer is the OWNER's, and there are
  exactly three: `import_autonomy: "review"`, a family named in
  `import_review_families` (or held by its own switch, `cover_review`), and a
  family no source could state at all — the case `raise_prompt` announces
  (R121). Everything else that stops an import for a person is a bug under this
  rule, and the fix is the family's own automatic step, not a new question:
  what the app is missing is a decision it can make with the evidence it has
  (R162 is the first of those filled in). The manual half is not optional
  either — a family the app decides is a family a person can still re-decide by
  hand, on the surface the family's rows name (`wizard`, `tag-actions`, the
  entity pages), and that path runs the very same writer.

- **R161 — an album that is WAITING for a person is not SWEPT.** While an
  import waits on an answer, its entry stands in `server.import_autonomy` and
  the album is PARKED: a person is mid-decision in the wizard, so a chain that
  rewrites it in the meantime is how a half-answered album gets half-written.
  What "waiting" means is `_awaits_answer`'s own line and nothing else — a
  review stop (`reason == "stopped"`) and a video prompt are waits; a family no
  source could supply is a WARNING on an album that already finished (R166) and
  a sweep touches it like any other album.
  `import_autonomy.chain_scope` is the one rule, and it is asked by the one
  seam every chain passes (`server.script_runners.run_chain`): a run that
  DISCOVERS its own albums — the library-wide sweep (Run All, a chain with no
  targets) — is narrowed to the albums that are not parked, and the run logs
  which ones it left alone ("`N album(s) are waiting on you — left untouched by
  this run: …`"); a run that names its albums is not filtered, whoever started
  it, because that is either the person's own press (`/api/import/finish`, the
  wizard's ticked scripts — the way a park gets resolved) or an import finishing
  the album it names, whose own steps rewrite it either way. What an import
  never does is touch an album it was not asked about: its chain is scoped to
  its own folder (R89), and a same-release re-download is refused before any of
  it (R89's identity check). Nothing else in the app runs a chain over a
  library album on its own — there is no scheduled sweep and no worker that
  re-imports a folder already in the library — so this is the whole surface,
  and the sweep is the one that used to reach a parked album. A prompt is
  withdrawn by the thing that fills the gap (R121), never by a chain that ran
  over the top of it.

- **R166 — an import that finished is FINISHED, and a gap is a warning, not a
  hold.** When the pipeline could not supply a family, the album does not wait
  for anyone: it is in the library, its release's row is a **finished** row,
  and what is left is announced (the `import_needs_data` notification, whose
  headline for this case reads "*album* — needs extra data"), shown on that
  finished row with the wizard link and the dismiss, and shown again on the
  album page's own banner (`GET /api/album`'s `needs`, `AlbumPage.tsx`) — all
  three from ONE payload (`server.import_autonomy.warning`), so a gap cannot be
  described one way in the list and another on the page it links to. It is NOT
  a row in the section for work holding on the user, and `server.api_queue
  .build_queue` attaches the warning to the row the album already has rather
  than emitting a second one: one album is one row, and a released album was
  read as *both* "Completed" and "Needs you" — the exact shape that made a
  finished import look like a stall. The only entries that still belong in that
  section are the ones that really are waits: a **review** import
  (`reason == "stopped"` — the person is mid-decision and the chain has not
  run) and a **video prompt** (a disc structure whose main feature is unpicked,
  so the remux it belongs to has not happened). `import_autonomy
  ._awaits_answer` is that line, and **R161** reads it: a library-wide run
  skips the waits, not the warnings. Skipping a warning was the same mistake
  from the other side — an album imported short of a cover was left out of
  Run All with no way to tell it apart from one still being written, and the
  log line that said so ("*N album(s) are waiting on you*") was the only hint.
  The wizard says the same thing in its own words: its list of open entries
  reads "Imports still needing data" unless one of them really is a stop, and
  the per-album banner ("Still missing: …") says the album is in the library
  rather than claiming the import could not finish.

- **R167 — what a track's lyrics need is decided from EVIDENCE, not from the
  letters alone, and the question is asked once.** `mlo.lyrics_xlit
  .detect_language` is the one rule, and its sources are ordered by how much
  each is about the lyrics themselves: (1) a **declared** language — the
  track's own `LANGUAGE` tag, which an import writes from MusicBrainz's release
  **text representation** (`server/integrations.release_lookup`'s
  `language`/`script`, stamped by `_stamp_mb_tags`; the app writes it only into
  an empty slot, so the user's own value stands) — when the text's script
  agrees with it (a wrong tag never romanizes or skips the wrong thing: the
  lyrics are what is being transformed); (2) the text's own **script**, for the
  scripts that belong to one language (kana → ja, hangul → ko, …); (3) the
  function words of the Latin languages (`_latin_lang`). `xlit_needs` then
  decides from that answer: transliteration for non-Latin text in a script the
  reader does not use (unchanged), translation unless the lyrics ARE the
  reader's language — which is where the evidence changes the outcome, because
  the function-word vote can only ever say "one of the seven the app knows": a
  Turkish track with no English stopwords in it used to pass as English and get
  no translation, and it does now. `""` is an answer too: script 17 then asks
  the model ONE question about that track (`server.ai.detect_language`, a
  strict parse — a bare code or nothing) and **stores** what it answers in the
  `LANGUAGE` tag, so a re-run asks nothing and grading reads a stored fact:
  the grader passes the tag into the same rule and never calls a model at all.
  `normalize_lang`/`_MB_LANG` is the one place MusicBrainz's ISO 639-3 answers
  (`jpn`) meet the app's 639-1 codes (`ja`), and codes that state nothing —
  `mul` (a compilation), `und`, `zxx` — normalize to `""` so they are asked
  past rather than obeyed.

- **R140 — an add writes the record and answers; its provider work happens
  AFTER the reply.** `POST /api/library/add` answers from what the request
  itself holds — the framework album (the folder the naming script names, its
  manifest, the release-group placeholder cover), its wish, and the kick of the
  one queue — and everything the reply does not need runs off-request: the
  release resolution on the daemon thread the deferred path already had
  (`server/api_add._prepare_add`, `_prepare_artist`), and the album's PAGE
  content on `pending_albums.prefetch_content(folder, cfg, background=True)`.
  The page content is the half that used to be paid inside the request:
  measured on a bare-id add (`{"mbid": <release id>, "kind": "release"}`,
  scratch scope, real network) the reply took **13.4 s** wall clock, of which
  10.0–13.1 s was `prefetch_content`'s provider work in the request path —
  `cover_search` 3.4–8.5 s, the RateYourMusic link lookup 2.7 s, the
  MusicBrainz metadata step 3.0–5.3 s — for content only an OPENED album page
  reads, while the album row, its manifest, its wish and its cover were already
  on disk. The same add answers in **1.3 s** with that content fetched behind
  the reply, and the remainder is the two MusicBrainz lookups that DO name the
  folder (0.6 s) — the case that legitimately pays a resolution inside the
  request, because `albums[].album_path` is not knowable without it; the
  reply's own `background`/`resolving` flags say which case it was, and a
  caller that already holds a title and an artist pays neither (0.2 s,
  deferred). The reply vocabulary — `ok`, `queued`, `albums`, `skipped`,
  `errors`, `note`, `matched`, `by_name`, `wish_id`, `resolving`, `background`
  — keeps its meanings, and a `note` must be true at the instant it is shown:
  it must not claim a search that has not started (`_deferred_note`'s own
  standard).

- **R141 — the acquisition search is a QUEUED wish, never work inside the
  request.** There is one queue (`server.wishes_worker` →
  `server.soulseek_auto`) and one search path; an add records a wish and ends
  with `wishes_worker.trigger()`, which starts the worker's OWN pass on a
  daemon thread (`wid=None`: the pass reads the wish store itself and searches
  each wish that is due by its own policy). Nothing in the add awaits a search,
  and no add has a second lane for one: what the pass searches, and what it
  leaves to its own retry/backoff, is the STORE's decision. This is what makes
  the reply's note ("Soulseek is searching for them now.") true when it is
  written — the kick has already happened when the note is composed, and a
  brand-new wish is due immediately (`wishes.due_at`), while a wish already
  waiting out a backoff keeps that wait. A wish the pass cannot search yet (no
  slskd, no login) keeps its place and its reason: the queue row, not the
  reply, is where that shows up.

- **R142 — one release is one album folder, from the press to the grade.** The
  framework album an add creates IS the import's destination:
  `server.soulseek_auto._import` asks `pending_albums.framework_for_release`
  first and moves the finished download's ENTRIES into that folder
  (`_adopt_into` — `os.replace` cannot merge two directories) instead of moving
  the album to `<library root>/<Artist - Album>` and leaving organize to
  redirect it afterwards. The old detour was not only a wasted move: the
  intermediate folder is an ALBUM to the library walker — one level too shallow
  to sit under its artist — so the grid drew it with the library root's own
  folder name as its artist ("Artists") and the folder name as its title,
  BESIDE the album it was about to become, and the download's own cover ended
  up renamed to `cover (2).jpg` next to the placeholder's `cover.jpg`, which
  the import then removed as its own placeholder — leaving the album with NO
  cover at all. Which framework album is this release's is identity, not name:
  the wish that created it (`wishes.find_for_release`, the job's own
  `wish_id` first) names it, its marker must agree about the release, it must
  hold no audio, and it must be inside THIS scope's library root. The
  placeholder still appears at the instant of the press (that is the feature)
  and it still ends the moment the album really lands
  (`pending_albums.clear_if_filled`), so one release shows one tile for the
  whole acquisition.

- **R151 — each candidate's search is BOUNDED, and the walk stops at the end of
  its own list.** A walk asks at most `soulseek_fallback_candidates` editions
  (shipped **5**, clamped to 1–10; **1** is the pre-walk behaviour — the best
  edition and nothing behind it), and each candidate's search is given
  `soulseek_search_timeout_seconds` of quiet (shipped **60**) before it counts as
  not found and the walk moves on — **per candidate**, so a walk of five may
  wait up to three of those, while a usable folder still ends a candidate's
  search in seconds. That window is the app's EXISTING one, not a second timer:
  it is the quiet time `soulseek_auto_search_wait` means, plus the response grace
  tail `_search_queries` adds (`wait_s + _SEARCH_GRACE_S`), now passed per job
  (`start_job(search_seconds=…)`). It bounds the SEARCH only — a candidate that
  finds a usable folder downloads, verifies and imports on the pipeline's own
  ceilings, because nothing here may cut a transfer short. A release group with
  fewer eligible editions than the cap simply ends the walk at the end of its own
  list: no error, no empty slot, and nothing waiting for a candidate that does
  not exist. A candidate that answers with nothing usable ends ITS search and the
  walk moves on; a candidate that fails for a TRANSIENT reason (a refused slskd,
  a MusicBrainz outage) stops the walk and goes through
  the store's retry/backoff policy unchanged — one policy, asked per candidate,
  and the walk invents no schedule of its own. The walk only ever moves FORWARD
  inside an attempt, and the next attempt starts at the BEST candidate again
  (`wishes.restart_walk`), so a release whose third edition was empty last time
  is not asked for a third edition first next time.

- **R152 — a walk says where it is, and one that landed says WHICH edition
  arrived.** The wish row carries `walk` — `{index, total, label, mbid, title,
  tried}` from `wishes.candidate_state`, the ONE block the queue row
  (`server/api_queue._wish_rows`), the album's pending payload
  (`library._wish_state_of`, which reads the same block) and the announcement all
  read, so no surface re-derives a position and none of them can disagree. Its
  `note` says `release 2 of 3: <title>` while a candidate is being asked,
  `next: release 1 of 3: <title>` between attempts (the next pass starts at the
  best edition, which is what that row is really about to ask) and, in the
  background phase, `tried 3 of 3 · no usable copy yet · searched again
  automatically around 14:20`. A search that lands an edition other than its best
  says so in its own notification — "the best edition was not available, so this
  is release 2 of 3" — because an album that arrived from a different pressing
  must never read as the one the user asked for. All of it is data the store
  already holds: a row still costs no MusicBrainz request of its own.

- **R153 — a spent walk is not a give-up: the release goes to the BACKGROUND.**
  When every edition the walk may ask has answered with nothing, the wish does
  NOT end and its framework album is NOT taken down (`wishes.mark_background`,
  status `background`, `server/wishes_worker._settle_attempt`): the editions are
  all still editions, so the release keeps its place in the pipeline, is
  re-walked on the worker's own ticks (`wishes_interval_hours`: a background wish
  is not terminal, so the pass picks it up exactly as it picks up any other open
  wish) and ends only when one of its candidates lands or the user cancels it.
  What still ends `not_found` — terminal, announced once, framework album removed
  — is a wish that carries NO ranked list at all: a name-keyed wishlist row
  (R95), which has nothing left to ask. The background phase is its own
  subsection of the Soulseek page — "Background", a `SECTIONS` name of the ONE
  queue (`server/api_queue.py`) rendered between what is running and what needs a
  person, with its own count and its own scope for "clear" — and a release in it
  is ONE row whatever the size of its walk: the candidates are asked one at a
  time inside the one wish, so there is never a row (or a second job) per
  candidate to merge away, and the row is cancellable (the standing request goes)
  and retryable (its "Search now" re-arms the wish and re-walks it immediately).
  `wishes_not_found_attempts` (shipped 3, 0 = never) keeps its meaning and its
  units — empty searches — now counted per WALK: the number of empty walks the
  release may have before it settles into the background.

- **R214 — a candidate whose copies were REFUSED is spent, not fatal, and the
  walk moves on.** "Every candidate was rejected (…)" — a batch every peer of
  which the pipeline refused on grading, on the `.log` a CD folder must carry,
  or on a verification that did not pass — is its own classification
  (`wishes.outcome_of` answers `"rejected"` beside `"not_found"` and
  `"transient"`), because the network HAVING copies this app will not take is a
  different fact from a network that has none, and only one of them is worth
  retrying unchanged. `wishes_worker._try_candidate` returns `empty` for it, so
  the walk advances to the next ranked edition exactly as it does after a miss,
  and `_settle_attempt` sends a walk whose every edition was refused to the
  BACKGROUND (R153) WITHOUT spending a not-found attempt — the refusal was not
  a miss. What this fixes: the sentence classified as `transient`, the one
  classification that STOPS a walk, so the wish was re-marked `wanted` with the
  backoff, `wishes.restart_walk` put it back on edition 1, and a release group
  whose best edition scores below `soulseek_auto_log_min_score` (or whose
  rip logs never reach it) looped on that edition for ever — the reported "won't
  move on to the next best release", with a "Retrying in 28m 55s" that never
  named another edition. `tools/test_wishes_pipeline.py` now drives a
  three-edition wish through `_run_one` and asserts every edition is asked in
  ranking order, that the walk's own record moves with it, and that the end is
  `background` with the not-found counter still zero.

- **R178 — a failure retries on ITS OWN clock, and a walk that keeps failing
  stays quiet.** Two halves of one report ("Retrying after a failure at 12:10",
  five hours after the download started; a fallback release landing in the
  queue's *Failed* section):

  - **the cadence.** `wishes.due_at` was `max(last_search + interval, retry_at)`,
    so a transient failure's backoff (`wishes_retry_backoff_minutes`, shipped 30,
    doubling to a day) was swallowed whole by the periodic interval
    (`wishes_interval_hours`, shipped 6): a peer that was simply down was
    re-asked six hours later, and the time the row showed had nothing to do
    with the failure it followed. Now the two clocks are separate and each
    governs its own case — a wish whose last attempt FAILED is due at
    `retry_at` (the backoff), and a wish that merely found nothing is due at the
    interval. `retry_at` is only ever stamped by that failure path, so its
    presence IS "the last attempt failed"; with the backoff turned off (0
    minutes) nothing stamps it and the interval remains the floor, which is what
    keeps a disabled backoff from turning every tick into a retry.
  - **the silence.** A wish that carries a ranked walk never ends `failed`
    (`wishes_worker._settle_attempt`): spending `wishes_max_attempts` on a
    release EVERY edition of which the network refused sends it to the
    BACKGROUND instead — `mark_background` with the walk's own report, the same
    non-terminal state R153 defines — so the row stays where it was, keeps its
    framework album, and is re-walked from the best edition on the worker's
    ticks. *Failed* is the section for things a person has to deal with, and
    "the network did not have it yet" is not one of them for a release the app
    was asked to find by name. A wish with NO ranked list (a name-keyed
    wishlist row) keeps the terminal `failed` outcome it always had, because
    nothing is left to re-ask.

- **R215 — one spent candidate of a walk is not an outcome and announces
  nothing.** A settled auto-import job that fills a wish is ONE step of that
  wish's search: its failure means another ranked edition is about to be asked,
  or the release is resting in the background (R153) — not that the request
  gave up. `soulseek_auto._wish_keeps_looking()` reads the job's `wish_id` and
  the store's own verdict (`wishes.is_terminal`), and `_notify_finish` returns
  BEFORE the `download_failed` frame when the store will search again; the ends
  that ARE outcomes are announced by the layer that owns them (`wish_failed`,
  `wish_not_found` from `server/wishes`), and R153's background phase stays
  silent. A job with no wish behind it — the interactive search, a bulk add —
  keeps its own `download_failed`, because nothing else will ever say it gave
  up, and so does a wish-keyed job whose wish HAS given up. Before this, a
  release the app had not given up on announced "Download failed" once per
  abandoned candidate.

- **R179 — ONE RELEASE IS ONE TILE, even mid-import.** A framework album is a
  row of its own (a folder with a marker and no audio) and the album the audio
  landed in is another, so a release that exists as TWO folders was listed
  twice for as long as the placeholder survived — the chain's own end clears it
  (`imports._finish_album` → `pending_albums.clear_if_filled`), which is
  minutes of a duplicate tile, and a placeholder whose chain never ends (a
  review stop, a crash) stayed a duplicate for good. `server.library`'s payload
  now answers the question the folders cannot: `_drop_filled_placeholders` runs
  over the finished tree and the placeholder YIELDS to the album, matched on the
  release id (`MUSICBRAINZ_ALBUMID`, the release GROUP id as the fallback) —
  never on the folder name, because the whole point is that the two folders are
  named differently. A placeholder whose release is NOT in the payload keeps its
  row (that is the album the user asked for and nothing has filled yet), and an
  artist row left with no albums goes with it.

- **R180 — ONE IMPORT PER ALBUM.** A second autonomous import of an album
  another job is already importing used to QUEUE behind that job's claim and
  then run the whole pipeline again — the six pre-chain lookups and every
  script, over an album the first caller had just finished. `imports.finish_album`
  answers `already_importing` instead (`_importing_now` reads the ONE registry,
  `server.job_locks`, and takes only a claim whose kind is `auto-import`,
  `import` or `scripts` — the same three `server.api_queue` reads for its In
  progress section). A job importing its OWN claim is never a duplicate: the
  download job holds the album from its first byte and then runs this very
  import under that claim, and a caller with no job of its own keeps the old
  wait-then-run behaviour, because skipping there could leave an album
  unimported. The user's own press (`wait=False`) keeps its 409, whose sentence
  names the holder.

- **R181 — a disc that could not be checked is not a failed check.**
  `mlo.grader`'s CD verdict charges a missing leg once — every leg the app's own
  artefacts decide (a LOG_GRADE the scorer writes, the log's CRCs) — with ONE
  exception: the AccurateRip leg. A pressing the database has never seen reads
  exactly like a disc with no `.accurip` at all and no code path can tell the
  two apart, so failing the album for it failed the rip for what the network
  does not know. It is reported in the grade's `notes` channel (the same one the
  artist image checks use: inform without failing) and rendered as "Not
  checked", never under "Failed checks" — the state is stated, the album is
  judged on what could be measured, and the stored verdict is still never
  guessed.

- **R182 — the app's own sidecars are a FAMILY, and a numbered copy is one of
  them.** `mlo.artistdata.write_description` replaces `description.txt`
  atomically, so the app never writes "description (2).txt" — a copy arrives
  from outside (a file manager, a sync client, an older build). It is still the
  album's description: `paths.album_sidecar_of` recognises the family,
  `artistdata.description_path` reads a copy (the canonical name wins when both
  are there), and the layout scan reports `sidecar_copy` with a rename fix to
  the canonical name instead of calling the app's own file dead weight. Two
  descriptions side by side stay the reader's to sort out — the app does not
  guess which text is the right one.

- **R183 — the import pipeline runs the steps that CAN overlap side by side,
  and one track at a time where the provider's interval is the wall.** The six
  steps between the press and the first script are not one kind of work: links,
  genres, advisory and instrumentals write TAGS on the audio files, while
  metadata (artist image, descriptions) and cover art write FILES — the review
  record they share, and the art the album folder keeps. The file pair is
  therefore started on one worker right after the genre step (which settles the
  identity its lookup reads, `album_identity`) and joined before the chain,
  which is the first thing that needs it on disk (script 5 processes the
  images, the grade wants the cover). One worker for both, in their own order,
  because they stage into ONE review record. The pair announces itself with ONE
  phase line ("Fetching metadata and cover art…"), and the phase list
  `tools/test_import_pipeline.py` checks is that list.
  **The per-track passes are NOT all fanned out, and that is measured.** They
  were, on the shape the per-file writers use (`drop_arrived_values`,
  `_stamp_release`): one worker per file, distinct files sharing nothing. For
  the ADVISORY pass that made the album slower — 24 s to 41 s, with 38 s of the
  pass asleep inside `_apple_json` where the serial pass spent 5 — because
  Apple's interval is GLOBAL and the serial pass never reached it: three
  seconds already separate its per-track calls, and the first track warms the
  album-level answers the rest reuse. Eight lanes arriving together turn that
  headroom into queueing. The pass is serial again, with the measurement in a
  comment so nobody re-fans it by pattern-matching. Instrumentals DO fan out per
  file, and pay, because LRCLIB's interval is 0.4 s. The rule is the interval,
  not the pattern: fan out when the provider's spacing is shorter than the work
  between calls, never when it is the wall.
  `_apple_json` keeps the spacing under its lock and takes the REQUEST outside
  it (the shape `discovery._throttle` always had): held across the call, one
  slow answer stalled every other Apple caller behind it, and the interval
  became a ceiling for the client instead of a gap between requests.
- **R184 — an import writes each file ONCE per pass, and a tag write never
  costs a decode.** Auto tagging (script 8) fills tags in six stages — the
  whitespace fix, the release identity, INSTRUMENTAL, the derived album
  advisory, the instrumental zero and its re-derivation — and each `set_tag`
  used to save the whole container for itself: six whole-file copies of a 30 MB
  track, on a library whose script 3 writes `--padding=0`, so there is no
  padding to absorb them. The album pass now defers (`mlo.audio.defer_save`,
  the idiom `_fill_release_tags` already used for its own dozen) and flushes
  once per file at the end, so a file costs ONE write however many of the six
  stages fire, reporting by name any file whose single write could not land — a
  failed flush wrote NOTHING, so it is never counted as written. (Measured on a
  fixture where one stage fires, the count is 1 per file either way: the bound
  is what the change buys, and it pays on the albums where the advisory,
  instrumental and derived tags all land at once.)
  And the CRC memo (`mlo.discs._audio_crc32`, the decoded-PCM CRC every CD log
  check and the audit share) is keyed on the container's OWN audio identity —
  FLAC's STREAMINFO MD5, the identity `mlo.accurip` already keys its evidence
  on, which a tag rewrite leaves and a re-rip or re-encode changes — as well as
  on size+mtime. Before it, every script that wrote a tag moved the mtime and
  made the next reader decode the whole track again with ffmpeg; the import
  writes tags on every track, so the second grade of an album paid for twelve
  decodes it had already done.

- **R169 — the walk asks DISTINCT PRESSINGS: two editions that state the same
  catalog number are ONE search.** Separate MusicBrainz releases really do share
  one — the same CD issued under two labels (DGC's `GED 24425` beside Geffen's
  `GED24425`, both catalogued releases of one pressing), a reissue catalogued
  twice, a country variant printed with the number unchanged — and the catalog
  number is what a CD search is keyed on, so the next edition in the walk can
  only find the folders the previous one already found. That is a whole search
  window (R151) spent for nothing. `mlo.release_choice.catalog_key` is the ONE
  folding — letters and digits, case, spaces, dashes and dots removed, because
  a number's spelling is whoever printed it — and
  `mlo.release_choice.distinct_pressings` is the ONE rule: an entry sharing a
  folded number with one already in the walk is dropped, and the dropped entries
  are RETURNED rather than swallowed. An edition stating NO catalog number is
  always kept (there is nothing to compare it by, and a pressing with no number
  may still be a different upload — its search is built from artist, title, date
  and label instead), and the first entry is always kept, so a walk never comes
  back empty. The numbers cost no request: the release-group browse fetches them
  with `inc=…+labels`, the same call that already returned those editions. The
  rule is applied where the list is BUILT (`integrations.group_targets`, so the
  count a row shows is the walk it will really take) and again where the walk is
  built (`server.wishes_worker._walk_candidates`, so a list stored before the
  rule existed is deduplicated on its next attempt), and the walk LOGS what it
  skipped — naming the editions — because a fallback that quietly loses a ranked
  edition is exactly the kind of shortcut nobody notices until an album never
  lands.

- **R170 — the queue's add takes ANY MusicBrainz entity.** The bar above the
  queue accepts a release, release-group, artist or recording **URL** (or a bare
  MBID) and turns it into exactly what the MusicBrainz pages' own *Add to
  library* makes, because it calls the same route: `POST /api/library/add` with
  the entity's own `kind` — parsed off the URL path
  (`web/src/pages/SoulseekPage.tsx`'s `mbRef`, `release` / `release_group` /
  `artist` / `recording`) — and `kind: "auto"` for a bare MBID, which
  `server.api_add._intended_kind` resolves through `integrations._kind_for`.
  The two paths therefore cannot disagree about what a pasted id is: a
  release-group walks the group's ranked editions (R150), an artist queues its
  discography in the background (`background: true`, the albums appearing as
  each is created), a recording resolves to the release that carries it, and a
  release is added as itself. What the bar used to do was parse ANY MBID as a
  RELEASE — an artist or release-group link was queued as if the artist's UUID
  named a pressing, which created a framework album that could never be found —
  and a link to an entity the queue cannot look for (a label, a work, a place)
  is now refused with the list of what it does take, rather than having its UUID
  read as a release.

- **R175 — every add RECORDS the walk it will take, or the walk is one edition
  and a spent search ends the release.** The ranked list R150 walks is only ever
  as good as who writes it: `server.api_add._create_all` resolves its targets
  through `integrations.auto_import_targets` (whose rows already carry
  `candidates`, best first, deduped by catalog number per R169) and had dropped
  that field on the floor, so every ordinary add — the MusicBrainz pages' *Add
  to library*, the queue bar (R170), a deferred add's background resolution, an
  artist's prepared rows — recorded a wish with NO list. With no list the wish
  is a single-candidate acquisition: a pressing whose peer folders hold nothing
  usable ends `not_found` (terminal, framework album taken down) instead of
  moving on to the next edition, and `wishes.candidate_state` has nothing to
  report, so no surface can say where the search is. The list is passed straight
  through to `pending_albums.create(candidates=…)`, which is the ONE writer of
  `wishes.set_candidates`: it fills an EMPTY list only (a wish already walking
  keeps its order; `rearm` starts a fresh walk), it accepts a ONE-entry list as
  readily as three — that single entry is what tells the store this wish carries
  the ranked editions of an album request, and therefore keeps a spent walk in
  the BACKGROUND (R153) instead of ending it — and it keeps each entry's catalog
  numbers, which is what the walk dedupes by. Every other writer of that list
  (the artist watch's `queue_release`) already did this; the add path is the one
  that did not.

- **R176 — a walk is ONE row, and that row says which edition it is asking.**
  The candidates are asked one at a time INSIDE the one wish
  (`wishes_worker._run_one`), so a release is one queue entry however many
  editions it tries — never a row (or a job) per edition — and the row carries
  the position as its own badge: `SlskQueueItem.walk`
  (`{index, total, label, mbid, title, tried}`, built by
  `wishes.candidate_state`) renders beside the stage and source chips, labelled
  with the server's own wording (`release 2 of 3`) so the row, the album page
  and the notification cannot disagree, with a tooltip naming the edition being
  asked and the ones that already came back empty. `walk` is null — and the
  badge is absent — when there is nothing to walk: no list, or one entry, where
  "release 1 of 1" would be noise. The row's `note` keeps the sentence
  (`release 2 of 3: <title>` while a job is asking it, `next: …` between
  attempts, `tried N of M · no usable copy yet · searched again automatically
  around HH:MM` in the background phase); the badge is the at-a-glance form of
  the same fact, and it is the ONLY new UI a walk needs.

- **R154 — an import USES what the add already fetched, where the add's record
  is an IDENTITY.** The add path resolves the album's page content before the
  audio exists (`imports.prefetch_album`, R140) and the import then asked the
  providers for the same answers a second time. Measured on one album through the
  real chain with the real network (`.pi/import_reuse.py`, one process per side,
  the pre-change sequence reproduced by stubbing the reuse seams off): the ADD
  paid **1** `integrations.rym_links` and the IMPORT paid **1** of it AGAIN —
  18.5 s of add, 73.9 s of import — for a RateYourMusic link the framework marker
  already recorded. So `imports._marker_links` hands `stamp_rym_links` the links
  the add resolved (the TAGS are still written — only the lookup is skipped, and
  only for a link the marker really carries for THIS album: it must be a
  framework marker, the release group the album's own tags state — when they
  state one — must be the group the marker was created with, and the switch
  `rym_links_auto` is checked here too, so "off" still writes no auto-resolved
  link). The same rule covers the metadata step: with `metadata_review` on, the
  add staged the artist/description candidates for exactly this artist and album,
  and `imports._staged_metadata_held` lets `run_metadata_step` keep that record
  rather than fetch the same candidates again — it only skips when the entry's
  own artist and album ARE this album's, and the review screen the user already
  has is then the answer this step would produce.

  The COVER candidates are deliberately NOT reused, and that is the rule and not
  an omission: the staged cover record is a PICK SCREEN — any surface may restage
  it, `staged_metadata` finds it by folder name or by MB id as well as by path,
  and it carries no proof of which search, for which album, produced it — so
  ranking it would mean writing an image the policy chose from ANOTHER search's
  rows instead of the best of what exists for the album being imported. That is
  the one thing both cover modes share (`test_covers` pins it: with
  `cover_review` off, the same fresh candidate set is ranked and its winner
  written), so `run_cover_step` still ranks a fresh set and the import pays that
  search. What each step saves is bounded by that: a saved lookup must be
  attributable to THIS album by identity, or it is not reused.

- **R155 — one grade per import, and the invalidation is scoped to the album.**
  The import's own report and the chain's own Grade step were asking the grader
  the same question about the same album seconds apart: script 4 is LAST in the
  shipped `run_all_order`, and `_report_gaps` then graded the album again to
  derive the missing families (`mlo.import_policy.gaps`). `run_grade_library`
  now publishes what it graded into a PRIVATE sink the import put on its own copy
  of the run config (`config["_grade_sink"]`: nothing else reads it, no run's
  payload grows, and no other caller pays for it), and `gaps(...,
  grade=…)` uses that grade instead of running `_grade_album` a second time.
  One grade per import. It is used ONLY when it is an answer to the same
  question: with a family the user kept for review, `gaps` grades with that
  family's writer switched back on (its own comment above), which is a different
  question, and `finish_album` leaves the sink off entirely for that config —
  `mlo.import_policy.review_families(cfg)` empty is the condition — so what is
  reported as missing is unchanged in every configuration. The invalidation is
  the other half: an import rewrites ONE album's tags and writes that album's
  cover, and `imports._invalidate_caches` used to answer with
  `tagcache.invalidate_all()` — the whole tag cache (16384 entries of the user's
  library, re-parsed by the next page), the cover cache and the assembled
  `/api/library` payload. It now passes the folders it actually wrote
  (`tagcache.invalidate_album`: the entries under those folders, plus the one
  assembled payload, which is keyed by library folder and config and so cannot be
  scoped), from every writer in the import path: the album's own steps, the
  albums an advisory pass touched, the artist folder a metadata write filled, and
  `finish_album`'s own two calls (the folder it was handed and the folder the
  chain left the album at, because a script may have moved it). A caller that
  cannot name what it touched still gets `invalidate_all()`, and nothing about
  what a reader then sees changes — the album's tags are re-read from disk, not
  served from the cache that was just invalidated by name.


- **R110 — the app's two transient stores have two INDEPENDENT size caps, and
  a store over its cap is emptied oldest first.** `soulseek_cache_cap_gb` and
  `trash_cap_gb` (both 5 GB shipped, Settings → Storage, one decimal; 0 or
  negative = that store's cap off, never a shared total) are enforced by
  `server/cache_caps.py` against the LIVE folders, so a leftover from an older
  version counts like anything else: the Soulseek cap measures the configured
  download dir (`soulseek_download_dir`, else `<music folder>/.mlo/downloads`)
  plus the `incomplete` sibling slskd stages into, and the trash cap measures
  `<music folder>/.mlo/trash` across every per-user bin. The unit of deletion is
  the one the app's own routes delete — a top-level entry of a staging root
  (`/api/soulseek/staging/delete`, the Downloads page) and a child of a
  per-user bin (`/api/trash/delete`, the Trash page), dot-entries under a
  staging root excepted because they are slskd's own staging tree — and the
  pass stops the moment the store fits, so an entry that alone would overshoot
  by far is taken only when the store is still over without it.
- **R110a — a prune never takes what is in use, and says what it took.** Two
  questions decide, both asked of state the app already trusts: is a path held
  in `server.job_locks` (the registry every route is refused against, so the
  report carries that job's own sentence), and does a running slskd transfer
  own the name — its peer's username, the leaf of the remote folder it is
  writing into, and, for a LOOSE entry, its file name, read from
  `soulseek.downloads_state()`: the same evidence `import_completed()` refuses
  to move an unfinished album on. Such an entry is skipped WHOLE, named in the
  report, and the cap stands above its limit until the transfer or the job is
  done; an entry the filesystem refuses to give up (a file slskd still holds
  open) is kept and reported the same way, never worked around. A trash entry
  is deleted exactly as `/api/trash/delete` deletes one — the entry first, then
  its origin record dropped from the bin's `.mlo_manifest.json` — so every
  entry a prune KEPT is still restorable to the path it came from. What a prune
  did is a normal, expected action: one log line and one notification
  (`storage_pruned`, "Freed …", linking to the page that owns the store), and
  only what could not be deleted is reported as a problem. The pass runs from
  its own worker thread started with the app (`server/main.py` lifespan, tick
  300 s, first pass after a 60 s settle), so an install nobody has opened a page
  on still holds its caps.

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
- **R91a — the player carries the same track-details entry as a library row.**
  A listener who wants a track's stored readout and credits should not have to
  go and find it in a table: the player bar's own ⓘ (`PlayerBar.tsx`) and the
  fullscreen player's options menu both open `DetailsDialog` — the same modal
  the library row's info button opens, from the same `["album", dir]` payload
  (so the bar makes no request until the entry is pressed, and the two surfaces
  can never show different data). Rendered from inside the fullscreen player
  rather than through it: the Modal layer is `z-[60]` against the player's
  `z-50`, which is what lets a dialog sit over the fullscreen view.

- **R221 — a title that does not fit DRIFTS, on both surfaces.** The player
  bar's marquee is its own component now — `web/src/components/ScrollingText.tsx`,
  out of the inline copy that used to live in `PlayerBar.tsx` — so the bar and
  the fullscreen player share ONE mechanism, and with it the `title-marquee`
  keyframe and its `--title-shift` variable in `index.css`. It MEASURES rather
  than guesses: a `ResizeObserver` watches the wrapper AND the text, so a
  web-font swap, a badge appearing beside the title and a window resize all
  re-measure, and `shift` stays `0` for anything that fits — a short title must
  not wobble. The drift distance is the overflow plus 6 px of visible padding,
  the period is `Math.max(5, Math.min(24, shift / 12))` seconds, and
  `prefers-reduced-motion` still kills the animation. Both of the fullscreen
  player's long lines use it: its TITLE (which used to `truncate`, cutting a
  track name mid-word — the reported case) and its album·artist row.
- **R222 — the fullscreen block names one fact pair per row.** The text block
  in `NowPlayingView.tsx` draws the title row (h-8, title plus the advisory
  mark and the codec readout), then `Album · Artist` on ONE h-5 row, then the
  star row (h-7) — where the album and the artist used to stack as two rows
  that read as two unrelated lines (reported). The pair is joined with " · "
  and the whole pair rides the row's own `title`. Every row keeps its fixed
  height and is always rendered, so the block still cannot jump on
  next / previous.
- **R223 — the player bar's grid may not let a flank overlap its centre.** The
  desktop grid in `PlayerBar.tsx` is
  `grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]`, not `1fr_auto_1fr`: a bare
  `1fr` track still carries an `auto` MINIMUM, so neither flank could shrink
  below its own content and, at high browser zoom or a narrow window, the left
  cluster ran over the centred seek row — the reported overlapping "up next" /
  duration readouts. `minmax(0,1fr)` lets a flank truncate instead, which is
  what the title's own marquee (R221) and the flank's `min-w-0` already assume.
- **R224 — the frequency strip's resize check is its BACKING STORE.**
  `Visualizer.tsx` sizes the canvas from its box and
  `dpr = Math.min(2, window.devicePixelRatio || 1)`, and compares
  `Math.round(w × dpr)` / `Math.round(h × dpr)` against `canvas.width` /
  `canvas.height`. The old test compared the CSS width alone (`lastW`), so a
  HEIGHT change (the strip's own box, a zoomed pane) or a devicePixelRatio
  change (the window dragged to another monitor, a browser zoom step) left the
  previous bitmap in place and the browser stretched it into the new box — the
  reported "two offset rows of bars". Comparing what the canvas actually holds
  catches all three, and a resized frame is repainted rather than skipped as an
  idle frame.
- **R225 — a slider's dot and its ring are one shape, so neither animates
  into place.** `index.css`'s `input[type="range"]::-webkit-slider-thumb` draws
  the outer ring as a `box-shadow` ON the thumb and no longer transitions
  `transform`: the ring is drawn from the thumb's own transform, while the
  thumb's POSITION follows the pointer natively and never animates, so a
  `transition: transform .1s` under a hover `scale(1.25)` (`.seek-fat`:
  `1.15`) showed the dot jumping per pixel and the ring scaling after it — the
  reported "the dot and the outer ring move at different times", worst during a
  drag, where hover flickers and the scale is mid-transition for most of the
  gesture. Hover changes instantly now, in the same rule, and
  `::-moz-range-thumb` carries the same ring.

### 7.16 The library page: the five views, the columns and the filters

- **R103 — the library offers five views, and each one draws rows.** Grid
  (covers), Compact (status rows), Albums (one row per album), Artists (one row
  per artist, ARTIST-IMAGE aside) and Tracks (one row per file) — `VIEW_TABS` in
  `web/src/lib/libraryView.ts`, the tabs the Library and Home share. Grid and
  Compact draw cards/lists; the three table views are real tables over the same
  payload (`Albums` also carries the per-album tracklist under an expanded row).
  A table that filters down to nothing says so in the table (one row, spanning
  it: "No tracks match these filters — clear them in the Filter menu"), because
  a header over blank space reads as a broken view rather than as an answer.
- **R104 — a table cell's text is never squeezed to nothing.** The app's tables
  are `table-layout: fixed` with a per-column px FLOOR (`lib/columns.tsx`), and
  the floor is what the cell's content actually needs: a cell that shares its
  line with fixed-width marks (`TrackTitleCell`'s trailing controls, the
  title's advisory/grade/cached badges) WRAPS those marks onto a second line
  when the column is too narrow, instead of shrinking the name to a one-pixel
  column that renders one character per line. The Tracks view shipped that
  failure: its title cell held the name, its marks AND the star rating in 220 px
  against ~200 px of controls, so the name lost and the view read as empty
  200 px-tall rows. The rating therefore has a COLUMN of its own
  (`TRACK_RATING_COL`), the title floor is 280 px, and a regression is caught by
  measuring the rendered table — `tools/check_library_tables.cjs` asserts, for
  every view, that no text-bearing link is under 40 px, no row is over 120 px
  tall, every header label fits its column and every column holds its widest
  value.
- **R105 — the library filters on the user's own ratings and on the advisory.**
  The toolbar's Filter menu carries the presets (Failing, CD rips, Digital,
  Instrumental, Music videos, No lyrics) plus two FACETS, each with the count of
  the rows it would leave:
  * **Star rating** — Any / Rated / Unrated, over the user's own stars and
    nothing else. A track counts as rated when its own file has a rating; an
    album only when the verdict on the ALBUM itself is in (its folder rating)
    AND every track in it carries one of its own — a half-rated album is not
    finished, and the "Unrated" list is where it belongs (the reported "only
    one track counted"); an artist when any of its albums is. Nothing here is
    an average, and the rule is printed in the menu itself (`RATED_NOTE`)
    rather than left to a tooltip.
  * **Advisory** — Any / Explicit / Clean. Explicit means `ITUNESADVISORY` 1 (the
    badge the tables draw); Clean means everything that does not flag explicit:
    2 (the clean EDITION) and 0/absent (nothing marked it explicit). An album
    counts as explicit when ANY of its tracks is, and clean only when NONE is —
    the direction that matters when the filter is used to keep that material
    away.
  The chip names every facet in force and marks itself when any is on, and each
  menu row's count is computed with the OTHER filters applied but the facet
  itself open — a count that already had its own filter applied could only ever
  echo the current selection's size. The old `Explicit` PRESET is gone: two
  controls for one condition is how they end up disagreeing.

- **R106 — a page names an artist the way every other page does, and shows the
  picture it has.** The library payload carries each artist row's folder
  `name` AND its `display_name` (`server/library.py`: the artist's own
  ALBUMARTIST tag when the albums state one, else the folder basename with its
  MusicBrainz disambiguator stripped — `strip_mbid_suffix`). Everything that
  PRINTS an artist reads the display name: Home's Top artists shelf, the
  Artists table, and the album rows' fallback when a file carries no
  ALBUMARTIST (a folder is named `Radiohead [<mbid>]`, and the raw basename was
  what these surfaces used to show). The folder identity stays `name`/`path`,
  so links and ratings keep pointing at the same rows. Home's shelf draws the
  artist's OWN image (`/api/artist/image`, the endpoint the artist page uses)
  when the artist has one — the payload carries `has_image` so a folder without
  a picture is never probed, and a URL that fails anyway falls back to the
  representative album cover and then to the placeholder. Home also carries the
  **Your ratings** shelf: the user's rated releases, highest first, in
  half-stars — the same unit the API and `lib/ratings.ts` speak.

- **R218 — a details menu fits the window, and every action it lists is
  reachable.** The "…" menus are capped to the room their OWN trigger leaves, in
  the viewport, by `Popover`'s fixed mode (`maxHeight` from the measured rect,
  `100dvh` so a phone's URL bar is not counted twice) — and to the room on the
  side the panel OPENS, so a top-placed panel is bounded by the space above
  its trigger (the trigger's own `top`), never by the space below it; `OverflowMenu` therefore
  defaults to `fixed` and drops its old `max-h-[70vh]`. The cap is what makes
  the panel scroll INSIDE the window: the app shell is `h-dvh overflow-hidden`,
  so a panel running past the fold was unreachable, not merely clipped — the
  reported "the menu is cut off", whose fix cannot be another `overflow-y-auto`
  (the panel already had one). The long menus keep the app's own thin scrollbar
  (`index.css`, no `scrollbar-hide` anywhere) and `overscroll-contain`, so the
  tracklist behind them does not move with the wheel. A ROW's menu and an
  album's readout both carry the two file actions the pages' headers have —
  **Download for offline playback** (the ONE implementation, `lib/offline.ts`,
  shared with `DownloadButton`) and **Export…** (the pages' own `ExportDialog`)
  — because "download / export this" must not mean opening another page first.
  `tools/check_menus.cjs` walks the sidebar and the cover menu's geometry.

### 7.17 The first-run setup wizard asks only what is required

- **R107 — the wizard's steps are the ones that need an answer.** Six:
  Folder (the music library), Account (the login gate), Tools (the dependency
  download), Keys (the source credentials and cookies: Spotify, Discogs,
  Last.fm, RYM, AcoustID), Soulseek (the managed slskd login and sharing) and
  Done. Every quality/check knob — the grading switches, the audit and
  verification options, the script chain, the naming script, the interface
  preferences — is NOT asked here: those are `mlo/config.py` defaults, editable
  in Settings afterwards. The Keys step asks for the CREDENTIALS themselves
  (the seven fields a first run has to paste, each with where it comes from and
  a Save & test) and leaves the provider rows — statuses, notes, enable
  switches, Test buttons — to Settings → Sources or behind a per-row
  disclosure: asked with those visible it was 4,779 px tall, which is what "the
  wizard is a wall of knobs" meant in practice, and it is 1,468 px with them
  folded away (measured at a 1280 px viewport, same pass as the other five
  steps: 900–1,330 px). The defaults ARE the strict ones (the
  `grade_check_*` / `grade_include_*` / `audit_*` families, the 100 % log-score
  thresholds, the zero cover tolerances), so a fresh install grades as strictly
  as a configured one; `tools/test_setup_coverage.py` pins both halves of this
  rule — each group is rendered by at most one step, and every group no step
  renders still has all of its keys named by a rendered Settings control (with
  one documented exemption, the export defaults the Export page's own form
  writes). A key may never lose its only editor to a shorter wizard.

### 7.18 Transfer progress is pushed, and it is as live as the bytes

- **R120 — a bar that tracks moving bytes is drawn from a PUSHED row, and the
  push is paced by the bytes, not by a timer.** The Soulseek page's transfer
  rows (`GET /api/soulseek/downloads`) and every live job's progress block
  (`server/soulseek_auto.py`'s own `progress`, the same one
  `GET /api/soulseek/auto` serves and the queue rows are built from) are also
  sent over the progress WebSocket as `{"type":"transfers"}` frames
  (`server/main.py`'s `_soulseek_transfers_watch`), and the page draws those
  bars from the frame (`web/src/pages/SoulseekPage.tsx`, via
  `web/src/lib/notifications.ts`'s `publishTransfers`/`useLiveTransfers`).
  Three things this pins down:
  - **Cadence.** 0.4 s while a transfer is `InProgress` or a job is running —
    2.5 frames a second, roughly 370 bytes each — and 5 s otherwise, including
    for a queue slskd has not started yet. Nothing is sent when nothing
    changed, and with no UI socket open the watcher reads slskd not at all.
    Measured on a scratch instance against a 2 MB/s transfer (see the README's
    measurement note): the bar moved every **0.40 s** and showed a value
    **0.17 s** old on average (p90 0.31 s), where the 3 s poll it replaced gave
    **3.02 s** and **1.48 s** (p90 2.79 s); a state flip reaches the screen in
    **0.38 s**; and once a queue has settled, 12 s of it send **0 frames** and
    cost 0.5 slskd reads a second.
  - **Accuracy.** A frame carries slskd's own `bytesTransferred`,
    `percentComplete`, `size`, `state` and `averageSpeed`, so the percentage
    stays byte-exact and a bar moves BACKWARDS only when slskd really reports
    less (a retried transfer, or a partial deleted under it). Nothing is
    interpolated: a frame says what the wire says.
  - **One source per bar.** The page's own poll of the same query stays as a
    **30 s** fallback for a dead socket, not as the cadence; a frame that says
    the list changed shape (`resync`) triggers the one refetch a patch cannot
    express.
  Progress is a STATUS, never an outcome: frames ride the progress socket only
  and never reach the notification tray — `publishTransfers` is deliberately
  not `ingest`, so a byte count changing four times a second can raise neither
  a toast nor an OS notification.
- **R121 — an import that needs a HAND stays an outcome, in three places.** An
  album an import could not finish raises `import_needs_data`
  (`server/imports.py`'s `_report_gaps` → `server/import_autonomy.py`'s
  `raise_prompt`, over `mlo/import_policy.py`'s `gaps`, i.e. the grader's own
  issue codes), and that ONE fact is published where a person actually meets
  it:
  - **the tray and the OS popup** — `import_needs_data` is in
    `OS_KINDS` (`web/src/lib/notifications.ts`), so the desktop/mobile shell
    raises a system notification and the bell's panel keeps the entry;
  - **the push** — the same frame on `/ws/events`, with `link`,
    `album_path`, `reason` and the missing `families`;
  - **the Soulseek page** — the queue's *Needs you* section carries a row that
    NAMES the album and lists the families and the reason in the app's own
    words ("Links — no source could supply it (MusicBrainz release link, …)"),
    whose action (`action: "manual"`, `action_link`) opens the import wizard at
    that album's first missing step (`/import?album=…&step=…&missing=…`) —
    an item to press, not a line of text.
  It fires on the manual-tagging state ONLY: never for progress, never for a
  finished import, and never twice for the same album — the same gaps, reason
  and mode are not a new outcome (`raise_prompt` compares them before it
  emits), while a different set of missing families IS. Verified end to end in
  a scratch instance: with `import_review_families: ["cover"]` the import of an
  album left undecided by its own switch, the chain finished, the prompt named
  `2021 - Vinyl Rip {CD}` with `Links, Cover art, Genres, Lyrics`, the tray
  held "2021 - Vinyl Rip {CD} — needs extra data" (R166's wording: the album
  had already landed), and *Enter manually* landed on
  `/import?album=…&step=Links&missing=links,cover,genres,lyrics`.

### 7.19 Downloads: what a copy holds, and which copy plays

- **R171 — a downloaded copy is the file's OWN codec unless the user says
  otherwise.** Downloading caches a track on the device (the Downloads page, the
  Download button, the player bar's own control) so it plays with the server
  away. `download_codec` ships as **`copy`**: the cached bytes ARE the library
  file's — same codec, same bits, nothing re-encoded — so a track is never
  downloaded in a format it is not already in, and the setting exists only for
  the device that has no room for the library's own format. Any other value is a
  target from `mlo.containers.CODECS` (the same list `library_codec` takes,
  minus `keep`, because `copy` IS "never re-encode" — offering both would be two
  names for one state), and `download_bitrate` is that target's rate exactly as
  `library_codec_bitrate` is the library pass's: kbps for the CBR targets
  (mp3/aac/opus — the flag is ffmpeg's own `<n>k`), libvorbis' 0-10 quality
  scale for ogg, **0 = the codec's own shipped default**, clamped per codec by
  `mlo.containers.codec_args` and never reaching a lossless target. The
  rendition is the DOWNLOAD's: `/api/stream?download=1` serves it
  (`server/api_media.download_rendition`), the library file is never touched,
  streaming an un-downloaded or un-preferred track always serves the library's
  own bytes, and `POST /api/media/bulk` — which frames each file's size up front
  and so cannot carry a re-encode — refuses with **409** and says which key is
  responsible; the client's queue then drops to one request per track
  (`lib/mediaCache.downloadTracks`).
- **R172 — which copy PLAYS is `playback_source`, and it ships as streaming.**
  `stream` (the shipped default) asks the server for the library file even when
  a copy is downloaded; `downloaded` plays what is cached. ONE resolver decides
  it (`web/src/lib/mediaCache.playbackSource`) and every player surface goes
  through it — the audible `<audio>`, its gapless preload, both lyric previews
  and the video popout — so the setting cannot be honored in one place and
  ignored in another. Two things outrank it: the server being unreachable
  (`isOffline()`, the API answering from its own cache) makes the copy the only
  thing that can play, so it is played whatever the setting says; and a video's
  live transcode (`?transcode=1`) is a DIFFERENT rendition, so a copy — which
  holds the direct stream's bytes — stands in for it only offline. A stream URL
  a copy exists for carries `nocache=1`: the service worker's media branch is
  cache-first on the element's own URL, so without that marker "prefer
  streaming" would play the very bytes it is asking to avoid (the server ignores
  the parameter, and nothing is ever stored under it).
- **R173 — cached playback works in every client, and the cache is keyed by the
  TRACK, not by the session.** The download is stored under the URL the player
  asks for (`playbackUrl`), which is what makes the service worker's
  `cache.match` hit on ordinary playback in a browser; a shell has no service
  worker at all (Tauri skips the registration), and there the same lookup hands
  the bytes back as a `blob:` URL — the ONE way a downloaded track plays with
  the server away, and the reason `playbackSource` exists rather than each
  element building its own URL. That key holds NO session token
  (`mediaCache.cacheKey`): a shell's media URLs carry `?token=…` because a
  webview cannot send the cookie, and the token is re-issued at every sign-in,
  so a token-bearing key would stop naming its own bytes the moment the user
  signs in again — a downloaded album reading as undownloaded with every byte
  still in the cache. Stripping is textual, never a URL re-serialization:
  `URLSearchParams` would rewrite a space as `+` where `streamUrl` wrote `%20`,
  and the key would then miss the very request the service worker matches. The
  artwork warmed beside the audio is keyed the same way.
- **R174 — the rip-log bar is 100, and it is the same number in all three
  places it is asked.** `soulseek_auto_log_min_score` (the acquisition gate: a
  CD candidate's `.log` must be scorable, score **>= 100** and not
  checksum-invalid before its audio is even queued), `grade_log_score_threshold`
  (the LOG_GRADE check) and `audit_log_score_threshold` (the AUDIT verdict's
  log-score leg) all ship **100** — a Logchecker-perfect log, by default, and
  `mlo/discs.score_disc_log` is what scores it (Logchecker's own
  `Checksum: checksum_invalid` is a hard refusal, never a low score). The bar
  applies to every candidate the app finds BY ITSELF. The three paths that do
  not gate are the user's own hands, and each says so where it happens: a
  MANUAL entry (a peer folder picked in the browse/manual flow) runs the gate
  only for the logs it selected, so choosing a folder with no log is a choice
  the user made; a CD candidate with no log at all on the searched path is
  re-scored as Digital Media and offered through the `no_logs` confirm prompt
  rather than silently refused; and a folder already on disk (the staging
  imports) is graded after the fact, never refused. Lowering any of the three is
  a settings change, never a default.

### 7.20 The library layout: what it tolerates, and what the optimize pass removes

The canonical library is `<music>/Artists/<Artist>/<Album>/<files>`, and the
layout module's job is to say where a library is not that — then, for what the
folder itself proves, to settle it. ONE scan answers every surface (script 20,
`GET /api/library/layout` for the Optimization panel, and the stored report the
Library page warns from), so their numbers cannot disagree, and ONE apply does
the work (`POST /api/library/layout/apply`, or script 20's own run).

- **R185 — the optimize pass removes excess to the Trash, and re-derives every
  removal at the move.** The scan reports; the apply settles what the folder
  itself proves: a wrong-case name is renamed to `naming_script`'s spelling,
  audio outside any album folder is moved into the album its own tags name, and
  what is EXCESS goes to the app's Trash — a stray file (not audio, artwork or
  a known sidecar: an nfo, a db, a stray text file), a folder inside an album
  that is neither a disc folder nor holding audio, an album folder with no
  audio in it, a foreign folder in the music-folder root holding no audio, an
  album-less artist folder, and the `.mlo_*` leftovers of the old layout.
  NOTHING IS DELETED: every removal is `mlo.paths.trash_path`, carrying the
  origin manifest the Trash page restores from, and `layout_apply` off makes
  the whole pass a report again. Two kinds are never removed — a foreign folder
  that HOLDS AUDIO (nothing can say where its contents belong) and a hidden
  folder inside `Artists/` (a sync client's or a checkout's marker) — and every
  row that is left is reported with the reason it stayed.
  **The reason is asked again at the move** (`mlo.layout._may_trash`), never
  trusted from the report: a row's path must be inside the music folder (never
  that folder itself, never `Artists/`, never `.mlo`), and a folder that gained
  audio between the scan and the apply — or a stray that became a sidecar — is
  refused and reported instead of acted on. The panel's route re-scans first;
  a runner passes the report it just built, which is exactly what that guard is
  for.
  **It runs automatically, and scoped.** Script 20 is in the import chain, and
  `mlo.layout._scope` confines a run handed `targets` to those albums, so an
  import optimizes the one album it just wrote (after beets (14) has put it in
  its canonical place, before the grade (4) reads it) rather than re-walking the
  library once per album. A library-wide *Run All* still gets the whole-folder
  pass and the stored report; a scoped run stores nothing, so the report never
  describes half a library.

### 7.21 The storage card: what it measures, and what it warns about

The card answers "how much disk is behind this install" from one walk per
folder (`server.api_storage.scan`): the library, the app's state, the bin, the
transfers, the app's own tools — with `app_total` the sum of those four app
folders, and the library deliberately not part of it (the music is the user's,
not the app's). Every figure is a number or `null`: a volume the OS will not
measure is never reported as 0, because "0 GB free" and "could not ask" are
different answers.

- **R186 — a link not followed is not a gap, and only a real gap warns.** The
  walk never follows a link (a file reached through one is counted once, where
  it really lives), so a linked file or directory is skipped ON PURPOSE: the
  figures lose nothing, and the card says so quietly — "N links not followed —
  a linked file or folder is counted once, where it really lives". An
  UNREADABLE directory is the other kind, a figure nobody could take, and that
  is the one the card warns about in amber, with the paths in its tooltip. The
  reply carries the split (`skipped_count`, `skipped_links`,
  `skipped_unreadable`, and `kind` on every skipped row), because the
  `reason` strings are the walk's own words and a client must not have to parse
  them to know which kind it is looking at. A healthy install has both, in the
  bundled tools: a `libjpeg.so` version symlink is a link; a toolchain unpacked
  onto a network mount that went away is unreadable.

### 7.22 The phone player: one scroller, a real play state, the favourite's home

The fullscreen player is ONE component on every client, so "the phone" is a set
of decisions inside it rather than a second player: below `lg` the cover, titles
and controls collapse into a header and the lyrics pane takes the rest of the
screen; above `lg` the pane sits beside the artwork.

- **R188 — the element is the truth about what is playing.** `playing` is
  written by the media element's OWN `play`/`pause` events, not only by the
  app's buttons, and the lock screen's `playbackState` follows the same value.
  The bug this settles (owner-reported on iOS): the OS suspends a backgrounded
  webview, the audio stops, and the bar goes on drawing Pause — "the song is
  still 'playing'". A pause the app did not ask for — an interruption, a
  headset button, a decode error, the OS freezing the page — is therefore
  reflected at once, and on the way back in from a lock screen
  (`visibilitychange`, `pageshow`) the store is reconciled against the element
  instead of trusted.
- **R189 — on a phone the lyrics own the screen.** With the lyrics pane open at
  phone widths there is exactly ONE scrolling surface, and it is the pane: the
  cover drops to a thumbnail, the header stops scrolling, and the pane fills the
  height that is left. Nested scrollers, a literal `100vh` cap and a scrolling
  body behind a scrolling pane are what made the view read as broken. In a
  viewport too short for the header (a phone in landscape) the body scrolls
  instead, so nothing is clipped away.
- **R190 — the favourite has exactly one home per width.** Below `lg` it is the
  bottom-left of the player (where Apple Music keeps its own); above `lg` it is
  the transport row's control — never both at once. It is the app's own
  favourite, a heart: a STAR in this app means a rating, which is a different
  store (`lib/ratings`).

### 7.23 The export archive, and the offline shell that must not become it

- **R191 — a download is a navigation, and the service worker must not treat it
  as the app's shell.** `<a download href="/api/export/zip/<id>">` reaches the
  worker with `mode: "navigate"`, and the navigation branch used to take it: it
  fetched the archive, stored it under the SHELL's own URL, and — when that store
  failed, which a large archive being aborted does — answered the download with
  the cached document. The owner's report was exact: "a 2.6 KB invalid .zip",
  which is this app's `index.html` (2,689 bytes) saved under the archive's name,
  with the offline shell left holding a zip. Only a real app route may take that
  branch (`destination === "document"`, and nothing under `/api/`), and the shell
  cache is versioned so an install that was already poisoned drops it on the next
  activation. `tools/check_export_zip.cjs` measures both halves: the bytes the
  browser really saves, and that no cache holds an archive as a document.

### 7.24 What the client calls things

- **R192 — an album's dynamic range is ADR, everywhere it is an album's.** The
  card chip and the library's album column said "DR" while the album page and
  the stats called the same value "ADR"; per-track DR keeps its own name (the
  track tables' `DR` column and the `DYNAMIC RANGE` tag). One value, one word:
  the letters say which tag the number came from, which is exactly the
  distinction a reader needs when both are on screen.
- **R193 — the top bar says who you are, and can switch.** The account control
  is the bar's rightmost item: it names the signed-in user (the server's own
  default scope is named as such, never blank), lists every user the server
  reports, and switching runs the same sign-in the login screen does — token
  kept, then a reload, because every cached query, the player and the event
  socket are keyed on being signed in. A server with no users says so and points
  at Settings → Security rather than showing an empty list. Signing out is
  available from the same panel.

### 7.25 A rejected download explains itself, and the explanation outlives the job

- **R194 — the failure names the peers and the reasons, and the reasons are
  written where the failure points.** A run whose candidates were all refused
  ends in one sentence, and that sentence is what the user keeps: the wish row
  persists it as `last_error`, and `wish_found`/the notification carry it. It
  now carries the attempts themselves — the first three as `peer: reason`,
  counted past three — because the reasons used to live only in the job's own
  in-memory list, which the row's clearing discards, while the sentence told the
  user to go read a LOG the attempts had never been written to ("see the log" was
  a dead pointer, and a release stuck at 0 % explained nothing). `_reject`
  writes each attempt to the wishes log as well, so the pointer is true. With no
  reasons recorded the sentence is **byte-for-byte** the one it always was: this
  grew a field, it did not change a wording.

### 7.26 The cover's play control, and the cascade behind it

- **R195 — the play control is IN the overlay, at every width, and clear of the
  badges.** A cover's play button shares ONE flow column with the badges: the
  ADR row, the button band, then the chips grouped at the bottom — and the column
  clips its own last chip rather than letting anything cover the control. The bug
  this settles (owner's phone screenshot): `.tap-hit`'s `position: relative` was
  an UNLAYERED rule inside the phone media query, so it beat Tailwind's
  `absolute` on the same element — at 390px the button left the overlay entirely
  and landed below its own cover in normal flow (measured: button top 734 against
  a cover box ending at 698), dragging the bottom-anchored chip column down with
  it and putting a two-line country chip over the control. Two rules follow: the
  hit-area position declarations live in `@layer components`, so a positioning
  UTILITY on the same element wins (the rule is about the tap target, not about
  where the element sits), and no overlay places its control with an absolute
  offset. A cover too small for both — the S size, 147px, with a wrapped country
  list — keeps the button whole and clips the chip that does not fit, never the
  other way round.

### 7.27 The queue says what is true, and one press takes a row off it

- **R196 — a row IS the job's own state, field by field, and it says so while it
  waits.** The row's progress block carries what the job is really doing: the
  query being asked and slskd's own state for it (a search is a LIST of queries
  asked one after another, so "which one now, and is it still being asked" is
  the difference between a search that is working and one stuck on a query
  nothing answers), the peer and folder arriving, the phase, `files_arrived`
  counted APART from `files_done` (what the wait has accepted on disk versus
  what slskd calls complete — a row must not claim a file arrived that has not),
  the job's own last log line as `stage_text` (empty once the job has SETTLED: a
  finished job's last line describes a step nobody is running), and the rejected
  candidates with the reason each was refused (bounded to the last eight, so one
  row's payload cannot grow without limit). A row whose step has stopped never
  claims it is still running.
- **R197 — a row still in the pipeline is CANCELLED, not cleared.** One action
  takes ONE item off the list through whichever mechanism owns it — the wish
  list, the running job, or the bulk queue — instead of making the user find the
  page that started it; `clear` stays for rows whose work is over. The bug this
  settles: the client had ONE cancel call but the server branched on the wish's
  own status, so cancelling a wish that had moved past "searching" removed the
  folder and the wish while the download kept running — and a job whose wish is
  gone still draws a row of its own, so it settled later as a failure and left a
  SECOND row needing its own clear. A cancelled row reports that it was
  cancelled rather than looking like a failure of its own.
- **R198 — the landing notice is the download's, and it says so.** "Imported … —
  it is in your library" is emitted when the album LANDS; the import chain starts
  after that (`_start_import_chain`) and runs on its own thread, so the sentence
  used to imply a finished pipeline while the scripts had not run yet. Every
  surface that announces the landing — the app-level toast, the Auto tab's own
  and its panel line — appends `queue.chain_running_brief` while `chain.running`,
  and the queue row keeps the long form. The row's stage already stays
  "importing" until the chain settles; the sentence now agrees with it.

- **R217 — the counters describe the list beside them, and count a peer
  ONCE.** Two surfaces showed numbers that did not belong to the rows under
  them. `soulseek.search_results` derived `responseCount`/`fileCount` from
  slskd's SEARCH STATE, which only settles once a search has ended: a page
  rendering those beside a file list served by `/responses` read "0 peer(s), 0
  file(s)" over a list of hits, and a search ended early by `response_limit`
  never settles at all. They are computed from the response list itself now —
  `len({username})`, `len(responses)` — with the state's own counters only as
  the fallback for the window where slskd has counted responses it will not
  serve yet. `soulseek_auto._search_queries` SUMMED each template's counters
  while asking every template at once: the templates of one album overlap by
  design (catalog number, barcode, title all name the same release), so one peer
  was counted once per template (15 "responses" for three peers) — it counts
  DISTINCT usernames and distinct user+file across the responses in hand.
  `tools/test_soulseek_candidates.py` pins both: two templates returning the
  same peer's same two files read 1 peer / 2 files, and the counters travel with
  the list they describe.

### 7.28 The import arrives complete: the name, the cover, and when the notice may speak

- **R199 — the album folder is named from the FINAL tags, so the last tag writer
  is followed by the namer.** Script 8 (Auto tagging) is the last writer of the
  tags the naming script reads (`mlo.autotag._fill_release_tags`: both DATEs
  sharpened, RELEASECOUNTRY widened, LABEL/CATALOGNUMBER/MEDIA/RELEASETYPE
  filled), while the chain's only full rename is script 14's `organize()`. The
  ORDER is not the fix and must not move: 8 stays after 13 (its INSTRUMENTAL
  stage reads the lyrics 13 stored) and after 14 (on a library-wide run beets is
  what MATCHES and stamps the `MUSICBRAINZ_ALBUMID` that `_fill_release_tags`
  keys on). Script 8 therefore ends by re-applying the naming script to exactly
  the albums whose release tags it filled (`mlo.autotag._rename_to_script`) and
  reports where they are now as `stats["moved_targets"]`, so the rest of the
  chain follows the album (`script_runners._follow_moved_targets`). The rename
  is idempotent, scoped to albums inside the music folder, and never fatal.
  Without it the folder spells the stale tags' answer and script 4 — Grade, last
  — reports every file of an impeccably tagged album as `PATH: expected '<what
  these tags imply> (run organize)'`.
- **R200 — an add-time folder name never outlives the tags.** A framework
  album's name comes from the MusicBrainz PAYLOAD (`pending_albums.create`, the
  provisional guess of `create_from_request`), while every namer in the pipeline
  names the album from the TAGS. `server.main.organize` keeps `adopt_root`'s
  identity-first destination — so the album lands IN the framework folder
  created for its release rather than beside it — and then moves that folder
  onto the path the naming script gives the tags
  (`pending_albums.rename_placeholder`), which is what it does with every album
  it lands (`_adopt_deferred` is the same move at add time). The marker is
  inside the folder and travels with it, so it stays the same framework album,
  and the wish that created it is re-pointed (`album_path`/`target_dir`) because
  that path is what the queue links to and what `_revive` reads. Only a folder
  still carrying its marker is ever renamed, and never onto a name a real album
  already occupies: the album then keeps the folder it landed in rather than
  being merged into someone else's. Chosen over "make the grade tell the user to
  rename": the app owns the naming script, so a name no tag can produce is the
  app's defect, not a task for the user.
- **R201 — an album is never left coverless, and the placeholder cover is not
  one of its own.** The framework album's placeholder (Cover Art Archive's
  release-group front, at the library's own minimum) is a stand-in, not "the
  album already has a cover": the cover step treats it as no cover and fetches
  the release's own artwork over it (`pending_albums.placeholder_cover_present`),
  it is never DELETED on the way in, and it goes only once a real cover has been
  written — under its own name or another extension, so the album never keeps
  two cover files. When nothing clears the cover floor, or the step cannot run
  at all, the placeholder STAYS: `_drop_placeholder_cover` refuses to take the
  folder's LAST cover away, and the step's note says the album keeps the
  framework album's own release-group artwork. The bug this settles: the
  placeholder was deleted before the cover step could say whether it had
  anything better, so an album whose every candidate `mlo.cover_choice`'s
  minimum refused ended with no cover file at all and graded "Missing cover
  image" while an image of that very release sat on disk. **And the step
  FINISHES before the chain starts** — the metadata+cover pair runs on its own
  thread and `_finish_album` waits for it before `run_chain` — which is what
  makes "Process images" (script 5, mid-chain) see the fetched file: a cover
  landing afterwards would sit unprocessed in a library that normalises
  everything else. Pinned in `tools/test_chain_after_acquire.py`.
- **R202 — an import that cannot place every file is reported, not aborted, and
  "Imported" means the pipeline really finished.** A naming-script failure no
  longer raises out of the auto-importer's `_import`: the album is in the
  library, so its cover step, its chain and the rest of the pipeline still run
  and the download is kept (`partial`), with the files that stayed in the
  download folder named in the job's own line — a partial landing is a fact
  about the album, not a reason to lose it. And `import_done` is emitted where
  the pipeline is actually finished, after the gap phase and its prompt
  (`['import_started', 'gaps', 'prompt', 'import_done']` — the order the
  notification seam is asserted to observe), because the notice is what a user
  reads as "done": it may not speak one step early.

### 7.29 Push reaches a client that is not open

- **R203 — push is a real transport, and it may never take the event bus down
  with it.** The server signs with VAPID (RFC 8292, ES256) and encrypts each
  message with RFC 8291 `aes128gcm`; the key pair is generated once and kept in a
  file beside the state (`webpush.json`), **never in the config** — `GET
  /api/config` hands the whole config to every signed-in session, so a private
  key stored there is a credential any client could read. Subscriptions live
  beside `sessions`/`users`, keyed by their endpoint (re-subscribing updates the
  row instead of double-sending), and carry the kinds that device asked for. A
  404 or 410 from the push service deletes the row; anything else leaves it. The
  emit path only QUEUES — no database, no socket, no blocking — and the sender
  never raises: an exploding device leaves the event published, because a
  notification must not fail the import that earned it. Measured: the RFC's own
  Appendix A vector reproduces byte for byte, and the fan-out is asserted to POST
  a payload that decrypts with the device's private key.
- **R204 — a client is told the truth about its own platform.** The switch is
  rendered only where push can actually work (secure context, `PushManager`, a
  registered service worker); everywhere else the panel shows the sentence for
  that platform instead of a control that would fail — the desktop shell
  notifies only while it runs (no service worker there), iPhone and iPad need
  la musica on the Home Screen (iOS 16.4+), and a plain-http page has no push at
  all. A server that cannot sign (no `cryptography`) says so and answers the
  subscribe with 503 rather than half-working.
- **R205 — a subscription belongs to the identity that made it.** Subscribing
  carries the signed-in user, and the row follows whichever user last signed in
  on that browser; signing out or replacing a device's session drops it
  (`revoke_all`, `delete_user`, and the client's own `onAuthLost`), a client can
  only unsubscribe its own endpoint, and the server prunes by itself. A device
  must not keep being woken for an account that left it.

### 7.30 The script chain's wall clock: what is shared, and what is measured

- **R216 — the notification a client missed is STILL THERE when it comes
  back, and a kind's reach is the same on every client.** `?since=` replays from
  two stores: the in-memory ring (`_MAX_EVENTS` 100, R90's channel) and a
  durable log beside the app state (`server/events.py::_log_append`, newest
  `_LOG_KEEP` = 400 frames, atomically rewritten past `_LOG_MAX_BYTES`),
  because a client that is closed for a day — and a desktop or mobile shell in
  particular, which no push service can reach at all (R204's own "notifies only
  while it runs") — otherwise hears nothing about the import that finished
  overnight. `recent()` merges both, dedupes on `seq` and returns the newest
  `limit`; a client that has never seen an event still asks "from now"
  (`eventsUrl` sends `Date.now()/1000`), so a fresh install is not greeted with
  a hundred notices for things that happened before it existed. In the same
  change `OS_KINDS` and `PUSH_KINDS` (`web/src/lib/notifications.ts`) became ONE
  set: an outcome worth interrupting an open app for is worth waking a closed
  one for, and the kinds that were asymmetric are the ones that proved it —
  `import_done` (pushed, never popped), `watch.new_release` and
  `storage_pruned` (neither). `tools/test_notifications.py` asserts the frame
  survives the memory ring, that a client which already saw it is not sent it
  twice, and that the log keeps exactly the newest `_LOG_KEEP` frames. The append and the compaction it may trigger share the module lock: the rewrite is a read-modify-write of the whole file, so a frame appended between its read and its `os.replace` would be rewritten away — silently, and only under concurrent emitters.

- **R206 — a stored audit verdict is trusted for the AUDIO it was written for,
  not for the file's mtime.** Script 6 re-decides nothing it can already prove: a
  file whose evidence record describes what is on disk — its size and mtime, or
  the audio identity a tag write cannot move (the FLAC STREAMINFO MD5 that R23
  and `mlo.discs`' CRC memo already key audio evidence on) — and whose bytes the
  integrity test (`flac -t`, `ffmpeg -f null`) already passed is skipped, verdict
  and all, and the integrity test runs only over the files this run is actually
  going to audit. A record written by a version that had no integrity element, or
  by a run with `audit_integrity` off, settles nothing: that file is verified
  once more and then settles. A container that states no identity (an mp3, a WAV)
  is trusted on its stamp alone, as before, and a record that matches neither
  re-audits rather than guesses. Measured on a 20-track fixture: the second run
  of script 6 drops from 20 `flac -t` decodes + 1 AudioAuditor batch (1.32 s) to
  none at all (0.03 s), with the AUDIT tags and the grade output identical; a
  re-encoded track is audited again, a tag write is not.
- **R207 — one container parse answers both of the audit's questions, through
  the shared tag cache.** MEDIA (is this track a CD?) and AUDIT (does it already
  carry a verdict?) come out of ONE `server.tagcache.read_track(path, ["MEDIA",
  "AUDIT"])` per file — the stat-keyed cache the API serves tracks through, whose
  key carries the mtime, so a tag write re-reads rather than serving what the
  write replaced — and the audit opens a container itself only to WRITE. The
  import is lazy, the `mlo.layout` precedent: `mlo` must not import `server` at
  module level. Measured: 3 container parses per file (MEDIA pass, verdict pass,
  write) become 1 read + 1 write; 60 opens for a 20-track run become 40 on a
  re-run.
- **R208 — a chain's decodes are not shareable, and that is measured, not
  assumed.** Each script that decodes a track decodes it for its own question —
  `flac -t`'s frame CRCs and stream MD5 (3, 6), AudioAuditor's spectral pass (6),
  rsgain's EBU R128 (7), the DR meter's 44.1 kHz per-channel `pcm_f32le` (7),
  librosa's 22.05 kHz mono (12, 16), the decoded-PCM CRC (4, 9) — and no two of
  them want the same artefact, so handing one script another's decode would move
  a number another rule quotes. Measured on one real 5-minute 23 MB FLAC: the
  decodes scripts 12 and 16 pay are 0.306 s and 0.125 s, against 13.5 s (mood
  features) and 11.3 s (BPM + key) of analysis over the same signal — 1-3% —
  while holding one album's samples to hand them from 12 to 16 costs 12 × 5 min
  × 44100 × 4 B ≈ 636 MB of RAM. The decode that WAS repeated for nothing is
  script 6's (R206); the shared-decode cache itself is measured and kept out of
  the tree (`local://issue48-decode-cache.py`).

### 7.31 The unattended acquisition: what may be taken, and when it is asked for

- **R209 — a download queued from the Soulseek page imports itself.** The page's
  three Download routes (`POST /api/soulseek/download`, `-bulk`, `-user`) record
  what they queued (`server.main._remember_page_download`: the peer and the
  remote files, nothing else — a refused enqueue records nothing), and the pass
  that watches them (`_page_download_pass`, its own thread
  `_soulseek_page_downloads_watch`, 5 s) imports the folder those files became
  through `import_queue` — i.e. `_import_one_album` → `imports.finish_album` →
  the configured chain, the very call the Import button makes, so there is no
  second import pipeline to keep in step. Readiness is `soulseek.ready_albums`,
  the ONE rule the Import button works from, so nothing here re-decides what
  "downloaded" means; the remote-file-to-folder mapping is the pipeline's own
  (`_index_download_tree`/`_local_download_candidates`), so slskd's batch layout
  and the older shapes resolve the same way. Only folders holding a file THAT
  press queued are taken, so the auto-importer's own downloads (which import
  under their own job) are untouched. Two switches decide whether it runs at all
  (`mlo.import_policy.page_download_auto_import`): `import_autonomy` "review" and
  `manual_import_enabled` off each leave the album in the download folder with
  its "ready to import" row and say so once — an install that wants to review
  still reviews, and the press that imports it is the manual route, which keeps
  working. `auto_acquisition_enabled` is deliberately not asked: the download was
  the user's own action. The notification sequence is the pipeline's own —
  `import_started`, `import_done`, then `import_queue`'s `download_done`.
- **R210 — a background acquisition and a lossy-only album: the policy decides,
  and either answer is said out loud.** `soulseek_auto_lossy_policy` ("never" —
  the shipped default, and what every config written before the key existed
  reads as; "best") is read only by the UNATTENDED path (`_run` with
  `confirm_lossy` False: the wishes worker, the artist watch), which used to end
  in a flat refusal — with it, a release that exists only as MP3 could never be
  filled by either. "never" leaves that sentence untouched ("a lossless copy is
  preferred, so nothing was downloaded", the line the wish row carries); "best"
  takes the top-ranked candidate the ranking ALREADY offers (`candidates` is
  `_rank`-sorted), and the departure is reported everywhere the job is: the log,
  `result["lossy"]`, the completed queue row ("Imported into the library — a
  lossy copy (MP3)") and `_notify_finish`'s body, which names the format and the
  key. The INTERACTIVE path is unmoved: a person who asked for the release by
  hand parks on the same question whatever the key says, so the key can never
  overrule an answer given by hand. The same rule covers the other departure: a
  completed job whose naming script could not move every file says where the
  files are ("the naming script could not move every file, and what stayed
  behind is still in the download folder"), with `partial`/`organize_error` on
  the row — the old line named only the script and read as "the album is fine, a
  script grumbled".
- **R211 — a settle that is not a failure is scheduled by the interval, not by a
  spent backoff.** `retry_at` is the transient-failure backoff and nothing else:
  `wishes.due_at` reads its mere PRESENCE as "the last attempt failed" and
  returns it in preference to `wishes_interval_hours`. The two settles that have
  no backoff to give — an empty search and a spent walk
  (`wishes_worker._settle_attempt`'s not-found branch and `mark_background`) —
  pass none, and `wishes.mark_wanted` USED to skip the column when the argument
  was None, so a stamp an earlier failure left behind (in the past by then)
  survived: `due_at` returned it as "due now" on every tick and the wish was
  re-searched every ~2 minutes instead of on its interval — the opposite of what
  the function documents and of what `advance_candidate`/`rearm` do.
  `mark_wanted` now always writes the column (its default 0 is "as soon as the
  interval allows", exactly as documented) and `mark_background` writes it too,
  so an empty search and a background pass both go back on the interval.
  `tools/test_wishes_pipeline.py` asserts both, and fails (due_at in the past)
  with the old guard.

### 7.32 The sharing card tracks the scan it is reporting

- **R212 — a scan in progress is polled until it settles, and a settled answer
  is re-checked at the page's own cadence.** The card's own sentence is the
  server's audit (`slskd is indexing the shared folders (x% done)` /
  `slskd is sharing N files in M folders`), so its truth is only as good as how
  often it is asked for. The bug this settles (the owner's screenshot): the
  shares query had **no refresh of any kind**, so the single fetch a rescan's
  invalidation caused landed while slskd had indexed a fraction of the folders —
  the card then read "…(0.0% done)" for the rest of the session while the log
  lines printed under it went on to "Scanned 100% … Found 88 files". It now asks
  every 1.5 s while `scan.scanning`/`scan.pending` and every 15 s otherwise (the
  cadence the rest of that page uses), so a scan started behind the card's back
  — a save in another tab, slskd restarting, the share watcher — is noticed too.
  Verified against a slskd reporting a live scan: the card went from "indexing
  … (99.7% done)" to "sharing 88 files in 6 folders" with no user action, six
  fetches in 45 seconds.
- **R213 — the port probe answers on a network with no UPnP device.**
  `mlo.portmap.read_port` called `pmp_external_address(gw, …, pmp_port=…)` — a
  keyword that function does not take — so the read raised `TypeError` and
  `GET /api/soulseek/port-check` (the Soulseek tab's **Test port**, and the
  endpoint `README.md` sends a user to when a peer cannot reach the share)
  answered **500 on every install**, gateway or no gateway — precisely at the
  moment it is the only tool for the job. The call passes the parameter the
  function declares, and the no-device path is pinned against the REAL discovery
  and the REAL NAT-PMP client, nothing stubbed: the suite's other cases replace
  `portmap.read_port` wholesale, which is how a broken read shipped in the first
  place.

## 8. Recommended runbook

Nothing here is a substitute for the app's own Dependencies page: run it first
and install what the platform supports.

1. **Before touching anything** — set the music folder, then script **20 (Optimize
   library layout)** and script **4 (Grade)**. The grade is read-only and tells
   you what is missing; script 20 walks the canonical
   `<music>/Artists/<Artist>/<Album>/…` shape and, with `layout_apply` (ON),
   settles what the folder itself proves — a wrong-case name is renamed, audio
   outside any album folder is moved into the one its own tags name, and what is
   excess goes to the Trash (stray files, folders inside an album that hold no
   audio, empty album folders, foreign root folders holding no audio, album-less
   artist folders, the `.mlo_*` leftovers) — while a foreign folder that HOLDS
   AUDIO and a hidden folder inside `Artists/` are reported and left alone.
   Nothing is deleted: the Trash lists every removal and can put it back (R185).
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
- **Background playback is a platform declaration, and only iOS's is asserted in
  CI.** `UIBackgroundModes: audio` is what lets iOS keep playing once the app
  leaves the foreground, and the mobile workflow now reads that key (and the ATS
  web-content exemption) out of the built `.app`'s Info.plist, so a merge that
  stopped happening cannot ship unnoticed. Android has no equivalent switch: a
  WebView keeps playing while the process lives, and the OS may reclaim a
  backgrounded app. The app declares no foreground playback service, so "keeps
  playing with the screen off" is not a promise Android makes here — what the
  app controls on every platform is that its own state is honest about what the
  element is doing (R188), and that is enforced in the client.
- **Push is Web Push, and only Web Push.** The server signs with VAPID and
  encrypts per RFC 8291 (R203), and the browser, the installed PWA and the
  desktop shell's own window all receive it — but nothing here speaks APNs or
  FCM, so the **Tauri iOS and Android apps cannot be woken while they are
  closed**: on those, push requires la musica added to the Home Screen as a PWA
  (iOS 16.4+), and the panel says exactly that rather than offering a switch
  (R204). A native bridge is a separate piece of work, not a setting. Delivery
  through a real push service is what *Send a test notification* is for: the
  suites stub the service's HTTP layer (the payload is decrypted with the
  device's own private key, and RFC 8291's Appendix A vector is reproduced byte
  for byte), which is everything short of dialling Mozilla's or Apple's endpoint
  from a test run.
- **The storage walk counts DIRENT NAMES, not blocks.** A hard link made by
  hand inside the library is a second real file to `os.scandir`, so the card
  counts it twice; a symlink or junction is not followed at all, and is
  reported as a link rather than a gap (R186). Nothing in the app creates a
  hard link — the only links a healthy install has are the bundled tools' own
  version symlinks.
- Detection is heuristic where the evidence is: AudioAuditor's spectral
  detectors can disagree with a provably intact rip, which is why a verified CD
  rip outranks them (R21) and why `AUDIOAUDITOR_OVERRIDE` exists (R25).
- **A ranked walk is bounded, and a spent one WAITS rather than giving up**
  (R150/R153/R214): after `soulseek_fallback_candidates` editions have been asked
  and none answered — or after every edition answered with copies the pipeline
  REFUSED — the release keeps its place in Background and is searched again
  on the worker's ticks. A release nobody on the network shares therefore stays
  there until the user removes it — the app does not stop trying on its own, and
  it does not pretend the album arrived.
- **A lyrics-absent `INSTRUMENTAL` is the app's own conclusion, not a source's
  claim** (R162). The tag records what was actually done (every configured
  provider was asked and none had the track) under its own evidence key, so it
  is auditable and reversible — but a vocal track whose lyrics exist nowhere the
  app can reach would be tagged instrumental wrongly, which is the trade the
  owner asked for against parking the album on a person.
- **A warning can be dismissed, and a dismissed gap is silent** (R166). *Mark
  complete* takes the album's gap off every surface — that is the point of the
  button (the album is fine as it is) — and the next import of that album is
  what asks again. Nothing re-checks a dismissed gap on its own, so an album
  short of a cover stays short of it until you import it again or fill the
  family in; the grading line it earns stays visible on the album page.
- **The player's equalizer is the BROWSER's biquads, not Equalizer APO's own
  engine** (R219): a peaking band, a pass and a notch are rendered the way an
  export's ffmpeg chain renders them, so the frequencies, the gains and the band
  order agree between the app and an exported copy — but a SHELF's width does
  not (APO's custom slope reaches ffmpeg as a Q, and a WebAudio shelf is
  fixed-slope), and any line the parser reports as unsupported (`Include:`,
  `Convolution:`, an unknown construct) is reported and NOT applied, in playback
  exactly as in an export.
- **A notification a client missed survives a restart, but not for ever**
  (R216): the durable log keeps the newest 400 frames, so a device that was
  away longer than that (or that is reopened after a long absence) sees the most
  recent notices and not the whole history — the tray keeps its own newest 50
  anyway, and push (R203) is the transport for waking a device while it is
  closed.
- **A stored language is sticky until someone edits it** (R167). The `LANGUAGE`
  tag is what the transforms are decided from, so a wrong value there (the
  model's answer for one track of a mixed album, MusicBrainz's own
  `text-representation` for a release whose lyrics are not in that language)
  decides wrongly and keeps deciding wrongly — the app never overwrites a tag a
  source stated, and the tag is the user's to correct in the tag editor. Two
  Latin-script languages are also beyond the app's own evidence on purpose: with
  no tag and no AI, an undecided text is treated as the reader's own, because a
  guess would translate (or skip) a track on nothing.
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
