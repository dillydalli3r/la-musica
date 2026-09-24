# la musica

**v3.23.4** — a self-hosted app that *manages, optimizes, audits, grades and plays* your music library, from a browser, a desktop window or a phone.

**la musica** (formerly Music Library Optimizer) is a FastAPI backend plus a React UI over the `mlo` engine: music and music videos (karaoke-synced lyrics), playlists, likes and favourites, artist artwork and biographies, a multi-source lyrics chain, MusicBrainz/Discogs/AcoustID identity, and a managed Soulseek client whose auto-importer verifies what it downloads. It grades the library (68 checks), audits it, and runs an installable toolchain — all local.

State — config, playlists, the beets library, Soulseek config, caches, runtime-installed tools — lives in one hidden `.mlo` folder beside your music.

- Release notes: `release-notes-<version>.md` in the repo root (pre-3.14 under `local/`).
- Grading and optimization contract (check ids, defaults, presets, runbook): [`docs/OPTIMIZATION-GRADING-SPEC.md`](docs/OPTIMIZATION-GRADING-SPEC.md). This README and the GitHub description follow it.

## Quick start

Docker is the only supported **server** install; every client connects to it over the network.

### Docker (recommended)

```bash
docker compose up -d --build     # build from source and start
docker compose logs -f           # follow the backend log
docker compose pull              # fetch the prebuilt GHCR image instead
docker compose down              # stop and remove
# open http://localhost:8000 — your library is mounted at /music
```

- **Edit one line first**: the bind mount, committed as `./music:/music`. Use an absolute host path (`/path/to/music:/music`, `D:/Music:/music`) — compose would otherwise create an empty `./music` and read that.
- Image `ghcr.io/dillydalli3r/la-musica:latest`, container `la-musica`, port `8000`. `MLO_MUSIC_FOLDER=/music` is the only env var it needs; `MLO_SERVER_HOST=0.0.0.0` makes the published port reachable (and turns the login gate on).
- `/music` holds the library **and** all app state (`data`, `downloads`, `trash`, tools), so one mount carries everything.
- Unprivileged `mlo`, uid/gid **1000**: the mounted folder must be writable by uid 1000, or `.mlo` cannot be created. Healthcheck: `http://127.0.0.1:8000/api/health`, 30 s.
- `watchtower` is on (`WATCHTOWER_POLL_INTERVAL`, 300 s) and replaces a `--build` image with the published one; to keep building from source, start only the app: `docker compose up -d lamusica`.
- Tools install themselves at runtime from **Settings → Dependencies** (apt rows: `ffmpeg`, `flac`, `libjxl`, `jpegtran`, `fpcalc`, `rsgain`, plus `mono-runtime` with `libgdiplus` and `php-cli` for CUETools/AudioAuditor and Logchecker). `GET /api/capabilities` says what a given server can do.

### From source

```bash
# backend
python -m pip install -r server/requirements.txt
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000

# frontend (dev, http://localhost:5173 proxies /api and /ws to :8000)
cd web && npm install && npm run dev
```

Production build — the backend serves `web/dist` itself:

```bash
cd web && npm run build          # tsc -b && vite build
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000   # http://127.0.0.1:8000
```

