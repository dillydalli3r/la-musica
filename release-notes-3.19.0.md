# la musica 3.19.0 — nothing waits for you, a warning is not a lock, and the player's text sits on nothing

Everything below came from reports on the running app, and every one is fixed where the
cause was rather than where the symptom showed. Evidence is named per section; the house
rules are in `docs/OPTIMIZATION-GRADING-SPEC.md` (136 rules, `R166`–`R170` new here).

## An import never waits for a person, and a warning is not a lock

The pipeline used to hold an album in the queue's *Needs you* section when no source could
supply a family — the release read as a stall, and Run All skipped the album as if it were
being written. It is not: the import **finished**, the album is in the library and graded
like any other, so the gap is now a **warning** (`spec R166`):

- the release's own row stays in **Completed** with `⚠ Needs data: …`, *Enter manually* (the
  wizard at the step that decides it) and *Mark complete* — the same album is no longer two
  rows in two sections;
- the album page shows the same sentence with the same link, and the wizard's list reads
  "Imports still needing data";
- only entries that really are waits — a **review** import whose chain has not run, a disc
  structure whose feature is unpicked — stay in *Needs you*, and only those are skipped by
  a library-wide Run All. A review import is still parked exactly as before (`R161`).

## Presses and runs cannot collide, and a claim follows the album

Four holes, each proven with a failing case before the fix and fixed at its seam
(`spec R168`, `tools/test_job_locks.py`):

- **an import claims the album for the whole import** — the steps before the chain (links,
  genres, metadata, cover art) used to run unclaimed, so two presses could write the same
  album at once;
- **an auto-import's background chain inherits the job's claim** — it used to be released
  when the download settled, leaving the whole chain unlocked, and the release's **download
  folder** is claimed with it, so nothing imports or clears it underneath;
- **a chain that MOVES an album keeps holding it** — script 14 renames the folder, and the
  tail of the chain used to rewrite an album that was unclaimed under its new name
  (`job_locks.move` takes the new path first, then hands the emptied one back);
- **a library-wide run claims what it walks** — the root plus every album filed outside it,
  which is where a Run All could rewrite an album an album-scoped run was holding.

Measured live: pressing *Run the import chain* on a busy album answers **409** —
*"2026 - Big is in use by Auto Tagging (job-12) — wait for it to finish, then retry"* —
instead of starting a second chain.

## The fallback walk asks distinct pressings

Separate MusicBrainz releases really do share a catalog number — one CD issued under two
labels (`GED 24425` beside `GED24425`), a reissue, a country variant printed unchanged — and
the number is what a CD search is keyed on, so the next edition in the walk could only find
the folders the previous one already found. The walk now skips those and **logs what it
skipped** (`spec R169`). Measured on Nirvana's *Nevermind*: **98 ranked editions → 60
distinct searches, 38 skipped**. Editions with no number are always kept.

## The queue takes any MusicBrainz link

Paste a **release, release-group, artist or recording URL** — or a bare MBID — into the
queue bar and it goes through the same `POST /api/library/add` the MusicBrainz pages' own
*Add to library* uses: a release-group walks the group's ranked editions, an artist queues
its discography in the background, a recording resolves to its release, a bare MBID is
resolved server-side so both paths agree (`spec R170`). It used to parse any MBID as a
*release* — an artist link was queued as a pressing that could never be found — and a link
the queue cannot look for is now refused by name.

## The fullscreen player paints no panel, and its ink answers the cover

The grey box under the title and behind the lyrics is gone, and so is the full-bleed grey
wash that replaced it (`spec R52c`):

- the **ink is derived from the cover** — the cover's own average colour, the value the
  ambience is painted from, decides between a light and a dark table. A dark cover gets
  white text and **nothing at all behind it**; a bright one flips to near-black ink over a
  lift built from the cover's own colour, never a grey scrim. The glyph shadow flips with
  the ink;
- the transport glyphs, the time readouts and the top bar follow the same table, so nothing
  is left washed out on a white cover;
- the lift has a **floor** for a measured reason: what the dark table must clear is the
  dimmest patch the text covers, which is dark whatever the cover's average is (a `#b4b4b4`
  cover measured 2.5:1 without the floor, 4.69:1 with it).

Measured on four covers (`tools/check_np_metadata_contrast.cjs`, 39 checks): dark
20.0:1/13.5:1, mid-grey 12.0:1/8.1:1, bright-grey (the boundary) 6.26:1/4.69:1, white
10.9:1/8.1:1 — title / secondary tiers, AA on every one, with no scrim at all on the dark
cover. The structural half (`tools/check_fullscreen_player.cjs`, 29 checks) asserts nothing
paints over the art but the ambience and the chosen scrim.

