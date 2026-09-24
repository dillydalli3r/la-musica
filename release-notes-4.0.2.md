# la musica 4.0.2 — the phone, told the truth

4.0.2 is the pass over 4.0.1 that two owner reports asked for — one of them the
whole reason the iPhone app went quiet and its star never appeared — plus the
finish on a batch of smaller things that had been landing behind them.

## The iPhone keeps playing, and the star appears

Two reports, one bug, and the bug was the audio session. iOS plays a webview's
audio through whatever `AVAudioSession` **category** the app has configured, and
this app had never configured one. The default (`soloAmbient`) is incidental-UI
sound: iOS mutes it the moment the app stops being the frontmost app, and mutes
it with the ringer switch. That is "audio is muted when app is unfocused",
verbatim.

The same session is why the star was missing. The Now Playing module — the lock
screen, Control Center, the widget you were looking at — draws its card only for
an app whose session is a *playback* session, and the star this app registers
(`MPRemoteCommandCenter.likeCommand`) lives on that card. No category, no card,
no star.

`UIBackgroundModes: [audio]` has been in the plist from the beginning, but that
key is the *permission* to play in the background; the category is what makes it
true. `desktop/src-tauri/src/ios_audio.rs` (iOS only, like the star's own
module) puts the session in `AVAudioSessionCategoryPlayback` — the framework's
own exported constant, the default mode, no options, since this app mixes with
nothing — and activates it at setup. Music now survives the app leaving the
foreground, and the star is there to press; it toggles the same favourite every
heart in the app does. Spec rule R265, beside R251; `desktop/README.md` carries
the wiring.

## The SideStore source imports

The source this repo publishes for SideStore/AltStore was missing the version
entry's `date`, which AltStore's decoder requires — so the phone refused the
source with

```
Decoding failed: Key 'date' not found. No value associated with key CodingKeys
```

and behind it sat a second refusal: the app's `category` was `"music"`, which is
not one of the eight values AltStore's closed enum accepts. Both assets (v4.0.0
and v4.0.1) were regenerated from their own IPAs and re-uploaded, and the
generator now stamps the release date, states `minOSVersion` 14.0 (the app's own
floor) and names a valid category. `tools/test_sidestore_source.py` pins every
key the decoder requires, so the published file cannot become one no phone will
read again.

## A 69th check: the FLAC header's own MD5

A lossless file carries a checksum of its own audio in the header — STREAMINFO's
MD5, written when the file was encoded — and nothing in the app had ever
compared it, so a file damaged *after* ripping graded clean. **FLAC stream MD5
(STREAMINFO)** is now a grading check (on in the factory defaults, like the
rest): the stored checksum is verified against the decoded audio, the same
verification runs in the audit and in the AccurateRip handling, and the check
sits in the Grading page's Integrity group beside the other tag checks
(`mlo/flac.py`, `tools/test_flac_md5.py`).

## The Storage card adds up

The Home card's three figures now include **Total (App + Library)**, the sum of
the two it already showed. The API's byte counts are integers end to end, which
is what makes the third one add up rather than round.

## The details menu got its missing entries

- **Run all N scripts** — the whole applicable chain, in the stack's own order,
  as one entry at the head of the script sections, with a confirmation that
  lists exactly the scripts it will run (the count in the label comes from that
  same list). Every script's **force** flag is reachable in the menu too.
- **Open track page** — on a listed track's own "…", so a row no longer has to
  be found inside its album first.
- **Download music video** — moved off the album row (the film flyout is gone)
  into the track's "…" menu, which asks for exactly what the album's release-wide
  film button asks for, through one shared implementation
  (`web/src/lib/videoDownload.ts`). `tools/check_menus.cjs --only=trackmenu`
  drives all of it in a browser: no row carries the film-download button, the
  row's "…" offers both entries, "Open track page" lands on that track's page,
  the right track's artist/title/length reach the server, and a menu that is NOT
  on one track offers neither entry.
- The **lyrics maker's** stamped timestamps show the configured precision from
  the first stamp on, instead of a rounded second until a save.

## Lyrics: found under their other names, and honest about removals

- The lyrics search falls back to a track's **alias names** (the artist/album/
  title's other names from MusicBrainz — 宇多田ヒカル for Hikaru Utada) when the
  first pass finds nothing. On by default; the Lyrics tab's one translated row
  says what it does.
- A settled import now says **why** lyrics were removed, per file: timed-but-not-
  in-the-form-this-install-asks-for is named as such instead of being folded into
  the un-timed count, and the message no longer words the same removal two
  different ways.
- Arrived lyrics count as correct when they are **synced or canonical** (a plain
  fetch is the fallback, and a plain file still fails while
  `lyrics_allow_plain` is off).
- The grader's own lyrics flags now come from the same predicate the kind does
  (`has_lyrics_text`), so a metadata-only tag can never be counted as lyrics by
  one and not the other.

Everything else is 4.0.1, unchanged — see `release-notes-4.0.1.md`.
