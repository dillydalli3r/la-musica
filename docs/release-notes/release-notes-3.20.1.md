# la musica 3.20.1 — the import pipeline runs an album once, and a disc you cannot check is not a failure

Every item below came from watching a real library work through a bulk import on
the running app. The house rules are in `docs/OPTIMIZATION-GRADING-SPEC.md`
(`R178`–`R182` new in 3.20.0/3.20.1).

## One release is one tile, even mid-import

A release that exists as two folders — the framework album the app creates for a
queued download, and the album the audio actually landed in — was listed TWICE.
The chain clears the marker at its own end, so the duplicate tile was there for
minutes, and a run that never reached that end (a review stop, a crash) left it
for good. The library payload now answers what the folder names cannot: the
placeholder YIELDS to the album that has audio, matched on the release id (the
release-group id as a fallback) — never on the folder name, because the two
folders are named differently on purpose. A placeholder nothing has filled keeps
its row (`spec R179`).

## One album is imported once

A second import of an album another job was already importing queued behind that
job's claim and then ran the WHOLE pipeline again — the pre-chain lookups and
every script, over an album the first caller had just finished. Two autonomous
paths for one album (the download's own chain and the import queue, an
auto-import and a bulk import) is the normal way it happened. The second caller
now answers "already importing" and runs nothing; a job importing its own claim
— the download that holds the album from its first byte and then imports it — is
never treated as a duplicate, and your own press of "Run the import chain" keeps
its 409 naming whoever holds the album (`spec R180`).

## A disc AccurateRip cannot check is not a failed check

The AUDIT readout counted a CD without a `.accurip` verdict as a failed check —
"nothing established the CD verdict's 'accuraterip' evidence for 14 track(s)" —
while the same sentence admitted it: a pressing the AccurateRip database has
never seen reads exactly like a disc with no `.accurip` at all, and no code path
can tell the two apart. That line is now what it says it is: **Not checked**,
reported in the grade's own notes channel and charged to nobody, so an album is
judged on what could actually be measured and its verdict is still never
guessed. The legs the app's own artefacts decide (a LOG_GRADE, the log's CRCs)
keep their charge — those are the app's to fix (`spec R181`).

## The app's own description.txt is a family

`description (2).txt` shows up in album folders from outside the app — a file
manager, a sync client, an older build; the app's writer replaces the file
atomically and never makes one. It is still the album's description, so it is
now READ (the canonical name wins when both are there), and the library scan
reports it as a `sidecar_copy` with a one-click rename to `description.txt`
instead of calling the app's own file dead weight. Two descriptions side by side
stay yours to sort out — the app does not guess which text is right
(`spec R182`).

## Fix: a finished download stays *In progress* until its import is done

The queue's row for a release flickered through *Completed* for the seconds
between "the album landed" and "the chain said it started", then jumped back.
The album's claim is the one thing that knows it is being written right now, and
the queue now reads it (one registry, one matcher), so a download whose import is
running stays in *In progress* with the holder's name on it.

## Fix: the download's notice no longer says "imported" too early

"Downloaded and imported into your library" arrived while the import pipeline —
artwork, metadata, then the configured script chain — was still running, so a
notice that read as "finished" came minutes before the album was. It now says
what it is: the DOWNLOAD is done, the import is still finishing, and one more
notice follows when it really is.

## Fix: wishes retry on their own clock, and a walk that keeps failing stays quiet

A transient failure's backoff (30 minutes, doubling) was swallowed whole by the
periodic interval (6 hours), so a peer that was simply down was re-asked six
hours later and the time the row showed had nothing to do with the failure it
followed — and a release with a ranked walk that spent its attempts ended in
*Failed*, a section meant for things a person has to deal with. A failed wish is
now due at its backoff, a wish that merely found nothing is due at the interval,
and a wish carrying a walk goes to the background instead of failing
(`spec R178`).

## Fix: Refresh re-walks

The Library page's Refresh (and Home's) dropped the tag, identity and
recommendation caches but not the library payload itself, so pressing it
answered from a cached tree — which reads exactly like a dead button after a
file was added or a script was run. Both buttons now share the one drop.

## Fix: the Checks & scripts header

Its Discard/Save pair was laid out with a zero-width actions box (a long
subtitle took the whole row), so the buttons were drawn across the blurb's own
text; the box is sized from its content now and shrinks with the row instead of
vanishing. The page's header also no longer pins itself: 160 px of description,
counts, buttons and filters held over a list of a hundred-odd rows covers most
of a laptop viewport, and rows scrolling under it read as broken.

## New: Generate AccurateRip, and *Run all* where it was promised

- The album's **Tag actions** menu carries **Generate AccurateRip (.accurip)**
  — script 9's plain run: each disc's `.accurip` is written where it is missing
  and one that already matches is left alone. The Force entry beside it still
  rewrites whatever is there.
- The import wizard's Finish step **Run all** is back — it runs every ticked
  script (the import chain until you change them). It is a run of the SCRIPTS,
  not a re-run of the import chain: pressing that re-fetched links, genres,
  cover art and the advisory and emptied the arrived values of the four families
  an import decides, undoing work just done by hand in the same steps.
- The **fullscreen player** carries the playing track's star rating, beside the
  album and artist lines: half stars, click the value already set to clear it,
  keyboard included — the same control the player bar and the track rows use,
  and one rating behind all of them.

## New: album ratings on the grid cards, and a library that keeps up

Library and Browse grid cards draw the album's own rating under their caption
(read-only — the table row and the album page are where it is edited), so a
shelf of covers says how you judged them, not only what they are called. And the
pages that draw what an import or a tag write changes now refresh themselves the
moment the outcome lands, instead of waiting for the next visit.

## Defaults: five pressings per walk

`soulseek_fallback_candidates` ships 5 rather than 3. A rank is a guess about
which pressing is best, the walk exists because the network disagrees with it
often enough, and each candidate costs one quiet search window — the walk stops
the moment a usable folder appears.

## Upgrading

Nothing to do. `soulseek_fallback_candidates` ships 5 from now on; a config file
that already stores the old 3 keeps it (a saved value is a saved value), so set
it to 5 in Settings → Downloads if you want the longer walk. Every other change
is behaviour in place, and `description (2).txt` files become readable — and
renameable from the library scan — without touching a byte of them.
