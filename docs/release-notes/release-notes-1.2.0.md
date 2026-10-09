# la musica 1.2.0 — the grading tells the truth, and the writes stop colliding

The second round of the owner using 1.0.0 and reporting what it got wrong —
twelve reports, each traced to a cause in the code rather than a symptom.

Two threads run through the release. The first is **contention**: an app that
both reads and writes the same files, on a machine where a Soulseek daemon, a
player, a full-disk scan and an antivirus all touch them too, kept blaming the
wrong thing — a rating that "was not written", a track that "took no genre", a
cover that "did not load". The second is **evidence**: the app checked things it
had the means to check and then said less than it knew — a log whose CRCs it
could not compare, an artist folder named without the id it could have looked
up, an advisory marked explicit off another edition's flag.

## Writes and reads under contention

- **A refused tag write is retried, and says who is holding the file.**
  `set_tag: [WinError 5] Access is denied` on a `.mlo_tmp_*` → target replace was
  a **held destination** — a second reader (the app's own player stream, slskd's
  share scan, a sibling job's `flac.exe`, antivirus) without the share mode that
  lets a rename proceed. Every atomic write now goes through one retry
  (`mlo/atomic.py::replace_locked`: six attempts over ~1.5 s on WinError 5, 32
  and 33, the same ladder on the read side as `read_bytes_locked`), the give-up
  message names the file and says plainly that **nothing was written and the
  original is unchanged**, and every library-content rename in the app routes
  through it — audio, FLAC, discs, format-all, AccurateRip, artist data,
  containers, cue, paths, cover and lyrics uploads.
- **The app-state migration no longer aborts on one busy file.** The log's six
  `WinError 32` were `_move_state_dir` giving up the whole move because slskd
  held its own `.mlo/data/slskd.log`; it now retries per file and reports the one
  it could not take, so the rest of the state still lands.
- **A rating is never lost to a running job.** The star click stores the rating
  first, always, and when a job holds the album the tag write is **deferred**
  into a durable intent (`tag_writes` in `ratings.db`) that a pump drains when
  the claim clears — last value wins, drained on restart. The message is
  "Rating saved — the `RATING` tag will be written automatically when the job
  running on this album finishes", never a raw WinError; on the bulk route the
  reply carries `tags_deferred`.
- **An unreadable file is not cached as untagged.** The tag cache stored an
  empty result for a file it could not read, so one transient lock became a
  persisted "untagged" row until the file changed; a failed read now caches
  nothing and the next read retries.
- **An unreadable album folder is a graded row**, not a blanket error that
  erased the album's grade: the reason lands on the album with the issue
  `UNREADABLE_FOLDER` and the readable siblings still grade.
- **A share rescan waits for a write.** The app re-indexes shares right after
  every library change, and slskd's scan holds the very file a tag write is
  replacing. The rescan now asks the app's per-path job claims whether anything
  is being written, re-arms a 5 s timer instead of firing (bounded at 10
  minutes so a wedged job cannot postpone it forever), and the manual Rescan
  answers `deferred` with that explanation.

## Ratings reach the tags, and grading says when they do not

- **A new grading check**: when the app's store holds a track rating and the
  file's `RATING` tag disagrees (missing, different value, wrong scale) the
  album fails, with the fix named. A tag the app never wrote (a Picard value it
  never adopted) is **not** a divergence and does not fail; a missing or
  unreadable file is reported by its own check, never as a phantom mismatch; and
  a user who keeps ratings app-only is not failed at all, because the check is
  gated by the same switch the writer honours.

## The cover the player shows

- **A stale cover name no longer sticks.** The album's cover is renamed by the
  app itself (`front.jpg` → `cover.jpg`), and a payload — or the queue row —
  could hold the old name; the route then 404'd while the cover sat on disk, and
  the player kept that failure for the rest of the track. A missing named file
  now falls through to the album's own cover, a read retries a transient denial,
  and **the embedded picture in the tracks is served** when there is no cover
  file at all (an album being filled, or an embed-only library).
- **The surfaces retry instead of remembering a failure.** Both the player's
  thumbs and the shared `CoverImg` use one hook that re-asks a failed URL three
  times over ~4.6 s and only then shows the placeholder.

## Imports copy what you hand them

- **A dragged or chosen folder is COPIED.** Dropping a folder on the wizard
  **consumed the source tree** — audio, scans, artwork, notes — because the
  ingest route moved whatever path it was given. There is now one origin
  predicate (`mlo/paths.is_app_staging`: anything inside `<music>/.mlo`) and a
  verified byte copy (`copy_path`: temp beside the destination, atomic place,
  size-verified, refuses a short copy, refuses when the destination volume
  cannot hold the source, retries the same sharing violations). App-owned
  staging still drains to nothing, and the reply tells the UI which it did
  (`copied`); a failed copy says the folder you chose is untouched.

## CD rips: X Lossless Decoder logs

- **XLD's TOC and CRC lines are read properly.** The TOC regex accepted EAC's
  `mm:ss.ff` frames only, while XLD writes `mm:ss:ff` — so an XLD log parsed to
  no track seconds at all, the matcher's playtime guard was blind, and a log
  whose file name states no disc could not be attributed to its disc (the audit
  then said "missing CD-1.log"). And XLD writes a `(test run)` CRC line and the
  final one with the same key; they were matched at equal priority, so a track
  XLD had re-read compared against the wrong value. One shared time pattern and
  a split regex pair with an explicit priority fix both, EAC behaviour
  byte-identical.
- **The verdict says which case it is.** A log whose CRCs differ only in the
  `(skip zero)` value is the same audio with different leading/trailing silence
  and is reported that way; one whose CRCs match neither variant is reported as
  **different audio of the same length — another transfer of this CD, or files
  re-encoded since the rip** — instead of the old flat "the rip does not match
  its own log". Verified on the owner's own two albums: a genuine XLD rip
  compares 11/11 real, while a log from another transfer is named as such.

## Metadata that fills itself in

- **Genres are asked for by the album's own tags.** The automatic step was
  gated on a MusicBrainz release id, so an unmatched download (no MB tags, no
  pinned release) asked no source at all and landed `GENRE_MISSING` — while the
  manual button, reading the files' own artist/album, answered instantly. The
  import now asks by the release in hand **or** the album's own tags.
- **Lyrics refetch what the policy refuses.** Any stored text counted as "has
  lyrics", so an untimed lyric under `lyrics_allow_plain = false` was skipped by
  the import and fixed by hand; only an *accepted* lyric is skipped now, and a
  synced one is still never replaced.
- **Dynamic range lands completely.** The DR pass swallowed a refused write and
  counted the album done, `ALBUM DYNAMIC RANGE` was written only to tracks that
  measured, and a helper that failed to start disabled the fallback engine
  instead of using it. The album value now lands on every file, a failure is
  reported per file with its reason, an album whose only outcome was failures
  counts as failed rather than skipped, and the helper's failure falls back.
- **The artist folder always carries its MBID when it can be known.** The naming
  script's artist segment is `Artist [<mbid>]` and an empty bracket group is
  dropped, so an album imported without an artist id was filed under a bare
  `Radiohead` — and the grader could only say "run organize", a script that
  cannot help while the tag names no id. The identity writer now resolves the id
  from the artist's **name**, exactly (a fuzzy neighbour is refused), for a
  single-credit release only; an artist MusicBrainz does not know stays bare.

## iTunes advisory: no false "explicit"

- **Deezer's track flag wins over the edition's.** "Just" carries two ISRCs, and
  the app read `explicit_content_lyrics` — `2` on the "Just (Edit)" single —
  before the track's own `explicit_lyrics`, marking a clean album track
  explicit. The track's own flag is read first, and album/edition-level evidence
  is no longer a per-track route at all.
- **The AI can never be the only voice for "explicit".** A model-only `1` is
  dropped and falls back; a real source saying not-explicit ends the question
  (the AI is not even asked). An existing value the app itself invented is
  correctable by the next import, because an arriving advisory is cleared before
  the step runs.

## Soulseek

- **Browse my share understands structure.** slskd returns the share tree
  already flattened (one entry per folder carrying its whole path), so the modal
  could only list full-path rows. It now builds the tree itself — both
  separators, intermediate folders synthesised when slskd does not list them,
  files under their parent, expand/collapse, child paging so a large share does
  not paint thousands of rows — with the server payload and every other consumer
  of the flat rows untouched. A share index larger than the 48 MB the app reads
  now says so ("Share index too large to list here") instead of claiming the
  account shares nothing.
- **"Port unconfirmed" has a way forward.** The Sharing card runs the existing
  read-only port check on demand, shows the fresh verdict with its `checked_at`
  and the failing rows' own next step, and the chip now says **when** the
  snapshot it shows was read.
- **Share totals and clearing history.** The card shows the share's own totals
  (files and bytes offered, from the share index, with one honest truncation
  story — "too big to count", never a wrong partial) and has Clear controls per
  file, folder, peer and whole history. slskd can only drop all completed
  uploads or cancel a live one, so clearing is an app-side forget list with a
  `before` stamp: a transfer arriving later shows again, slskd's tree is
  untouched, and the audit's own read still sees everything.
- **The obfuscated port is shown** beside the listen port as information ("the
  listen port + 1", which a Soulseek client would call), with the honest note
  that slskd advertises no obfuscated port and nothing needs forwarding to it.

## RYM is first, and honest about when it cannot answer

RateYourMusic was already first in both shipped source orders (genres and
web ratings) and every manual path shares them; the gap was the panel, which
called it "Skipped / needs rym_cookie" on a default install and never probed it.
An archive-backed RYM now reads `ok` and **is** probed (it answers real genre
data from the archived snapshot without a cookie), and a stale cookie surfaces
the exact Cloudflare 403 the app used to write only into the log — in the UI
where the credential can be pasted.

## Mid-import albums are not graded, and can be resumed

- **An album an import is on is left alone.** A per-album marker
  (`<album>/.mlo_importing.json`) plus one predicate decide it, and every
  surface reads it: the grade warning and the Home "needs attention" shelf, the
  library payloads (a neutral row with `importing: true`, checks and grade
  cleared), the library-wide Grade (which skips it; the import's own targeted
  Grade is exempt), and the web — a pulsing badge "Importing — not graded yet",
  the Failing filter excludes them.
- **An interrupted manual import is resumable.** The wizard session writes the
  marker, a folder renamed after the interruption is followed by its marker, and
  startup recovery keeps a session-backed marker (continue from the wizard) while
  clearing an abandoned autonomous one so it cannot hide from grading forever.

## CUE sheets and the console window

- **A `.cue` naming a track the album does not have** is no longer reported and
  left: the surplus entry — its `FILE` line and the `TRACK`/`INDEX` block it
  owns — is removed when the rest of the sheet provably *is* the disc (every
  resolving entry claims a different file, and between them exactly the disc's
  files). Anything else is left exactly as written and reported.
- **No console window flashes.** The windowed server owns no console, so every
  console child now carries `CREATE_NO_WINDOW` — including the ones that were
  missing it (`route print`, `tasklist`, `netstat`, `taskkill`) and the Rust
  helper's own `ffmpeg` children.

## Upgrading

Nothing to do: the app migrates its own state on first start. Two notes for
this release:

- **RYM's live site needs a fresh cookie.** Paste the whole `Cookie:` header
  from a browser that passed Cloudflare into Settings → Discovery → `rym_cookie`
  (it must match `rym_user_agent` and the network). Without it RYM still runs
  from the archived snapshot, which the panel now says out loud.
- **Folders dragged into the wizard before this release were moved, not
  copied.** Their contents are in the library (nothing was lost) but no longer
  at the drop location; from 1.2.0 on, a chosen folder is always copied and the
  originals stay where they are.
