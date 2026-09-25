# la musica 4.0.1 — the lyrics mark, in its place

4.0.1 is the first pass over 4.0.0 after using it, and it is mostly one change
you asked for within an hour of the release.

## Synced / Plain only where a track's own lyrics are

The marker that says which kind a track's lyrics are shipped on *every* surface
that shows lyrics, and on an album's tracklist that made the Synced/Plain chip
sit in a row beside the advisory mark and the issue count — noise on a line the
eye is scanning for other things.

It now appears only where the surface is about **one track's own lyrics**:

- the import wizard's **Lyrics** step (with the counts and the plain-lyrics
  question it asks),
- the **track page** (its header), the **readout**'s Lyrics row, the **lyrics
  pane** and the **lyrics manager**.

An album's tracklist carries **no kind at all** now, and neither does the album
readout's Lyrics row (it says `2 of 2 tracks` again). Nothing about the rule
changed where the mark remains: synced vs plain comes from the file's own stored
truth, and a plain lyric is the failing state (red ✗, reason naming the setting)
exactly while `lyrics_allow_plain` is off — neutral while it is on, and no mark
at all when there are no lyrics.

`tools/check_lyrics_kind.mjs` was rewritten for that contract and pins both
directions: nothing on the album tracklist or the readout (with a spliced-in
chip proving the detection is not blind), and the mark present on each
single-track surface for synced, plain-with-the-setting-off,
plain-with-the-setting-on and no lyrics.

## Two test assumptions the release's own CI caught

Neither was the app, both were mine, and both are fixed in the engine's own
suites (they run on Linux in CI and on Windows here):

- the archive suite compared a *repr'd* member name (`'C:\Windows\evil'`, where a
  backslash arrives doubled) against a normalised one, so a crafted
  drive/UNC member failed the "the refusal names the member" assertion on Linux
  while the refusal itself was correct on both platforms;
- the single-song suite asserted the AccurateRip script's "partial album"
  sentence, which a host without CUETools never reaches — it stops at its own
  precondition and says so. The assertion accepts that report, and the guard
  stays proven by the file: the rip's `.accurip` is byte-identical and nothing
  is written for the album.

`check_menus.cjs` (and `check_responsive.cjs`) also stopped sampling panels
mid-animation: the nav's active entry lands through a background-colour
transition, and a stalled frame read as "not current" on a menu that was
correct. It settles before measuring now, and asserts the settled paint equals
the entry's own accent — `check_menus` passes repeatedly, `check_responsive`
330/330.

Everything else is 4.0.0, unchanged — see `release-notes-4.0.0.md`.
