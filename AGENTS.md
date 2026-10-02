# Notes for coding agents working in this repo

## Never host the app on port 8000

The owner runs la musica on **127.0.0.1:8000** (the Docker container `la-musica`,
with `F:/media/music` mounted at `/music`). A scratch server on 8000 either fails
to bind or, worse, tools and checks talk to *their* live instance.

Use a free port from **8011 up**:

```bash
python -m uvicorn server.main:app --host 127.0.0.1 --port 8011
```

## Always point a test run at a scratch music folder

The app writes its state beside the library it is given, so a test that runs
against the real folder can touch the owner's wishes, playlists and files:

```bash
MLO_MUSIC_FOLDER=F:/tmp/mlo-<what-you-are-testing> \
  python -m uvicorn server.main:app --host 127.0.0.1 --port 8011
```

When you are done: stop the process and delete the scratch folder. A scratch
scope opens on the setup wizard — flip `first_run_done` through
`POST /api/config` with the folder it belongs to
(`{"music_folder": "F:/tmp/mlo-<what-you-are-testing>", "first_run_done": true}`;
the flag alone is not saved, and `tools/check_*.cjs` refuses a server still on
the wizard) rather than clicking through it.

## The dev bed, for work that needs the app running by hand

`python dev.py` does all of the above in one command — a throwaway library
under `local/dev/music` (gitignored), a port from 8011 up (the bed asks the port,
so it can never land on 8000 or on one you are already using), the wizard already
flipped, the backend under `--reload` watching only `server/` and `mlo/`, and the
UI on vite with hot reload proxying `/api` and `/ws` to it. Its logs are
`local/dev/logs/{server,web}.log`.

```bash
python dev.py --no-open --no-tray           # what an agent wants: console, no browser
```

Ctrl+C stops both halves — and the TREE, not just the leader: `uvicorn --reload`
and `npm run dev` each leave a child holding the inherited listening socket, so a
plain kill leaves the port bound to a dead pid.

## Commands worth knowing

- The suites: `python tools/test_*.py` — one per area, each exits non-zero on
  failure. Run the ones your change touches, and add a case there rather than a
  new suite.
- Every version copy must agree: `python tools/check_versions.py [vX.Y.Z]`.
- Release notes go in **`docs/release-notes/`** (as
  `release-notes-<version>.md`), never in the repo root — nothing reads them at
  build time, they are the record of what a release changed. `git mv` keeps the
  history when one moves.
- Web: `cd web && npm run build` (typecheck: `npx tsc -b`).
- UI checks: `node tools/check_*.mjs <payload.json>` are payload-driven (no server);
  `tools/check_*.cjs` drive a running scratch server.

## House rules that came from real bugs

- **Stage files explicitly.** Never `git add -A` while other writers are active —
  a sweep mid-flight once committed a half-finished file.
- **Stay album-scoped.** A script or import that walks the music folder must check
  `config.get("targets")` first; only a deliberate Run All walks the library.
- **Writers fill, they do not overwrite** — except the four families an import
  decides (lyrics, genre, advisory, embedded cover), which
  `server/imports.drop_arrived_values` clears first so the import's values land.
- **A behaviour change gets a rule.** `docs/OPTIMIZATION-GRADING-SPEC.md` is the
  contract users check the app against; `README.md` and the GitHub description
  follow it.
