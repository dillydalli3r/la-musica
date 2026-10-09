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
| Script ids, titles, order | `mlo/scripts.py` `SCRIPTS`, `server/script_runners.py` `RUNNERS`, `mlo/config.py` `DEFAULT_RUN_ALL_ORDER`, `server/imports.py` `DEFAULT_CHAIN` |
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
- **R7 — artist folders have their own grade.** `grade_artist()` fails an
  artist folder that holds NO album folder at all (`ARTIST_EMPTY`): nothing of
  theirs is here, so it cannot pass as one. An artist that DOES hold an album
  reports 100 % and `pass: true` (nothing graded is nothing failed). An absent
  artist folder is `ARTIST_FOLDER_MISSING`.

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
| `TAGS`, `COMMENT`, `LINK` | excess tags: a tag name outside the vocabulary (`TAGS`), a `COMMENT` carrying a value, a tag value naming an external link (`LINK` — a bare MusicBrainz id/UUID is not a link) |
| `PATH`, `PATH_CASE` | naming-script mismatch / case-only mismatch |
| `LYRICS` | lyrics missing, wrongly formatted, or present on an instrumental |
| `XLIT_MISSING`, `XLIT_UNNEEDED` | a needed transform is absent / an unneeded one is stored |
| `MB_LINK` | a required identity link is missing |
| `COVER` | cover missing or failing the size/square rules |
| `CRC`, `CRC_MISMATCH` | a track is not covered by its disc's `.log` CRC / its CRC does not match |
| `CD_FORMAT` | a CD track is not 16-bit/44.1 kHz FLAC |
| `LOG_CHECKSUM` | the rip log's EAC SHA256 does not verify |
| `AUDIT` | the audit tag is missing or not REAL (with `grade_check_audit` on) |
| `EMPTY_FOLDER`, `EXPECTED_TRACKS_MISSING`, `EXPECTED_TRACKS_INCOMPLETE` | folder/release-level failures (`INCOMPLETE`: the album holds part of its recorded tracklist) |
| `ARTIST_FOLDER_MISSING`, `ARTIST_EMPTY` | artist-folder failures. `ARTIST_EMPTY` is an artist folder holding NO album folder at all — the artist is not in the library, so the folder is not a graded artist |

---

## 2. The 21 optimization scripts

Ids, titles and the shipped order are `mlo/scripts.py:SCRIPTS` and
`mlo/config.py:DEFAULT_RUN_ALL_ORDER`; the runners are
`server/script_runners.py:RUNNERS` (that table is what `/api/run` and the import
chain both call).

**R8 — Run All runs `run_all_order`**, shipped as
`[11, 3, 14, 15, 2, 1, 13, 17, 8, 5, 6, 7, 9, 12, 16, 10, 23, 20, 21, 4]`:
everything that moves a file first, everything that reads it last. A
saved order is honoured as saved (ids outside 1–23 are dropped; legacy 8/9-id
orders are migrated).
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
runs this chain and no wider one** — the bulk queue and
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
analysed file is scanned, whether or not the write changed anything) follows the
same rule, and **a missing tool is
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
| 8 | Auto tagging | `ITUNESADVISORY`, `INSTRUMENTAL`, `MOOD`, `ENERGY`, `GENRE`, plus empty MusicBrainz identity/date completion | those tags | no | optional (advisory/genre providers) |
| 9 | AccurateRip | CUETools `.accurip` generation and verification; an existing file is regenerated only when a track's **audio** changed (each track's FLAC audio-md5, recorded per `.accurip` — a tag write no longer looks like a re-rip), and a PARTIAL album's file is left alone (R248) | writes `CD-N.accurip` | no | **yes** (AccurateRip DB) |
| 10 | Format all | Final canonical pass: `.accurip`/`.cue`/`.lrc`/tag trim, the canonical tag-value spelling (`mlo/tagtext.py`) + embedded-cover policy | tags, sidecars, embedded art | **yes** (strips tags outside the allowlist) | no |
| 11 | Remux videos (MKV) | Any video container → MKV, video copied bit-exact when possible, audio to FLAC, chapters kept | video files | **yes** when `video_remove_original` (ON) | no |
| 12 | Key & BPM | librosa key/tempo analysis (every file it analyses is counted as scanned, changed or not) | `INITIALKEY`, `BPM` | no | no |
| 13 | Fetch lyrics | The configured synced-lyrics chain into `lyrics_format` | `LYRICS`/`UNSYNCEDLYRICS`, `.lrc` | no | **yes** |
| 14 | Beets tagging | Managed beets import with the naming script, work/movement tags | identity/release tags, file paths | **yes** (moves/renames, overwrites identity tags) | **yes** (MusicBrainz) |
| 15 | Release tracklist | Writes `.mlo_expected.json` from the release's own tracklist | adds a manifest file | no | **yes** (MusicBrainz) |
| 16 | Mood & Energy | The mood classifier alone | `MOOD`, `ENERGY` | no | no |
| 17 | Lyrics transliterate (AI) | Romanization/translation tags and sidecars, re-synced at `lrc_sync_level`; the per-track work runs through the worker pool (one track's chunk requests used to be paid one after another) | `TRANSLITERATION-*`, `TRANSLATION-*`, sidecars | no | **yes** (configured AI endpoint) |
| 20 | Optimize library layout | The music folder's shape against `<music>/Artists/<Artist>/<Album>/…`: audio at the root or in an artist folder, stray files, unexpected folders, empty albums, `wrong_case` rows. With `layout_apply` (ON) it SETTLES what the folder itself proves — a wrong-case name is renamed, audio outside an album folder is moved into the one its tags name, and what is excess goes to the Trash (a stray file, a folder inside an album that is neither a disc folder nor holds audio, an album folder with no audio, a foreign root folder holding no audio, an album-less artist folder, the `.mlo_*` leftovers) — and reports every other row with the reason it stayed, re-derived at the move (R185). Writes ONE report describing the whole library (plus a `fixes` list) to `<music>/.mlo/data/`, which the Library page warns from; scoped to `targets` when a run names them, and library-wide when it does not (R9) | one report file + the renamed/moved/removed paths | `layout_apply` (removals go to the Trash) | no |
| 21 | Fix AcoustID pairs | Completes (or CREATES) a track's `ACOUSTID_ID`/`ACOUSTID_FINGERPRINT` pair — the failures `Missing ACOUSTID_ID and ACOUSTID_FINGERPRINT` and `Missing ACOUSTID_ID` / `Missing ACOUSTID_FINGERPRINT` (all `(run Fix AcoustID pairs)`). The recording the pair must name is a question the FILE answers itself (its own `ACOUSTID_ID`, its `MUSICBRAINZ_TRACKID`, or the recording MBID this app's naming script wrote into the file name), and the fingerprint is taken from the audio locally by fpcalc — so a CD rip AcoustID has never seen, or a run with no usable key, is repairable with no request at all. The service is asked only for a half pair whose file names no recording anywhere; a file carrying no AcoustID tag and naming no recording is skipped, never written from a guess | `ACOUSTID_ID`, `ACOUSTID_FINGERPRINT` | no | only for a half pair that names no recording |
| 22 | Submit fingerprints (AcoustID) | Gives AcoustID the fingerprint and the MusicBrainz recording id a file already states (`mlo.acoustid.submit_files`): the recording is read the way script 21 reads it, the fingerprint is taken locally, the service is asked what it already links (`pair_known`) and what this app already handed over (`load_submissions`), and only what is genuinely new goes in ONE batched `v2/submit` — each track reported ACCEPTED, ALREADY_KNOWN, REJECTED or skipped with a named cause. **Not in the shipped order** (`OPT_IN_SCRIPTS`): a submission is a public, outward-facing write, so it runs only when someone asks — a details menu, `POST /api/import/acoustid/submit`, the wizard's AcoustID step, or a `run_all_order` the user put it in themselves | nothing locally | no | **yes** (AcoustID database) |
| 23 | Optimize tags | The tag strip script 10 already performs as a step of its own pass, on its own and scoped (`mlo/taghygiene.py`): every tag the grader calls excess — a name outside the shared vocabulary, a `COMMENT` carrying a value, an alias nothing needs (R16a/R16b: the name is one the locale reads, the spelling is for another locale, a second spelling of the same alias, or the value is the name itself) | tags | **yes** (deletes the excess tags) | no |

