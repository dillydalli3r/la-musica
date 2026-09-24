# la musica 4.0.0 — the features, then the audit of them

4.0.0 is the release the issue asked for: add the missing features, **then** audit
the app around them. It adds importing playlists from streaming services,
podcasts, a real cookie story, custom accent colours, single songs pulled out of
a rip, and a video route that falls through both networks instead of guessing —
and then it goes back over the sharing, the download queue, the importing pipe,
the cover choice, the menus, the phone and the player, with every rule written
into [`docs/OPTIMIZATION-GRADING-SPEC.md`](docs/OPTIMIZATION-GRADING-SPEC.md)
(R243–R258) and pinned by a test.

The engine grew 11 suites this turn — **124 suites, all passing** — plus new
browser checks; `tools/perf_pipeline.py` is committed beside them, so the next
change to the pipeline has a before/after to answer to.

## Playlists come in from Spotify, YouTube Music, Apple Music and Deezer

Paste a playlist URL on the Playlists page and the app reads it, matches each
track against your library, and tells you what it found before it writes
anything: matched, unmatched (with the reason), and duplicates. What it does
next is configurable, and every option ships off or conservative — "off by
default" is the rule the issue asked for.

- **`playlist_import_parent_albums` (default false)** — when ON, every imported
  track whose album you do not have queues that track's **parent album** (the
  album, never the loose track) through the same add-by-name path the MusicBrainz
  browser uses: MusicBrainz match, then the wish and the framework album. A track
  whose album is already in the library queues nothing. The dialog carries a
  per-import override that opens on the configured value.
- `playlist_import_unmatched` is `skip` (default) or `wish` — the second also
  queues the tracks themselves by name. A track whose album the parent-album pass
  already queued is never queued twice.
- `playlist_import_create_empty` (default true): an import that matched nothing
  can still be a playlist, or write nothing and say so.

