# la musica 4.9.2 — RateYourMusic rates tracks too, and MusicBrainz's disambiguation comments become tags

Two corrections to 4.9.x, both from the owner reading the app against the
sources themselves.

## RateYourMusic is a TRACK source, not album-only

4.9.0/4.9.1 treated RYM, Album of the Year and Discogs as album-level and
MusicBrainz as the only track-level source. That was wrong about RYM: it rates
songs as well as releases, at
`rateyourmusic.com/song/<artist-slug>/<title-slug>/` with its own aggregate —
Paranoid Android reads **4.67 from 17,654 ratings**.

So a track's `WEBRATING` can now come from **two** sources, and its
`WEBRATING_SOURCE` reads e.g. `RateYourMusic; MusicBrainz`. MusicBrainz stays on
because it is the fast, credential-free floor: it needs no cookie and no archive
leg, and its track answer costs one throttled request with no slug guess.

How the RYM lookup works, and what it costs:

- The song page is found by slug (the same generator the release pages use,
  verified against real URLs: `Lady Godiva's Operation` →
  `lady-godivas-operation`, `Earth, Wind & Fire` → `earth-wind-and-fire`), and
  the answer is believed **only** when the page's own `<title>` names both the
  asked title and the artist — a mismatch is a miss, never a guess. (A
  whole-page text search was tried and rejected: a song page names every other
  track on the album in its rating widget, so asking for the wrong track passed
  it.)
- The walk is deliberately short — the bare slug and one `-1` spelling, newest
  archive capture only — because a track rating is one datapoint and a miss is
  the common case. That bound cut the worst case from ~15 requests to 2 per
  track; measured on a two-track OK Computer album, **63.2 s → 43.4 s** (a
  track RYM has nothing for: **61.4 s → 33.7 s**).
- A miss is cached 30 days like a hit, so the first pass over a library pays it
  once. MusicBrainz and RYM's other sources are untouched.

Known limits, stated rather than hidden: a title RYM files under `-2` is never
tried, a bare-slug page wins over RYM's own `-1` entry for the same name and artist
(the two are indistinguishable by a name check), and a song RYM credits to
multiple artists or under another spelling is a miss.

## MusicBrainz's disambiguation comments are now tags, and they render

MusicBrainz states a **disambiguation comment** for most entities and shows it
after the name — the owner's example being the release group
`1967–1970 ("The Blue Album")`. It is stored as three separate tags now, one per
entity level, and drawn where the name is:

| tag | level | what it holds |
|---|---|---|
| `ALBUMDISAMBIGUATION` | every file of the album | the release **group**'s comment ("The Blue Album") |
| `ARTISTDISAMBIGUATION` | every file of the album | the credited artist's ("UK rock band") |
| `TITLEDISAMBIGUATION` | the file's own track | the recording's ("2011 remaster") |

They follow the alias family exactly (`TITLEALIAS` and friends): same container
spellings — a Vorbis key, `TXXX` for ID3, `com.apple.iTunes` freeform for MP4 —
written by the same MusicBrainz stage (`mlo.autotag`, script 8 and every
import), registered in the app's tag vocabulary so the excess-tag grade and the
strip passes leave them alone, and read back by `server.library` for the UI. All
three ride a MusicBrainz payload the app already fetches, so they cost **no
extra request**.

The app renders them the way MusicBrainz does: plain text in parentheses
immediately after the name, one shade dimmer — on the album card, the album
header, the artist page header, the library's album rows/table and artist rows,
Home's artist shelf, Favorites' artist table, every track row, and the track
page. An absent comment renders **nothing** (never empty parentheses).

One consequence worth knowing: whether MusicBrainz states a comment is only
knowable from the release, so unlike an alias it cannot open an Auto Tagging
"slot" on its own (that would cost one request per album on every tagging run).
The comments therefore land on every **import** and on any tagging run whose own
slots are open — an album that already carries everything else is not re-asked
just for a comment.
