# la musica

**v3.2.0** — a self-hosted app that *manages, optimizes, audits, grades and
plays* your music library, from the browser, a desktop window or a phone.

**la musica** (formerly Music Library Optimizer) is a FastAPI backend plus a
React UI over the proven `mlo` engine. It plays music **and** music videos (with
karaoke-synced lyrics), keeps manual and smart playlists, likes and favourites,
fetches artist artwork and biographies, walks a multi-source lyrics chain, caches
music for offline playback, exports to a device, and drives a managed Soulseek
client whose auto-importer verifies what it downloaded. All app state — config,
playlists, favourites, the beets library, the Soulseek config, measured loudness,
caches — lives in one hidden `.mlo` folder inside your music directory.

Release notes for this version are in `local/release-notes-3.2.0.md` (older ones
follow `local/release-notes-<version>.md`); the grading and optimization contract
is in [`docs/OPTIMIZATION-GRADING-SPEC.md`](docs/OPTIMIZATION-GRADING-SPEC.md).

## Quick start

Docker is the only supported **server** installation: a container needs no
Python or Node toolchain on the host, and every client connects to it over the
network.

### Docker (recommended)

```bash
docker compose up -d --build     # build from source and start
docker compose logs -f           # follow the backend log
docker compose pull              # fetch the prebuilt GHCR image instead
docker compose down              # stop and remove
# open http://localhost:8000 — your library is mounted at /music
```

`docker-compose.yml` is the template, and it ships one line you must edit: the
bind mount that points at your library (`./music` in its comments,
`F:/media/music:/music` as committed). What matters:

- Image `ghcr.io/dillydalli3r/la-musica:latest`, container `la-musica`, port
  `8000`, pinned compose project name. `MLO_MUSIC_FOLDER=/music` is the only
  environment variable it needs; `MLO_SERVER_HOST=0.0.0.0` makes the published
  port reachable and switches the login gate on for non-local clients.
- Volumes: `/music` holds the library **and** all app state (`/music/.mlo/data`,
  `…/downloads`, `…/trash`); `lamusica-dependencies` at `/app/.dependencies`
  holds runtime-installed tools, so they survive a rebuild.
- It runs unprivileged as `mlo`, uid/gid **1000**, `HOME=/home/mlo`, so the
  bind-mounted folder must be writable by uid 1000 or `.mlo` cannot be created.
  The healthcheck probes `http://127.0.0.1:8000/api/health` (30 s interval).
- A `watchtower` service is on by default, checking every
  `WATCHTOWER_POLL_INTERVAL` seconds (300). It updates the **image**, so a local
  `--build` is replaced by the published one on the first check; to keep building
  from source, start only the app (`docker compose up -d lamusica`).
- `ffmpeg`, `flac`, `libjxl`, `jpegtran`, `fpcalc` and `rsgain` come from apt in
  the image; `oxipng` and `slskd` install at runtime from their upstream Linux
  builds. `CUETools`, `AudioAuditor` and `Logchecker` + `php` are Windows-only, so
  AccurateRip generation, the Logchecker grade and the AudioAuditor audit are
  unavailable in Docker.

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

`python -m server.main` reads `server_host` / `server_port` from the config and
binds there, so the module and the Settings page cannot disagree about the
address — which is also the address the login gate reads. A hand-typed `uvicorn --host` bypasses the config, so bind through the
config or set `auth_mode: required`. The music folder is picked from the UI
(**Settings → General → Music folder → Change…**, or step 1 of the setup wizard;
`GET /api/fs/dirs` browses the server's own folders and flags the ones that
already hold audio); choosing one *moves* the app state (`<music>/.mlo`) into it
and never moves music files, and `MLO_MUSIC_FOLDER` pins the choice instead.

