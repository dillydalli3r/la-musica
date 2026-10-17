# la musica 2.1.1 — no online discovery, no video downloader, and the medium must match the copy

The owner's decisions of 2026-10-17: *"remove yt-dlp (video downloading from
the app as well, and all recommendation behavior (online sources … basically
the Discover tab and all features like it querying online sources))"* — plus
five follow-ups from the same session, all in this release.

## Recommendation stops asking the internet

The Discover page, the Recommended page, the providers' charts on the Charts
page, the Home page's online shelves (Trending / New & notable / For you) and
the online recommendation shelf on artist, album and track pages are gone, with
`server/discover.py`, `server/api_discover.py` and every `/api/discover/*`
route, their sidebar entries, api client and settings rows.

What stays is what the library itself can say: the play-history charts
(`/api/top`), genre facets and tag-scoped browse, and the **offline**
"Recommended" shelves and infinite-playback queue (pure tag arithmetic, no
request). The provider layer keeps doing its non-recommendation work — artist
artwork and descriptions, cover fallbacks, lyrics, genres and web ratings as
tags — and the sources panel still reports it.

## yt-dlp and video downloading are gone

- `server/youtube.py`, `server/api_youtube.py`, the YouTube cookie jar
  (`server/cookies.py`, `server/api_cookies.py`) and
  `/api/videos/download-youtube` are deleted, with yt-dlp itself (installer
  tables, Dependencies row, config keys) and its three call sites: the
  YouTube-captions lyrics source (the chain is LRCLIB, NetEase, QQ, Kuwo,
  Kugou), the `youtube-age` advisory route, and YouTube Music playlist import
  (Deezer, Spotify and Apple remain).
- The video **library** is untouched: probe, remux (script 11), subtitles,
  thumbnails and the match panel never needed yt-dlp.

## The medium must match the copy

- **An explicit release choice now writes its MEDIUM.** A bulk pin, the
  wizard's link or a download's resolved match writes the release's own medium
  over a differing `MEDIA` tag; a release the import merely resolved from the
  files' tags stays fill-only. The owner's *MTV Unplugged* copy — tagged
  `12" Vinyl` while its MusicBrainz link names the CD — now becomes `CD` on the
  next import of that release (measured: 14 files written).
- **A rip log/cue against a non-CD medium fails grading** (new check
  `grade_check_media_evidence`, `MEDIA MISMATCH`), and the import warns the
  same thing inline in the Links step (`GET /api/import/media-check`) — one
  rule, both directions, and it never blocks: settling which of the two names a
  different edition is the user's call.

## The surfaces the owner asked about, the same day

- **Album cards**: the MusicBrainz disambiguation comment truncates instead of
  eating the line — the artist name keeps its room and the comment ends in an
  ellipsis after it.
- **Artist pages**: the header is a row of square icon buttons (favourite,
  play, download, export, tag actions, metadata review, links, stats), no text;
  "Change image" (and remove) appear on the artist image itself on hover or
  focus, and are always visible on a coarse pointer.
- **"Recommended (Local)"** is now just "Recommended".
- **Spotify and Apple Music are artist-picture sources**, Spotify primary: the
  default chain leads with Spotify's artist photo (needs the client id +
  secret; a hit counts only when its name is the artist asked for), then
  Deezer, TheAudioDB, Wikidata, Discogs, Last.fm, Apple's album artwork and
  Wikipedia — a provider without its credential answers nothing and the walk
  moves on.
- **The auto-import / bulk-import release dialog opens immediately** (rows
  seeded from the staged name) and fills each row's release as that album's
  payload lands, instead of waiting on one read per album before appearing.
- **An import run's notice is published when the run ends** (the coalescer is
  flushed at its terminal step) instead of waiting out the 20-second quiet
  window.

## Verification

- `tools/test_charts.py` (library history only), `test_sources_health.py` (70
  checks, no Discover kind), `test_streaming_playlists.py` (three services, a
  YouTube Music link refused with the reason), `test_lyrics_extra.py` (five
  providers), `test_genres.py` (the `youtube-age` route gone),
  `test_video_pipeline.py` (caption codecs), `test_platform_guards.py` and
  `test_dep_updates.py` (yt-dlp absent from every installer table),
  `test_import_controls.py` (136 checks, the dialog's rows still open on the
  release their files name), `test_notifications.py`, `test_grading_paths.py`,
  `test_setup_coverage.py`, `test_config_ui_parity.py` and
  `test_alias_images.py` all pass.
- The medium rule was measured on the owner's own album: the pinned release
  states `CD` (`media_formats`), the copy said `12" Vinyl`, and
  `_stamp_release_identity(..., explicit=True)` wrote all 14 files to `CD`
  while `explicit=False` left it alone. `GET /api/import/media-check` warns for
  a Vinyl release against a folder with `.log`/`.cue` and stays silent for the
  CD release.
- `test_release_choice.py` pins `medium_mismatch` both ways and the silence
  when either side states nothing; `test_cd_audit.py` pins the Disc ID
  algorithm; `check_import_minimum.mjs` (136 wizard checks), `check_setup_connections.mjs`
  (25) and the i18n parity (462 keys × 6 locales) pass; `tsc -b` clean,
  `oxlint src` warnings pre-existing, `npm run build` ok.