## The wizard's Finish step runs scripts, not the import again

The **"Run the import chain"** button is gone. It re-ran `finish_album`, whose own steps
re-fetch the links, genres, cover art and advisory and **empty the four families an import
decides first** — so it undid exactly the work done by hand in the eight steps before it.
*Run ticked scripts* and *Finish* stay, the album's scripts are re-runnable from the album
page, and Run All over the library still lives on the Optimization page (`spec R9`).

## Lyrics: what a track needs is decided from evidence

Romanization and translation were decided from the letters alone: two Latin-script languages
could not be told apart, so a Turkish track with no English stopwords in it was skipped as
already-English (`spec R167`). Now:

- a track's own **LANGUAGE tag** — which an import fills from **MusicBrainz's release text
  representation** (`jpn` → `ja`; `mul` states nothing), written only into an empty tag —
  is read first, then the lyrics' own script, then the function words;
- when nothing can say, the configured AI is asked **one question about that track** and its
  answer is **stored in the tag**, so the question is paid once and the grader reads a
  stored fact instead of asking a model anything. Verified on real files: asked once, tag
  written, re-run silent.

## The advisory ladder asks the AI only when nothing else answered

The multilingual **word scan is gone** — module, config key and its settings row
(`spec R63`). The ladder is now: a source that stated a value wins as it stands → an
instrumental is 0 → the AI, asked only when every source came up with nothing → the
configured fallback. A stated 0 is final; nothing second-guesses a source (`R62`, `R65`).

## The setup wizard is six steps, and the library page's views work

- **Setup**: 13 steps → **6**, with the strictest checks as the shipped defaults and one
  screen for credentials (that step's page: 4 779 px → 1 468 px tall). Verified with six screenshots and
  a reachability test that every key still has an editor.
- **Library**: *Albums / Artists / Tracks* rendered empty 200 px rows — the title cell held
  name, marks and stars in 220 px, so the name collapsed to zero width. Fixed with a wrap
  and a real Rating column (`tools/check_library_tables.cjs`, 20 checks), plus
  **Rated / Unrated** (your own stars) and **Explicit / Clean** facets with live counts.
- **Force**: the Force menu sent config-key names that the runner dropped, so *Force
  re-audit* re-audited nothing — now short keys, a bare bool covering a whole chain, and
  imports never send a force dict (so `layout_apply` really applies). Verified live: force
  off → 0 modified, `{flac: true}` → 2/2, `force_reencode_flac` dropped.
- **Home**: clean artist names, real artist pictures with fallbacks, a *Your ratings* shelf.

## Everything else in this release

- **Add to library / auto-import**: the add answers in about a second instead of thirteen, the
  search is queued and one album is **one tile** through the move; the ranked walk gives each
  candidate its own 60 s window, three candidates by default, and a release nobody shares
  moves to **Background** and keeps being searched.
- **Imports that end badly**: the chain used to report the folder beets wrote *before* its
  own organize pass renamed it, so every later script ran against an emptied folder; the
  album's own files (cover, description, expected tracklist, cue/log) now travel with it.
- **Soulseek** progress is pushed at 2.5 Hz (displayed age 1.48 s → 0.17 s, updates 3.0 s →
  0.4 s, idle silent), and two independent **5 GB caps** prune the download cache and the
  trash oldest-first (`soulseek_cache_cap_gb`, `trash_cap_gb`).
- **Tags**: the multi-value audit — video containers keep their representations, credit lists
  are completed rather than truncated, `%genre%` is the same value on both sides of an
  import, and every ISRC is asked (`R57a`–`R57e`).
- **Player**: a lyric offset control on both lyric surfaces (±0.1 s, saved into the lyrics),
  and track details & credits open from the player bar and the fullscreen options menu.
- **MusicBrainz artist page**: the redundant *Whole artist* row is gone (the header button is
  the one control).
- **UI audit**: responsiveness 145 → 160 checks (the check itself carried the bug), the 404
  renders the app's own empty state, and two broken check harnesses were repaired.

## The contract, the tests, and how to run it

`docs/OPTIMIZATION-GRADING-SPEC.md` is the contract (136 rules, `R166`–`R170` new, `R9`,
`R52c` and `R161` rewritten); `README.md` and the repository description follow it.

**All 113 Python suites pass** (`python tools/test_*.py`, one per area, each exiting
non-zero on failure), plus the payload-driven UI checks (`tools/check_*.mjs`), the config/UI
parity check, 6 locales × 308 keys, and `python tools/check_versions.py` — which every
release and every push runs, so no build can disagree with itself about its own version.

```bash
docker compose pull && docker compose up -d      # ghcr.io/dillydalli3r/la-musica:3.19.0
```