Each service is read the way that service actually allows, and the error names
the thing that is missing rather than "failed": Deezer and Apple Music need no
key (Apple's public page is scraped, and the code says so), Spotify uses the
client credentials you already configure for advisory lookups, and YouTube Music
goes through the app's own yt-dlp — the vendored binary, your cookie jar
included. A playlist row now remembers where it came from (`origin` /
`origin_url`), so the detail page shows it and links back. `POST
/api/playlists/import/streaming` is the route; `?dry_run=1` is the check without
the import.

## Podcasts, modelled the way MusicBrainz models them

MusicBrainz has no "Podcast" release-group type — a podcast there is a **series**
of type `Podcast`, and an episode is a release group linked to it with `part of`.
The app now reads that relation from the release group itself (one cached
request, riding along on the artist browse it already makes) and records the
identity in three tags the tagging chain writes like any other:
`PODCASTSERIES`, `PODCASTSERIESMBID`, `PODCASTEPISODE`. `Podcast` is a *derived*
type in the vocabulary — offered and compared where the app compares release
types, never confused with the release-group types MusicBrainz really has.

What you see: a **Podcasts shelf on Home**, grouped by series, showing the newest
episode of each, hidden when the library has none; a series page listing that
podcast's episodes with their numbers; a podcast preset in the Library; and an
artist page that no longer files episodes under "Other". Grading stops treating
an episode like a music CD — the checks that cannot apply to a podcast are not
charged to it, and nothing in the music rules was weakened.

## A single song out of an album finally lands in the album

Importing one track of a release used to make that track its own "album" beside
the real one. Now an import that brings exactly **one** audio file is resolved
into the album it belongs to — by the release identity, by the file's own `ALBUM`
tag, or by the album the import was asked for — and the album reads **partial**
with the missing rows, the way a full import with unticked files already did. A
genuinely standalone single is untouched and still becomes its own album.

For a CD rip the sidecars carry the truth, so the app uses them: the `.cue`
(or the `.log`'s TOC) supplies the album's real tracklist and **which row the
imported file occupies** — matched by the sheet's own file reference, not by
counting — and the `.accurip` supplies that track's verdict. Script 9 refuses to
regenerate a `.accurip` from a slice of a disc, and a CD rule that cannot apply
to a partial album says so specifically instead of failing the album for missing
a whole disc's worth of evidence.

## Music videos: the network is a preference, and both ways fall through

The route still decides which network to try **first**, from the release's own
metadata, but it is no longer a verdict:

- a **physical** video release (DVD, Blu-ray, VHS, Video CD, LaserDisc) goes to
  Soulseek first and now **falls back to YouTube per track** when the search
  comes back empty — the branch that had no YouTube in it at all;
- a **Web / digital** release goes to YouTube first and the Soulseek fallback
  really happens: an unreachable or signed-out slskd is no longer recorded as
  "no copy" (the reason now says "Soulseek is not running" / "not logged in"),
  and a Web release with YouTube switched off still gets its Soulseek attempt
  instead of hard-failing before any fetch;
- `Web` is a medium the code knows (`mlo/tagtext.MEDIA_VALUES`), and it is
  digital for routing.

`auto_import_medium_order` now ships as
`[CD, Vinyl, Cassette, Other, DVD, Blu-ray, VHS, Video CD, LaserDisc, Digital Media]`
— the physical carriers named ahead of digital, with a migration that recognises
the old shipped list and updates it without touching a list you customised. The
box-set tier fires on a release that carries video media *beside* the album's own
medium (a 3-CD anniversary box with a DVD); a release whose medium **is** the
disc — a single-disc DVD or Blu-ray — is no longer penalised as a box set. Every
file's `SOURCE` records which network actually produced it.

## The cookie logins take a `cookies.txt`, and every cookie can carry a note

One Netscape module now backs every cookie credential (YouTube's jar and
RateYourMusic's header — the parser's one implementation, not two). Paste a
`cookies.txt` from an extension like "Get cookies.txt", or drop the file on the
box, in **Settings and in the setup wizard's Keys step**: the app keeps the
cookies that credential is actually sent to, refuses a jar that holds none of
them (in the provider's own words), and warns about expired ones.

Each imported cookie can carry a **comment** you write — a label for the account,
a note about which session it is. Comments persist in `cookie_notes`, are keyed
by the cookie's identity (domain, path, name) so a re-import or a reordering
never moves one to the wrong cookie, and round-trip into the jar as `#` comment
lines, so the file stays valid for every reader (yt-dlp included).
`GET /api/cookies/{source}` lists them; a cookie's value is never returned.

## Accent colours, including your own

Fifteen presets, and a **colour picker**: the native swatch and a hex field stay
in sync, `#rgb` and `#rrggbb` both work, and an invalid entry says so instead of
pretending. The choice is per device (`localStorage["mlo.accent"]`) and every
derived value comes from one place: the accent triplet, a soft variant in the
same hue, and the ink for text drawn **on** the accent, chosen by contrast (AA at
the sizes the app uses). The visualizer and the equalizer curve repaint live
instead of caching the colour they were born with, and every preset that shipped
before renders byte-identically to what it was.

## The "…" menu now offers every script that applies

The entity menus were three hand-written sections that had drifted from the
registry: the CUE re-format, the image pass, Auto tagging, Format all, Remux
videos, Key & BPM, Release tracklist, Mood & Energy and Layout fix were not in
them at all. They are now **generated from the server** (`GET /api/script-menu`),
one entry per script that applies to what you are looking at — an album gets all
22, a track row or a playlist gets the 11 whose work unit is a file — each
running through the same `/api/run` the Optimization page uses, with a forced
twin where the script owns a single force flag. A script added to the registry
without an applicability answer **fails `tools/test_script_menu.py`**, so the menu
cannot quietly drift again.

The Checks & scripts page had its own version of the same bug in the other
direction: a script's gate was shown as off unless *every* switch was on, while
the run proceeds if *any* is (`lyrics_xlit_enabled` alone really does run script
17). The page now reads the payload's own answer through one helper
(`web/src/lib/stackDraft.ts`), and unticking one switch moves one switch.

## The fullscreen player's chrome stops disappearing into the artwork

The lyric lines and the metadata already answered to the cover through the
player's one ink table; the controls around them did not. The **seek bar** and
the **volume bar** drew a fixed zinc track (`#3a3a42`) and a white accent thumb,
which measured **1.70:1** on a near-black cover, **1.01:1** — the same tone as
the field, i.e. invisible — on the mid-grey one, and **1.64:1** for the thumb on
a white one. They now take four values from that same table (`seekTrack`,
`seekFill`, `seekThumb`, `seekRing`, written onto the player as `--seek-*`), and
the bar has a real **played run** at last: the native track is one flat colour,
so the whole bar read as unplayed. Measured per cover polarity, the unplayed run
is 3.4–4.1:1, the played run 7.9–20:1, the two runs 2.3–4.9:1 apart, and the
thumb 7.9–20:1. The player bar and the equalizer keep the sliders they had.

The **top-right icon row** was three brightnesses in one bar: the plain buttons
drew full ink, the fullscreen button an accent, the settings button an open-state
white, and the two toggles a dimmed tone when disengaged. Every icon now draws
its two states from one pair — engaged is the accent ink (the light table's full
ink on a bright cover, where the accent *is* white and would vanish), disengaged
is one dimmed tone for the whole row, and either takes full ink on hover. The
queue readout beside them stays a readout in the row's own tone.

The two **lyric chips** at the bottom right (`− 100 % +` and `− 0.0s +`) were one
box each and two different interiors: the offset printed flush into its value box
while the size chip inset its number behind an absolutely-positioned `%`, so the
same `−`/`+` pair stood at different distances from the number it steps (17 px
against 13 px, measured from the pixels). Both now share one geometry — the
stepper, the 40 px value box and the 9 px unit are single shared class strings
(`LYRIC_STEP_BTN` / `LYRIC_VALUE_BOX` / `LYRIC_VALUE_UNIT`) — and they rest on the
table's full ink instead of the muted tone at 60 %, which was 2.9:1 before the
glyph dim on a white cover and is 6.4:1 now (11.6:1 on a dark one).
`tools/check_np_metadata_contrast.cjs` owns all of it: it now measures both bars,
both icon states, and both chips per cover polarity, from the pixels.

## The lyrics player mode comes back on a phone

A phone opened the fullscreen player on a track that **has** lyrics and showed
none — artwork, title, transport, visualizer, then empty space — and the offset
and zoom chips the lyrics carry were not on screen anywhere. One button had two
meanings: at `md` and up the lyrics control wrote the reader's saved pane pick
(`mlo.np.lyrics`, default on), while below `md` the **same** button flipped a
compact mode and wrote nothing. So a phone could only show lyrics by also
unfolding the whole block, the saved pick was ignored below `md`, and the pane —
with the two chips at its bottom edge — never mounted there at all.

There is now **one piece of state and one derivation** at every width: the button
always writes `showLyrics`, `paneOpen` is *the track has lyrics and the reader
wants them* in both layouts, and the phone's compact header is a function of the
width alone instead of a second owner of the pane. On a phone the pane is a real
reading surface under that header — full width, scrolling inside its own box,
inside `safe-np-body` so the notch and home-indicator insets still apply — with
`− 100 % +` and `− 0.0s +` on its bottom edge, reading and writing the same state
the wide layout does. Turning the lyrics off leaves the plain compact block
exactly as it was, a track with no lyrics draws no pane and no control that could
not do anything, and the desktop layout is untouched.
`tools/check_fullscreen_player.cjs` gains the narrow-width cases and measures
them at 390×844 and at 566×1040, the owner's own window: the lines on screen,
the pane scrolling without pushing the transport off the viewport, both chips
changing what is displayed, the OFF press surviving a reload and being the same
state a widened window reads, and the wide layout's right-hand column at
1440×900 and 1920×1080.

*One consequence worth naming*: the phone's old "unfold the whole block" mode was
that same button, so it is gone — a phone shows the compact block, and the lyrics
control opens the reading pane rather than changing the block. If you want the
big-artwork phone view back as its own control, that is a separate ask.

## The cursor goes away, and a tall flyout scrolls

- In the fullscreen player the mouse arrow now hides after **3 s** of stillness
  and comes straight back on a move — fine pointers only (a touch device has
  nothing to hide and is never left in a hidden state), never while a button is
  held (a scrub keeps its arrow), never with a menu, popover or dialog open, and
  the pane's own controls opt out so the arrow cannot vanish from under a click.
  The window was measured, not guessed: the aim-and-reach gap between artwork and
  transport is 1–2 s.
- **Every flyout** is clamped to the window and scrolls: the Force menu on the
  Optimization page (the owner's report — it ran past the bottom of the screen
  with no way to reach the last flags) and every other Popover/OverflowMenu list,
  with `overscroll-contain` so the page underneath does not move. A panel that
  cannot fit its anchor is promoted to the measured form before paint, and the
  horizontal overflow a phone showed (456 px of panel in a 390 px window) is gone.
- **Dialogs have a phone form**: a bottom sheet with the safe-area insets, a
  scrollable body, a reachable close/confirm row, and a `visualViewport` hook so
  the keyboard cannot hide the field you are typing in.
- The lyrics sidebar takes the same insets and Escape, the queue drawer takes a
  press-outside shield, and the sort menus on Downloads and Trash are the shared
  Popover instead of hand-rolled lists.

## The iOS star marks a favourite

iOS draws that star in the Now Playing module only for an app that registers the
media remote's **like** command — the web `MediaSession` API has no such action,
so nothing in the webview could ever have handled it. The iOS shell now registers
`MPRemoteCommandCenter.likeCommand` (and leaves `dislikeCommand` off, because the
app has no dislike), and the press runs the app's **one** like writer: the shell
emits an event, the web bridge toggles the current track's favourite through
`useFav`/`api.likeToggle` — the same path as every heart in the app, with the same
optimistic update and invalidation — and the app pushes the state back so the star
draws filled when the track is favourited and hollow when it is not. Outside the
Tauri shell the bridge is inert and every failure is swallowed: a favourite must
never break playback.

The three hearts (row/card, player bar, fullscreen player) are now one component
with one behaviour, so they cannot drift apart again. The native registration is
iOS-only and off every other target; it is compiled by `mobile.yml` in CI, and the
press itself needs a real device to feel — that is stated plainly in
`desktop/README.md` rather than claimed here.

## ACOUSTID_ID is written, and repairable

The grader's "Missing ACOUSTID_ID (run Fix AcoustID pairs)" was unrepairable for
a file whose **name already stated the recording**, and that was the bug: the
repair asked AcoustID about a recording the file itself names, so with no API key
or no score ≥ 0.75 above the noise it completed nothing, while the file that
needed no lookup at all was exactly the one it refused.

Script 21 now **completes or creates** the pair, and it identifies the recording
from the file itself, in order: an existing `ACOUSTID_ID`, else
`MUSICBRAINZ_TRACKID`, else the one bracketed UUID in the file name that none of
the file's other id tags claims — the app's own naming script's recording id. The
fingerprint comes from local `fpcalc`; the AcoustID service is consulted only for
a half pair that names no recording. It runs for a track, an album or any
selection — `POST /api/run {ids:[21], targets:[…]}` — from the entity menus, the
Optimization page, the terminal menu or the CLI, and every import path
(auto-import, add-to-library, manual import) writes the same tags.

## The pipeline got faster, measured

- **One HTTP client for the whole process** (`server/httpclient.py`) instead of a
  fresh one per provider call — a fresh `httpx.Client` means a fresh TLS context,
  and script 8 alone made 120 requests: **106.52 s / 120 clients → 73.13 s / 1
  client (−31 %)** on the offline harness, requests unchanged, and the library's
  own **manifest sha1 identical** before and after. A local-server bench puts one
  request at 3.95 ms against 241.9 ms. The client carries a discarding cookie jar,
  so a session one source obtained can never ride along on another's request,
  while a caller's own per-request cookie (RYM's pasted jar) still goes out.
- The **DR pass** no longer spawns an `ffprobe` per track: it reads the channel
  count off the file handle it already holds (20 tracks: 3.39 s → 2.37 s, 20 → 0
  spawns, identical `DR` tags), and a **converted file's duration** comes from the
  container the pipeline just wrote instead of a second probe (one spawn per file
  instead of two).
- `tools/perf_pipeline.py` is committed with the four JSON reports behind those
  numbers (`perf_before_local.json`, `perf_after_local.json`,
  `perf_http_before.json`, `perf_http_after.json`), so the next optimisation has
  to beat a measurement, and two new suites pin the wins (spawn counts and client
  constructions, not timings).

## Soulseek: the queue tells the truth

- A **page download is imported only once its whole intended file set has
  arrived**, at the size the press asked for — the old pass imported whatever was
  on disk, so an album was chained and graded from a fragment while the rest of
  the files landed afterwards, orphaned.
- Those intents **survive a restart** (they are written beside the app state), so
  a download queued before a restart is still known to be yours.
- Pending-folder readiness matches on the **peer and the remote path**, not the
  folder's leaf name, so an unrelated peer's unfinished transfer can no longer
  make your complete album look busy (or the reverse).
- **A re-press merges** with the download already in flight instead of queueing
  the same album a second time (slskd does not dedupe: the old path fetched it
  twice into two batches).
- The **share audit** no longer says "ok" and "other users can download them"
  while the listen port is dead — its verdict agrees with the port check.
- An import that ran **no chain** (a review-only setup) now asks slskd to
  re-index, so a newly imported album is actually offered in the share.

Each of the six is pinned by a case that was shown to fail with the fix reverted.

## The cover an import lands is the cover the dialog would pick

The Cover Search dialog and the unattended import were not asking the same
question, and the difference changed the answer:

- the dialog asked the finder for **40** candidates while the import asked for
  **12**, and since the size tier outranks the source tier, truncating the stream
  could hand the import the *second* best image — with the dialog's own best pick
  sitting in the rows it never saw;
- the release **group** was derived from a release-only tag in the import path
  and not in the dialog, so for such an album the two ranked different identities.

One number (`mlo.cover_choice.SEARCH_LIMIT = 40`) and one derivation (inside
`integrations.cover_search`, where both paths reach it) later, the two land the
same URL with the same reason — and `tools/test_cover_parity.py` drives both
entry points over eleven candidate-set cases and fails if either stops consulting
the shared policy (it was shown to fail on three deliberate divergences,
including the 12-candidate truncation that caused this).

## Every edition row can be added on its own

The release-group page's RELEASES table lists seven editions of the same album;
the page-level button added the one the ranking picked, and a specific pressing —
the 2023 2×CD, a country's CD — had no way in. Each row now carries its own
**Add to library**, adding *that* release (`kind:"release"`, the row's own MBID,
per-row spinner, the page button disabled while a row is in flight), reachable at
phone widths where the table hides columns.

## The rest of the audit

- **Menus**: every sidebar entry was walked in the rail and in the phone drawer
  and pressed (navigation, active state, drawer close), the nav-order check now
  presses entries instead of reading them, and the checks that were done by hand
  are pinned — `tools/check_responsive.cjs` runs a dialog sweep over 16 routes ×
  3 viewports (196/196), plus the sidebar (12/12) and menu groups (phone drawer,
  sheet, page menu, flyout, lyrics).
- **The 21 rules in the spec** are the contract for all of the above: R243–R258
  are new this release, R84 (the medium order and what a box set is) and R163
  (whose identity a cover is judged against) were corrected, and the README's
  endpoint table, config keys and feature list follow it.

## The audit's own findings, fixed

The last pass was a read-only sweep of the surfaces the feature work did not
touch, looking for the things a geometry check cannot see: states that lie,
actions that cannot be undone, and elements that never fit the screen. It found
nine, and the ones that matter are fixed:

- **The offline-downloads page accused the user's cache.** Its "your cache is
  orphaned" verdict and the **Clear all** button beside it were derived from a
  library query with no loading and no error branch — so on a cold load, and
  permanently if `/api/library` failed, the page told the user their downloads
  were stale and offered to delete them. There are now three states: loading,
  failed (which says the library could not be read), and the real verdict, which
  is only ever computed from a library that actually loaded. `Clear all` is
  offered when there is something to clear, not when nothing is known.
- **One click renamed the whole library.** The Optimization page's *Apply fixes*
  runs the layout script over everything (rename, move, and trash what the rules
  say) and asked nothing. It now confirms first, and the confirmation names what
  it will do; only the confirm button reaches the endpoint. A partial failure
  from **Run All** is reported in the app rather than "see console".
- **The playlist page's hero never folded on a phone** — a fixed 224 px mosaic
  beside a fixed row, unlike the album and artist heroes it sits between, so the
  page overflowed at 390 px. It now folds the same way they do. Favorites'
  playlist rows open the playlist they name instead of the playlists index, and
  a failed fetch reads as failed rather than "you have none".
- **The library tables stop clipping their own columns**: a header label and the
  widest value a column actually renders both fit now. `tools/
  check_library_tables.cjs` owns the rule (it is the check that caught `ADR`
  wanting 53 px in a 48 px column, and a value wanting 108 px in a 40 px one),
  and it is run against a narrow and a wide library.

## The Import tab takes archives, folders and single files

Drop a `.zip`, `.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`, `.tar.xz`/`.txz`,
`.7z` or `.rar` on the Import tab (or pick it) and the app unpacks it into a
staging folder and then runs the *ordinary* album detection over what came out:
a rip in a zip and the same rip as a folder land as the same album folder, with
the same partial marking and the same `.cue`/`.log`/`.accurip` handling.

Unpacking a user's archive is untrusted input, so the rules are the ones this
repo already holds itself to, in one place (`mlo/archives.py`, which the tool
installer now shares): a member with an absolute path, a `..` segment, a drive
or UNC prefix, a symlink, a hardlink, a device or a fifo is **refused before
anything is written**, and the refusal names the member. `.7z` and `.rar` say
they need 7-Zip when it is not installed, a format nothing can read says so, and
a nested archive is deliberately not unpacked (the inner file stays a file).
`tools/test_import_archives.py` pins 24 refusals — and that a refusal leaves the
destination empty.

The picker and the drop path take **one file, several files, a nested folder, an
archive, or a mix of them in one gesture**; in the desktop shell an OS drop that
yields a path resolves through the same server-side scan, and a phone (which
cannot read the server's disk) says so instead of doing nothing. One audio file
picked on its own lands by the single-song rule below. The wizard shows what it
took in — albums, tracks, and anything it refused — before you commit.

## The library says which kind the lyrics are

"Has lyrics" was never the whole truth: the grader distinguishes lyrics with
timestamps from lyrics without, and the app's own `lyrics_allow_plain` (shipped
**off**) is the setting that says which is acceptable. Every surface that shows
lyrics now says which kind it is — **Synced** or **Plain** — from the file's own
stored truth: a `.lrc` with timestamps or embedded lyrics with them is Synced, a
timed `.lrc` beside plain embedded lyrics is Synced, plain alone is Plain, and a
track with nothing carries no kind at all (`lyrics_kind` in the payload,
`GET /api/album/scan-tracks`, and a query field, all deriving presence from the
kind so the two can never disagree).

With `lyrics_allow_plain` off, a **plain** lyric renders as the failing state it
is — the red ✗ with the reason naming the setting — on the track page, the album
tracklist, the lyrics manager and the import wizard's Lyrics step; with it on,
plain is simply "Plain", and a setting that cannot be read never claims a
failure. The kind is measured from the stored file, not from what a fetch once
returned.

## A digital release settles itself

The three things the grader reported after a manual digital import — no
`SOURCE`, lyrics "not optimally formatted", no album description — are now the
import's own business:

- **SOURCE** is written from the release's own store links (the MusicBrainz
  `purchase for download` / `download for free` / `streaming` relations —
  Bandcamp, Qobuz, Deezer, Spotify, Apple Music, Amazon, Tidal, 7digital, Juno,
  Beatport, HDtracks, ProStudioMasters, SoundCloud, YouTube, archive.org, OTOTOY,
  mora, e-onkyo, Presto, Boomkat, Napster, Traxsource, Bleep) or from the
  provider the files came from. When nothing states a value it is **asked** —
  once, in the wizard's Match step (`POST /api/import/source`, defaulting to
  `digital_media_source_value`, shipped `Digital`), or through the unattended
  import's own prompt mechanism. It is written fill-only, only for a Digital
  Media release, and only where tag writes are allowed.
