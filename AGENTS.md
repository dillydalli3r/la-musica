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
`POST /api/config` (`{"first_run_done": true}`) rather than clicking through it.

## Commands worth knowing

- The suites: `python tools/test_*.py` — one per area, each exits non-zero on
  failure. Run the ones your change touches, and add a case there rather than a
  new suite.
- Every version copy must agree: `python tools/check_versions.py [vX.Y.Z]`.
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
