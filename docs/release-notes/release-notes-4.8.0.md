# la musica 4.8.0 — loading is near-instant, and the player stops talking about lyrics formats

## The library draws a window, and the shell is small

Large libraries were slow for two different reasons, and both are gone.

**The Library page rendered everything.** Every view drew one row per album or
track — 50,000 tracks meant ~700,000 DOM nodes, tens of thousands of component
instances and one long synchronous commit. Every view now renders a window of
**300 rows** and grows it as you scroll (an IntersectionObserver sentinel, 800 px
early) or press *Show N more of M remaining*. Counts, facet tallies and the
header checkboxes still read the FULL filtered list, and *select all* still
selects every filtered row — the selection is Sets now, so a 50k-row library no
longer does 50k² membership scans. Changing the view, search, preset, facet,
A–Z pick or sort resets the window. Rows carry `content-visibility: auto`, so
the browser skips offscreen layout and paint.

**The first load shipped everything.** The entry chunk carried the fullscreen
player, the lyrics sidebar and all six languages before the first paint. The
fullscreen player and the docked lyrics sidebar are their own chunks now
(preloaded on hover, so opening them feels the same), and each non-English
language is a chunk of its own that falls back to English for the frame it takes
to arrive, then re-renders. **Entry chunk: 696.7 KiB → 486.4 KiB (190.8 → 149.4
KiB gzip).** The service worker precaches exactly the app shell (14 URLs) instead
of activation downloading every page chunk (~1 MB less), and lazy chunks are
cached the first time a page asks for them.

**Covers are fetched at the size they are drawn.** Grid cards asked the server
for the 1200–3000 px master under `no-cache` and revalidated it on every visit;
they now ask for a 160/320/640 thumbnail (the user's own grid size) served from
the server's disk cache. The offline-artwork probe (one Cache Storage
transaction per image, on every render) now runs only for artwork that is
actually downloaded, or when you are offline.

## Refresh re-walks the folder without re-reading it

The Refresh buttons re-walk the library and answer with fresh rows — but they no
longer throw away the caches that are keyed on the files themselves (tags by
`(path, mtime, size)`, an album's indexed payload by the folder's own
signature). A file you added, removed or retagged outside the app is still seen;
the files that did not change are simply not re-parsed and re-graded. Measured
on a scratch library: **29–52 ms per refresh** instead of a cold rebuild of the
whole ladder.

`GET /api/library` is also serialized once per build, with an ETag: a repeat load
answers **304** in ~3 ms instead of re-encoding the app's largest payload every
time (R334/R338). The server's startup path is lighter too: `import mlo` loads
nothing, the scripts' one table moved to a leaf module, and `server/main.py`'s
route surface is split into `server/api_*.py` routers — verified route-identical
(279 routes, 273 OpenAPI paths, same handlers).

## The fullscreen player never says "✕ Plain", and plain lyrics stay editable

The fullscreen player used to park a red `✕ Plain` chip beside the track title
whenever your install refuses untimed lyrics. The owner's report was simple —
*"doesn't say any extra text like 'X Plain' next to the title"* — so the mark is
gone: the title row states the track (title, advisory, codec readout), and
`plain-refused` keeps its only effect there, which is that no lyrics pane and no
toggle are offered. The track's own surfaces (its page, its details row, the
lyrics manager, the wizard's Lyrics step) keep the mark.

And the pane can still EDIT a plain text: a track whose stored lyrics carry no
timestamps now opens the raw editor with those words in it. Before, the pane
showed *"No lyrics yet…"* for a track whose plain lyrics were right there —
un-syncable. The Raw/Lines toggle no longer wipes a plain text either.

## LRCLIB gets the synced text when it only has the plain one

Submitting lyrics to LRCLIB refused anything LRCLIB "already had" — including a
record that holds only untimed words. That is backwards: a synced text ADDS the
timings the community entry lacks, and LRCLIB keeps every revision. The rule is
now content-based (`lrclib_would_add`): a plain-only record takes this library's
synced text (the manual panel reports `upgraded: true`, script 18 records it per
name pair), while a record that already carries timings still blocks a plain
submission and a like-for-like copy is still the duplicate it always was.
`force` is unchanged.

## Smaller things

- **An album card's artist name opens the artist page** (the MBID-preferred
  route, so it survives a move) — the library grid, Home's shelves and every
  other surface that draws the shared card.
- The README is a brief front door now: install, what the app does, clients,
  security, tests — the contract lives in `docs/OPTIMIZATION-GRADING-SPEC.md`.

## Verified

- `python tools/test_tagindex.py` (the tree is still served stale-while-
  revalidate, an in-app write still busts it), `python tools/test_lyrics_publish.py`
  (the new upgrade cases: a plain-only record is submitted to; a plain text
  against it is still refused — script 18 and the manual endpoint),
  `python tools/test_lyrics_kind.py`, `node tools/check_lyrics_kind.mjs`.
- A real browser against a scratch server with real FLACs (plain + synced
  lyrics): the plain track's pane opens its raw editor with the words in it; the
  fullscreen player draws **zero** lyrics-kind marks and its title row reads
  `Plain Song 16/44.1 The Album · The Artist · 1972`; an album card's artist name
  is a link and opens `/artist/…`; picking German fetches `de-*.js` and renders
  "Bibliothek"; the library grid draws its cards.
- The Refresh contract, live: `?refresh=1` (29–52 ms) still sees an externally
  added file and loses it again when removed; `If-None-Match` answers **304**.
- `python tools/check_versions.py v4.8.0` — all copies agree.