Install the external toolchain from **Settings → Dependencies**. It knows
sixteen tools — `ffmpeg`, `flac`, `libjxl`, `libjpeg-turbo` (`jpegtran`),
`oxipng`, `rsgain`, `simple-dr-meter`, `AudioAuditor`, `Logchecker`, `php`,
`CUETools`, `chromaprint` (`fpcalc`, optional — AcoustID), `librosa`, `beets`,
`slskd`, `yt-dlp` — and shows each one's installed, pinned and upstream version.
Per platform a row is `deps` (the installer fetches it), `system` (a distro
package — `apt: flac`, no Install button) or `unsupported` (no build here, with
the reason). `dependencies_auto_update` (off by default) installs missing or
outdated tools in the background.

### Configuration & credentials

- The whole configuration is `<music>/.mlo/data/config.json`, edited through the
  Settings pages (`GET/POST /api/config`; `GET /api/config/defaults` is what
  *Reset to defaults* writes back). `MLO_MUSIC_FOLDER`, `MLO_SERVER_HOST` /
  `MLO_SERVER_PORT` (they seed `server_host` / `server_port`) and `MLO_VERSION`
  seed it from the environment.
- Keys worth knowing: `music_folder`, `server_host`, `server_port`, `auth_mode`,
  `mb_genre_count`, `genre_sources`, `run_all_order`, `import_scripts`,
  `import_auto_scripts`, `naming_script`, `lyrics_format`, `cover_target_size`,
  `embed_covers`, `dependencies_auto_update`, `ui_locale`; every key that changes
  a grade is listed in
  [`docs/OPTIMIZATION-GRADING-SPEC.md`](docs/OPTIMIZATION-GRADING-SPEC.md#9-config-keys-that-change-a-grade).
- Credentials live in that same `config.json` **in clear** — it is your server's
  file. All optional: `spotify_client_id` / `spotify_client_secret` (advisory and
  genre by ISRC), `discogs_token`, `lastfm_api_key`, `rym_cookie`,
  `acoustid_api_key`, `ai_base_url` / `ai_api_key` / `ai_model` (script 17 and
  the genre ranking), `soulseek_username` / `soulseek_password`.
- `GET /api/sources/health` lists every external source the app can ask (six
  lyrics, six advisory, eleven genre, four metadata, one links — 28 rows) with
  what each needs (`?probe=1` tests them). The RateYourMusic cookie is only
  needed for the *scrape* fallback, because MusicBrainz states the RYM
  album/artist page as a URL relation for well-known releases.

## What the app does

### Library, search and identity

Artists → albums → tracks, with live grade/audit badges, a search box, a *fail
only* filter, bulk tag tools, custom tag columns and sortable/resizable columns
(year, grade, audit, genre, advisory, duration, bitrate, dynamic range…); music
videos are first-class tracks. The top search bar searches the library (with
`composer:`, `person:`, `genre:` and `tag:` prefixes) or MusicBrainz, and
`/mb/search` is a full in-app MusicBrainz browser (artists, release groups,
releases, recordings) with *Auto-import* and wishlist actions. Entity pages carry
grading and auditing detail, MusicBrainz + RateYourMusic links, cover
upload/search, Wikipedia descriptions, manual tag editing, a lyrics editor, and a
**Credits** view built from MusicBrainz `artist-rels` (falling back to the file's
own PERFORMER/COMPOSER/… tags, and saying so). **More like this** and **Home**
are computed locally from the library's own tags (genre and family, mood, energy,
era, artist) — no provider, no model, no network.

### Player

A persistent player bar (queue, drag-reorder, shuffle, repeat-one, speed, sleep
timer, ReplayGain, visualizer, app-wide volume) plus a fullscreen player with
animated karaoke lyrics. ReplayGain is applied through the WebAudio gain stage in
**track**, **album** or **off** mode (`replaygain_mode`) with a preamp
(`replaygain_preamp_db`, ±24 dB); a file without ReplayGain tags is measured on
the fly with ffmpeg's EBU R128 meter when `replaygain_analyze_missing` is on
(default), cached in `.mlo/data/replaygain.json`, with clip protection. Music
videos play at the correct aspect ratio, incompatible codecs are transcoded to
fragmented MP4 (`GET /api/videos/stream?transcode=1`), and the keyboard shortcuts
(`F`, `/`, `?`, Space, ← →, `[` `]`, `0`) live in
`web/src/components/Shortcuts.tsx`.

