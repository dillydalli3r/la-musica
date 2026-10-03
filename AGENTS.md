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

`python dev.py` does all of the above in one command — a library from
`dev.config.json` beside it (`music_folder`; the bed's own scratch folder under
`local/dev/music` when unset, and `--music` overrides for one run), a port from
8011 up (the bed asks the port, so it can never land on 8000 or on one you are
already using), the wizard already flipped, the backend under `--reload` watching
only `server/` and `mlo/`, and the UI on vite with hot reload proxying `/api` and
`/ws` to it. Its logs are `local/dev/logs/{server,web}.log`.

A library the bed does not own (the real one, say) is never written to by the
bed: it withholds the env vars the app would seed into that shared config, and
puts back the one value the app itself re-stamps on every start. It records that
value in a marker beside the config before the app starts, so a run killed
before it could put it back (Task Manager, a second Ctrl+C) is healed by the
next run instead of being read as the library's own value.

```bash
python dev.py --no-open --no-tray           # what an agent wants: console, no browser
```

Ctrl+C stops both halves — and the TREE, not just the leader: `uvicorn --reload`
and `npm run dev` each leave a child holding the inherited listening socket, so a
plain kill leaves the port bound to a dead pid.

## Commit locally; release only when the owner says so

Work lands as **local commits on `main`** — explicit `git add`, a message that is
the record of what changed and what proved it. That is where a task ends:
**do not `git push`, tag or cut a release until the owner says the state is good.**
They run the app from this repo, so an unreleased change is already live for
them; releasing is their decision, not the last step of a task.

When they do ask for one:

1. `python tools/check_versions.py vX.Y.Z` — every copy that must agree (10 of
   them: `mlo/__init__.py`, `Dockerfile`, `README.md`, `desktop/README.md`,
   `desktop/package.json`, `web/package.json`, `desktop/src-tauri/Cargo.toml`,
   `Cargo.lock`, `tauri.conf.json` ×2) is listed, and the run fails until all say
   the same version. Bump first, then run it again with no argument.
2. Release notes: `docs/release-notes/release-notes-<version>.md`, never the repo
   root — nothing reads them at build time, they are the record of what a release
   changed. `git mv` keeps the history when one moves.
3. `git push origin main`, then tag and push it
   (`git tag -a vX.Y.Z -m "la musica X.Y.Z" && git push origin vX.Y.Z`): the tag
   runs `.github/workflows/release.yml` — suites, then the web/desktop/mobile/
   docker builds and the published GitHub release (~10 min). Watch it with
   `gh run watch <id>`; a red job there is the release, not the change.

## Commands worth knowing

- **Suites**: `python tools/test_*.py` (one per area) and `node tools/test_*.cjs`.
  Each exits **0 pass · 1 failed · 2 cannot run here** (no `flac.exe`, no
  Playwright, no server) — 2 is this machine, not a failure. Add a case to the
  suite your change touches rather than a new suite.
- **The gate before handing work over** — every suite, the typecheck, the linter
  and the build, plus the check for the surface you touched:

  ```bash
  for f in tools/test_*.py;  do python "$f" >/dev/null || [ $? = 2 ] || echo "FAIL $f"; done
  for f in tools/test_*.cjs; do node   "$f" >/dev/null || [ $? = 2 ] || echo "FAIL $f"; done
  (cd web && npx tsc -b && npx oxlint src && npm run build)
  node tools/check_<the surface you touched>.mjs    # payload-driven: no server needed
  ```

- **UI checks**: `node tools/check_*.mjs <payload.json>` stand the real app up on
  a stub backend, so they need no server and never touch the owner's library;
  `tools/check_*.cjs` drive a running scratch server (8011 up, scratch
  `MLO_MUSIC_FOLDER`).
- **A UI change is not verified by a check alone**: start the app
  (`python dev.py --no-open --no-tray`), drive the real page, and read what it
  did — a check that passes while the screen is wrong is a check that is wrong.
- Web on its own: `cd web && npm run build` (typecheck `npx tsc -b`).
- **The Rust helper** (`rust/`, binary `mlo-audio`): build it with
  `cargo build --release --manifest-path rust/Cargo.toml` (zero crate
  dependencies, so it builds offline). `mlo/dr.py` prefers it and falls back to
  the numpy block math; `tools/test_dynamic_range.py` pins the two engines to
  the same integers and skips only the parity block when the binary is not
  built, so the gate passes either way. The container builds it in its own
  stage.

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
