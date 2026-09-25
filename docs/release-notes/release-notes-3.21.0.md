# la musica 3.21.0 — the phone player, and the things that lied about themselves

Five separate reports from the phone, and one from the desktop, all came down to
the same shape: something on screen said a thing was true when the thing itself
knew better. The bar drew Pause over silence. The lyrics pane fought the page for
a scrollbar. A download called `la-musica-export-5-tracks.zip` was 2.6 KB of
`index.html`. The album chip said `DR` where every other screen said `ADR`. This
release makes each surface answer to the thing it is describing.

## "Audio stops, but the song is still playing"

Exactly the report. iOS suspends a backgrounded webview: the sound stops, the
element pauses, and **nothing told the app** — `playing` was written only by the
app's own buttons, so the bar kept drawing Pause and the fullscreen player kept
claiming a track that was not playing (and the lock screen agreed with them).

The media element's own `play`/`pause` events now settle it, guarded by identity
so that a handover — which pauses the outgoing element as the queue moves on —
cannot stop the incoming track. Coming back from a lock screen, a call or another
app is the other half: a frozen webview receives no events while it is frozen, so
`visibilitychange` and `pageshow` re-check the element before the bar is allowed
to claim the track is playing. The honest state is now the only state.

## On a phone, the lyrics get the screen

The fullscreen player was a two-column layout squeezed into a column: the page
scrolled behind a pane that scrolled itself, the cover took the height the lyrics
wanted, and a literal `100vh` cap clipped the rest. Below `lg` it is now a
compact header — cover as a thumbnail, title, transport, seek — with the lyrics
pane owning **the one** scrolling surface and the height left over. In a viewport
too short for the header (a phone in landscape) the body scrolls instead, so
nothing is clipped away at any size.

## The favourite, where a thumb looks for it

Issue #47 asked for the control Apple Music's own playback widget has, bottom-left.
On a phone the favourite now sits exactly there: bottom-left of the player, above
the bottom bar, at every width below `lg`; above `lg` it stays in the transport
row, never both at once. It is a **heart**, not a star, and that is deliberate: a
*star* in this app is a rating (1–5, in `lib/ratings`), and a single star beside
it would read as "one star" rather than "favourite". It writes the app's own
favourite — the same state the library's heart and the smart playlists read.

## A download that saved the app instead of the album

`<a download href="/api/export/zip/5">` arrives at the service worker as a
**navigation** — and the navigation branch is the offline shell: it fetched the
archive, tried to store it as the app's own document under the shell's URL, and
when that store failed (a 300 MB archive against the cache's quota) the `catch`
answered the *download* with the cached document. The owner's report was the
fingerprint: a 2.6 KB "invalid .zip", which is this app's `index.html` (2,689
bytes) saved under the archive's name — and the installed app was left with a zip
where its shell should be.

Only a real page takes that branch now (`destination === "document"`, and nothing
under `/api/`), and the shell cache is versioned, so an install that was already
poisoned drops the bad entry on the next activation.
`tools/check_export_zip.cjs` measures both halves: the bytes the browser really
saves, and that no cache holds an archive as a document.

## ADR, not DR

The album card's chip said `DR` while the album page and the statistics called the
same number `ADR`. One value, one word now — `ADR`, the album's dynamic range,
which is what the tag means next to the per-track `DR` a track table may still
show.

## Who you are, in the top bar

The top bar's rightmost control is the account: it names the signed-in user, lists
every user the server reports, switches between them (the same sign-in the login
screen performs, then a reload — every cached query, the player and the event
socket are keyed on being signed in), and signs out. A server with no users says
so and points at Settings → Security rather than showing an empty list.

## What an export copies, family by family

The export's sidecar set was one boolean: everything, or nothing. It is now a
selection — the tracks, covers/artwork, `.lrc`, `.cue`, the rip's `.log`/
`.accurip`, `description.txt`, checksum lists, notes/scans, the album's own
playlists, and anything else the app cannot classify — ticked per run on the
Export page and saved as this device's default (`export_copy_files`). A family
left out is **named in the run's report** rather than dropped in silence, the
cover still travels embedded in every written file, an empty selection is refused
rather than writing an empty folder, and `export_sidecars` still resolves to the
classic set it always copied.

## A failed download that explains itself

A run whose candidates were all refused ends in one sentence, and the wish row
keeps that sentence as its `last_error`. It used to end *"see the log"* while the
reasons lived only in the job's own memory — which clearing the row discards —
and the log carried nothing: a release stuck at 0 % explained nothing at all. The
sentence now carries the attempts themselves (`peer: reason`, first three, the
rest counted), and each attempt is written to the wishes log as well, so the
pointer is true. With no reasons recorded, the sentence is byte-for-byte the one
it always was.

## iOS and Android keep playing — and where that stops

The iOS app declares `UIBackgroundModes: audio`, which is what lets playback
continue once the app leaves the foreground; the ATS web-content exemption that
lets it talk to a server on your LAN is declared beside it. The mobile workflow
now reads **both out of the built `.app`'s `Info.plist`**, so neither can be
dropped by a merge without the build failing.

Android is the honest column of the same table: a WebView keeps playing while the
process lives, and the OS may reclaim a backgrounded app. The app declares no
foreground playback service, so "keeps playing with the screen off" is not a
promise Android makes here. What the app *does* control on every platform is that
its own state is honest about the element, which is the fix above.

## The queue tells you what it is doing — and one press gets rid of a row

A row now carries the job's own state instead of a bar that explains nothing:
which query is being asked and whether slskd is still asking it (a search is a
list of queries run one after another, so "working" and "stuck" look identical
without it), which peer and folder is arriving, the phase, the job's own last log
line as its stage ("Now: Downloading 12 file(s) from peer_three…" — and gone the
moment the job settles, because a finished job's last line describes a step
nobody is running), the files the wait has really accepted on disk counted apart
from what slskd calls complete, and the candidates that were refused with the
reason each was refused — collapsible, and kept in full in the log.

**Cancel is one press, and it cancels.** It used to branch on the wish's status:
cancelling anything past "searching" removed the folder and the wish while the
download kept running — and since a job whose wish is gone still draws a row of
its own, it came back later as a failure with a *second* row to clear. One
endpoint now stops whatever owns the row, and the row says it was cancelled
rather than failing.

The "Imported … — it is in your library" notice no longer implies the pipeline
has finished, either: the album reaches the library when the *download* does,
while the scripts run on a thread of their own, so every surface that announces
the landing says "— the import pipeline is still running" until the row's own
stage says otherwise.

## Upgrading

Nothing to do. A cached shell poisoned by the download bug repairs itself on the
next load (the shell cache is versioned). `export_sidecars` keeps working exactly
as it did; the new selection is simply more specific when it is set.
