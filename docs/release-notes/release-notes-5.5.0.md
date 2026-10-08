# la musica 5.5.0 - the library says what it found, and favourites manage themselves

Seven reports, and the thread through them is that a surface kept quiet about
something it already knew. The Library layout panel painted a stored scan and
waited for a Rescan press, so a folder the app had itself moved — or one the
reader deleted behind it — kept being reported. An artist whose grade failed
drew nothing beside its name, so an artist whose image never arrived looked
like one nobody had graded. A favourite the library cannot resolve was filtered
out of the one page whose job is managing favourites, and so could not be
removed at all. The player held the words back behind the cover, and the
fullscreen player's own top row came out under the shell's title bar.

## The layout panel scans when it is opened

The panel paints the stored report first — it arrives with an answer instead of
a spinner — and then runs a **scan** of its own, on every open. The scan is the
read-only half of the layout route (`mlo.layout.scan_library`, the walk script
20 runs), so nothing behind a page open can settle a reader's files by
surprise; a scan that cannot run leaves the stored report standing, silently.

`web/src/lib/layoutScan.ts`'s `rescanLayout` is the other half, called by every
action that **moves** a folder: an album to the Trash (`AlbumPage.removeAlbum`,
`LibraryPage.removeAlbums`), a restore from the Trash (`TrashPage`), and the
panel's own Apply fixes / album-less-artist removal — which also invalidate the
query the Library page's layout warning reads, so the two surfaces cannot
disagree. The fixing half stays where it was: Apply fixes and script 20's
`layout_apply`.

Proved by `tools/check_layout.cjs` (new, 5/5 on a scratch library with an
album-less artist folder): opening the panel moves the stored report's
`scanned_at` with **no** Rescan press, and a folder deleted on disk behind the
app's back — the reported case — is gone from both the stored report and the
panel when the page is reopened. R378.

## A failing artist is visible, where the name is

An artist whose own grade fails (`grade_artist`: its image and its description)
now draws the amber warning beside its name wherever a name is listed — the
artist page's title, the Library's Artists view, Home's artist shelf, Favorites.
The mark's tooltip names the failing checks (`label — reason`, the words the
page's chips use), and an artist the payload never graded still draws neither
mark: "not looked at" is not "failed". R322.

The grading strip's album findings now carry **every** album-wide sentence the
grader recorded (`reasons`), not just the first: the row still prints one and
its tooltip names the rest beside the tag codes, so "Album description missing —
fetch one on the album page" reaches a reader whose album happens to fail
another album-wide check first (MEDIA, the album tags, the cover). R232.

## Favourites and playlists manage their own items

* Favorites and the playlist page gained the app's select mode (Select toggle,
  `SelectAllButton`, per-row checkboxes, batch bar) — taking twenty tiles off a
  favourites list one heart at a time is not managing anything. The batch write
  is the store's own: `unfavoriteMany` (one `likeToggle`/`favoriteToggle` per
  ticked key, **one** invalidation at the end) and `api.playlistRemove(pid,
  picked)`, which already took a list.
