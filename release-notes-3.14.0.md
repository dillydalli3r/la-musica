# la musica 3.14.0 — disc rips, YouTube music videos, and the edition you actually want

This release is about getting the RIGHT file in: which edition of an album is
fetched, where a music video comes from, and what a DVD or Blu-ray rip becomes
once it lands in the library. Every part of it is a rule you can read in
**docs/OPTIMIZATION-GRADING-SPEC.md**, and the panels show the reason for the
pick they made.

## A DVD or Blu-ray rip is now one title, not a pile of parts

A disc rip arrives as a folder: `VIDEO_TS/` with a title set cut into 1 GB
`.VOB` parts (and `VTS_nn_0.VOB` is the MENU — never the feature), or `BDMV/`
where the `.mpls` playlist, not the file names, says which clips form which
title and in what order. The app reads both, picks the **longest title** (a
DVD's duration is its parts summed; a Blu-ray's comes from the playlist, so
nothing is decoded to choose), and remuxes it as **one MKV** beside the
structure through the same machinery every other video uses: video copied
bit-exact, lossless audio to FLAC, lossy audio copied, captions kept, chapters
off, then verified by ffprobe (stream counts and duration within 0.5 %). The
consumed parts are removed only after that verification, and a re-run is
idempotent.

**When the app cannot be sure, it asks instead of guessing.** You get a question
— in the notification bell, the import prompts API and the queue's "Needs you"
row — when the disc is an `.iso` (nothing here reads inside a disc image, and a
Blu-ray one is usually encrypted), when a playlist or part cannot be read (even
when only *some* of a disc's playlists parse, because the unreadable one may be
the feature), when a title has no measurable duration, when the runner-up is
within `max(30 s, 5%)` of the longest, or when a playlist plays a clip twice or
only part of one. A refusal touches nothing on disk, and the question withdraws
itself once the disc is resolved.

`prefer_disc_streams` (on by default) is the one switch, and it says what it
does: **on**, the disc's own streams win, so the "700 MB rip" a release ships
beside its `VIDEO_TS` folder is left where it is — never remuxed as if it were
the feature, never claimed to be a disc. **Off**, the disc handling goes with it
and those files take the ordinary per-file path.

## A digital music-video release is fetched from YouTube, in the same job

A music video published as **Digital Media** cannot be on Soulseek as a folder —
no disc, no log, no CRC — so "Add to library" now routes it: one YouTube search
per track through the same filter the film button uses (achieved length,
lyric/cover/tribute rows refused), downloaded with your own cookie settings,
renamed to `<disc>-<NN> <title>` before the import so nothing is named after an
upload title, and then the SAME import the Soulseek path runs — MB stamping,
`MEDIA=Digital Media`, `SOURCE=YouTube`, the naming script, and the configured
post-import chain in the background. A collection that is a dozen separate
uploads imports from what came back and tells you which tracks are missing;
finding nothing ends on the same wish offer an empty search does.

Everything else is untouched: a music video on a DISC (DVD, Blu-ray, VHS, Video
CD…) keeps the Soulseek path byte for byte, an audio release is never routed
anywhere, and a medium that is unstated or unknown is never guessed at.

## Which edition is fetched: a re-encode never beats the disc

The release-choice policy gained two things, and an audit against live
MusicBrainz found a third.

- **A compressed derivative of a disc sorts below the disc's own streams**
  (`prefer_disc_streams`, on by default). A `BDRip`, `DVDRip`, `WEBRip`, `x264`
  or `x265` release is somebody's lossy re-encode of a source that usually still
  exists, so it ranks under a remux or a full disc. Deliberately *not* markers:
  `remux`, `bdmv`, `dvd`, `blu-ray` (those name the disc itself, which is what
  wins) and codec names such as `h264`/`hevc`, which a remux carries too.
- **The date tier now prefers exact dates** — among editions of the same year
  the one stating `YYYY-MM-DD` beats one stating only its month or its year,
  because the album folder is named after that date. The rule was implemented
  but appeared nowhere you could read it; it is now in the policy list the
  release-group page shows, and the reason sentence names the part MusicBrainz
  actually omitted ("only the month", not "only the year").
- **The originals actually win again.** The distance penalty was linear and
  reached zero at a nine-year gap, so every older reissue tied and MusicBrainz's
  listing order decided: live, "The Dark Side of the Moon" (first released
  1973-03-24) came back as a **2016** reissue, ahead of the 2011 and 2014 CDs —
  and the album folder was therefore named 2016. It is a strictly-decreasing
  curve now: the same browse picks the 1988 CD (the first CD edition the group
  offers, since the medium order puts CD before vinyl), with the runners-up in
  strict date order.

## Also in this release

- **Aliases everywhere they help**: a MusicBrainz name shows the alias in *your*
  locale in parentheses — `宇多田ヒカル (Hikaru Utada)` — on the browser, entity,
  release-group and credits pages, picked by one setting (`locale`, which the
  old `beets_locale` migrates into) with an exact locale beating a regional or
  script cousin, and a search-hint alias never shown. It rides along in the
  request that was already being made, and the same setting translates non-Latin
  names for the Soulseek searches.
- **No wizard popup** on an unattended auto-import that finished before you
  looked: the album is simply there.
- **The compressed-source switch actually ships.** The Settings row and the
  policy read `prefer_disc_streams`, but the key itself was missing from the
  shipped defaults, so an install that never touched the row had a setting that
  only existed as a fallback. A new suite (`tools/test_config_ui_parity.py`)
  compares every Settings row's key against the shipped config, so a row without
  a key — or a key with no default — fails instead of hiding.

## Verified

113 test suites (the same set CI runs), the release-choice suite (10 rules, the
day/month/year rungs, the far-reissue regression) and its panel check, the disc
suite (86 checks: recognition, the pick, every refusal, the concat remux, the
originals policy, the layout and the prompt), a config/UI parity check, and the
Settings row driven live in a scratch app with headless Chromium (it renders,
ships checked, saves both ways).
