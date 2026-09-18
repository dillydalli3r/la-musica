# la musica

**la musica** (formerly Music Library Optimizer) — a modern, self-hosted web
app that *manages, optimizes, audits, grades and plays* your music library.
Built on the proven `mlo` engine with a React UI: playback of music **and**
music videos (with karaoke-synced lyrics), manual + smart playlists,
favorites, artist artwork and biographies pulled from Deezer, TheAudioDB,
iTunes and Wikipedia, a multi-source lyrics chain, an offline player cache,
multi-format export, and a Soulseek client with an automatic
MusicBrainz-driven importer that can verify what it downloaded with AcoustID.

All app state (config, playlists, favourites, the beets library, the
Soulseek config) lives in a single hidden `.mlo` folder inside your music
directory — app state in `.mlo/data`, with downloads (`.mlo/downloads`) and
trash (`.mlo/trash`) beside it — one folder to back up or carry between
machines.

## Highlights
- **Offline app, honest downloads, live share index** (new in 2.7.4) — the app itself now opens with the server down: the service worker precaches the built shell (every lazy route chunk, listed by the build as `precache.json`) and caches the library/config/album/artist payloads, so a downloaded album plays, renders its description and browses its artist offline. Downloads warm those payloads and drop them with the last track of the album. Download failures carry a reason ("the server has no file at that path", "the download was cut short (2 MB of 41 MB)") instead of a bare count, a stalled stream now has a 120 s deadline and one retry, and a truncated body is rejected rather than cached. Two dead routes were found and fixed on the way: `/api/track/download` had lost its decorator and `/api/track/export` referenced `detect_all_tools` it never imported — every export answered 500. Soulseek's share refresh was dead code (a worker defined and never started): tags, scripts, optimizations, organizes and imports now schedule a debounced share rescan, so the daemon stops serving its boot-time file list.

- **Dependencies: available version + auto-update** (new in 2.7.4) — the dependency tables (Dependencies page, Settings → Dependencies, setup wizard, `mlo` CLI) show **three** versions per tool: installed, the pinned target the app installs, and a new **Available** column fed by a live GitHub check (30-minute background TTL, `?refresh=1` to force, degrades to the pinned pair with a note when the API rate-limits). Status is derived from the live value, and a new `dependencies_auto_update` setting installs missing/outdated tools in the background when enabled.