### Import

Drag & drop uploads, a watched import folder, staged `.mlo/downloads` or the
Soulseek paths, all through one pipeline (`server/imports.py`). **AcoustID
fingerprint matching** tells you which release the *audio* is, not what the tags
claim (needs `acoustid_api_key` and `fpcalc`). The wizard's eight steps are **Select & separate → Links → Match → Covers →
Genres → Lyrics → Advisory → Finish**, and *Finish* can run the import chain or
the whole `run_all_order` over that album. The **import script chain** then runs (default `import_scripts`, i.e.
`DEFAULT_CHAIN`: 11 → 3 → 14 → 15 → 2 → 1 → 13 → 18 → 8 → 5 → 6 → 7 → 9 → 12 →
10 → 4); `import_auto_scripts` turns it off, and the chain only ever *fills* a
tag, so what you typed in the wizard survives. **Bulk import** queues several
albums with `import_bulk_concurrency` (2 by default, 1–8).

### Soulseek & wishes

A managed slskd instance (autostart, shares = the music folder, a share rescan
scheduled whenever the library changes), with search & download UI, a live status
dot, share browsing, bulk and whole-user downloads, transfer-level clearing and
staging management (`GET /api/soulseek/staging`). The **auto-importer** searches
each release by its most specific trait (a CD by its catalog number,
`soulseek_auto_cd_queries`; Digital Media by `artist album year`), gates a CD
candidate on its rip log *before* requesting any album byte
(`soulseek_auto_log_min_score`, default 100), ranks candidates towards the copy
that arrives fastest, verifies completeness (`soulseek_auto_complete_ratio`) and
losslessness, and cleans up everything a rejected candidate left behind. A
**wish** records a MusicBrainz release without downloading anything; a background
worker re-searches every open wish on `wishes_interval_hours` (default 6) and
imports a verified copy, flipping the wish to **Imported**. *Import all completed*
imports every finished download **sequentially**, with cancel finishing the album
in flight (`GET /api/soulseek/import-all/status`).

### Optimization — the 18 scripts

Optimization → *Run All* executes `run_all_order`, shipped as **11 → 3 → 14 → 15
→ 2 → 1 → 13 → 18 → 17 → 8 → 5 → 6 → 7 → 9 → 12 → 16 → 10 → 4** — everything that
moves a file first, everything that reads it last. Every script also runs on its
own, on a selection, or with its force flag from the *Re-run & overwrite* menu.