* A favourite or playlist entry the **library cannot resolve** still loads. The
  Favorites page built its rows with `.filter(Boolean)`, so a favourite whose
  folder was moved, renamed or holds no audio was invisible *and* unremovable
  (Home's own shelf was drawing the same entry as an `owned: false` card). Every
  tab renders it now — the album tab as that same card, the artist and playlist
  tabs as a row naming the folder / `Playlist #id` with "not in the library" —
  each carrying the heart that takes it off. R377.
* "Releases", not "albums", wherever an artist's count is written:
  `releaseCount` in `lib/fmt.ts` is the one pluralizer, used by the shelf
  caption, the artist page's subtitle and its own section heading (RELEASES),
  and Favorites' artist table — the word the Library's Artists column has used
  since it was added.
* The "Top artists" shelf was an 80 px circle that filled a screen with six
  artists. It is `grid-cols-3 sm:4 md:6 xl:8`, `gap-2`, a `p-1.5` card with a
  64 px **square** tile (`rounded-lg` — the geometry the artist page's hero tile
  and every album card already draw): a card measures 113 px tall. The Library's
  Artists view carries the same square (`rounded`, 32 px) beside the name, so an
  artist looks like itself on every surface. R376.

## The player: the queue is warmed ahead, and the words no longer wait

* The warm is a **window**, not one row: `lib/queueWarm` reads the tags (title,
  artist, album, year, tech, lyrics, MBIDs, the public web rating — one
  payload), the album payload that names the cover, and the cover's colour for
  the track playing and the next `QUEUE_WARM_AHEAD` (3) rows, on the same query
  keys the bar and the fullscreen player read. It replaces the bar's
  next-track-only tags prefetch, re-runs on every track/queue change, never
  re-fetches a payload that is fresh, and fetches nothing while paused. The next
  track's cover **image** keeps its own warm (`PlayerBar`), which was already
  there.
* The now-playing block's gate is `settled` — the strings are final — and the
  art is no longer part of it. It used to hold the words until the cover's
  address was known *and* the image decoded, so a slow cover held the title back
  with it; `useArtReady` and the `cover` argument are gone. The committed record
  is live, so a late album payload or a late image still updates the art in
  place. With the warm, an ordinary handover has everything in the cache and
  commits complete anyway. R344 rewritten.
* The shell's title bar no longer eats the fullscreen player's top row: the bar
  sits in the app's normal flow, but a `fixed inset-0` overlay anchors to the
  **window** and painted its top 2rem under the window controls. `TitleBar`
  publishes its one height as `--mlo-titlebar-h` on `:root` while it is mounted,
  and every window-anchored surface carries `.shell-top` (`top:
  var(--mlo-titlebar-h, 0px)`) — the player, the music-video layer under it, the
  nav drawer, and the docked lyrics pane's own floor. The variable is 0 in a
  browser and on a phone, where the host draws the chrome. R370.
* The lyric pane paints nothing per frame while it scrolls. The 1 px `filter` on
  an inactive line gave the line a render surface of its own, which appears in
  the very frame the pane moves; the quiet emphasis keeps its two other carriers
  (the dim ink and the 0.9 opacity) and `LINE_QUIET` replaces `LINE_BLUR`. The
  `color` half of the emphasis ease went too — colour is not compositable, so it
  re-rasterised the line's text every frame; `LINE_EASE` transitions `transform`
  only now. R372.
* The pane's auto-scroll lands **on** its target: an exponential ease never quite
  arrives, and a measured 12 px step spent its last ~8 frames under a pixel each
  (~130 ms of visible drift). The ease now runs only while it is the faster of
  the two and a landing speed floor (`LAND_PX`, 1.4 px per 60 Hz frame) carries
  the rest; the handover is continuous by construction. R371.

## Smaller

* A stored description can be many screens long, and the only collapse control
  was the "Show less" *after* the text — putting a long one away meant
  travelling to its end. Expanded, `Description` now also carries a control at
  the top, pinned (`sticky top-12`, the app's own line for "under the top bar")
  for as long as the block is being read; its row is `pointer-events-none` with
  the button's own `pointer-events-auto`, so the words passing under it stay
  clickable, and the control at the end stays. Album and artist pages are this
  one component. R373.
* "Add to library" on a row with no MusicBrainz id resolves the release by
  **name**, and the index lists same-named groups of every type: for "All Hope
  Is Gone" by Slipknot it answered the 1-track digital single first and the
  14-track album second, so an album row became the single and its framework
  album held one track. The row's own kind (and a caller's `types` selection) is
  now a preference among the rows the search returned, ahead of the year hint,
  matched through `mlo.release_choice.type_matches`; a search where nothing
  states the wanted type falls through to the year and then the provider's order
  exactly as before, so no match that existed can be lost. R374.
* Import already fetched what was asked — `imports.run_metadata_step` ->
  `apply_metadata` (the artist image when the artist folder holds none, the
  artist biography, the album blurb, each gated by its own switch and never
  overwriting what is stored), and the chained layout pass (script 20,
  `layout_apply` on by default) settles the folder structure inside the same
  run, Trash included. Both are now **pinned**: `tools/test_import_pipeline.py`
  writes a real 1200×1200 JPEG through `artistdata.save_image` with only the
  sources stubbed, asserts an album with nothing ends the step with all three on
  disk, that an edited description survives a second run untouched, and that the
  three switches off write nothing — and it fails if the artist-image switch is
  turned off, i.e. it pins the fetching itself, not the call. R375.

## What proved it

* **the suites**: every `tools/test_*.py` and `tools/test_*.cjs` suite, 0
  failures, plus `npx tsc -b`, `npx oxlint`, `npm run build` and
  `python tools/check_versions.py v5.5.0` (all 12 copies agree).
* **the browser checks**, each on a scratch library: `check_layout.cjs` 5/5
  (new), `check_description.cjs` 18/18 (new, against an 18k-character album
  blurb and a 51k-character biography — 9,467 px of text),
  `check_library_az.mjs` 143/143, `check_fullscreen_player.cjs` 119/119,
  `check_lyrscroll.cjs` 18/18, `check_np_metadata_contrast.cjs` 243/243,
  `check_responsive.cjs` 293/293 on the canonical fixture.
* **live reads**: a running scratch library read back with the layout scan's
  banner ("The last layout scan found 2 problems across 2 categories …"), the
  amber mark with its sentence on a failing artist in the Artists view, Home and
  the artist page, and the strip's new tooltip lines; and the built desktop
  shell driven over CDP for the title-bar inset — bar 32 px and
  `--mlo-titlebar-h: 2rem` live, player top 32, exit button 44–80 with the hit
  test landing on it.
* **the named-add fix, both ways**: `tools/test_add_to_library.py` asserts the
  release group that was matched and the whole tracklist that reaches the
  manifest; with the preference stashed the case matches the single
  ("got [...0001], want [...0002]") and the manifest is empty — the report.
* **known-before failures**, reported rather than passed over:
  `check_player_state.cjs` §1/§6/§7 (5 failures, the natural-end handover) fail
  against that fixture with these changes stashed as well, and
  `check_push.mjs`'s "kinds this device asked for" assertion fails identically
  stashed.