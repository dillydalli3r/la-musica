# la musica 3.16.0 — add to library works, scripts stay on their album, and the player listens

This release is the audit's bill, paid: an add answers at once and heals its own leftovers, scripts and imports stop queueing behind unrelated albums, and the player stops lying about gain, play presses and text contrast. Every rule is in **docs/OPTIMIZATION-GRADING-SPEC.md**.

## Add to library

- **It answers before MusicBrainz does.** The album and its wish are created from what the caller already holds — the reply comes back in well under a second, verified with a resolver that blocks as long as it likes — and the resolution runs on its own thread. The queue shows one **"Searching MusicBrainz…"** row until it lands.
- **Nothing is left standing behind it.** The framework folder is adopted onto the release's real name (one folder, the naming script's own name); a placeholder whose album has arrived anywhere else is torn down; and an **audio-less framework folder is refused as "already in your library" everywhere** — that single too-generous rule is what once turned an add into a terminal wish with nothing behind it. Rows are matched by release id, not by folder name.
- **A terminal wish is re-armed by a fresh add** (attempts and backoff reset, due now), so "Soulseek is searching for them now" is true again — and the **startup sweep repairs what already went wrong**: an "imported" wish with no audio is put back to searching, a placeholder whose album is really there is removed.
- **Recommendations without an MBID can be added** — one MusicBrainz search by name, continuing as an ordinary add on a hit, and on no hit a **name-keyed wish** so the Soulseek search still runs (with no framework folder, since its folder could never be tied back). The same works in the Flutter client.
- **A folder with no audio is never a playable album** — no play button over nothing, whatever a wish happens to claim.

## Scripts and imports stay on their album

- **No album-scoped action waits on an unrelated album** (R94). The global script lock is gone; a run claims the *paths it holds*, so two imports of different albums are genuinely in flight at once (proved with two threads inside a barrier), while two runs over the same album — or a library-wide Run All — refuse or queue with the claim's own sentence.
- **The wizard's Finish runs the import chain**, not the Run All order. Verified by rendering the step against a configured chain and reading the boxes; reverting that one line fails three checks.
- **Every script already scoped to its album**; the two that were serial for no reason now run lanes: AccurateRip albums (with the ffmpeg budget divided across them) and the layout scan's artist folders — 3.9× and 3.5× in their tests. Script 1 takes one walk and one open per track instead of two of each.
- The runner no longer walks the library on every import (the moved target is already known), and the header bar is dispatched per run instead of being swapped by whichever script started last.

## Imports overwrite what they decide

An imported file's pre-existing **lyrics, genre, ITUNESADVISORY and embedded art** no longer survive: the import clears them first (each family gated by the same switch the later step reads, so nothing is dropped that nothing will re-fill) and the writers put theirs in. **`import_keep_synced_lyrics`** (off by default) keeps a lyric only when it holds real words *and* timestamps. A family you have put under review keeps what arrived.

## The player

- **A gain that arrives late lands on the playing track** — the 1 s analysis bound stays, but the player pre-warms the current track and re-asks on a widening schedule, ramping the value in when it lands (harness: unity while pending → 0.4365 on the live element with the readout agreeing). Music videos now carry a real gain instead of a readout that claimed one. Flutter caught up: its gain refresh has a caller, and its clamp was widened from +12 dB to the player's ±24 dB.
- **Pressing play on what you are already playing restarts it** — album card, album header, artist "play all", rows and the transport share one rule (a reorder still must not restart anything). The check runs green here and reproduces the old no-op against the previous build.
- **Fullscreen text holds contrast**, metadata included ("16/44.1", album, artist) — measured from real pixels against mid-grey and light covers.
- **Lyrics: 150 % is the new 100 %**, and 100 % is the default — the fullscreen viewer's rendering is unchanged and only its label rebased, the sidebar's scale moved once.

## Smaller

- **Export** gains a **custom folder structure** (tag fields and functions, with a live server-validated preview and named refusals) and its shipped layout is now **`Album artist / Album / 1-01 Title`** — ALBUMARTIST with a fallback, disc number always written, exactly like the library's own script. A saved layout key is migrated rather than silently ignored.
- **Recommendation notes stop lying**: sources that cannot answer at a page's own level (artist-only feeds on an album) are one grey capability line, not amber errors, nothing is truncated mid-sentence, and only real failures stay amber.
- **The AI is a backup for ITUNESADVISORY, never an opinion that overrules a source.**
- **Covers lose their hairline** app-wide (the shared image component included), the cover's "…" menu is portalled so it can no longer be clipped by the sidebar, and the collapsed sidebar draws one divider instead of two.

## Verified

The suite battery, the new checks added with each change (play-press, ReplayGain player 17/17, fullscreen metadata contrast 16/16, cover menu 19, sidebar 12/12, ReplayGain tower 116, script optimizations 72), the Flutter widget tests 16/16, and live browser runs for the play restart, the menu flip, the lyric scrim and the metadata contrast.
