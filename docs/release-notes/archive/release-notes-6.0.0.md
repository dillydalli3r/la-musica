# la musica 6.0.0 - the library manager, and nothing else

la musica manages, optimizes, audits and grades a music library. It was also a
player, a playlist manager, a ratings app, a podcast client and an app you
installed on a phone — and every one of those pulled against the thing it is
good at: the files on disk. **6.0.0 removes them.** What is left does one job:
import music, hold it to a standard, fix it, and hand it back.

The app is a **Docker server and a desktop window** (Windows, macOS, Linux). The
Android APK, the iOS IPA and the SideStore/AltStore source are discontinued.

## Removed

- **The player.** The bar, the fullscreen now-playing pane, the queue, gapless
  and infinite playback, the sleep timer, the volume control, the ReplayGain
  playback mode, the equalizer's playback application, the visualizer, synced
  lyric display, the OS media card and the mobile audio sessions are all gone,
  with their endpoints (`/api/stream`, `/api/videos/stream`, `/api/replaygain`,
  `/api/media`, …). Audio and video files are library content: the app tags,
  audits, grades and exports them, and does not play them.
- **Ratings, favourites and playlists.** The half-star store, the hearts, the
  manual and smart playlists, the streaming-service playlist import and the
  m3u8 export option are gone, together with the `RATING`, `WEBRATING` and
  `ALBUMWEBRATING` tags and the *Web ratings* script that wrote them. This app
  no longer keeps a verdict of its own about a track; it reports what the files
  say. The library's query builder stays — it is how you search a library, not
  how you build a playlist.
- **Podcasts, charts and discovery.** The podcast series page, play history,
  the charts page, the discover page and the recommendation shelves (local and
  online) are gone. Genre browsing of *your* library stays.
- **Offline downloads and notifications that reach a closed app.** The offline
  media cache, the service worker and the PWA shell, and Web Push with its
  subscription store are gone. The in-app tray still tells you a long run
  finished while the app is open.
- **Artist images, artist descriptions and album descriptions.** The app no
  longer fetches, stores, displays or grades them — script 19, `/api/artist/image`,
  `/api/artist/artwork`, `/api/album/description`, the metadata-review modal and
  the `metadata_*` settings are gone. Files already in an artist folder are
  **left where they are**: nothing is deleted, the app just stops looking at
  them. Artist folders are still graded for the one thing that is about the
  music — that the folder holds albums at all (`ARTIST_EMPTY`).
- **YouTube acquisition.** yt-dlp is no longer a dependency; the video search,
  download, match and caption paths are gone. Video **files** you already have
  are still tagged, remuxed, audited and graded.
- **Mobile and the phone's app shell**, including the mobile CI workflow, the
  Tauri iOS/Android configuration, the mobile icon sets and the SideStore/IPA
  release assets.

## Changed

- **23 scripts became 21.** Script 19 (*Optimize artist images*) and script 24
  (*Web ratings*) are gone; ids and titles are otherwise unchanged, and the
  shipped Run All order drops them:
  `[11, 3, 14, 15, 2, 1, 13, 17, 8, 5, 6, 7, 9, 12, 16, 10, 23, 20, 21, 4]`.
- **Grading is 67 checks.** The artist-image, artist-description,
  album-description and rating checks are gone with the things they judged;
  cover checks, the audit, the rip-log evidence and every tag check stay.
- **Config keys for the removed features are dropped on load.** A `config.json`
  that still names them is normalised without them, so an existing install
  upgrades by starting the new build.
- **State the removed features wrote is no longer read.** `plays.db`,
  `playlists.db` and the ratings tables are not consulted; they are not deleted
  from disk, they are simply no longer part of the app.
- **The equalizer is now an export tool.** It is the curve a *device* gets when
  you export to it (`export_eq_profile`) — auto-equalizer profiles and AutoEQ
  imports still work, there is just nothing to listen through in the app.
- **Lyrics stay, as metadata.** Fetching, formatting, translating, embedding
  and editing lyrics are untouched — the synced-lyrics *display* went with the
  player.
- **Per-device accent colour and the donations page are gone.**

## Kept

The import wizard and pipeline; the 21 optimization scripts; grading and the
check stack; MusicBrainz / Discogs / AcoustID identity; album covers, including
online cover search and the staged "choose a cover" review; lyrics; genre;
parental advisory; dynamic-range and ReplayGain **tag writing**; beets; trash;
storage; library statistics; export (including the export EQ bake); the desktop
shell in both of its modes (its own bundled backend, or a server you run);
Docker; and the in-app notification tray.