- **The lyrics format** is settled by running the Lyrics formatter (script 1)
  as part of the import, so "not optimally formatted" cannot survive one. A
  lyric that arrives untimed (with `lyrics_allow_plain` off) or that the app's
  own grade still rejects afterwards is **removed together with its `.lrc`
  sidecar and its derived translations, and the count is reported** — but only
  when the chain's fetch step will replace it: never with script 13 out of the
  chain, never for a family the user kept, never with `lyrics_allow_plain` on.
- **The description** is fetched by the import's own metadata step (the same
  machinery the album page uses) and the import reports what happened: fetched,
  candidates staged for review, or not found — with the pointer to the album
  page. A step that cannot run at all says that, rather than implying success.

The wizard's Finish step runs the same settle pass every other import path does,
so a manual import and an unattended one end in the same state.

## AcoustID, in full — including submission

The app has always *read* AcoustID (fingerprint → release matching, `ACOUSTID_ID`
+ `ACOUSTID_FINGERPRINT` pairing, script 21 completing a missing pair). It now
also **submits**, as the service actually requires, and the audit against
AcoustID's own API found four things the old client got wrong:

- **a fingerprint with no MusicBrainz recording id was sent anyway.** AcoustID's
  rule is explicit — a submission with no metadata is not useful — so it is now
  refused with `NO_RECORDING_ID`. The recording id comes from `ACOUSTID_ID`,
  then `MUSICBRAINZ_TRACKID`, then the one unclaimed bracketed UUID in the file
  name; the only exception is the credential probe that tests a user key.
