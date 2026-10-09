# la musica 1.0.0 - the acquisition pipeline stops guessing

**The versioning restarts here.** The line that ran 3.x → 6.0.0 → 7.0.0 (whose
last release was never published) is closed and every note it wrote is kept in
`docs/release-notes/archive/`; 1.0.0 is the same app, told as what it is — a
first release whose numbering starts over. Everything that line built is in
this one: the library manager, the player, the phone clients and the
acquisition pipeline.

This release is also one round of the owner using that app and reporting what
it got wrong.

The thread through it: the app knew better than it acted on. A CD told it
`SHM-CD` was not a CD. A track whose sources answered nothing had an AI
configured to look the genre up and was never asked. A run of ten albums
raised ten notifications. A peer at the end of a 500-file queue was waited out
for two hours while three other peers went untried. And a title long enough to
break a filesystem spelled a folder the OS refused.

## Soulseek: a queue is not a download

- **`soulseek_queue_wait`** (Settings → Wishes, **600 s** by default): how long
  one candidate may sit at the end of a peer's queue with NOTHING arriving
  before the app gives up on that peer and moves to the next candidate. Until
  now a candidate's own ceiling budgeted the peer's advertised queue too, so a
  job could hold ONE peer for two hours having transferred nothing. It bounds
  the wait for BYTES, never the transfer: a peer that has delivered anything
  restarts the clock, and a batch shares ONE window.
- **Which peer a job asks first is the one that will FINISH soonest** — the
  album's bytes plus the files the search reported queued ahead of us, at the
  peer's own rate — not the one with the fastest link. A quick link behind a
  500-file queue no longer outranks an idle peer that can start now.
- **A finished file is not a peer going quiet.** Progress was tracked as the
  sum of the pending transfers' bytes, so a file completing subtracted its
  whole size and a healthy download could be abandoned 180 s later as
  "stalled". Bytes are now a per-file high-water mark, so completions (and
  records slskd prunes) cannot read as a stall.
- **Candidates are tested at once** (a batch's `.log` gate no longer waits one
  candidate out before grading a ready sibling, and a batch's CD checks run in
  parallel), and the fallback walk's position, its total and the edition really
  being asked now come from ONE list — the row used to count a different list
  than the worker asked and jump from "release 1 of 5" to "3 of 5".
- **An import run earns ONE notice.** A per-album success and the run's own
  tally are steps now (`server.events.import_step`): they are counted, and once
  nothing has happened for 20 s the run speaks once — "Imported 5 albums", or
  that album's own name and summary when the run was a single album. Progress
  keeps its switches (off by default, and on means at once), and a FAILED
  download still speaks the moment every candidate has been rejected.
- **The shared history card says what the SHARE holds, and can be cleared.**
  Beside the uploads' own totals (`N files · X given`) the card now shows the
  share's OFFER — the file count and byte size of the index slskd serves (the
  configured share folders and excludes, not the library on disk; an index too
  big to read is said to be uncountable, never undercounted) — and clears the
  history at the granularity asked for: one file, one folder, one peer, or the
  whole list. A clear forgets RECORDS only — it cancels no upload, deletes no
  file and changes nothing about the share (slskd keeps its own transfer tree);
  a download that arrives later shows again.

## Every form of CD is a CD

The app had one "is this a CD?" question and answered it by equality over two
values, so an album MusicBrainz states as `SHM-CD`, `Enhanced CD`, `HQCD`,
`Blu-spec CD`, `XRCD`, `CD-R`, `Copy Control CD`, `Mixed Mode CD`, `Data CD`,
`DTS CD`, `Minimax CD`, `8cm CD` or `CD+G` took the DIGITAL path: a folder with
no rip log counted as a complete album, the album was stamped
`MEDIA=Digital Media`, the log-CRC audit never ran, and the grader answered
"Unrecognized MEDIA value" for its own tag.

`mlo.tagtext.CD_MEDIA_VALUES` is MusicBrainz's whole CD family now (matched
case-insensitively and with the separators folded, so `SHMCD` is `SHM-CD`), and
**that one predicate** is what the best-release choice, the search templates,
the Soulseek `.log`/`.cue` gate, the grader's CD checks, the log-CRC audit,
AccurateRip and the `SOURCE` strip all read. Each variant keeps its own
spelling as a MEDIA value — a `SHM-CD` album says `SHM-CD` — while every CD rule
treats it as one. Deliberately not the family: `SACD` (a DSD disc with its own
evidence rules) and the video discs (`VCD`/`SVCD`), which a `CD` label no
longer claims either.

## Genres: RYM first, and a model when RYM cannot

