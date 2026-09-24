# la musica 3.23.4 — the columns come back, and everything after 3.23.3

3.23.4 carries the two test changes that keep 3.23.3 honest, plus the fixes and
asks that followed the release: the Library's Albums / Artists / Tracks tables,
the search field's magnifier, links (and marquees) in the player's metadata, the
Library opening grouped by artist, an edition ranking that prefers the earliest
pressing — for the queue too — and music videos YouTube cannot serve falling
back to Soulseek.

## The strip stops contradicting itself

The owner's Library showed a failing album and, in the same sentence, **100% of
checks pass** — with the headline reading "1 album falls the library's grading
checks", which is missing its complement. Three things were wrong and all three
are fixed:

- **A percentage can no longer round a shortfall away.** One failed check in
  10 281 is 99.99 %, and `round(…, 1)` printed it as `100.0` beside the album
  that failed it. The rule is now one function (`mlo.grader.printed_pct`) used
  by **every** surface that prints a percentage — the Home header's library
  score, an album row's `Fail · N%` badge, the strip — so two of them can never
  disagree: 100 is printed only by a score with nothing failed, and a shortfall
  prints `99.9`.
- The headline is a sentence: **1 album falls short of the library's grading
  checks** / *2 albums fall short of …*.
- A finding whose reason was a code fragment now carries the instruction:
  `AcoustID id missing (run Fix AcoustID pairs)` instead of `acoustid id`, from
  the grader's own message.

## The fullscreen player's ink and toggles

The lyric lines and the metadata carried a **ghost border** — a stack of wide
shadows at 0.3–0.42 that read as an outline around every glyph on a bright
cover. The opacity is halved and the layers are softer (the densest point about
a third of what it was): what is left is depth under the line, not a stroke.
The **visualizer** and **lyrics** controls in the fullscreen player now obey the
app's own convention too — an engaged visualizer is the accent ink, a
disengaged one is dimmed with full ink on hover, instead of both states reading
as the same pale grey.

## The Library opens grouped, and a running job is not a fault

- **Group by artist is ON by default** (and remembered once you change it): the
  flat run of every album is the exception, not the opening state. That switch
  also exposed a real bug in the preference hook beside it — a key that had
  never been written read as a stored `false`, so a switch whose default is ON
  opened OFF for everyone who had never touched it (`useLocalPref` now honours
  the default it is given).
- **An album a running job holds is not a finding.** The grading strip counted
  an album mid-run as failing — the owner's screenshot showed it reporting a
  missing `ALBUMITUNESADVISORY` while the chain's own Auto tagging step was
  still queued behind Beets — so the strip now skips any album a live job claims
  (the same registry the album row's "Script run" chip reads), and refreshes
  when the live job set changes so the row returns to the truth the moment the
  run ends.

## The Library's table views lost their columns, and now cannot

The owner's Albums view drew its expand chevron and nothing else; Tracks drew a
row number; Artists drew one name. The tables were fine and the data was
complete — the **column-visibility preferences** were not. They persist in
`localStorage` under a key that is versioned with the column ids, and the reader
that restored them kept whatever ids it still recognised: a list written before
the ids changed left a handful of columns standing and silently hid every other
one — nothing in the Columns menu looked wrong, because a column the old build
never offered was never unticked.

- A prefs list from the previous key is now **migrated, not trusted**: the ids
  it still has are kept (your deliberate choices survive) and every column this
  build ships visible by default is restored, because such a list cannot be
  evidence about a column that did not exist when it was written. Untick
  anything afterwards and the choice is stored under the new key for good.
- The Artists view gained an **avatar** per row: the artist's own picture when
  the folder holds one (`GET /api/artist/image`, asked only when the payload's
  `has_image` says it will answer) and a representative album cover otherwise.
  The count column is now labelled **Releases** — it is the albums of that
  artist in your library, pending ones included.

## The search field's magnifier was behind its own input

The top bar reserved 40 px for the magnifying glass at the left of the search
field and then painted the input's translucent background **over** it, so the
placeholder began 40 px in with nothing in the gap. The icon is above the input
now (`z-10`, `pointer-events-none`), and the narrow-field rule that hides it
below 192 px still drops the inset with it.

## The player's metadata is a door

The title, artist and album in the now-playing bar and in the fullscreen player
are links to the track, artist and album pages — a plain click opens the page,
and the surrounding block's own click (the bar's block opens the fullscreen
view) does not also fire. A playing row whose album or artist is not in the
library keeps its plain text rather than linking to a route that cannot resolve.
Long artist or album names scroll like the title does.

The lyric **−/+** controls (offset and zoom, in the lyrics sidebar and the
fullscreen player, and the editor's speed stepper) are fixed 28 px squares with
the glyph centred by flex rather than by its own font metrics — the `+` sat low
and heavy beside its `−` because a padded text glyph lands wherever its metrics
put it, and both sides now share the box size of the buttons beside them.

## A music video YouTube cannot serve goes to the network

A Digital Media music-video release is still fetched from YouTube track by
track, but a track YouTube has no upload for — or whose upload never arrived —
is looked for on **Soulseek** before it is reported missing: one query for the
track, video containers only (`mlo.paths.is_video_file`, the library's own
vocabulary), the track's artist and title in the file's name, the length within
the tolerance the album matcher already allows, and the peer's own facts
(speed, queue) ordering the candidates. What lands goes to the same staging
folder and through the same naming and import as the YouTube half. A track that
neither source can serve keeps its line naming both, and a release both ends
come back empty for still ends on the dead end it always did.

The same fallback sits behind **Download video** on an album page. A Soulseek
transfer takes minutes, so the route does not wait: the search runs for a few
seconds and, when a peer has the file, the transfer is queued as the app's own
download — the Downloads page shows it from then on — and the toast says so.
The answer now reports what each source said ("…; Soulseek is not logged in —
set your username and password in Settings → Soulseek") instead of reporting
the network as empty when slskd is signed out or not running.

## The earliest pressing wins, and the queue follows

The edition walk preferred the edition **closest to** the release group's first
date. It now prefers the **earliest** one, keeps the configured medium order and
official-before-unofficial ahead of it, and an edition whose MusicBrainz title
carries a disambiguation comment — "(BMG club edition)", "(CB 811)" — loses a
tie to a clean one, because a comment is MusicBrainz saying this edition needs
distinguishing. A release already waiting in the queue is re-ranked when its
walk starts instead of replaying the order it was added with.

## 3.23.3's own work, in short

Exports: one toggle per file family with `.accurip` its own, cancel at a file
boundary that keeps what was written, and a destination claimed like the sources
so two exports cannot fight over one drive. One labelled progress row per
producer, ended by that producer's own `progress_end`. The organizer no longer
writes `description (2).txt`; the layout scan reports numbered copies beside
their canonical name and offers to trash them. The grading strip folds to three
findings behind a *Read more*, and a passing library says **All checks pass**
without a number. The equalizer's import accepts Equalizer APO's own alias
spellings, reports what it cannot carry instead of guessing, clamps to the same
bounds as the export, and gives a profile that parsed with an error ONE verdict
on all three surfaces. See `release-notes-3.23.3.md`.
