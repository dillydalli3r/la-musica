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

- **Narrow the gate while you work.** `python tools/affected_tests.py --diff`
  (or name the files: `… mlo/discs.py server/plays.py`) prints only the suites
  whose imports reach those files, so a small edit runs a handful instead of all
  ~140 — the full loop above stays the handover gate. It reads imports, so a
  transitive dependency it cannot see is yours to judge: when the change touches
  a shared seam (`mlo/config.py`, `mlo/paths.py`, `server/events.py`,
  `server/main.py`, `server/integrations.py`, a wire format), run everything.
  Its own suite is `tools/test_affected_tests.py`.
- **UI checks**: `node tools/check_*.mjs <payload.json>` stand the real app up on
  a stub backend, so they need no server and never touch the owner's library;
  `tools/check_*.cjs` drive a running scratch server (8011 up, scratch
  `MLO_MUSIC_FOLDER`).
- **A UI change is not verified by a check alone**: start the app
  (`python dev.py --no-open --no-tray`), drive the real page, and read what it
  did — a check that passes while the screen is wrong is a check that is wrong.
- Web on its own: `cd web && npm run build` (typecheck `npx tsc -b`).
- **`web/src/components/ui/` is shadcn/ui's home.** New *shared* UI primitives
  go there, built on Radix + `cn()` from `@/lib/utils` (e.g. `ui/switch.tsx`).
  The rest of the app keeps its relative imports and the `.btn*` / `.panel` /
  `.input` classes in `web/src/index.css` as the design vocabulary — the ui/
  layer wraps those, it does not replace them. `web/components.json` plus the
  `@/*` alias (`tsconfig.app.json`, `vite.config.ts`) is what lets
  `npx shadcn add <component>` drop a file that resolves.
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
- **GENRE holds the names a SOURCE stated; the level above them is derived,
  never written.** `mlo.genre_vocab.path_of` walks `deep house` → `house` →
  `electronic` (three levels, one node per name — a parent in one place and a
  genre in another is the same node), `normalize_genres` drops EVERY name that
  is a strict ancestor of another name in the SAME list — wherever the source
  put it, so order can never decide what a tag holds — and ADDS nothing, and
  grading's `GENRE_STRUCTURE` reports the two defects that remain (a repeat,
  that redundant parent). `mlo.genres._drop_derived_parents` is that one
  predicate; script 10, the import and the grader all call it. The genre chain
  is RYM (per TRACK, through the release page's own `/song/` links) →
  MusicBrainz (per recording) → AOTY (album level ONLY: the site publishes no
  per-track genre), a track is settled by a track-level answer or the cap, and
  a RYM page is trusted only for the EDITION whose track list covers the
  album's own titles. See spec R37/R38/R38a/R93.
- **A web rating is ONE SOURCE'S OWN number, never a blend.** `WEBRATING` /
  `ALBUMWEBRATING` (0-100, Picard's scale) carry the FIRST source in the
  configured chain that answered — the order IS the hierarchy (RYM → AOTY →
  MusicBrainz), a TRACK-level answer beats an album-level one whoever stated
  it, and the walk STOPS there (`mlo.web_ratings.choose`), so the number is a
  score a reader can open the site and check. The tag holds a WHOLE number, and
  the conversion TRUNCATES to the app's 0.05 grid (`scale_to_100`) so the
  one-decimal readout equals the source's own reading (RYM's 4.54 → 90 →
  "4.5"; rounding to 91 would show 4.6 — the double rounding the owner
  reported). The SHAPE is graded and canonicalised through ONE rule
  (`mlo.tagtext.rating_problem` / `canonical_rating`, spec R358e): a source's
  own 0-5 score pasted in raw ("4.54") fails as `WEBRATING_FORMAT` and is
  never "fixed" by a guess, while "91.0" is the same rating spelled
  differently and script 10 rewrites it. All three answer per track (AOTY's
  own album rows are its per-track scores); `*_SOURCE` names the one source
  whose number the file carries. See spec R358/R358a/R358b.
- **The chain is a graph: one album-lane step at a time, the free lane beside
  it.** `mlo.scripts.SCRIPT_DEPS`/`SCRIPT_LANES` + `run_chain(..., extra=...)`
  (spec R388). Every script that reads or writes the album's own files is
  "album" lane and runs ONE AT A TIME — a tag writer loads a whole container
  and writes it back, so two of them on one file lose one side's writes, and
  no such pair may be run concurrently without a per-file write lock. Artist
  art / submission steps (19, 22, 25, 26) are "free" lane and run beside the
  spine, off the run's bar. The import's advisory fetch is a NODE of that
  graph, AFTER script 13 (lyrics) — an advisory is never decided before the
  words it reads.
- **An album description is fetched by IDENTITY first, and a name match must be
  STRICT.** `server.discovery.album_description` reads the release group's own
  `wikipedia` (or `wikidata` → sitelink) relation when it has the MBID, and
  otherwise accepts a search hit only when the article title, stripped of its
  parenthetical, EQUALS the album title and the artist is confirmed (from the
  disambiguator, or from the fetched lead, which must read as an album). A hit
  whose title merely CONTAINS the album title is rejected before any fetch —
  otherwise a self-titled record stores a different album's article (the
  owner's *Gorillaz* held *The Mountain*). Nothing strict → the chain falls
  through; a wrong description is worse than none. Spec R389.
- **Two Latin-script languages are separated by the LYRICS' own words, not by
  the tag.** `mlo.lyrics_xlit.detect_language` weighs a declared `LANGUAGE`
  against the text's function words and lets the text win when its count for
  another language EXCEEDS the declared one — a release's own
  text-representation is often not the words on the track (the owner's Spanish
  "Latin Simone" carried `eng` from the release and got no translation). The
  tag itself is still filled-only: the app never rewrites a stored language.
  Spec R167.
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
  `web/src/components/TitleBar.tsx` is the only title bar in WINDOWED desktop
  mode: every screen owes it room and it owns dragging/minimize/maximize/close.
  When Tauri's window is fullscreen, the bar and its controls disappear and
  `--mlo-titlebar-h` becomes 0; the fullscreen player's own `Leave fullscreen`
  control remains the way out, and leaving restores the bar/inset. Links that
  leave the app go through the shell (`web/src/lib/externalLinks.ts` →
  `open_external`): a webview drops `window.open` and `target="_blank"` alike.
  And no window the app did not open may appear.
- **A scratch run isolates its app data — `MLO_MUSIC_FOLDER` alone is not
  isolation.** The app data dir stays the install's
  (`%LOCALAPPDATA%\la musica`) unless `MLO_APP_DATA_DIR` names another, and
  `mlo.config.save_config` stamps that dir's own `config.json` with whatever
  music folder the save carried. So one `POST /api/config
  {"music_folder": "<scratch>"}` from a scratch run repoints the REAL install:
  the owner's next launch opens the scratch folder and lists no albums, while
  their library sits untouched. Set **both** variables for every scratch server
  (`MLO_MUSIC_FOLDER=<scratch> MLO_APP_DATA_DIR=<scratch>/appdata`), never start
  the packaged `mlo-server.exe` or the desktop app for scratch work, and never
  post a wizard flip to a port you have not just checked is both free and yours
  (`netstat -ano | findstr LISTENING`).
- **A route someone is watching never spends an unbounded solve.** FlareSolverr
  drives ONE browser, and on this network it cannot pass either scraped host's
  challenge, so a cold solve costs its whole `cf_solver_timeout` — MEASURED
  60-75 s, with the two hosts in series: "Importing genres…" sat for 124 s, and
  the first import also raced the boot warm-up, which was solving the same host
  at the same time. `server/cfchallenge.solve` now runs ONE solve per host at a
  time (a second caller gets `None` — not a browser queued behind the first) and
  honours a per-thread deadline (`cfchallenge.budget`, which the genre route
  sets from `integrations.GENRE_IMPORT_BUDGET_S`); `warm_solver` caches a
  clearance it WON as well as the failures it met. Any new user-facing path that
  can touch a browser must set a budget too, or it inherits the same stall.
- **The two scraped hosts are fetched by a REAL browser, and that is not a
  fallback to be "simplified" away.** MEASURED, 2026-10-10: FlareSolverr cannot
  pass either challenge; a cleared cookie read out of the owner's own Firefox —
  fresh, unexpired, sent with that browser's exact User-Agent — still gets
  `403 Just a moment…` from RateYourMusic over `httpx` AND over `curl_cffi`
  impersonating Firefox 135/144/157 (AOTY accepts it, RYM does not); and
  HEADLESS is detected by both Chrome and Firefox — re-measured 2026-10-11 and
  still true, so the route below runs a headed browser on a desktop no one can
  see instead (see the release-page rule). `server/browserfetch.py`
  drives this machine's own Chrome/Edge over DevTools, is preferred to the
  solver inside `cfchallenge.fetch_page`, and keeps the clearance in a profile
  under the app's state dir (`<music>/.mlo/data/browser`, ~280 MB) so later
  fetches never meet the challenge. It is gated on `cf_browser_fetch`, and that
  gate reads "no cfg, or no key ⇒ OFF" — so a suite's own cfg can never launch
  a browser on the machine running it, and a suite that DOES exercise the route
  patches the module (see `tools/test_browserfetch.py`). A smoke run with the
  live config opens NO window and needs none: the browser is launched onto a
  hidden Windows desktop (`browserfetch`'s `CreateDesktopW` route), so nothing
  appears on any display — and whatever started it must still stop it
  (`main.py`'s lifespan does). The challenge itself is taken at BOOT
  (`cfchallenge.warm_browser`, fired by the lifespan): the first navigation to
  a host after the profile goes cold spends it all (~27 s AOTY, ~49 s RYM
  measured 2026-10-11), which read as "no data" when the user's first import
  was what paid it.
- **A signed-in session is the ONLY thing that reads a RYM release page.**
  MEASURED, 2026-10-10: `/release/album/...` answers `503` — Chrome renders an
  error page for it — to every client with no session, while `/artist/` and
  `/search` answer `200`; with the owner's own cookie set the release page is
  `200` and 654 KB. The Settings cookie import (`POST /api/cookies/{source}/from-browser`)
  remains the explicit way to STORE that session. When `rym_cookie` is blank
  and browser fetching is on, `server/browserfetch._browser_session` lazily
  reads only rateyourmusic.com cookies from local browser profiles through
  yt-dlp's own reader — `yt_dlp.cookies`, imported IN PROCESS
  (`mlo.tools.python_pkg_path("yt-dlp")`), because the install the app itself
  makes is a pip folder whose entry point is `bin/yt-dlp` with no `.exe` beside
  it: an executable-only lookup finds nothing and reads an EMPTY jar, which is
  what "per-track genres never arrive" looked like (MEASURED 2026-10-11; a PATH
  or `.dependencies` executable is still the fallback) — filters to that host,
  caches the cookie string in memory for 45 minutes, and
  never writes it to config or sends it elsewhere. Each page fetch injects the
  borrowed pairs as host-scoped session cookies and deletes them in `finally`;
  Chrome keeps its own clearance/site-set cookies in the managed profile. The
  user-supplied UA still travels with a stored cookie for plain HTTP; managed
  Chrome keeps its own UA and drops only Cloudflare's own cookies
  (`cf_clearance`, `__cf_bm`, `__cfruid`, `__cfuvid` — bound to the browser+UA
  that earned them), while the site's signed-in tokens, `sec_*` included,
  travel with the session.
  **HEADLESS is not an option, and no longer
  attempted.** RE-MEASURED 2026-10-11: `--headless=new` ends on the
  interstitial for BOTH hosts even with a real Chrome UA, a matching
  `userAgentData` override, the anti-automation JS patches and a profile that
  HOLDS a valid persisted `cf_clearance`. What does work, invisibly, is a
  HEADED Chrome on a desktop this process creates and never shows
  (`server/browserfetch.py`: `CreateDesktopW` + `CreateProcessW` with
  `lpDesktop`): the same launch clears both hosts and returns real pages with
  no window on any display. Positioning a real window off-screen is NOT that —
  a multi-monitor layout can put `-3200,-3200` back on a real display, which is
  the window the owner saw; the hidden desktop is invisible on every layout.
  A `--user-agent` flag (or any UA override) breaks it on its own: a UA that
  disagrees with Chrome's own `userAgentData` build stops the challenge
  clearing, which is how the first version of this route failed. RYM also
  THROTTLES this client (a burst of requests turns into `403 Just a moment…`
  for a while, credentials or not) and rotates its own `sec_*` tokens in
  `Set-Cookie`, so a jar read before the burst is not a jar that works during
  it. And `Page.navigate`'s `errorText` (and a `chrome-error:` location) must
  be treated as a FAILED fetch: Chrome's error page carries no challenge
  marker, so it was being returned as "a page with no genres" and reported as
  "no data".
