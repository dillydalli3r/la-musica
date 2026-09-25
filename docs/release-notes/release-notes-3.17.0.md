# la musica 3.17.0 — the picks land, the bar tells the truth, and the export says what it leaves behind

Everything below came from reports on the running app, and every one is fixed where the
cause was rather than where the symptom showed. Evidence is named per section; the
house rules live in `docs/OPTIMIZATION-GRADING-SPEC.md`.

## The cover you pick is the cover you get

Pressing **Use this cover** in the import wizard's cover finder had never written a
file. The finder's single write call never sent the wizard's `staged` allowance — the
opt-in that lets the app write into a folder the library does not list yet — so
`_guard_folder` answered every pick with **400 "album outside music folder"**, nothing
was written, and the panel below kept showing the album's arrived `cover.jpg`, which is
whatever the download carried. That is one cause behind both reports: the preview not
matching the pick, and the picked cover not matching the tags.

Behind it sat a second one. The art proxy silently substitutes an image when the URL it
was asked for cannot be fetched, caching the substitute under the requested URL for 30
days. Asked for a dead URL with an **artist but no album**, the album tiers answered
with that artist's most popular release — measured: a different Radiohead album's
artwork, pixel distance 147.5 from the one on screen. The album tiers are now asked only
when there is an album to ask about. (Apple Music rows still land as the iTunes-store
rendition rather than the 23 MB CDN master, because the app's 20 MB image cap is
deliberate — same picture, different bytes.)

*Driven live: the panel's caption went `cover.jpg — 1200×1200 px · 23 KB` (arrived) →
`· 387 KB` (the picked art).*

## The progress bar never shows another run's work

The header bar could sit at 50 % before a run had done anything: the surface is global,
and between a run starting and its first published frame it still showed whatever the
last producer left there — an import stage's 4/8.

- A run now **claims both surfaces with its own zero state** the moment it starts, with
  the frame its first script would publish, so the handover is invisible.
- A run **cut short** (by the auto-updater, at a script boundary) now **ends** the
  surface at `N/N` with `#ran/N · stopped` instead of freezing mid-way.
- The wizard's strip **never mixes two producers**: a frame older than the press cannot
  paint this run's strip; a later import stage appears under the *stage's* name with its
  own numbers; the run's frames appear under the run's own `#i/N`; both chain bars draw
  from one reading. Reverting just those props fails 13 of the 65 checks that pin it.
- `test_job_locks.py` 144 checks, 0 failures.

## Pressing Run no longer means waiting in silence

