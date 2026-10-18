# la musica 2.2.1

Faster imports, faster scripts, a faster tag refresh — and a pull date a track
cannot ship without.

## Every step that only READS now runs beside the writes it used to wait for

The audit found three places where the app waited on itself rather than on a
service:

- **MusicBrainz no longer holds its gate while a request is on the wire.** The
  1 req/s etiquette stays (one global budget, shared with the write path), but
  `mb_get` now *reserves* the next slot under the lock and sleeps outside it.
  A slow MusicBrainz answer used to block every other MB caller for its
  latency **on top of** the gap; now the queue moves at exactly 1 req/s and
  never stalls behind a socket. Two callers can no longer wake on the same
  slot.
- **The discovery hosts each wait only their own limit** (`server/discovery.py`).
  The per-host gate now books its slot and releases the lock before sleeping,
  and a host that publishes no 1 req/s policy waits only for what it does
  publish — so the free lane (artist images / descriptions, scripts 25/26) no
  longer queues behind every genre and advisory ask at a flat 1.05 s.
- **The script chain's album lane is still serial per album** (that is the
  correctness contract for tag writers), while independent steps keep running
  beside it; the import's own lookups were the real wall, and they are below.

## An import writes each file ONCE per stage, not five times

Measured before/after on the same album: **5-6 full container rewrites per
file** became **one per stage**.

- The release identity and the GENRE land in **one deferred save per file**
  (`server/imports.py` `_stamp_release`): the genre network lookup happens for
  the whole album *before* the pass, and the pass opens each file once.
- The bulk path no longer stamps the identity a second time before the naming
  script; the identity lands in `finish_album`'s one-pass genre step.
- **Every MusicBrainz writer is idempotent.** `fill_release_identity` plans the
  exact writes first (empty values skipped, the sharpen/widen rules applied, the
  list-completion rule kept) and touches **zero bytes** when the file already
  holds them. The end-of-import refresh therefore costs reads, not rewrites,
  and a repeat refresh of an album refreshed moments ago answers in ~3 ms.

## The tag refresh queue is as parallel as its one rate limit allows

- Default `tag_refresh_workers` is **8** (1-16); each album is one MB lookup
  (the gate above) plus one idempotent write pass, so the workers overlap file
  I/O instead of sleeping behind the lock.
- A job resets the status memo **once at start and once at end**, never per
  album, and marks the assembled tree for its ordinary stale-while-revalidate
  pass instead of rebuilding it.
- Measured on a scratch album: one refresh of 3 tracks **1.08 s** (MusicBrainz
  latency included, MEDIA Vinyl → CD), an immediate repeat **2 ms**, the Tags
  page read 0.24 s.

## Scripts: the remaining repeated I/O is gone

- An unscoped Run All resolves its album list **once** (the preflight and the
  chain scan shared one walk); nested targets are walked once.
- The grader reuses the album listing it already holds for sidecar covers,
  translations and the disc map, and resolves a folder's stored case once per
  run instead of per album.
- The leftover sweep lists each folder once; the loudness pass opens an album
  once for ReplayGain completeness and DR; instrumental detection runs as ONE
  pooled pass instead of one call per track.

## A track without a MusicBrainz pull date now fails grading

New check **`grade_check_mb_updated`** (issue `MB_UPDATED_MISSING`, ON by
default, Settings → Grading → *MusicBrainz pull date*): every parsed track must
carry `MUSICBRAINZ_UPDATED`. The album's age is its oldest stamp, so a file
without one cannot be dated — and MAINTAIN → Tags cannot act on the album.
Off, the check is neither graded nor counted.

## The desktop app no longer opens on a sign-in screen

The shell's gate treated "still asking" as "no server" and drew the sign-in
form for a second or two on every boot. It now shows a neutral
**"Starting the local server…"** splash while `/api/auth/status` is in flight
(re-asking every 2 s instead of waiting for the 60 s poll), opens the app the
moment the server answers `required: false` — including clearing a stale
"sign out" latch — and only then falls back to the address field after a
bounded wait. No screen before the shell renders an empty page.

## Also

- New suite coverage: `tools/test_tag_refresh.py` (stamps, rollup, queue,
  scheduler), and the grading/import suites pin the new behaviour.

Contract: spec **§7.87 / R454–R458**. Version 2.2.1 across all twelve copies.