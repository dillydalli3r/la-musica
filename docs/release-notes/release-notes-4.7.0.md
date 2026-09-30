# la musica 4.7.0 — the player's metadata arrives whole, and credits name the release

## The now-playing block is one record

The bar (and the fullscreen player with it) used to paint its pieces from four
different clocks: the queue row's own strings arrived instantly, the per-track
tags payload then rewrote the title and appended the release year to the album
line, the payload that names the cover files resolved on its own schedule, and
the image bytes landed last — the owner's "sometimes the title loads in at the
very start, then the artist name then album then cover image".

`web/src/lib/nowPlaying.ts` is now the one source: a record is READY when the
track's tags have settled, its cover's address is known and — when there is art
— that image is **decoded**, and until then the block keeps painting the record
it last committed (the stale-hold the lyrics pane and the tech readout already
use). A surface with no record yet draws its rows empty rather than a filename
stem, a folder name or a "—" that the real values then replace, and
`NOW_PLAYING_WAIT_MS` (1.5 s) caps the wait so a stalled source ends in the old
behaviour instead of a permanently blank block. Measured on a track change:
the strings and the decoded cover appear in the same paint (0.1 ms apart).
Spec rule R344; pinned by `tools/check_player_state.cjs` §11.

## Credits say which release they are about, and list every relation

The Credits popout printed a raw path where a title belongs
(`/music/…/1-04 How to Disappear Completely [uuid] [uuid].flac`) and listed far
fewer relations than MusicBrainz states.

`GET /api/credits` now returns an **identity block** — title, artist, album,
album artist, label, catalogue number, barcode, date, original date, country,
release type, medium, the five MusicBrainz ids and the path, every key always
present and empty where nothing states it — and the panel draws it as a header:
the title as the heading, one label/value line per fact with its own copy
button, the ids last and the path last of all, small and dimmed. Under it, EVERY
row the payload carries, grouped by role in a sensible order (work and its
authors, the people in the room, the studio, the packaging; a role the table
does not know sorts last alphabetically rather than disappearing).

The rows themselves are wider now, out of the one cached release response: an
album request merges the release's own relations, **every track's recording
relations** and **every work node's relations** (which is where composers,
lyricists and writers live), and a track request does the same for its own
recording and work. `tidy_credit_rows` normalises (lower-case role, dedupe,
drop empties) and drops nothing else. The files' own credit tags remain the
FALLBACK — `source: "tags"` — and only when MusicBrainz answers no rows at all,
so the badge beside the rows always says which kind of evidence they are.

An empty row list is an ANSWER, not a 404: a file with no MusicBrainz id and no
credit tag still states its title, artist and album, and the 404 that used to
answer it is what left the raw path as the header. The only refusals left are
"no such file/folder", "outside the music folder" and "an album holding no
audio". Spec rule R345; pinned by `tools/test_credits.py` and
`tools/test_mb_search.py`, with the modal itself checked by
`tools/check_menus.cjs` (heading is a name, facts and ids drawn, path last).

## The queue carries its own infinite-playback switch

`infinite_playback` had one Settings row; a listener wondering whether the queue
keeps going had to leave the queue to find out. The switch now sits where the
queue is: a checkbox in the player bar's queue popover and an `∞` chip in the
fullscreen player's queue drawer, both writing the same config key through the
same POST. Neither is drawn on a server too old to ship the key, so an install
behind the release cannot write a setting that would be dropped. R324.

## The queue menu's chrome follows the cover

The two hairline separators in the fullscreen player were a fixed
`bg-white/15`: 1.19:1 on a white cover and 1.02:1 on the mid-grey one — the
separators the owner reported as fading into the artwork while the icons either
side of them had already flipped. They now take the ink table's own polarity
(`ink.divider`; the dark table's white stays, the light table draws the same
weight in near-black, measured 2.95:1 and 4.71:1 on a white cover).