- `python -m server.main` reads `server_host` / `server_port` from the config and binds there, so module, Settings page and login gate agree. A hand-typed `uvicorn --host` bypasses the config — bind through the config.
- The music folder is picked in the UI (**Settings → General → Music folder**, or wizard step 1); `GET /api/fs/dirs` browses the server's folders. Choosing one *moves* the app state (`<music>/.mlo`) into it.
- Install the toolchain from **Settings → Dependencies**: `ffmpeg`, `flac`, `libjxl`, `jpegtran`, `oxipng`, `rsgain`, `AudioAuditor`, `Logchecker`, `php`, `CUETools`, `fpcalc` (optional), `librosa`, `beets`, `slskd`, `yt-dlp`. Each row shows installed, pinned and upstream versions; a copy an older release installed beside the app is still read.
- A row is `deps` (installer fetches it), `unsupported` (no build here, with the reason) or `system` (the button copies the package manager's command). Every `deps` row has **Install** / **Update**; **Update all** presses every missing or outdated row.
- A press fetches the newest publisher release (GitHub, PyPI, windows.php.net); the pinned version is what a first install fetches when nothing upstream is reachable. An updated row installs into a *new* folder, so a running install is never rewritten in place.
- `dependencies_auto_update` (off) installs missing or outdated tools on boot.

### Configuration & credentials

- **First run asks six things**: music folder, account (the login gate), external tools, source keys and cookies, Soulseek login and sharing. Everything else has a shipped default, editable in Settings.
- Config: `<music>/.mlo/data/config.json`, edited through Settings (`GET/POST /api/config`; `GET /api/config/defaults` is what *Reset to defaults* writes). Seeded from the environment by `MLO_MUSIC_FOLDER`, `MLO_SERVER_HOST` / `MLO_SERVER_PORT`, `MLO_AUTH_MODE`, `MLO_LOCALE`.
- Keys worth knowing: `music_folder`, `server_host`, `server_port`, `auth_mode`, `mb_genre_count`, `genre_sources`, `run_all_order`, `import_scripts`, `naming_script`, `lyrics_format`, `cover_target_size`, `embed_covers`, `playback_eq_profile`, `library_codec`. The ones that change a grade are in [the spec](docs/OPTIMIZATION-GRADING-SPEC.md#9-config-keys-that-change-a-grade).
- Credentials sit in that file **in clear** — it is your server's. All optional: `spotify_client_id` / `spotify_client_secret`, `discogs_token`, `lastfm_api_key`, `rym_cookie`, `acoustid_api_key`, `slskd_username` / `slskd_password`.
- `youtube_cookies_mode`: `none` (default), `file` (a jar saved on **Settings → Videos**; the app owns `<music>/.mlo/data/cookies.txt`) or `browser`.
- `GET /api/sources/health` lists every external source (38 rows) with what each needs; `?probe=1` tests them against the provider's own endpoint, so a refused key is reported in the provider's words.

## What the app does

### Library, search and identity

- Artists → albums → tracks, with live grade/audit badges, a *fail only* filter, bulk tag tools, custom tag columns and sortable/resizable columns.
- Music videos are first-class tracks; a web/digital album can fetch its own through yt-dlp.
- Album badges: measured dynamic range (`ADR`), the medium, every release country, format/bitrate.
- Every track row has the same "…" menu: tag editor, genre/advisory imports, its scripts, credits, the stored readout.
- The top search bar searches the library (`composer:`, `person:`, `genre:`, `tag:`) or MusicBrainz; `/mb/search` is a full in-app MusicBrainz browser with *Auto-import* and *Add to library*.
- An entity's alias in your locale is shown beside its name (`宇多田ヒカル (Hikaru Utada)`), exact locale first, then MusicBrainz's primary alias.
- Entity pages: grading and audit detail, MB/RYM links, cover upload and search, Wikipedia descriptions, tag editing, a lyrics editor, a **Credits** view from MusicBrainz `artist-rels`.
- The cover finder ranks candidates with the album's own identity and writes the winner by default (`cover_review` asks first); a URL that cannot be fetched writes nothing.
- **Recommended (Local)** and **Home** are computed from the library's own tags — no provider, no model, no network.
- **LIBRARY → BROWSE** is a query builder over 120 fields with the operations each declares, match all/any and a live count; a saved one is a **smart playlist** (the filter, not the rows).
- **Ratings** are half stars in the UI, Picard's 0–10 in the store, written to the file's `RATING` tag.
- **Five views** — Grid, Compact, Albums, Artists, Tracks — each with its own sort and columns; filter presets (*Failing*, *CD rips*, *Instrumental*, …) plus facets with live counts.

### Discover, recommendations and watching

- **DISCOVER** browses genres across every configured provider (MusicBrainz, Deezer, iTunes, TheAudioDB, Last.fm, ListenBrainz, Discogs, Wikidata, Wikipedia, Spotify, RateYourMusic, Bandcamp) with a library/online/both scope.
- **RECOMMENDED (ONLINE)** takes a seed (the library, a genre, or the page you are on); its **(LOCAL)** half is scored from the library's own tags and needs no network.
- **WATCHED ARTISTS** keeps an artist under watch: policy, release types, allow/never lists, a per-cycle cap and auto-add. Each check queues release groups into the download queue.

### Player

A persistent player bar (queue, drag-reorder, shuffle, repeat-one, speed, sleep timer, ReplayGain, visualizer, app-wide volume) plus a fullscreen player with animated karaoke lyrics.

- Play state comes from the media element's own events, so an OS-made pause is what the bar shows.
- The lyrics pane, metadata block, transport and top bar draw no background or blur of their own; the ink is derived from the cover (`npInk`), so a bright cover flips to near-black text.
- Both lyric surfaces share the same size control (`−`, a typeable percentage, `+`, 5 % a press, 85–160 %, remembered per surface) and the same offset control (`−`, pending shift, `+`, Save).
- The pane is a control you own: one microphone toggle in the player's control row, shown only while the track has lyrics.
- Lyrics come from the `.lrc` beside the file and/or its `LYRICS` tag; a stamp before the file's start clamps at `[00:00.00]`.
- ReplayGain runs through the WebAudio gain stage in **track**, **album** or **off** mode (`replaygain_mode`, `replaygain_preamp_db`, ±24 dB); a file without tags is measured on the fly when `replaygain_measure_missing` is on.
- **Equalizer** (sidebar → MAINTAIN): an Equalizer APO / Peace profile applies to playback (`playback_eq_profile`, `""` = off, so every client of the server hears the same curve). Built-in presets, APO/Peace/AutoEq import, a live curve editor.
- Pressing play on what is already playing restarts it; a video is transcoded to fragmented MP4 when its codec needs it (`GET /api/videos/stream?transcode=1`).

### Import

Drag & drop uploads, a watched folder, staged `.mlo/downloads` or Soulseek paths — all through one pipeline (`server/imports.py`).

- **AcoustID fingerprint matching** identifies the release from the *audio*, not the tags (needs `acoustid_api_key` and `fpcalc`); accepting a match writes `ACOUSTID_ID` + `ACOUSTID_FINGERPRINT` in one save.
- The wizard's eight steps: **Select & separate → Links → Match → Covers → Genres → Lyrics → Advisory → Finish**; *Finish* runs the scripts you ticked.
- The **import script chain** (`import_scripts`, i.e. `DEFAULT_CHAIN` = `run_all_order`, one list in `mlo/config.py`) runs after the wizard; `import_auto_scripts` turns it off for imports.
- An import decides four families for itself — lyrics, genre, advisory, embedded cover — and replaces what arrived (`server/imports.py::drop_arrived_values`); a family in `import_review_families` is left as it arrived.
- **Bulk import** queues several albums with `import_bulk_concurrency` (2 by default, 1–8).
- **An import finishes on its own and only asks when the answer is yours.** `import_autonomy` ships as `automatic`: it decides every family it can and reports what no source could supply; what waits is the family you kept, the cover when `cover_review` is on, or something no source could state. Those are what the notification menu and the Soulseek page's *Needs you* row carry. A waiting album stays **parked** (a library-wide Run All skips it).
- **An import never loses the album it is working on.** *Beets tagging* (script 14) moves and renames it, so later scripts act on the folder it is in now; the chain's end is what the queue row and album page report.
- **The cover a framework album carries is a stand-in**: the real cover is fetched over it, and the stand-in is dropped only once that cover is written.
- **When the picked edition is not on the network, the album is not given up.** *Add to library* tries the release group's ranked editions in order, up to `soulseek_fallback_candidates` (1–10, 5 by default); if none is there, the release moves to the queue's **Background** list and keeps being searched.
- **A wish or a followed artist takes only a lossless copy.** `soulseek_auto_lossy_policy` ships as `never`; the Soulseek page always asks.
- The **Genres** step asks the whole chain with one button, in `genre_sources` order (RateYourMusic first), cascading track → release → release group → artist; the answer says which level spoke.
- The **Advisory** step resolves `ITUNESADVISORY` from every applicable source (Deezer/Spotify by ISRC, Apple's routes), merged so a stated value ends the question; the configured AI answers only when everything else came up empty (`advisory_ai_classify`).

### Soulseek & the download queue

A managed slskd instance (autostart, shares = `<music>/Artists`, a rescan whenever the library changes), with search and download UI, a live status dot, bulk and whole-user downloads.

- **Transfer progress is pushed**, not polled: `GET /api/soulseek/downloads` rows and every live job ride the progress WebSocket as `{"type":"transfers"}` frames (0.4 s cadence), with a 30 s fallback for a dead socket. They never reach the notification tray.
- **An import that needs a hand is an outcome**, announced wherever you are: `import_needs_data` raises a system notification, a tray entry, an in-app event and a *Needs you* row.
- **Is the port open?** *Test port* (`GET /api/soulseek/port-check`) answers with five rows that each say what they prove: a real TCP connection, a bind test, the gateway's own mapping entry, the LAN/WAN address shape (CGNAT named as such), a public-address self-connect (refused ⇒ unknown) and slskd's signed-in state.
- The listen port is opened on the router by the app (`soulseek_upnp`, ON) — UPnP IGD first, NAT-PMP behind it, reported as made only when the gateway confirms it. In a container, map it yourself.
- **Which edition is fetched** is `mlo/release_choice.py`, the same policy the release-group page shows: official editions first, then the configured medium order, then the date. Disc streams are preferred (`prefer_disc_streams`), and a disc structure beside a re-encode is remuxed as the disc's own title.
- **The original pressing wins the date, exactly**: an edition is scored by closeness to the group's first release date, and a full `YYYY-MM-DD` beats a bare year.
- **Where a release is fetched from decides itself**: a music video on Digital Media cannot be a Soulseek folder, so it comes from YouTube (achieved length, lyric/cover/tribute rows refused) into `<downloads>/YouTube/<Artist - Album>`, then the same import. A video on a disc keeps the Soulseek path; an audio release is never routed elsewhere.
- **The auto-importer searches by what can only point at that release**: a physical pressing by catalog number and barcode (`soulseek_auto_physical_queries`), else by label and country; digital by `soulseek_auto_digital_queries`. Search terms are stripped of punctuation and resolved through MusicBrainz aliases (`ぴーなた` → `pinata`).
- A CD candidate's `.log` is gated *before* any album byte is requested (`soulseek_auto_log_min_score`, default 100), and candidates are ranked towards the copy that arrives fastest — up to `soulseek_candidate_slots` (3) at once from three different peers; the first that verifies becomes the import, the others are cancelled.
- **Two limits**: `soulseek_search_concurrency` (releases at once, default 3) and `soulseek_candidate_slots` (candidates of one release, default 3). Over them, work waits (slskd's own `soulseek_download_slots` is never touched); the Queue tab's header reads all three back.
- A release is imported **once**, into one album folder: the destination is checked against the release's MusicBrainz ids, so a download of an album you already hold is refused with a sentence instead of appearing as `… (2)`. The album stays locked from the first tag write through the last script.
- The bar takes **any MusicBrainz link**: a release, a release-group (its editions are walked), an artist (its discography is queued in the background) or a recording.
- A wish keeps looking every `wishes_interval_hours` (6) with retry backoff **until it is found or cancelled**. Notifications cover the add, the download and the import. When the import lands, the downloaded copy is deleted (`soulseek_clear_downloads`, ON).
- **The search asks more than one pressing**: every add records the group's ranked editions, deduped by folded catalog number, so `GED 24425` and `GED24425` are one search.
- **What counts as a good rip log is 100 wherever it is asked**: the acquisition gate, the grading check and the audit. A release with no log is offered through an explicit confirm.
- **Two independent size caps**, both 5 GB by default (`soulseek_cache_cap_gb`, `trash_cap_gb`, 0 = off). Over its cap a store is emptied oldest-first, and what is in use is never touched (`server/job_locks`); a kept file is reported and still restorable.
- **Clear all** empties queued/waiting work in one press and never touches a running release (that is a cancel, on its own row) or a finished one. Waiting releases are their own group, in the order they will start.

### Optimization — the 21 scripts

Optimization → *Run All* executes `run_all_order`, shipped as **11 → 3 → 14 → 15 → 2 → 1 → 13 → 18 → 17 → 8 → 5 → 19 → 6 → 7 → 9 → 12 → 16 → 10 → 20 → 21 → 4**: everything that moves a file first, everything that reads it last. Every script also runs on its own, from the menu or an album page. A library-wide run claims the paths it walks; an album-scoped run holds only its own album, and a script that moves an album takes the claim with it (`server.job_locks.move`).

| # | Script | What it does |
| --- | --- | --- |
| 1 | Format lyrics | Canonical embedded LYRICS / `.lrc` (padding, blank lines, zero-timestamp rule, Enhanced/Extended word-sync); MEDIA/SOURCE normalization |
| 2 | Format CUEs | Canonical CUE text, `FILE`-line fixes, `CD-N` sheet renaming |
| 3 | Optimize FLACs | Re-encode at `library_codec_quality`, strip padding/CUESHEET/APPLICATION and non-canonical tags, convert non-conforming files to `library_codec` |
| 4 | Grade | The full grading battery (see the spec) |
| 5 | Process images | Covers resized/cropped (`cover_target_size`, default 1200; per-format targets), JPEG/PNG/JXL optimization |
| 6 | Audit library | AudioAuditor detectors + CD `.log` CRC verification → `AUDIT` |
| 7 | DR & ReplayGain | In-process loudness-war DR tags + rsgain ReplayGain (album gain, FLAC and MP4 alike) |
| 8 | Auto tagging | `ITUNESADVISORY`, `INSTRUMENTAL`, `MOOD`, `ENERGY`, `GENRE` |
| 9 | AccurateRip | CUETools `.accurip` generation and verification; regenerated only when a track's audio changed |
| 10 | Format all | Final canonical pass: `.accurip`/`.cue`/`.lrc`/tag trim + the embedded-cover policy |
| 11 | Remux videos (MKV) | Any video container → MKV, video copied bit-exact where possible, audio to FLAC, chapters kept |
| 12 | Key & BPM | librosa-backed `INITIALKEY` + `BPM` |
| 13 | Fetch lyrics | The configured synced-lyrics chain into the configured format |
| 14 | Beets tagging | Managed beets (Picard parity) with the naming script and work/movement tags |
| 15 | Release tracklist | Writes `.mlo_expected.json` (the release's own tracklist) into the album folder |
| 16 | Mood & Energy | The mood classifier alone (`MOOD` + `ENERGY`) |
| 17 | Lyrics transliterate (AI) | `TRANSLITERATION-<LANG>-LATN` / `TRANSLATION-<LANG>` tags and sidecars, re-synced at `lrc_sync_level` |
| 18 | Publish lyrics (LRCLIB) | Submits this library's lyrics for recordings LRCLIB lacks (`lrclib_auto_publish`, `force_publish`) |
| 19 | Optimize artist images | Crops `Artists/<Artist>/artist.*` to `artist_image_aspect`, downscales to `artist_image_target_size` (never upscales) |
| 20 | Optimize library layout | Settles what it can prove with `layout_apply`: case-correct names, audio outside an album folder, empty albums, stray sidecars (including numbered duplicates like `description (2).txt`) |
| 21 | Fix AcoustID pairs | Completes a half-written pair: an id with no fingerprint gets `fpcalc`, a fingerprint with no id gets the lookup |

**What script 17 transliterates is decided from evidence**: the rule (`mlo/lyrics_xlit.xlit_needs`) reads the track's own `LANGUAGE` tag, then the script of its lyrics (kana is Japanese, hangul Korean; a statement in Cyrillic or Han states nothing), then the function words of the languages the app knows. When nothing can say, script 17 asks the configured AI one question about that track and stores the answer.

Force flags, one per script (`force_lyrics`, `force_cue`, `force_tracklist`, `force_reencode_flac`, `force_reencode_images`, `force_audit`, `force_accurip`, `force_dr_replaygain`, `force_audiometa`, `force_mood`, `force_auto_tag`, `force_xlit`, `force_publish`, …): a script in the one-shot **Force** menu really turns its switch off rather than falling back to a saved one. Script 20 is the one key that turns work *off* (`layout_apply`).

Scripts that are pure no-ops when their feature is unpicked are skipped rather than run: `dr_replaygain_enabled` (7), `audiometa_enabled` (12), `mood_enabled` (16), `lyrics_xlit_enabled` / `lyrics_translate_enabled` (17), `lrclib_auto_publish` (18), `acoustid_enabled` (21).

Script 6 keeps an evidence record per file (size, mtime and, where the container states one, an audio identity a tag write cannot move — FLAC's STREAMINFO MD5), so a second run over an unchanged album re-audits nothing.

By default the optimizer does **not** embed cover art — it removes it, and covers live on disk as `cover.*` plus per-track sidecars. Settings → *Embedded covers* (`embed_covers`, `embed_cover_jpeg_quality`, `embed_cover_resolution`) makes script 10 embed them.

### Grading

**68 checks** across tracks, albums, artist folders and folders, each toggleable on the **Grading** page with a live filter and the **Strict / Balanced / Relaxed** presets. Every check is on in the factory defaults; an album passes only when every enabled check passes, and the summary counts checks (`summary_pass` / `summary_total`) plus `albums_passed` / `albums_failed`.

- A check that raises counts as *could not be evaluated* and fails.
- Two checks follow the codec target: `grade_check_lossless_source` stands down when `library_codec` is uncompressed (`wav`/`aiff`) or `keep`, and `grade_check_cd_format` applies where a CD is expected.
- A tag **value** has one canonical form (`mlo/tagtext.py`), applied on every write and re-applied library-wide by script 10: closed-vocabulary tags are cased and folded, spacing collapsed, and an unknown value left alone. `RELEASECOUNTRY` holds every country the release states, earliest first. Genres are stored broad-first (`Rock / Shoegaze / Dream Pop`).
- A tag with more than one answer is a LIST, stored as repeated container fields (one per value, `; `-joined on read): credit roles, the track's performers, and so on.
- Home and the Library open with the verdict and, when it fails, what fails: one row per album or track, each linking to the thing it names, capped at 12 with the rest a `+N` link into the Library's *Failing* filter. Past three findings the list folds behind **Read more — N findings** (the summary line is always first and is never folded).
- Every check id, what it asserts, its default, the presets, the audit workflow, tag families, quality bars and a runbook: [`docs/OPTIMIZATION-GRADING-SPEC.md`](docs/OPTIMIZATION-GRADING-SPEC.md).

### Export, playlists, offline

**Export** writes a playlist, albums, artists, tracks or the whole library as MP3 (VBR/CBR or custom), AAC, Opus, Vorbis, WAV, AIFF, ALAC, WavPack, WMA or a bit-exact `copy`, in the shipped `albumartist_album_disc` layout — or `flat`, `mirror` or a structure you type in the same Picard-style grammar the naming script uses (`export_codec`, `export_structure`, `export_subfolder`; the page previews one).

- **Where it goes**: `export_target` is `zip` (one archive the client downloads — the only mode a browser can honour, and the default) or `server`, a folder the machine can see, picked with the drive list and free-space readout.
- **What gets copied** is `export_copy_files`, ticked one by one and named per file by the run's `excluded` report: tracks, covers, `.lrc`, `.cue`, `.log`/`.accurip`, `description.txt`, checksums, other text, playlists, and anything the album holds that the app cannot classify (subfolders are reported, never walked). A run nobody asked anything of writes the tracks alone; an empty selection is refused.
- IDs are ID3v2.3 plus optional ID3v1; `export_manifest` writes `checksums.sha256`; `export_playlists` writes an `.m3u8` per album plus `all.m3u8`; every written file is re-opened and verified. Sync mode (`export_prune`) removes audio the source no longer has.
- **A run can be cancelled**: it ends at the next file boundary, what it wrote stays, and the finishing passes are skipped. Two exports cannot fight over one destination — the export holds the drive it writes into, so the second answers 409.
- **ReplayGain** is a mode: `export_replaygain_mode` is `off`, `tags` (measure and write `REPLAYGAIN_*`) or `apply`, which rewrites the audio so the files themselves are level — the album gain for a whole album, the track gain otherwise.
- **An equalizer** rides along: `export_eq_profile` selects a built-in curve or a profile imported from Equalizer APO / Peace / AutoEq (`Preamp:`, `Filter N: … PK|LS|HS|LP|HP Fc … Gain … Q …`, `GraphicEQ:`, `FilterCurve:`), applied to the exported copies only; anything unrenderable (`Include:`, unknown constructs) is reported instead of silently dropped. Both processing modes need a real codec.
- **Playlists** are manual (drag-reorder, favourites, `.m3u8` import/export) or smart, driven by saved grade/audit/tag filters.
- **Offline**: "Download" caches a track in the service worker's media cache and warms its album/artist payloads, so the UI opens and a downloaded album plays with the server down. In the Tauri shells a small filesystem layer does the same job.
- `download_codec` ships as **`copy`** (the cached bytes are the library's own); a codec target re-encodes for that device's cache only. `playback_source` ships as **`stream`** — the player asks the server even when a copy is downloaded, or plays the copy (`downloaded`).

### Notifications and languages

The backend publishes every settled outcome on `/ws/events` — `wish_found`, `wish_failed`, `wish_not_found`, `download_started`, `download_done`, `upload_started`, `import_needs_data`, and the rest. Every client keeps that socket open and raises an OS notification (Tauri's plugin on desktop and mobile, the Web Notification API in the browser) with an in-app toast when permission is refused.

- Kinds are switchable per client (`notify_wish_found`, `notify_download_done`, …); `?since=` replays what a client missed, from the in-memory ring plus the durable log beside the app state.
- **Remote push too**: the server signs with VAPID, encrypts each message (RFC 8291 `aes128gcm`), keeps one row per subscribed device and prunes a device the push service reports gone. The key pair lives outside the config, because `GET /api/config` hands the whole config to every signed-in client. Settings → Notifications shows a switch per device with *Send a test notification*.
- The UI ships in six languages — English, Español, Français, Deutsch, 日本語, Português (Brasil) — chosen per browser (`localStorage: mlo.locale`), then the server's `ui_locale`, then the browser's own.

## Clients

The same React build runs in five targets, and **every one is a client of a server you run**: the Docker container on `127.0.0.1:8000`, a machine on the LAN, or a Tailscale name. No shell carries Python, a sidecar or a port of its own.

| Target | Built by | Artifact | What it is |
| --- | --- | --- | --- |
| Browser | the server (`web/dist`) | — | the app itself, over HTTP/S |
| Windows / macOS / Linux | `npx tauri build` | `.msi` + NSIS `.exe`, `.app` + `.dmg`, `.deb` + `.AppImage` | client of a server you run |
| Android | `npx tauri android build --apk --debug` | debug-signed APK | client of a server you run |
| iOS | `npx tauri ios build --target aarch64 --no-sign` | unsigned IPA | client of a server you run |

Both halves are built locally — `cd web && npm install && npm run build`, then `cd ../desktop && npm install && npx tauri build`. `.github/workflows/desktop.yml` builds the three desktop bundles.

- **Desktop** keeps only the shell furniture: a tray icon (Open la musica / Auto-start on login / Exit), a native folder picker and the window. Quitting it stops nothing but itself.
- **Capabilities are the server's, not the app's**: `GET /api/capabilities` is derived from what the server can actually do (subprocess, ffmpeg/flac/fpcalc/rsgain present), so a client shows what will work rather than offering an Install button that cannot succeed.
- A shell that has never been set up opens **its own wizard** (Server → Account → Notifications → Done); *Test* probes `${address}/api/health` with a 3 s deadline and **Next** stays disabled until a real server answers.
- Both mobile artifacts are **sideload builds, signed by nobody here**: the APK is a debug build, the IPA is built `--no-sign`, and a plain-http server needs the platform's exemption (`desktop/src-tauri/Info.plist`, which also sets `UIBackgroundModes: audio`). `mobile.yml` reads both back out of the built `.app`, so a merge that drops either fails the build. `tools/make_sidestore_source.py` writes the `source.json` the release publishes.

## Terminal entry point

```bash
python -m mlo
```

The classic console menu — scripts 1–21, Run All, the config editor and the dependency table. It is **not** stdlib-only: `mlo` imports `mutagen` for every tag operation, so run it from the same environment as the server. A missing module makes its script unavailable and fails loudly instead of reporting a clean "0 processed" run.

## Security & accounts

Everything the API can do — read the library, rewrite tags, move and delete files, start downloads — is one password away from anyone who can reach the port. The top bar's rightmost control is the account menu (change password, sign out everywhere).

| Key | Default | What it does |
| --- | --- | --- |
| `auth_mode` | `auto` | `auto` = gate ON when `server_host` is not loopback; `required` = always; `off` = loopback only |
| `server_host` | `127.0.0.1` | where the server binds — and the address the gate reads |
| `server_port` | `8000` | the port |
| `auth_username` | `""` | the name shown on the login screen |
| `auth_password_hash` | `""` | PBKDF2-HMAC-SHA256, `pbkdf2$<rounds>$<salt-hex>$<hash-hex>` |
| `auth_session_days` | `30` | how long a session stays valid |
| `server_public_url` | `""` | the address clients should dial when it is not the page's own origin |

`auto` follows the bind: `127.0.0.1`, `::1` and `localhost` keep a single-user install password-free; anything else asks for a login — **from clients**, decided per request from the peer address, so the machine running the server never has to sign in while a phone does. `X-Forwarded-For` is never trusted, and `off` on a non-loopback bind is treated as `required`.

- **A login**: PBKDF2-HMAC-SHA256, 600 000 rounds, random salt, constant-time comparison, minimum 8 characters. `auth.db` also holds a `users` table; a session carries its username, and every playlist, like, favourite and trash folder is that user's. Five consecutive failures make an address wait 30 s, doubling to 15 minutes.
- **Sessions** are 32-byte tokens; only their SHA-256 is stored, in `<music>/.mlo/data/auth.db`. The token travels as `Authorization: Bearer`, as the HttpOnly `mlo_session` cookie, or as `?token=` for the WebSockets (which close with code **4401** when it expires).
- `/api/auth/setup` and the static shell answer while `428` (`{"needs_setup": true}`) is in force; every other route refuses until `POST /api/auth/setup` sets the password.
- **First run over the network**: start bound to the address you will use (`server_host: 0.0.0.0`, or `MLO_SERVER_HOST=0.0.0.0` in the container), open the app from a client and claim the server; each client signs in once and keeps its own session.

### The honest limits

- **One password, one user.** No roles, no permissions, no signup — two people sharing a server share the password.
- **The app does not terminate TLS.** Over plain HTTP the token and password travel in the clear. Put it behind a reverse proxy or a mesh VPN when it leaves the LAN, and set `server_public_url`.
- **The gate follows the configured `server_host`, not the socket.** The Docker image seeds that key from `MLO_SERVER_HOST`, so config, bind address and gate cannot disagree there; a hand-started `uvicorn --host` can make them.
- **A closed client is reached only where the platform allows it.** A browser with the push switch on is woken by the server's own Web Push; the desktop and mobile shells have no service worker and notify while they run.
- **The Windows-only tools run in Docker through their runtimes** — CUETools and AudioAuditor on the image's mono runtime, Logchecker on its `php-cli`. `GET /api/capabilities` says what a given server can actually do.

## Architecture

```
web/         React 19 + TypeScript + Tailwind UI (Vite, the service-worker media
             cache + offline JSON copy, six locales in src/locales)
server/      FastAPI backend: library payload, playlists, integrations, import
             (AcoustID + the shared script chain + bulk queue), discovery
             (Deezer/ListenBrainz/iTunes/TheAudioDB/Wikipedia + MusicBrainz),
             artist artwork and descriptions, streaming, export, organize,
             WebSocket progress, the sequential import runner (import_queue),
             the login gate (auth + api_auth) and /ws/events
mlo/         core engine (imports mutagen for every tag operation): grader, audit,
             flac, images, lyrics (word-sync + the provider chain), lyrics_xlit,
             genres, moods, artistdata, acoustid, cue, accurip, loudness, autotag,
             remux, naming, discs, stats
desktop/     Tauri v2 shell — desktop bundles plus Android/iOS clients
tools/       test-library generator and test suites; docs/ holds the contract
```

Where the app stores what it fetches (all under `<music>/.mlo/`):

| File | What it holds |
| --- | --- |
| `data/config.json`, `data/playlists.db`, `data/auth.db` | all settings; playlists, likes and favourites (per user); sessions and user rows (the token's SHA-256, never the token) |
| `data/artwork.json`, `data/metadata_review.json` | provenance for artist images/descriptions; staged metadata candidates |
| `data/replaygain.json`, `data/audit_evidence.json` | on-demand ReplayGain measurements (invalidated on size/mtime change); what each `AUDIT` verdict was proved on |
| `data/rym_cache/`, `data/lyrics_ai_cache/`, `data/wishes.db`, `data/update_check.json` | RYM genre pages (30 days); AI answers by prompt hash; the wishlist; the last release check (6 h) |
| `downloads/`, `trash/<user>/`, `Artists/<Artist>/artist.jpg` | Soulseek staging; deleted files per user; artist image and description |
| `<album>/description.txt`, `<album>/.mlo_expected.json` | album description; the release's own tracklist (script 15) |

## API overview (selected)

| Endpoint | Purpose |
| --- | --- |
| `GET /api/library` | tag-rich library tree (grades, audits, tags, tech info; gzipped) |
| `GET /api/health` `GET /api/version` | liveness; `{version, latest, update_available, release_url, checked_at, source}` cached 6 h, `latest: null` when GitHub is unreachable |
| `GET /api/auth/status`, `POST /api/auth/setup` `…/login` `…/logout` `…/password` `…/revoke-all`, `GET …/sessions`, `GET/POST /api/auth/users`, `DELETE …/{name}` | the gate's state (`required`, `has_password`, `username`, `host`, `public_url`, `session_days`), sign-in and out, sessions, users |
| `GET /api/library/layout`, `GET /api/library/layout/report` | read-only layout scan (misplaced audio, stray and duplicate sidecars, empty albums, `wrong_case`); the same scan as script 20, plus the stored report |
| `GET /api/storage` | one disk snapshot for the Home card: the volume, the library, the app's own footprint, the bin and the transfer folders |
| `GET /api/album` `GET /api/artist` `GET /api/artist/artwork`, `GET /api/stream` `GET /api/videos/stream` `…/meta` `…/thumb` | entity details, stored artist image/description and the artist's own grade; audio and video streaming |
| `GET /api/tags` `POST /api/tags/bulk` `…/videos/tag`, `POST /api/run`, `POST /api/organize` | per-track tag view; bulk tag surgery; video tag writes; run scripts 1–21; apply the naming script (dry run supported) |
| `POST /api/export`, `GET /api/export/codecs` `…/drives` `…/defaults` `…/files`, `POST /api/export/cancel` | multi-format export, its codec table, drives, saved defaults, the file families `copy_files` names, and cancel |
| `GET/POST/PATCH/DELETE /api/wishes…`, `POST /api/wishes/{id}/search` `…/search-all` `…/reconcile` `…/import` | the wishlist and its worker |
| `GET /api/sources/health` `…/{id}`, `GET /api/capabilities`, `GET /api/dependencies` | every external source with its `needs`/`configured` state (`?probe=1`); what this server can run; the tool table |
| `GET/POST/DELETE /api/youtube/cookies` | the yt-dlp cookie jar: which mode is on, save a pasted/dropped `cookies.txt`, delete it |
| `POST /api/mb/match` `…/assign` `…/auto-import` `…/advisory/fetch`, `POST /api/genres/import`, `GET /api/genres/facets` | MusicBrainz matching, tag writes, queued downloads, advisory resolution; genre import and facets |
| `POST /api/import/upload` `…/commit` `…/acoustid` `…/finish` `…/bulk`, `POST /api/lyrics/auto` `…/write` `…/embed` `…/wordsync`, `GET /api/lyrics/find` `…/providers` | the import pipeline, its fingerprint step, the chain and the bulk queue; the lyrics chain |
| `GET /api/cover/search` `…/sources`, `POST /api/cover` `…/fromurl` | cover meta-search, upload and save-as-cover |
| `POST /api/soulseek/download-bulk` `…/download-user` `…/search/cancel`, `GET /api/soulseek/ready`, `POST …/import-one` `…/import-all`, `GET/POST …/import-all/status` `…/cancel` | Soulseek downloads, the ready list and the sequential importer |
| `GET /api/soulseek/port-check` | the listen port's own check (a TCP connect, the router's mapping entry, the address shape, a public-address self-connect, slskd's state) |
| `WS /ws/progress` `WS /ws/events` | live script progress, transfer/job frames and daemon state; every published notification kind, with `?since=` replay |

## Tests & development

```bash
python tools/make_test_library.py   # synthetic library for end-to-end runs
python tools/test_remux.py          # video remux suite (VOB/MKV/AVI/WebM fixtures)
python tools/test_lyrics_xlit.py    # script 17: alignment, romanization, cache (offline)
python tools/test_script_menus.py   # every script menu agrees (numbers, labels, Run All order)
python tools/smoke_api.py           # route smoke test against a running backend
python tools/check_versions.py      # the release gate: every version string agrees
```

- The 113 `tools/test_*.py` suites are standalone scripts (`python tools/test_x.py`; `sys.exit(2)` means "skipped", e.g. a missing toolchain) and mostly run offline with providers stubbed. The frontend gate is `cd web && npx tsc -b && npx oxlint && npm run build`.
- The browser checks need a running backend serving `web/dist` and Playwright (`npm i -D playwright`): `tools/check_menus.cjs` walks every route, `tools/check_responsive.cjs` re-measures every top-level route at 390×780, 834×1112 and 1440×900, and `tools/check_ui.cjs` drives the client-side flows.
- `tools/check_versions.py` compares the app version in `mlo/__init__.py`, `tauri.conf.json`, `Cargo.toml`, both `package.json`s, the Dockerfile's `MLO_VERSION`, this README's header and `desktop/README.md`. CI runs the Python suites and the web build.

## Licence, credits and donations

MIT — see [`LICENSE`](LICENSE). The services and projects the app is built on are in the app's bottom-left credits popover, in [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) and in `web/public/credits.json`. `/donations` carries two addresses (a photograph of the maintainer's cat with one line under it), and **nothing is gated behind it**.

Legacy v1 (the Tkinter app, CLI and PyInstaller/Inno packaging) is archived on the `archive/legacy-v1.7` branch.
