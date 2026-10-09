# la musica 7.0.0 - the player, the phone and Soulseek are back

6.0.0 cut la musica down to a library manager and nothing else. That was the
wrong trade: the player, the phone clients and the music-acquisition pipeline
were the parts the app is used *with*, not around. **7.0.0 reverses the cut and
brings Soulseek back on top of it** — the whole 5.5.0 surface restored, plus the
MusicBrainz-driven download stack that was removed at 4.9.0.

## Restored

- **The player.** The bar, the fullscreen now-playing pane, the queue, gapless
  and infinite playback, the sleep timer, the volume control, the ReplayGain
  playback mode, the equalizer's playback application, the visualizer, synced
  lyric display, the OS media card and the mobile audio sessions — with their
  endpoints (`/api/stream`, `/api/videos/stream`, `/api/replaygain`,
  `/api/media`, …).
- **Ratings, favourites and playlists** — the half-star store, hearts, manual
  and smart playlists, streaming-service playlist import, the `RATING`,
  `WEBRATING` and `ALBUMWEBRATING` tags and the *Web ratings* script.
- **Podcasts, charts, discovery and the recommendation shelves** (local and
  online).
- **Artist images, artist descriptions and album descriptions**, with script 19
  and the metadata-review modal.
- **YouTube acquisition** (yt-dlp) and the video search/match/caption paths.
- **Mobile and the phone shell** — the Android APK and iOS IPA workflows, the
  Tauri iOS/Android configuration, the mobile icon sets, the SideStore source
  and its release assets, and the `share-reachable` workflow.
- **Offline media cache, the service worker and Web Push.**

## Soulseek (back, and driving the import)

A whole acquisition stack, removed at 4.9.0, is restored and wired into the
app's own release-choice policy:

- **Search, download, verify, import.** A release is searched by what a peer's
  folder can carry — pressing traits (catalog number, barcode, label + country),
  the MusicBrainz release/recording ids, and per-track `artist title` queries —
  all in one parallel batch. A peer's folder must be **complete and lossless**;
  a CD rip must carry a `.log` (and `.cue`), the log is scored against
  `soulseek_auto_log_min_score` (100 = an Orpheus 100% log), and its per-track
  checksums are verified against the decoded tracks. A download that fails any
  of that is discarded and the walk moves to another peer, then to another
  edition.
- **Best-release selection** (`mlo/release_choice.py`): video-only < status <
  medium order (**CD first, then the other physical media, digital last**) <
  box-set < compressed derivative < track count < earliest date < full-date
  precision < the original over a clean/edited edition < a plain title.
  A physical release with no catalog number, barcode or label+country has no
  query to run, so the walk falls through to the next edition.
- **Fallback budget.** `soulseek_fallback_candidates` (default **5**) caps how
  many ranked editions one walk asks before the release settles into the
  background as a wish; a group with fewer candidates is handled without error.
- **Sharing, with router port mapping.** The library share (default: the
  `Artists/` folder, configurable) is served on the listen port, and the app
  asks the router itself for the mapping — UPnP IGD then NAT-PMP, with an
  explicit router address for container installs (`mlo/portmap.py`).
- **Queue view, wishes and artist watch.** `/api/queue`, the wishlist worker and
  the artist-watch worker (new releases only, never the back catalogue) are
  back, with their pages and notifications.

## Changed

- **23 scripts again.** Script 19 (*Optimize artist images*) and script 24
  (*Web ratings*) return, with their shipped Run All positions.
- **Config keys for the restored features are part of the defaults again**, and
  the acquisition keys (`soulseek_*`, `wishes_*`, `artist_watch_*`, `notify_*`)
  are normalised on load, exactly as 4.8.0 shipped them.
- **Mobile capability sets** (`capabilities/mobile.json`, the iOS permission
  manifest) ship again, so the packaged shells can drive the window.

## Kept from 6.0.0

Everything 6.0.0 kept is still here: the import wizard and pipeline, grading and
the check stack, MusicBrainz / Discogs / AcoustID identity, covers, lyrics,
genre, advisory, dynamic range and ReplayGain tag writing, beets, trash,
storage, library statistics, export, and both desktop-shell modes.