| # | Script | What it does |
| --- | --- | --- |
| 1 | Format lyrics | Canonical embedded LYRICS / `.lrc` (padding, blank lines, zero-timestamp rule, Enhanced/Extended LRC word-sync); MEDIA/SOURCE normalization |
| 2 | Format CUEs | Canonical CUE text, `FILE`-line fixes, `CD-N` sheet renaming |
| 3 | Optimize FLACs | Re-encode at `library_codec_quality`, strip padding/CUESHEET/APPLICATION and tags outside the canonical set, convert non-conforming files to `library_codec` (what `library_codec_optimize` permits) |
| 4 | Grade | The full grading battery (see the spec) |
| 5 | Process images | Covers resized/cropped (`cover_target_size`, default 1200; per-format targets), JPEG/PNG/JXL optimization |
| 6 | Audit library | AudioAuditor detectors + CD `.log` CRC verification → `AUDIT` |
| 7 | DR & ReplayGain | rsgain + simple-dr-meter tags (album gain, FLAC and MP4 alike) |
| 8 | Auto tagging | `ITUNESADVISORY`, `INSTRUMENTAL`, `MOOD`, `ENERGY`, `GENRE` |
| 9 | AccurateRip | CUETools `.accurip` generation and verification |
| 10 | Format all | Final canonical pass: `.accurip`/`.cue`/`.lrc`/tag trim + the embedded-cover policy |
| 11 | Remux videos (MKV) | Any video container → MKV, video copied bit-exact when possible, audio to FLAC, chapters kept |
| 12 | Key & BPM | librosa-backed `INITIALKEY` + `BPM` |
| 13 | Fetch lyrics | The configured synced-lyrics chain into the configured format |
| 14 | Beets tagging | Managed beets (Picard parity) with the naming script and work/movement tags |
| 15 | Release tracklist | Writes `.mlo_expected.json` (the release's own tracklist) into the album folder |
| 16 | Mood & Energy | The mood classifier alone (`MOOD` + `ENERGY`) |
| 17 | Lyrics transliterate (AI) | `TRANSLITERATION-<LANG>-LATN` / `TRANSLATION-<LANG>` tags and sidecars, re-synced at `lrc_sync_level` |
| 18 | Publish lyrics (LRCLIB) | Submits this library's lyrics for recordings LRCLIB does not have (`lrclib_auto_publish`, `force_publish`) |

Force flags, one per script: `force_lyrics`, `force_cue`, `force_tracklist`,
`force_reencode_flac`, `force_reencode_images`, `force_audit`, `force_accurip`,
`force_dr_replaygain`, `force_audiometa`, `force_mood`, `force_auto_tag`,
`force_xlit`, `force_publish`. Scripts whose feature has its own off switch are
skipped rather than run as no-ops: `dr_replaygain_enabled` (7),
`audiometa_enabled` (12), `mood_enabled` (16), `lyrics_xlit_enabled` /
`lyrics_translate_enabled` (17), `lrclib_auto_publish` (18).

By default the optimizer does **not** embed cover art — it removes it, and
covers live on disk as `cover.*` plus per-track sidecars; Settings → *Embedded
covers* (`embed_covers`, `embed_cover_jpeg_quality`, `embed_cover_resolution`)
makes script 10 embed the album cover into every track (FLAC picture, MP3 APIC,
MP4 `covr`, OGG/Opus `METADATA_BLOCK_PICTURE`). Script 14 and *Organize* apply
`naming_script`, shipped as `Artist [mbid]/[Type] date - date - Album
{country - media - catalog} [label] [release] [releasegroup]/1-01 Title
[recording] [releasegroup].flac`; `short_folder_names` trims the UUIDs to 8
characters for paths that need the room.

### Grading

**67 checks** across tracks, albums, artist folders and folders, all toggleable
on the **Grading** page with a live filter, enable/disable-all and the **Strict /
Balanced / Relaxed** presets; `grade_check_audit` is the only check that ships
**off**. A verdict is binary: an album is `PASS` only when every enabled check
passes, otherwise `FAIL` with the failed checks itemized. The summary counts
checks (`summary_pass` / `summary_total`) and reports `albums_passed` /
`albums_failed`, plus `albums_audit_failed` for albums that pass every check
while their audit is FAKE/Mix (the library badges those red on the Audit column).
A check that raises counts as *could not be evaluated* and fails, so the
percentage always covers every enabled check. Two checks follow the codec target:
`grade_check_lossless_source` stands down (and is not counted) when `library_codec`
is an uncompressed container (`wav`/`aiff`) or `keep`, and `grade_check_cd_format`
exempts a file that already is the configured lossy target. Artist folders are graded on
exactly two things — image and description — by `grade_artist()`.

**The specification** — every check id, what it asserts, its default, the
presets, the audit workflow, tag families, quality bars, score semantics and a
runbook — is
[`docs/OPTIMIZATION-GRADING-SPEC.md`](docs/OPTIMIZATION-GRADING-SPEC.md).

### Export, playlists, offline

**Export** writes a playlist, albums, artists, tracks or the whole library to a
drive as MP3 (VBR/CBR or custom), AAC, Opus, Vorbis, WAV, AIFF, ALAC, WavPack,
WMA or a bit-exact `copy`, in `artist_album`, `album`, `flat` or `mirror` layout
(`export_codec`, `export_structure`, `export_dest`, `export_subfolder`). Covers,
ID3v2.3 plus optional ID3v1, ReplayGain track and album tags, `.m3u8` playlists,
`.lrc`, `.cue`, `.log`, descriptions and the artist image travel with the files,
and every written file is re-opened and verified. Sync mode (`export_prune`)
removes audio the run did not write; exporting *into* the music folder is
refused. **Playlists** are manual (drag-reorder, favourites, `.m3u8`
import/export) or smart, driven by saved grade/audit/tag filters.

**Offline**: "Download" caches a track's audio in the service worker's media
cache and warms the album/artist payloads around it, so the UI opens and a
downloaded album plays with the server down. In the Tauri shells, where no service
worker runs, a smaller JSON cache (GETs only, 512 KiB per entry, 3 MiB total) and
`blob:` playback cover the same case, and an **Offline** pill says when a stored
answer is being shown. Writes, Soulseek, imports and exports still need the
server.

### Notifications and languages

The backend publishes `wish_found`, `download_done` and `import_ready` on
`/ws/events`; each client keeps that socket open and raises an OS notification
(Tauri's plugin on desktop and mobile, the Web Notification API in the browser)
with an in-app toast when permission is refused, and `notify_wish_found`,
`notify_download_done`, `notify_import_ready` decide which kinds are published.
This is **not** remote push. The UI ships in six languages — English, Español,
Français, Deutsch, 日本語, Português (Brasil) — picked as this browser's own choice
(`localStorage: mlo.locale`), then the server's `ui_locale`, then
`navigator.language`, then English; the deeper tool pages are still English.

---

## Clients

The same React build runs in five targets, and **every one is a client of a server
you run**: the Docker container on `127.0.0.1:8000`, a machine on the LAN, or a
Tailscale name. No shell carries Python, a sidecar or a port of its own; each talks
HTTP to the address it was set up with (`localStorage: mlo.server`).

| Target | Built by | Artifact | What it is |
| --- | --- | --- | --- |
| Browser | the server (`web/dist`) | — | the app itself, over HTTP/S |
| Windows / macOS / Linux | `npx tauri build` | `.msi` + NSIS `.exe`, `.app` + `.dmg`, `.deb` + `.AppImage` | client of a server you run |
| Android | `npx tauri android build --apk --debug` | debug-signed APK | client of a server you run |
| iOS | `npx tauri ios build --target aarch64 --no-sign` | unsigned IPA | client of a server you run |

Both halves are built locally — `cd web && npm install && npm run build` (the
`web/dist` the shell wraps), then `cd ../desktop && npm install && npx tauri
build`. `.github/workflows/desktop.yml` builds the three desktop bundles
(`npx tauri build --bundles …`), `mobile.yml` the Android APK and the iOS IPA,
and `release.yml` — on a `v*` tag — attaches all of them plus the Windows zip
and the GHCR image.

- **Desktop** keeps only the shell furniture: a tray icon (Open la musica /
  Auto-start on login / Exit), a native folder picker and the window. Quitting it
  stops nothing but itself — the server's lifecycle belongs to whoever runs the
  container — and mobile compiles the same crate without the tray and the folder
  picker.
- **Capabilities are the server's, not the app's.** `GET /api/capabilities`
  (`platform`, `backend`, `python` plus the per-feature rows) is derived from
  what the server can actually do — can it spawn a subprocess, are
  ffmpeg/flac/fpcalc/rsgain present — so a full container and a NAS container
  report different lists, and the Dependencies page and both setup wizards read
  that report instead of offering an Install button that cannot succeed.
- A shell that has never been set up opens **its own wizard**
  (`web/src/pages/ClientSetup.tsx`: Server → Account → Notifications → Done)
  before anything else. *Test* probes `${address}/api/health` with a 3 s
  deadline and **Next** stays disabled until a real la musica `version`
  answered; the address is saved per device as `localStorage: mlo.server`, and
  Settings → Security offers *Run setup again* for every client (the browser
  included).
- Both mobile artifacts are **sideload builds, signed by nobody here**: the APK
  is a debug build (the SDK debug keystore), the IPA is built `--no-sign`, and a
  plain-http server needs the platform's exemption
  (`desktop/src-tauri/Info.plist` sets `NSAllowsArbitraryLoadsInWebContent`;
  `mobile.yml` flips `usesCleartextTraffic` for Android).
  `tools/make_sidestore_source.py` writes the `source.json` the release
  publishes, so a sideloading tool gets the name, version, bundle id, icon and
  download URL from the IPA itself.

## Terminal entry point

```bash
python -m mlo
```

That is the classic console menu — scripts 1–18, Run All, the config editor and
the dependency table. It is **not** stdlib-only: `mlo` imports `mutagen` for
every tag operation, so run it from the same environment that has
`server/requirements.txt` installed (script 14 additionally needs
`server/beetscfg` plus a vendored beets, and scripts 9/10 their optional
modules). A missing module makes its script unavailable and fails loudly instead
of reporting a clean "0 processed" run.

## Security & accounts

Everything the API can do — read the library, rewrite tags, move and delete files,
start downloads — is one password away from anyone who can reach the port. That is
what the gate covers; it is not a UI lock. The keys it reads:

| Key | Default | What it does |
| --- | --- | --- |
| `auth_mode` | `auto` | `auto` = gate ON when `server_host` is not loopback; `required` = gate ON always; `off` = gate off for loopback only |
| `server_host` | `127.0.0.1` | where the server binds — and the address the gate reads |
| `server_port` | `8000` | the port |
| `auth_username` | `""` | the name shown on the login screen; `POST /api/auth/login` accepts it as an optional `username` |
| `auth_password_hash` | `""` | PBKDF2-HMAC-SHA256, written as `pbkdf2$<rounds>$<salt-hex>$<hash-hex>` |
| `auth_session_days` | `30` | how long a session stays valid |
| `server_public_url` | `""` | the address clients should dial when it is not the page's own origin |

`auto` follows the bind: `127.0.0.1`, `::1` and `localhost` keep a single-user
install password-free; anything else asks for a login — **from clients**. Who
counts as a client is decided per request from the peer address, because the
machine running the server is not one: loopback, one of this machine's own
addresses and the gateway of the server's own container are let in, while a phone
or a laptop on the network signs in. `X-Forwarded-For` is never trusted, and
`off` on a non-loopback bind is treated as `required` (with a startup warning).

- **A login**: PBKDF2-HMAC-SHA256, 600 000 rounds, random salt, constant-time
  comparison, minimum 8 characters. `auth.db` also holds a `users` table; a
  session carries the username it was opened for, and every playlist, like,
  favourite and trash folder (`<music>/.mlo/trash/<user>/`) is scoped by it —
  downloads stay one shared queue, because slskd is. Settings → Security adds and
  removes users (the last one is refused), and five consecutive failures make an
  address wait 30 s, doubling to 15 minutes.
- **Sessions** are 32-byte tokens; only their SHA-256 is stored, in
  `<music>/.mlo/data/auth.db`. The token travels as `Authorization: Bearer`, as
  the HttpOnly `mlo_session` cookie, or as `?token=` for the WebSockets (which
  check it the same way and close with code **4401** when it is missing, expired
  or revoked). **Without a session** only `/api/health`, `/api/auth/status`,
  `/api/auth/login`, `/api/auth/setup` and the static shell answer; every other
  route answers `428` (`{"needs_setup": true}`) until `POST /api/auth/setup` sets
  the password.
- **First run over the network**: start bound to the address you will use
  (`server_host: 0.0.0.0`, or `MLO_SERVER_HOST=0.0.0.0` in the container), open
  the app from a client and claim the server with a password; each client signs in
  once and keeps its own session.

### The honest limits

- **One password, one user.** No roles, no permissions, no signup — every
  session is the owner, and two people sharing a server share the password.
- **The app does not terminate TLS.** No certificate handling anywhere; over
  plain HTTP the token and the password travel in the clear. Put it behind a
  reverse proxy (nginx, Caddy, Traefik) or a mesh VPN when it leaves the LAN, and
  use `server_public_url` to name the https address clients should dial.
- **The gate follows the configured `server_host`, not the socket.** The Docker
  image seeds that key from `MLO_SERVER_HOST`/`MLO_SERVER_PORT`, so config, bind
  address and gate cannot disagree there. Starting the app by hand with a different `uvicorn --host` does
  disagree — set `server_host` (or `auth_mode: required`) to match.
- **Notifications are not push** (they reach a client with la musica open), and
  **anything already on the host** — another local user, a container neighbour —
  reads `.mlo/data/auth.db` and the config: the gate defends the network
  boundary, not a hostile local account.
- **Docker cannot run the Windows-only tools** (CUETools, AudioAuditor,
  Logchecker + php), so AccurateRip generation, the Logchecker grade and the
  AudioAuditor audit are unavailable there — `GET /api/capabilities` says what a
  given server can actually do.

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
mlo/         core engine (imports mutagen for every tag operation — the CLI needs
             the same environment as the server): grader, audit, flac, images,
             lyrics (word-sync + the provider chain), lyrics_xlit, genres, moods,
             artistdata, acoustid, cue, accurip, loudness, autotag, remux,
             fetchdeps, naming, discs, stats
desktop/     Tauri v2 shell — desktop bundles plus Android/iOS clients
tools/       test-library generator and test suites; docs/ holds the contract
```

Where the app stores what it fetches (all under `<music>/.mlo/`):

| File | What it holds |
| --- | --- |
| `data/config.json`, `data/playlists.db`, `data/auth.db` | all settings; playlists, likes and favourites (per user); sessions and user rows (the token's SHA-256, never the token) |
| `data/artwork.json`, `data/metadata_review.json` | provenance for artist images/descriptions; staged metadata candidates |
| `data/replaygain.json`, `data/audit_evidence.json` | on-demand ReplayGain measurements (invalidated on size/mtime change); what each `AUDIT` verdict was proved on |
| `data/rym_cache/`, `data/lyrics_ai_cache/`, `data/wishes.db`, `data/update_check.json` | RYM genre pages (30 days); AI answers keyed by prompt hash; the wishlist; the last GitHub release check (6 h) |
| `downloads/`, `trash/<user>/`, `Artists/<Artist>/artist.jpg`, `description.txt` | Soulseek staging; deleted files per user; artist image and description |
| `<album>/description.txt`, `<album>/.mlo_expected.json` | album description; the release's own tracklist (script 15) |

## API overview (selected)

| Endpoint | Purpose |
| --- | --- |
| `GET /api/library` | tag-rich library tree (grades, audits, tags, tech info; gzipped) |
| `GET /api/health` `GET /api/version` | liveness (`status`, `version`); `{version, latest, update_available, release_url, checked_at, source}` cached 6 h, `latest: null` when GitHub is unreachable — a container reports its image's `MLO_VERSION` |
| `GET /api/auth/status`, `POST /api/auth/setup` `…/login` `…/logout` `…/password` `…/revoke-all`, `GET …/sessions`, `GET/POST /api/auth/users`, `DELETE …/{name}` | the gate's state (`required`, `has_password`, `username`, `host`, `public_url`, `session_days`); first-run password, sign in (optional `username`), sign out, change, revoke everywhere, session count, user management |
| `GET /api/library/layout` | read-only layout scan (misplaced audio, stray files, empty albums, `wrong_case`); `GET /api/home` and `GET /api/recommend` back the Home shelves and "More like this" |
| `GET /api/album` `GET /api/artist` `GET /api/artist/artwork`, `GET /api/stream` `GET /api/videos/stream` `GET /api/videos/meta` `GET /api/videos/thumb` | entity details, stored artist image/description + provenance and the artist's own grade; audio/video streaming (Range; `?transcode=1`), codec probe, scrub frames |
| `GET /api/tags` `POST /api/tags/bulk` `…/videos/tag`, `POST /api/run`, `POST /api/organize` | per-track tag read view; bulk tag surgery; video tag writes; run scripts 1–18; apply the naming script (dry-run supported) |
| `POST /api/export`, `GET /api/export/codecs` `…/drives` `…/defaults` | multi-format export plus its codec table, drives and saved defaults |
| `GET/POST/PATCH/DELETE /api/wishes…`, `POST /api/wishes/{id}/search` `…/search-all` `…/reconcile` `…/import` | the wishlist and its worker |
| `GET /api/sources/health` `…/{id}`, `GET /api/capabilities`, `GET /api/dependencies` | every external source with its `needs`/`configured` state (`?probe=1`); what this server can run; the tool table with installed/pinned/upstream versions |
| `POST /api/mb/match` `…/assign` `…/auto-import` `…/advisory/fetch`, `POST /api/genres/import`, `GET /api/genres/facets` | MusicBrainz matching, tag writes, queued downloads, advisory resolution; genre import and Genres-page facets |
| `POST /api/import/upload` `…/commit` `…/acoustid` `…/finish` `…/bulk`, `POST /api/lyrics/auto` `…/write` `…/embed` `…/wordsync`, `GET /api/lyrics/find` `…/providers` | the import pipeline, its fingerprint step, the chain and the bulk queue; the lyrics chain, previews and writes |
| `GET /api/cover/search` `…/sources`, `POST /api/cover` `…/fromurl` | cover meta-search, upload and save-as-cover |
| `POST /api/soulseek/download-bulk` `…/download-user` `…/search/cancel`, `GET /api/soulseek/ready`, `POST …/import-one` `…/import-all` `GET …/import-all/status` `POST …/import-all/cancel` | Soulseek downloads, the ready list and the sequential importer |
| `WS /ws/progress` `WS /ws/events` | live script progress; the notification channel (`wish_found` / `download_done` / `import_ready`, `?since=` replays the 100-event ring) |

## Tests & development

```bash
python tools/make_test_library.py   # synthetic library for end-to-end runs
python tools/test_remux.py          # video remux suite (VOB/MKV/AVI/WebM fixtures)
python tools/test_lyrics_xlit.py    # script 17: alignment, romanization, cache (offline)
python tools/test_script_menus.py   # every script menu agrees (numbers, labels, Run All order)
python tools/smoke_api.py           # route smoke test against a running backend
python tools/check_versions.py      # the release gate: every version string agrees
```

- The 57 `tools/test_*.py` suites are standalone scripts (`python tools/test_x.py`;
  `sys.exit(2)` means "skipped", e.g. a missing toolchain) and mostly run offline
  with providers stubbed. The frontend gate is `cd web && npx tsc -b && npx oxlint
  && npm run build`, plus `node tools/test_i18n.cjs` for the locale bundles.
- The browser checks need a running backend serving `web/dist` and Playwright
  (`npm i -D playwright`): `tools/check_menus.cjs` walks every route and
  `tools/check_responsive.cjs` re-measures every top-level route at 390×780,
  834×1112 and 1440×900. They exit **2** when Playwright is not resolvable, so a
  missing browser is never mistaken for a defect.
- `tools/check_versions.py` compares the app version in `mlo/__init__.py`,
  `tauri.conf.json`, `Cargo.toml`, both `package.json`s, the Dockerfile's
  `MLO_VERSION`, this README's header and `desktop/README.md`. CI
  (`.github/workflows/ci.yml`) runs the Python suites, the frontend gate and
  `cargo check` for the Tauri shell on every push and PR; the checks needing a
  live backend are manual.

## Licence, credits and donations

MIT — see [`LICENSE`](LICENSE). The services and projects the app is built on are
listed in the app's bottom-left credits popover, in
[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) and in
`web/public/credits.json`. `/donations` carries two addresses with a copy button
each — Litecoin `LRisZa9HYBKE2sUc3VELYZq2WnyYtG6Jvu`, Bitcoin
`bc1qf2snsus59ydvmk8rwp09e698gxjdlmyxyrnycu` — and **nothing is gated behind it**.

Legacy v1 (the Tkinter app, CLI and PyInstaller/Inno packaging) is archived on
the `archive/legacy-v1.7` branch.