- **RYM requests are browser-faithful, and say why they failed** (new in 2.7.4) — the scraper now sends a full Chrome header set, keeps a real cookie jar (RYM's own `Set-Cookie`, e.g. `__cf_bm`, rides along) and makes one warm-up navigation per pasted cookie before the page it wants. Refusals are classified: missing cookie, 403 without a challenge (WAF/network), challenge page (expired cookie), 429, 5xx — each with the fix, surfaced both in the sources panel and as `rym_last` (`HTTP 403 · no challenge marker · <url> · <time>`). Note: on a network Cloudflare blocks, a valid cookie still gets a 403 — the app now says exactly that instead of "the cookie is stale or this network is blocked".

- **Icon-button fixes** (new in 2.7.3) — an accent-filled icon button (play) takes its glyph colour from the theme's `--accent-fg`, not white: the mono theme's accent IS white, so the play glyph was invisible until something else coloured it on hover. The album/playlist heart now sits immediately right of play, and the cover-art menu wears the same 36px box as the action row instead of its own 30px square.

- **One row, one button, one moving background** (new in 2.7.2) — the album and playlist action rows are now a single shared 36px square recipe (`.btn-icon`, with an accent-filled play and a red armed state), so play, download, links, like, tags and the overflow menu line up instead of being four slightly different boxes; the square download button never prints a label — it fills a determinate progress arc while caching, scales its check in when done, and arming a removal turns the SAME box red and pulsing rather than expanding into "Remove 8?"; and the fullscreen background actually moves: the music window is measured against a rolling loud reference (a fixed dB window moved the glow by ~0.15 and the backdrop looked frozen), the glow swings most of its opacity and a third of its scale per beat, and the cover/sweep/colour-field clocks are 24-55 s instead of 62-180 s.

- **Downloads page, downloaded marks, calmer visuals** (new in 2.7.1) — the offline cache is a sidebar page again (`/downloads`, the library's own album table, per-track removal, two-step *Clear all*), and every track title carries a small green check while its audio is downloaded — and nothing when it is not; the album page's download control is a square icon button in the row with play and the tag actions; the fullscreen player's frequency strip allocates one gradient per frame instead of one per bar, drops from the display clock to a 10 Hz timer with no signal and stops painting once it has eased onto its baseline; and the background glow's CSS easing was shortened to a fraction of a second so it tracks the music instead of lagging a second behind it (writes are skipped unless the value moved).

- **Polish, robustness and hardening pass** (new in 2.7.0) — sidebar hover nudge plus shared motion tokens for consistent animation; lyrics robustness with millisecond precision, `[offset:]` clamping, translation/transliteration alignment, mixed synced+plain files, and a stale-track seek guard; performance via debounced library search, O(1) cover lookup, 60 s library cache, optimistic offline cache, and no background-tab polling; backend hardening with symlink-safe path guards, capped caches, and partial-success bulk tagging; Soulseek with bounded wish waits, no silent-drop handoffs, and daemon errors surfaced in the UI; plus keyboard/screen-reader and small-phone/tablet fixes.

- **Home** — a sidebar landing page of library highlights: recently added,
  best-graded, rediscover, top-artist, favourite, wanted and
  needs-attention shelves with a skeleton loading state, plus the library's
  own stats header. Everything on it is derived from the library itself —
  no provider is consulted, so it loads from the same cached payload the
  rest of the app uses.
- **Keyboard shortcuts** (new in 2.6.9) — `F` opens (and closes) the fullscreen viewer,
  `/` jumps to the search box, `?` (or the keyboard button in the top bar)
  shows the sheet, and Space / ← → / `[` `]` / `0` drive playback. Nothing
  fires while you are typing, and the lyrics editor keeps its own keys. The
  full list lives in `web/src/components/Shortcuts.tsx` — the one place the
  handler and the sheet read from.
- **Credits where you can see them** (new in 2.6.9) — the bottom-left corner of the app names
  the services and projects it is built on and links each one out; the popover
  behind it carries the full list with licences, and
  `THIRD-PARTY-NOTICES.md` has the legal text. The data is
  `web/public/credits.json` (the same file Settings → *Credits* renders), so a
  new provider is credited in one place.
- **Your columns, everywhere** (new in 2.6.9) — every table that can carry one has a Columns
  menu: the library's albums/artists/tracks views, the favourites track table
  and the album tracklists (album page and the library's expanded album rows
  share one set). Beyond the built-in columns you can add a **tag column** —
  name a tag (`MOOD`, `COMPOSER`, `CATALOGNUMBER`…), give it a label, sort it
  like any other — and it is stored per view. Settings → bottom bar has
  **Reset UI & layout**, which clears this browser's UI preferences (accent,
  sidebar, grid sizes, column layouts and widths, custom columns, viewer
  options) and reloads; the server config has its own *Reset to defaults*
  beside it.
- **Wishes** — save any MusicBrainz release to the library *without*
  downloading it. A background worker re-searches Soulseek for every open
  wish on a configurable interval and auto-imports a release the moment a
  verified rip appears; wishes already present in the library (a manual
  download) resolve themselves. An auto-import search that ends with nothing
  usable can be handed to that same worker instead of failing. See *Wishes*
  below.
- **Library explorer** — artists → albums → tracks with live grade/audit
  badges, search, "fail only" filter, selectable rows with bulk tag tools,
  and sortable/resizable columns (year, title, grade, audit, genre,
  advisory, duration, bitrate, dynamic range, …). Music-video files are
  first-class tracks.
- **Artist pages** — a real artist page: hero image, its own grade (only the
  checks that apply to an artist folder — the image and the description),
  the stored description with its source and a Fetch/Edit/Clear flow, and
  the album list. Artist images are fetched
  automatically from four providers when you ask for one, and the picker
  offers every candidate (plus a manual upload) when the automatic pick is
  wrong or missing. See *Artist pages* below.
- **Artist / Album / Track pages** — grading and auditing detail, identity
  links (MusicBrainz + RateYourMusic logo buttons on each link's own
  metadata row), the link paste-editor, album + per-track cover upload and
  online cover search, album/artist descriptions fetched from Wikipedia,
  TheAudioDB or MusicBrainz, manual tag editing, per-track video tag
  editing, and a full lyrics editor (synced/word-synced ELRC, translations,
  transliterations, hotkeys). A **Credits** view (the track detail modal and
  the album's overflow menu) lists who actually played on the record —
  MusicBrainz `artist-rels` grouped by role (performer with its instrument,
  vocals, producer, engineer, mix, mastering, arranger, conductor, remixer,
  plus the work a classical track belongs to), loaded only when you open it;
  when MusicBrainz has nothing (or the file carries no MBID) it falls back to
  the file's own PERFORMER/COMPOSER/LYRICIST/… tags and says so with a
  *file tags* badge instead of passing them off as catalogue data.
- **Player** — persistent player bar (queue, drag-reorder, shuffle,
  repeat-one, speed, sleep timer, ReplayGain, visualizer, volume shared
  app-wide) plus a **fullscreen player** with animated karaoke lyrics,
  queue and display options. The spectrum (fullscreen strip and the background
  ambience) is drawn on a perceptual dB scale with reserved headroom and a
  per-band rolling reference, so a loud master shows shape instead of pinning
  every bar at full height, and it meters both audio and music videos —
  starting on the first play, surviving track changes, seeks and buffering.
  The strip is bars only — the falling peak caps were removed, they read as
  clutter rather than information.
  With nothing playing the strip eases onto its baseline and then stops
  drawing (the loop drops from the display clock to a 10 Hz timer and skips
  the repaint), and its colour ramp is one gradient per frame rather than one
  per bar, so a paused or idle player is not a 60 fps redraw of the same
  line.
  The fullscreen background is **alive**: a blurred cover, an aurora sweep and
  three drifting colour fields (24-37 s clocks, each hue-rotated so the
  backdrop is colour rather than one flat tint) move on their own, and the
  music drives THREE layers at once — a wide glow, a hue-shifted bloom and the
  colour field itself, which swells with the beat. That value is measured
  against a **rolling loud reference** rather than a fixed dB window (a fixed
  window cannot work across masters — on a real track the mix swings inside
  ~4 dB, which moved `--amb` by only 0.15 and left the backdrop looking
  frozen): measured 0.31-0.88 across a track, with the glow's opacity swinging
  0.33-0.81 and its scale 1.03-1.22, and 30-40% of background pixels changing
  between frames three seconds apart. It is written ~14x/s and eased under a
  quarter-second by CSS, so it rides a kick without ever flashing (a write is
  skipped unless the value moved).
  The layers are switchable under the player's *Background* options
  (`mlo.np.orbs` color drift, `mlo.np.vis` music glow) and
  `prefers-reduced-motion` freezes the lot.
  ReplayGain is applied through the WebAudio gain
  stage in track, album or off mode with a preamp, and a track whose file
  carries no ReplayGain tags is measured on the fly (ffmpeg EBU R128,
  cached) instead of silently playing loud. Music videos play fullscreen
  with auto-hiding chrome, correct aspect ratio (no cropping), and every
  codec the bundled ffmpeg can probe (incompatible ones are transparently
  transcoded to fragmented MP4). Dragging a video's seek bar grows a
  **scrub preview** above the cursor: the frame at that position
  (`GET /api/videos/thumb`), debounced, cached per second, and silent — a
  frame that cannot be cut leaves the time label standing alone. Browser-fullscreen
  with a two-stage Esc.
- **Offline cache** — "Download" puts a track's audio in a service-worker
  media cache *and* warms what the page around it needs: the album and artist
  payloads (description, credits, cover references) plus the cover and artist
  image — all dropped again when the last cached track of that album or artist
  is removed. The worker also precaches the built app (every lazy route chunk,
  listed by the build), so the UI itself opens with the server down: a
  downloaded album plays, prints its description and browses its artist
  offline. "Export" is the real file-saving path.
- **Playlists** — manual playlists (drag-reorder, favorites, .m3u8
  export/import) and **smart playlists** driven by saved grade/audit/tag
  filters. Playlist pages look and behave exactly like album pages, with a
  2×2 mosaic cover built from the first four tracks.
- **Export** — export tracks, albums, artists, playlists or the whole
  library to MP3 (VBR/CBR presets + custom bitrates), Opus, Vorbis, WAV or
  bit-exact FLAC copies, with a size estimate and drive-fit warning.
  Cover art and identity tags travel with the files.
- **Favorites** — liked tracks / albums / artists / playlists, consistent
  with the library views (ctrl-click a track title anywhere to open its
  track page for editing; the player bar title opens it too).

- **Import** — drag & drop uploads, a watched import folder, or the
  Downloads/Soulseek paths: MusicBrainz release matching, **AcoustID
  fingerprint matching** (it tells you which release the audio actually is,
  not just which one the tags claim), cascading genre import, lyrics,
  **ITUNESADVISORY resolved per track by ISRC** (the file's own ISRC, or
  every ISRC MusicBrainz holds for its recording — every applicable source is
  asked on every track: Deezer by ISRC, Spotify by ISRC when
  `spotify_client_id`/`spotify_client_secret` are set, Apple's explicit-edition
  album route and its exact-title song search, Discogs' *Parental Advisory*
  format when `discogs_token` is set, and yt-dlp's `age_limit` for a track that
  records a YouTube id. An explicit statement anywhere wins (1), else any clean
  statement is 0, else 0 — an unstated advisory is written as 0, with the
  per-source `answers` map as the provenance; the Discogs and YouTube signals
  are explicit-only and can never clear a track, a `cleaned` Apple edition is
  never reported as clean, and an existing valid value is never overwritten),
  organize into the
  naming-script layout — and then the **import script chain** runs
  automatically (CUEs → FLACs → videos → lyrics format → lyrics fetch → auto
  tagging incl. mood/genre → images → audit → DR & ReplayGain → AccurateRip →
  key & BPM → beets → format all → grade), so an imported album lands complete
  instead of half-tagged. The chain is configurable (Settings → *Import*) and
  can run over several albums at once: **bulk import** queues them, imports
  with a configurable concurrency and reports per-album and per-script
  results. **An album the Soulseek auto-importer fetched is deliberately NOT
  tagged by itself**: it is placed in the library, its MusicBrainz identity,
  album+artist RYM links, artist metadata and cover candidates are resolved and
  the app then walks you into the wizard (see *Soulseek*) — every tag write
  after that is yours, in the wizard's steps or the tag-action menus.
  The artist image, artist description and album description are fetched by
  the same chain (`metadata_auto_fetch`) — with `metadata_review` on, the
  candidates are staged instead of written and you apply the one you want
  (see *Artist pages*).
  The wizard's eight steps are **Select & separate → Links → Match → Covers →
  Genres → Lyrics → Advisory → Finish**, and each one now offers what the rest
  of the app can do:
  - **Links** resolves the album's RateYourMusic page *and* its artist page
    automatically (one button, nothing written until you save) and reads the
    URL you paste — an `/artist/…` URL fills the artist field, a `/song/…`
    page is refused instead of being stored as the album link, which used to
    block the automatic lookup forever.
  - **Covers** offers the candidates the import fetched when `cover_review` is
    on (*Choose a cover (N)* opens the picker pre-loaded, same as the album
    page), plus upload / URL / MusicBrainz / finder.
  - **Genres** keeps the MusicBrainz import and adds the RYM-first genre
    *chain* the rest of the app uses.
  - **Finish** keeps the script checkboxes and the *Run the import chain*
    button (now enabled for an album opened by path, not just uploads) and adds
    **Run all scripts**, which runs your configured `run_all_order` over the
    album — the same pipeline Optimization → *Run All* runs, right where the
    import ends.
- **MusicBrainz auto-import** — a release, a release group or an artist can
  be downloaded without picking an edition by hand: *best* takes one release
  per release group, *all* takes every eligible edition of that group (an
  artist page keeps one per group either way, and skips groups you already
  own). The button **queues first, resolves later**: the request comes back
  in about a second with `{queued, items, skipped}` and the background job
  does the MusicBrainz lookups and the downloading. An ID MusicBrainz does
  not answer for within a few seconds is still queued, as
  `queued (resolving)`, and the job resolves it itself — an artist or group
  with nothing to queue answers `queued: 0` plus a `skipped` reason, never a
  404. MusicBrainz calls retry with backoff (429/5xx and connection errors,
  `Retry-After` honoured) under a ~45 s ceiling, so an outage fails only
  that job with a readable reason ("MusicBrainz is busy (HTTP 503)") while
  the queue moves on; the client stops after ~20 s and reports the reason
  ("MusicBrainz is busy — try again") while every Auto-import button shows a
  spinner and *Queuing…*. The pick is the auto-import policy below: Official
  first, promotional/bootleg editions dropped while
  `auto_import_avoid_promo` is on, editions with no RELEASECOUNTRY dropped
  while `auto_import_require_country` is on (default), then the medium order
  `CD → Digital Media → Vinyl → Cassette → Other`
  (`auto_import_medium_order`), earliest date breaking ties. Negative traits
  are ranked, not merely ignored: a withdrawn/expired/cancelled edition sorts
  below a plain release, a promo/bootleg below that, and an edition carrying a
  release country above one that does not — so a group whose only edition
  lacks a country is reported as ineligible instead of being downloaded on a
  guess.
- **Soulseek** — managed slskd instance (autostart, shares = music folder),
  whose share index is **refreshed whenever the library changes** (tags,
  scripts, optimizations, organizes, imports and deletes all schedule one
  debounced `PUT /shares` rescan, so the network stops browsing the file list
  the daemon saw at boot),
  search & download UI with a live status dot in the sidebar (the backend
  pushes a frame the moment the login state, the daemon or a port conflict
  changes, so the dot turns green on login — or red on an unexpected logout —
  without waiting for a poll or a reload), and an
  **auto-importer** that searches each release by its own most specific
  trait, verifies rip logs (minimum logchecker score) and download
  completeness, then imports and organizes the album automatically. A CD is
  searched by its catalog number alone; the search stops the moment a folder
  is both complete and lossless, instead of waiting out a window. Each query
  gets a **fallback search window** (`soulseek_auto_search_wait`, Settings →
  *Auto-import*, default 10 s) counted as quiet time since the network's
  last response — a rare album ends there, and a transfer that moves no bytes for 3 min is abandoned
  rather than tying up the job. While a job runs, the Soulseek page shows
  live response and file counts for the running query — no countdown, since
  the window is a ceiling a usable candidate ends early and a timer would
  promise a duration the search does not serve. It carries a **response
  limit** too (`soulseek_auto_response_limit`, default 15 peers): slskd only
  hands a search's results back once it has ENDED, so without the limit a
  popular album — peers replying for a minute straight — never went quiet and
  nothing was readable until the whole window had elapsed. Measured through
  the app's own client: no limit ⇒ 32 s before the first result; 5 responses
  ⇒ 0.5 s; 40 ⇒ 11 s. A search still running when the window closes is
  cancelled instead of left occupying slskd.
  **Every candidate the search scored is tried** in rank order — there is no
  rejection cap, each refused peer costs only its own attempt and everything
  it left behind is removed before the next one starts: the transfers are
  cancelled in slskd and confirmed, then the candidate's files are swept from
  the download folder (including slskd's `<name>_<ticks>` partial writes and
  anything the .log gate had already fetched) and from the staging dir, with
  the sweep repeated until a pass deletes nothing; the scope is the rejected
  candidate's own album folder, so a same-named file of another peer or of
  the same peer's other album is never touched. A job whose candidates all
  fail now offers the same *Wishes* handoff as an empty search, so a release
  whose peers never deliver a usable rip keeps being watched for instead of
  ending in a bare error. A failure to
  queue names slskd's own reason (`User <name> appears to be offline` for a
  peer that left between search and enqueue) instead of an opaque "500
  Internal Server Error". A peer whose transfers are all **queued** is held
  for its own queue budget (its reported queue length at its advertised rate,
  floored at 3 min) instead of being abandoned at the first stall, and a
  transfer that moved bytes and then froze is still dropped at 3 min.
  When the catalog number finds nothing usable, ONE broader `artist album
  year` query follows it — never in parallel, never when the first found a
  candidate — and a CD whose peers hold only WEB rips (all 23 tracks,
  lossless, no `.log`/`.cue`) parks on a **"No CD rip with logs found"**
  prompt offering those folders instead of dead-ending: accepting downloads
  one and stamps the album as *Digital Media* up front, so verification,
  tagging and grading all match what actually arrived. Declining falls back
  to the wishes offer.
  A CD candidate is **gated on its rip log before any album byte is
  requested**: the `.log` (one per disc) is queued alone, waited for and
  scored with Logchecker, and only a log that passes the configured minimum
  (`soulseek_auto_log_min_score`, default 100) triggers the second call that
  queues the album — so a peer whose log scores 60 costs a few kB and one
  queue join instead of a partial album that then has to be swept. The
  rejection names the score and the required bar, and the next candidate is
  tried; a log that never arrives is reported with the seconds it waited.
  The folder a download lands in is decided by the job, not by the peer: the
  app points slskd's destination template at a per-download folder under
  `.mlo/downloads`, so two peers offering the same album never collide in one
  top-level folder and a multi-disc peer keeps its disc folders *inside* the
  album folder (which is what used to get multi-disc candidates rejected).
  Emptied directories are pruned after every rejected candidate, cancelled
  job and successful import.
  A release the library already holds is refused everywhere it can be
  queued — the bulk routes skip it with `already in the library`, and the
  interactive job refuses with the same reason — so a finished import cannot
  be downloaded a second time; two jobs for one release are deduplicated
  against the running job and the queue as well.
  Download completion is judged on slskd's verdict AND the file itself: a
  transfer slskd reports succeeded must have landed at the expected size, and
  a file that is complete on disk with a stable mtime counts as arrived even
  when slskd has pruned the transfer record (cleared history, restart) —
  which is what used to make a finished album look unfinished and re-download
  it. Progress reports the **instantaneous** rate (byte delta between polls,
  never slskd's lifetime average), an ETA from remaining bytes at that rate,
  and three separate counts that are not the same thing: files slskd calls
  complete, files the pipeline has accepted, and the byte-weighted
  percentage. Candidates are
  ranked towards the copy that arrives **fastest**: complete and lossless
  first, then logs/cues, a free upload slot, the shortest queue and the
  **peer's advertised upload rate** — the folder's slowest file decides, worth
  up to 4.5 points (1 MiB/s each), so speed breaks ties between equally
  complete folders but can never buy an incomplete one. Search terms are as
  specific as the release allows: a **CD is searched by its catalog number
  and nothing else** (`soulseek_auto_cd_queries`), the trait rip folders
  actually carry and the one query that does not drag in every other
  pressing; Digital Media, which has no catalog number, uses
  `artist album year`. A release that carries **several catalog numbers**
  (MusicBrainz keeps every label-info number — a reissue under two labels, or
  the same number spelled `XLCD 324` and `XLCD324`) is searched once per
  number, all of them in flight together and their results merged before
  scoring, capped at four so a release with a dozen numbers cannot spam the
  network; wishes inherit the same set (a wish stores no query list, so the
  worker derives it from the release). A CD whose release carries no catalog number falls back
  to that wording once (and says so in the job log) instead of failing with
  nothing to search by. The tab names the account slskd is **actually signed
  in as**, and an auto-import that finishes with an album on disk takes the
  app straight to `/import?album=…` so tagging starts without hunting for the
  job.
  Search returns every codec the network offers, grouped per shared folder
  and ordered CD rip (log + cue) → lossless → free slot / shortest queue,
  with All / CD rips (log + cue) / Lossless / Lossy chips plus a codec
  filter built from what the results actually contain. The search box keeps
  your last 10 queries in a recent-searches list under it. From any result
  group, **Browse** opens that peer's whole share tree — filterable and
  paged — where a folder can be queued as-is or handed to the
  auto-importer; a browsed folder needs no MusicBrainz release, because its
  audio files become the track list.
  Lossless means the
  files' extensions — M4A/MP4 count as lossless only when the peer reports
  a bit depth, since those containers hold ALAC or AAC. Downloading a
  folder with no lossless audio asks for confirmation first. slskd
  publishes a search's file responses only after the search has ended, so
  the UI polls and shows live response/file counts while it aggregates
  (up to 180 s). Imports convert any lossless source (WAV, AIFF, APE, WV,
  SHN, TTA; ALAC in MP4) into the codec set by `lossless_target_codec`
  *before* the files are named and graded — the same conversion script 3
  applies library-wide. When the search window closes without a usable
  candidate, the job parks and offers to move it to *Wishes* rather than
  reporting the old "no candidate folder contained every track" failure
  (`soulseek_auto_wish_prompt`, default on); see *Wishes* below. The page
  toasts every terminal auto-import state, including "added to wishes", and
  every wish that flips to **Imported** or **Failed**. The *Downloads* tab
  buckets transfers into active, queued, completed and failed, with
  per-transfer **Cancel**, **Retry** on the failed ones and three clearing
  actions: **Clear finished** (history only), **Clear failed** and **Clear
  incomplete** — the last cancels every in-flight transfer in slskd *and*
  deletes the partial bytes it had staged, then prunes the emptied
  directories, so an abandoned download stops occupying the staging area.
  Each reports `N cleared · X freed` and lists anything slskd refused to
  drop.
  The sidebar dot is green when logged into the
  Soulseek network (tooltip names the account), amber when slskd runs but
  isn't logged in (tooltip carries the daemon's own error, e.g.
  `INVALIDPASS`), red when another app's slskd holds the web port, and
  hidden when slskd isn't running.
  **Bulk and whole-user downloads** — tick any number of files in a result
  group and queue them in one request, or take everything a peer shares with
  *All from <user>* (it re-browses the share first and skips transfers that
  are already queued); a running search can be cancelled instead of waited
  out. slskd's own transfer settings are editable in Settings → *Soulseek*:
  download/upload slots (`soulseek_download_slots`, default 3;
  `soulseek_upload_slots`, default 2) and optional speed limits in KiB/s,
  written into the generated slskd yaml — a limit of 0 means "no limit", so
  the key is omitted rather than sent as zero.
  **Music videos** for a release whose medium is digital/web are downloaded
  from **YouTube** on the album page (yt-dlp, `youtube_enabled`): the best
  video-only + best audio-only streams merged into MKV, ranked by **highest
  video bitrate, then audio bitrate, then resolution** — `youtube_max_height`
  (0 = best) is the only way to cap it. The candidate must pass the lyric /
  cover / tribute filter and the track's own length (±5 s). A CD or vinyl
  release stays Soulseek-only, and a video download never touches the
  album's audio tracks.
- **Grading** — a configurable battery of ~50 checks per album (tags,
  encoder identity, naming + capitalization, lowercase extensions, links,
  covers, CUE/log/AccurateRip, lyrics formatting,
  **mood, energy & genre presence**,
  **album descriptions**, file categories…). Every check can be toggled on
  the Grading page; the verdict shows as a red/green dot on every album and
  track with the failed checks itemized. Artists are graded too — on the
  two things that actually apply to an artist folder (image, description) —
  and the artist page shows that badge next to the album aggregate, so the
  two are never confused.
- **Desktop + Web** — served by FastAPI (browser or Docker); a Tauri v2
  desktop shell lives in `desktop/`.

## Optimization: the 17 scripts

Run All executes a configurable order (default shown in parentheses where
it differs). Every script can run individually, on selected albums, or be
forced to redo work.

| # | Script | What it does |
| --- | --- | --- |
| 1 | Format lyrics | Canonical embedded LYRICS / .lrc (padding, blank lines, zero-timestamp rule, Enhanced/Extended LRC word-sync tags) |
| 2 | Format CUEs | Canonical CUE text, FILE-line fixes, CD-N sheet renaming |
| 3 | Optimize FLACs | Re-encode at target level, strip padding/CUESHEET/APPLICATION, remove tags outside the canonical set, convert every other lossless source (WAV, AIFF, APE, WV, SHN, TTA; ALAC in MP4) to the codec set by `lossless_target_codec` losslessly (default FLAC, alternative ALAC — Settings → *FLACs & lossless sources*); `lossless_remove_original` decides whether the pre-conversion file survives a verified conversion |
| 4 | Grade | The full grading battery below |
| 5 | Process images | Covers resized/cropped (default 1200×1200 JPEG q90; per-format size targets for JPEG/PNG/JXL, configurable), JPEG/PNG/JXL optimization, optional JPEG XL conversion |
| 6 | Audit library | AudioAuditor detectors (silence, DR, peaks, LUFS, BPM, MQA, fake stereo…) + CD .log CRC verification → AUDIT tag |
| 7 | DR & ReplayGain | rsgain + simple-dr-meter (album gain, FLAC and MP4 alike) |
| 8 | Auto tagging | **ITUNESADVISORY** normalization + cross-referenced auto-fetch (every applicable source asked on every track and merged — Deezer and Spotify by ISRC, Apple's explicit-edition album route and its song search, Discogs' parental-advisory format with a token, yt-dlp's 18+ gate for a track with a YouTube id; explicit anywhere wins, else clean, else 0 — an unstated advisory is written as 0, and 0 is *clean*), **INSTRUMENTAL** cross-referenced from LRCLIB's `instrumental`, Spotify audio-features `instrumentalness` (when its credentials are set), the file's own name and lyrics evidence — an "instrumental" answer anywhere wins (1), "not instrumental" is 0, and nothing is written when no source can state anything — plus **MOOD** and **ENERGY** (0-100) from the track's own audio (valence/arousal quadrant, refined by the genre), and a missing **GENRE** filled from the genre chain |
| 9 | AccurateRip | .accurip generation via CUETools, checksum verification |
| 10 | Format All | Final canonical pass: accurip/cue/lrc/tag trim + **embedded cover policy** |
| 11 | Remux videos | Any video container (VOB/AVI/WMV/TS/MOV/FLV…) → MKV, video copied bit-exact when possible, every audio stream re-encoded to FLAC, subtitles copied, chapters preserved (MP4/M4V are scanned but only remuxed while `video_process_mp4` is on — they already play natively) |
| 12 | Key & BPM | librosa-backed INITIALKEY + BPM (musical/camelot/openkey notation) |
| 13 | Fetch lyrics | The configurable synced lyrics chain (default LRCLIB → NetEase → QQ Music → Kuwo → Kugou → YouTube captions) into the configured format (embedded / .lrc / both) |
| 14 | Beets tagging | Managed beets (Picard parity) with the naming script, genre import, work/movement tags. Its output is streamed to the progress bar (one tick per item, so the longest step of a run is no longer a static label) and the plugin skips the locale-alias lookups a Latin-script library cannot use — measured on one 8-track CD album: 42 s → 14 s |
| 15 | Release tracklist | Records the MusicBrainz release's own tracklist as `.mlo_expected.json` in the album folder (release id + disc/position/title/recording MBID per track) — the only way a PARTIAL import can name what never arrived. Grading requires it (`grade_check_expected_tracks`), the album page greys out the missing tracks from it, and `finish_album` runs it on every import path (after tagging, since it needs the release id the match wrote). An album with no MusicBrainz id is reported and skipped, never given a fabricated manifest |
| 16 | Mood & Energy | The mood classifier on its own: decodes each track's audio (librosa; videos through ffmpeg) and writes **MOOD** plus **ENERGY** — the 0-100 arousal the verdict was scored from. Script 8 runs the same stage as part of its pass; this is the one to run when only the mood work is wanted (a genre rewritten since, `mood_source` changed, ENERGY backfilled onto a library tagged before it existed). Already-tagged tracks are skipped unless the script is forced (`force_mood`), and `mood_enabled` off skips it entirely |
| 17 | Lyrics transliterate (AI) | The one optional model in the app: romanizes non-Latin lyrics and translates them into every language in `lyrics_translation_langs`, writing `TRANSLITERATION-JA-LATN` / `TRANSLATION-EN` tags (and `.romaji.lrc` / `.<lang>.lrc` sidecars for LRC/BOTH lyric formats). Line structure and timings are preserved and re-synced at `lrc_sync_level`, so the transforms stay karaoke-aligned with the original; blank lines pass through, already-Latin lyrics are skipped (romanizing them is a no-op) and a "translation" that mirrors its source is not stored. Any OpenAI-compatible endpoint works (Settings → AI, or the setup wizard); answers are disk-cached per track so re-runs only pay for changed lyrics, and with no AI configured the script logs one line and does nothing |
| 18 | Publish lyrics (LRCLIB) | Gives back: for every track that carries lyrics (embedded `LYRICS` or an `.lrc` sidecar) it asks LRCLIB whether it already knows that recording — artist, title, album and duration, the same exact-then-search lookup the fetch chain uses — and, when it does not, submits this library's own text (`POST /api/publish`). A synced text goes with its plain form beside it, because LRCLIB wants both. LRCLIB is the app's first lyrics provider, so a hand-tagged library is exactly what the database is missing. Default ON (`lrclib_auto_publish`; off skips the script everywhere), and a per-track rule the script can never override: a track LRCLIB already answers for is never touched (`force_publish` re-submits anyway). Skips are counted apart — `already on LRCLIB`, `no lyrics`, `instrumental`, `no duration` — and a 409 duplicate is a skip, not a failure. The manual *Publish to LRCLIB* button on the lyrics editor is unchanged and shares the same client. |

### Tag actions: re-running a script on a selection

Every selection (a track, an album, an artist's folder, or a checked batch)
carries the same menu, and its **Re-run & overwrite** section is the way to
redo work the files already carry — each entry sets the script's own force
flag, which is the only thing that makes it look at a file again:

| Entry | Runs | Force flag |
| --- | --- | --- |
| Force re-audit (rewrite AUDIT tags) | 6 | `force_audit` |
| Force AccurateRip (.accurip rewrite) | 9 | `force_accurip` |
| Force DR & ReplayGain (rewrite tags) | 7 | `force_dr_replaygain` |
| Force re-encode FLACs | 3 | `force_reencode_flac` |
| Re-grade | 4 | — (grading always re-reads) |

The same flags are on the Optimization page's Force panel; the menu is the
short path when you are looking at the one album that needs it.

### Embedded covers (new in 2.1.0)

**By default the optimizer does not embed cover art — it removes it.** Audio
files stay lean; covers live on disk as `cover.*` (and per-track sidecars),
which every player can read. Settings → *Embedded covers* flips the policy:

- **Embed covers into audio files** (off by default). When on, script 10
  embeds the album cover into every track (FLAC picture, MP3 APIC, MP4
  `covr`, OGG/Opus `METADATA_BLOCK_PICTURE`), replacing any existing art;
  script 3 stops stripping the FLAC PICTURE block.
- **Embedded JPEG quality** — applies only when the embedded image is a
  JPEG (PNG/lossless embeds ignore it).
- **Embedded cover max resolution** — longest side in px, downscaled with
  the aspect ratio preserved; 0 keeps the cover file's own size.

The pass is idempotent — it only rewrites files whose embedded art actually
changes.

### Missing covers (fetched at import, and YOU pick by default)

An import that finds no cover art does not decide for you any more
(`imports.run_cover_step`). Two switches in Settings → *Images*:

- **`cover_auto_fetch`** (on) — fetch candidates for a missing cover during
  the import. Off means nothing is fetched: the cover finder, the wizard's
  *Covers* step and the grader's *Missing cover* verdict are the only cover
  paths.
- **`cover_review`** (on, the default) — the fetched candidates are **staged,
  not written**: the album page shows *Choose a cover (N)* next to the empty
  cover slot, opening the finder pre-loaded with exactly those candidates, and
  your pick is written. Turn it off for the old automatic behaviour (the best
  candidate is applied as part of the import).

Either way the chain is the same as the finder's: the album's own
`MUSICBRAINZ_RELEASEGROUPID` asks the Cover Art Archive by id, otherwise the
COV meta-search runs on artist + album and falls back to the Cover Art
Archive → Deezer → iTunes. Whatever is finally accepted goes through the
upload writer, so the file is cropped/resized/re-encoded to the library's
settings (`cover_target_size`, `cover_jpeg_quality`, …). An album that
already has a cover is skipped without a single request, a provider failure
leaves the album exactly as it arrived (the reason lands in the import
result), and nothing is ever fatal.

### Cover compression

Covers are normalized by script 5 (Settings → *Images*):

- **Resize** to `cover_target_size` px (default 1200) when
  `cover_resize_enabled` is on, and **crop to square** when
  `cover_crop_enabled` is on. `cover_force_exact_size` crops regardless of
  aspect deviation, so the output is exactly *target*×*target*.
- **Re-encode** as JPEG at `cover_jpeg_quality` (default 90 %). Other images
  keep their own quality setting; covers are the only ones re-encoded at 90.
- **Per-format size targets** — `cover_jpeg_target_size`,
  `cover_png_target_size` and `cover_jxl_target_size` override the global
  target for that one format; 0 means "use the global target".

### Filenames, capitalization and extensions (new in 2.1.0)

The app now enforces canonical file naming in all three ways:

- **Check** — the organizer's preview and the naming-script grading compare
  every track's full path (folders included) against the configured naming
  script.
- **Grade** — new checks: *Path capitalization* (a path that matches the
  naming script except for letter case — `TOXICITY` vs `Toxicity` — fails)
  and *Lowercase extensions* (`01 - Song.FLAC` fails). Both toggleable on
  the Grading page, both on by default.
- **Optimize** — organize applies the naming script's exact capitalization
  and lowercases every extension it touches (audio files, same-stem
  sidecars, and leftover files like covers/logs). Case-only renames work on
  case-insensitive filesystems too.

#### The default naming script (current)

```
%albumartist% [%musicbrainz_albumartistid%]/$if(%releasetype%,[%releasetype%] ,)$if(%originaldate%,%originaldate% - ,)$if(%date%,%date% - ,)%album% {$if(%releasecountry%,%releasecountry%)$if(%media%,$if(%releasecountry%, - ,)%media%)$if(%catalognumber%,$if(%media%, - ,$if(%releasecountry%, - ,))%catalognumber%)}$if(%label%, [%label%])$if(%musicbrainz_albumid%, [%musicbrainz_albumid%])/%discnumber%-$num(%tracknumber%,2) %title%$if(%musicbrainz_trackid%, [%musicbrainz_trackid%])
```

Every level is identifiable without reading tags, and the segments are all
`$if`-guarded — no dangling `[]` or ` - `:

```
System of a Down [cc0b7089-…]/[Album] 2001-08-27 - 2001-09-04 - Toxicity {US - CD - CK 62240} [American Recordings] [f8a44d0f-…]/1-04 Psycho [4f0e7e10-…].flac
```

- the album folder: name + release id, the artist folder: name + artist id
- `[Release type]` uses the tag's own spelling; **both dates are written in
  full** — the original (release-group) date first, then the release's own.
  The tags are kept full, not merely read: *Auto tagging* fills DATE and
  ORIGINALDATE from the cached MusicBrainz release and **sharpens** a value
  that only holds a year of the same date (`1980` → `1980-10-01`,
  `1980-10` → `1980-10-01`), the beets plugin writes both from its own
  match, and the Soulseek stamper writes the release it downloaded. A tag
  that already carries the day — or that names another date entirely — is
  never touched. Re-run *Auto tagging* (or a beets pass) and then *Organize*
  to bring an existing library's folders onto the full dates.
- the original date reaches the folder from whatever container it lives in:
  MP3 keeps it in **TDOR** (`ORIGINALDATE`'s vorbis comment on FLAC, the
  `ORIGINALDATE` freeform atom on MP4) — the spelling Picard and beets read,
  so the beets import computes the same album folder the organizer expects.
  Files this app tagged before that spelling are still read and are rewritten
  to TDOR on the next write.
- the brace group is `country - media - catalog number`, each segment joined
  only when the one before it is present (a rip with no catalog number keeps
  its `CD`, and one with nothing to say keeps no braces at all)
- the optional ` [label]` / ` [release id]` groups follow the braces, and the
  file name ends with the recording id (`%musicbrainz_trackid%`)
- with nothing but album/title the same script degrades to
  `Artist/Album/1-01 Song.flac`

Scripts from before this one are migrated on load: a stored default is
swapped for the current one, a script you actually edited is kept.

- **Long paths are handled** (new in 2.6.8) — the default layout spells the
  artist id, both dates, the media type and the release id into the path, and
  a real library reached **257-279 characters**, past Windows' 260-character
  limit. Python reads those files fine (it uses the wide APIs) but the
  bundled tools do not, and they failed QUIETLY: `flac` answered "No such
  file or directory", so the FLAC pass did nothing, the audit could not read
  the audio and stamped `AUDIT=FAKE`, and file listing (`glob`) skipped the
  folder entirely. Every tool call now hands the child a path it can open —
  the volume's 8.3 alias where one exists, otherwise a temporary junction on
  the album's own folder (created on demand, removed when the app exits) — so
  the optimizer, the audit, AccurateRip, the DR pass and the video paths work
  on deep libraries too. Nothing about the naming script changes: the same
  file is read and written either way.

- **Multi-value tags keep the first value** (new in 2.4.0) —
  `%releasecountry%` (or its `%country%` fallback) and `%label%` may hold a
  list (`; `, ` / ` or `+`); the first non-empty entry wins and the rest are
  dropped, so one album always yields exactly one deterministic path. The
  organizer, the beets plugin and the grader share that one implementation,
  so they cannot disagree about where a file belongs.
- **Release type** — `%releasetype%` is evaluated with the tag's own spelling
  (`Album; Live`), and MusicBrainz's `album+live` and the lookup's
  `Album (Live)` are equally accepted. A tag that is missing while the
  in-process MusicBrainz cache is cold is matched as a wildcard and reported
  as *Missing RELEASETYPE tag* — never as an invented path, and never with a
  network call.

## Home

The sidebar opens on **Home**: library stats plus shelves of owned albums —
**Recently added**, **Best graded**, **Rediscover** (a random library slice),
**Favorites**, **Top artists**, **Wanted** (open Soulseek wishes) and
**Needs attention**. Every card is an album you already have and clicks
through to the album page.

Nothing on Home asks a provider anything: the shelves are built from the same
cached library payload the rest of the app reads, so the page never waits on
a network call. Settings → *Home* controls how many albums each shelf shows
(`home_recent_count`).

## Discovery — the provider chain (new in 2.4.0)

The keyless provider layer (`server/discovery.py`) now serves one thing:
**artist artwork and biographies**, walked in the order set under
Settings → *Artist images & descriptions*.

| Provider | What it gives |
| --- | --- |
| **Deezer** | Artist photos (1000px) |
| **iTunes** | High-resolution artwork |
| **TheAudioDB** | Artist biographies and press photos/banners |
| **Wikipedia** | Artist and album descriptions (lead-paragraph summaries) |
| **MusicBrainz** | The identity anchor: release-group/artist MBIDs, and the final fallback |

- **Fallbacks everywhere.** Each feature walks its configured source list in
  order and falls back to the next provider; MusicBrainz is always last, so a
  provider going dark degrades to "fewer, plainer results", never to
  "no results". Order and on/off switches live in Settings → *Discovery*.
- **Cached.** 30 minutes for metadata, with per-host politeness (MetaBrainz
  and Wikimedia get 1 req/s and a contactable User-Agent).

## Artist pages (new in 2.4.0)

- **Image** — fetched automatically from Deezer → TheAudioDB → iTunes →
  Wikipedia on request, **by MusicBrainz id when the folder carries one**
  (TheAudioDB's exact `artist-mb.php` record first): a bare name picks the
  wrong subject often enough to matter ("Nirvana" is a 1960s UK band on
  Deezer, whose album cover was stored as the Seattle band's photo). When the
  automatic pick is wrong or nothing is
  found, the picker lists every candidate from every provider (plus a manual
  upload), so "no image" is a prompt, not a dead end. Images are stored in
  the artist folder (`artist.jpg`, provenance in
  `.mlo/data/artwork.json`) and normalized to the configured cover aspect
  ratio at the configured JPEG quality — **no resolution requirement by
  default** (`artist_image_target_size` 0 keeps the provider's native size;
  small images are never rejected, and nothing is upscaled).
- **Description** — the **full article**, not the lead paragraph: Wikipedia's
  whole page as text, its in-text links kept as markdown and its section
  headings kept in wiki form (`== History ==`, one `=` per heading level, the
  viewer hides the markers), fetched from MediaWiki's `action=parse` (the
  link-free `prop=extracts&explaintext` is the fallback), TheAudioDB's
  complete biography, or a MusicBrainz annotation, saved as `description.txt` in
  the artist folder, with the source shown and a Fetch / Edit / Clear flow. For
  an artist with a MusicBrainz id the Wikipedia *title* is resolved through the
  entity itself (MusicBrainz → Wikidata QID → the `enwiki` sitelink), so
  "Nirvana" reads as `Nirvana (band)` and never as the Buddhist concept;
  `description_full` (on by default) can be switched off to store just the lead
  paragraph again. Long text is clamped to a few lines with a **Read more /
  Show less** control (the same component on album pages), so a 36,000-character
  article never pushes the page around.
- **Fetched on import too** (new in 2.4.0) — the import chain's metadata step
  runs the same providers for the artist image, the artist description and the
  album description (`metadata_auto_fetch`, on by default) and saves the best
  candidate, never overwriting something you already stored. The artist folder
  is now matched even when its name carries the naming script's `[mbid]`
  suffix — the lookup that, before 2.6.x, silently skipped every artist folder
  and made this step look like it had run when it had written nothing.
  **Manual review mode** (`metadata_review`, off by default) stops the
  writing: the candidates are staged in
  `<music>/.mlo/data/metadata_review.json`, the album/artist page shows them
  with their source and fetch time, and only *Apply* writes the file you
  picked. The tag actions menu re-runs the step on demand, so a batch that
  landed with the wrong artist photo is one click from being fixed.
- **Its own grade** — the artist page badge grades exactly two things:
  *Artist image stored* and *Artist description stored*. Album checks stay
  album-level; the page shows the album aggregate next to it so the two are
  never confused.
- **Releases as a grid** — the artist page lists releases with the same card
  grid as the library page (`AlbumCard`, the shared grid-size setting, cover /
  year / track count / grade badge / play overlay), grouped by the type tags
  the tracks carry (Album, EP, Single, Live, Compilation, Other) with the
  search box and selection bar unchanged.

## Lyrics — six synced sources, in the order you choose (new in 2.4.0)

Lyrics are no longer one provider. The chain is **LRCLIB → NetEase → QQ
Music → Kuwo → Kugou → YouTube captions** by default, and it walks the list
until a provider has the song. Every one of them is free, needs no key, and
answers with timestamps — the order is a ranking, and each step is a reason:

- **LRCLIB (#1)** — open, community-maintained synced lyrics; no key, best
  global coverage of the six, and the only one that is both open data and
  worldwide.
- **NetEase (#2)** — a very large catalogue, synced with translations, the
  strongest for CJK releases. Unofficial API.
- **QQ Music (#3)** — synced with translations, strong mainstream coverage.
  Unofficial API, and a loose search: the match score decides what is the track.
- **Kuwo (#4)** — synced with translations, large catalogue. Unofficial API,
  loose search (only originals are written).
- **Kugou (#5)** — synced, large catalogue, weaker match quality than the four
  above. Unofficial API.
- **YouTube captions (#6)** — synced captions of a *known* video id,
  auto-generated ones included (those can mishear). Only for tracks that carry
  a YouTube id, so nothing is ever searched on YouTube; needs yt-dlp.

`GET /api/lyrics/providers` returns that ranking with each provider's notes,
the saved order and the plain-lyrics policy.

- **"Has lyrics" means lyrics, not a file.** A `.lrc` sidecar counts only
  when real text survives stripping: a 0-byte file, a lone `[00:00.00]` stub or
  a metadata-only header (`[ar:…]`, `[ti:…]`, `[offset:…]`) is *absent*, so the
  wizard stops claiming lyrics a track does not have and the fetch overwrites
  the stub instead of skipping it. A sidecar shared by two same-stem files
  (`01 Song.flac` + `01 Song.mp3`) is credited to neither — one file's lyrics
  never make another file look finished. The same rule gates the
  INSTRUMENTAL=1 → 0 flip, so a stub cannot silently mark a track as having
  vocals.

- **Credits are not lyrics.** NetEase, QQ and Kugou hand the contributor
  block back as the first "line" — usually at `[00:00.00]`, once per label
  (`作词 : Byrne, Eno, Talking Heads` / `Lyrics: Byrne, Eno, Talking Heads`).
  Script 1 and script 13 drop those lines: the credit becomes a blank line and
  its stamp dies with it, so the first real lyric keeps its own time. A real
  lyric that shares the stamp is kept — only the credit line goes — and a file
  that held nothing but credits is left with no lyrics at all, which is what
  "has lyrics" and the fetch chain then report. A lyric that merely mentions
  the words ("and the lyrics by heart") is untouched: the rule is anchored at
  the start of the line and needs a label or a `… by`.
- **Automatic fetches need a confident match.** The chain's search floor
  (0.6) is deliberately loose — it is what a person browsing candidates
  wants — but a hit is only *written* unattended at 0.85, which a same-title
  answer from a different artist cannot reach. The manual search box keeps the
  loose floor and never writes on its own.
- **Synced or nothing.** An answer without timestamps is thrown away as if
  the provider had none. LRCLIB's untimed records can be allowed back with
  `lyrics_allow_plain` — off by default, and the only opt-in that lets plain
  text through.
- **Configurable** in Settings → *Lyrics*: reorder the sources, drop the ones
  you don't want, and see each provider's note (what it is good at) in the
  list. An empty order means the built-in chain.
- **One button.** The track page's **Auto-import lyrics** (and the import
  wizard's bulk action) runs the whole chain, reports which provider filled
  each track (`stats["by_provider"]` in script 13's report, `provider_label`
  in the API response) and writes the result exactly like script 13 — same
  canonicalization, same `lyrics_format` (embedded / .lrc / both) rules.
- INSTRUMENTAL tracks are never touched and existing lyrics are never
  overwritten unless you ask for a re-fetch.
- The lyrics manager's search box can query the chain without writing
  anything (`GET /api/lyrics/find`).
- **Publishing gives back, never overwrites.** "Publish to LRCLIB" (the lyric
  editor and the manager) asks LRCLIB first, with the same exact-then-search
  lookup the fetch chain uses, and refuses when the database already answers
  for that recording — the community copy is not this app's to replace. Script
  18 applies the same rule for a whole library, with `force_publish` as the
  one documented exception. A lookup that cannot be reached does not block the
  submission: only a *found* record does.

## Mood, energy & genre (rewritten in 2.4.0)

Script 8 (Auto tagging) writes three audio-derived tags now, and grading
requires them:

- **MOOD** — computed from the track's own audio: RMS energy, onset rate,
  tempo, spectral centroid (brightness), dynamic range and the detected
  key feed a valence/arousal score, which maps to one of eight moods
  (`happy`, `energetic`, `aggressive`, `sad`, `calm`, `dreamy`, `dark`,
  `party`). In the default `hybrid` mode, the track's GENRE nudges an
  ambiguous verdict (a low-confidence reading of an *Ambient* track becomes
  `calm`); `audio` mode ignores the genre entirely. Thresholds are named
  constants in `mlo/moods.py` and documented there.
- **ENERGY** — the same arousal value as an integer 0-100, written in the
  same pass as MOOD (`mood_enabled` gates the stage, the per-filetype
  `audio_tag_writes[<filetype>]["ENERGY"]` switch gates the tag alone), so
  switching ENERGY on for one format backfills exactly that format and
  leaves its MOOD untouched. Music videos get both tags too — the mood
  writer covers video containers, in one write per file.
- **GENRE** — filled only when the tags carry none, from `genre_sources`,
  merged per track: the source order, then each source's names, then a
  case-insensitive de-duplication, Title Case, and a cap of
  `mb_genre_count` (default 3) **per track**. The default is **MusicBrainz →
  RateYourMusic** — the two the library actually agrees with; the other nine
  providers stay in the registry and can be added back in Settings →
  Discovery (a saved list is used exactly as saved, so an old eleven-source
  config is migrated to the new default only when it is byte-for-byte the
  shipped order). In the import wizard the two run as **separate buttons**
  (*From MusicBrainz* / *From RateYourMusic*), one source each, so a failing
  provider can never look like a slow one:
  - **Every source is asked on every track**, at the best level that API
    allows. Per-track sources come first: RateYourMusic's release page (per
    track where the page states one, else the release's own list), ListenBrainz
    (recording → release group → artist), MusicBrainz (recording → release →
    release group → artist), iTunes' `primaryGenreName`, Last.fm's track tags,
    TheAudioDB's track and Wikidata's recording (then its work). Album- and
    artist-wide sources — Bandcamp's album tags, Discogs styles, Deezer's album
    genres, Spotify's *artist* genres — are marked `level: album`/`artist` in
    the provenance and are never promoted to a track answer.
  - **RateYourMusic** is asked **through MusicBrainz first**: the release
    group's (or the artist's) `url-rels` already carry the RYM page — `type:
    other databases` — so the Links button resolves
    `…/release/album/nirvana/mtv_unplugged_in_new_york/` and
    `…/artist/nirvana` with no scrape at all. Scraping is the fallback for
    what MusicBrainz does not state: browser-like headers (RYM sits behind
    Cloudflare), **1 request per second under a lock**, and a **30-day on-disk
    cache** (`<music>/.mlo/data/rym_cache`) so repeat work is paid for once.
    The optional `rym_cookie` setting carries your own Cloudflare cookie;
    without it **one** probe is made per run — not a slug-by-slug walk — and
    the note says so (`blocked by Cloudflare; set rym_cookie in Settings, or
    MusicBrainz links are used`), with the whole lookup capped at 20 s so an
    import can never hang on it. A datacenter IP is blocked regardless, and
    charts are *not* scraped — nothing in the app consumes them.
  - **Provenance, not guesses.** Every ask is throttled per host and cached for
    30 days, and the response says who answered: per-track `sources` and
    `levels`, per-source counts, and `notes` naming every source that stayed
    silent. RateYourMusic behind Cloudflare without a working `rym_cookie`, a
    missing Discogs token, a missing Last.fm key, or any provider without an
    answer is simply **no data** — no genre is ever invented.
  - `soulseek` is not in the default list (peers advertise folders and file
    names, not genres); a saved source list naming it stays a documented no-op
    so older configs keep working.
- **Graded.** *Mood tag present*, *Energy tag present* and *Genre tag
  present* are per-track checks (on by default, `MOOD_MISSING` /
  `ENERGY_MISSING` / `GENRE_MISSING`), so a library that never ran script 8
  fails them until it does — which is the point: no track ships without a
  mood.

## Import (new in 2.4.0)

The import paths share one pipeline now (`server/imports.py`):

- **No button is a dead click.** Every action in the wizard — the two genre
  buttons, the advisory fetch, the artist/album metadata fetch, lyrics,
  format/cue/image/optimize/DR/key&BPM/beets steps and *run all scripts* —
  shows a progress bar: a real count where the work counts steps (the
  metadata fetch `1/3…3/3`), the websocket relay's own `done/total` for the
  long script runs — during *Run All* the label reads
  `#step/total · script name` (e.g. `#4/18 · Format lyrics`), the bar carries
  that step's own fraction, the readout beside it prints the WHOLE step count
  (`4/18`, never a spliced `3.9/18`: the fraction belongs to the bar, the
  number to the scripts finished), and a step that reports what it is doing
  adds it
  behind a dash (`#2/18 · Beets tagging — looking up on MusicBrainz`), so a
  long silent phase still says where the run is — and an
  indeterminate bar plus a ticking clock for anything that cannot count.
  Every HTTP error — including "no MusicBrainz album/release-group ID on the
  track" — is shown verbatim next to the button. *Run all scripts* ends with
  one row per chain id and its ok/error text, so a failing script is visible
  instead of silent.
- **Advisory has its own button** — one call fetches the advisory rating for
  the whole album (Apple's album route answers per album), with per-track
  outcome rows and a progress bar.
- **Artist image, artist description and album description** get the same
  treatment: a card with one row per item, its current state
  (present/missing), a fetch button and a progress bar. The same step runs
  unattended after a Soulseek auto-import, honouring
  `artist_image_enabled`/`artist_description_enabled`, skipping what is
  already there, and logging one line per item (`present — artist.jpg`,
  `not-found — no source had a description`) so the job account is complete.
  When the artist folder carries a MusicBrainz id the providers are asked
  **by that id** (TheAudioDB's `artist-mb.php`), because a name alone picks
  the wrong subject — "Nirvana" returned the 1960s UK band's album cover
  from Deezer and the Buddhist concept from Wikipedia.
- **Lyrics state is the file's own.** The wizard's track rows carry
  `lyrics_embedded`/`lyrics_lrc`/`lyrics_present` from the grader's own
  detection, so an album that arrived with embedded `LYRICS` reads as having
  them (staged albums under `.mlo/downloads` included, which the library
  tree never lists), and the editor opens the file's stored lyrics instead
  of claiming none.

1. **AcoustID matching** — with a free AcoustID application key
   (Settings → *Import*) and `fpcalc` installed (Dependencies →
   *Chromaprint*), the wizard can fingerprint an album's tracks and ask
   AcoustID which MusicBrainz recording — and therefore which release group —
   the audio actually is. It reports the match (`matched/total` tracks,
   score, release group) and feeds the existing MusicBrainz match/assign
   flow; with no key, no `fpcalc`, or no match it says why and the
   title/artist search takes over. The Soulseek auto-importer uses it as a
   *verification*: a download whose audio says another release group is
   logged as a warning instead of being silently accepted.
2. **The script chain** — every import path (wizard, downloads, Soulseek
   manual and auto) runs the same configurable chain afterwards:
   **2 CUEs → 3 FLACs → 11 videos → 1 lyrics format → 13 fetch lyrics →
   18 publish lyrics → 8 auto tagging (mood/energy/genre/advisory) → 5 images → 6 audit →
   7 DR & ReplayGain → 9 AccurateRip → 12 key & BPM → 14 beets →
   10 format all → 4 grade**. Before the chain runs, the advisory step resolves
   each track's `ITUNESADVISORY` by ISRC — every applicable source asked on
   every track (Deezer and Spotify by ISRC when its optional credentials are
   set, Apple's explicit-edition album route and its song search, Discogs'
   parental-advisory format with a token, yt-dlp's 18+ gate for a track with a
   YouTube id), merged explicit-anywhere-wins: 1 to explicit, else clean to 0,
   else 0, with MusicBrainz supplying the missing ISRCs
   (`advisory_auto_fetch`) — and the instrumental step cross-references
   `INSTRUMENTAL` the same way (LRCLIB's `instrumental`, Spotify
   audio-features `instrumentalness`, the file's own name, lyrics evidence;
   instrumental wins, else not-instrumental is 0, and a track nobody can rate
   keeps no value; `instrumental_auto_fetch`). Both cross-references report
   their provenance per track (`answers`/`evidence`) and run every name-based
   match through one shared variant guard, so an instrumental/karaoke/demo/
   cover/tribute candidate is never accepted as the track — and the
   metadata step fetches the artist image / artist description / album
   description (`metadata_auto_fetch`); all three never
   overwrite a value you already have. Settings → *Import* replaces that list
   (`import_scripts`), or turns it off entirely (`import_auto_scripts`). A
   script that fails is reported and the chain carries on — one bad script
   never costs the rest of the pipeline.

   **What you typed in the wizard survives the chain.** The chain only ever
   FILLS a tag: the import stamp and script 8 write `GENRE` only where the
   track has none, lyrics are fetched only where there are none
   (`force_lyrics` is the documented override), and beets no longer imports
   genres at all (`musicbrainz.genres: no` in the generated config) — it used
   to fetch them from MusicBrainz and write them back over the user's own
   genres on every import, since `import.write` is on. The app's
   source-ordered genre chain stays the single writer, and it fills.

   **The finished screen points at where the album IS.** The chain's beets
   tag/organize steps rename the folder to its canonical layout, so
   `finish_album` re-resolves the album's path by its MusicBrainz id when the
   folder it was handed no longer exists; the reply carries the current path
   and the wizard adopts it. *Open album* additionally prefers the album's
   `mb:` reference, which survives any later reorganization.
3. **Bulk** — drop or stage several albums and the wizard imports them as a
   queue (`import_bulk_concurrency`, default 2 at a time, adjustable 1-8),
   with per-item state and per-album/per-script results. Downloads'
   multi-select import uses the same queue. A path already inside the
   library is never moved again — it just re-runs the chain.

## Playback loudness — ReplayGain, on demand (new in 2.4.0)

- The player applies ReplayGain through the WebAudio gain stage, in
  **track**, **album** or **off** mode (`replaygain_mode`) with a preamp
  (`replaygain_preamp_db`, ±24 dB) — both editable from the player's options
  and from Settings. Album mode prefers `REPLAYGAIN_ALBUM_GAIN` and falls
  back to the track value.
- **Missing tags are measured, not guessed** — with
  `replaygain_analyze_missing` on (default), a file without ReplayGain tags
  is measured with ffmpeg's EBU R128 filter (`gain = -18 LUFS − measured
  loudness`), cached in `.mlo/data/replaygain.json` and applied; the player
  labels it *"measured on demand"*. Peak-aware clip protection reduces a
  gain that would push the track's peak past 0 dBFS.
- The player bar shows the applied gain (`RG -11.3 dB`) with a tooltip
  saying where it came from — tags, tags + clamp, or a fresh measurement.



Some albums simply aren't on Soulseek right now. A **wish** records a
MusicBrainz release identity without downloading anything, so it can be
filled in automatically later:

1. Paste a release ID or musicbrainz.org URL under Soulseek → *Wishes* (or
   use **Add to wishes** on a library artist page's bulk bar, which queues
   every album you select that carries a MusicBrainz id).
2. A background worker re-searches Soulseek for every open wish on the
   configured interval (default every 6 h), running the same
   find → verify-logs → download → audit → import pipeline as the one-shot
   auto-importer. Auto-import ranks lossless folders ahead of lossy ones:
   when no lossless candidate turns up, an interactive run parks and asks
   *Only lossy copies found — download anyway?* on the Soulseek page, while
   a wish-filling run never takes a lossy copy — it leaves the wish open
   for a lossless match.
3. When a verified copy is found the release is imported and organized, and
   the wish flips to **Imported**. Wishes whose release already exists in the
   library (e.g. a manual download) are reconciled too — the **Sync library**
   button does this on demand.
4. Each wish has **Search now**, per-wish notes and attempt tracking; the
   whole list shares one searchable activity log.

Settings → *Wishes* controls the master switch, interval, per-wish attempt
cap and auto-import.

New in 2.3.0, the end of a fruitless search: an auto-import job whose search
window closes with **no usable candidate** parks instead of failing and asks
whether to move the job to wishes. Accepting creates a wish carrying the same
search queries, so the background worker keeps looking with no further input;
declining keeps the old *no candidate folder contained every track* failure.
The prompt appears after the configured time — the search window
(`soulseek_auto_search_wait`, default 10 s) plus the 45 s grace tail, about a
minute by default — and there is deliberately no second timer.
`soulseek_auto_wish_prompt` (default on) switches the prompt off; searches the
wishes worker itself started never prompt.

Completion is notified by toast: an auto-import job reaching a terminal state
(including *added to wishes*), and a wish flipping to **Imported** or
**Failed**.

## Grading — what the checks cover

Every check is toggleable on the Grading page, which also offers
**Strict / Balanced / Relaxed** presets, a live check filter, and
enable-all / disable-all bulk actions.

- **Tracks & albums** — unreadable files, required per-track and album-level
  tags, encoder identity tags, naming-script match, path capitalization,
  lowercase extensions, INITIALKEY + BPM, excess tags, media/source tags and
  their consistency, instrumental/lyrics consistency, disallowed file types,
  stray images, un-remuxed videos, disc folder naming, CD requirements
  (.log exact match, .cue, FLAC lossless, CRC checksums — per disc).
- **Identity tags** (new in 2.4.0) — the required-tag sweep now covers TITLE,
  ARTIST, ALBUM, ALBUMARTIST, DATE and TRACKNUMBER, plus DISCNUMBER on albums
  that really have several discs (a `D-TT` filename, or DISCTOTAL/TOTALDISCS
  above 1). A missing identity tag fails that track by name, whether or not
  the naming check is enabled.
- **Excess tags are Picard-safe** (new in 2.4.0) — what counts as an
  unexpected tag comes from one shared vocabulary: the app's own tags
  (`ENERGY` included), the encoder markers, beets' `mediafile` spellings and
  **Picard's standard output** (its `TXXX:` / MP4-freeform spellings of the
  release metadata, the per-track credit tags, the `MUSICBRAINZ_*` ids, and
  enumerated non-text ID3 frames such as `PRIV:owner`). The Optimize/Format
  All strip pass imports that same predicate, so a strip can never leave a tag
  the grader flags, or delete one it requires.
- **A check that throws fails, loudly** (new in 2.4.0) — an internal error is
  reported as *could not be evaluated*, counted and failed instead of dropping
  the check from the total, so the percentage always covers every enabled
  check.
- **Mood, energy & genre** (new in 2.4.0) — every track must carry `MOOD`,
  `ENERGY` and `GENRE` (`MOOD_MISSING` / `ENERGY_MISSING` / `GENRE_MISSING`,
  each with its own toggle); script 8 writes all three, so the fix for a
  failure is one click on the Optimization page.
- **Descriptions & artwork** (new in 2.4.0) — the album folder must hold a
  `description.txt` (album page → *Fetch description*), and the artist
  folder its own `artist.jpg`/`artist.png` + `description.txt`. The
  `description.txt` sidecar is an allowed file category
  (`grade_include_description`), so storing one never trips the stray-file
  checks, and the layout report was taught the same exception.
- **Opt-in checks** (new in 2.4.0) — ReplayGain tags are graded only when a
  file already carries at least one of the four `REPLAYGAIN_*` tags (then
  all four are required) and AcoustID tags only when a file already carries
  `ACOUSTID_ID` or `ACOUSTID_FINGERPRINT` (then both). A library that never
  ran script 7 or fingerprinted anything is never failed for their absence;
  a half-written set is.
- **Auditing** — AUDIT tag presence, log checksum validity, AccurateRip
  verification, log grade within a configurable threshold. A CD's `.log` must
  be *usable* (a real log with content) — an empty or truncated file no longer
  counts as one.
- **Identity links** — the MusicBrainz release (or release group) and the
  RateYourMusic release page must be tagged. Artist/recording-level links
  remain optional.
- **Covers** — presence, size, and squareness: the check is an **aspect
  ratio** test (issue text says "not square", tolerances configurable, gated by
  `grade_check_cover_crop`), applied to the album cover and to per-track
  sidecar covers under the same rules.
- **Strict formatting** — tag padding/blank lines, lyrics canonical form,
  CUE canonical form.
- **Lyrics** — presence, canonical form, and transform tags (TRANSLATION-* /
  TRANSLITERATION-*) carrying their language when a track stores any.
- **File categories** — which file types participate in grading at all
  (music, covers, CUE, log, LRC, accurip, videos, descriptions, other).

### Artist grading (new in 2.4.0)

Artists are graded on exactly what applies to an artist folder — the image
and the description — and nothing else: `grade_artist()` returns its own
check count, percentage and issue list, which the artist page shows as a
badge beside the album aggregate. An artist whose albums all pass but that
has no image still fails its own grade, and vice versa. Both checks are
individually toggleable (Settings → *Grading*, group *Artist*).

### AudioAuditor override

A track's audit verdict can be forced from the track page: **REAL** / **FAKE**
writes the `AUDIOAUDITOR_OVERRIDE` tag, while *Auto* clears it and hands the
track back to AudioAuditor. The override wins over every derived verdict — it
is applied last, so the album-level all-real gate agrees with it, and a forced
re-audit reproduces the user's call instead of erasing it.

### CD rips: verified integrity outranks the spectrogram detectors

A CD rip is graded on its own evidence, in this order:

1. **Its rip log's CRCs.** Script 6 decodes each track and compares it against
   the `Copy CRC` lines of the album's `.log`. A match is `AUDIT=REAL`, written
   immediately — it does not wait for AudioAuditor, which is a Windows-only
   tool a Docker or Linux install does not even have. A track whose CRC just
   matched is also exempt from every *log-file* gate — a log with no
   verifiable EAC SHA256 (older EAC, a log edited after the rip), a log the
   tool cannot score, or one below `audit_log_score_threshold`: those measure
   the log's documentation, not the audio, and they no longer turn a proven
   track `FAKE`. (That exemption is about the AUDIT verdict. The *grade* has
   its own two checksum checks — see below — which the checksums must satisfy
   on their own terms, and which the settings can switch off for a collection
   whose logs predate EAC checksums.)
2. **Its `.accurip`.** A REAL AccurateRip verdict stands on its own: a disc
   whose rip matches the database passes even when its log's EAC checksum is
   unverifiable (XLD, older EAC, a log the tool cannot score).
3. **AudioAuditor, only when neither applies.** Its fake-lossless / MQA /
   clipping detectors are *spectral* evidence: on a provably intact CD rip a
   disagreement is recorded as a **warning** (clipping flags survive), never as
   a `FAKE` verdict, and a missing `.accurip` no longer fails a rip whose log
   verifies. `audit_cd_require_both` (Settings → Audit) decides whether
   AudioAuditor is run over `MEDIA=CD` at all; it can no longer downgrade a
   verified disc.

Grading follows the same rule: with `grade_check_audit` on, a CD track whose
log checksum or `.accurip` verifies satisfies the AUDIT requirement even when
the stored tag still says `FAKE` from an earlier run, and the album's live
audit readout reports `REAL` (the track's `audit_verified` names which source
proved it). **The live readout applies with the check off too**: the library,
album and track pages show the verdict derived from the rip's own evidence, so
a provably intact disc never renders red off a stale tag written by an older
run.

### The rip's checksums are graded, not merely present (new in 2.6.9)

Two checks make a rip's own numbers cost it the grade — independently of
`grade_check_audit`, so a bad log cannot pass just because the AUDIT tag is
not required:

* **`grade_check_crc` — the CRC values.** Every track must be covered by a
  per-track CRC in its own disc's `.log` (issue `CRC`), **and** that CRC must
  equal the CRC-32 of the track's decoded PCM (issue `CRC_MISMATCH`). Coverage
  alone let a log from a different rip, or audio edited after the rip, grade
  PASS. Lossy encodes can never reproduce the uncompressed WAV CRC and are
  judged on coverage alone; undecodable files likewise fall back to it.
  Decoding is the cost of the proof, so the CRC of a file is memoized per
  `(path, size, mtime)` — the audit pass and the grader share one decode.
* **`grade_check_log_checksum` — the log's EAC SHA256.** A log whose stored
  checksum does not verify, or that states none while
  `audit_verify_log_checksum` requires one, fails the album (issue
  `LOG_CHECKSUM`). XLD logs — and older EAC logs that carry no checksum
  concept — pass: nothing claimed, nothing refuted. `AUDIOAUDITOR_OVERRIDE=REAL`
  still wins, because the override is applied before this check reads the
  per-track verdicts.

## Library layout — a read-only report

`GET /api/library/layout`, surfaced on the **Optimization** page, walks the
whole music folder — root, `Artists/`, every artist folder, every album
folder — and reports where the canonical
`<music>/Artists/<Artist>/<Album>/<files>` shape is not met: misplaced audio,
unexpected folders and subfolders, empty album folders, stray files (including
a file sitting directly in `Artists/`), hidden folders, and leftovers from the
old `.mlo_data` layout.

The scan never moves, renames or deletes anything; acting on the report is
what **Organize** and the scripts are for.

- **A completely empty folder is a grading failure, not a footnote.** A folder
  with no files anywhere beneath it can never become an album (albums are
  derived from audio paths), so grading used to *skip* it silently; the
  `grade_check_empty_folders` check (on by default) now walks the library and
  reports every such folder as an `Empty folder` problem of its own, so an
  empty artist/disc folder shows up in the counts instead of hiding. Toggle it
  on the Grading page with the other checks.
- **The scripts clean up after themselves.** At the end of every chain run
  (`/api/run`, an import, the bulk queue, the Soulseek importer) the folders a
  step may have emptied are pruned bottom-up: a disc folder or album folder
  with nothing left in it is removed, never a folder holding anything, never
  the music root, and the run reports how many were removed. Organize does the
  same for the folders it moves files out of.

New in 2.3.0 it also reports **`wrong_case`** — an artist folder, album folder
or file name whose stored capitalization differs from the naming script's
expectation. The comparison is case-sensitive, which works because
`os.listdir` returns the stored casing even on Windows' case-insensitive
filesystem. Each row hints at running Organize; the scanner itself never
renames.

## Cover finder

The album/track online cover search is a meta-search over the musichoarders
providers, and it falls back to **Cover Art Archive → Deezer → iTunes** when
they have nothing — every result row says which provider answered. The modal
also shows a **MusicBrainz reference**: when the album's release group has
Cover Art Archive art, its front cover appears as a small labelled thumbnail
(with a hover enlargement) so a candidate can be compared against it — and
nothing at all when there is no release-group id or the archive has no image.
Results
carry the image's **real pixel dimensions** (`width`/`height`, probed from the
file's own header bytes, `null` when unknown — never a guess) and a storefront
**region**. The finder's source list and region pickers are **per-search**,
change nothing in Settings, and only its *Save as default* button writes
`cover_sources` + `cover_country`; the next search (and the wizard) then starts
from those choices. `GET /api/cover/search` runs the search against the
catalogue, `GET /api/cover/sources` lists the selectable sources, the regions
and the saved defaults, and `POST /api/cover/fromurl` saves a chosen result to
disk.

## Downloads — the sidebar page, and Soulseek's own tabs (new in 2.7.1)

**Downloads** is a sidebar page again (`/downloads`), and it is the *offline
cache*: the tracks this browser can play with the server down, laid out as the
library's album table (album rows with covers, expandable tracklists, the same
Columns menu and drag-resizable widths), with play and "remove from cache" per
track plus a two-step *Clear all*. It renders the very same panel the Soulseek
page shows as its **Cached tracks** tab, so the two can never disagree, and the
total size comes from the same cache the player reads.

Downloading stays where the music is — the album, artist, playlist and player
bar controls all mean "cache this for offline playback". Every track title in
the app (library rows and tracklist, album page, favourites, downloads) carries
a small green check while its audio is in that cache and **nothing at all**
when it is not, so the mark only ever means *plays without the server*. The
album and playlist action rows are one shared 36px square recipe — play,
download, links, like, tags and the overflow menu differ only in glyph and
accent — and the square download button states everything through that glyph:
a determinate progress arc fills as it caches, the check scales in when the
entity is complete, and a removal arms the same box red and pulsing (second
click confirms, outside click or Escape disarms) instead of growing a label.
The state is one shared query, so a download started in the player bar marks
the title rows immediately.

Saving a file to disk is *Export*'s job and the staging folder is Soulseek's —
neither is this page.

**The app itself works offline.** The service worker precaches the built shell
— the document plus every bundle, with the lazy route chunks listed by the
build (`web/dist/precache.json`, emitted by a tiny Vite plugin) because
index.html never references them — and then caches the payloads the offline UI
renders from: `/api/library`, `/api/config` and the `/api/album` /
`/api/artist` bodies, all network-first with the cache as the fallback. A
download warms the album and artist payloads for its own entity (that is where
the description, the credits and the cover references live) and removes them
when the last cached track of that entity goes. Measured with the backend
stopped: the sidebar, the Downloads page and a downloaded album page all
render, and playback of a cached FLAC advances normally.

Releases downloaded to the staging folder `<music>/.mlo/downloads` are listed
by `GET /api/downloads` (newest first); `POST /api/downloads/import` moves
entries into the library as albums and `POST /api/downloads/delete` removes
them. The *Downloads* tab shows the same entries as transfers bucketed into
active, queued, completed and failed.

That tab also manages **what is actually on disk** in both staging folders -
`GET /api/soulseek/staging` reports slskd's download dir (default
`<music>/.mlo/downloads`, or `soulseek_download_dir` when set) and its sibling
`incomplete` dir with per-entry size, file count and the `partial`/`album`
flags, and `POST /api/soulseek/staging/delete` / `.../clear` remove one entry
or empty a whole root (name guard: basename only, nothing that resolves
outside the root; the roots themselves are never removed, and an entry that
cannot be deleted is reported instead of aborting the rest). This is the half
of the picture transfer-level clearing cannot see: a rejected candidate that
left an empty folder chain, or partial bytes whose transfer record is already
gone, has no row in the transfer list at all.

The **Cached tracks** tab is the other half of "downloaded": the tracks held
in the browser's offline cache — the copy the player keeps on this device so
an album plays without the server or the network. It is laid out like the
library viewer (album rows with covers, expandable tracklists, the same
Columns menu and drag-resizable widths), groups by album, and offers play and
"remove from cache" per track plus a two-step *Clear all*. The total size is
read from the same cache the player uses. The *Downloads* page in the sidebar
is this exact panel with a page header, for when you are managing the offline
copy rather than fetching something.

## Getting started

```bash
# backend
python -m pip install -r server/requirements.txt
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000

# frontend (dev, http://localhost:5173 proxies /api to :8000)
cd web && npm install && npm run dev
```

Production build is served by the backend automatically (`web/dist`):

```bash
cd web && npm run build
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

The music folder is chosen at startup, never from the UI: point
`MLO_MUSIC_FOLDER` at your library (that is what the Docker image and the
compose template do), or set `music_folder` in the app's own
`<music>/.mlo/data/config.json`. There is no music-folder picker — Settings
shows the folder it resolved and the raw config is editable in the app (the
import wizard's own "pick the folder to import" dialog is a different thing).
Everything else (playlists, favourites, the beets library, the Soulseek
config) lives in the same `.mlo` folder.

Install the external toolchain from Settings → Dependencies (ffmpeg, flac,
libjxl, oxipng, rsgain, AudioAuditor, Logchecker, CUETools, librosa, beets,
slskd, yt-dlp — and optionally chromaprint/`fpcalc` for AcoustID matching).
The UI walks you through the first-run setup.

### Sources & setup

Every provider the app can ask — the six lyrics sources, the six advisory
routes, the eleven genre sources, the four metadata providers and the
RateYourMusic link source, 28 rows — lives behind `GET /api/sources/health`
(with `?probe=1` to run one cheap live lookup per configured source,
`kind=lyrics|advisory|genre|metadata|links` to filter, and
`GET /api/sources/health/{id}` for a single row). Each row says what it needs,
whether that is configured, and — when probed — what actually answered.

The wizard's step 3 **Sources** and the Settings → *Sources* panel are the same
panel: every row has a **Test** button, and the keyed providers collect their
free credentials right there — Spotify client ID/secret, Discogs token,
Last.fm API key and the RateYourMusic cookie. Nothing blocks finishing setup: a
provider without its key is skipped like any other unavailable source, and
`needs` is what says which key is missing.

Wizard step 4 **AI & RYM** is where the two optional integrations are set up,
and it stays re-runnable from Settings → General (*Run the setup wizard
again*):

- **AI lyric transforms** (Settings → *AI*) — base URL, API key, model and
  reasoning effort for any OpenAI-compatible `/chat/completions` endpoint
  (OpenAI, OpenRouter, LM Studio, llama.cpp, or Google Gemini's
  OpenAI-compatible endpoint: pasting the bare `generativelanguage.googleapis.com`
  host is routed for you), plus the translation languages and the
  transliterate/translate switches. **Test connection** sends one tiny prompt
  and shows the provider's own answer — or its own error — before anything is
  saved. Script 17 is the only thing that needs it.
- **RateYourMusic links** — the same link row the Sources panel shows, with the
  cookie hint (dev tools → Network → any rym request → Cookie) and a Test that
  really resolves an album + artist pair, so "is my cookie good?" has an
  answer. *Look the links up automatically during imports* (`rym_links_auto`)
  sits beside it. MusicBrainz states the RYM page for well-known releases with
  no cookie at all; RYM is only scraped for the rest.

The keyless sources (LRCLIB, the CJK lyrics APIs, MusicBrainz, ListenBrainz,
iTunes, TheAudioDB, Wikidata, Bandcamp, Deezer, Cover Art Archive, Apple's
routes) work as-is; yt-dlp is the only *installed* requirement, for YouTube
captions and the 18+ advisory gate.

### Getting a RateYourMusic cookie

RYM has no API and answers an automated client with a Cloudflare challenge, so
the app borrows a signed-in browser session. It is needed only for the *scrape*
fallback — MusicBrainz states the RYM album/artist page as a URL relation for
well-known releases, which needs no cookie at all.

1. Sign in to rateyourmusic.com in your browser.
2. Press `F12` → **Network** → reload the page.
3. Click any request to `rateyourmusic.com` → **Headers** → **Request Headers**.
4. Copy everything after `Cookie:` (in Firefox: right-click the request → *Copy*
   → *Copy Request Headers*, then take the `Cookie` line).
5. Paste it into **Settings → Discovery → RateYourMusic cookie**, or the wizard's
   *AI & RYM* step, and press **Save & test**.

The paste is normalised on the way in: the `Cookie:` label, wrapped lines and
stray whitespace are all handled, so the whole copied value works. The value is
a session credential — it is stored in your local config only, never sent
anywhere but rateyourmusic.com, and you should not share it. Signing out or
clearing cookies invalidates it; when RYM starts refusing, paste a fresh one.
`GET /api/rym/validate` (the link editor's Valid/Invalid check) and the Sources
panel's **Test** (one live album+artist resolve) both say whether the cookie
works right now.

Without a cookie the source is `skipped` — never an error — and imports simply
leave the links for you to paste by hand.

A refusal is never permanent. RYM blocking this network (or a Cloudflare
challenge) makes the app stop asking for a **five-minute window** — it does not
hammer a site that just refused it — and the refusal is remembered together
with the exact cookie it was recorded against. Paste a new cookie (or press
**Test** / **Save & test**, which clears the refusal first) and the very next
request is real: no backend restart, which is what an older build required.
Transient answers (429, 5xx, a timeout) are retried before a source is counted
as unreachable, and a fetched RYM page must state the album/artist that was
asked for before any genre from it is accepted. The shipped genre order asks
RateYourMusic first, then MusicBrainz, matching the documented chain.

### Dependencies: latest versions & updates

`GET /api/dependencies` reports, for every tool the installer knows, three
versions — **Installed** (on disk / on PATH), **Latest** (the platform's
target, `latest_version`: *the version this app would fetch*) and
**Available** (`upstream_version`: the newest release GitHub actually has, for
the tools published there). Settings → Dependencies, the sidebar page and the
setup wizard all show the same three columns, and `python -m mlo`'s
Dependency Manager prints the same table in the terminal.

Latest is a deliberate *pin* (see `PINNED`): the installer downloads reviewed
releases so installs and CI builds are reproducible, and a newer upstream tag
is a release decision, not a runtime one. Available is the honest answer to
"is there something newer?", checked against GitHub's `releases/latest` for
every GitHub-published tool. php (windows.php.net), simple-dr-meter (a tag
archive) and the two PyPI packages have no release to ask about; their rows
report no Available version.

The check is cached for 30 minutes and always runs **in the background**: a
request returns what is known plus `checking: true` while the fetch is in
flight (`upstream_checked_at` says when the last pass finished, and it is
polled by the Dependencies page). `?refresh=1` re-checks now instead of
waiting out the TTL. A network or API failure never breaks the response — the
affected tool keeps its previous value (or `null`) and carries a `note`; the
row's state becomes `error` only when nothing is known at all.

`state` is derived from Available, not from the pin: `ok` (installed ==
upstream), `update` (upstream known and different — the row's hover shows the
upstream version), `missing`, `error` (that tool's check failed). Rows with no
upstream at all fall back to the pinned pair.

Updates are one click (*Install / update all*, or per tool) and land in
`<app>/.dependencies`. In Docker that is the `lamusica-dependencies` volume,
so updates survive a container rebuild; pip-based tools (beets, librosa)
install into the same folder at runtime, and distro-provided tools (ffmpeg,
flac, libjxl, …) are reported ready from the image's own packages. Only the
Windows-only tools (AudioAuditor, CUETools, Logchecker+php, slskd) cannot be
fetched on Linux — their rows say so instead of failing an install.

**Install always fetches the pinned Latest**, never Available: an upstream tag
that has not been reviewed is exactly what the pin exists to keep out. With
`dependencies_auto_update` (Settings → Dependencies, off by default) a
background pass every few hours installs every tool whose state is `missing`
or `update` through the same pinned path and logs one line per tool; the flag
is re-read on every pass, so switching it off stops the next one.

### Terminal entry point

```bash
python -m mlo
```

That is the classic console menu (scripts 1–17, Run All, config editor), and
it is **not** stdlib-only: `mlo` imports `mutagen` for every tag operation, so
run it from the same environment that has `server/requirements.txt` installed
(script 14 additionally needs `server/beetscfg` plus a vendored beets, and
scripts 9/10 their optional modules). A missing module makes its script
unavailable and fail loudly instead of reporting a clean "0 processed" run.

## Docker

```bash
docker compose up -d --build     # build from source and start
docker compose logs -f           # follow the backend log
docker compose pull              # fetch the prebuilt GHCR image instead
docker compose down              # stop and remove
# open http://localhost:8000 — mount your music at /music
```

`docker-compose.yml` is the copy-paste template: it mounts your library at
`/music` (the same path `MLO_MUSIC_FOLDER` names inside the container), keeps
app state in `/music/.mlo`, and pins `ghcr.io/dillydalli3r/la-musica:latest`
so `docker compose pull` and watchtower have something to compare against.

- **Unprivileged.** The image runs as `mlo`, uid/gid **1000**, with
  `HOME=/home/mlo` (numba/librosa caches would otherwise try to write to `/`).
  Your bind-mounted music folder must be writable by uid 1000 — a host folder
  owned by another user shows up as "cannot write `/music/.mlo`". chown it, or
  rebuild the image with a matching uid.
- **Volumes.** `/music` holds the library *and* all app state
  (`/music/.mlo/data`, `…/downloads`, `…/trash`). `/app/.dependencies` is a
  named volume for tools installed at runtime from Settings → Dependencies, so
  replacing the image does not throw them away.
- **Healthcheck.** The image probes `http://127.0.0.1:8000/api/health` with its
  own Python (`python -c "import urllib.request…"`, `2>/dev/null`) — the slim
  base has no curl. 30 s interval, 5 s timeout, 30 s start period, 3 retries;
  compose declares the same check so `docker compose ps` and watchtower see
  readiness.
- **Toolchain in the image.** `ffmpeg`, `flac`, `libjxl`, `jpegtran`
  (`libjpeg-progs`) and `libchromaprint-tools` (`fpcalc`) are installed from
  apt and a failure there fails the build — these are the Linux counterparts
  of `mlo/fetchdeps.py`'s Windows downloads, which the in-app installer refuses
  to fetch on Linux and points at the distro package instead. `oxipng` and
  `rsgain` are best-effort: Debian bookworm does not ship them, the image
  installs them inside a `|| true` sub-shell, and the pipeline degrades
  without them.
- **yt-dlp** is not baked in: Settings → Dependencies installs it with
  `pip --target` into `/app/.dependencies`, exactly like beets and librosa. A
  pinned copy in the image would only save that first-run download — the
  YouTube acquisition works the same either way.
- **Windows-only tools.** `slskd`, `CUETools`, `AudioAuditor` and
  `Logchecker`+`php` have no Linux build; the installer reports them as
  unsupported inside the container, so AccurateRip generation, the logchecker
  grade, the AudioAuditor audit and the managed Soulseek daemon are not
  available in Docker.
- **Automatic updates (optional).** Uncomment the `watchtower` service at the
  bottom of `docker-compose.yml` and set `WATCHTOWER_LABEL_ENABLE=true`: the
  label on the la musica service (`com.centurylinklabs.watchtower.enable=true`)
  plus that flag means watchtower touches this container and nothing else on
  the host. It needs the `image:` line, and the example ships
  `WATCHTOWER_CLEANUP=true` and a daily `WATCHTOWER_SCHEDULE`.

## Architecture

```
web/         React 19 + TypeScript + Tailwind UI (Vite, service-worker media cache)
server/      FastAPI backend: library payload, playlists, integrations,
             import (AcoustID + the shared script chain + bulk queue),
             discovery (Deezer/ListenBrainz/iTunes/TheAudioDB/Wikipedia +
             MusicBrainz), artist artwork and descriptions, streaming
             (direct + on-the-fly transcode), export, organize,
             WebSocket progress
mlo/         core engine (imports mutagen for every tag operation — the CLI
             needs the same environment as the server, it is NOT stdlib-only):
             grader, audit, flac, images, lyrics (deterministic
             word-sync + the multi-provider lyrics chain), moods (incl.
             ENERGY), artistdata, acoustid, cue, accurip,
             loudness (batch rsgain + on-demand EBU R128), autotag, remux
             (chapters + text-subtitle filtering), fetchdeps (external
             toolchain installer), naming, discs, stats
desktop/     Tauri v2 desktop shell
tools/       test-library generator and test suites
```

Where the app stores what it fetches:

| File | What it holds |
| --- | --- |
| `<music>/.mlo/data/config.json` | all settings |
| `<music>/.mlo/data/artwork.json` | provenance for artist images and album/artist descriptions (provider, source URL, fetch time) |
| `<music>/.mlo/data/replaygain.json` | on-demand ReplayGain measurements (invalidated on size/mtime change) |
| `<music>/.mlo/data/metadata_review.json` | artist/album metadata candidates staged by `metadata_review` until you apply one |
| `<music>/.mlo/data/rym_cache/` | RateYourMusic genre pages, cached for 30 days (1 request/second) |
| `<music>/.mlo/data/wishes.db` | the wishlist |
| `Artists/<Artist>/artist.jpg` | the artist image (normalized to the configured cover aspect + JPEG quality) |
| `Artists/<Artist>/description.txt` | the artist description |
| `<album>/description.txt` | the album description |
## API overview (selected)

| Endpoint | Purpose |
| --- | --- |
| `GET /api/library` | tag-rich library tree (grades, audits, tags, tech info; gzipped) |
| `GET /api/library/layout` | read-only layout scan: misplaced audio, unexpected folders, empty albums, stray files, hidden folders, `wrong_case` |
| `GET /api/home` | Home page: stats plus the library-only shelves (recent, top-rated, favorites, discover, top artists, wanted, needs attention) |
| `GET/POST/PATCH/DELETE /api/wishes` | release wishlist CRUD; `POST …/{id}/search`, `…/search-all`, `…/reconcile` |
| `GET /api/album` `GET /api/artist` | entity details |
| `GET /api/stream` `GET /api/videos/stream` | audio/video streaming (Range; `?transcode=1` pipes fragmented MP4) |
| `GET /api/videos/meta` | codec probe deciding direct play vs transcode (carries the video's `duration`) |
| `GET /api/videos/thumb?path=…&t=…&w=…` | one JPEG frame of a library video at `t` seconds for the scrub preview (keyframe seek before `-i`, cached under `.mlo/data/thumbs/`, uncached frames take ~50–150 ms) |
| `GET /api/tags` | per-track tag/lyrics/cover read view |
| `POST /api/tags/bulk` `POST /api/videos/tag` | bulk tag surgery; music-video tag writes |
| `POST /api/lyrics/embed` `POST /api/lyrics/write` | embedded LYRICS / .lrc sidecar writes |
| `POST /api/run` | run any of scripts 1–17 on targets |
| `POST /api/organize` | apply the naming script (dry-run supported) |
| `POST /api/export` | multi-format export with codec/bitrate config |
| `GET/POST /api/playlists…` | manual + smart playlists, .m3u8 |
| `GET /api/mb/release?mbid=…` `GET /api/mb/release-genres?mbid=…` | MusicBrainz release + genre cascade |
| `POST /api/mb/match` `POST /api/mb/assign` | track/disc matching, MB/RYM/genre/advisory writes (ITUNESADVISORY is accepted only as 0/1/2 or empty) |
| `POST /api/mb/auto-import` | enqueue a release / release group / artist for download: `mode=best` (one edition per group) or `all` (every eligible edition). Returns in about a second — `{queued, items: [{mbid, title, status}], skipped: [{mbid, reason}]}` — with `status` `queued` (waiting behind a running job), `running` (started at once) or `queued (resolving)` when MusicBrainz did not answer inside the inline budget and the job resolves the ID itself; nothing to queue is `queued: 0` plus a `skipped` reason, never a 404 |
| `POST /api/mb/advisory/fetch` | resolve `ITUNESADVISORY` for tracks (or a release): every applicable source is asked on every track (Deezer and Spotify by ISRC, Apple's explicit-edition album route, Apple's song search, Discogs' parental-advisory format when a token is set, yt-dlp's `age_limit` for a track with a YouTube id) and merged — explicit anywhere is 1, else clean is 0, else 0 — returning `sources` (who stated each path's value) and `answers` (what every source said, `{path: {source: 0\|1}}`) |
| `POST /api/instrumental/fetch` | resolve and write `INSTRUMENTAL` (0/1) cross-referencing LRCLIB's `instrumental`, Spotify audio-features `instrumentalness` (when configured), the file's own name and lyrics evidence: an "instrumental" answer anywhere is 1, else a "not instrumental" answer is 0, else nothing is written; every name-based match passes the shared variant guard (an instrumental/karaoke/cover/tribute hit is never accepted as the track); returns `values` and `evidence` (`{path: {source: 0\|1}}`) |
| `POST /api/genres/import` `GET /api/genres/facets` | import genres for paths from the named `sources` (default: MusicBrainz + RateYourMusic; per-track answers with `level` fallbacks, per-source counts + notes); facet list with category cards for the Genres page. The wizard calls it once per source, one button each |
| `GET /api/metadata/candidates` `POST /api/metadata/apply` | artist image / artist description / album description candidates (staged when `metadata_review` is on) and the write of the chosen one |
| `POST /api/videos/download-youtube` `POST /api/videos/match` | download a music video from YouTube for an artist+title (best candidate by duration); assign downloaded video files to tracks |
| `POST /api/soulseek/download-bulk` `…/download-user` `…/search/cancel` | queue the selected search files, take everything a user shares through a fresh browse (already-queued transfers skipped), or cancel a running search |
| `GET /api/discovery/sources` | the provider catalogue behind the artist-image / description order pickers (Settings → *Artist images & descriptions*) |
| `GET /api/sources/health` `GET /api/sources/health/{id}` | every external source (lyrics, advisory, genre, metadata, links) with its `needs`/`configured` state; `probe=1` runs one cheap lookup per configured source against a fixed sample (`kind=` filters, the single-source route returns the bare row) |
| `GET /api/artist/artwork` | stored artist image + description + provenance + the artist's own grade |
| `GET /api/artist/image` `…/candidates` `POST /api/artist/image` `…/upload` `DELETE …` | serve / list candidates / save a chosen or automatic image / upload / remove |
| `POST /api/artist/description` `DELETE …` | store (fetched or supplied) / remove the artist description |
| `POST /api/album/description` `DELETE …` | store (fetched or supplied) / remove the album's `description.txt` |
| `GET /api/lyrics/providers` | lyrics sources, the saved order and the plain-lyrics policy |
| `POST /api/lyrics/auto` | auto-import lyrics for tracks through the provider chain (writes like script 13) |
| `GET /api/lyrics/find` | look lyrics up in the chain without writing anything |
| `GET /api/lyrics/*` `POST /api/lyrics/write` | lyrics proxy + LRC sidecar write |
| `POST /api/cover` | album cover upload (`?track=` writes per-track sidecar covers) |
| `GET /api/cover/search` `GET /api/cover/sources` | cover meta-search (each row: real `width`/`height`, its provider, and the fallback chain Cover Art Archive → Deezer → iTunes) and the selectable catalogue / saved defaults |
| `POST /api/cover/fromurl` | save a chosen search result as the cover |
| `GET /api/downloads` `POST /api/downloads/import` `POST /api/downloads/delete` | staged `.mlo/downloads` entries: list newest-first, import as albums, delete |
| `POST /api/import/upload` `…/commit` | upload + link assignment |
| `POST /api/import/acoustid` | fingerprint an album's tracks and return the release group the audio actually is |
| `POST /api/import/finish` | run the configured import script chain over already-imported albums |
| `POST /api/import/bulk` `GET /api/import/bulk/status` | bulk import queue: start a multi-album import, poll its per-item progress |
| `POST /api/import/scripts/preview` | exactly which scripts will run after an import |
| `POST /api/lyrics/wordsync` | deterministic line→word/syllable ELRC for a track's stored lyrics |
| `POST /api/ai/test` | one tiny round trip to the configured AI endpoint (settings/wizard overrides allowed) — a refused provider is a `{ok:false, error}` payload, never a 500 |
| `WS /ws/progress` | live progress |

## Tests

```bash
python tools/make_test_library.py   # synthetic library for end-to-end runs
python tools/test_remux.py          # video remux suite (VOB/MKV/AVI/WebM fixtures)
python tools/test_lyrics_xlit.py    # script 17: line alignment, the romanization
                                    # rules, and the on-disk cache (offline)
python tools/test_script_menus.py   # every script menu agrees (numbers, labels,
                                    # Run All order, force switches) — the gate
                                    # for adding a script anywhere
python tools/smoke_api.py           # route smoke test against a running backend
                                    # (python tools/smoke_api.py http://127.0.0.1:8000)
```

The browser-side check (`tools/check_menus.cjs` — every sidebar entry and
route renders with no page errors) needs a running backend and Playwright
(`npm i -D playwright`).

The other `tools/test_*.py` suites cover config migration, CUE disc renaming,
grading paths (mood/energy/genre, the identity-tag sweep, the Picard-safe
excess allowlist, the release-type / multi-country naming rules, the opt-in
ReplayGain/AcoustID checks and album descriptions), artist grading
(`test_artist_grading.py`), the artist/album artwork store
(`test_artistdata.py`), mood classification (`test_moods.py`), the lyrics
provider chain (`test_lyrics_providers.py`), AcoustID fingerprint parsing and
quorum (`test_acoustid.py`), on-demand ReplayGain (`test_replaygain.py`), the
import pipeline and bulk queue (`test_import_pipeline.py`), YouTube
acquisition + the remux/subtitle pipeline (`test_video_pipeline.py`), the
platform guards that keep the Windows-only downloads off Linux
(`test_platform_guards.py`), Home shelves, lyrics merge/repair, the Soulseek
client and the layout scanner's capitalization reporting
(`test_layout_case.py`). Most of them run offline by design — network
providers are stubbed, and the tests that need a toolchain (ffmpeg, fpcalc)
skip that section when it is not installed.
Run them all before a release. The frontend gate is `cd web && npx tsc -b &&
npx oxlint && npm run build`. CI (`.github/workflows/ci.yml`) runs the Python
suites, that frontend gate and `cargo check` for the Tauri shell on every push
and pull request; the suites that
need a live backend (the browser check and `smoke_api.py`) are manual.

---

Legacy v1 (Tkinter app, CLI, PyInstaller/Inno packaging) is archived on the
`archive/legacy-v1.7` branch.
