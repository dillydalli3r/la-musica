# la musica 4.3.0 — one knob for the threads, and a queue that never ends

Three things: the app's worker threads become **one setting** that every script
honours (default: every core this process may use), the player gains
**infinite playback** (off by default), and the tables learn to **fit the
screen** — a stored column width is a preference about how a table's width is
spent, never a way to widen it past its box.

## One setting for the threads

`worker_limit` (Settings → *Worker threads (0 = every core)*, 0–64) already
existed; what did not was that every script used it. All 22 scripts were
audited, and their inner loops now run through one helper at that width:

- **0/unset means every core this process may use** (`mlo.stats.usable_cores`,
  affinity-aware), capped only by each script's own ceiling.
- **44 call sites** rewired across `mlo/*` and the server's own pools, so the
  exporter, the importer and the instrumental network lookups honour the same
  number instead of their own hardcoded sizes.
- **Subprocess- and native-bound work is where the throughput came from**:
  FLAC re-encode, image encode, ffmpeg/remux, `fpcalc`, `rsgain`, librosa
  key/BPM. Each ffmpeg call now takes **one lane's share** of the budget
  (`tool_threads`) rather than every core, so the pool and the tool inside it
  do not fight over the same machine.
- **Inside an album too**: DR/ReplayGain decoding now runs one lane per track
  (album gain and DR are still computed after every lane reports, so the
  numbers are identical) — measured 0.94 s → 0.61 s on a one-album fixture at
  the default, with the same DR written.
- **Safety came first**: parallel and serial arms produce byte-identical
  library manifests (94 files, one manifest hash) for scripts 3/6/7/11; the
  remux pass books its removals on the runner thread instead of racing the
  counters, and the artist-provenance map (a lost update before) is now
  lock-guarded and re-read from disk.
- Measured with `tools/perf_pipeline.py` (12 albums × 5 tracks, 16 cores):
  chain total **161.4 s → 156.8 s**, local subset 84.6 s → 81.3 s; at the same
  default vs `worker_limit=1`, FLACs **4.9×**, DR **4.5×**, audit **1.7×**.
  `tools/perf_pipeline.py --worker-limit N` records the setting in its JSON so
  the next change has a before/after to answer to.

Ruled as **R323** (one knob; a script parallelises only over units that share
nothing) with the 22-row pooled-unit table.

## Infinite playback

A switch (`infinite_playback`, **off by default**, Settings → Downloads &
playback): when the queue reaches its last track the player appends a handful
of tracks SIMILAR to what was in the queue — scored locally from the library's
own tags (`server.recommend.recommend_for_queue`, so the answer needs no
network and cannot stall the music) — as **ordinary queue rows** the reader can
reorder, remove and see like any other.

- The batch is bounded (5 by default, 10 imposed server-side) and the seed set
  is also the EXCLUSION set: nothing already queued is added twice.
- It extends **before the gapless preload arms** for the last track, so the
  transition into the recommended set is as seamless as any other advance.
- Repeat-one is respected (nothing is appended while it is armed); shuffle
  still draws from the seeds' similar set; a provider that fails simply leaves
  the music where it was.
- New endpoint `GET/POST /api/recommend/queue?paths=…&limit=…` with the tests in
  `tools/test_recommendations.py`; `tools/check_player_state.cjs` grew a
  section that drives the real player (append, no duplicates, reorderable,
  preload intact, off = nothing) — 51/51 with the feature, and the same block
  red against the previous build (43/51).

Ruled as **R324**.

## The columns fit the screen

"columns are VERY long … rows seem really wide" — a `table-layout: fixed` table
takes the sum of its columns for its own floor, so four stored drag widths,
every one inside the handle's own 40–900 px range, drew a **3848 px** album
tracklist inside a 1200 px box (every column after the title off the right
edge). `useFittedWidths` now reads each column's own floor off the table and
spends only the room that exists: a column the reader sized gets its floor plus
a share of what is left, proportional to how much more than its floor it asked
for. The table therefore renders exactly as wide as it would with **no stored
widths at all** (3848 → **1200 px**, measured at 1440/1100/801/390, with and
without the hostile map) and the sideways scroll stays only where the floors
genuinely cannot fit (R313). One hook, six tables: the library's Albums /
Artists / Tracks views, the album page, the expanded tracklists and the
Downloads page.

## Fixes that came out of the release

- **A redirected run keeps its own config.** `MLO_MUSIC_FOLDER` is an
  instruction, so a scratch run writes *its* `<folder>/.mlo/data/config.json`
  and never this checkout's legacy stub — a tool that stamped a temp folder
  into `config.json` made every later process in the repo answer from it, which
  is what turned the login gate on for a dozen test suites at once.
- **A remembered folder that no longer exists decides nothing**
  (`mlo/paths.py`): the legacy stub points at where the state lives, so if that
  folder is gone there is no state to read and the app starts from its
  defaults.
- `tools/test_codec_policy.py` follows the per-lane `-threads` flag the
  conversion now carries (the value is the machine's own share, so it is
  asserted as a count and the rest of the argv exactly).

## Verified

- All 128 `tools/test_*.py` suites pass (each also run on its own); `npx tsc
  --noEmit -p tsconfig.app.json` and `npm run build` clean.
- `tools/check_player_state.cjs` 51/51 (including infinite playback),
  `check_os_stop_resume.cjs` 24/24, `check_library_az.mjs` 93/93,
  `check_library_tables.cjs` PASS with the hostile-width cases,
  `check_lyrscroll.cjs` and `check_release_choice.mjs` unchanged and green.
- The thread budget's own semantics are pinned in
  `tools/test_script_optimizations.py` (`check_worker_budget_semantics`), and
  the parallel/serial equivalence by the manifest-hash comparison above.