**R343 — Optimize tags (23) is the excess-tag strip on its own: scoped, and it
only deletes.** The list it deletes is the grader's own —
`mlo.format_all.excess_tags`, built from `mlo.grader.tag_key_allowed` (names
outside the shared vocabulary: TAG_MAP, the encoder identity tags,
beets/Picard's spellings, the app's `AUDIOAUDITOR_OVERRIDE`), `tag_value_excess`
(a `COMMENT` carrying a value) and `alias_file_excess` (the alias family's own
excess, R16a/R16b) — and the deletion is `mlo.format_all.strip_excess_tags`, the
ONE stripper script 10's tag pass calls too. Two callers, one predicate: a
scoped hygiene run can never leave a tag the grade flags, nor delete one it
requires, and the two passes cannot drift apart about what "excess" means. A
write is a change: the file's own tags are compared before and after the
deletes, and a file with nothing excess is not written at all (`delete_tag` is
what dirties the container, and the flush of a clean handle is a no-op), so a
run over a clean library touches no file's bytes — `tools/test_tag_hygiene.py`
asserts that with mtimes — and a run reports its counts the way R10a requires
(`scanned == modified + skipped + errors`, plus `tags_removed`; a write that
fails is reported with its file). Gated by `strip_unknown_tags` (R12). It has NO
force flag (R11): its subject IS the excess tag, so a file that carries none is
deliberately left alone on every run — the same reasoning as script 21's pair.
It is FILE-scoped (`server/script_menu.py`, `mlo.stats._collect_targets`), which
is what puts "23 · Optimize tags" in an album's menu and in a track row's: one
album's junk tags are cleared without a lossless re-encode (3) or a
whole-library format pass (10), which is why the excess and alias failures now
name it FIRST in their own instruction ("run Optimize tags (script 23) on the
album, or …", `mlo/grader.py`). Every library item's flyout therefore offers it
— the registry-generated menus an album, a track row and an artist
mount (`web/src/lib/scriptMenu.ts` over `GET /api/script-menu`), the library
page's selection dropdown, AND the album page's hand-written "All album
actions" flyout, which lists a curated subset of scripts and is the one place
that had to be told: its "Tags & scripts" section ran 1/2/3/5/6/7/8/4 and now
carries 23 beside them, so the entry point the grade failures name is in the
menu a reader who just saw that failure opens. It writes files and nothing
else, and drops the tag cache of exactly the folders it rewrote
(`server.tagcache.invalidate_album`,
never `invalidate_all` — the scoped drop `/api/run` and an import already make
for the folders a run names).

**R358 — a stored tag VALUE naming an external link is excess.** The app never
keeps a URL in an audio tag: `mlo.grader.tag_value_excess_reason` answers
`"link"` for any value matching `_URL_RE`, the excess-tag grade fails the track
with its own issue code **`LINK`**, and the strip passes DELETE the tag — Format
all (10), Optimize FLACs (3) and Optimize tags (23) all read the one predicate
(`mlo.grader.tag_value_excess` via `mlo.format_all.excess_tags`), so a strip can
never leave a value the grade flags, nor delete one it requires. A bare
MusicBrainz id is an IDENTITY, not a link: a UUID does not match `_URL_RE`, so
`MUSICBRAINZ_ALBUMID` and friends stay fine and the identity-link requirement
(`grade_check_mb_links`, `MB_LINK`) still demands one — nothing writes a URL
into a tag, so the grade never requires a URL. This is the general rule the
former RateYourMusic link tags fell under: the app no longer writes
`RATEYOURMUSIC_ALBUM` / `RATEYOURMUSIC_TRACK` / `RATEYOURMUSIC_ARTIST` (their
names left the tag vocabulary, `mlo.audio.TAG_MAP`, so any such tag is now
excess by NAME too), and `grade_check_rym_links` / `RYM_LINK` are gone with
them. The RateYourMusic **scraper** survives for what it is good at — genres,
the cookie (`rym_cookie`), the archive fallback and the link RESOLVER
(`server.integrations.rym_links`, used by the Sources probe) — none of which
writes a tag.

**R243 — Fix AcoustID pairs (21) completes OR creates the pair from the file
itself, and only asks the service for a half pair that names no recording.**
The recording the pair must name is resolved in ONE order
(`mlo/acoustid.py::_recording_identity`, `mlo/acoustid.py:1165`): the file's own
`ACOUSTID_ID`, then `MUSICBRAINZ_TRACKID` — the recording tag this app's own
imports and the beets/naming path write — then, for a name the naming script
wrote, the ONE bracketed UUID (`_UUID_RE`) that none of the file's other
identity tags claims (`_NAME_OTHER_ID_TAGS`: the album, release-group,
release-track, artist and album-artist ids are casefolded in and subtracted, so
the release group's own id in the same name is never mistaken for the
recording). `fix_pair` then completes or creates the pair: an id found that way
is paired with a fingerprint taken from the audio locally
(`fingerprint()`/`fpcalc`), a file that states only a FINGERPRINT is given the
id its own `lookup()` matched the recording FROM (so both halves describe the
same fingerprint), and a file that names no recording at all is skipped with
that reason — the network is never asked to identify a whole library by name.
`write_tags` still refuses a lone `ACOUSTID_ID` (NO_FINGERPRINT); the runner
(`run_fix_pairs`) counts `modified`/`unchanged`/`skipped`/`failed` per file. No
force flag exists, and none is wanted: its subject IS the incomplete pair. The
grader's `Missing ACOUSTID_ID and ACOUSTID_FINGERPRINT (run Fix AcoustID pairs)` is therefore repairable
for the file whose NAME already stated its recording — the case that made the
instruction unactionable — and the script is runnable for a track, an album or
any selection through `POST /api/run {ids: [21], targets: […]}` (a FILE target
is collected by `mlo.stats._collect_targets`). Pinned by
`tools/test_acoustid_integrity.py`.

**R11 — force flags are the only way to redo work.** Each script has one, and it
is what makes the script look at a file it has already processed:
`force_lyrics` (1), `force_cue` (2), `force_reencode_flac` (3), `force_reencode_images`
(5), `force_audit` (6), `force_dr_replaygain` (7), `force_auto_tag` (8),
`force_accurip` (9), `force_audiometa` (12), `force_mood` (16), `force_xlit` (17),
`force_tracklist` (15). Grade (4) needs none — it re-reads.
Fetch lyrics (13) has NO flag since v4.4.0: a run fills what is missing and
never replaces stored words, so there is no "redo" for it to force (R330) —
replacing one track's lyrics is the manual route's job.
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
bulk queue — passes `force=None`, so the saved
switches apply, while `{}` would clear them all (`layout_apply` included, which
left an import's layout pass a read-only report on the album it had just
imported).
**R12 — a switched-off feature skips its script** instead of running it as a
no-op: `dr_replaygain_enabled` (7), `audiometa_enabled` (12), `mood_enabled` (16),
`lyrics_xlit_enabled` / `lyrics_translate_enabled` (17), `acoustid_enabled` (21 — the same switch the AcoustID lookup itself
refuses on, so a run says WHY it did nothing instead of reporting an empty
pass), `strip_unknown_tags` (23 — with that switch off nothing in this app is
excess, since the excess-tag grade and script 10's strip read it too, so the
hygiene pass would have no subject). Scripts 9/10/11/12/13/16/17 whose module is missing are reported
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
(Settings → General → "Worker threads", 0 = every core the machine has) bounds
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

**R80b — the desktop app updates itself, and only from a signed release.** The
shell asks the release's own manifest
(`releases/latest/download/latest.json` — `plugins.updater` in
`desktop/src-tauri/tauri.conf.json`) whether a newer build exists, and Settings →
Security offers to install it: the download is verified against the public key
baked into the app, and only then does the installer run — silently on Windows,
which then starts the new build, and in place everywhere else, where the shell
re-execs itself. Nothing else may install an update (no unsigned download, no
"latest" guess): this is the one path that replaces the binary a user launched,
and the signature is what makes "install it for me" safe to offer. The version it
compares is the SHELL's own, because a server it happens to talk to may be a
different install at a different version whose number says nothing about the app
on this machine. A browser client is served by whichever server it connects to
and that image updates itself (R80), so it is not offered this — the notice it
gets is a link, not an installer.

---

## 3. Grading checks

67 keys exist; **every one of them ships ON**, checks and file categories
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
| `grade_check_album_tags` | Album-level tags | ON | `ALBUM DYNAMIC RANGE` is present |
| `grade_check_mood` | Mood tag present | ON | `MOOD` exists (`MOOD_MISSING`) |
| `grade_check_energy` | Energy tag present | ON | `ENERGY` (0-100) exists (`ENERGY_MISSING`) |
| `grade_check_genre` | Genre tag present | ON | `GENRE` exists (`GENRE_MISSING`) |
| `grade_check_genre_count` | Genre count per track | ON | at most `mb_genre_count` genres (default 2, max 3) — a ceiling, never a quota (`GENRE_COUNT`) |
| `grade_check_genre_order` | Genre order (family first) | ON | the family slot, if present, is FIRST and no genre repeats (`GENRE_ORDER`) |
| `grade_check_genre_vocab` | Genre vocabulary | ON | every name is one MusicBrainz publishes (`GENRE_VOCAB`); grading never rewrites the tag |
| `grade_check_replaygain` | ReplayGain tags present | ON | opt-in per file: any `REPLAYGAIN_*` tag means all four must exist |
| `grade_check_acoustid` | AcoustID tags required | ON | every audio track carries the `ACOUSTID_ID` + `ACOUSTID_FINGERPRINT` pair — neither half stored fails naming both, a half pair fails naming the missing one (`ACOUSTID_ID` / `ACOUSTID_FINGERPRINT`). `acoustid_api_key` is NOT needed (script 21 takes the fingerprint locally with fpcalc and reads the recording id off the file); the check stands down while `acoustid_enabled` is off, since then script 21 does nothing |
| `grade_check_alias_needed` | Locale alias for names the locale cannot read | ON | a `TITLE` / `ARTIST` / `ALBUM` written in a script the configured `locale` does not read needs its alias tag — `TITLEALIAS` / `ARTISTALIAS` / `ALBUMALIAS`, in a spelling the app writes (`mlo.audio.alias_spelling_ok`: the bare tag, or the configured locale's own suffix). The rule is `server.integrations.alias_required` (the SAME answer the writers, the import's stamp and script 8's prescan ask), built on `mlo.lyrics_xlit`'s script reading (`non_latin_ratio` ≥ `_LATIN_THRESHOLD` and `dominant_script` ≠ latin) plus the locale's own script, so a Latin name — and a name in the locale's own script — is never graded or counted |
| `grade_check_alias_excess` | Locale alias only where needed | ON | an alias tag NOTHING needs fails: a name the configured locale already reads carrying one (`Radiohead` with an `ARTISTALIAS`), a spelling for a locale the app does not write (`TITLEALIAS-JA` in an `en` library), a second spelling of the same alias, or a value that is the name itself (`X (X)`). Predicate `mlo.grader.alias_keys_excess` — the same one Format all (10) and Optimize tags (23) delete by |
| `grade_check_encoder` | Encoder identity | ON | the `ENCODER_*` markers switched on in `encoder_tags` are present (covers included while `reencode_images` is on) |
| `grade_check_naming` | Naming script match | ON | the full relative path equals the evaluated `naming_script`; full and 8-char MBIDs both accepted (`PATH`) |
| `grade_check_filename_case` | Path capitalization | ON | letter case matches the script exactly (`PATH_CASE`) |
| `grade_check_ext_case` | Lowercase extensions | ON | no `.FLAC`-style extension in the folder |
| `grade_check_key_bpm` | Key & BPM | ON | `INITIALKEY` (in `audiometa_key_notation`) and `BPM` exist |
| `grade_check_excess_tags` | Excess tags | ON | no tag outside `mlo.grader.TAG_ALLOWLIST` (`TAGS`), and no VALUE in the one allow-listed name nothing here ever writes — a non-empty `COMMENT` fails with its own code (`COMMENT`, value rule `mlo.grader.tag_value_excess`); gated as well on `strip_unknown_tags`, which the strip passes (3, 10, 23) follow |
| `grade_check_media` | Media type | ON | `MEDIA` exists, is in `KNOWN_MEDIA`, and is uniform across the album (`MEDIA`) |
| `grade_check_source` | Source tag | ON | `MEDIA=digital media` requires a non-empty, uniform `SOURCE`; any other medium must NOT carry one (`SOURCE`) |
| `grade_check_instrumental` | Instrumental consistency | ON | `INSTRUMENTAL=1` tracks carry no lyrics; `INSTRUMENTAL=0` tracks are graded for lyrics. An `INSTRUMENTAL=1` track is charged nothing by the lyrics presence/format checks — script 1 clears the leftover both stores (R318) — while this check keeps naming the contradiction until it does |
| `grade_check_disallowed` | Disallowed file types | ON | no file whose category is switched off in `grade_include_*` |
| `grade_check_extra_images` | Stray images | ON | no image that is neither `cover.*` nor a per-track sidecar |
| `grade_check_empty_folders` | Empty folders | ON | no audio-less folder (`EMPTY_FOLDER`) |
| `grade_check_expected_tracks` | Whole release present | ON | an album carrying a MusicBrainz release id has a non-empty `.mlo_expected.json` (`EXPECTED_TRACKS_MISSING`), **and** every row of a manifest the album does carry is on disk (`EXPECTED_TRACKS_INCOMPLETE` — 14 of a CD's 15 tracks fails) |
| `grade_check_raw_video` | Raw videos | ON | no un-remuxed video container (`.vob`/`.avi`/`.wmv`/`.ts`…) |
| `grade_check_lossless_source` | Lossless sources | ON | no uncompressed lossless source (`.wav`/`.aif`/`.aiff`/`.ape`/`.wv`/`.shn`/`.tta`) is left in the library. The check **stands down** (adds no check at all) when the conversion pass would never touch one: the target is itself one of those containers (`library_codec` = `wav`/`aiff`) or nothing is converted (`library_codec`/`library_codec_optimize` = `keep`). The issue names the target: *"… (script 3 converts them to FLAC)"* |
| `grade_check_disc_naming` | Disc rip-sheet naming | ON | a CD's `.log`/`.cue`/`.accurip` follow `discs_rename_pattern` (`CD-{n}`) |
| `grade_check_cd_log` | CD — .log present | ON | every CD disc has an exact-match, non-empty `.log` |
| `grade_check_cd_cue` | CD — .cue present | ON | every CD disc has a `.cue` |
| `grade_check_cd_format` | CD — lossless format | ON | a CD track is 16-bit/44.1 kHz (`CD_FORMAT`; FLAC is what the shipped target produces). A file that **is** the configured *lossy* target is exempt and not counted — the CD-DA stream is gone once the album was deliberately converted, and Opus resamples to 48 kHz by design |
| `grade_check_crc` | CRC checksums | ON | every track is covered by a per-track CRC in its **own disc's** `.log` (`CRC`) and that CRC equals the decoded PCM's CRC-32 (`CRC_MISMATCH`); lossy or undecodable files are judged on coverage alone |

### Auditing

| Check id | Label | Default | Asserts |
| --- | --- | --- | --- |
| `grade_check_audit` | Require audit tag | ON | the track's audit verdict is REAL — missing or non-REAL fails (`AUDIT`). It ships **on** now: with the CD verdict decided by the rip's own evidence (§5, R21) an unaudited library is a library nobody has checked, which is the thing this check exists to say |
| `grade_check_log_checksum` | Log checksum valid | ON | a log checksum that IS PRESENT must verify (`LOG_CHECKSUM`); one that is ABSENT is not required and costs nothing — XLD, EAC before v1.0 and a 1.0+ log whose `Log checksum` line is gone are judged by their per-track CRCs alone (R30) |
| `grade_check_accuraterip` | AccurateRip verified (audit only) | ON | a `.accurip` whose verdict is not REAL marks the album's audit FAKE (and the track red). Together with `audit_require_accuraterip` it is what can turn the audit verdict FAKE; **it never adds a grade point** |
| `grade_check_flac_md5` | FLAC stream MD5 (STREAMINFO) | ON | a FLAC's own decoded-audio digest is verified against the audio (`FLAC_MD5` / `FLAC_MD5_ABSENT` / `FLAC_MD5_UNKNOWN`) |
| `grade_check_log_grade` | Log grade present & in range | ON | `LOG_GRADE` exists, is an integer 0-100 and is at least `grade_log_score_threshold` (default 100; 0 disables the threshold) |

### Identity links, covers, formatting, lyrics, categories

| Check id | Label | Default | Asserts |
| --- | --- | --- | --- |
| `grade_check_mb_links` | MusicBrainz release link | ON | `MUSICBRAINZ_ALBUMID` (or a release-group id) is tagged (`MB_LINK`) |
| `grade_check_cover` | Cover art | ON | the album has a cover (`cover.jpg`/`jpeg`/`png`/`jxl`) meeting the size rules (`COVER`) |
| `grade_check_cover_crop` | Cover aspect ratio (squareness) | ON | `|w/h − 1| ≤ cover_crop_threshold` (an aspect test, not crop detection) |
| `grade_check_sidecar_cover` | Per-track sidecar covers | ON | per-track covers meet the same rules |
| `grade_check_tag_spaces` | Tags — no padding | ON | no leading/trailing space or tab, and no run of 2+ internal spaces, in a single-line tag value (a value carrying a newline is never judged — its whitespace is text) |
| `grade_check_tag_case` | Tags — canonical value case | ON | `MEDIA`, `SOURCE`, `RELEASETYPE`, `RELEASESTATUS`, `AUDIT`, `RELEASECOUNTRY`, `SCRIPT` and `MOOD` hold the canonical spelling `mlo/tagtext.py` writes (`TAG_CASE`), and every name in `GENRE` holds the form every genre writer ends on — `mlo/genres.py::display_name` of the name's canonical spelling (`GENRE_CASE`, e.g. `metal` → `Metal`). GENRE is deliberately NOT in `CANONICAL_CASE`: it is an open, multi-value tag whose canonical form is per name, not a closed vocabulary. Free text — `TITLE`, `ALBUM`, `ARTIST`, `LABEL`, lyrics — is never touched |
| `grade_check_tag_blank_lines` | Tags — no blank lines | ON | no blank line inside a tag value (`LYRICS` exempt) |
| `grade_check_lyrics_spaces` | Lyrics — no padding | ON | no leading/trailing space on a lyric line |
| `grade_check_lyrics_blank_lines` | Lyrics — blank line rules | ON | blank lines match the formatter's canonical output |
| `grade_check_lyrics_zero` | Lyrics — zero timestamp rule | ON | the `[00:00.00]` leader follows `lrc_add_zero_timestamp` / `lrc_zero_timestamp_blank` / `lrc_zero_timestamp_target` |
| `grade_check_lyrics_format` | Lyrics — canonical formatting | ON | re-running the formatter would change nothing (timestamps at `lrc_timestamp_precision`, `lrc_strip_metadata`, `lrc_collapse_blank_lines`, no merged timestamps) — so a stored lyric still carrying a stray `[id:…]` frame descriptor, an `[ti:]`/`[ar:]`-style header, or a leading credit line that repeats the track's own `TITLE - ARTIST` fails, while `[offset:…]` (an instruction, not a header) passes. The finding names its reason and offers the Lyrics script only for what the formatter repairs (R347) |
| `grade_check_cue_spaces` / `grade_check_cue_blank_lines` / `grade_check_cue_format` | CUE — no padding / no blank lines / canonical formatting | ON | CUE lines are trimmed, blank lines absent, and the sheet is byte-equivalent to the canonical formatter's output (`keep_empty_cue_lines`, `keep_other_cue_lines`, `cue_file_type`, `append_final_newline`) |
| `grade_check_accurip_format` | `.accurip` — canonical formatting | ON | each line trimmed, outer blank lines handled (`keep_empty_accurip_lines`) |
| `grade_check_cue_files` | CUE — referenced files exist | ON | every `FILE` line names a file that is in the album |
| `grade_check_lyrics` | Lyrics present | ON | every non-instrumental track has lyrics (embedded and/or `.lrc`, per `lyrics_format`) |
| `grade_check_lyrics_lang_tags` | Transform language tags | ON | `TRANSLATION-EN`, `TRANSLITERATION-JA-LATN`-style names, never the bare legacy ones |
| `grade_check_xlit_transliteration` | Transliteration — needed, never extra | ON | `mlo.lyrics_xlit.xlit_needs` says a transliteration is required and it exists, or is not required and none is stored (`XLIT_MISSING` / `XLIT_UNNEEDED`) |
| `grade_check_xlit_translation` | Translation — needed, never extra | ON | same, against the reader's language (`lyrics_translation_langs`, first entry) |
| `grade_include_music` | Audio tracks | ON | the audio files themselves participate in grading |
| `grade_include_cover` | Cover art | ON | `cover.*` images participate |
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
**R16 — ReplayGain is an opt-in family** (R42): absence is never a failure, a half-written
set always is. **AcoustID is required**, key or no key: every audio track carries the
`ACOUSTID_ID` + `ACOUSTID_FINGERPRINT` pair (`grade_check_acoustid`), neither half stored
fails naming both, and script 21 creates the pair locally — the API key gates lookups only.
**R16a — an alias tag exists only where the locale cannot read the name**
(`grade_check_alias_needed`). One rule, asked by everything: `server.integrations.alias_required`
— a name needs its alias when it is written in a script the configured `locale` (`locale`,
Settings → Import & tags) does not read, and MusicBrainz states a readable name for it. A LATIN
name never needs one (`Radiohead` carries no `ARTISTALIAS` in an `en` library — the owner's own
example), and a name in the locale's OWN script does not either (`宇多田ヒカル` needs
`Hikaru Utada` for `en`, nothing for `ja`). The script reading underneath is `mlo.lyrics_xlit`'s
(`non_latin_ratio` ≥ `_LATIN_THRESHOLD` and `dominant_script` ≠ latin — the pair that decides a
lyric transliteration), and the picker stays `alias_for`'s: `alias_for` chooses the ONE name the
pages show beside the stored one, so the file and the page agree (R87). Every writer asks the
same pair — `mlo.autotag._alias_tag_values` (the Auto Tagging stage, and the beets import plugin
through the same `write_mb_tags`), the import's own stamp (`mlo.autotag.fill_release_identity`
via `server.imports._stamp_release_identity`, album-level `ALBUMALIAS` / `ARTISTALIAS`), and
script 8's prescan, whose alias slot `mlo.autotag._alias_slot_open` opens ONLY for a name that
needs one — an album that is otherwise complete still costs the one release request that can
write its alias, while a Latin album's prescan stays exactly as cheap as before.
**WRITE AT MOST ONE VALUE PER ENTITY.** The bare `TITLEALIAS` / `ARTISTALIAS` / `ALBUMALIAS`
carries `alias_for`'s pick and that is all that is written: no per-locale fan-out
(`TITLEALIAS-JA`, `TITLEALIAS-RU` … for every locale MusicBrainz states), no second spelling of
the same alias, no `-<LOCALE>` spelling but the configured locale's own. That spelling rule is
`mlo.audio.alias_spelling_ok`, asked by everything (the writer's own "is it already there" probe
`_alias_slot_open`, the grade that requires the tag, the grade and strip that delete the wrong
one): the bare family, or the family suffixed with the configured locale or a variant of its
language. A file holds what a reader of the configured locale needs and nothing else — and a
spelling for another locale does not stand in for it, so script 8 still writes the one the reader
needs while script 10 clears the other.
**R16b — an alias tag nothing needs is EXCESS** (`grade_check_alias_excess`, default ON). The
mirror of R16a, and the reason "only what's required" is auditable rather than merely intended:
grading fails the family when the name needs no alias (a Latin name — or one in the locale's own
script — carrying one), when the tag is spelled for a locale the app does not write
(`TITLEALIAS-JA` in an `en` library), when it is a second spelling of the same alias, or when its
value IS the name (`X (X)`). `mlo.grader.alias_keys_excess` is that ONE predicate: the grade
fails the family when it answers anything, and Format all (10) (`mlo.format_all`) deletes exactly
those tags — gated on `strip_unknown_tags` like the rest of the strip — so a stale alias an
earlier locale wrote is cleaned up instead of living on the file forever. Optimize tags (23,
R343) is that same deletion on its own — `mlo.format_all.excess_tags` +
`strip_excess_tags`, one stripper with two callers — scoped to the album or track the user
pressed, which clears one album's aliases without the library-wide pass (10) or a lossless
re-encode (3, whose rewrite strip follows the vocabulary half only, `mlo.containers`).
`ALBUMALIAS` is graded by both halves like the other two; it is written by the import
and the tagging stage and was graded by nothing before this rule.
**R17 — CD vs Digital Media vs other.** `_is_cd()` is `mlo.tagtext.is_cd_media`:
`MEDIA == "cd"` or `"hdcd"`, case-insensitively. An **HDCD is a CD** — the same
disc with the extra High Definition Compatible Digital encoding on it, ripped
and logged the same way — so the CUE/LOG/AccurateRip/CRC/`LOG_GRADE`
expectations, the `.log`-CRC audit legs (R21) and the `SOURCE` stripping a CD
gets all apply to `MEDIA=HDCD` exactly as they do to `MEDIA=CD`; the two are one
vocabulary value each (`mlo.tagtext.CD_MEDIA_VALUES`), and `HDCD` is a value of
`MEDIA_VALUES` rather than an unknown one. `MEDIA == "digital media"` requires
`SOURCE`. Any other value in `KNOWN_MEDIA` is graded like Digital Media without
the `SOURCE` requirement, and a value outside `KNOWN_MEDIA` fails
`grade_check_media`. Two interactions follow
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
`grade_include_*` key ships `true` (67 of 67), so a fresh install grades
strictly with nobody pressing anything. The two that used to ship off —
`grade_check_audit` and `grade_include_other` — are named in
`mlo/config.py::STRICT_DEFAULT_KEYS`, so the change is a readable fact rather
than an implied one. **R18a — Strict** is therefore the identity preset (load
the defaults and set every `grade_check_*` true): it is what a fresh install
already has, and pressing it on an edited config restores it.
**R19 — Balanced** is the pre-strict set: the defaults with `grade_check_audit`
and `grade_include_other` off — the one-click way back to the old behaviour for
a collection nobody has audited.
**R20 — Relaxed** loads the defaults and then switches these 15 keys **off**:
`grade_check_tag_spaces`, `grade_check_tag_case`, `grade_check_lyrics_spaces`,
`grade_check_cue_spaces`, `grade_check_cover_crop`, `grade_check_lyrics_zero`,
`grade_check_tag_blank_lines`, `grade_check_lyrics_blank_lines`,
`grade_check_cue_blank_lines`, `grade_check_filename_case`,
`grade_check_ext_case`, `grade_check_excess_tags`, `grade_check_alias_excess`,
`grade_check_mb_links`, `grade_check_replaygain`.

---

## 5. Audit workflow, rip evidence and overrides

The audit verdict is the one grade input that is *derived*, and the order of
evidence matters.

- **R21 — a CD's verdict is its rip's OWN evidence, and only that.** For
  `MEDIA=CD` — and for its `HDCD` variant, the same disc to every rule here
  (R17) — script 6 writes `AUDIT=REAL` exactly when every enabled leg
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
  a CD — `MEDIA=CD` and the `HDCD` variant that is the same disc (R17) — at
  all; it can no longer downgrade a verified disc.
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
| `TITLEALIAS`, `ARTISTALIAS` (identity), `ALBUMALIAS` (release) | identity / release | Beets tagging (14) · import (the album-level pair) · locale aliases · the tag editor | `grade_check_alias_needed` (missing where a name needs one), `grade_check_alias_excess` (present where none is needed), `grade_check_excess_tags` (the family is in `TAG_ALLOWLIST`, bare or locale-suffixed, so a legitimate alias is never a foreign tag) |
| `GENRE` | identity | Auto tagging (8) · genre import · Format all (10) trims | `grade_check_genre`, `_genre_count`, `_genre_order`, `_genre_vocab` |
| `MEDIA`, `SOURCE` | release | Format lyrics (1) · media/source normalization | `grade_check_media`, `grade_check_source` |
| `ITUNESADVISORY` | identity | Auto tagging (8) · advisory fetch | `grade_check_missing_tags` |
| `INSTRUMENTAL` | identity | Auto tagging (8) · instrumental fetch | `grade_check_missing_tags`, `grade_check_instrumental` |
| `MOOD`, `ENERGY` | audio | Auto tagging (8) · Mood & Energy (16) | `grade_check_mood`, `grade_check_energy` |
| `BPM`, `INITIALKEY` | audio | Key & BPM (12) | `grade_check_key_bpm` |
| `DYNAMIC RANGE` | audio | DR & ReplayGain (7) | `grade_check_missing_tags` (never on video) |
| `ALBUM DYNAMIC RANGE` | audio | DR & ReplayGain (7) | `grade_check_album_tags` |
| `REPLAYGAIN_TRACK_GAIN` / `_PEAK`, `REPLAYGAIN_ALBUM_GAIN` / `_PEAK` | audio | DR & ReplayGain (7) | `grade_check_replaygain` (opt-in family) |
| `AUDIT`, `LOG_GRADE`, `LOG_CRC`, `INTEGRITY` | provenance | Audit library (6) | `grade_check_audit`, `grade_check_log_grade`, `grade_check_excess_tags` |
| `AUDIO_MD5` | provenance | nothing — legacy, read only | `grade_check_excess_tags` |
| `COMMENT` | identity | nothing — the free text of whatever ripper or vendor tagger made the file | `grade_check_excess_tags` (the value rule: a non-empty `COMMENT` fails with code `COMMENT`, and Optimize tags (23)/Optimize FLACs (3)/Format all (10) clear it) |
| `AUDIOAUDITOR_OVERRIDE` | provenance | the track editor (manual) | `grade_check_audit` (wins over every derived verdict) |
| `LYRICS`, `UNSYNCEDLYRICS` | lyrics | Fetch lyrics (13) · lyrics editor | `grade_check_lyrics`, `_lyrics_format` |
| `TRANSLITERATION`, `TRANSLATION` | lyrics | Lyrics transliterate (AI) (17) | `grade_check_xlit_transliteration`, `_xlit_translation`, `_lyrics_lang_tags` |
| `ACOUSTID_ID`, `ACOUSTID_FINGERPRINT` | provenance | the import wizard's AcoustID apply (fingerprint match) — the PAIR in one save, verified by re-read — and Fix AcoustID pairs (21), which every import chain runs over the album it just imported: it completes a half pair, and creates the pair for a file that names its recording but carries none (the id from the file, the fingerprint local) | `grade_check_acoustid` ; the pair is REQUIRED on every audio track (`grade_check_acoustid`), and BOTH spellings a tagger writes are read — this app's `ACOUSTID_ID` and beets/mediafile's `Acoustid Id` / `Acoustid Fingerprint`, the spelling its chroma plugin writes and this app's own import runs |
| `ENCODER_PROGRAM`, `ENCODER_QUALITY`, `ENCODER_VERSION` | provenance | Optimize FLACs (3) | `grade_check_encoder` |
| `MUSICBRAINZ_*`, `RELEASETYPE`, `CATALOGNUMBER`, `LABEL`, `BARCODE`, `ISRC`, `WORK`, `MOVEMENT`, … | release | Beets tagging (14) · import · MusicBrainz writes | `grade_check_album_tags`, `grade_check_mb_links`, `grade_check_naming` |
| `PERFORMER`, `PRODUCER`, `ENGINEER`, `MIXER`, `ARRANGER`, `DJMIXER`, `CONDUCTOR`, `WRITER`, `DIRECTOR`, `COMPOSERSORT`, `MUSICBRAINZ_COMPOSERID` | release | Beets tagging (14, `beets_credits`) · Auto tagging (8) — the release's own artist/recording/work relations, fetched in ONE request per album | `grade_check_excess_tags` (allowlisted, never foreign) |
| `ASIN`, `LANGUAGE`, `DISCSUBTITLE`, `LICENSE`, `ENCODEDBY` | release | Beets tagging (14) · Auto tagging (8) | `grade_check_excess_tags` |

Notes that are easy to get wrong: `MEDIA`/`SOURCE` belong to script 1, not to
script 8; `ACOUSTID_*` are written by the wizard's AcoustID apply — the
fingerprint/recording pair goes in with one save and is read back to prove it
landed, and a container the app cannot tag is reported per file — and by script
21, which completes a pair the file holds HALF of and CREATES the pair for a
file that names its own recording without one — the id read off the file
(`MUSICBRAINZ_TRACKID`, or the recording MBID the naming script wrote into
the file name) and the fingerprint taken from the audio locally, with the
AcoustID service asked only when a half pair names no recording at all;
`ENCODER_*` are written by the FLAC
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

The advisory tag answers to **one switch**, `advisory_auto_fetch` (the
provider fetch — the import step, the wizard and the *Fetch / refresh advisory
rating* action).

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
track page's *Check advisory + instrumental*, all
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
  for them (`grade_check_replaygain`, ON).
- **R43** — script 7 writes the album gain/peak and the track gain/peak for FLAC
  and MP4 alike, using the ReplayGain 2.0 reference of **−18 LUFS**;
  `replaygain_skip_existing` (ON) skips an album only when ALL FOUR tags are
  already on every track (`mlo.loudness._album_rg_complete`), unless
  `force_dr_replaygain` is set — never rsgain's own `-S`, which skips a track
  carrying ANY ReplayGain tag: an album whose tracks had track gain/peak but no
  album ones made the scan print no rows at all, so its album tags were never
  written and every re-run re-skipped it forever (pinned by
  `tools/test_dynamic_range.py`). **rsgain measures; the app writes.** The scan is
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
  exportable and applicable to an export like any other. An id that is not a
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
  finder shows beside the candidates, the wizard's Links/Covers preview, and the
  level rule 1 scores highest (the release group's art, then the matched
  release's, then a row found by name search) — and the **matched release's own
  front cover ranks just below it, above any row found by name search**: a named
  edition is evidence, a text match is a guess, and inverting those two is how
  the owner's Toxicity import took a store row's blue-tinted art (a different
  pressing) over the release's own cover. Both are asked when both ids are
  known, and the group's cover being absent falls back to the release's.
- **R56g — the cover score is a WEIGHTED MIX, not a positional order.** The
  policy's tiers no longer decide lexicographically: each contributes its level
  in [0, 1] times a weight (`mlo.cover_choice._TIER_WEIGHTS`, summing to 1.0 —
  appearance 0.22, size 0.20, release 0.14, identity 0.12, quality 0.08,
  source 0.07, kind 0.06, format 0.05, square 0.05, rank 0.01), and the score
  is their sum. The two biggest weights are image size and **cover-likeness**,
  so a slightly smaller but clearly better-looking cover can win; the rest
  decide the difference between two otherwise comparable images. Cover-likeness
  is measured from the image's own bytes in the SHARED search layer
  (`server.integrations._attach_looks`, memoized 30 days like
  `image_dimensions`), so the dialog and the unattended import rank identically:
  the row's `small` thumbnail is fetched through the same ranged-GET probe
  (256 KB), decoded with Pillow, converted to grayscale, downscaled to 64×64,
  and scored on its mean neighbour difference (edge energy). A near-solid/blank
  image (detail below `COVER_LOOK_MIN_DETAIL`) is REJECTED as not cover art; any
  other measured image carries a 0..1 `cover_likeness` on its row. Only the
  first `COVER_PROBE_LIMIT` rows are measured, and a row nobody measured is
  neither rewarded nor blamed — it scores the candidate set's own MEDIAN
  measured cover-likeness. The tier the winner actually won on (the biggest
  weighted advantage over the runner-up) is the one the deciding sentence names.
  Pinned by `tools/test_cover_choice.py` (the weights sum, the blank gate, the
  median, a better-looking smaller cover beating a flat larger one) and
  `tools/test_covers.py` (the metric decoded from real bytes and attached to
  the finder's rows).
- **R56h — the Cover Art Archive is not a default source.** Its MusicBrainz
  catalogue is a user-upload database, not a store, so it is off the shipped
  `DEFAULT_SOURCE_ORDER` (qobuz, applemusic, tidal, bandcamp, deezer, spotify,
  itunes, discogs) and `integrations._cov_enabled_ids`/`resolve_cov_search`
  never add it on their own — though a caller or a saved `cover_sources` that
  names `musicbrainz` explicitly is honoured (validated against the COV
  catalogue, as every id is). The archive is likewise no longer in
  `integrations.COVER_FALLBACKS` (now `("deezer", "itunes")`) nor in
  `server.artcache._fallback_candidates`, so no display path serves it in place
  of a row whose own URL refused. What stays is the IDENTITY read: the
  release-group front (the reference) and the release's own front are still
  asked whenever an id is known, and the policy's source tier neither prefers
  nor blames them (an identity read is not a store source; rule 1 carries it).
  Pinned by `tools/test_cover_choice.py`, `tools/test_covers.py` and
  `tools/test_artcache.py`.
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
  an edition's sleeve or a name-searched row: the group is now derived in ONE
  place, `integrations.cover_search`, from `integrations.release_lookup`'s own
  `release_group_id` (cached MusicBrainz read; a failed lookup leaves the
  identity untouched and the run continues) — so every caller gets it, the
  unattended import AND the finder's dialog, whose route passed only the release
  id an album's tags held and therefore ranked a set the import could never
  match. `imports.cover_candidates` still reads and passes the album's own ids
  (`_album_mbids`), and its own derivation is now a harmless repeat of the
  finder's; the ids are also what `staged_metadata` records, so a
  staged candidate set and the CAA read agree. Verified live for **OK Computer**
  (release group `b1392450-e666-3926-a536-22c65f834433`, no release id given):
  22 candidates ranked, the group's own front cover won —
  `coverartarchive.org/release/30702389-…/30730533321.jpg`, 1400×1400 JPEG,
  `release_cover: false` — over the Tidal/Apple 1400–4000 px rows (rule 1 and
  the front-vs-other label give it a lead the store rows' source pull cannot
  overturn, and a 4000 px file is *not* better than one at the target: it is
  only ever downscaled, so it scores the size tier's bottom), and with the
  karaoke/tribute rows (*Vitamin String Quartet*, *Mother Falcon*, *Molotov
  Cocktail Piano*) rejected by name; the sources report is part of the payload,
  so a search that could not ask the group says so.
- **R56c — a cover is never framed by a decorative border in the UI.** No
  border, ring or outline is drawn around a cover wherever it appears — the
  album grid, the library and list rows, the album
  header, the cover pickers, the menus. Covers keep their rounding, their
  placeholder background and their elevation shadow (`shadow-lg` /
  `shadow-2xl` are a drop shadow, not a frame). Two things are deliberately NOT
  that frame: the keyboard-only `:focus-visible` ring on whatever a keyboard
  user focuses (`web/src/index.css`, kept — without it there is nothing to see
  where they are), and a picker's own selection highlight, which must be
  transparent at rest so nothing is drawn until it is earned.
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
- **R56e — the finder's dialog and the unattended import rank the SAME candidate
  set, and say so with the same reason.** The policy can only pick the same
  image from the same rows, and `integrations.cover_search` TRUNCATES its answer
  at the ask (`_cov_results` stops once it has that many cover events), so the
  ask is ONE number — `mlo.cover_choice.SEARCH_LIMIT` — read by the route's own
  default (`server.main`'s `/api/cover/search`), by `cover_search`'s signature
  and by the import chain's `imports.COVER_REVIEW_LIMIT`. The import used to ask
  for 12 (a pick-one screenful) while the dialog's route asked 40, so a policy
  winner past the twelfth row — the weighted mix (R56g) lets a lower-priority
  source's image lead on size and cover-likeness, so the winner can sit anywhere
  in the list — was
  invisible to the unattended path and it landed an image the dialog never
  showed. The row SET, not only the rule, is therefore what "the same pick"
  means: `tools/test_cover_parity.py` drives both entry points (`GET
  /api/cover/search` through `TestClient` — the modal sends no `limit` — and
  `imports.run_cover_step` in both its staging and writing modes) over one
  candidate set per case (below-floor, unmeasured, another release's cover, an
  oversized file, an upscaled thumbnail, a size stated as text, a source the
  order does not name, the release group's art vs one release's sleeve, two
  sources at the same size, the format tier, the provider's own order as the
  last tiebreak, and a winner past the twelfth row), and asserts the same COV
  request, the same Cover Art Archive endpoints, the same winner, the same
  deciding sentence in the BEST PICK card and in the import's own report, the
  same ranked rows with the same verdicts, and the same file on disk. Verified
  live on the scratch scope with the providers stubbed: the dialog and the
  import both answered `qobuz` 1200×1200 `img/qobuz.jpg` with "ranked above the
  1200×1200 deezer candidate on the configured source order" over the same 23
  candidates, and the import stored that image as `cover.jpg` (1200×1200 JPEG).
- **R56f — a MANUAL cover write processes the image it just wrote.** `POST
  /api/cover/fromurl` (the finder's pick) and `POST /api/cover` (an upload)
  queue script 5, "Process images", over THAT album and nothing else
  (`server.main._schedule_cover_process`) — the same in-process path `/api/run`
  takes: `server.script_runners.run_chain` with `cfg["targets"]` set to the one
  album folder, so the run claims the album in `server.job_locks` (the row
  MAINTAIN → In progress lists, and the lock every other writer of that album
  answers) and ends with the cache drop a run does (`_invalidate_run`), which
  is what makes the page show the processed cover. The library is never walked:
  the scope is the one folder. Only a write to a LIBRARY album fires — a
  `staged` write (the import wizard's folder) and the importer's own cover step
  (`server.imports.run_cover_step`, the automatic writer, which shares
  `_write_cover_bytes` with the routes) queue NOTHING, because the import chain
  that finishes such an album already carries script 5 (R183). Two presses on
  one album inside `_COVER_PROCESS_COALESCE_S` are ONE run: the run the first
  queued processes the folder as it stands when it gets there. Pinned by
  `tools/test_track_covers.py` (a manual pick and a manual upload each queue
  exactly one album-scoped script-5 run, twice in a row is still one, a pick
  after the window queues its own, the importer's cover step and a staged write
  queue none, and the queued run reaches the script runner with its album still
  held in `job_locks`).

- **R56g — a surface asks for the cover at the size it DRAWS, and the answer is
  cached under the master's own stat.** `GET /api/cover?w=` takes a width
  bucketed to 160 / 320 / 640 / 1200 (`server.artcache`), re-crops and re-encodes
  from the master only for a bucket it does not hold, keeps the result under
  `<music>/.mlo/data/cover_thumbs` keyed by the cover file's path + size + mtime,
  and a cache hit reads the thumb and never the master. The URL a cover WRITE
  reports carries the new bytes immediately (the write invalidates its own
  entry), so a replaced cover is never a stale hit. Measured on a grid tile
  (74 px slot, 1400 px master, emulated 20 Mbit/s link): 3.13 MB and two
  requests, artwork at ~1230 ms → 12 KB, one request, artwork at ~98 ms — the
  same paint budget as the tile's own metadata — and a second view moves no
  bytes over the wire.
  Pinned by `tools/test_cover_preview.py` (drawn width, its crop, its bytes and
  the invalidation). `tools/measure_cover.cjs` is the measurement harness the
  numbers came from.

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
  `set_any_tag`, `set_video_tags`), so the beets import, the import wizard,
  every script and a manual edit all land canonical; script
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
  Apple and Discogs, and `merge_advisory` settles what they said: a
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
  sidecar — the read `mlo/lyrics.py::local_lyrics` does). Its rubric is
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
- **R67b — a tools folder two hosts share keeps BOTH hosts' installs.** R67a
  puts the tools beside the library, and one library can be read by two installs
  at once: the Docker container's `/music` is the very folder a desktop install
  writes. pip unpacks the wheels of whichever OS runs it into that folder, so a
  package built for another OS or CPython cannot be imported here and is not
  THIS host's install — `mlo.tools.python_pkg_path` reads each vendored
  folder's own `.dist-info/WHEEL` and counts only one whose tags this host can
  load (`py3-none-any`, and the stable-ABI `cp3x-abi3`, belong to every host; a
  folder with no WHEEL at all is never hidden for lack of evidence). Measured on
  the owner's library, whose `.mlo/tools` holds the container's linux
  `librosa v1.0.0` (485 MB, `numpy … cp312-manylinux…`) beside the desktop's
  `librosa v0.11.0`: the newest-version pick took the linux folder, its numpy C
  extension cannot be imported by the Windows interpreter, and every
  librosa-backed tag — MOOD/ENERGY (16) and BPM/INITIALKEY (12), in a run and
  during an import — silently stopped being written, because the analysis call
  is wrapped in a try/except. The same fact holds on the install side: a version
  the other host already holds is installed BESIDE that folder
  (`<pkg> v<version>-<platform>-<interpreter>`, e.g. `librosa v1.0.0-win-amd64-cp313`),
  because installing over it would merge two numpy builds into one folder —
  which detection then refuses for both hosts — and the failed-install path
  DELETES the folder, an install this host never made; and the stale-version
  pruner never removes another host's folder, which is not this one's to
  replace.
- **R68 — the page's own action is always on screen.** Install/Update-all is
  sticky (it does not scroll away with the first rows) and is disabled only when
  there is nothing this host can install, with the reason in its title. It never
  disappears, and it never offers a row this host cannot install.
- **R69 — detection matches what the installer writes.** A tool installed into
  the tools folder must be what detection reports (`mlo/tools.py`'s per-tool
  `_exe` field map, native Linux builds included), or an update the installer
  performed would be invisible, the row would keep reading the PATH copy, and
  its amber chip could never clear.
- **R69a — a pip install is judged by what the DETECTOR finds in the folder it
  wrote, and by nothing else about pip's run.** `_install_pip_package` asks the
  app's own detector (`tools.python_pkg_version` — pip's `.dist-info` beside
  the package, falling back to the folder name) for the version it asked pip
  for, through the ONE rule detection uses as well (`tools.pip_import_present`:
  the `<import name>/` package, or the `<import name>.py` module a package of
  that shape ships — `eac-logchecker` installs the latter), so a folder that
  landed is a folder whose row shows it. pip's exit status is not that verdict:
  pip writes the package first and its console script, with the "not on PATH"
  warning, last, so a run that fell over on the script — a bind mount that
  refuses chmod, a killed pip — left a complete, importable package this app
  never runs the script of, and calling that a failure also DELETED it. A run
  that landed nothing still fails loudly with pip's own last line as the
  reason, which is what covers a genuine pip failure, an unreachable release
  and an unwritable tools folder: all three leave nothing to find. Pruning the
  versions an update replaced is housekeeping and never fails a landed install
  either (a tools folder the process could not list is logged, and the stale
  folder is pruned by the next install).
- **R69b — a packaged desktop install installs with its OWN interpreter and
  reads NSIS with its own 7-Zip.** The frozen backend has no separate Python,
  so `pip install --target` runs through the backend itself
  (`mlo-server --mlo-python -m pip …`, `mlo/fetchdeps._pip_python`): the
  interpreter that WRITES the wheels is the one that will IMPORT them, which a
  PATH python cannot be — a `cp312-…-win_amd64` numpy a PATH python 3.12 writes
  cannot be loaded by a frozen 3.13 server, so the folder would land and
  detection would refuse it for both hosts. `pip` rides inside the PyInstaller
  build for that reason. The Windows installer's libjpeg-turbo asset is an NSIS
  executable upstream ships no zip of, and upstream's installer REFUSES to run
  a second time ("an existing version … is already installed", rc 2), so the
  bundle carries the full 7-Zip console (`tools/stage_desktop_bundle.py` copies
  `7z.exe`/`7z.dll` into the frozen tree) and `mlo.archives.find_7z` prefers it
  over PATH — a bare `7za` on PATH cannot read NSIS. Both are what turn the
  three failures on a Windows box with no Python and no 7-Zip — libjpeg-turbo
  ("silent install failed"), librosa and beets ("vendored Python packages need
  a Python interpreter on PATH") — into installs.
- **R69c — a packaged desktop install keeps its state beside it in AppData, and
  its uninstall removes the app and nothing else.** The Windows shell's backend
  is a CHILD of `mlo-desktop.exe`, and Windows does not end a child with its
  parent: uninstalling while the local backend ran left
  `%LOCALAPPDATA%\la musica\mlo-server` — a live server whose `python3XX.dll`
  and `*.pyd` files were mapped and could not be deleted — behind. The NSIS
  hook (`desktop/src-tauri/installer-hooks.nsh`, wired through
  `bundle.windows.nsis.installerHooks`) runs before install and uninstall: it
  stops the shell FIRST (a live supervisor respawns a killed backend within
  seconds), then the backend, and polls until both are gone before the delete
  list runs; the uninstall hook then removes the backend tree recursively,
  which also clears files an earlier build left under names this one does not
  use. The install's own state — `config.json`, `shell.json`, `mlo-server.log`,
  `server/data` (auth.db, the beets library) and
  `.dependencies` — lives BESIDE that tree and survives the uninstall, because
  `backend_launcher._redirect_engine_home` redirects `mlo.paths`
  (`SCRIPT_DIR`, `CONFIG_FILE`, `DEPS_DIR`, `LEGACY_DATA_DIR`) before the engine
  is imported; a reinstall reads it back.

---

### 7.9 Export processing and destination

An export is a copy of the library, so nothing it does may reach back into the
library files — and what it does to the EXPORTED copies has to be the thing the
user asked for, in the order they asked for it. `server/exporter.py` owns both.

- **R70 — the destination is the client's or the server's, and the client's is a
  zip.** `export_target` is `zip` (the client downloads one archive) or `server`
  (a folder the machine running this app can see, chosen with the drive picker).
  The zip target stages the export under `<music>/.mlo/data/export_zip/<id>/`,
  packs it with whatever the run wrote (the manifest when `export_manifest`
  is), answers
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
  device reading those tags would otherwise correct the gain twice. A track
  that would clip after the gain is reported, never silently distorted.
- **R72 — an equalizer profile is the user's own file, and its losses are
  named.** `export_eq_profile` selects a built-in preset or a profile imported
  from **Equalizer APO / Peace EQ** text (`mlo/eq.py`): `Preamp:`, `Filter N:
  ON|OFF PK|LS|HS|LP|HP|BP|NO|LSC|HSC Fc … Gain … Q …`, `GraphicEQ:` band lists, free
  field order, optional units, case-insensitive keywords. APO's OWN other
  spellings are read as the filter its configuration reference says they are —
  `PEQ` and `Modal` are its peaking row, `LPQ`/`HPQ` its pass filters with a Q,
  `LS 6dB`/`LS 12dB`/`HS 6dB`/`HS 12dB` its fixed-slope shelves, `LSC x dB`/
  `HSC x dB` its custom-slope shelves, `BW Oct n` a bandwidth — so a low-pass
  stays a low-pass and a shelf stays a shelf; a shelf's own slope in dB/octave
  and a Modal's `T60 target` are what the rendered filter cannot carry, and the
  import result states that rather than looking exact. OFF filters are
  skipped; a line with no equivalent (`Include:`, unknown constructs, an `AP`
  all-pass — phase only, so leaving it out leaves the magnitude exactly as the
  file wrote it — and an `IIR` filter, which IS the file's own coefficients) is
  IGNORED and REPORTED in `unsupported` rather than dropped, and so are the
  bands inside an `If:`/`ElseIf:` block: APO evaluates those against its own
  variables (sample rate, channel count, device name, user variables), which
  this app does not model, so a conditional band is NOT applied and the block
  is named with the count of lines it cost. A BAND line that
  cannot be read is an ERROR naming its attribute or its line, and a profile
  carrying one is REFUSED WHOLE on every path — the import and the export
  (`mlo.eq.apply_refusal`, one sentence, the words the editor's banner
  shows) — because a profile missing the band that failed to parse is not
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
  An album export writes no `.accurip`, `.log`, `.cue`, `.txt` or `.jpg`
  file: the cover travels EMBEDDED in each exported file
  (`embed_covers`, ON) and the rip's evidence stays in the library where the
  audit, the grading and the log's own checksum read it. WHICH families do
  travel is the user's file selection (R187); untouched, it is the tracks alone.
  One switch keeps the old behaviour available and it is OFF by default —
  `export_sidecars` (mirror `cover.*`/`.lrc`/`.cue`/`.log` — R187's
  `LEGACY_SIDECAR_FAMILIES`) — and the run
  result reports what did not travel in `excluded` (one row per file: album,
  name, `kind` — the file FAMILY of R187, one of `FILE_FAMILIES`' own keys — and
  the reason), with `excluded_counts`, `excluded_total` and the sentence
  `excluded_note` that also goes to the run log. Nothing is dropped in silence:
  an unexpected `.nfo`/`.md5`/`.sfv`/`.pdf`/`Thumbs.db`, a `.bak` nobody
  anticipated and a stray subfolder are all classified and counted. The
  `export_manifest` key (OFF) still writes `checksums.sha256` listing every
  written file, so a copied library can be proven intact at the other end.
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
  sheet's own name (`re_safe_filename`) and the organizer (`eval_script` →
  `sanitize_path`) all call this one function: a second spelling of the character set is how a path
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
  `replaygain_mode`, `clean_tags`, `sidecars`, `manifest`,
  `verify`, `prune`, `workers`, how lyrics travel (`lyrics`), the equalizer
  profile **by id** (`eq_profile`)
  — so a load points at the profile itself rather than at a copy of its curve —
  and `source_kind`, the Export page's source tab, stored verbatim for
  whichever surface has tabs. What it deliberately does NOT hold: the
  selection (which albums/artists/tracks are ticked) — data,
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
  is a viewer showing a second, stale copy. In the audit, a source `.lrc` whose
  track IS in the selection is output when the mode writes `.lrc`, and is
  reported as `lyrics` (the one non-audio kind the run itself can account for)
  when it is not; the `.lrc` of a track outside the selection is always
  reported. The run result carries `lyrics_mode` and the number of `.lrc`
  files it wrote.

- **R187 — WHAT an export copies is a file selection, family by family.**
  `copy_files` (per run) / `export_copy_files` (saved default) is a list of the
  keys of `server/exporter.py`'s `FILE_FAMILIES` — `audio` (the tracks
  themselves), `cover` (the album's `cover.*`), `lyrics`
  (`.lrc`), `cue` (`.cue`), `log` (`.log`/`.accurip`),
  `checksum` (`.md5`/`.sfv`/`.ffp`/`.torrent`), `text` (`.txt`/`.nfo`/`.url`/
  `.pdf`) and `other` (a non-audio file this app classifies as none of those) —
  and it is the ONE thing that
  decides what a run writes. One classifier (`_extra_kind`, over the extension
  table `_EXTRA_REASONS`, plus the cover names it knows by
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
  accurip) through the same code path: a caller that sends nothing new — or
  only that boolean — gets the behaviour it had, with one honest widening,
  since the files the audit already classifies as those families now travel
  with them.

### 7.11 MusicBrainz browsing

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
  the entity chip rows already use. Nothing else moves: a release's
  per-TRACK genre rows and the cascade's `per_track`/`per_source` lists keep
  MusicBrainz's spelling (`tools/test_genres.py` pins the source order and the
  mixed spelling of that merge), identity is untouched — every comparison
  folds case — and the string a reader copies off a chip is the string a tag
  holds.

- **R244 — every edition row on a release-group page adds THAT edition, and one
  press never marks the others.** The group page's editions table
  (`web/src/pages/MusicBrainzPage.tsx`, `MBReleaseGroupPage`) carries an action
  per ROW beside its MusicBrainz link, and it adds the row's own release:
  `run([release.id], "release", "best", 0, {title, artist, year})` — the same
  call the page header's button makes over the POLICY's pick, so the header and
  the row can never fetch two different editions. The row deliberately sends no
  `release_mbid` override (that is the header's own way of pinning the edition
  the policy would otherwise choose), and it passes the row's title and date so
  the server can name the framework folder without waiting on MusicBrainz. The
  spinner is per row (`rowBusy` holds the pressed id) while the hook's one
  `busy` flag keeps every add control disabled for the duration, because a
  single hoisted spinner over a seven-row table said "seven albums are being
  added". The reply is the page-level button's own — `useAddToLibrary`'
  `missing` count, the server's `albums[].created` replacing the press
  acknowledgement, and the queued search reported in the same words.

- **R317 — every edition row says its own catalog number, and the column keeps
  its floor.** R244's table gains the one fact that identifies which PRINTING a
  row is: `MBReleaseGroupPage`'s editions table now reads `… Country · Cat # ·
  Barcode`, the catalog number in a column of its own immediately before the bar
  code (the header row a reader sees is `Date · Title · Format · Discs · Tracks
  · Country · Cat # · Barcode`). The label and its `w-[150px]` floor are the MB
  search results' own `catalog_number` column's ("Cat #", same width), so the
  app names this one thing once. Nothing had to be added at the source: the
  number already rides every row of the payload the page holds —
  `integrations.release_group_browse` reads each release's own `label-info`
  through `mlo.release_choice.catalog_numbers`, the release
  choice's own reader for a candidate's catalog numbers, so a row and the ranking
  cannot disagree about which number an edition carries — and it publishes both
  `catalog_numbers` (every one, MusicBrainz's order) and `catalog_number` (the
  first, the key a search hit carries). The cell prints the LIST joined with
  " + ", the way the app joins multi-value text elsewhere (a group's compound
  release types, a multi-disc row's `10 + 11` breakdown), because MusicBrainz
  states one number per label and a two-label pressing really has two; it is
  `cell-ellipsis` with the whole value in its tooltip so a long join neither
  wraps nor squeezes its neighbours (R313's discipline, applied to this table),
  and an edition that states none prints the em dash the Date column prints
  rather than an empty cell. Pinned by `tools/check_release_choice.mjs`, which
  renders the page and reads the header row's own words IN ORDER, a row's own
  numbers joined, the tooltip, exactly one Cat # cell per edition, and the em
  dash beside the edition that states none.

### 7.12 What enters the library: the edition, the source, and the name in your language

- **R84 — one deterministic policy decides which edition is fetched.** The ten
  tiers of `mlo/release_choice.py`, in the order they are scored
  (`_TIER_NAMES`): release **status** (official → promotion → bootleg — an
  unofficial edition is chosen only when nothing official exists), the
  configured **medium** order (`auto_import_medium_order`, shipped as CD, Vinyl,
  Cassette, Other, DVD, Blu-ray, VHS, Video CD, LaserDisc, Digital Media — the
  video carriers ABOVE digital so a music video published on a disc outranks the
  same video published as a download, and digital last because a digital edition
  carries no catalog number and no pressing to match against; a format the list
  does not name ranks after every configured one — see R246), the **box-set**
  rule (an edition carrying video media BESIDE the album's own medium, or three
  or more discs, sorts below the album's own CD/digital media, so a 3-CD
  anniversary box no longer outranks the plain CD it contains — while an edition
  whose own medium IS the video carrier, a single-disc DVD, is not that and
  ranks on the medium order like any other — see `_set_level`), the
  **disc-versus-re-encode** rule (R85), the **track count** (an
  edition short of the release group's own count is penalised), the **release
  date** — the EARLIEST edition wins, and the reference is the earliest edition
  the group OFFERS, not the group's own `first-release-date`: a pressing that
  predates that date is still the earlier record of the two, and an album whose
  original is not on offer (a 1973 first release beside two 2010s remasters) is
  decided by the editions that are. The penalty for being later is strictly
  decreasing in the distance and NEVER flat (two reissues a decade apart are
  never a tie, which is what a linear term that reached zero at a nine-year gap
  let happen: a live "The Dark Side of the Moon" browse came back as a 2016
  reissue over the 1988 CD, and the album folder was named 2016) — the **date
  precision** tier (an edition that states its date in FULL, `YYYY-MM-DD`, beats
  one stating only its month or its year when the two could be the same day,
  because the folder is named after that date) — the **clean/edited-edition**
  rule (`prefer_original_edition`: a clean/edited edition sorts below the
  original), the **plain-title** rule (a title carrying a MusicBrainz
  disambiguation comment — "(BMG Club edition)", "(CB 811)", "edited version" —
  loses the tie to a title that carries none; nothing is read INTO the comment,
  one comment against no comment is the whole of it) and
  **prefer_release_country**, which only ever breaks a tie. The order is
  fixed and total: equal scores are broken by MusicBrainz's own listing order,
  never by chance, and `_deciding_reason` names the tier that decided
  (`"the disc-versus-re-encode rule"`). The SAME module serves the release-group
  page's ranking, `group_targets`, `resolve_release`, `auto_import_targets`,
  `pick_releases` and `GET /api/mb/release-choice`, so a page
  and the add cannot disagree about which edition "this album" means.
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
  scores every candidate the same, so the other nine decide exactly as they did
  before the rule existed. It is not a grade key: it decides which file the
  grade is computed on, and the same switch decides the disc-folder case of
  §7.13.

- **R246 — one medium vocabulary, and an untouched install follows its new
  order.** `mlo.tagtext.MEDIA_VALUES` (21 value sets, `mlo/tagtext.py:67`) is
  the closed vocabulary every writer canonicalizes a `MEDIA` tag against, and it
  gained **`Web`** — MusicBrainz's own spelling for a release published online
  only, as `Digital Media` is for a download — and **`HDCD`**, MusicBrainz's
  spelling for a CD carrying the extra HDCD encoding, which every CD rule reads
  as the CD it is (`mlo.tagtext.CD_MEDIA_VALUES`, R17). The `Web`/`Digital
  Media` pair is the SAME medium to every rule that reads one:
  `mlo.grader`'s known-medium test and the tag writers
  all treat the pair alike, so a Web release is not left as an unknown value.
  `auto_import_medium_order`'s shipped default now names the video carriers
  (CD, Vinyl, Cassette, Other, DVD, Blu-ray, VHS, Video CD, LaserDisc, Digital
  Media), and `normalize_config` migrates an install whose SAVED list is still
  the old default (`LEGACY_DEFAULT_MEDIUM_ORDER`, `mlo/config.py:114`: CD,
  Vinyl, Cassette, Other, Digital Media) to the current one — a copy of the
  default is not a choice, and Settings used to carry it. A list the user
  actually edited is kept exactly as saved (unknown labels included, capped at
  12). Pinned by `tools/test_release_choice.py`.

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
  SAME setting is what translates non-Latin names for the beets import.
- **R95 — "Add to library" records the request, and a request is not the
  album.** Three things follow, and the library and interrupt recovery have to
  agree about all of them (`server/api_add.py`, `server/pending_albums.py`,
  `server/interrupt_recovery.py`):

  * **The button answers before MusicBrainz does.** A request that already
    carries the title and the artist has given everything a framework album
    needs, so the folder and its marker are written from the
    request (`pending_albums.create_from_request`), the reply says
    `"background": true` + `"resolving": true`, and the release lookup
    (`integrations.auto_import_targets` + the rest of the add) runs on a daemon
    thread (`api_add._prepare_add`). A caller that gave only an id (a bare MBID
    or URL) keeps the synchronous resolution — there is nothing to name a folder
    with until MusicBrainz answers — and its reply says `"resolving": true` for
    the same reason: the server, not the caller, named the release.
  * **A framework album is never the album.** A folder holding a
    `.mlo_pending.json` marker and NO audio is a REQUEST on disk: its marker
    carries the release's MBIDs, so the library's own "already in your library"
    check (`library.owned_mbids`) refuses to count it
    (`pending_albums.is_placeholder`) — counting a placeholder as the album is
    what left an empty album standing in the library for ever. Only a folder
    with audio satisfies "already in your library".
  * **One release is one album folder, and an import ends the placeholder.** An
    import that lands somewhere else (its own name disagreed with the naming
    script, or it was aimed at a folder by hand) is tied back to the placeholder
    by release identity — `pending_albums.adopt_root` writes into the
    placeholder's folder, and `pending_albums.clear_if_filled` takes the
    placeholder down when the album really arrived elsewhere
    (`_drop_other_placeholder`, matched by `imports._album_mbids`, never by
    name) — and the startup sweep (`server/interrupt_recovery`) removes the
    placeholder whose album IS there.

- **R374 — a named add searches for the shape the row named, not just the
  words.** "Add to library" on a row that carries no MusicBrainz id resolves
  the release by NAME (`api_add._name_match`), and the index lists same-named
  release groups of every type: for "All Hope Is Gone" by Slipknot it returns
  the 1-track digital SINGLE first and the 14-track album second. The add took
  the provider's order (or the year, when a row stated it), so an album row
  became the single and its framework album held ONE track — the album's own
  title (owner report: "it only has one track being the album title for some
  reason"). The row's own kind — and a caller's `types` selection when it made
  one — is now a PREFERENCE among the rows the search returned, ahead of the
  year hint, matched through `mlo.release_choice.type_matches` (the one
  vocabulary, so "Album + Live" still means exactly that). It is a preference
  and never a filter: a search where nothing states the wanted type falls
  through to the year and then to the provider's order, exactly as before, so
  a match that existed cannot be lost. `tools/test_add_to_library.py` pins it
  with the real shape — a stubbed search answering single-then-album — and
  asserts both the release group that was matched and the whole tracklist that
  reaches the manifest; without the preference the case matches the single.

### 7.13 Disc rips: a DVD or Blu-ray structure is one title, not a pile of parts

- **R88 — a disc structure is recognized, its feature is never guessed, and
  what it produces is a bit-exact remux.** A folder holding `VIDEO_TS/` (or a
  loose `VTS_nn_m.VOB` title set) is a DVD rip; a folder holding `BDMV/` (or a
  `BDMV/STREAM/` directory) is a Blu-ray one; both are read by
  `mlo/videodisc.py`, which knows the grammar and nothing else — the parts of a
  title set are `VTS_nn_1.VOB`, `VTS_nn_2.VOB`, … and the part index starts at
  1, because `VTS_nn_0.VOB` is the set's MENU and is never the feature. A
  Blu-ray's titles are NOT its file names: the `.mpls` says which clips
  form which title and in what order, each play item carrying the clip's own in
  and out time, and a `.mpls` that cannot be parsed is refused rather than
  guessed at — including when only SOME of a disc's `.mpls` files parse, because a
  title the disc states and this code cannot read may be the feature. An `.iso`
  is recognized only to say so: nothing in this app reads inside a disc image
  (a Blu-ray one is usually AACS-encrypted), so it is asked about, never
  opened. A single `.vob`/`.mpg`/`.m2ts` with no structure around it is an
  ordinary video file and keeps the ordinary single-file path.

- **The main feature is the longest title, and an unclear one is a QUESTION,
  not a coin toss.** Durations come from the structure itself where it states
  them (a Blu-ray's play items) and from one `ffprobe` per part for a DVD, so a
  disc is picked without decoding a frame. The app refuses, and asks, when: the
  structure is an `.iso`; a `.mpls` or a part cannot be read; a usable title has
  no measurable duration; the runner-up is within `max(30 s, 5%)` of the longest
  (both durations are named in the question); or a Blu-ray `.mpls` replays a
  clip twice or plays only PART of one (the concat demuxer could not reproduce
  that title, so the app asks instead of shipping something else). A refusal
  touches nothing on disk. The question is stored as one row per album — the
  same shape the wizard's family questions use, so the notification bell
  and `GET /api/import/prompts` show it — and
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
  scoped to that album.** The import queue reports "two separate releases" when any of
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
    already in the library (`library.owned_mbids`), and it RE-CHECKS the running
    set inside the same critical section that registers the job
    (`_running_keys_locked` under `_lock`): two requests arriving together — a
    double press and a manual grab — used to both find the
    release "not running" and register two jobs, which the album-folder claim
    only made WAIT, after which the second imported the same album again.
  * **Two jobs heading for one folder serialize, and the chain stays in its
    album.** `job_locks` holds the folder `_import` names (before any `(2)`
    suffix) for the job's whole life — the whole import — so two
    albums cannot move their files in at once; and the chain runs
    as `script_runners.run_chain(targets=[album])`, which sets `cfg["targets"]`
    for every script, so nothing in an import ever walks the library (`mlo/cli`'s
    own Run All is the explicit, user-started library-wide path and is not what
    an import runs).

- **R340 — the fingerprint never chooses a release by itself, EVER.** The
  wizard's automatic detect reads the album's own TAGS (`GET /api/album/
  mbdetect`: a MusicBrainz release id in any track's tags) and, when they name
  none, searches MusicBrainz for the album its tags describe — both are
  evidence about what the files SAY they are, and a manually entered link is
  authoritative over both. An AcoustID fingerprint is evidence about the AUDIO,
  and what it matches is a release group with many editions, so it never picks
  one: the ONLY press that uses it is **Match from fingerprint**, the button
  beside the release field (`ImportWizard.matchReleaseFromFingerprint`), which
  ends in the same `acceptAcoustidRelease` → `pickRelease` flow a manually
  pasted id takes, so the tags are written by one writer either way and the
  accepted match files its `ACOUSTID_ID`/`ACOUSTID_FINGERPRINT` pair. There is
  no setting that lets a fingerprint fill a release automatically — the old
  `import_acoustid_autofill` opt-in is gone — so nothing the user did not ask
  for can ever replace the MusicBrainz link they entered.

- **R344 — a MusicBrainz link a file already carries is the USER's, and an
  import never replaces it with the edition it happened to resolve.**
  `server.imports._stamp_mb_tags` force-writes the release identity, but the
  two LINK ids — `MUSICBRAINZ_ALBUMID`, `MUSICBRAINZ_RELEASEGROUPID` — are left
  alone when the file already states one; `force_ids=True` is the caller saying
  the release was EXPLICITLY chosen for this album (a bulk pin, a download
  match), which is when the id may land over an existing value. Changing a link
  by hand stays the raw writers' (`POST /api/mb/assign`, `POST /api/
  import/commit`). So a MusicBrainz link pasted in the wizard survives every
  later re-import, and an album that arrived tagged with another pressing's id
  keeps it until the user says otherwise. The other identity slots
  (DATE/country/status/label/catalog number) are still force-written, as R287
  documents. Pinned by `tools/test_import_pipeline.py`.

- **R345 — a manual import that was never finished is REMEMBERED, and the tray
  offers to continue it.** The wizard bookmarks the album it is on at each step
  (`server.import_sessions`, one entry per album in
  `<music>/.mlo/data/import_sessions.json`, written through
  `POST /api/import/sessions`), and clears the bookmark when the album reaches
  Finish (`POST /api/import/sessions/dismiss`). A client restores the list from
  `GET /api/import/sessions` on load and the notification tray shows one
  **Continue import** row per unfinished album, linking back to
  `/import?album=…&step=…` so the wizard re-opens on that album at the step it
  was left on. A bookmark whose folder is gone is pruned on read. This is NOT
  R121's gap prompt: a prompt is raised AFTER a finished run reports what its
  sources could not supply, while a session is a run that has not reached
  Finish at all. Pinned by `tools/test_import_sessions.py`.

- **R346 — a native analysis helper is allowed, the engine that ran is named,
  and no dependency stays without a call site.** `rust/` is a zero-dependency
  cargo crate that builds the `mlo-audio` binary; `mlo.dr` prefers it (it
  spawns the app's ffmpeg and reads the PCM itself, so a decoded track never
  materializes in Python) and falls back to the numpy block math, and
  `tools/test_dynamic_range.py` pins the two to the same integers on stereo,
  mono, 96 kHz, silent, one-block and undecodable fixtures — plus the case
  where numpy is absent and the helper still measures. A build with neither
  engine keeps reporting the missing dependency instead of skipping silently
  (`mlo.loudness`'s gate reads `have_helper() or have_numpy()` and logs which
  one ran). The container builds the helper in its own stage and copies it onto
  PATH; a build without that stage keeps working on numpy, so this is an
  optimization, never a requirement. Every runtime dependency is audited with
  its call sites in `docs/DEPENDENCY-AUDIT.md`, and one that no call site needs
  is removed rather than kept: `aiofiles` and the three unused `desktop/` npm
  deps went that way. Pinned by `tools/test_dynamic_range.py` and
  `tools/test_import_sessions.py`.

- **R347 — changing the MusicBrainz link in the import wizard TAKES EFFECT, and
  a release-group link resolves before anything is written.** The Links step
  resolves the chosen id through `GET /api/mb/release`, which — like
  `POST /api/mb/match` — resolves a release GROUP to its best edition through
  the same release-choice policy the import and the bulk queue use, and then
  commits THAT release id. Before this, Links committed the new id and the Match
  step's Confirm wrote the PREVIOUSLY fetched release's id back over it, so
  picking a different release appeared to do nothing; and a group link was
  stored in `MUSICBRAINZ_ALBUMID` as the group's own id. The Match writer also
  never writes an EMPTY album-level id (`ImportWizard.assignTracks` omits
  `MUSICBRAINZ_ALBUMID`/`RELEASEGROUPID`/`RELEASEID` when the release is
  unknown), so a Match step that lost its release can no longer CLEAR a link the
  Links step just committed — which is what left freshly-linked albums grading
  "Missing MusicBrainz release link". Pinned by `tools/test_mb_search.py` (a
  release-group id resolves in both routes, never the group's own id) and
  verified live against the real page (link A → link B through the wizard).

- **R348 — the library refreshes itself after any write, and the refresh ENDS
  on the fresh tree.** Every mutating request that can touch the library
  publishes one coalesced `library_changed` frame — an HTTP middleware
  (`server.main._library_write_signal` → `server.events.note_library_write`),
  so a tag write, a cover, a rename, a trash move or an import step reaches
  every open page, not only the surface that made it. `server.tagcache`
  publishes the same frame again when its background rebuild of the assembled
  tree lands, and that second frame is the point: `invalidate_album` serves the
  tree STALE-WHILE-REVALIDATE, so the refetch a write immediately triggers is
  answered with the PRE-write rows — without the rebuild's own frame a page kept
  them until the next visit. The kind is SILENT to the client
  (`notifications.SILENT_KINDS`): `/ws/events` hands it to `onAppEvent` (which
  drops the library-derived queries in `App`) but never to the tray and never to
  an OS notification — a tag write must not fill the panel. Credentials,
  config, dependencies, export and EQ are excluded: they invalidate their own
  client queries and must not cost a library refetch per click. A rebuild that STARTED
  before a change must not END on the pre-write rows either: two generation
  counters (`tagcache._lib_write_gen` / `_lib_drop_gen`) let
  `_refresh_library` tell, so a WRITE landing mid-build keeps the tree dirty
  (the next request rebuilds again, and the rebuild's own frame brings a
  client back) and a DROP landing mid-build (a settings change, Refresh)
  discards the in-flight payload instead of resurrecting a tree built with the
  old config. Pinned by `tools/test_notifications.py` (the coalesced burst, the
  middleware over HTTP, a GET announcing nothing, and all three rebuild/mid-flight
  cases) and `tools/test_notifications.cjs` (the kind refreshes the derived
  queries, and is silent).

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
  fetched BEFORE the chain ran and the expected-tracklist manifest (script 15).
  Measured on the real import:
  the album landed in its canonical folder holding only the FLACs while
  `cover.jpg`/`.mlo_expected.json` stayed in the staging
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
    left in MAINTAIN. What waits: the AUTONOMOUS paths — the bulk queue and the
    one-click downloads import
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
    those files at the same time, and a background chain was
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
    the library root, and a second press on an album already being imported
    (refused, naming the holder).

- **R160 — `automatic` finishes an import WITHOUT a person.** The shipped mode
  (`import_autonomy: "automatic"`, `mlo.import_policy`) means the pipeline does
  every step it can and then reports what is left; it never stops to ask. What
  it decides on its own, each through the family's own writer (the same entry
  point the manual option in `mlo.import_policy.FAMILIES` calls, so the two can
  never drift apart): the MusicBrainz **links**
  (`imports._stamp_release`), the **cover** (`cover_candidates`
  → `mlo.cover_choice`, R163), the **genres** (`_stamp_release`), the
  **lyrics** — and, when the chain finds none, the **instrumental** mark that
  settles them (R162) — the **advisory** (`fetch_advisories`) and the
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
  PICKS its own albums — the library-wide sweep (Run All, a chain with no
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
  a hold on the user — a released album read as *both* "Completed" and
  "Needs you" is the exact shape that made a finished import look like a stall. The only entries that still belong in that
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
  manifest, the release-group placeholder cover) — and everything the reply
  does not need runs off-request: the
  release resolution on the daemon thread the deferred path already had
  (`server/api_add._prepare_add`, `_prepare_artist`), and the album's PAGE
  content on `pending_albums.prefetch_content(folder, cfg, background=True)`.
  The page content is the half that used to be paid inside the request:
  measured on a bare-id add (`{"mbid": <release id>, "kind": "release"}`,
  scratch scope, real network) the reply took **13.4 s** wall clock, of which
  10.0–13.1 s was `prefetch_content`'s provider work in the request path —
  `cover_search` 3.4–8.5 s, the MusicBrainz metadata step 3.0–5.3 s — for content only an OPENED album page
  reads, while the album row, its manifest and its cover were already
  on disk. The same add answers in **1.3 s** with that content fetched behind
  the reply, and the remainder is the two MusicBrainz lookups that DO name the
  folder (0.6 s) — the case that legitimately pays a resolution inside the
  request, because `albums[].album_path` is not knowable without it; the
  reply's own `background`/`resolving` flags say which case it was, and a
  caller that already holds a title and an artist pays neither (0.2 s,
  deferred). The reply vocabulary — `ok`, `queued`, `albums`, `skipped`,
  `errors`, `note`, `matched`, `resolving`, `background` —
  keeps its meanings, and a `note` must be true at the instant it is shown:
  it must not claim a search that has not started (`_deferred_note`'s own
  standard).

- **R142 — one release is one album folder, from the press to the grade.** The
  framework album an add creates IS the import's destination: an import that
  would land beside it is re-pointed INTO it (`pending_albums.adopt_root` —
  `os.replace` cannot merge two directories, so the album writes into the
  placeholder's folder), and the folder then takes the name the naming script
  gives it (`pending_albums.rename_placeholder`), so an add-time name never
  outlives the tags. Landing beside it was not only a wasted move: the
  intermediate folder is an ALBUM to the library walker — one level too shallow
  to sit under its artist — so the grid drew it with the library root's own
  folder name as its artist ("Artists") and the folder name as its title,
  BESIDE the album it was about to become. Which framework album is this
  release's is identity, not name: its marker must carry the release's own
  MBIDs, it must hold no audio, and it must be inside THIS scope's library
  root. The placeholder appears at the instant of the press (that is the
  feature) and ends the moment the album really lands
  (`pending_albums.clear_if_filled`), so one release shows one album folder for
  the whole import.

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
  `server.job_locks`, and takes only a claim whose kind is `import`
  or `scripts` — the same two the In progress page reads). A job
  importing its OWN claim is never a duplicate: an autonomous import holds the
  album and then runs this very import under that claim, and a caller with no
  job of its own keeps the old
  wait-then-run behaviour, because skipping there could leave an album
  unimported. The user's own press (`wait=False`) keeps its 409, whose sentence
  names the holder.

- **R181 — a disc that could not be checked is not a failed check.**
  `mlo.grader`'s CD verdict charges a missing leg once — every leg the app's own
  artefacts decide (a LOG_GRADE the scorer writes, the log's CRCs) — with ONE
  exception: the AccurateRip leg. A pressing the database has never seen reads
  exactly like a disc with no `.accurip` at all and no code path can tell the
  two apart, so failing the album for it failed the rip for what the network
  does not know. It is reported in the grade's `notes` channel and rendered as
  "Not checked", never under "Failed checks" — the state is stated, the album is
  judged on what could be measured, and the stored verdict is still never
  guessed.

- **R183 — the import pipeline runs the steps that CAN overlap side by side,
  and one track at a time where the provider's interval is the wall.** The
  steps between the press and the first script are not one kind of work: links,
  genres, advisory and instrumentals write TAGS on the audio files, while cover
  art writes FILES — the review record it shares with the cover review screen,
  and the art the album folder keeps. The file step is therefore started on one
  worker right after the genre step (which settles the identity its lookup
  reads, `album_identity`) and joined before the chain, which is the first thing
  that needs it on disk (script 5 processes the images, the grade wants the
  cover). It announces itself with its own phase line ("Fetching cover art…"),
  and the phase list `tools/test_import_pipeline.py` checks is that list.
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
  it (the shape the provider throttle always had): held across the call, one
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

- **R170 — an add takes ANY MusicBrainz entity.** `POST /api/library/add`
  accepts a release, release-group, artist or recording **URL** (or a bare
  MBID) and turns it into exactly what the MusicBrainz pages' own *Add to
  library* makes, because it is the same route: the entity's own `kind`, and
  `kind: "auto"` for a bare MBID, which `server.api_add._intended_kind` resolves
  through `integrations._kind_for`. The paths therefore cannot disagree about
  what a pasted id is: a release-group resolves the group's best edition through
  the release-choice policy (R84), an artist records its discography in the
  background (`background: true`, the albums appearing as each is created), a
  recording resolves to the release that carries it, and a release is added as
  itself. A link to an entity the app cannot add (a label, a work, a place) is
  refused with the list of what it does take, rather than having its UUID read
  as a release.

- **R154 — an import does not reuse the add's fetched page content, and the
  cover candidates are the reason.** The add path resolves the album's page
  content before the audio exists (`imports.prefetch_album`, R140); the staged
  cover record is a PICK SCREEN — any surface may restage it, `staged_metadata`
  finds it by folder name or by MB id as well as by path, and it carries no
  proof of which search, for which album, produced it — so ranking it would
  mean writing an image the policy chose from ANOTHER search's rows instead of
  the best of what exists for the album being imported. That is the one thing
  both cover modes share (`test_covers` pins it: with `cover_review` off, the
  same fresh candidate set is ranked and its winner written), so
  `run_cover_step` still ranks a fresh set and the import pays that search. A
  saved lookup is only ever reused where it is attributable to THIS album by
  identity; the add's page content is not, so it is fetched fresh.

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
  served from the cache that was just invalidated by name. The same rule now
  covers the person's own run: `/api/run` (`server.main._invalidate_run`) drops
  the folders the run NAMED plus the folders a script moved an album INTO
  (`stats["moved_targets"]`, the report the chain itself follows), and only a run
  with no targets at all — Run All, a library-wide sweep, which really did touch
  everything — keeps `invalidate_all()`. `mbresolve.invalidate()` is still called
  either way: that index is ONE timestamp over the whole library and rebuilding
  it is lazy, while a run that renamed a folder must not keep resolving the old
  paths to it.

- **R110 — the app's trash bin has a size cap, and a store over its cap is
  emptied oldest first.** `trash_cap_gb` (5 GB shipped, Settings → Storage,
  one decimal; 0 or negative = the cap off) is enforced by
  `server/cache_caps.py` against the LIVE folder, so a leftover from an older
  version counts like anything else: it measures
  `<music folder>/.mlo/trash` across every per-user bin. The unit of deletion is
  the one the app's own route deletes — a child of a
  per-user bin (`/api/trash/delete`, the Trash page) — and the
  pass stops the moment the store fits, so an entry that alone would overshoot
  by far is taken only when the store is still over without it.
- **R110a — a prune never takes what is in use, and says what it took.** One
  question decides, asked of state the app already trusts: is the path held
  in `server.job_locks` (the registry every route is refused against, so the
  report carries that job's own sentence). Such an entry is skipped WHOLE,
  named in the report, and the cap stands above its limit until the job is
  done; an entry the filesystem refuses to give up is kept and reported the
  same way, never worked around. A trash entry
  is deleted exactly as `/api/trash/delete` deletes one — the entry first, then
  its origin record dropped from the bin's `.mlo_manifest.json` — so every
  entry a prune KEPT is still restorable to the path it came from. What a prune
  did is a normal, expected action: one log line and one notification
  (`storage_pruned`, "Freed …", linking to the page that owns the store), and
  only what could not be deleted is reported as a problem. The pass runs from
  its own worker thread started with the app (`server/main.py` lifespan, tick
  300 s, first pass after a 60 s settle), so an install nobody has opened a page
  on still holds its cap.

- **R247 — one song of a rip is imported INTO the album that rip is, and the
  album says what is missing.** An import bringing exactly ONE audio file is a
  different question from a folder, and `imports.import_album_target`
  (`server/imports.py:1082`) answers it only when the caller named the "album"
  after the track itself (the wizard's free-text album field, or a dropped
  file with no folder of its own) — a multi-track import keeps the requested
  name, and a real album name is never hijacked by a track's own tags. The
  destination is then the file's OWN evidence, in one order: `library` (the
  library already holds that release — matched by `MUSICBRAINZ_ALBUMID`, else by
  the canonical ALBUM/ALBUMARTIST pair, `library_album_for`) and the song lands
  IN that folder, `album-tag` (the album the arriving file's own `ALBUM` tag
  names), or `requested`. A library folder is filled by `merge_into_album` —
  NEVER overwritten, so the rip's own `.cue`/`.log` and any same-named track
  stay exactly as they are. The rip's sheets are then read into the album's own
  manifest (`record_sidecar_tracklist` → `mlo.discs.sidecar_tracklist`): the
  `.cue` is the tracklist when one is there (it names titles AND files), the
  `.log`'s TOC otherwise, and a log that names no file has its rows placed by
  the evidence left — the file's own track number, then its playtime against
  the log's (`match_disc_row`). The manifest written is the `.mlo_expected.json`
  the wizard writes from a MusicBrainz release, so a partial album is partial
  the way every other surface already understands — the library page's *partial*
  flag, the missing-rows list, and the grade. Three guards keep it honest: the
  manifest is written only when the folder has none (writers fill, they do not
  overwrite), only when the sheets state a tracklist, and only when some row is
  actually missing. Pinned by `tools/test_single_song_import.py`.

- **R248 — a partial disc is graded on the evidence it HAS, and its `.accurip`
  is never regenerated from a slice of it.** A partial album is not a smaller
  album, and four places say so:
  * **script 9 refuses** (`mlo/accurip.py::album_pass`, `mlo/accurip.py:920`):
    when `mlo.discs.album_expected_state(album_dir)` reports a missing row, the
    folder's stored `.accurip` describes the WHOLE disc and is left exactly as
    it is — asking AccurateRip about a disc of one track would write that answer
    over the rip's own file, a lie about the tracks that are missing that reads
    perfectly current afterwards. The skip is logged with the present/total
    counts.
  * **the per-track verdict comes from the file that is here**: grading reads
    the `.accurip`'s per-track table (`mlo.accurip.parse_accurip_per_track`,
    keyed by disc + track number) and a partial album takes the verdict its OWN
    tracks carry — the disc's album-level word is NOT read, because it claims a
    verification of the tracks that are not in the folder.
  * **a CD rule that cannot apply says WHY** (`mlo.grader._grade_album`'s
    `partial_reason`): the `Missing .log file` / `Missing .cue file` / no
    per-track CRC failures become "… — this album holds N of M tracks of its
    recorded tracklist, and a CD is graded on the whole disc's sheets: import
    the rest of the rip, or its .cue/.log next to this track", and the missing
    CD legs are worded the same way. A sheet that IS present keeps its normal
    result — the suffix is added to a failure, it never invents one — and
    `Missing LOG_GRADE tag` stays unworded, because on a partial album with its
    log present that failure is about the audit not having run.
  * **the grade reports it**: `partial`, `partial_reason`
    ("N of M tracks of the album's tracklist are in this folder"),
    `expected_total` and `expected_present` ride the album result, and the
    report's first line says "Partial album: …" rather than letting a slice read
    as a small album.
  * **the tracks that are missing FAIL the album** (`EXPECTED_TRACKS_INCOMPLETE`,
    `grade_check_expected_tracks`, the owner's ask): a folder holding 14 of a
    CD's 15 tracks used to grade PASS while the page said "14 of 15" beside it —
    the readout knew and the grade did not charge for it. The rule is the one
    completeness rule the app already has (`mlo.paths.expected_tracks_state`,
    read by the page, the grade and script 9), never a second opinion, and the
    count rides `partial_reason`. This is the one thing a partial album cannot
    do: it is graded on the evidence it HAS (the bullets above) and it fails on
    what it does not have.

- **R287 — a release-driven import writes the album's OWN identity, and the
  album entry reads it while the audio is still arriving.** The owner's report:
  an album card sitting at "Verifying" showed a title and nothing else, and an
  import that knew exactly which pressing it was fetching left the medium, the
  country and the catalogue number blank until somebody noticed weeks later.
  The three cells the readout shows have three different sources, and only two
  of them are tags:

  * **medium → `MEDIA`, country → `RELEASECOUNTRY`** are *release* facts, and
    `server.imports._stamp_release_identity` writes them (plus the rest of the
    album-level identity MusicBrainz states: `LABEL`, `CATALOGNUMBER`,
    `BARCODE`, `RELEASESTATUS`, `ASIN`, `SCRIPT`, `LICENSE`,
    each disc's `DISCSUBTITLE`) during `finish_album`, before the chain runs, so
    script 1's MEDIA/SOURCE normalization and the digital settle see the
    release's own medium rather than a guess. The writer is
    `mlo.autotag.fill_release_identity` — the Auto Tagging stage's album-level
    pass (`mlo.autotag.album_release_tags`) fed the release the import ALREADY
    holds, so an import and script 8 can never spell a value differently.
  * **the source is the pressing that LANDED.** The payload handed to
    `finish_album` is the edition the wizard's user picked or the release the
    import resolved — never the album's manifest or framework marker when it
    names a different edition. No lookup is made when the payload is in
    hand; with none handed in, the one `integrations.resolve_release(album_mbid)`
    the genres step already makes is reused — one cached request for the album,
    never one per track.
  * **fill-only, like every writer here.** A `MEDIA`/`RELEASECOUNTRY`/
    `CATALOGNUMBER` the user typed survives an import — so an album the app
    mis-detected keeps the word it had until this step states the release's
    medium itself. The only two values an import may change without them being
    empty are the ones the app already sharpens:
    `DATE`/`ORIGINALDATE` (a year gains its day) and `RELEASECOUNTRY` (a strict
    subset of the release's country list gains the rest) — both by
    `mlo.autotag._mb_replace`, the same rule as the Auto Tagging stage. The
    import's result carries `release_identity: {written, skipped, failed}`.
  * **bitrate / format is NOT a tag and is never guessed.** The card's third
    chip and the album page's Format row come from `server.tagcache`'s read of
    the audio itself (codec, bitrate, depth, rate — mutagen/ffprobe), so they
    appear the moment a file is readable and no writer has anything to store;
    `ENCODER_PROGRAM`/`_QUALITY`/`_VERSION` stay Optimize FLACs (3)'s for the
    same reason. A value nobody measured is empty rather than invented.
  * **the album ENTRY reads the pressing while it is pending.** A framework
    album has no tags to carry anything, so `server.library._pending_album_row`
    fills `meta.MEDIA`, `RELEASECOUNTRY`, `CATALOGNUMBER`, `LABEL` and
    `RELEASESTATUS` (and the row's own `media`) from the identity block the ADD
    recorded on the framework marker — `server.pending_albums.create` writes the
    release payload it resolved into the marker's own `release` block
    (`library._pending_release_identity` reads it), with no extra request.
    `track_count` stays 0 and
    the tracklist stays `expected_tracks` (the release's own, every row
    missing): a placeholder never claims a file it does not have, and the
    format/bitrate cell stays empty until audio exists.
  * **pinned by**: `tools/test_import_pipeline.py` (a specific release writes
    country + medium + the release's identity; a value already there is kept; a
    landing of a different pressing writes the LANDED pressing's facts and the
    asked-for one appears nowhere; the readout then shows the pressing and the
    measured format) and `tools/test_add_to_library.py` (the pending tile's
    readout).

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
  failure: its title cell held the name AND its marks in 220 px
  against ~200 px of controls, so the name lost and the view read as empty
  200 px-tall rows. The title floor is 280 px, and a regression is caught by
  measuring the rendered table — `tools/check_library_tables.cjs` asserts, for
  every view, that no text-bearing link is under 40 px, no row is over 120 px
  tall, every header label fits its column and every column holds its widest
  value.
- **R105 — the library filters on the advisory.** The toolbar's Filter menu
  carries the presets (Failing, CD rips, Digital,
  Instrumental, Music videos, No lyrics) plus one FACET, with the count of
  the rows it would leave:
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

- **R106 — a page names an artist the way every other page does.** The library
  payload carries each artist row's folder
  `name` AND its `display_name` (`server/library.py`: the artist's own
  ALBUMARTIST tag when the albums state one, else the folder basename with its
  MusicBrainz disambiguator stripped — `strip_mbid_suffix`). Everything that
  PRINTS an artist reads the display name: Home's Top artists shelf, the
  Artists table, and the album rows' fallback when a file carries no
  ALBUMARTIST (a folder is named `Radiohead [<mbid>]`, and the raw basename was
  what these surfaces used to show). The folder identity stays `name`/`path`,
  so links keep pointing at the same rows.

- **R218 — a details menu fits the window, and every action it lists is
  reachable.** The clamping is the `Popover` primitive's own, for EVERY panel,
  not a per-caller `max-h-`: fixed mode caps a panel to the room its trigger
  leaves — vertically `maxHeight: calc(100dvh - top - max(8px,
  env(safe-area-inset-bottom), var(--mlo-inset-bottom)))`, or the room ABOVE the
  trigger for `placement="top"`, so a top-placed panel is never bounded by the
  space below it (`100dvh` so the visible height is not counted twice) — and now
  horizontally as well, because a left-aligned panel used to run off a narrow
  window (the Force flyout landed at left 216 + width 240). In-place mode is
  bounded by the same recipe in CSS (`.popover-panel`: `max-height:
  calc(100dvh - 1rem)`, `overflow-y: auto`, `overscroll-behavior: contain`), and
  a panel that would still leave the window is re-rendered in the
  measured/portalled form before paint. `OverflowMenu` therefore defaults to
  `fixed` and drops its old `max-h-[70vh]`, and `OptimizationPage`'s Force
  flyout is `fixed` + `anchorRef`; callers that used to carry their own
  `max-h-`/`overflow-y-auto` (QueryBuilder, MusicBrainzPage) dropped them,
  because two places deciding the same bound is how they end up disagreeing.
  The cap is what makes the panel scroll INSIDE the window: the app shell is
  `h-dvh overflow-hidden`, so a panel running past the fold was unreachable, not
  merely clipped — the reported "the menu is cut off", whose fix cannot be
  another `overflow-y-auto` (the panel already had one). The long menus keep the
  app's own thin scrollbar (`index.css`, no `scrollbar-hide` anywhere) and
  `overscroll-contain`, so the tracklist behind them does not move with the
  wheel. A ROW's menu and an
  album's readout both carry the file action the pages' headers have —
  **Export…** (the pages' own `ExportDialog`)
  — because "export this" must not mean opening another page first.
  `tools/check_menus.cjs` walks the sidebar and the cover menu's geometry (its
  `sheet`, `pagemenu`, `flyout` and `lyrics` groups), and
  `tools/check_responsive.cjs` measures the panels at 390/834/1440.

- **R250 — the details menu is GENERATED from the script registry, and a script
  the registry does not classify fails a test.** `GET /api/script-menu`
  (`server/script_menu.py`, mounted in `server/main.py`) is the menu's one
  source: every script in `server.script_runners.RUNNERS` with its label and
  description (`mlo.scripts.SCRIPTS` — never a second copy of the names), its slot
  in the Run All order (`order`, `in_order`), its feature switch (`gate`:
  the config keys that skip it and the run's own sentence, `enabled` false when
  all of them are off), and its force flags (the SHORT keys
  `/api/run` accepts, from `_FORCE_KEYS` + `_FORCE_ALIASES`). What the menu may offer is DERIVED, not typed into it: `applies_to`
  follows the script's own work unit — a FILE-scoped script (1, 3, 6, 11, 12,
  13, 16, 17, 21, 22, 23) applies from every kind of selection, a
  FOLDER-scoped one (2, 4, 5, 7, 8, 9, 10, 14, 15, 20) only where a folder is
  in hand, which is album, artist and library (`KINDS` =
  album/track/artist/library, `_FOLDER_KINDS` the three). So an album's menu
  offers **21** entries and a track row selection offers the **11** file-scoped
  ones, computed
  from the table rather than counted by hand — and the section is headed by a **Run all N scripts** entry (N is
  `ids.length`, so the label and the request cannot drift): the chain's own
  order scoped to the entity, posted as ONE `POST /api/run` over the menu's own
  targets after a confirm. `run_all.by_kind`/`run_all.order` EXCLUDE the
  OPT-IN scripts (`script_runners.OPT_IN_SCRIPTS`: 22 submits to AcoustID's
  public database and is never swept up by a menu button, even if a user put it
  in their own order) and the payload NAMES them under `run_all.excluded` with
  the reason; `tools/check_script_menu.mjs` pins that 22 appears in no posted
  body. The force flags are one entry PER FLAG the registry knows, not one per
  script: `force.options[]` carries `{key, config, owner, owner_label}` beside
  `force.keys`, so 10 (whose flag is the composite `force_accurip`/`force_cue`/
  `force_lyrics`/`force_auto_tag`) offers all four, each labelled with the pass
  it re-runs ("9 · AccurateRip"), a flag with no short spelling `/api/run`
  accepts is reported with `key: null` rather than dropped, and every
  single-flag script keeps its one forced twin. A registry id with no scope is
  reported in `unclassified` and offered everywhere (fail OPEN) — and
  `tools/test_script_menu.py` fails on it, which is the rule that makes the
  table complete rather than aspirational (it also asserts one force option per
  `_FORCE_KEYS` entry, and that the option set is exactly what
  `web/src/lib/force.ts` can send). The set is served in the stack's own
  order (`scripts.sort` on `order`, then the id) and `web/src/components/
  TagActionsMenu.tsx` renders one entry per applicable script — with a forced
  twin where the script owns one flag — and every entry runs `POST /api/run`
  over the entity's own paths (the selection's files for a file-scoped script,
  the album/artist folder for a folder-scoped one, `web/src/lib/scriptMenu.ts`).

### 7.17 The first-run setup wizard asks only what is required

- **R107 — the wizard's steps are the ones that need an answer.** Five:
  Folder (the music library), Account (the login gate), Tools (the dependency
  download), Keys (the source credentials and cookies: Spotify, Discogs,
  Last.fm, RYM, AcoustID) and
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
- **R107a — setup is one surface, and it never wears the library.** The wizard
  renders BARE: while `first_run_done` is false, and when it is re-run from
  Settings, `/setup` is rendered OUTSIDE the shell — no sidebar, top bar
  or live event socket — and while the config query is still
  pending the app holds a setup frame instead of painting the shell first (the
  shell used to mount on the first frame and be torn down for the wizard a
  beat later, which is library UI flashing through first run). The pre-shell
  screens — the backend chooser, the client wizard, the login/claim screen and
  this wizard — draw the same centered frame, the same `PageHeader` idiom and
  the same `SetupRail`, and every one of them renders the toast stack, so a
  message raised during setup ("Install finished with N failure(s)", a refused
  sign-in) is seen where it was raised instead of being stored on a screen
  with no stack to show it. The Tools step draws the Dependencies table with
  the page's own wrapper (`table-scroll`, the page's radius) at a width that
  fits its five columns.

### 7.18 An import that needs a hand is an outcome, and it reaches the user

- **R121 — an import that needs a HAND stays an outcome, in two places.** An
  album an import could not finish raises `import_needs_data`
  (`server/imports.py`'s `_report_gaps` → `server/import_autonomy.py`'s
  `raise_prompt`, over `mlo/import_policy.py`'s `gaps`, i.e. the grader's own
  issue codes), and that ONE fact is published where a person actually meets
  it:
  - **the tray and the OS popup** — `import_needs_data` is in
    `OS_KINDS` (`web/src/lib/notifications.ts`), so the desktop shell
    raises a system notification and the bell's panel keeps the entry;
  - **the event frame** — the same outcome on `/ws/events`, with `link`,
    `album_path`, `reason` and the missing `families`; its `action: "manual"`,
    `action_link` opens the import wizard at that album's first missing step
    (`/import?album=…&step=…&missing=…`) — an item to press, not a line of
    text.
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

### 7.20 The library layout: what it tolerates, and what the optimize pass removes

The canonical library is `<music>/Artists/<Artist>/<Album>/<files>`, and the
layout module's job is to say where a library is not that — then, for what the
folder itself proves, to settle it. ONE scan answers every surface (script 20
and the stored report the Library page warns from, read through
`GET /api/library/layout` and `GET /api/library/layout/report`), so their
numbers cannot disagree, and ONE apply does the work (script 20's own run).

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
  kept, then a reload, because every cached query and the event
  socket are keyed on being signed in. A server with no users says so and points
  at Settings → Security rather than showing an empty list. Signing out is
  available from the same panel.

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
  inside the folder and travels with it, so it stays the same framework album.
  Only a folder
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
  longer raises out of an import: the album is in the
  library, so its cover step, its chain and the rest of the pipeline still run,
  with the files that stayed behind named in the report — a partial landing is a fact
  about the album, not a reason to lose it. And `import_done` is emitted where
  the pipeline is actually finished, after the gap phase and its prompt
  (`['import_started', 'gaps', 'prompt', 'import_done']` — the order the
  notification seam is asserted to observe), because the notice is what a user
  reads as "done": it may not speak one step early.

### 7.30 The script chain's wall clock: what is shared, and what is measured

- **R216 — the notification a client missed is STILL THERE when it comes
  back, and a kind's reach is the same on every client.** `?since=` replays from
  two stores: the in-memory ring (`_MAX_EVENTS` 100, the live-event ring) and a
  durable log beside the app state (`server/events.py::_log_append`, newest
  `_LOG_KEEP` = 400 frames, atomically rewritten past `_LOG_MAX_BYTES`),
  because a client that is closed for a day — and a desktop shell in
  particular — otherwise hears nothing about the import that finished
  overnight. `recent()` merges both, dedupes on `seq` and returns the newest
  `limit`; a client that has never seen an event still asks "from now"
  (`eventsUrl` sends `Date.now()/1000`), so a fresh install is not greeted with
  a hundred notices for things that happened before it existed.
  `tools/test_notifications.py` asserts the frame
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

- **R252 — the provider calls share ONE client, the DR pass stops probing what
  it already holds, and a converted file's length comes from the container it
  just wrote.** Three costs, each paid once per file or per request before:
  * **one process-wide `httpx.Client`** (`server/httpclient.py`): a fresh
    `httpx.Client()` construct+close measures 333 ms here (it builds an
    `ssl.SSLContext`, 55 ms of it), and a library-wide pass that makes ~100
    provider requests paid that setup for connections it then threw away.
    `install()` points the module-level `httpx.get`/`httpx.post` at the shared
    client, so the call sites keep their spelling — they are the seam the
    provider suites replace — and `keepalive_expiry` means an idle process (or
    the next test in a suite) never inherits a live socket. Response cookies are
    DISCARDED (`extract_cookies` overridden): a provider's `Set-Cookie` must not
    ride into another provider's request. Measured by
    `tools/perf_pipeline.py` (it patches `httpx.Client.__init__`/`send` and
    prints `Nc/Mr` per script): script 8 over 12 albums went from
    `120c/120r`/106.52 s to `1c/120r`/73.13 s, the chain from 106.57 s to
    73.18 s (−31 %), with an IDENTICAL output manifest
    (`tools/perf_http_before.json` → `tools/perf_http_after.json`).
  * **the DR pass asks the handle it already holds**: `mlo.dr.handle_channels`
    reads the channel count off mutagen's stream info for the file the pass
    opened anyway, and `measure_track_detailed` probes with ffprobe only when
    the container states none (0) — one process spawn per track removed on a
    library-wide run. `mlo/loudness.py` passes `dr.handle_channels(af)`. On the
    local pair (`tools/perf_before_local.json` → `tools/perf_after_local.json`,
    the same 12 albums × 5 tracks) script 7 went from 3.69 s to 2.20 s, and
    every AUDIO file in the output manifest is byte-identical between the two
    runs — the only four entries whose hashes move are that album set's
    `.accurip` files, which the external CUETools pass writes itself.
  * **a converted file's duration is read from the container just written**:
    `mlo.flac._convert_lossless_source` opens the output for the tag copy anyway
    (`AudioFile(tmp)`), so its own stream info answers the duration check that
    used to be a second ffprobe on a file this pipeline had just produced; a
    container mutagen cannot read still falls back to the probe. The import's
    convert step measured 0.413 s → 0.302 s in the same pair.

### 7.34 The Browse sheet reads the payload's names, not the disk's spellings

- **R227 — the sheet's columns are the app's own facts, never the disk's
  spellings, and its first request sorts by what the toolbar shows.**
  `mlo.query` stamps every returned row with the artist and album **folder**
  basenames, and the Browse sheet printed those stamps in two columns every
  other page fills from the payload: `Radiohead [a74b1b7f-…]` and
  `[Album] 1994-11-29 … {GB - CD …} [Parlophone] [<mbid>]` where the rest of the
  app shows *Radiohead* and *The Bends* (R106). The payload's
  `album_artist || display_name` and its `ALBUM` tag now lead, with the stamps
  kept as the fallback for a row the payload does not carry. The sort had the
  same shape of bug: the request fell back to `library.path` while
  `/api/library/fields` was still in flight, so the first page came back in
  file order under a header that read
  *Artist*; the fallback is now the option the `<select>` actually paints
  (`GROUPS[0].id`, the "Artist" grouping key). Verified in the browser against
  a decorated-folder fixture: the cells read `Radiohead` / `Amnesiac`,
  and the captured `POST /api/library/query` bodies show
  `{"sort":{"key":"artist"}}` before the catalogue lands and
  `{"sort":{"key":"artist.name"}}` after it — never `library.path`.

### 7.36 A rip log that carries a checksum has to be checked, and shown

- **R229 — Logchecker's `checksum_ok` is only evidence when something actually
  checked.** The phar does not compute an EAC log's SHA256 itself: it shells
  out to the pypi `eac-logchecker` script and prints **`Checksum: checksum_ok`
  either way** — measured on a real EAC 1.3 log with one digit of "Peak level"
  changed: `Score 100`, `Checksum: checksum_ok`, and the fixture's own verifier
  computed a different SHA256. The verifier was in neither the image nor the
  Dependencies page, so `mlo.discs.check_log_checksum` fell back to that word
  and returned `ok` for a doctored log, and the app's "a log that carries a
  checksum must verify" rule was unenforced on every install. Now:
  `eac-logchecker==0.8.1` is in `server/requirements.txt` (the image) and in
  `mlo.fetchdeps.PIP_PACKAGES` (the Dependencies page, so a bare-metal install
  can add it), the phar's word is read only alongside a real answer — the
  "checksum not validated" notice or `mlo.discs._eac_helper_on_path`, which
  asks the same question the phar asks itself and holds for a log the phar
  cannot even parse — and a claimed-but-unchecked checksum reads
  **`unverified`**, never `ok`. `mlo.grader` maps it to `UNVERIFIED` (per disc
  and as the album aggregate, ordered FAKE > UNVERIFIED > REAL > NONE) and
  `mlo.audit` names it as an unevaluable leg, so the file keeps the verdict its
  CRCs earn instead of being blamed for a helper nobody installed. Verified
  against three copies of a real log (untouched / one digit changed /
  structurally edited): without the verifier `ok`·`unverified`·`unverified`,
  with it `ok`·`invalid`·`invalid`.
- **R230 — the log itself is readable in the app.** `GET /api/log/report`
  (album folder or `.log`, `disc=N` for a disc set; read-only) returns
  Logchecker's own report — ripper, version, language, score, checksum word,
  its `Details:` lines and the raw output — plus this app's checksum verdict
  and the log's decoded text, capped. It is the answer to the owner's report
  ("I don't even know a way to view logs") because a score alone never said
  why: an album whose copies are one scene rip fails on every candidate with
  Logchecker's own arithmetic (`−30` an EAC older than 0.99, `−10` gap
  handling), and those lines are where a deduction explains itself. The panel
  (`web/src/components/LogReport.tsx`) opens from the album readout's **Rip
  log** section and from a track's readout, shows `available: false` as "no
  scorer installed" rather than a score of zero, and carries a disc switcher
  when the folder holds several logs.
- **R231 — a rejected candidate names Logchecker's own note.** The reason was
  `score 60 is below the required 100`, seventeen times over, with
  nothing about the deduction and no way to look at the log: `_score_logs` now
  carries the phar's `Details:` lines (minus the "could not find EAC
  logchecker" notice, which is about this machine and already said by the
  checksum state), and `_log_fail_reason` writes
  `<name> — Logchecker <score>/100, required <min_score> (<its own note>)`, or
  the checksum verdict when that is the cause. The bar stays what it was —
  `grade_log_score_threshold`, **100 by default** (the owner's rule) — and the
  reason names it so the way out (lower it, or pick another release) is one
  screen away.

### 7.37 The library page says what is failing, and the tools install themselves

- **R232 — Home and the Library open with the grading verdict, and it names
  things.** `GET /api/grades/summary` (and the same object as `grade_warning`
  in the Home payload, so Home needs no second request) returns whether the
  library passes, the totals the Home header prints (`pass_count` over
  `total_checks`, one sum, so the strip can never contradict the percentage
  beside it), and — when it does not — the findings themselves. **A library
  that passes says `All checks pass` and no number**: the count is a fact about
  the checks, not about the library, and it is printed where it is read (the
  Home header's percentage). An install with every check switched off is the
  one other thing the strip can say — `No grading checks to report yet` — since
  "all checks pass" would be a claim about a library nothing looked at.
  **Nothing in the strip may read as a perfect pass while it lists a finding**,
  which is what the two things printed beside the list have to obey: the
  percentage is printed to one decimal and **held below 100 whenever any check
  failed** (`pass_count < total_checks`) — a library failing one check in ten
  thousand reads `99.9`, not the `100.0` the old rounding produced next to the
  album that failed it, and exactly 100 is printed only by a library with no
  failed check at all — and the rule is ONE function, `mlo.grader.printed_pct`,
  used by every surface that prints a percentage (the Home header's library
  score, an album row's `Fail · N% of checks passed`, the strip), because they
  are read within inches of each other and two of them rounding differently is
  the contradiction again — and the headline is a sentence with the complement
  its verb needs: `1 album falls short of the library's grading checks` / `2
  albums fall short of …`. A code that says nothing a reader can act on is printed
  with the grader's own instruction (`AcoustID id missing (run Fix AcoustID
  pairs)`, `mlo.grader`'s message) rather than bare (`acoustid id`). The
  owner's
  rule decides the shape of a finding: **one failing track in an album is shown
  as the track, two or more as the album** (carrying `failing_tracks` and the
  union of their codes), and a failure the grader recorded against the folder
  itself always makes an album row carrying the grader's own sentence — EVERY
  such sentence, in fact: the row prints the first (`reason`) and its tooltip
  names them all beside the tag codes (`reasons`), because an album can fail
  several album-wide checks at once and the one a reader went looking for was
  not always the first on the list. An album-wide sentence the strip once
  buried is the case that asked for it: with another album-wide sentence ahead
  of it the strip named everything else and never that. Three
  albums are never findings: a PENDING framework album (nothing was graded
  because its audio has not arrived), an album with `total_checks` 0 (which
  passes by the grader's own rule, `0 == 0`), and **an album a live job holds**
  (`server.job_locks.busy` — the claim is on the album's folder, on a folder
  above it or on a file inside it, the registry's own containment rule): a chain
  writes an album across its steps, so the tag a later step has not written yet
  is missing right up until that step runs, and a strip that named it would be
  reporting the run rather than the library. An album an **import** is on right
  right now — `server.imports._importing_now`, the same two claim kinds
  (`import`, `scripts`) — is that same finding held back, and it
  is **counted
  apart** in `albums_importing`: the summary line says so in one clause (`1
  album being imported is left out` / `2 albums being imported are left out`)
  rather than leaving a reader whose failing album just left the list to wonder
  which album the strip lost. The count is of the FINDINGS left out that way
  (graded, failing, mid-import), so a passing album or one whose audio has not
  arrived is never in it. `albums_failing`, `tracks_failing`,
  `items` and `more` are the findings that are LISTED and a busy or mid-import
  album is in none of them, while `pass_count`/`total_checks`/`grade_pct` stay the library's
  own sums including it, so the strip never disagrees with the header printed
  beside it. **The strip cannot stay stale through a run**: `["gradesSummary"]`
  is refetched whenever the client's own lock list (`web/src/lib/locks`, the 2 s
  poll behind the row's *Script run* chip) changes its held-path signature —
  never once per poll tick, or the summary would be refetched forever. Each row
  links to the album (`albumRef`) or the track (`trackRef`), the list is capped
  at 12 with the rest as a `+N` link into the Library's existing Failing
  filter, and
  `["gradesSummary"]` is in `invalidateLibrary`'s list — a run that graded,
  tagged or imported just changed the very checks the strip reports. **The
  Refresh buttons are held to the same rule**: they re-walk the music folder
  SERVER-side (`?refresh=1` drops the library and home caches),
  so the reads derived from that walk are re-asked with it — the page's own
  payload AND `["gradesSummary"]`, whose 5-minute staleTime otherwise left the
  strip quoting the counts from before the press (the owner's "even after
  pressing Refresh this warning doesn't get updated"). `tools/check_page_states.cjs`
  pins it as the request claim it is: after each press, both the payload with
  `?refresh=1` and the summary have been asked. **A long
  list opens folded**: past three findings the strip shows the first three and a
  real button (`aria-expanded`) reading *Read more — N findings*, and *Show
  less* folds it back, so seven failing albums cannot push the page's own
  content below the fold. The summary line is always first and is never folded:
  it is the answer, and the list is what you open when you want it.
- **R233 — the tools install themselves, and the cookie imports are named
  where the keys are.** `dependencies_auto_update` is **ON by default**: the
  app's checks are worth what the tools behind them are, and a user who never
  opens the Dependencies page would otherwise run a library whose log scoring,
  DR measurement or AccurateRip evidence silently did nothing. Installs stay in
  the dependencies folder (never system-wide) and the pass is capped at one
  every six hours; unticking it stops the next pass immediately, which is what
  `mlo.fetchdeps.auto_update_enabled` promises (its own fallback matches
  `mlo.config`'s default, so a config written before the key existed does not
  read as "off" while the page shows it ticked). And the Keys surfaces name the
  programs behind the sources: RateYourMusic's row points at the
  **Netscape-format `cookies.txt`** import for `rym_cookie` (a browser
  extension's export, imported into the app's own jar), whose help text names
  the Netscape file as well as the
  devtools header. One limitation, stated: while the first-run wizard is up,
  `/settings` is not routable (App.tsx's first-run gate), so the wizard's Keys
  step NAMES both imports and the clickable jump appears once setup is finished.

### 7.38 Exporting what you chose, stopping it when you want, and seeing it

- **R234 — the export menu offers one toggle per file family, and `.accurip` is
  its own.** The file selection is `server.exporter.FILE_FAMILIES` — the ONE
  table the menu, the run's copy pass and the run's report all read — and
  `.accurip` was bundled into `log` (a single "rip log and accuracy report"
  switch). It is now `accurip`: its own label ("AccurateRip report (.accurip)")
  and hint, its own `_EXTRA_REASONS` entry, its own checkbox in both surfaces
  (the panel renders the table, so the family appears without a UI change), and
  `LEGACY_SIDECAR_FAMILIES` gains it so a caller still sending the old
  `sidecars` boolean copies exactly the files it always did. One toggle per file
  family: audio, cover, lyrics, cue, log, accurip, checksum, text and other.
- **R235 — a running export can be stopped, and it keeps what it wrote.**
  `POST /api/export/cancel` asks the in-flight run to stop (answering
  `cancelled: false` when nothing was running, rather than pretending). The
  pass checks the flag BEFORE each file, so a stop lands on a file boundary and
  never mid-file; everything already written STAYS (an export is a copy
  service — deleting finished files because the user stopped the run would
  destroy what they may still want), and the finishing passes are skipped,
  because a manifest, an album ReplayGain pass and `prune` all
  describe a COMPLETE export. The result carries `cancelled: true` and the
  route answers `ok: false`. The flag is cleared at the start of every run, so
  a press between two exports cannot arm the next one. Pinned by
  `tools/test_export_dialog.py` (a run stopped during its first file: one file
  written, `cancelled`, and the next run unaffected).
- **R236 — an export holds its destination as well as its sources.**
  `POST /api/export` claims the source tracks AND the destination root
  (`_export_claim_paths`), so two exports into one drive answer 409 instead of
  racing — one run's `_copy_once` overwriting the other's files while its
  `prune` deletes them. A destination outside the library never collides with a
  script run, so export-B-during-import-A stays legal (disjoint paths), which is
  what "exporting while other albums import" needs.
- **R237 — the shell draws one progress bar per producer, labelled, until that
  producer ends.** One store field and one slot meant a run and an export on
  screen together overwrote each other, and the client cleared the bar 2.5 s
  after any frame that looked complete — so a long job's bar blinked off
  between steps. A frame now names its producer (`job`/`kind`/`label`, read
  from the job registry's thread-local by `job_locks.frame_identity`), the shell
  keeps a map keyed by that id, and an entry leaves the screen on ONE fact:
  `{"type": "progress_end", "job": …}`, sent by `job_locks.release`. Each row
  shows its label beside the bar; the row whose kind is `export` carries a
  Cancel control that calls the route above. A frame with no identity (a chain
  ticking between two scripts) still draws on the single legacy bar, so an older
  producer never loses its bar — and that bar now EXPIRES: a total-complete
  frame plus 2.5 s of quiet, or a minute of quiet on its own, drops it from the
  map (the owner's report was bars that stayed on screen for good, an id-less
  producer having no `progress_end` to be ended by). A NAMED job is exempt from
  the quiet rule — a stalled export still holds its locks and keeps its bar, and
  clears on the 5 s "absent from `/api/jobs/locks`" sweep instead. The Beets
  step's own ticks carry its job when one is held (`server/beetscfg.py`
  publishes through `job_locks` rather than calling the relay raw), so the
  Beets page's import ends its bar like every other job.
- **R238 — a duplicate sidecar is dropped, not copied twice.** A sidecar the
  app writes can land in two places — the pre-organize staging folder and the
  album folder an "Add to library" prepared — and the organizer's leftover
  sweep used to carry the second copy in under a ` (2)` name. Now the sweep
  DROPS a source file whose bytes are identical to the file already at the
  destination (`filecmp.cmp(..., shallow=False)`), so the library never holds
  two copies of one sidecar; a genuinely different file still takes the ` (2)`
  name rather than overwriting it. The canonical file is never the one moved:
  it is what every reader opens.
- **R239 — a column preference cannot gut a table, and an artist row wears
  a face.** The Library's Albums / Artists / Tracks views draw every data cell
  behind a visible-column id list persisted per view in `localStorage`
  (`useColumnPrefs`), and that list is **versioned with the ids**: a list under
  the previous key is MIGRATED — the ids it still carries are kept, and every
  column this build ships visible by default is restored — because a list
  written before an id existed cannot be evidence that the user hid it. (An
  unversioned key that kept only the ids it recognised drew the owner an Albums
  view with its chevron and no album names, a Tracks view with its row numbers,
  and a Columns menu in which nothing looked wrong.) The Artists view draws
  `ArtistAvatar` per row — a representative album cover; the app stores no
  artist pictures — and its count column is labelled
  **Releases** (the artist's albums in this library, pending ones included).

### 7.43 The Import tab takes archives, folders and single files

- **R259 — an archive imports like the folder it contains, and unpacking it is treated as the untrusted input it is.** A dropped or picked archive (`.zip`, `.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`, `.tar.xz`/`.txz`, `.7z`, `.rar` — `mlo.archives.ARCHIVE_FORMATS`, mirrored by the wizard's `accept=`) is unpacked into a staging folder and then goes through the *existing* album detection, partial marking and sidecar rules: a rip in a zip and the same rip as a folder produce the same album folder and the same `.mlo_expected.json`, so a `.cue`/`.log`/`.accurip` inside an archive is used exactly as one on disk. Extraction refuses, **before writing anything**, a member with an absolute path, a `..` segment, a drive/UNC prefix, a symlink/hardlink, a device, a fifo or an unknown type, and the refusal names the member; a format with no available extractor (`.7z`/`.rar` without 7-Zip) is refused in those words rather than silently skipped. A nested archive is not unpacked and does not become an import file. The wizard states what it took in (albums, tracks, refusals) before the user commits.
- **R260 — a drop takes files, nested folders and archives, and a single file is a first-class pick.** The picker and the drop path accept one file, several files, a folder (walked recursively), an archive, or a mix of them in one gesture; in the desktop shell an OS drop that yields a server-side path resolves through the scan path, and a client with no access to the server's filesystem says so instead of doing nothing. One audio file imported on its own lands by the rule in R247 — in the album that rip is, or as its own album when it genuinely is one.

### 7.44 The library says which kind the lyrics are

- **R261 — `lyrics_kind` is `synced`, `plain` or absent, and it is the stored truth.** The library payload (and `GET /api/album/scan-tracks`, and the query field `library.lyrics_kind`) carries the kind, derived from what the file actually holds (`mlo.lyrics.stored_lyrics_kind` over the embedded lyrics and the `.lrc` — one source with timestamps makes it `synced`; no source makes it absent, and `lyrics_present` is exactly "the kind exists"). Every lyrics surface names the kind (Synced / Plain) instead of only "has lyrics". A **plain** lyric is a failing state — the red ✗ with the reason naming the setting — exactly while `lyrics_allow_plain` is off (the shipped default), neutral while it is on, and a setting that cannot be read never claims a failure. A missing lyric is its own state, not "plain". The mark appears where the surface is about ONE track — its page, its details row, its lyrics views and the import wizard's Lyrics step — and **never on a list of tracks** (an album's tracklist, the library's rows): there it was clutter from the day it shipped. The marks keep their homes on the track's own surfaces. Plain lyrics stay READABLE and editable where they land: the track page's lyrics pane opens its RAW editor for untimed words (`LyricsViewer` sets `rawMode` whenever the incoming text parses to no lines — the host hands the stored text straight in, so without that branch a plain track showed "No lyrics yet…" with its words right there, un-syncable), and the Raw/Lines toggle never wipes a plain text (`serializeLrc([])` is "", so the textarea is only re-seeded when there are lines to seed it with).

### 7.45 A digital-media import settles what the grader would otherwise report

- **R262 — SOURCE and the lyrics format are settled by the import, which asks only for what it cannot know.** For a release whose medium is Digital Media: `SOURCE` is written from the release's own store URLs (the `purchase for download` / `download for free` / `streaming` relations, `mlo/digital_source.py`), else **asked** through the import-policy family `source` (the wizard's Match step, `POST /api/import/source`, the unattended prompt) with `digital_media_source_value` as the suggested answer — and written fill-only, never over a stated value, and only where `should_write_audio_tag` allows. The lyrics formatter (script 1, `mlo.lyrics._process_lyrics_for_audio`) runs as part of the import, so "Lyrics not optimally formatted" cannot survive it; a lyric that arrives untimed (with `lyrics_allow_plain` off) or that the app's own grade still rejects after the formatter is removed together with its `.lrc` sidecar and derived translations, **with the count reported**, and only when the chain's fetch step will replace it (never when script 13 is not in the chain, never when the user keeps the lyrics family, never when `lyrics_allow_plain` is on). 

### 7.46 AcoustID submission is opt-in and follows the service's own rules

- **R263 — script 22 submits fingerprints to AcoustID, and only what the service can accept.** A submission needs `acoustid_user_key`; a fingerprint with **no** MusicBrainz recording id is refused with `NO_RECORDING_ID` (AcoustID's own rule — the only exception is the credential probe `verify_user_key`), and the recording id is taken from `ACOUSTID_ID`, then `MUSICBRAINZ_TRACKID`, then the single unclaimed bracketed UUID in the file name (R243's rule). The fingerprint comes from `ACOUSTID_FINGERPRINT` or is taken locally with the bundled `fpcalc` when the file carries none — a CD rip the file itself names must be submittable. A pair the service already links is never re-sent: the app asks `v2/lookup` (at any score, not `acoustid_min_score`) and keeps a local ledger of accepted and pending pairs (`<music>/.mlo/data/acoustid_submissions.json`). Batches respect the service's 100-entry limit, the answer is reported per file (accepted / already known / rejected with the service's words), a path the user names is what is submitted (a file path does not expand to its album), and **22 is never in the shipped Run All order or the default chain** (`server.script_runners.OPT_IN_SCRIPTS`): upgrading an install must not publish anything by itself, while a value the user saved keeps it.

### 7.47 A verdict is only drawn from a payload that loaded, and a whole-library action asks first

- **R264 — states do not lie.** A page's verdict is computed only from a payload that actually loaded: loading and failure are their own states, a failure says so (with the way to retry) and never accuses the user's data. An action that rewrites or moves the whole library (**Apply fixes** → the layout script) confirms first, naming what it will do and how many rows it covers, and only the confirm reaches the endpoint. A hero folds its own layout at a narrow width, and a table column is laid out to hold its own header and the widest value it actually renders — where a value genuinely cannot fit, the cell clips with the whole value in its `title`, checked by `tools/check_library_tables.cjs`.

### 7.51 An album title wears the advisory its FILES state

- **R293 — the boxed E/C mark is read from the strongest statement in the
  album, and the grid cards draw it too.** `AdvisoryMark` (the boxed letter
  beside a title) was drawn on the album page but NOT on `AlbumCard` — the
  surface a reader scans a library by — and the value the page gave it came from
  the album-level tag alone, which lags the tracks inside it. Measured on the owner's library:
  Evil Empire carries `ITUNESADVISORY 0` on the album while TEN OF ITS ELEVEN
  tracks state 1, and Hail to the Thief carries 0 with three of fourteen at 1,
  so covers for plainly explicit records said nothing. `albumAdvisory`
  (`web/src/components/Badges.tsx`) is the one rule now, shared by the card and
  the album page: any track at "1" makes the album explicit (what every store
  does with a compilation), a "2" is read as clean only when nothing in the
  album says otherwise, and "0", a blank or no tag at all draws NOTHING —
  `AdvisoryMark` renders only the two states it has a symbol for. On that
  library it marks four albums (Evil Empire, Hail to the Thief, Nonagon
  Infinity, Rage Against the Machine) and leaves the six clean ones bare.
  Proven by `tools/check_library_az.mjs`, which serves a library carrying both
  shapes — one album explicit only through its tracks, one clean all the way
  down — and asserts the mark on the card in a real browser (54/54 checks).

---

### 7.55 The import queue: one chain, one row, and a notice only when it ends

- **R307 — one chain per album.** An album the pipeline is already on (a
  `job_locks` claim of kind `import` or `scripts`) is never
  chained a second time: the import queue and the manual finish both
  route through the same check, so the scripts run exactly once for one album
  (the owner's "the scripts seem to run twice"). `tools/test_import_pipeline.py`
  counts the invocations.

- **R308 — a release that needs a music video to be complete is never the
  default pick.** `mlo/release_choice` ranks a video-carrying edition below an
  audio-only one that covers the expected tracklist, and a video-only edition
  below both. A video RELEASE (a concert film) still ranks as what it is.

- **R309 — a notice means the work ended.** The end of a WHOLE import is the
  only frame that raises an OS popup
  (`web/src/lib/notifications.ts`'s OS kind list): one album of a bulk run
  and a chain still running stay in the app. Every one of them names the album —
  artist, album, year — never a folder name or a path with UUIDs in it.

- **R311 — the import's step is named for the work it is doing.** The chain's
  progress text is the album-scoped label (`Remux videos (MKV) + 20 more` for a
  purely audio album is exactly what this rule forbids): a script that provably
  cannot apply to the album is dropped from the chain before it runs, and the
  import's own `_phase` reads are published only when the step really happens
  for this album (no genre frame with no genre source, no instrumental frame
  with the step off). `tools/test_import_pipeline.py` asserts the sequence.

### 7.56 A verdict is not drawn while the album is still being imported

- **R312 — a mid-import album is left out of the verdict, and said so.**
  `GET /api/grades/summary` asks `server.imports._importing_now` (the same
  `job_locks` claim the import path reads) and skips those albums before counting, so
  Home's strip and the Library banner cannot report an album as failing the
  checks its own import chain is still filling; the payload carries
  `albums_importing` and the banner prints that clause instead of silently
  losing a row (the owner's Ænima, reported "falls short" mid-import).

### 7.57 The tables fit their data

- **R313 — a table's floor is its columns, and a title never collapses.**
  Album tracklists carry per-column pixel floors (`web/src/lib/columns.tsx`,
  `AlbumRow`, `TrackTitleCell`) so the wrapped cell cannot squeeze the title to
  0 px; below `md` the album page's table scrolls as a whole instead of wrapping
  each cell, and unnamed/oversized columns fold rather than crush their
  neighbours. The album row's name column is `w-32 md:w-[220px]` for the same
  reason. `tools/check_library_tables.cjs` holds the floor.

- **R370 — a layer that fills the WINDOW leaves the shell's own title bar
  alone.** The desktop shell is undecorated and draws its bar in the app's
  normal flow (`web/src/components/TitleBar.tsx`), so everything laid out
  INSIDE the shell starts below it — but a `fixed inset-0` surface anchors to
  the window, and one that starts at `top: 0` paints its own top row behind the
  window controls. That is where the music-video layer came out on Windows.
  The bar publishes its one height as `--mlo-titlebar-h` on `:root` while it is
  mounted (`TITLEBAR_H`, `index.css`), and every such surface carries `.shell-top`
  (`top: var(--mlo-titlebar-h, 0px)`); the docked lyrics view's own floor adds
  the same term. The variable is 0 in a browser — the host draws the chrome
  there — so that client lays out pixel-identically.

### 7.58 The pipeline's wall clock is measured, and what was measured

- **R315 — the pipeline's own cost is a number, and the cheap wins are in.**
  Measured on `tools/perf_pipeline.py` (which already times every script
  through the app's own runner), the 4.2 changes were: `/api/run` invalidates
  the tag cache for the run's own targets (and each mover's `moved_targets`)
  instead of the whole library, the chain resolves the album list ONCE and
  hands it to every script that would otherwise walk the tree again, the
  replaygain store keeps its map in memory with an mtime check and a debounced
  write instead of rewriting the file per measurement, `fpcalc` answers are
  memoised on `(path, size, mtime_ns)`, the exporter's preflight hands its
  parsed sources to the worker instead of re-opening every file, and the layout
  pass asks "does this hold audio below" with a short-circuiting walk. Numbers
  and the harness command: `docs/release-notes/release-notes-4.2.0.md`.

### 7.59 An alias is a name

- **R316 — aliases are imported, shown, searched and required, and
  only where they are needed (R16a/R16b).** MusicBrainz aliases arrive from the
  same release request as everything else (`inc=aliases`) and are written as
  `TITLEALIAS` / `ARTISTALIAS` / `ALBUMALIAS` — AT MOST ONE value per entity,
  the name `server.integrations.alias_for` picks for the configured `locale`,
  and only when the entity's own name is written in a script that locale cannot
  read (`alias_required`): a `-<locale>` suffix is part of the vocabulary (it
  names the language an alias is for) but the app writes no spelling but the
  configured locale's own. The library payload carries the alias BESIDE the
  original (`track.alias`, `album.alias`, `_artist_display_name`), the search
  bar's haystack and the query catalogue both see the alias tags, the advisory
  and lyrics lookups ask the ORIGINAL name first and re-ask under the alias
  only when the first states nothing. Grading fails both directions: `grade_check_alias_needed` fails a name
  that needs an alias and has none, `grade_check_alias_excess` fails an alias
  nothing needs (or a spelling the app does not write), and the family is in the
  tag allowlist so a legitimate alias is never an excess-tag failure.

### 7.60 A lyric is words, not headers

- **R318 — the stray header and the identity credit line are not lyrics, in the
  file and in the grade.** Script 1's canonicaliser (`mlo/lyrics.py`,
  `format_lyrics_text`) drops the whole family of dead lines a download leaves
  above the words: the ID3v2 unsynchronised-lyrics frame's own content
  descriptor `[id:…]` in every spelling and value (`[id:$00000000]`, `[id:]`,
  `[ID:…]`) together with the descriptive headers
  `[ti:]`/`[ar:]`/`[al:]`/`[au:]`/`[by:]`/`[re:]`/`[ve:]`/`[length:]`/`[la:]`
  (`LRC_DROP_RE`), and a LEADING line that merely repeats the track's own
  identity — `TITLE - ARTIST` as well as `ARTIST - TITLE`, read from the file's
  real tags, case/dash/spacing-insensitive (`_is_identity_line`,
  `_norm_identity`). `[offset:…]` is the one header that is an INSTRUCTION a
  client applies, so it survives formatting; it is still a header for the
  presence question, so `has_lyrics_text` (`LRC_META_RE`) counts a file that
  holds only an offset or only `[id:…]` as having no lyrics. Both predicates
  live in `mlo/lyrics.py` and are asked by the writer (`_process_lyrics_for_audio`
  carries `TITLE`/`ARTIST` in the cfg view the formatter reads) and by the
  grader (`_lyrics_formatted` takes the same tags from the file it is grading),
  so **script 1 clears exactly what the grade flags** — no second, drifting
  rule. This is the existing `grade_check_lyrics_format` check (its
  idempotency comparison: re-running the formatter must change nothing), with
  the existing `grade_check_lyrics` presence check reading the corrected
  `has_lyrics_text`; **no new check key is introduced, so the count stays 67**.
  `tools/test_lyrics_fix.py` holds the case: a file carrying
  `[id:$00000000]`, an `[ar:]`-style header and a matching `TITLE - ARTIST`
  line formats to the timed lines only and grades as one failed check, the
  same file with an unrelated prose line keeps it, `[offset:-250]` survives,
  and script 1's own pass turns the failing fixture into the passing one.

  **The same predicate settles a track that says it is instrumental.** A file
  whose OWN `INSTRUMENTAL` tag states 1 has no words by its own definition, so
  a `LYRICS` tag or `.lrc` left behind (the empty/stub/whitespace tag, or text
  an earlier lookup stored before the track was recognised as instrumental) is
  a contradiction, not a fact: script 1's `_process_lyrics_for_audio` clears
  BOTH stores on such a file, and the file's classification is kept. This is
  not a writer overwriting a fact it did not establish — it removes text the
  file itself says cannot be there — and it is the opposite direction of
  `fix_instrumental_from_lyrics`, which still flips a tag whose lyrics the
  script cannot clear (a filetype whose LYRICS writes are switched off). The
  grade agrees: the lyrics presence check already asks only `INSTRUMENTAL=0`
  tracks, and the format block now skips `INSTRUMENTAL=1` entirely, so an
  instrumental is charged nothing for a leftover its own script is about to
  clear (R4 — a check that does not apply is not counted), while
  `grade_check_instrumental` keeps naming the contradiction for as long as the
  text is there. `tools/test_lyrics_fix.py` asserts both halves:
  `INSTRUMENTAL=1` + leftover `LYRICS` tag + 0-byte `.lrc` grades 0/0 (nothing
  reported), script 1 removes both and keeps `INSTRUMENTAL=1`, and the same
  leftover on an `INSTRUMENTAL=0` track still fails the format check and is
  NOT cleared by the script.

- **R347 — the lyrics verdict names its reason, and it names a script only when
  a script can fix it.** `grade_check_lyrics_format` fails for several different
  reasons, and the message used to be one string for all of them —
  "Lyrics not optimally formatted (run Lyrics script)" — which was advice that
  could not work for two of the families: a file whose stored words carry NO
  timestamps (an untimed arrival the install keeps because it is not in
  `import_review_families`, `lyrics_allow_plain` off) and a file whose timing is
  coarser than `lrc_sync_level` are both conditions no formatter can invent
  timing for — script 1 rewrites what is stored and never adds a timestamp
  (R330's own boundary). Running it left the failure exactly where it was (the
  owner's report: "Running format lyrics script doesn't fix this error"). The
  verdict now collects the failing condition per branch and says what it was:
  - the reasons the formatter REPAIRS keep the script's name and carry the list
    ("Lyrics not optimally formatted (run Lyrics script) — the stored text is
    not in the configured form"; "the first line does not match the [00:00.00]
    rule"); `_lyrics_formatted` is the formatter's own idempotency predicate, so
    "the formatter would change this text" IS "this is repairable";
  - the reasons nothing can repair are said as such ("Lyrics cannot be repaired
    by a script: …") with the way out named instead: an untimed lyric (no
    timestamps, `lyrics_allow_plain` off) points at fetching a synced version
    with the track's own **Find lyrics** — its "Use these lyrics" is the manual
    route that DOES replace what is stored (`fetch_one(replace=True)`) — or at
    turning on "Accept plain (unsynced) lyrics" in Settings → Lyrics & CUEs; a
    line-synced lyric under a WORD or SYLLABLE `lrc_sync_level` at fetching a
    word-synced version; stacked timestamps under Extended LRC at a version with
    one line per stamp; and out-of-order word timestamps at fetching again (no
    formatter re-orders words — an invalid word-timestamp line the formatter
    WOULD change is still the repairable half).
  Both halves are one issue and one `LYRICS` code, as before. Pinned by
  `tools/test_lyrics_fix.py` (an untimed leftover on an `INSTRUMENTAL=0` track
  fails with the plain reason and is left alone by script 1), `tools/test_arrived_lyrics.py`
  (a kept untimed arrival's message names the plain state) and
  `tools/test_import_pipeline.py` (the finding is asserted as a FINDING — either
  wording — never as one spelling).

### 7.61 The AI answers what every other source was silent on

- **R319 — a lyric-less track nobody could answer for is asked of the AI, and
  only a lone 0/1 is believed.** `server/instrumental.detect_instrumental`
  gains `ai` as its LAST source (`server/instrumental._ai_answer`, evidence key
  `AI = "ai"`), asked at most once per track, under three conditions that must
  hold at the same moment: no other source stated anything (the merge would be
  None — an answer a provider already gave is never second-guessed), the file
  carries NO lyrics at all (the existing `_lyrics_present` predicate: a LYRICS
  tag with real text or a real `.lrc` sidecar — lyrics ARE the evidence of
  vocals, so a track that has them never costs a call), and
  `instrumental_ai_classify` (ON, one model call per lyric-less track) with a
  configured `ai_base_url` + `ai_model`. The prompt
  (`server/instrumental._AI_SYSTEM`) demands ONE digit and nothing else — 1
  instrumental, 0 not — and the reply is parsed STRICTLY: a lone 0/1 after
  trimming is the answer, written the way the app writes INSTRUMENTAL (the
  `0`/`1` tag, through the same `should_write_audio_tag` gate every writer
  uses) and attributed to `ai` in the reply's evidence, so the readouts say the
  model answered rather than LRCLIB. Anything else — prose, a hedge, a digit
  inside a sentence, an empty reply, or a failed/timed-out call — writes
  NOTHING and records why (`answer["ai"]`, e.g. "ai call failed (TimeoutError)
  — nothing written"), which is what keeps the import path
  (`server.imports.fetch_instrumentals`, the detector's caller) from guessing
  and from crashing: the file keeps the state it had and the import continues.
  No call at all for a track with lyrics, one a source already answered for, an
  unconfigured endpoint, or the switch off. `tools/test_instrumental.py` holds
  every case, exercised through the import's own writer.

### 7.62 A stored preference is never evidence about this build

- **R320 — a persisted view preference can never blank a view or invent a scroll
  bar.** Two layers, both in `web/src/lib/columns.tsx`. The visible-column list
  is keyed by version (`mlo-cols4-*`) and a list from an older key is MIGRATED
  rather than trusted (its surviving ids kept, every column this build ships
  visible added), because a prefs entry cannot be evidence about a column that
  did not exist when it was written — and a list that would leave the table
  nothing but furniture (the row number, the cover) is replaced by the view's own
  defaults AND re-stored, so the menu, the table and the next toggle agree. The
  stored WIDTH map is sanitized on read (`sanitizeWidths`): only finite numbers
  inside the range the drag handle itself can produce (40–900 px) survive, so a
  `0`, a string, a legacy shape or a non-object falls back to the column's own
  floor instead of painting it 0 px wide in a `table-layout: fixed` table. The
  owner's four symptoms came from exactly this class (Albums → artist headers
  over empty rows, Tracks → a `#` column and nothing else, no covers, an
  unnecessary sideways scroll on Artists).

  Sanitizing is not enough on its own: `table-layout: fixed` takes the sum of a
  table's columns for the table's own floor (CSS 2.1 §17.5.2.1), so a map whose
  every value is inside the handle's own 40–900 clamp — nothing `sanitizeWidths`
  may drop — still drew a 1420 px Artists table in a 1200 px box and a 3848 px
  album tracklist in the same box (`mlo-colw-album-tracks` = {title, genre,
  bitrate, dur} at 900): the owner's "columns are VERY long ... rows seem really
  wide". A stored width is therefore a preference about how a table's width is
  SPENT, never a floor the table IS that wide: `useFittedWidths` (same file)
  reads every column's own floor off the table itself in one layout pass, gives
  each column that floor first, and shares what the box has left over among the
  columns the reader sized, in proportion to how much MORE than its floor each
  one asked for — `shown(i) = floor(i) + (stored(i) − floor(i)) × room / Σ
  excess`. The table then renders exactly as wide as it would with no stored
  widths at all: `w-full` where the floors fit, and the floors' own sum where
  they do not — the one case that still scrolls (R313). One hook, wired to the
  library's Albums/Artists/Tracks views, the album page's tracklist, the
  library's expanded tracklists and the cached tables; the floors are read, never
  duplicated as a second table of numbers. Pinned by the hostile-prefs cases in
  `tools/check_library_az.mjs` (each seeded through an init script before the app
  loads: the table's width, its scroll AND every column are the clean table's)
  and by `tools/check_library_tables.cjs`, which measures the album tracklist at
  1440/1100/801/390 px in both states.

  A list under the CURRENT key is not evidence either, and the reason is sharper
  than the key's own: a list of three ids looks exactly like a reader who once
  unticked four columns. The owner's album tracklist read three columns
  (`mlo-cols4-album-tracks` = `["num","cover","title"]` out of seven) behind a
  Columns menu that listed all seven ticked — that menu draws the list the hook
  RETURNS — so nothing said where the other four had gone, and obeying the list
  kept it that way. What is stored is therefore the reader's CHOICE, as a
  versioned record beside the list (`mlo-coldft-*`): `{v: 2, removed, added}` —
  which of the columns this build SHIPS visible they took away, and which
  columns it does not ship (the `defHidden` built-ins, and their own tag
  columns) they put there. The drawn set is DERIVED from the record — every
  shipped column minus `removed`, plus `added`, in the table's own order — so a
  column a later build starts shipping appears by itself, a column this build no
  longer has is simply not there, and no stored list can quietly subtract a
  column the reader never touched. A record WITHOUT that version (the v1 shape,
  which was a fingerprint of a list; a hand-edited key; no record at all) is
  UNKNOWN rather than a choice: it is the same MIGRATION the v3 key gets — the
  ids the list still has are kept, every column this view draws by default is
  added back (the reader's own tag columns included), and the result is
  re-recorded as v2, once. An untick after that is
  a `removed` entry and holds across reloads for good (showing the column again
  takes it out of `removed`). Pinned by `tools/check_library_tables.cjs` at
  1568 px: the three-id list draws the clean table's columns with a v1 record
  and with none, both are rewritten as v2, a v2 record that removed `dr` keeps
  `dr` hidden on the next load, the owner's `mlo-colw-album-tracks` 900s hide
  nothing either, and a one-line row is the CLEAN row's height — 52 px at 100 %,
  the cover cell's own 32 px box plus `.td`'s 20 px of padding, which is why the
  ~70 px of the owner's screenshot is that row at the app's own zoom
  (`mlo.zoom` 135 → 70 px exactly), not a box in the row.

### 7.63 The album badge names where its files came from, and the artist wears its own verdict

- **R321 — a `Digital Media` album's badge names its SOURCE.** The badge order is
  fixed: medium · source · countries (`Digital · Bandcamp · US`, the medium in
  the badge's own short form — `mediaShort` prints the medium's first word, so a
  `Digital Media` release wears `Digital`, exactly as a `Compact Disc` one wears
  `CD`), built by `mediaSourceLabel`/`mediaCountryLabel` in
  `web/src/components/Badges.tsx` from the album payload's `source_summary`
  (`web/src/types.ts`). A source that only repeats the medium (the app's own
  `Digital` default when nothing stated where the files came from) is dropped
  rather than printed twice, `INCONSISTENT` — the server's word for files that
  disagree — stays with the album page's Source readout instead of reading as a
  shop, and every surface that wears the badge — the library's cards and the
  album page header — names the same release the same way.
- **R322 — an artist's own verdict is a dot beside the name, and its hero fades
  rather than cuts.** `mlo.grader.grade_artist` is what the artist's checks are
  (that the folder holds an album at all), the library payload now carries that verdict
  per artist row (`server/library.py`), and every place an artist's NAME is
  drawn — the artist page's title, the library's Artists view, Home's artist
  shelf — draws `web/src/components/ArtistName.tsx`'s mark for that
  verdict: the green dot when it passes, and the amber warning when it FAILS,
  instead of a chip spelling the check out (the artist page has room and
  still writes it out). A failure drawing NOTHING was the owner's complaint —
  "make sure the library displays some sort of warning / grade error" — because a
  list that stayed silent made a failing artist look like one
  nobody had graded; the warning's tooltip names the failing check the way the
  page's chips do (`label — reason`), and an artist the payload never graded
  still draws neither mark ("not looked at" is not "failed"). The hero's blurred
  cover backdrop carries the `.hero-ink` mask (a radial gradient ending
  transparent, the same idiom the app's other hero surfaces use), so it fades out instead
  of ending on a hard edge. Pinned by `tools/check_library_az.mjs` (the dot for a
  passing artist, the warning mark — with the missing check in its tooltip — for
  a failing one on every surface, the two chips gone, the counts kept, and
  the blur layer's box and computed mask measured).

- **R376 — an artist card is condensed, its face is a SQUARE, and its count
  is in RELEASES.** The owner's screenshot of Home's "Top artists" shelf: an
  80 px CIRCLE, a name and a count in a 2/3/4/6 grid, which filled a screen
  with six artists. The shelf is now the same information at half the box —
  `grid-cols-3 sm:4 md:6 xl:8`, `gap-2`, a `p-1.5` card, a 64 px tile and
  smaller text (a card measures 113 px tall) — and the tile is a
  SQUARE (`rounded-lg`), the geometry the artist page's own hero tile and every
  album card already draw: the circle was the only round image in the library,
  and the owner asked for "more rectangular like how album covers are". The
  Library's Artists view carries the same square (`rounded`, 32 px) beside the
  name, so an artist looks like itself everywhere. The count reads
  **"N Release(s)"**: `releaseCount` in `web/src/lib/fmt.ts` is the ONE
  pluralizer, so the shelf caption, the artist page's subtitle ("1 Release · 0
  tracks") and its section heading (`RELEASES`) cannot disagree about it — the
  Library's own Artists column has used "Releases" since it was added
  (`ARTIST_COLS`).
  `tools/check_responsive.cjs` holds the geometry; hunting it also found the
  check's own word-width probe broken — `getComputedStyle().font` is EMPTY for
  a font it cannot represent as a shorthand, and the probe then measured a
  10 px caption against a 16 px word and called "1 Release" in a 45 px box
  crushed. It now builds the font from its parts when the shorthand is empty.

### 7.64 One knob decides the lanes, and only independent units get them

- **R323 — `worker_limit` is the ONE thread setting, and a script parallelises
  only over units that share nothing.** Settings → General → "Worker threads"
  (`worker_limit`, `mlo/config.py`, 0–64) is read by every script through
  `mlo.stats.worker_count(config, maximum=…, items=…)`. `0`/unset means "use the
  machine": the pool is `mlo.stats.usable_cores()` lanes (`process_cpu_count`,
  so a container's affinity mask is honoured), capped only by the script's own
  declared ceiling (`maximum` — its worker's native tool already saturates the
  disk, or its work is nested inside another pool and must not multiply the
  budget, R79) and by the number of items it actually has; an explicit number is
  the user sizing the pools themselves and is never clamped by a ceiling.
  `thread_budget` reports the same machine number and `tool_threads` divides it
  among the lanes, so `lanes × threads per lane` adds up to the machine — not to
  lanes × cores.
  A loop may be pooled only when its iterations SHARE NOTHING: each unit writes
  its own path (one file's tags, one sidecar, one image, one remux temp file),
  and everything shared is either left to the runner thread that consumes the
  pool's results in submission order (stats counters, the progress bar, the log
  order, a whole-file JSON map) or computed AFTER the pool has finished (album
  gain, album DR, a grade). A lane never increments a `stats` counter: two
  lanes' `+=` lose updates, which is why the remux counters moved to the booking
  loop. A script that cannot meet that stays serial and says why.
  What the shipped scripts do (all widths from the setting, all ceilings stated):

  | # | script | pooled unit | width |
  |---|---|---|---|
  | 1 | Format lyrics | file; album for the MEDIA/SOURCE pass | cores; ceilings 64 / 16 |
  | 2 | Format CUEs | `.cue` file | cores, ceiling 64 |
  | 3 | Optimize FLACs | FLAC file (`flac.exe`) + conversion file (ffmpeg) | cores; each encoder takes `tool_threads` |
  | 4 | Grade | album (its tracks inside the lane) | cores, ceiling 16 |
  | 5 | Process images | image file (cjxl/oxipng) | cores; each encoder takes `tool_threads` |
  | 6 | Audit library | tag read · CD CRC · integrity · rip-log scoring · AudioAuditor | ceilings 8/16, nested CD decoders share one album's budget |
  | 7 | DR & ReplayGain | album (`rsgain`), then the album's TRACK decodes inside that lane | album lanes ceiling 8; tracks = budget ÷ lanes, each ffmpeg `-threads` its share |
  | 8 | Auto Tagging | album, then per TRACK for the decode-bound mood/genre stage | ceiling 8 |
  | 9 | AccurateRip | CD album (`ffmpeg` WAV decode per disc) | ceiling 8, per-disc decoders share the album's budget |
  | 10 | Format all | sidecar/sidecar-family task | ceiling 16 |
  | 11 | Remux videos | video file, or a whole disc structure as one input | cores; each ffmpeg gets `tool_threads` |
  | 12 | Key & BPM | track (tag scan, then librosa analysis) | ceiling 8, `bound_numeric_threads` |
  | 13 | Fetch lyrics | track (network + write) | ceiling 8 |
  | 14 | Beets tagging | — one `beet import` child owns one SQLite library | serial |
  | 15 | Release tracklist | — one rate-limited MusicBrainz request per album | serial |
  | 16 | Mood & Energy | track (tag scan, then librosa analysis) | ceiling 8, `bound_numeric_threads` |
  | 17 | Lyrics transliterate (AI) | file (own tags/sidecar) | ceiling 8 |
  | 20 | Optimize library layout | plan lane (scan), per artist | ceiling 16; the apply pass is serial (rename → move → trash rebases the paths the next row names) |
  | 21 | Fix AcoustID pairs | file (`fpcalc` + lookup + its own tags) | ceiling 8 |
  | 22 | Submit fingerprints | file (`fpcalc`), the dedupe/batch stays in order | ceiling 8 |
  | 23 | Optimize tags | file (excess tags read and stripped per file) | ceiling 16 |

  Deliberately serial INSIDE a pooled script: `mlo/cue.py`'s album rename
  pre-pass (renaming a `.cue` changes the path the other cues are keyed by),
  `mlo/images.py`'s cover-rename pass (at most ONE image per folder may take
  the cover name), `mlo/format_all.py`'s cue-repair pre-pass and its four family
  phases (each phase is ordered behind the last), `mlo/audit.py`'s CD-format
  tag reads and its INTEGRITY fill (each fill re-files the shared evidence map
  under the file's new size/mtime stamp), `mlo/grader.py`'s per-track loop
  (one pass accumulating the album's checkbook: issues, counters, the deferred
  AUDIT resolution), `mlo/acoustid.py`'s candidate lookups (paced by the
  service's own throttle, not by the CPU). Pinned by the pipeline suites
  (`tools/test_remux.py` accounting, `tools/test_dynamic_range.py` album DR,
  `tools/test_script_optimizations.py`, `tools/test_config_ui_parity.py`).

### 7.67 An instrumental has no lyrics verdict

- **R327 — the album and track pages are the app's page width, and a column
  floor is a MEASURED value.** Every page in the app is
  `mx-auto max-w-[1600px]`; AlbumPage and TrackPage were the two without it, so
  the tracklist was the one surface that stretched to the window — at 2560 px
  the table was 2320 px wide with the name column alone taking 1712 of it,
  which is the reported "columns are way to long, atleast on album pages, also
  rows seem to wide … columns should auto-fit to the space on screen". The
  name column is deliberately the row's one flexible column (R313), so the page
  width is the right lever: the free width is the page's, and the page is the
  same 1600 px the Library's tables get. The genre column's floor is the other
  half of that report: 96 px could not hold "Rock; Garage Rock" (148 px at the
  table's font), so it wrapped onto three lines and made every track row 80 px
  tall — the floor is 160 now, measured the way the other floors are (widest
  value + a few px of slack), and the row is 52 px again.
  `tools/check_library_tables.cjs` holds both: a 2560 px window may not widen
  the tracklist past the page width, the name column must be exactly what the
  wrapper has left after every other cell, and a one-line row keeps a one-line
  row's height.
- **R328 — an instrumental track carries no lyrics verdict, anywhere.** The app
  hides an instrumental's stored words (the instrumental state wins over the
  text), so no surface may grade them: a stored PLAIN text on a track tagged
  `INSTRUMENTAL=1` was reported with the full failing vocabulary — the red
  cross and the sentence "this track should hold a synced version" — on the
  track page's own chip row and in the stored readout (`TrackDetails`), a
  demand no instrumental can satisfy ("it shouldn't say 'x plain' for
  instrumental tracks", reported). Both surfaces now say what the app does
  instead ("Instrumental", "instrumental — stored lyrics stay hidden"), and the
  derivation lives once (`Badges.isInstrumental`) for the sidebar, the page and
  the readout. The lyrics-kind chip keeps its
  meaning everywhere else (a plain text in the EDITOR, and a staged file during
  an import, are statements about that text, which is what those surfaces are
  for).

### 7.68 The level decides the re-encode, and a run fills rather than replaces

- **R329 — the encoder LEVEL decides a re-encode; the encoder's VERSION does
  not (any more).** `ENCODER_VERSION` named the encoder BINARY, and the skip
  checks compared it, so every tool upgrade re-encoded every track — hours of
  CPU for a tag nothing reads once it matches, on a large library the whole
  night. The shipped default is OFF for all four formats since v4.4.0 (it can
  be turned back on per format in Settings → Encoder Tags), and the compare is
  gated on the marker being ENABLED (`mlo.tools._version_meets`, the mirror of
  `containers._quality_meets`), so a marker that is switched off can never
  re-encode a file forever for a value nothing rewrites. A stored `true` —
  what the old default itself wrote into every saved config — follows the new
  default exactly once (`normalize_config`, keyed by
  `encoder_tags_version_default_moved`), while a `true` written after that move
  is the user's own choice and stays. The LEVEL is what the checks ask about:
  a file encoded at a lower effort than `library_codec_quality` (or
  `jpegxl_effort` / `images_jpeg_quality` / `png_optimization_level` for the
  image passes) asks for is the only thing that makes a re-encode worthwhile.
  Disabled markers are also removed from a file the re-encode rewrites
  (`containers._clean_flac_tags` generalises the v1.4.2 ENCODER_PROGRAM rule),
  so a later re-enable cannot compare a value written under other settings.
  Pinned by `tools/test_script_optimizations.py`'s "level only" check (an old
  version alone does not re-encode; enabling the marker does) and the PNG
  identity check, which now asks for the marker it asserts.

- **R330 — a lyrics run FILLS; it never replaces words a file already holds.**
  Script 13 (Fetch lyrics) has no force flag any more: a run
  asks the whole chain for the tracks that hold no words (embedded or a real
  `.lrc` sidecar) and re-tries what nobody could answer, while a stored text —
  an import's answer, a provider hit from an earlier run, a person's own edit —
  is a fact of the file and not a cache of the search that found it. This is
  the owner-reported damage (issue #74: "sometimes, I've had lyric tags
  overwritten"): `force_lyrics` on the fetch script re-asked every provider for
  the whole library and overwrote what was there. `force_lyrics` still means
  the FORMATTER (script 1, and script 10 last), which only writes when the
  canonical text actually differs. Replacing one track's words is the manual
  route's job — `POST /api/lyrics/auto` with `force`, which is `fetch_one`'s
  `replace` parameter and the only caller that passes it. An `INSTRUMENTAL=1`
  track is never fetched by ANY caller, force or not: the file states there are
  no words, so a hit could only be written and deleted again by the very next
  pass, churn that reported "ok" while the file stayed silent. Pinned by
  `tools/test_script_optimizations.py` ("fills only": the stored text survives
  a forced run byte for byte, only the track without lyrics is searched, and an
  instrumental is never searched) and by `tools/test_import_corrections.py`,
  which asserts the route passes its `force` through as `replace` while the
  chain does not.

  The boundary, said out loud because force is not one thing: a force switch
  whose SCRIPT IS the asking — 8 `force_auto_tag` ("AutoTag re-run"), 15
  `force_tracklist`, 17 `force_xlit` — still asks again by
  design, which is what its label names, and each writes only the families it
  owns (genre/advisory/`INSTRUMENTAL` for 8, the manifest for 15, its own
  transforms for 17). What no forced pass does any more
  is write a value that would come out identical (R331), replace stored LYRICS
  (here), or re-encode for an encoder VERSION the settings no longer track
  (R329).

- **R331 — a forced pass writes only what changes.** Force is "re-run this
  pass", not "rewrite this file": a value already exactly what the pass would
  write is left alone, so a forced *Run All* no longer rewrites — and with
  `flac_no_padding` on, whole-file re-encodes — every track whose tags and
  sidecars are already canonical. Script 10's tag pass and its `.lrc` cleaning
  both compare before writing (`mlo/format_all.py`), which is what makes a
  forced run of a library that needs nothing finish with "0 formatted, N
  already correct" instead of N rewrites. The cost this removes is real on a
  large library: every tag write on a padding-less FLAC is a full re-encode of
  the file it was only going to trim.

### 7.69 Pages draw the same in every engine, and a page load does not re-read the library

- **R333 — the album page's column floor is a WIDTH, and it computes the same
  in all three engines.** The floor idiom was `w-full md:min-w-max` on a
  `table-layout: fixed` table — a definite width AND `min-width: max-content`.
  Gecko's intrinsic pass for that pair returns its unconstrained sentinel
  (measured in a five-element document: 17,895,698 px for `width: 100%|620px|
  100vw|62em`, and 738 px — the real floor — with `width: auto`), so the album
  page came out as a 17-million-pixel ribbon in Firefox with every column to
  the right of the name sitting off screen; WebKit does not implement the floor
  at all (the name column measured 188 px in an 860 px wrapper whose floor is
  280 px, and 0 px at 620 px). Chromium was right, which is why every
  measurement the repo had agreed with itself. The floor is now stated as
  `width: max-content; min-width: 100%` (`table-fit`, carried by `TABLE_FIT`
  and `ALBUM_TRACK_MIN_W`), with `.table-fit td { max-width: 0 }` at `md`+ so
  the floor is read off the HEADER row rather than the widest body cell (a
  no-op in all three engines: a fixed layout takes its column widths from the
  first row), and `useFittedWidths` pins inline widths while it measures so the
  fit cannot collapse a stored column. Pinned by
  `tools/check_firefox_album.cjs`, which drives the album tracklist in Firefox,
  Chromium AND WebKit and compares the geometry (59/59 on the fixed tree,
  48/60 with the pre-fix constants). Every other table in the app shares the
  same floor, so this one rule covers the Library, Browse, Export and Music
  Brainz pages too.

- **R334 — a page load does not re-read the library, and a write is never
  served stale.** Reading an album (mutagen open + tag copy per track, the
  cover decode, the log/sidecar reads, the grade verdicts derived from them) is
  the expensive half of every page, and it was recomputed per request and
  thrown away per process — a 170-file library in a container took **8.80 s
  per `GET /api/storage`** (polled every 60 s by the Home page card) and
  **5.59 s per cold `GET /api/grades/summary`**. The app now keeps:
  `server/tagindex.py` (a persistent per-album payload store keyed on every
  entry of the album folder as `relpath|mtime_ns|size`, plus a per-album stamp
  of the app-state stores whose content reaches a payload, plus the whole
  config),
  a single flight for the library payload's FIRST build and
  stale-while-revalidate after it (R338), a short-TTL memo for the album
  and artist routes, a 60-second storage snapshot refreshed BEHIND the request
  (stale-while-revalidate) and walked once at startup (`_lifespan`'s
  `storage-warm` thread, so even the FIRST poll after a restart answers from
  memory — measured 6.97 s → 0.051 s on the owner's install), and stat-keyed
  memos for ffprobe probes and the
  cover colour. The contract these must keep: a row is served ONLY when its
  identity matches exactly (a changed, added or deleted file misses; a settings
  change is unreachable), every in-app write drops what it invalidated through
  the existing `tagcache.invalidate_*` hooks (~60 call sites), a payload belongs
  to the BUILD that wrote it — `tagindex.payload_stamp()` carries the app
  version beside the payload's shape, so an upgrade that changes a grading rule
  drops every row and re-grades instead of answering with the old verdict (the
  owner's case: a fixed "Unrecognized MEDIA value: HDCD" stayed on Home and the
  Library page because that album's own files had not moved) — and the index is
  never a source of truth — an unreadable or locked database, a missing folder
  or an error payload all fall back to building. Pinned by
  `tools/test_tagindex.py` (51 checks: changed/added/deleted files, a config
  change, a real tag write read back from the page, a payload written by another
  build) plus
  `tools/test_storage.py`/`tools/test_remux.py` for the memos. Two costs stay,
  and both are named: the FIRST run after an upgrade pays one full build — the
  index stamp carries the app version, so another build's payloads are dropped
  rather than served with other rules (`tagindex.payload_stamp`), and on this
  feature's own first run the index is simply empty — and a change made outside
  the app is bounded by the TTL
  (30 s on the album/artist pages, 60 s on the library payload) rather than
  seen instantly.

### 7.70 A disc is one vocabulary, and a disc folder is one album

- **One disc vocabulary** (R335). `mlo.discs` is the only place that decides
  what states a disc: `disc_dir_number`/`is_disc_dir` (a folder named `CD1`,
  `Disc 2`, `Disk1`, `Volume 1`, `DVD 2`, `CD1 [FLAC]` — decoration stripped —
  or a bare index), `bare_disc_index`+`is_disc_parent` (a bare `1`/`2` counts
  as a disc only as part of a numbered set starting at 1: `1`+`2` is a rip, a
  lone `25` is the album "25"), `disc_number_of_path` (a file on disk: its own
  name, else its disc folder). The import mover and the layout scan all call
  these; no module keeps its own disc regex.
- **A disc-subfolder release is ONE album** (R336). When a folder's
  album-bearing children are its discs (`Album/CD1`+`Album/CD2`,
  `Album/1`+`Album/2`), the mover takes that folder whole: any other
  album-bearing child (a stray log/cue folder, art, a bonus folder) rides
  along instead of splitting off as an album of its own, and nothing is
  imported as `CD1`/`CD2`. `mlo.discs.is_disc_parent` is that rule, once,
  for the import move and the layout scan.
- **Discs never share a track-number space** (R337). A file's DISCNUMBER is the
  disc its own path states, recorded fill-only at import
  (`server.imports.stamp_folder_discs`, before the naming script) so
  `%discnumber%`, the organized path and the path grading expects all read the
  same value; `(disc, position)` is the key everywhere, and no file of one disc
  may be renamed onto another's name (an untagged `CD1/`+`CD2/` release used to
  collapse into one flat list on organize, disc 2's audio overwriting disc 1's).
  Files: `mlo/discs.py`, `mlo/naming.py`, `mlo/layout.py`,
  `server/imports.py`
  (`stamp_folder_discs`), `server/main.py` (organize passes the track path +
  records the disc).
- **A disc's rows and its files may be numbered in different conventions**
  (R341). A release numbered CONTINUOUSLY — disc 2 = tracks 14-26, which is
  what the CD's own TOC states and what this app writes as `2-14 …` — against
  a manifest numbered PER DISC (disc 2 = tracks 1-13, which is what
  MusicBrainz states) shares no `(disc, position)` key on that disc at all, and
  every row of the second disc read "not imported" beside the files that were
  right there (the owner's own library: The Wall, US CD C2K 36183 — 13 rows,
  13 files, 13 false "not imported"). `mlo.paths.expected_tracks_state` takes
  the album's own audio as rows (`mlo.discs.disk_rows`: disc, track number and
  TITLE per file) and, for a disc whose manifest rows and files share NO
  position, aligns them BY ORDER — the i-th row against the i-th file — and
  only when there is nothing to disagree with: the same COUNT on both sides and
  a title that matches wherever both state one. A different convention is a
  renumbering; a different title is a different tracklist, and those rows stay
  missing. Measured on that album: 13 of 13 disc-2 rows present with the rule,
  0 of 13 without it, and 13 of 13 still missing when the titles are bent.
  Pinned by `tools/test_single_song_import.py`.
- **A rip log's checksums are matched to the files the same way** (R342). EAC
  numbers a DISC's tracks 1..N, so `CD-2.log` states rows 1-13 for the disc
  whose files are `2-14 …` — and every one of those tracks graded "Track not
  covered by .log CRC (unverifiable CD rip)" beside a log that covered all of
  them (measured on The Wall: 13 files, 13 CRC rows, 0 matched by number and
  13 by order). `mlo.discs.log_crc_map` is the ONE matcher, used by the
  grader's CRC coverage/value pass AND by the audit's `verify_album_checksums`:
  by track number first, and BY ORDER when the log's rows and the disc's files
  share NO number at all — guarded by equal counts, a number for every file,
  and the log's own TOC playtime against the file's within `TOC_TOLERANCE_S`.
  A partial overlap is ambiguous and stays uncovered; a file the log does not
  really cover is never given somebody else's checksum. Each caller keeps its
  own "which track is this file" rule (the grader reads the FILE NAME first,
  the verifier the tag) through `number_of`, so nothing else about the two
  passes moved. Pinned by `tools/test_grading_paths.py`.

### 7.71 A page load never waits for a rebuild, and a tag write is a change

- **R338 — the library tree is SERVED, and re-derived behind the page.** The
  assembled `/api/library` payload is what every page's first paint asks for,
  and its memo used to be a hard 60-second TTL with a blocking rebuild: the
  request that found it expired paid the whole walk and every album row in it,
  ON a request thread. Measured on the owner's install (170 files, bind-mounted
  library in Docker Desktop on Windows, warm reads 40-60 ms): **0.9 s, 5.5 s
  and 14.5 s rebuilds**, twice inside one minute of otherwise warm reads — the
  "pages take tens of seconds" this rule exists to remove. Three changes,
  together:

  * **Stale-while-revalidate.** `tagcache.get_library` returns the cached tree
    as it stands when it is merely old (`_LIB_TTL`) OR when an in-app write
    marked it dirty, and rebuilds it on a daemon thread
    (`tagcache._refresh_library`, single-flight, never raises, stamped after
    the build). The first paint of a COLD process still builds in-request and
    still single-flights — that is the only blocking build left.
  * **The tree is warmed at startup** (`_lifespan`'s `library-warm` thread,
    beside `storage-warm`), so the first visit after a restart is served from
    memory: measured 11.3 s cold walk → **0.13 s** on a 320-file scratch
    library, and 0.05-0.09 s warm.
  * **An in-app write drops only what it wrote.** `invalidate_path`/
    `invalidate_album` no longer clear the whole tag cache or the tree — the
    tree is marked dirty and re-derived behind the next request, and 36 of the
    45 `tagcache.invalidate_all()` call sites were narrowed to the folders the
    operation actually touched (an import used to make the next library page
    re-parse every track with mutagen). The 9 that stay global are the ones
    whose change really is library-wide: a settings save, the explicit Refresh,
    and the "the affected set is unknown" fallback in `imports._invalidate_caches`.

  The contract that pays for it, stated once: the rows a user sees immediately
  after an in-app write are the pre-write ones for as long as the background
  refresh takes (`tagcache._refresh_library`, ~1 s for a library this size),
  while every PER-PAGE payload (album, artist, grades) is still dropped by the
  same invalidation and rebuilt fresh — so the page in front of the user is
  never stale, only the list behind it can lag by one refresh. A change made
  OUTSIDE the app keeps its TTL bound.

  * **Refresh keeps the ladder, and drops only the assembled trees.**
    `tagcache.invalidate_library_payloads` is what the Library's and Home's
    Refresh buttons call: the tree (and Home) is dropped so the press's answer
    IS the fresh one, while every cache keyed on the files themselves stays —
    the tag cache by `(path, mtime_ns, size)`, an album's indexed payload by
    the folder's own signature, the grade inputs by the config and the state
    stores' stamps. The rebuild therefore re-walks the folder and re-reads
    every stat the payload depends on (verified: a file added outside the app
    appears after one `?refresh=1`, and disappears again when removed), but
    unchanged files are not re-parsed and unchanged albums are not re-graded —
    measured 29-52 ms on the 144-file scratch library against a cold rebuild of
    the whole ladder. `invalidate_all` stays for the cases that really
    invalidate everything: a settings save and the "affected set is unknown"
    fallback in `imports._invalidate_caches`.
  * **`/api/library` is serialized once per build, and answers 304.** The
    tree's own JSON bytes and ETag are derived where the tree is derived
    (`tagcache._json_document`, stored beside the payload in `_lib_body`), so
    FastAPI never re-runs `jsonable_encoder` + `json.dumps` over the app's
    largest payload per request; the route sends `Cache-Control: no-cache` +
    `ETag`, and a matching `If-None-Match` gets a 304 with no body. Measured:
    437 KB tree, ~3 ms warm, 2.7 ms for the 304.
  * **One album pool for the whole build, and one config dump.** `build_albums_map`
    builds every album of a library on ONE `ThreadPoolExecutor`
    (`worker_count(cfg, maximum=min(8, cores), items=len(albums))`); the
    per-artist call it replaces gave a folder-per-artist library — the common
    shape — a single-worker pool per album, i.e. a serial scan wearing a pool.
    The `/api/artist` reader keeps an ordered list wrapper
    (`build_albums_parallel`). The build also computes `tagindex.config_key(cfg)`
    ONCE and threads it into every `cached_album`, so the canonical dump of the
    whole config is not paid per album.
  * **The startup warm-ups are CHAINED, one walk at a time.** The library
    build runs first (`library-warm`); the storage snapshot — which walks the
    same bind-mounted library, measured 6.97 s cold on the owner's install —
    runs when that thread finishes, and Home's build waits on the library
    single-flight it shares. Three walkers on one disk was contention, not
    warming.
- **R339 — the library-state stamp is PER ALBUM.** `tagindex.dir_signature`
  used to append one library-wide stamp of the four app-state stores (audit
  evidence, artwork provenance, AcoustID submissions, AccurateRip identities),
  and the import chain rewrites those stores once per album — Audit library,
  AccurateRip and Process images all run on the album being imported. Every one
  of those writes therefore made EVERY album's row unreachable: that is what
  produced the 14.5 s and 5.5 s rebuilds above, on a library whose warm reads
  are 40 ms. `tagindex.state_stamp(album_dir, cfg)` now reads each store's own
  share: entries keyed by a path under the album, entries the album sits under
  (an artist-level record its albums all read), and — for a store whose records
  state no path at all (the AcoustID submissions are keyed by a fingerprint) —
  the whole file's stamp, which is written by the submit script alone and never
  by an import. Parsing is cached per (file, mtime, size), so a build parses
  each store once. Verified: another album's audit evidence leaves this album's
  identity byte-identical, while the album it is about changes. The rows each
  store contributes are prepared ONCE per file revision
  (`tagindex._state_rows` — folded key, raw key, JSON-dumped-and-truncated
  value, sorted), because sorting every key list and dumping every value per
  ALBUM made one build JSON-encode each store once per album (the audit store
  holds one record per audited file).
- **A tag write happens only when the value CHANGES.** Writing a container is a
  whole-file rewrite on FLAC/MP3/MP4, and the genre import set the tag
  unconditionally: every run rewrote every track of the album (8 tracks: 2.0 s)
  and moved their mtimes, which is what made the library pages after it re-read
  the album. `mlo.autotag.genre_plan(af, names, count)` is now the one decision
  — it canonicalizes and caps through the shared `normalize_genres` and
  compares against what the CONTAINER holds verbatim (so another tagger's
  `"; "`-joined value is still rewritten into repeated fields), `genre_apply`
  is the one applier, `trim_genres` is built on the same pair, and
  `server.main._write_album_genres` runs ONE pass per file (the second loop
  that re-opened every file it had just written is gone) in parallel across
  files (`worker_count`). Verified over HTTP: a second identical
  `/api/genres/import` reports `updated: 0` and moves **0** mtimes; the first
  writes 8 files in 40 ms. Pinned by `tools/test_tagindex.py` (the tree's
  serve-and-refresh behaviour, the per-album stamp) and
  `tools/test_genres.py`/`test_genre_format.py`/`test_tag_hygiene.py`.

### 7.72 The credits panel names what it is, and lists every relation

- **R345 — a release's credits are the release's, and the panel says WHICH
  release.** `GET /api/credits` (`server/main.py`, built from
  `server/integrations.py`) answers for a track (`?path=`) or a whole album
  (`?album=`) in one shape: `{artist, album, rows, source, identity}` plus the
  resolved id (`track_mbid` on the path branch, `release_mbid` on the album
  one). **`rows` are as complete as the sources allow.** An ALBUM request merges
  three relation families out of the ONE cached release response: the release's
  own relations, every track's recording relations
  (`recording-level-rels` — the album is not a second request per track) and
  every work node's own relations (`work-rels` names the work,
  `work-level-rels` inlines its composer/lyricist/writer, walked inside
  `_credit_rows`) — so a conductor stated on the release, an engineer stated on
  one recording and a lyricist stated only on the work all reach the panel. A
  TRACK request asks its recording with
  `artist-rels+work-rels+work-level-rels` and merges the same two families it
  can see, again with no second request. `tidy_credit_rows` **drops nothing but
  empty rows**: it strips and lower-cases the role, strips the name, the
  attributes and the mbid, drops a row with an empty role or an empty name,
  dedupes on (role, case-folded name, mbid, attributes) and sorts by (role,
  name) — never a curated role list and never a cap (the collectors' own
  500-row guard is the only bound). The files' own credit tags are the
  FALLBACK and only the fallback: `source` flips to `"tags"` when the MB side
  answers no rows at all (no MBID, or a release MusicBrainz describes with no
  relation), and a tag fallback is never mixed into an MB answer, so the badge
  beside the rows always says which kind of evidence they are. **The identity
  block is what the panel is titled by** — `title`, `artist`, `album`,
  `album_artist`, `catalog_number`, `label`, `barcode`, `date`, `original_date`,
  `country`, `release_type`, `media`, the five MBIDs and `path`, every key
  always present and `""` where nothing states it. An album request blanks
  `track_mbid`/`recording_mbid` (a file's TITLE names ONE of its recordings) and
  sets `title` to the release's own name; a track request carries
  `track_mbid == recording_mbid` from `MUSICBRAINZ_TRACKID`. **An empty row
  list is an ANSWER, not a 404**: the only refusals are "no such file/folder",
  "outside the music folder" and "an album holding no audio", because a file
  with no MBID and no credit tag still has a title, an artist and an album to
  name itself by — the 404 that used to answer it is what left the panel's
  header printing a raw path. The panel itself
  (`web/src/components/TrackDetails.tsx`) draws that header — the title as the
  heading, then one label/value line per fact with its own copy button, the ids
  last and the path last of all, small and dimmed — and then EVERY row the
  payload carries, grouped by `ROLE_RANK` (work and its authors, the people in
  the room, the studio, the packaging; a role the table does not name sorts
  last, alphabetically, rather than disappearing). A credit's NAME opens
  MusicBrainz — `https://musicbrainz.org/artist/<mbid>`, or the work's own page
  for a `work` row — and never a page in the app: a credit list is mostly people
  and works the library itself does not hold (a session player, a conductor, an
  engineer), and an in-app `/artist/mb:<id>` for one of them lands on a page
  with nothing to show. A row with no MBID is plain text. Both modals that
  mount the panel
  drop their old subtitle — it was the raw path — so the header is the one place
  the subject is named. Pinned by `tools/test_credits.py` (identity filled and
  offline, the work's own roles merged on both branches, the non-destructive
  tidy) and `tools/test_mb_search.py` (the tag fallback, and empty rows as a
  200).

### 7.73 Loading is near-instant: a bounded list, a small shell, and bytes the server already made

- **R348 — the Library page draws a WINDOW, and grows it as the reader goes.**
  Every view that repeats rows (Grid, Compact, Albums, Artists, Tracks) renders
  at most `RENDER_CHUNK = 300` rows of its already-sorted, already-filtered list
  and grows by 300 when the reader reaches the bottom (an `IntersectionObserver`
  sentinel, 800 px early) or presses the *Show N more of M remaining* control
  the sentinel row carries. What must NOT change: every count and label (the
  toolbar's "N albums · M tracks", the facet counts, the A–Z counts, the header
  checkboxes) reads the FULL filtered list, and *select all* still means every
  filtered row, not the drawn window — the selection lives in Sets
  (`selTrackSet`/`selAlbumSet`/`selArtistSet`) and the "all selected" flags
  compare sizes, so a 50k-track library no longer does 50k×50k membership
  scans. Changing the view, the search, a preset, a facet, the A–Z pick or the
  sort RESETS the window to 300 (it is stored with the list key it was grown
  for). The rows also carry `content-visibility: auto` +
  `contain-intrinsic-size`, so the browser skips offscreen layout/paint work.
  Measured on a synthetic 900-album / 1,800-track library: 300 cards drawn,
  toolbar still "900 albums · 1800 tracks", one press → 600, scrolling to the
  bottom → all 900; *select all* ticks the full list while 300 rows stay drawn.
  Side work follows the same rule: `tracksByAlbum`, `gridSections` and the
  albums-table rows are derived only for the view on screen, and the search
  haystacks are built from a fixed tag-key list (the `key:value` aliases plus
  the person tags) instead of every tag on every track.
- **R349 — a cover is fetched at the size it is drawn.** Grid cards ask for a
  bucket (`GRID_COVER_W`: S 160, M 320, L 640 — the user's grid size, the stored
  `mlo.gridSize` when the caller has no size control), album table rows ask for
  160, and Home's shelves follow their own grid size; the server answers from
  its disk thumbnail cache with
  `Cache-Control: private, max-age=300` (`artcache.THUMB_SIZES`). Before this,
  a card asked for the 1200-3000 px master under `no-cache` and revalidated it
  on every visit. Nothing upscales: heroes and lightboxes still ask for the
  full-size cover.
- **R350 — an album card's artist name opens the artist page.** The caption's
  artist line is a link (`albumArtistRef`: `/artist/mb:<MUSICBRAINZ_ALBUMARTISTID>`
  when the album carries one, else the containing folder — the same
  MBID-preferred rule as every other entity link, so it survives a move), on
  every surface that draws the shared album card. The status dot, the text and
  the year stay where they were, and the title keeps its own album link.
- **R351 — the shell is small, and everything else arrives on first use.** The
  entry chunk carries the shell only (React, the router, the query client);
  the docked lyrics sidebar and every page are `lazy()` chunks, and the five
  non-English locale bundles are their own chunks — `i18n` re-exports nothing
  eagerly, and a locale whose chunk has not landed yet falls back to English
  for the frame or two it takes, then re-renders in the reader's language
  (measured: the entry chunk 696.7 KiB → 486.4 KiB, 190.8 → 149.4 KiB gzip).
- **R352 — the server loads the engine on demand, and one script table serves
  every menu.** `mlo/__init__.py` re-exports the engine's entry points LAZILY
  (PEP 562 `__getattr__`), so `from mlo import __version__` — which the server
  does before it can answer anything — no longer imports grader, images, flac,
  autotag, audit, layout, loudness and their libraries (mutagen, Pillow, numpy)
  at process start; `import mlo` is a 0.02 s no-op and the engine arrives with
  the first call that needs it. The scripts' ONE table lives in `mlo/scripts.py`
  (a stdlib-free leaf module), so `server.tags_registry` — imported by the
  whole API — no longer pulls `mlo.cli`, which imported every script module for
  a dict of strings; `mlo.cli` re-exports the table for the terminal. The
  server's own route surface is split into `server/api_*.py` routers by
  surface (cover, trash, WebSockets, MusicBrainz/LRCLIB/RYM,
  export, run-scripts) over a shared `server/api_common.py`, so `main.py` is
  the app wiring and the routes that have no home of their own rather than one
  file per everything; the route table is verified byte-identical across the
  split (279 routes, 273 OpenAPI paths, same handlers).

- **R353 — a whole-disc image rip becomes one file per track before anything
  reads the album.** A folder whose `.cue` describes many TRACKs but names a
  single FILE is the shape most CD rips arrive in, and the app used to keep
  the image whole — one 400 MB file where the naming script, the per-track
  tags, the grader's per-track checks and AccurateRip all expect a tracklist.
  `mlo.cue.split_image_rip` cuts it at the sheet's OWN INDEX 01 points, the
  convention every CD splitter uses: track N runs to track N+1's INDEX 01, so
  a pregap belongs to the track in front of it, and track 1 starts at 0 so a
  hidden track before its INDEX 01 stays with track 1. The cut is a
  decode/re-encode through ffmpeg and never a stream copy — a copy cuts at a
  frame boundary and can shift a track by up to one frame (~93 ms at
  44.1 kHz) — so every track's PCM is bit-identical to the image's own samples
  at those offsets (`tools/test_cue_rename.py` pins it). ALL OR NOTHING per
  image: a failed track removes the tracks already written and leaves the
  image intact, and on success the image goes to the app's Trash (never a
  bare delete) while the `.cue` stays exactly as written — an image-style
  sheet whose FILE no longer exists is a shape the app already reads
  (`mlo.discs.fix_cue_filenames` repoints it, `_cue_matches_disc` is
  pregap-tolerant). It runs at BOTH doors into the import —
  `main._import_one_album` before the codec conversion and the organizer, and
  `imports._finish_album` before the tracklist is recorded — and is
  idempotent, so the second call is a no-op.
- **R353a — a sheet that cannot be cut is not cut.** No INDEX 01 for a track,
  or INDEX 01s that do not strictly increase, or a last track starting past
  the end of the audio: the folder imports as the image it is. A wrong cut is
  worse than no cut.
- **R354 — a grade finding reaches the notification tray from any page, and
  the strip says HOW MUCH is wrong rather than everything.** The strip's own
  rows (and the tray entry each one derives) print the failing CHECK COUNT for
  the album or track plus the grader's sentence when there is one; the check
  NAMES are one hover away (and full in the Library's Failing filter), because
  a row that printed twenty names buried the album it was about. The derived
  ingest is mounted ONCE in the app shell (`GradeTraySync`), not in the two
  pages that draw the strip: a finding is in the tray whether or not the
  reader ever opens Home or the Library, which is what "regardless of whether
  it just happened" means. It stays OUT of `OS_KINDS` — it is a
  datapoint re-read from a payload, not an outcome the server announced, so it
  must never pop a banner.
- **R355 — a run scoped to one album is named by the album's IDENTITY, never
  by the folder.** `script_runners._scope_album` answers with
  `imports.album_identity_label` (artist — album (year), off the album's own
  tags, then a framework marker), and only falls back to the folder's name
  when the folder states no identity at all. The folder was the old answer on
  the reasoning that it is what the user filed the album under; the report
  that killed it was a progress row titled "[Album; Compilation] 197…" — a
  naming-script path truncated to the point of naming nothing.
- **R356 — a bulk tag write runs its files in lanes.** `POST /api/mb/assign`
  writes each file through `mlo.atomic.rewrite_via`, which copies the file and
  lets mutagen rewrite the copy — two passes over every byte — so an album's
  tracks are the one slow, independent thing in the request. The per-file body
  is `assign_one` and the pool is
  `worker_count(cfg, maximum=8, items=len(tracks))`: one lane per file, capped
  at 8 because this is disk work on ONE album folder, and a lane per core on a
  big box would only queue at the disk. Files share nothing (one path, one
  temp, one mutagen object each) and `genre_cap` is read-only. `errors` and
  `changed` are folded in submission order, so the reply is byte-identical to
  the serial one, error order included.
- **R357 — RYM's refusal names the cookie it needs, and a 403 is checked for
  the challenge.** rateyourmusic.com sits behind Cloudflare, and only a
  `cf_clearance` cookie gets past it — bound to the SAME User-Agent and the
  SAME egress IP that earned it. So `rym_user_agent` (empty = the built-in
  Chrome UA) sets the UA the requests are sent with, and a stored cookie with
  no `cf_clearance` says so in `_rym_reason` and in the credential warnings
  the Sources panel renders, naming both halves of the pair. The status check
  reads the interstitial out of a NON-200 body too — the live site serves the
  challenge as HTTP 403, and only 200s used to be checked, so every blocked
  request was reported as "refused without a Cloudflare challenge" and the 403
  branch never used the detector at all (caught live: a 27-pair signed-in
  cookie, 403 + `<title>Just a moment...</title>`).
- **R360 — MusicBrainz genres are read at the level the entity is entitled
  to.** Album-level genres come from the RELEASE GROUP (the album is the group,
  not the pressing), and a track's come from its recording, with the WORK's
  genres merged behind it — the work is the "release group" a track does not
  have. The recording is never dropped in the work's favour: measured, works
  carry no genres at all for a mainstream album, so a work-first-without-fallback
  reading would have LOST data the app already had. Album-of-the-Year is available as a genre
  source on the same archive-only footing and is selected by adding
  `albumoftheyear` to `genre_sources` (it is not in the shipped list, because an
  archive-backed source costs seconds rather than milliseconds per album).

- **R361 — the Library's batch bar runs the import pipeline, and MANY batches
  run at once.** With albums, artists or tracks selected, the selection toolbar
  offers **Import** beside Organize: it starts the same pipeline a fresh arrival
  goes through — the pre-chain lookups (release identity, links,
  cover art, genres, advisories, instrumentals) and then the configured script
  chain (`server.imports.bulk_import` → `finish_album`) — as a JOB, so the page
  never waits for minutes of work; the running albums are the ones the
  In-progress page lists (`server.job_locks`, each chain holding its album).

  A batch started here does not queue behind the wizard's queue, or the
  reverse. The server keys jobs: `POST /api/import/bulk` answers with the job
  and `GET /api/import/bulk/status?job=<id>` answers for THAT job (without the
  id, the newest — the shape the route always had), and every payload carries
  `jobs`, what else is running. What stays exclusive is the ALBUM: a row for a
  path an import already chains is `failed` with `already_importing` and names
  the holder (`server.imports._bulk_one`), so pressing Import twice, or
  importing from the wizard and the Library at once, can never import an album
  twice.

  The pipelines all share ONE budget: `import_bulk_concurrency` (default 5,
  clamp 1–16) is the number of albums being imported at once for the whole
  process, however many batches are asking — three batches of four albums are
  four imports, not twelve. What the budget does not admit WAITS: every album
  has a row of its own from the moment a batch starts, `"status": "queued"`
  until a worker picks it up and `"running"` while it is imported, and the job
  payload carries `running`, `queued` and the pool's `concurrency`, so a
  surface can say "4 at once, 9 queued" instead of leaving the reader to guess.
  A changed setting is picked up at the next idle moment (the shared pool is
  rebuilt then, never under running work). MusicBrainz is what makes a big
  batch polite on the network side: every WS/2 request goes through the one
  one-request-per-second throttle (`server.integrations.mb_get`), so a large
  batch takes longer per album rather than being refused.
  `tools/test_import_pipeline.py` pins it: two batches at once, each answering
  for its own albums, one shared pool, and — at a width of 1 — one row
  `running` with the rest `queued` until their turn comes.

  **It ASKS which release, first** (the owner's ask: "no confusion about what
  album it is"). An import is a match, and a match can be wrong — the wrong
  pressing's identity, the wrong cover. With an album selected, Import opens
  `ImportIdentifyDialog` for the albums whose files name no release (the ones
  that DO name one, `MUSICBRAINZ_ALBUMID` in their tags, go straight through:
  there is nothing to ask), and each row offers the three ways a person knows:
  a MusicBrainz link or bare id (release or release-group — the server resolves
  it), **Detect**, which fingerprints the album with AcoustID and shows the
  release group it says the audio is (`POST /api/import/acoustid`), and the
  release's **catalogue number** (`GET /api/mb/search/releases?mode=catno`),
  whose hits are one click from being the pin. The link rides the bulk item as
  `mbid`, is resolved per album on its own worker through the shared
  rate-limited client, and a link that cannot be resolved fails that row BEFORE
  the album is moved — nothing is imported as something else, and nothing
  half-imported is left behind. A row left blank imports the way it always did.

- **R362 — the Finish step's script box carries the one-shot Force switch.**
  The wizard's *Run scripts after import* box has the same Force toggle and
  per-script picker the Optimization page uses — one component, two surfaces
  (`web/src/components/ForceRun.tsx`: `useForceRun` + `ForceControl`), so the
  two cannot drift — and both the box's **Run all** and the **Done** press send
  the resulting dict (`forceRun ? forceDict(forceSel) : undefined`, the shape
  R11 defines). Force re-runs the ticked scripts even where they would skip as
  already done; it is one-shot (browser-local `mlo.runAll.force` /
  `mlo.force.sel`), it never writes the saved Settings, and with Force OFF the
  run omits the dict so the saved switches apply — exactly the rule R11 already
  states for every import path. The report under the button says "(forced)", and
  the completion toast says so too. Pinned by `tools/test_script_menus.py`.

- **R363 — a tool that cannot REACH a file is not a verdict about its audio.**
  On Windows the CRT the bundled tools link against stops at
  `mlo.subproc.MAX_PATH_LIMIT` (260) characters, and flac.exe does not accept
  the `\\?\` prefix that lifts the limit in the Windows API (measured on this
  machine: 259 characters opens, 260 answers "can't open input file … No such
  file or directory"). The long-path bridge runs first — 8.3 alias, else a
  temporary junction (`mlo.subproc.tool_path`) — but when it cannot be made the
  file is verified by the ffmpeg decode-and-compare path instead of being
  reported from the tool's open failure: a stream that states an MD5 is `ok` or
  `md5-mismatch` from the decode, one that states none is `md5-absent`, and only
  with no decoder at all is it `md5-unknown` — whose reason names the length
  limit rather than inventing a missing flac.exe. So the grade's
  "FLAC MD5 not verified" can no longer come from a path the tool never opened.
  `mlo.subproc.tool_unreachable` is the one place that question is asked, and
  `tools/test_flac_md5.py`'s long-path section pins all four answers.

- **R364 — waits on different hosts overlap, and no lane claims the machine.**
  Waiting on one provider must not block another provider's ask: the RYM and
  Wayback waits have their own 1 req/s locks, so a slow answer on one host does
  not stall a request to another. One album is one lane and its calls share that
  lane's slice of the shared budget — the policy
  `mlo.accurip.run_generate_accurip` documents for its transport — so a Run All
  over N albums cannot multiply the requests in flight; a single-album run is
  one lane and gets the whole width. Script 9's per-lane
  ffmpeg WAV transport likewise takes `tool_threads(config, lanes)` — the
  per-lane share `mlo.flac`, `mlo.remux` and `mlo.images` already use — instead
  of every lane's ffmpeg claiming every core. Pinned by
  `tools/test_accurip_roundtrip.py` (the transport argv).

- **R365 — an encoder's thread count is the lane's share, not the machine's.**
  Every external tool that can thread itself takes `mlo.stats.tool_threads`
  (`cjxl --num_threads`, ffmpeg `-threads`, flac's `-threads`), and `oxipng` is
  the one that did not: its default is one thread per logical CPU, so script 5's
  `worker_count` lanes each started a full-machine encoder — 8 lanes x 16 cores
  on a 16-core box. All three oxipng spawns now take `--threads <n>` under the
  same "only when the share is > 0" guard the other tools use. The flag is NOT
  an encoding parameter: the produced bytes are identical at any width (proved
  end to end for the convert path, and `--threads 2` / `--threads 8` / default
  produce the same file). Pinned by
  `tools/test_script_optimizations.py`'s script-5 lane check.

- **R366 — politeness is per HOST, so providers stop queueing behind each
  other.** Script 13 walks several lyrics providers per track, each costing 1-2
  requests, and the 0.4 s spacing was one process-global timestamp: a
  MusicBrainz or Wayback wait queued behind NetEase/Kugou/QQ/Kuwo, and a miss
  cost ten-odd serialised starts. The timestamp is now keyed by the request's
  hostname (`mlo/lyrics_providers._request`), so every host keeps exactly the
  spacing it had while two hosts never wait on each other; the request COUNT,
  the provider order, the retries and every answer are unchanged. Pinned by
  `tools/test_script_optimizations.py` and `tools/test_lyrics_providers.py`
  (different hosts start together, the same host stays 0.4 s apart).

- **R368 — a probe reads what it needs, not the whole album.** Three passes
  stopped paying for work whose answer was already in hand: beets' pre-import
  snapshot takes the audio basenames from the directory listing and reads
  containers only until both MusicBrainz ids are seen (capped at 5 files, the
  bound `server.script_runners._TRACKLIST_PROBE_FILES` already keeps for the
  same question); the layout scan answers "does this album hold audio" from the
  listing it already has instead of walking the folder again (the walk remains
  the fallback for audio that lives only in a subfolder); and the grade pass
  detects the decoder tools ONCE per album instead of once per file that needs
  an MD5 (the detection is cached, but it was still a per-file call). Pinned by
  `tools/test_script_optimizations.py`, `tools/test_import_pipeline.py`,
  `tools/test_grading_paths.py` and `tools/smoke_organize_grade.py`.

- **R369 — an import names the album before its chain.** Every import path runs
  the naming-script organizer over the album before the configured chain, so the
  Grade step checks the paths the script produces instead of reporting each file
  as a PATH mismatch. The bulk/auto queue does it in
  `server.imports._bulk_one` (the step the original one-click import ran before
  the queue replaced it), and the wizard's own Finish calls `POST /api/organize`
  and runs the ticked scripts on the folder the organizer reports
  (`web/src/pages/ImportWizard.tsx`). An album whose tracks state no
  artist/album is left where it is — the script is evaluated from those tags, so
  there is nothing to name it by — and an organize failure is reported, never
  fatal. The organizer also prunes the folders it emptied, and a bulk batch
  sweeps the library's emptied shells once at the end
  (`server.imports._prune_import_dirs`), so an import never leaves the layout
  report's "Empty folders" finding behind. Pinned by
  `tools/test_import_pipeline.py`.

### 7.76 The library-layout report keeps itself up to date

- **R378 — the library-layout report keeps itself up to date, and every action
  that moves a folder asks for a fresh scan.** The report a page draws is a
  scan's output (`mlo.layout.scan_library`, the walk script 20 runs) and what
  the Library page's own layout warning reads is the STORED copy — so a folder
  the app (or the reader) had already changed kept being reported until somebody
  pressed Rescan (owner report: "I need to manually use this section under
  rescan for the library to update. It should be done automatically"). Now:
  `web/src/lib/layoutScan.ts`'s `rescanLayout` is called by every action that
  changes the tree — an album to the Trash (`AlbumPage.removeAlbum`,
  `LibraryPage.removeAlbums`), a restore from the Trash (`TrashPage`) — and it
  republishes the query the Library's warning reads. The scan is the READ-ONLY
  half of the layout route, so nothing behind a mutation can settle a reader's
  files by surprise — the fixing half is script 20's own `layout_apply` — and a
  scan that cannot run leaves the stored report standing, silently. Pinned by
  `tools/check_layout.cjs`: a folder deleted on disk behind the app's back (the
  owner's actual case) is gone from the report the Library page warns from when
  the page is reopened.

## 8. Runbook

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
   2/1 canonicalize sidecars, 13 fetch lyrics, 17 adds
   transforms, 8 writes mood/energy/genre/advisory, 5 normalizes images, 6 audits,
   7 measures DR/ReplayGain, 9 writes
   `.accurip`, 12 writes key/BPM, 16
   is the standalone mood pass, 10 is the final canonical pass, 20 puts the
   library's shape right, 21 completes (or creates) the AcoustID pair and 4
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
   sweep the whole library for itself, and a targeted run (a selection, the
   wizard's *Run ticked scripts*) makes every script work only on those
   targets. A script must never be handed an empty target list and left to
   report "nothing to do" — a run that changed nothing must be able to say why
   in terms of the files it looked at, not in terms of a scope it never had.

Safe to re-run at any time: **4** (read-only) and **20** (idempotent — a library
already in the canonical shape has nothing left to fix), 2, 1, 5, 6, 7, 8,
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
- `embed_covers` (default off) and `strip_unknown_tags` (on) — the last stages
  of a write.
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
| `grade_include_music`, `grade_include_cover`, `grade_include_cue`, `grade_include_log`, `grade_include_lrc`, `grade_include_accurip`, `grade_include_video` | ON | a file category participates; off means its files are also "disallowed" for `grade_check_disallowed` |
| `grade_include_other` | ON | unclassified files participate |
| `grade_log_score_threshold` | 100 | minimum `LOG_GRADE` for `grade_check_log_grade` (0 disables the threshold) |
| `grade_verbose` | ON | per-track detail in the Grade report |
| `grader_cover_size_tolerance_px` | 0 | pixel tolerance on the cover size test |
| `grader_strict_square_threshold` | 0.0 | strict aspect tolerance |
| `cover_crop_threshold` | 0.0 | `grade_check_cover_crop` tolerance |
| `cover_enforce_size` / `cover_enforce_square` | ON | whether the cover's size/squareness is enforced at all |
| `cover_resize_enabled` / `cover_force_exact_size` | ON | whether the cover is expected to be the target size exactly |
| `cover_target_size`, `cover_jpeg_target_size`, `cover_png_target_size`, `cover_jxl_target_size` | 1200 / 0 / 0 / 0 | expected cover dimensions (0 = the global target) |
| `reencode_images` | ON | whether cover encoder tags are graded |
| `encoder_tags` | per-format map | which `ENCODER_*` markers `grade_check_encoder` requires and the skip checks compare (`ENCODER_QUALITY` — the LEVEL — on; `ENCODER_VERSION` and `ENCODER_PROGRAM` off, per format; see R329) |
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
| `run_all_order` / `import_scripts` / `import_auto_scripts` | see R8 / R9 | what runs, and in which order |
| `digital_media_source_value` | `Digital` | the answer an import offers when nothing states where a Digital Media release came from (R262); the `SOURCE` it writes is what the source checks then read |

Two keys deliberately do **not** change a verdict on their own:
`grade_check_accuraterip` (AUDIT-only, R5) and `show_sidecar_files` —
deliberately NOT in the table above: it only makes the viewer list a file's
sidecar siblings (`cue`/`log`/`lrc`/`.accurip`) and compute their grades, and it
adds no check. The release-choice and locale keys are not grade keys
either — they choose
which file a verdict is later computed on, never the verdict itself:
`prefer_disc_streams` and the other release-choice keys (R84/R85, including
`auto_import_medium_order` — R246), and `locale` (R87).

A key that writes outside the grade is not in the table for the same reason:
`cookie_notes` is a
hidden key, written by the cookie routes and never by the settings form (no
Settings row offers it).

---

## 10. Honest limits of this spec

- The check **count** is 67 today; the registry derives it from
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
- **The storage walk counts DIRENT NAMES, not blocks.** A hard link made by
  hand inside the library is a second real file to `os.scandir`, so the card
  counts it twice; a symlink or junction is not followed at all, and is
  reported as a link rather than a gap (R186). Nothing in the app creates a
  hard link — the only links a healthy install has are the bundled tools' own
  version symlinks.
- Detection is heuristic where the evidence is: AudioAuditor's spectral
  detectors can disagree with a provably intact rip, which is why a verified CD
  rip outranks them (R21) and why `AUDIOAUDITOR_OVERRIDE` exists (R25).
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
- **A notification a client missed survives a restart, but not for ever**
  (R216): the durable log keeps the newest 400 frames, so a device that was
  away longer than that (or that is reopened after a long absence) sees the most
  recent notices and not the whole history — the tray keeps its own newest 50
  anyway.
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
