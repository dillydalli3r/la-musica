# la musica
**v2.0.1** — a self-hosted app that manages, optimizes, audits, grades, downloads and plays your music library, from a browser, a desktop window or a phone.

FastAPI backend + React UI over the `mlo` engine: music and music videos, playlists, favourites, artist artwork and biographies, a multi-source lyrics chain, and MusicBrainz / Discogs / AcoustID identity. All app state — config, playlists, the beets library, caches, downloads, trash, runtime-installed tools — lives in one `.mlo` folder beside your music.

- Contract: [`docs/OPTIMIZATION-GRADING-SPEC.md`](docs/OPTIMIZATION-GRADING-SPEC.md) · Release notes: [`docs/release-notes/`](docs/release-notes/)
- Licence MIT · Third-party credits: [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md)

## Quick start
**Docker (recommended)** — the only supported server install:
```bash
docker compose up -d      # build from source; `docker compose pull` fetches the GHCR image instead
docker compose logs -f    # follow the backend log
```
Edit the committed `./music:/music` bind mount to a real absolute host path first, then open <http://localhost:8000>. `/music` holds the library **and** all app state, and the container runs as uid/gid **1000**, so the mount must be writable by that user. Image `ghcr.io/dillydalli3r/la-musica:latest`, container `la-musica`; `MLO_MUSIC_FOLDER=/music` and `MLO_SERVER_HOST=0.0.0.0` are the only env vars it needs. Tools (ffmpeg, flac, yt-dlp, …) install from **Settings → Dependencies**. MusicBrainz is the only genre and web-rating source: the app does not scrape RateYourMusic or Album of the Year (both were removed 2026-10-12 — their terms forbid it), so no Cloudflare solver service, session cookie or browser fetch is needed or shipped.

**From source**:
```bash
python -m pip install -r server/requirements.txt
cd web && npm install && npm run build && cd ..
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000   # http://127.0.0.1:8000
python -m mlo    # the console menu (scripts 1–26), run from the server's environment
```

**Editing the app** — `python dev.py`, from the repo root (double-clicking it
does the same on Windows), is the whole dev bed in one command: the backend under
`--reload` watching only `server/` and `mlo/`, the vite server with hot reload
proxying `/api` and `/ws` to it, and a tray to open the app / the library folder /
the logs, restart it or quit. **Which library** is `dev.config.json` beside it —
one key, `music_folder`, machine-local and gitignored, `local/dev/music` when
unset (that one is wiped by `--fresh`). Point it at a real library and the bed
still writes nothing of its own there: that `.mlo` is shared with whatever else
uses the library, so it reads that install's settings as they are — login gate
included — and puts back the one value a Windows run re-stamps as its own. It
never uses port 8000 — that is the live install — and picks 8011 (backend) and
5181 (UI) up. `--no-web`, `--no-reload`, `--no-tray`, `--music DIR` and `--help`
are there for the rest.