Pressing **Run the import chain** on an album the app's own background import was
finishing meant up to **6 seconds of total silence** (claim asked at 0.76 s, granted at
6.76 s, only the other job's row visible) and then a **duplicate chain** — a rerun of the
chain that had finished 0.01 s earlier. The press's own pre-chain work published nothing
either: 5.35 s of lookups (`stamp_rym_links`, `run_cover_step`) with no frame at all.

- Every pre-chain phase now announces itself — *Looking up links… / Fetching genres… /
  Fetching advisories… / Checking instrumentals… / Fetching metadata… / Finding cover
  art…* — indeterminate, and yielding to a chain that owns the bar.
- The **user's own press never waits**: it is refused in 0.01 s with the holder's
  sentence (`{Digital Media} is in use by Organize (job-1) — wait for it to finish, then
  retry`), and a refused album reports in its own slot rather than folding into a note.
- The **autonomous** paths (bulk queue, downloads, Soulseek, wish/watch) still queue —
  and now say so: `waiting for Optimize FLACs — Probe Album is in use`.

Spec: R94a.

## One advisory action, and it asks

Every surface that could re-rate an advisory had two controls where one belongs. There
is now **one**, and it **forces** — it asks the sources even for a track that already
carries a rating, which is what the separate "Re-rate…" entry did. Four surfaces were
collapsed: the track menu, the wizard's advisory step, the metadata review modal, and the
track page's Song info action (found while doing it).

The **unattended import stays fill-only** (`finish_album → fetch_advisories(force=False)`)
and is now the only caller that is: nobody presses anything there, and an import must not
overrule a rating it did not decide. Recorded in the spec so the asymmetry reads as a
decision, not an oversight.

An import that skips a family now **says so** — `skipped_families` on the result, the
same one line the queue row, notification and job log print, and a log line per family —
and the album **keeps what it arrived with**, because nothing was overruled.

*Pinned end to end: arrived `Peer Genre`/`0`/`peer lyric` → gone; the release's
`Rock; Shoegaze`/`1`/fetched lyric → present; a configured skip reported, never silent;
the shipped defaults reach all three families.*

## Lyrics without a floating ✕

The fullscreen player has **one** lyrics toggle in its own control row — the same `Mic2`
icon the player bar uses, same 36 px box, `text-accent` when on, tooltip "Toggle the
lyrics pane" — and nothing is drawn over the artwork. It is hidden when the track has no
lyrics (a control that cannot do anything is hidden, not inert) and over music videos;
the choice is remembered like the other display picks.

Hiding it is seamless: the pane is never unmounted, its box animates and the art
**glides** back to the centre (measured 784 vs 784 px against the body's content centre),
the same DOM node, zoom still 1.5, scroll position preserved, the follow controller
frozen while hidden and re-centred when the width finishes growing.

**Decorative cover frames are gone everywhere** — the player art's `border-white/10`,
every grid/list/picker/dialog cover wrapper, the artist tiles and headers. Kept on
purpose: the keyboard focus ring, and elevation shadows, which are depth rather than
decoration.

## The Export page uses the window

The page capped at `max-w-6xl`, and the picker's wrapper carried `.table-scroll`'s
`min-width: 46rem` — so a 736 px table sat in a 510 px box, the row scrolled, and the
Artist cell was cut off at the wrapper's edge: "Radiohe…". Measured at 1568×756: container
1152, cards 544 each, and 749 px of the source card's 1181 px empty below the last row.

- Container follows **Browse** (the app's other content-heavy page): `max-w-[1600px]`.
- The two panels split at `xl`, not `lg` — at 1024 the old breakpoint left 384 px panels
  where names wrapped 3-8 lines deep.
- Prose columns flex; `count`/`dur` stay narrow because they hold numbers.
- The picker list is the card's body (`flex-1`), so it fills the height it is given.
- Short inputs are capped in the app's own idiom (Subfolder `max-w-xs`, workers
  `max-w-[16rem]`), so a two-word label cannot stretch a 600 px field.

## The export leaves things behind on purpose, and says what

By default an album export no longer carries `.accurip`, `.log`, `.cue`, `.txt`, `.jpg`
or `.m3u8` — and no artist images, artist descriptions or album descriptions go into the
tags. Damage that matters: a rip log, CUE or `.accurip` is the **evidence** an audit
verifies, and `.jpg` travels by embedding.

That means nothing silently disappears:

- Every non-audio file found is **audited** — reported by name, classification and reason
  (`excluded`, `excluded_counts`, `excluded_total`, `excluded_note`), with the run log
  printing `Export finished - N non-audio file(s) not exported (…)`. That covers `.nfo`,
  `.md5`, `.sfv`, `.ffp`, `.url`, `Thumbs.db`, `.DS_Store` and anything nobody
  anticipated — not just the known list.
- A **playlist** export still writes its `.m3u8`; the rule is scoped to album-type
  exports.
- **Lyrics are a choice**: embedded, `.lrc` files, or both — defaulting to whatever the
  library itself keeps. A source `.lrc` shows up in the audit as a lyrics sidecar when
  the mode does not write files, and in the output (not the audit) when it does.

## One rule for every filename

`mlo/naming.py:sanitize_segment` now owns it: `< > : " / \ | ? *` and control characters
become `_` one-for-one (`A***B` → `A___B`), a trailing dot or space becomes `_`, and the
reserved device names get a `_` on the stem (`AUX.mp3` → `AUX_.mp3`). Idempotent. NUL is
deliberately excluded — the OS refuses it in a path, and the grader's
`UNKNOWN_RELEASE_TYPE` wildcard is spelled with it and must survive.

The competing conventions are gone: a weaker duplicate in `server/main.py`, and Soulseek's
`_safe_component`, which **deleted** illegal characters instead of replacing them. A `/`
inside a title is now a name, never a directory.

And the **audit and grading paths match through the same rule** (`name_key`,
`cue_ref_names`): a rip log naming `01. AC/DC - Title.flac` still resolves to the
library's `01. AC_DC - Title.flac`, because a mismatch there reads as "no such file" and
quietly loses the album. Logs and CUEs themselves are never rewritten.

## Equalizer profiles, including Peace

The export menu's equalizer control now takes **Peace / Equalizer APO exports**. Both
shapes are accepted:

- Peace's `FilterCurve:` line — `f0..fN` frequencies paired by index with `v0..vN` values,
  plus `FilterLength`, `InterpolateLin` and `InterpolationMethod`. Constructed ladders are
  never assumed: the owner's own file is 50 points from 10 Hz to 18903.4 Hz and is not a
  clean geometric series.
- Equalizer APO's text form — `Preamp:`, `GraphicEQ: f g; f g; …`, and parametric
  `Filter N: ON PK Fc … Gain … Q/W`, with `ON None` slots skipped rather than treated as
  bands.

`v0`-less or unparseable pairs are refused **naming the line** — a curve is audio, and a
silent misparse is worse than a refusal. Where the app's model cannot represent what the
file says (B-spline interpolation, a shelf filter, BW instead of Q), the import result
**states the approximation**. The owner's `Earbuds.txt` parses with 0 errors and 0
unsupported lines: 50 peaking bands, median Q 6.49, and the resulting filter chain is
accepted by ffmpeg.

## Saved export configs

The export page's whole configuration can be saved under a name, loaded back and deleted
(~700 bytes of JSON for a 22-field config), including the **equalizer profile by
identity**. If that profile is gone when the config is loaded, the app says so — the
select reads `Peace_parametric - missing (no such profile)`, the bar carries
`"Smoke DAP" - its equalizer profile 'Peace_parametric' is gone - pick another profile
before exporting`, and the destination dropdown shows it — rather than exporting with a
different curve. (Also: "equalizer" is now spelled that way everywhere it is displayed.)

## The workflows build the same things, once

- **One producer for the frontend**: `ci.yml` uploads `web/dist`, and the desktop legs,
  the mobile jobs and the release job download it, instead of each running `npm ci` +
  `vite build`.
- **The Docker image runs beside the client builds**, not after them (new `docker` job,
  `needs: ci`) — and the release job still waits on it, so an image that fails fails the
  release.
- Two duplications removed: a separate type-check step that re-ran what `npm run build`
  already runs (`tsc -b`), and `tauri build`'s own `beforeBuildCommand`, which rebuilt the
  frontend a second time inside every desktop leg (and once per mobile leg).
- `pattern: la-musica-*` on the client download — without it, `download-artifact` with no
  name merges every artifact in the run, including the frontend's.
- Caches wired where they help (npm both lockfiles; nothing to cache on the release/docker
  jobs, which install no Python packages). Validated with actionlint 1.7.12, and the
  `--config '{"build":{"beforeBuildCommand":null}}'` override proved locally against the
  pinned tauri-cli 2.11.4.

## Suites

113 suites, the export and import batteries among them (`test_export_audio.py`,
`test_import_pipeline.py`, `test_job_locks.py` 144, `test_prune_empty.py` 19/19,
`test_script_optimizations.py` 72, `test_import_controls.py` 68), plus the new pins this
release added: `test_chain_bar.py` (65 checks, falsified by reverting the props) and
`check_chain_bar.mjs` driving the real page, and the export's EQ/lyrics/config cases.
