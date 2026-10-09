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

## The dev bed, in a container (what an agent reaches for first)

`docker-compose.dev.yml` is `dev.py` in a container: the app from this working
tree on **8011**, vite with hot reload on **5181**, the scratch library at
`local/dev/music`, and a project, container and port of its own — so it never
touches the real install, the host's Python, or the host's `node_modules` (a
named volume keeps the Linux copy out of `web/`). The two halves share a network
namespace, so vite's proxy arrives on the app's own loopback: anything else is a
CLIENT to the login gate (`server/auth.py`), and a dev URL that asks for a
password nobody set is not a bed.

```bash
docker compose -f docker-compose.dev.yml up -d --build   # the first build is minutes
docker compose -f docker-compose.dev.yml logs -f
docker compose -f docker-compose.dev.yml down
```

UI work belongs on the vite URL (it reads `web/src` straight from the tree); the
8011 URL is the image's own build of `web/dist`. A fresh bed comes up on the
setup wizard, flipped the usual way — with the CONTAINER's path:

```bash
curl -X POST http://127.0.0.1:8011/api/config -H 'Content-Type: application/json' \
     -d '{"music_folder": "/music", "first_run_done": true}'
```

`python dev.py` stays for the two things a container cannot do: a machine with no
container runtime, and **desktop-shell work**, which is host-only by nature (the
Tauri/Windows shell is built and driven on the host — `npx tauri build --debug
--no-bundle`, a scratch `MLO_APP_DATA_DIR`, and `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`
when a CDP port is wanted). A Linux container is not the Windows runtime either:
anything about console windows, the title bar or the tray still has to be proved
on the host.

## Commit locally; release only when the owner says so

Work lands as **local commits on `main`** — explicit `git add`, a message that is
the record of what changed and what proved it. That is where a task ends:
**do not `git push`, tag or cut a release until the owner says the state is good.**
They run the app from this repo, so an unreleased change is already live for
them; releasing is their decision, not the last step of a task.

When they do ask for one:

1. `python tools/check_versions.py vX.Y.Z` — every copy that must agree (10 of
   them: `mlo/__init__.py`, `Dockerfile`, `README.md`, `desktop/package.json`,
   `desktop/package-lock.json`, `web/package.json`, `web/package-lock.json`,
   `desktop/src-tauri/Cargo.toml`, `desktop/src-tauri/Cargo.lock`,
   `desktop/src-tauri/tauri.conf.json`) is listed, and the run fails until all
   say the same version. Bump first, then run it again with no argument.
2. Release notes: `docs/release-notes/release-notes-<version>.md`, never the repo
   root. They are the record of what a release changed, and the release workflow
   now requires them: `release.yml` fails the release if the file is missing,
   uses it as the GitHub release body, and `tools/make_updater_manifest.py`
   carries it as the notes an installed app shows. `git mv` keeps the history
   when one moves.
3. `git push origin main`, then tag and push it
   (`git tag -a vX.Y.Z -m "la musica X.Y.Z" && git push origin vX.Y.Z`): the tag
   runs `.github/workflows/release.yml` — suites, then the web/desktop/mobile/
   docker builds and the published GitHub release (~10 min). Watch it with
   `gh run watch <id>`; a red job there is the release, not the change.

   Redoing a release (a fix after a failure) means moving the tag — `git tag -f`
   locally, then delete and re-push it. Know what that does on the way: deleting
   a tag whose release exists turns that release into a **draft**, so
   `releases/latest/download/latest.json` 404s until the next run publishes it
   again (the release job always creates a published one). A 404 on a release
   that was green ten minutes ago is that draft, not a missing asset.