## What it does
- **Library** — artists → albums → tracks with grade/audit badges, five views (Grid, Compact, Albums, Artists, Tracks), sort/columns/presets, a query builder saved as smart playlists, bulk tag tools, half-star ratings, favourites, podcasts and music videos.
- **Player** — a persistent bar and a fullscreen player: queue, gapless playback, `infinite_playback` similarity queue, ReplayGain (track/album/off), equalizer, sleep timer, visualizer, synced **or plain** lyrics, and a per-device accent colour. It remembers where you were.
- **Import** — archives, folders and uploads all run one pipeline through the nine-step wizard (Select → Links → Match → Covers → Genres → **Ratings** → Lyrics → Advisory → Finish), then the import script chain (the Finish step ticks the chain's scripts and can force a re-run of the ones it ticks). The Finish step also offers **Close**, which settles and files the album but runs no script. Ratings are a step and an import-policy family of their own: MusicBrainz's per-recording score for each track plus the release group's for the album, written fill-only and shown per track with the tier that answered. The album's expected tracklist (`.mb_expected`) is re-recorded the moment an import settles which release the album is — a fetch, an auto-match, an AcoustID accept, a pinned re-link, a step advance — so the Library's gap view never lags the identity. A whole-CD image rip (one `.flac` plus its `.cue`) is split into one file per track on the way in. AcoustID matching, MBID assignment, and a framework album for a release you add before its audio exists. Manual Soulseek downloads are imported file by file: the downloads tab's picker expands an entry, its checkboxes pick the exact tracks to import, and one identify row per entry names the release — prefilled from the files' own tags, with **Detect with AcoustID**, a MusicBrainz search (artist/album or catalogue number), or a pasted link (spec R433). `SOURCE` is never autofilled: the wizard's Match step (or the album page) is where you say where the copy came from, with the release's own store — or the network that served the download — offered as the suggestion and nothing written until you state a value (R438).
- **Optimization** — 25 scripts, Run All or one at a time: lyrics, CUEs, FLAC re-encode, covers, audits, DR/ReplayGain, AccurateRip, key/BPM, beets tags, transliteration, artist images (fetch then re-fit), artist bios, layout, tag strip, web ratings. The chain runs as a graph — one album-writing step at a time, with the artist-art and submission steps running beside it, and an import's advisory ratings waiting for the lyrics they are decided from. A run reads each file once and each folder once per chain, and one MusicBrainz browse answers a whole album's genres and ratings (spec R399/R400); the queued imports run on lanes, one chain per album (R403). Nothing an import did not write is deleted: SOURCE stripping is opt-in and off by default (R398). **Optimize library layout** removes a folder to the Trash when everything inside it may be named one by one — junk, and the app's own `artist.jpg`/`description.txt` — so a dead artist folder or a folder of strays is settled in one run instead of being reported forever, while a folder holding a release's own audio, artwork or `.cue`/`.log`/`.accurip` is reported with the reason it stayed (R437). Each script, its force flags and its order: the spec.
- **Grading** — 73 grading keys over tracks, albums, artist folders and folders (64 checks + 9 file categories), toggleable per check with Strict/Balanced/Relaxed presets; Home and the Library open with the verdict and what fails.
- **Discover & export** — genre browse and recommendations from the library's own tags and from online providers (MusicBrainz for track genres and web ratings, and browsable by genre); export as MP3/AAC/Opus/Vorbis/FLAC-copy in `zip` or server-side, with a codec's own quality presets (MP3 V0…V5, a custom bitrate, or `copy` for no transcode) and one "set as default" behind both the Export page and the player bar's per-track panel; offline downloads; notifications (including Web Push) in six languages.
- **Home, online** — below the library's own shelves the Home page carries *Trending now* (Apple Music's most-played, keyless; Deezer's chart when Apple answers nothing), *New & notable* (Deezer's current album chart) and a library-seeded *For you*. Each shelf is its own request: a provider that is away leaves its shelf out rather than blanking the page, and the whole section disappears when Discovery is off (spec R407).
- **Scrobbling & submissions** — Last.fm scrobbling sends a now-playing notice when a track starts and one scrobble once the play reaches **70 % of the track** (`lastfm_scrobble_percent`, clamped to Last.fm's own 50 % floor; a track under 30 s is never scrobbled) — a scrobble the service or the network refuses is queued and retried, never dropped. MusicBrainz submissions write your ratings back: OAuth2 login, ONE request per album (its recordings plus its release group), values snapped to MusicBrainz's 20-point grid (a value that would snap to 0 is never sent). While logged in, rating a track or an album queues that album for upload by itself, and the panel's **Upload all ratings** queues every album the library has a rating for — both report per album what was sent and what was skipped. Last.fm's Connect now polls while you approve in the browser (no second press), and MusicBrainz's takes the code or the whole redirect URL. Both are set up in **Settings → Connections** or the first-run wizard's **Connections** step, each with a step-by-step guide; both are optional and nothing in the library needs them (spec R408/R409/R434/R436).

## Clients
The same React build runs in every target. The desktop install (Windows,
macOS, Linux) asks on its first run how it should get its server: run its OWN
backend — a frozen Python service bundled with the app, spawned on a free
loopback port from 8011 up, serving the UI and the API from the same origin so
the session cookie works — or connect to a server you run yourself (Docker, a
laptop, a home server). The tray's "Use the built-in backend" switches between
the two afterwards. A shell pointed at a server behaves like the other mobile
clients: browser (served by the server) · the desktop shell · an unsigned
Android APK · an unsigned iOS IPA, published as a SideStore/AltStore source:
`https://github.com/dillydalli3r/la-musica/releases/latest/download/source.json`.

## Security
Everything the API can do is one password away from anyone who can reach the port. `auth_mode: auto` (default) turns the login gate ON for any non-loopback bind; one password (PBKDF2-HMAC-SHA256, 600 000 rounds), sessions as `Authorization: Bearer` / HttpOnly cookie tokens stored hashed in `<music>/.mlo/data/auth.db`; five failed logins make an address wait. The app does **not** terminate TLS — put it behind a reverse proxy or a mesh VPN off-LAN.

## Tests & development
`python tools/test_*.py` — one standalone suite per area (`sys.exit(2)` means "skipped"). Frontend gate: `cd web && npx tsc -b && npx oxlint && npm run build`. Browser checks (`node tools/check_*.cjs`) drive a running scratch server plus Playwright; `python tools/check_versions.py` is the release gate. Repo layout: `web/` UI · `server/` FastAPI backend · `mlo/` core engine · `desktop/` Tauri shell · `tools/` suites and checks · `docs/` the contract.
