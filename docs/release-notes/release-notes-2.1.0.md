# la musica 2.1.0 — the app no longer downloads albums; your rip logs now name the pressing

Two decisions of the owner's (2026-10-17), in one release:

> *"I want to remove album download / soulseek from the app entirely."*
>
> *"In MANUAL / auto-importing (with user uploaded files) it should support
> .log TOC lookups for matching to musicbrainz releases."*

## Album download and Soulseek are gone

The whole acquisition pipeline is removed, not hidden: no slskd daemon (start,
stop, autostart, port mapping, sharing, share audit), no search, no transfers,
no uploads or private messages, no wishlist, no artist watch, no acquisition
queue, no "download" button on a release, and no downloads staging area of its
own. Deleted with it: `server/soulseek*.py`, `server/wishes*.py`,
`server/artist_watch*.py`, `server/api_queue.py`, `server/api_watch.py`,
`mlo/portmap.py`, their routes, the Soulseek / Downloads / Watched-artists
pages, the sidebar dot, the `.github/workflows/share-reachable.yml` share audit
and every config key that only served them (`soulseek_*`, `wishes_*`,
`artist_watch_*`, `notify_soulseek_*`, `notify_wish_found`,
`notify_download_done`, `auto_acquisition_enabled`). A config file that still
holds them keeps them harmlessly; no screen reads them and nothing migrates.

**Everything you drive is untouched.** Drag-drop, the nine-step import wizard,
AcoustID fingerprinting, the MusicBrainz search and link, the script chain,
grading, the audit, the rip-log report and its checksum/CRC/AccurateRip
readers, the Trash cap, the YouTube music-video download, and "Add to library"
for a release you will supply audio for (the framework album it writes no
longer carries a search).

## A rip log's TOC now names its pressing

A CD rip's `.log` states the disc's whole track table — and that table *is* a
MusicBrainz Disc ID. The importer reads it and looks the disc up:

- `mlo.discs.parse_log_toc` reads EAC's and XLD's "TOC of the extracted CD"
  (start and length per track, dot or colon as the frame separator) and
  whipper's per-track lengths (offsets are the running sum). Offsets are
  absolute (`start + 150` frames: the two seconds of lead-in are part of every
  Disc ID), and a table with a gap in its numbering is refused rather than
  guessed at.
- `mlo.discs.musicbrainz_disc_id` hashes it exactly as libdiscid does — SHA-1
  over the first/last track numbers, the lead-out and 99 track offsets,
  upper-case hex, base64 in MusicBrainz's own alphabet (`.`, `_`, `-`).
- `GET /api/import/log-match?path=<folder or .log>` answers the disc id each
  log states and the releases MusicBrainz holds for it: the release, its
  artist and date, the medium that carries this disc (position, format, track
  count) and the tracklist.
- The wizard's **Links** step carries **Match from rip log** beside *Match
  from fingerprint*: pressed, never automatic — several pressings legitimately
  share one disc id, so the field stays yours to confirm (and the toast says
  how many others share it). A folder with no log, a log with no TOC table,
  and a disc id MusicBrainz does not know each answer with the reason.

## Verification

- `tools/test_cd_audit.py` pins the algorithm against **both worked examples
  on MusicBrainz's own Disc ID Calculation page** (`49HHV7Eb8UKF3aQiNmu1GR8vKTY-`,
  `BPnh1KU.hea1C.KMYWLGZkHJr0w-`), the EAC and whipper spellings of one table
  giving the same id, the gap refusal and the no-table answer.
- Verified against a real library log: the TOC of *Nevermind*'s `CD-1.log`
  computes `eSYc6AlHqIoJuRcf47DZ9RQbxfQ-`, and MusicBrainz returns eight
  Nevermind releases for that disc — the album the library already holds among
  them (`a146429a-cedc-3ab0-9e41-1aaf5f6cdc2d`), same artist, date, country and
  twelve tracks, fetched from the live `/ws/2/discid/` endpoint.

## The fullscreen player's scrollbar (the release 2.0.4 was going to be)

The owner's report — *"the scroll bar still shows up in the bottom left of the
fullscreen player"* — was the cover column: a box that scrolls on one axis has
`overflow-x: auto` **computed** for it, and the artwork's `-inset-6` halo
overhung the 419 px column (measured `scrollWidth` 451 at 1061×1024), so the
column painted a bar along its own bottom-left edge. The column and the row it
stands in now declare `overflow-x-clip` beside their `overflow-y-auto`
(`clip`, not `hidden`: a hidden box is still a scroll container). Nothing
changes at the reader's own position — with animations frozen the cover and
halo crop is pixel-identical to the tree before it — and `check_fullscreen_player.cjs`
grew a measurement for it: no scroll container in the player may be a
horizontally scrollable box whose content is wider than it and whose
`scrollbar-width` is not `none`, at 1568×817, 1440×900, 1920×1080,
**1061×1024** (the owner's window, where the old tree reports 26 px of
overflow) and 1440×620 — **138/138**.

## The contract

`docs/OPTIMIZATION-GRADING-SPEC.md` gains **§7.84** with **R441** (no download
path exists; what remains) and **R442** (a rip log's TOC is a Disc ID and how
the wizard uses it), beside R440 (a box that scrolls on one axis never paints a
bar on the other) in §7.83.

Suites deleted with the feature they covered (`test_soulseek_candidates`,
`_api`, `_sharing`, `_port`, `_ownership`, `_page_downloads`, `test_portmap`,
`test_wishes_pipeline`, `test_chain_after_acquire`, `test_soulseek_notify`,
`test_queue_view`, `test_queue_hygiene`, `test_outcomes`, `test_mb_auto_import`,
`test_artist_watch`, `test_staging_manage`, `test_import_queue`); the suites
that shared a file with it lost only that half (`test_downloads_layout` is now
`test_layout_report`, `test_import_archives`, `test_import_controls`,
`test_notifications`, `test_dep_updates`, `test_setup_wizard` and
`test_config_ui_parity` trimmed to what still exists).