- **a single track published its whole album**, because the submission path
  expanded every file to its folder. What you point at is what is submitted.
- **there was no duplicate handling at all** — a pair the wizard had just
  accepted *from* the service was submitted straight back. The client now asks
  `v2/lookup` at any score (not `acoustid_min_score`) and keeps a bounded local
  ledger of accepted *and* pending pairs (`<music>/.mlo/data/acoustid_submissions.json`,
  because a submission is asynchronous — "pending" is a duplicate too).
- **a submission demanded an `ACOUSTID_FINGERPRINT` tag**, so the CD rip whose
  recording its file name already stated could not be submitted at all. The
  fingerprint is a property of the audio: it is taken locally with the bundled
  `fpcalc` when the file carries none.

Submissions go out in batches of at most 100 (the service's limit), and the
answer is reported **per file** — accepted, already known, or rejected with the
service's own words — from the wizard's AcoustID step, an entity menu, or
`POST /api/import/acoustid/submit`, and as **script 22 (Submit fingerprints
(AcoustID))**. Script 22 is deliberately **opt-in**: it publishes to a public
database, so it is never in the shipped Run All order or the default chain
(`server.script_runners.OPT_IN_SCRIPTS`) — upgrading an install publishes
nothing by itself, while an order a user saved keeps whatever they put in it.

## Verification

- **124/124 Python suites pass** (11 new this turn: `test_streaming_playlists`,
  `test_podcast`, `test_cookie_import`, `test_single_song_import`,
  `test_acoustid_integrity`, `test_script_menu`, `test_soulseek_page_downloads`,
  `test_http_client`, `test_cover_parity`, `test_lyrics_kind`,
  `test_import_archives`, plus new cases across the existing owners).
- TypeScript clean, `npm run build` green, `node tools/test_i18n.cjs` in parity
  across all six locales, and the payload-driven checks
  (`check_accent`, `check_script_menu`, `check_stack_gates`, `check_queue_view`,
  and the rest) pass.
- Browser checks were run against scratch servers on ports 8011+ with scratch
  music folders, never the live instance on 8000.
- The desktop, Android and iOS bundles and the container image are built by the
  release workflow from the tagged commit; the iOS like-command is compiled there,
  and the one thing no check here can do is press the star on a real iPhone.
