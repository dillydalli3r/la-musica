# la musica 3.22.0 — the import lands complete, and the phone hears about it

Issue #48, in the owner's words: albums downloaded from Soulseek arrive missing
data — *"this PATH is said to be wrong"*, *"Missing cover image"* — the
"import complete" notice appears before the import is done, and push should
reach web, mobile and desktop. Every one of those turned out to be a real defect,
and two of them were not where they looked.

## The grade said the path was wrong

It was right about the symptom and wrong about the cause: the folder really did
not match what the tags implied. *Auto tagging* (script 8) is the **last** writer
of the tags the naming script reads — it sharpens both dates and widens the
release country — while the chain's only full rename is script 14's, which runs
earlier. So the name on disk was the stale tags' answer, and Grade — the last
script in the chain — reported every file of a perfectly tagged album as
`PATH: expected '…' (run organize)`.

Script 8 now finishes by re-applying the naming script to exactly the albums
whose release tags it filled, and reports where they are so the rest of the chain
follows. Measured on the repro: the same grade run reads **`PATH issues: 0`**
(before: 2), and the folder it accepts is
`[Album] 1994-08-22 - 1997-03-25 - Dummy {US - CD - GO!-CD-1} [Go! Beat] [94eeba12-…] [group-1]`
(`spec R199`).

A framework album — the folder an *Add to library* creates before anything has
been downloaded — used to keep the name it was given from the MusicBrainz
payload, because the organize step adopts that folder by identity. It is now
moved onto the name its tags produce, marker inside, wish re-pointed to where the
album really is, and never merged onto a name a real album already holds
(`spec R200`).

## Missing cover image

The framework album arrives with a cover: the release group's front from the
Cover Art Archive. That stand-in was being **deleted** on the way in, before the
cover step could say whether it had anything better — so an album whose every
candidate fell under the cover minimum ended with no cover file at all, and the
grade said "Missing cover image" while an image of that very release had been on
disk a moment earlier.

The stand-in is treated as "no cover yet" now: the real one is fetched over it, it
is dropped only once that has been written, and if nothing clears the minimum it
**stays**, with the step's own note saying the album is keeping the framework
artwork. `_drop_placeholder_cover` refuses to take a folder's last cover away
(`spec R201`).

## "Imported" now means imported

Two separate lies, both fixed. A naming-script failure used to **abort** the
auto-import — the album was in the library, and the cover step, the chain, the
grade and the notification never ran. It is reported now, not raised: the album
is kept (`partial`), the files that stayed in the download folder are named in
the job's own line, and the pipeline still finishes.

And `import_done` fired as the *first* statement of the gap phase — before the
grade, before the prompt an album may need. The notice is now emitted where the
pipeline is really finished; the asserted order of the notification seam is
`import_started → gaps → prompt → import_done` (`spec R202`).

## Push, for a client that is not open

Until now everything was one WebSocket: a closed app heard nothing. The server
signs with VAPID (RFC 8292) and encrypts per RFC 8291 `aes128gcm`; RFC 8291's own
Appendix A vector is reproduced byte for byte in the suite. Subscriptions live
beside the sessions and users — one row per device, carrying the kinds that
device asked for — and a device the push service reports gone (404/410) is
pruned. The emit path only queues: a push that explodes can never fail the
import that earned it.

`import_done` is one of the kinds push carries, which is what closes the loop the
issue asks about. Settings → Notifications gets a switch per device and a **Send
a test notification** button, and it tells you the truth about the client you are
looking at: the desktop shell notifies only while it runs (no service worker in a
Tauri webview), iPhone and iPad need la musica on the Home Screen (iOS 16.4+),
and a plain-http page has no push at all — the switch is not drawn where it could
not work (`spec R203`–`R205`). The signing key lives in `webpush.json` beside the
state and deliberately **not** in the config, because `GET /api/config` hands the
config to every signed-in client.

## The re-run that re-audited everything

`canonical_value` was called in the audit's evidence check and never imported.
The `NameError` landed in a bare `except`, so script 6 **never read a stored
verdict**: the skip, the stamps and the counters were dead code, and a second run
over an unchanged album re-decoded every file, forever.

Restoring the import is what took a 20-track re-run from 20 `flac -t` decodes and
1.32 s to **none and 0.03 s**, with the AUDIT tags and the grade output identical
— and the evidence record now carries the audio identity (FLAC's STREAMINFO MD5)
alongside size and mtime, because the tags scripts 7–16 write afterwards move
every file's mtime while the audio sits untouched (`spec R206`–`R207`).

The pass also settles what it did **not** change: the decoder is not shared
between scripts. Each decodes for its own question — rsgain's EBU R128, the DR
meter's per-channel PCM, librosa's mono 22.05 kHz — no two want the same
artefact, and the decodes scripts 12 and 16 pay are 1–3% of their own analysis,
while holding one album's samples to pass along would cost 636 MB of RAM
(`spec R208`).

## A download you asked for finishes by itself

Two gaps, both of which made the app wait for a press it had already been given.

**A download started on the Soulseek page never imported itself.** A wish did —
the whole point of the wishlist is that nobody is watching — but a release you
found and pressed download on stopped when the bytes arrived and waited for you
to open the queue and press *Import*. The three page routes now record the intent
when slskd takes the files, and a watcher imports the album, runs the chain and
notifies exactly as a wish does (`spec R209`). It honours the same gate as every
other unattended import (`import_autonomy: automatic` and `manual_import_enabled`
on), because an install that wants to decide must still decide — the manual route
is the answer to "not this one".

**The background path could never acquire a lossy-only album.** Wishes and artist
watches pass `confirm_lossy=False` — nobody is there to answer a prompt — and the
candidate walk gave up the moment the only copies were MP3, so a release that
exists only lossy sat in the queue with *"a lossless copy is preferred"* and
nothing else, forever. `soulseek_auto_lossy_policy` decides it now: **`never`**
(the default, and byte-for-byte the behaviour and sentence until now) or `best`,
which takes the best lossy candidate the ranking already offers — and says so, in
the job's own log, in `download_done`'s body and on the row, because a lossy album
that arrives silently is the bug this key exists to avoid (`spec R210`). The
interactive path is untouched: with `best` set, a press still parks on the
question, because there somebody is there to ask.

**And a retry timer was wrong**, which is how a wish came to be re-searched every
couple of minutes instead of every `wishes_interval_hours`:
`wishes.mark_wanted` skipped writing `retry_at` when it was given none, so the two
settles with no backoff — an empty search, a background re-arm — inherited the
*previous failure's* stamp, already in the past, and every 120-second tick read it
as "due now". Both write the stamp they mean now (`spec R211`).

## Upgrading

The server gains one dependency, `cryptography` (pinned in
`server/requirements.txt`); everything else is behaviour. A library that has been
audited by an older version re-audits once — its records carry no audio identity
— and skips from then on. Nothing needs re-importing: the naming fix applies the
next time the chain runs over an album, and an album whose folder was named from
the MusicBrainz payload is moved onto the name its tags produce then.