The same table fixes the LIT controls: the like heart's favourite state and the
open add-to-playlist button light with `--accent`, which ships WHITE, so on a
bright cover the lit state was the invisible one (~1.1:1). `litInk()` keeps the
accent when the chosen field can show it and falls back to that table's full ink
when it cannot, so a custom dark accent still lights the glyph over a bright
cover. `FavHeart` gained one `likedClass` override for it; every other heart in
the app still lights `accent`. `tools/check_np_metadata_contrast.cjs` grew 16
assertions per cover for both families and passes 243/243.

## Refresh means the whole library, not just the page's payload

Both Refresh buttons re-walk the music folder SERVER-side, but the grading strip
reads its own summary (`GET /api/grades/summary`) — so a press that only
refetched the page's payload left the strip quoting the counts from before the
walk for its whole 5-minute staleTime ("even after pressing Refresh this warning
doesn't get updated"). The Library's and Home's Refresh now re-ask the summary
with everything else the walk drives; `tools/check_page_states.cjs` pins it as
the request claim it is. R232.

## Optimize tags is one press away in the album's own menu

Script 23 (`Optimize tags`) was in the registry-generated menus — an album, a
track row, an artist, a playlist, the library's selection dropdown — but the
album page's own hand-written "All album actions" flyout listed a curated subset
(1/2/3/5/6/7/8/4) and did not carry it, which is the menu a reader who just saw
an excess-tag or alias failure opens. It does now. R343.

## The bar's controls stop overlapping, and the seek bar answers under a menu

At 1024 px the player bar's right column handed its controls less room than
they need (they are all `shrink-0`), and a column that cannot shrink spills
BOTH ways — right over the Lyrics and Fullscreen buttons, left over the seek
row. The Download button sat exactly on the Lyrics button, which is why the
Lyrics control was dead there. The bar now uses its full grid only from `lg`
(below it the bar has always had a phone-shaped row, which carries the same
transport, the like and the way into the fullscreen player), the flank is its
own query container so the up-next/queue-position readouts measure the room
THEY have rather than the window's, and the four flank icon buttons are a
little tighter. A rect sweep over thirteen viewport/sidebar combinations now
finds zero intersecting controls and nothing painted past the bar's right edge.

And the bar's own flyouts (the queue, the sleep timer, the playlist menu) drop
a full-viewport click catcher so a press anywhere else closes them — which also
swallowed presses on the SEEK BAR underneath. Scrubbing is the one bar control
that should answer while a menu is open (reaching for the playhead means moving
the track, not dismissing a menu), so the seek input now sits above that
catcher and below the menu itself; an outside click still closes the menu.

## Verified

- `tools/test_credits.py` (identity filled per request kind and offline, the
  work's own roles merged on both branches, the non-destructive tidy),
  `tools/test_mb_search.py`, `tools/test_script_menu.py`,
  `tools/test_script_menus.py`, `tools/test_config_ui_parity.py`,
  `tools/test_tag_hygiene.py`, `tools/test_recommendations.py`.
- `tools/check_np_metadata_contrast.cjs` — **243/243** over four stubbed covers,
  now including the separators and the two lit controls.
- `tools/check_page_states.cjs` — PASS, with the Refresh pin on both pages.
- `tools/check_player_state.cjs` — **56/56**, §11 (one-paint metadata) and §10
  (infinite playback at both ends) included.
- `tools/check_fullscreen_player.cjs` — **111/111**; the corner strip's reveal
  is now read after it SETTLES (a mid-transition 0.9905 was being asserted
  against 1, which failed the phone passes and, on a loaded machine, the pass
  itself).
- `tools/check_menus.cjs` — PASS; the new credits section, and the lyrics pass
  at 1024×700 after the bar's right flank stopped overflowing (the Download
  button used to paint over the Lyrics button; the bar's three columns now
  share only widths they can each hold, and the readouts inside the flank are
  gated on the flank's own width rather than the window's).
- Live: the credits panel on a running server (name heading, facts, path last),
  the queue's own switch in both directions, a script-23 press from the album
  menu (`POST /api/run {"ids":[23]}`), and the refresh buttons.
- `npx tsc -b`, `npm run build`, `npx oxlint` clean.
