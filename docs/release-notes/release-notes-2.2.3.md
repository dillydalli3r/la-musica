# la musica 2.2.3 - the release's own facts, and one folder per artist

Every fix below answers a report from a real library: an album that could not
be re-picked, a folder that appeared twice, a check that passed when it should
not have.

## Tags (MAINTAIN > Tags)

- **The schedule switch lives in the schedule box.** The on/off used to be a
  checkbox in the page header, four boxes away from the status it governs —
  and the status said "turn it on above". The Schedule card now carries both:
  its status line and the one button that changes it.
- **A refresh lands the release's WHOLE MusicBrainz answer, not just its
  identity.** The genre and the public score were never re-read, so an album
  scored a year ago stayed scored a year ago and a track with no genre stayed
  that way. A refresh now writes the public score (`WEBRATING` /
  `ALBUMWEBRATING`, through script 24's own ladder) and a missing genre
  (the release group's, into empty slots only), from the release payload the
  identity pass already fetched — no second lookup.
- **A value the app wrote is the app's to move; a value you typed is yours.**
  The refresh keeps a small plan (`<music>/.mlo/data/tag_refresh.json`) of the
  scores and genres IT wrote, so a scheduled pass can correct its own number
  and never touches one the file already carried from anywhere else.
- **Missing lyrics can ride along**, off by default (`lyrics_auto_fetch`):
  a refresh fetches a line for an album whose tracks lack one, fill-only.

## Credits

- **Credits are read strictly from the files' own tags.** Opening a track's or
  album's credits asked MusicBrainz for every recording, every time — a
  rate-limited request per track for data the file already states. The panel
  now answers from the tags alone (`PERFORMER`, `PRODUCER`, `ENGINEER`,
  `MIX`, `ARRANGER`, the release facts and the ids), with no network call and
  no failure when the network is down.

## Cover art

- **A release credit is not a different artist.** Laika Come Home is filed
  under "Spacemonkeyz" while every store files its release under "Gorillaz &
  Space Monkeyz", so all 20 candidates were rejected as "a different artist's
  release" and none could be chosen. A row whose credit NAMES the album's
  artist is now that album's release — and a disagreeing space
  ("Space Monkeyz" / "Spacemonkeyz") is the same name, not a different one.

## Grading

- **"FLAC MD5 not verified" is a failure.** A stream that will not decode, or
  one whose digest nothing could establish (no flac/ffmpeg to verify with),
  used to be reported and not charged. It now fails the track — and therefore
  the album. The one state that is still reported and not charged is a stream
  that decodes and states no digest at all.
- **A failed track always fails its album.** The album's own count is re-read
  at the end of a grade: a track that recorded issues without reaching the
  counter is charged once, by name, so no album can pass while a track in it
  did not.

## Importing

- **One folder per artist.** An import that arrived with another spelling, or
  without the MusicBrainz id the naming script appends, opened a second artist
  folder beside the first ("System Of A Down" beside "System of a Down [id]",
  "Spacemonkeyz vs. Gorillaz" beside "Spacemonkeyz [id]"). The album now goes
  into the folder the library already has — matched by the MusicBrainz artist
  id first, then by name with spacing folded.
- **A file whose name states a disc reads by its name.** A rip's "6-06 5_4
  (P.45).flac" carries track 6 of disc 6 in its NAME; read off its
  `TRACKNUMBER` tag alone it shared track 6 of disc 1 with the release's own
  "5/4 (P.45)", so a complete album read as partial AND listed a stray file.
  The name's own disc and position are read first.
- **Match from rip log, in the import menu.** The "Which release is this?"
  dialog offered AcoustID and a name search; a CD rip's own `.log` states the
  disc's whole track table, which IS a MusicBrainz Disc ID. The dialog now
  reads it there (the one lookup that names the PRESSING), shows the disc id
  and the releases for it, and fills the row on a press.

## Track page

- **Candidates, beside Auto-import.** The player loaded the chain's single
  best guess. A new **Candidates** button opens every version the configured
  providers hold for the track — best match first, each with its own length
  against the file's, its match score, its provider and whether it is
  timestamped — so the version this file actually is can be picked.

## Home

- Every album Home draws uses the shared album card (title with its marks, the
  status dot, artist, original year and the rating row), and the "Rated
  albums" shelf no longer puts a second row of stars under it (spec R469).

## Upgrading

Nothing to do. Settings, tags, playlists and history are read as before; the
release carries the same assets as 2.2.2 for every platform.