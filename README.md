# la musica
**v5.5.0** — a self-hosted app that manages, optimizes, audits and grades your music library, from a browser or a desktop window.

FastAPI backend + React UI over the `mlo` engine: a library of artists → albums → tracks and music videos, album covers, a multi-source lyrics chain, and MusicBrainz / Discogs / AcoustID identity. All app state — config, the beets library, caches, trash, runtime-installed tools — lives in one `.mlo` folder beside your music.

- Contract: [`docs/OPTIMIZATION-GRADING-SPEC.md`](docs/OPTIMIZATION-GRADING-SPEC.md) · Release notes: [`docs/release-notes/`](docs/release-notes/)
- Licence MIT · Third-party credits: [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md)

## Quick start
**Docker (preferred)** — the only supported server install:
```bash
docker compose up -d      # build from source; `docker compose pull` fetches the GHCR image instead
docker compose logs -f    # follow the backend log
```
Edit the committed `./music:/music` bind mount to a real absolute host path first, then open <http://localhost:8000>. `/music` holds the library **and** all app state, and the container runs as uid/gid **1000**, so the mount must be writable by that user. Image `ghcr.io/dillydalli3r/la-musica:latest`, container `la-musica`; `MLO_MUSIC_FOLDER=/music` and `MLO_SERVER_HOST=0.0.0.0` are the only env vars it needs. Tools (ffmpeg, flac, rsgain, …) install from **Settings → Dependencies**.

**From source**:
```bash
python -m pip install -r server/requirements.txt
cd web && npm install && npm run build && cd ..
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000   # http://127.0.0.1:8000
python -m mlo    # the console menu (the 21 optimization scripts), run from the server's environment
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
- **Library** — artists → albums → tracks with grade/audit badges, five views (Grid, Compact, Albums, Artists, Tracks), sort/columns/presets, a query builder, bulk tag tools and music videos.
- **Import** — archives, folders and uploads all run one pipeline through the eight-step wizard (Select → Links → Match → Covers → Genres → Lyrics → Advisory → Finish), then the import script chain (the Finish step ticks the chain's scripts and can force a re-run of the ones it ticks). A whole-CD image rip (one `.flac` plus its `.cue`) is split into one file per track on the way in. AcoustID matching, MBID assignment, and a framework album for a release you add before its audio exists.
- **Optimization** — 21 scripts, Run All or one at a time: lyrics, CUEs, FLAC re-encode, covers, audits, DR/ReplayGain, AccurateRip, key/BPM, mood/energy, beets tags, transliteration, video remux, layout, tag strip, AcoustID pairs. Each script, its force flags and its order: the spec.
- **Grading** — 67 checks over tracks, albums, artist folders and folders, toggleable per check with Strict/Balanced/Relaxed presets; Home and the Library open with the verdict and what fails.
- **Export & storage** — genre browse; export as MP3/AAC/Opus/Vorbis/FLAC-copy in `zip` or server-side, with the export EQ bake; the storage card and the library stats.

## Clients
The same React build runs in the browser (served by the server) and in the
desktop shell (Windows, macOS, Linux). The desktop install asks on its first
run how it should get its server: run its OWN backend — a frozen Python
service bundled with the app, spawned on a free loopback port from 8011 up,
serving the UI and the API from the same origin so the session cookie works —
or connect to a server you run yourself (Docker, a laptop, a home server). The
tray's "Use the built-in backend" switches between the two afterwards.

## Security
Everything the API can do is one password away from anyone who can reach the port. `auth_mode: auto` (default) turns the login gate ON for any non-loopback bind; one password (PBKDF2-HMAC-SHA256, 600 000 rounds), sessions as `Authorization: Bearer` / HttpOnly cookie tokens stored hashed in `<music>/.mlo/data/auth.db`; five failed logins make an address wait. The app does **not** terminate TLS — put it behind a reverse proxy or a mesh VPN off-LAN.

## Tests & development
`python tools/test_*.py` — one standalone suite per area (`sys.exit(2)` means "skipped"). Frontend gate: `cd web && npx tsc -b && npx oxlint && npm run build`. Browser checks (`node tools/check_*.cjs`) drive a running scratch server plus Playwright; `python tools/check_versions.py` is the release gate. Repo layout: `web/` UI · `server/` FastAPI backend · `mlo/` core engine · `desktop/` Tauri shell · `tools/` suites and checks · `docs/` the contract.
