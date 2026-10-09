# la musica 5.0.0 - importing, answered; a library that keeps up

Importing is the story of this release: it stopped guessing and started asking,
and the library keeps up with what it does.

## Import

- **Import asks which release it is.** The Library's batch bar runs the pipeline
  an arrival runs, and for any selected album whose files name no MusicBrainz
  release it opens "Which release is this?" first: paste a release or
  release-group link (or the bare id), search by catalogue number, or check the
  audio with **Detect (AcoustID)**. Nothing is chosen from a fingerprint unless
  that button is pressed — the dead `import_acoustid` setting and the tooltip
  that claimed the app could fingerprint on its own are gone.
- **Import is a queue.** Several albums import at once (`import_bulk_concurrency`,
  default 5) and the rest wait their turn, each with its own row and progress.
- **A folder import is named before its chain runs.** The naming-script
  organizer now runs on the auto/bulk path — where the original one-click import
  ran it — and from the wizard's Finish, so an album is no longer graded file by
  file as a PATH mismatch because beets was switched off or matched nothing. An
  album whose tracks state no artist/album is left where it is.
- **The wizard's Finish runs the scripts you ticked**, not the saved Run-All
  order, with the same force options the other run surfaces take.
- **Changing the MusicBrainz link takes effect**: a release-group link resolves
  to its best edition through the same release-choice policy the import uses, and
  a Match step that lost its release can no longer write the previous id back
  over the Links step's choice.
- **Unfinished manual imports are remembered** and offered again as a Continue
  row, and **covers are picked by looks** rather than the first candidate.
- **Empty folders do not survive an import**: the organizer prunes the folders
  it emptied, and a bulk batch sweeps the library's shells once at the end, so
  the layout report's "Empty folders" finding is not left behind.

## Library

- **The library refreshes itself after any write.** Every in-app write publishes
  one coalesced `library_changed` frame; the tree is served
  stale-while-revalidate and its background rebuild announces the fresh rows
  when they land. This release closes the last gap: a rebuild that started
  before a write could store the pre-write rows and clear the dirty flag, so a
  long script run left the page stale until the next visit. A write — or a
  settings change / Refresh drop — that lands mid-rebuild is no longer
  swallowed.
- **Select all**, wherever Select mode shows up.
- **Ratings keep their column still** (the star readout reserves its box).

## Grading and tags

- **ReplayGain album values are written again.** rsgain's skip-existing skips a
  file that carries ANY ReplayGain tag, and skipped the whole album when every
  track already had track gain/peak — so `REPLAYGAIN_ALBUM_GAIN`/`_PEAK` were
  never written and every re-run re-skipped it permanently. The skip is a
  per-album decision now: the scan runs, and the album row is written, unless
  all four tags are already on every track.
- **An album holding part of its release fails** (`EXPECTED_TRACKS_INCOMPLETE`):
  a folder that shipped some of a release's tracks is a partial import, not a
  small album.
- **Dynamic range is measured natively.** The `mlo-audio` Rust helper (zero
  crate dependencies, built by the Dockerfile in its own stage) is preferred for
  the block math, with the numpy path as the pinned fallback; the two engines
  are held to the same integers.

## Player and lyrics

- **The fullscreen player reads; it no longer edits.** The integrated text
  editor is gone; the reader, the zoom and the lyric offset controls stay.
- **The lyrics publish pipeline is gone**, and the editor plays the song being
  edited.
- **Grade findings are ONE panel in the notification tray** — the same strip
  Home and the Library draw — instead of a row per finding. It is always
  current, stays while anything fails and clears when the library is fixed.

## Under the hood

- Dependency audit and dead-dependency cleanup; a tools folder shared with
  another host keeps both hosts' installs.
- `python dev.py` is the one-command dev bed: a scratch library, a free port
  from 8011 up, the backend under reload and the UI on vite, with a run killed
  before it could put a shared config back healed by the next run.
- The library audit's safe performance wins (decoder lanes, per-host
  politeness, fetch-skip), and the release/CI gates stay as they were.
