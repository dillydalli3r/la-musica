# la musica 4.7.1 — credit links go to MusicBrainz, and the queue extension says what it did

## A credit's name opens MusicBrainz, never a page in the app

4.7.0's credits panel linked each credited person to their own page IN the app
(`/artist/mb:<id>`), with a small outbound icon beside it. That is the wrong
target for a credit list: most of the names on one are people the library does
not hold — a session player, a conductor, an engineer — so the in-app link
lands on a page with nothing to show. Every name with an id now opens
MusicBrainz itself (`https://musicbrainz.org/artist/<mbid>`, and a `work` row
its own work page), in one link, with the id as the evidence for it. A row with
no MusicBrainz id stays plain text. Verified on Abbey Road's credit list: 147
links, none of them in-app.

## Infinite playback says what it did

The switch could not be told from a broken one: whether it appended five similar
tracks, got nothing back, or could not reach the library at all, the app said
nothing — the queue simply ended ("Keep playing past the end of queue doesn't
even do anything when enabled"). Every ask now records its outcome in the
player's own report (`queue-extend`: the seed count, what the scorer offered,
how many rows were actually added, `why: "repeat-one"` for the one by-design
no-op, and the error text when the route refuses — Settings → Downloads →
Playback report), and the two outcomes a reader cannot tell apart from a dead
switch now SAY so:

- nothing new to add (an empty answer, or every row of it already queued):
  *"Infinite playback — nothing similar left in the library to add; the queue
  ends here"*;
- the request refused: *"Infinite playback — could not reach the library: …"*.

A successful extension stays quiet — its rows are in the queue pane, where they
were always visible. Nothing else about the feature changed: the batch is still
a handful of the library's similar tracks, appended as ordinary rows, before the
last row ends.

## Verified

- `tools/check_player_state.cjs` — 56/56, including §11 (one-paint metadata) and
  §10 (infinite playback end to end).
- `tools/check_menus.cjs` — PASS (its credits section drives the panel on an
  album and a track row).
- Live on a scratch server carrying a copy of the owner's own albums: the queue
  grew 17 → 22 → 27 → 32 → 37 → 39 while an album played, and the album's last
  track handed over into the added set; a route forced to 500 and one forced to
  an empty answer each raised their own message, and a real ask left its
  `queue-extend` row in the playback report.
- `npx tsc -b`, `npm run build`, `npx oxlint` clean.