4. **The updater's signing key.** Every release signs its desktop bundles with
   the key whose private half is the `TAURI_SIGNING_PRIVATE_KEY` secret and
   whose copy on this machine is `~/.tauri/la-musica.key` (Tauri's own place for
   it); the public half is in `tauri.conf.json`. **Back that file up somewhere
   that is not this machine.** A GitHub secret cannot be read back out, and an
   installed app verifies updates against the key it was BUILT with — so losing
   the private key does not fail a build, it quietly ends in-app updates for
   every copy already out there, which then has to be replaced by hand. Rotating
   it deliberately has the same cost, so rotate only when the old one is known
   to be compromised.

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
- **The shell's capabilities must cover the page the window loads.** A packaged
  install points the window at the backend it spawned on loopback, and Tauri
  counts any http page as remote: without the `remote` URL list in
  `capabilities/default.json`, every `plugin:window` command (dragging,
  minimize, maximize, close) *and* every app command is refused there — a title
  bar whose controls do nothing. The app commands additionally need the manifest
  in `build.rs`, which is what gives them a permission to be granted at all, so
  its command list and `generate_handler!` must stay equal.
- **Writers fill, they do not overwrite** — except the four families an import
  decides (lyrics, genre, advisory, embedded cover), which
  `server/imports.drop_arrived_values` clears first so the import's values land.
- **A behaviour change gets a rule.** `docs/OPTIMIZATION-GRADING-SPEC.md` is the
  contract users check the app against; `README.md` and the GitHub description
  follow it.
- **`createUpdaterArtifacts` and the signing key live in the desktop workflow,
  never in `tauri.conf.json`.** With them in the config, every build that is not
  a release — a local `tauri build`, the mobile workflow's compile of the same
  crate — would demand the key and fail without it; the workflow passes both,
  and the release job assembles `latest.json` from the signatures the bundles
  left behind (`tools/make_updater_manifest.py`, whose suite pins every platform
  key Tauri's updater asks for). The app's endpoint and public key are in
  `tauri.conf.json` (`plugins.updater`) and must agree with that key. Do NOT
  turn on `plugins.updater.requireSignedVersion`: the pinned tauri-cli (2.11.4)
  signs with a trusted comment carrying only a timestamp and the file name, and
  that switch rejects any signature without a version in it — every update would
  fail, and the failure would look like a broken endpoint rather than a config
  mistake.
- **The OS media card is the shell's, and the webview's own session stays off.**
  Windows names a media session by its Application User Model ID, and the one
  WebView2 publishes for the page's Media Session belongs to the RUNTIME's
  process (`msedgewebview2.exe`), which resolves to no app — that is the
  "Unknown app" label in Windows 11's media flyout, and it is not ours to set.
  So the shell publishes its own session (`desktop/src-tauri/src/win_media.rs`,
  opened on the main window in `lib.rs`'s setup hook) and the webview's is
  switched off in `tauri.conf.json` (`additionalBrowserArgs`,
  `HardwareMediaKeyHandling`). Those two lines plus the bridge between them —
  the `set_now_playing` command, the `mlo-media-key` event, and the handlers
  `PlayerBar.tsx` shares between `navigator.mediaSession` and the shell — are
  one mechanism: turning either the session or the flag off alone leaves the
  flyout with two cards for one song.
- **A console child gets `CREATE_NO_WINDOW`.** The server runs windowed and owns
  no console, so anything launched without it flashes a terminal window;
  `mlo/deps.py`'s spawn probe did exactly that on the first Dependencies request.
  Every console spawn in `mlo/` and `server/` passes the flag — a new one must too,
  and so does the Rust helper's own ffmpeg child (`rust/src/main.rs`,
  `ffmpeg_cmd()`): the helper is spawned windowed, so an unflagged ffmpeg under it
  would allocate a console of its own.
- **The desktop shell is undecorated, and the web app draws its window.**
  `web/src/components/TitleBar.tsx` is the only title bar, and every screen owes
  it room. Links that leave the app go through the shell
  (`web/src/lib/externalLinks.ts` → `open_external`): a webview drops
  `window.open` and `target="_blank"` alike. And no window the app did not open
  may appear.