- **RateYourMusic is the first source** for track genres and for web ratings,
  and it now also **browses a genre and shelves an artist page** in
  Discover/Recommendations (its chart filter is what it browses with), so the
  one source the owner trusts is present wherever discovery happens. A chart
  built from an archived snapshot says so; a missing `rym_cookie` is reported
  as a skip instead of a live ask the site would refuse.
- **Cookie import** takes the whole `Cookie:` header, a DevTools cURL dump or
  JSON, and the Cloudflare path (challenge detection on 200 AND 403, the
  Wayback capture with its own pacing, a refusal latch, per-source notes) is
  audited and covered by cases.
- **When NO source answers, the AI is asked to research the genre** — the
  fallback was unreachable: a track nothing answered for was skipped before the
  model was ever consulted, which is exactly the track a researching model is
  for. And a track the chain had nothing for now takes the **album's** genre
  instead of landing graded `GENRE_MISSING` while the album's own genre sat in
  the same payload.

## Names: the title gives way, never the ids

No name the app built had a length bound, so a release whose album title is a
paragraph spelled a folder or file the OS refuses — and the import then failed
with the generic "a file inside it is still in use" sentence. The rule now has
a **length half** (`MAX_SEGMENT_BYTES`, 240 UTF-8 bytes, which leaves room for
the app's own suffixes and for Windows' 260-character path): `eval_script` caps
each free-text value, measures the longest segment and shrinks the cap until it
fits, so the disc/track numbers, the MusicBrainz ids, the brackets and the
extension survive while the WORDS are cut. Every writer goes through it — the
organizer, beets, the import commit, the export, cues, covers, playlists, the
Trash, and the peer-supplied names the Soulseek path creates (which were not
sanitized at all). Both halves stay fixed points, so organizing an organized
library is still a no-op.

## The player: a drag holds the sound

A drag on either progress bar (the bar's own, and the fullscreen one, audio and
video) now **stops the audio for the length of the gesture and resumes when the
pointer is released**, at the position the drag ended on — and only if the
track was really playing, so scrubbing a paused track never starts it. It is a
hold, not a stop: the pause never clears what is playing, so the bar, the
fullscreen player, the media session and the next-track preload are untouched
by a one-second gesture, and the release is listened for on the window as well
as on the bar so a drag that ends off the track still puts the sound back.

## The cover the bar shows

The artwork could go missing for a whole track and stay missing. A cover's URL
is its album folder plus a FILE NAME, and the app's own writers rename that
file inside a folder that does not move — script 5 takes the folder's cover
candidate to `cover.jpg`, and a write that re-encodes a PNG to JPEG drops the
old extension — so a queue row or a page payload built before the rename asked
for a file that no longer existed. Answering "nothing" there was permanent,
because the surface remembered the failure and never asked again. A cover
request now treats the name as a hint: the album's own cover answers it when
the named file is gone, a read the OS denies for a moment (a writer's replace
window, a scanner, an anti-malware pass) is waited out the way every write
already was rather than served as "no cover", and an album whose art lives
inside its tracks serves that picture instead of a blank slot. The player
itself re-asks a cover that failed, three times over the next five seconds,
instead of keeping the disc glyph for the rest of the track.
`tools/test_cover_preview.py` pins all of it.

## Sharing and export

- **The shared history is one row per peer** — how many files they took, the
  total size of them, when they last took something — and a row expands into
  that peer's own files. The size a row printed was the bytes transferred SO
  FAR, so an in-progress upload looked like a 3 MB file; every file now carries
  its own size (with the transferred amount as progress).
- **Sharing is faster out of the box**: upload slots ship as **10** (slskd's
  own default; the app had 2) with the ceiling raised to 50, and
  **`soulseek_share_rescan_minutes`** (1440 by default) makes slskd re-share a
  library that was edited by hand within a day.
- **The export panel on the player bar has real options**: codec and quality
  come from the server's own tables, so MP3 offers **V0…V5, 320/256/192/128
  CBR and a custom bitrate**, FLAC its compression levels, and `copy` exports
  the original with no transcode at all. **Set as default** writes
  `export_codec`/`export_quality` — the same keys the Export page's own button
  writes — and the panel opens on them.

## Also

- `tools/check_push.mjs` asserted a push kind (`wish_found`) that the app had
  dropped in 4.9.0; it now compares the subscription against the module's own
  list, so the two cannot drift again.
- The cover the auto-import writes is the manual picker's own recommendation —
  both surfaces call `mlo.cover_choice` over the same candidate set, and
  `tools/test_cover_parity.py` pins that they land the same image.