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

- **Playback never blocks a tag write, and a refused write is QUEUED, not
  lost.** The owner rated a track while it played and the write failed
  (`[WinError 5] … could not be replaced`): the media routes served the file
  through a plain `open()`, which shares read+write but NOT delete, so
  `os.replace` onto it was denied for the whole playback. A LONG-LIVED media
  route opens the file through `mlo.atomic.open_shared_read` and serves it via
  `server.api_media.SharedFileResponse`; a SWAP onto such a file lands through
  `mlo.atomic.replace_over_open_handle`; and anything still denied is recorded
  in `mlo/pending` (drained by the server lifespan) and reported as *QUEUED*,
  never as "nothing was written". So a new streaming route must go through
  `open_shared_read`, and a new tag writer must flow through `AudioFile`'s one
  atomic swap (never a hand-rolled `os.replace`) so it is covered. Spec R394.
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
- **GENRE holds the names a SOURCE stated, in the SOURCE's own order, and the
  parent idea is REMOVED.** MusicBrainz publishes each entity's genres by vote
  count (most-voted first), `server.integrations._genres` keeps that order, and
  `mlo.genres.normalize_genres` caps at `mb_genre_count` (2) WITHOUT sorting —
  so a file stores the TOP TWO MusicBrainz genres in MusicBrainz's order, a
  manually entered list is stored as typed, and the order is a ranking, never
  an alphabetization (`format_genres` never reorders what a file holds). The
  hierarchy is gone (2026-10-12, the owner's call): `normalize_genres` no
  longer drops a name another member sits under, `_drop_derived_parents` and
  `family_of` are deleted, `mlo.genres.issues()` reports only missing / too
  many / duplicate / unknown / spacing, and `GENRE_STRUCTURE` fails ONLY a
  repeated name. `mlo.genre_vocab.parent_of`/`top_of`/`is_parent` survive for
  exactly two internal browse aids (the artist recommender's bucketing and the
  keyword fallback's family) — no tag, check or row reads them. The genre chain
  is MusicBrainz ALONE since the same date — per recording, then the release
  group/release, then the artist — because the two scraped hosts
  (RateYourMusic, Album of the Year) and their cookie, solver and browser
  machinery were removed; a track's genres a MusicBrainz answer never supplies
  leave the album FAILING grading (`GENRE_MISSING`, `grade_check_genre`,
  default on). **The `RATEYOURMUSIC_*` tags left the app's vocabulary on the
  same date** — `RATEYOURMUSIC_ALBUM`/`_ARTIST`/`_TRACK` are in no registry
  (`mlo.audio.TAG_MAP`, `server/library.py`, `server/api_query.py`,
  `server/tags_registry.py`), nothing reads, writes, displays or grades them,
  `grade_check_rym_links` and its `RYM_LINK` issue code are gone, and a file
  still carrying one fails `grade_check_excess_tags` until the strip pass
  (script 10 / Optimize) removes it through that same vocabulary. See spec
  R37/R38/R38a/R39/R40/R92/R93.
- **A web rating is ONE SOURCE'S OWN number, never a blend.** `WEBRATING` /
  `ALBUMWEBRATING` (0-100, Picard's scale) carry the FIRST source in the
  configured chain that answered — the order IS the hierarchy, a TRACK-level
  answer beats an album-level one, and the walk STOPS there
  (`mlo.web_ratings.choose`), so the number is a score a reader can open the
  source and check. Since 2026-10-12 that chain is MusicBrainz alone (the two
  scraped hosts were removed), so it answers from the recording's rating with
  the release group's behind it. The tag holds a WHOLE number, and the
  conversion TRUNCATES to the app's 0.05 grid (`scale_to_100`, MusicBrainz
  0-5 ×20) so the one-decimal readout equals the source's own reading. The
  SHAPE is graded and canonicalised through ONE rule
  (`mlo.tagtext.rating_problem` / `canonical_rating`, spec R358e): a 0-5 score
  pasted in raw ("4.54") fails as `WEBRATING_FORMAT` and is never "fixed" by a
  guess, while "91.0" is the same rating spelled differently and script 10
  rewrites it. `*_SOURCE` names the one source whose number the file carries.
  See spec R358/R358a/R358b.
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
- **A tag write must survive a file the app itself is playing.** MEASURED
  2026-10-12 (Windows 11 build 26200): `os.replace` onto a file that is OPEN
  fails with WinError 5 whatever the holder's share mode — including a reader
  opened WITH `FILE_SHARE_DELETE` — while renaming the HELD file out of its name
  succeeds with share-delete and fails (32) without it. So the long-lived media
  routes serve library files through `mlo.atomic.open_shared_read`
  (`server/api_media.SharedFileResponse`, single-Range), a plain replace that
  meets an open file falls back to `atomic.replace_over_open_handle`'s
  rename-away swap, and a write a non-sharing holder still refuses is QUEUED in
  `mlo/pending.py` (drained from the lifespan; the error says queued, not
  "nothing was written") — the owner's "Rated, but the RATING tag was not
  written" while the track played. Spec R394.
- **A disc subfolder belongs to its album.** `CD1/`, `Disc 2/`, `1/` inside a
  release folder are the SAME release (spec R336): `mlo.discs.disc_subdirs` /
  `album_audio` / `merge_disc_albums` are the ONE rule, and the grader
  (`_grade_album`), the library builder and the artist payload all read through
  it. MEASURED 2026-10-12 on a 2-disc release: reading only the album's own
  folder listed disc 1's tracks plus a phantom `CD2` album, while the real
  library held every disc.
- **An album's release can be changed, and its tracks added to, without a
  re-import.** `POST /api/import/upload?album=<path>` fills THAT album —
  fill-only (`merge_into_album`), `kept` naming what it would not give up, no
  `(2)` folder opened — and the wizard's Match step "Add tracks…" posts it and
  re-matches. The Match step's writer must NEVER send `null`/`""` for a
  per-track tag on a row with no release match: `/api/mb/assign` reads those as
  DELETE, and that stripped the TITLE of exactly the extra tracks a user had
  added ("add more tracks to the album" lost their names the moment the matches
  were saved); `TRACKTOTAL` is the file's OWN disc's count, never both discs'
  (a 2-disc release wrote 19 onto a disc of 11). **Changing the MusicBrainz
  link IS the release change, in both menus** (spec R396/R397): the wizard's
  Links step fetches what its field names when the field is LEFT (no Fetch
  press; the album's own id is short-circuited) and Continue writes the id AND
  the release's own `.mb_expected` tracklist; the Library's Import menu lists
  EVERY selected album on the release its files name now, and an edited row is
  a bulk pin — `_stamp_release(force_ids=True)` stamps it OVER the existing id —
  while a row left alone sends nothing. A manifest whose `release_id` differs
  from the album's tracks' id is STALE and script 15 refetches it: without that
  rule a re-point through the import left the old release's tracklist recorded
  (measured: tags on the standard, `.mb_expected` on the deluxe, so the album
  showed the gaps of a release it no longer was).
- **The desktop shell is undecorated, and the web app draws its window.**
  `web/src/components/TitleBar.tsx` is the only title bar in WINDOWED desktop
  mode: every screen owes it room and it owns dragging/minimize/maximize/close.
  When Tauri's window is fullscreen, the bar and its controls disappear and
  `--mlo-titlebar-h` becomes 0; the fullscreen player's own `Leave fullscreen`
  control remains the way out, and leaving restores the bar/inset. **A
  maximized window is fullscreen's trap** (MEASURED 2026-10-12, the owner's
  report): `setFullscreen(true)` from a maximized window leaves Windows in the
  MAXIMIZED state — the client area stays the work area (1920x1040 of a 1080p
  screen), no resize fires, so the bottom of the window paints as black and
  the in-page bar never hides. `web/src/lib/windowFullscreen.ts` therefore
  `unmaximize`s before entering and `maximize`s again on exit (tao restores the
  placement it captured, which is the restored one), and the page is granted
  `core:window:allow-unmaximize` / `allow-maximize` beside
  `allow-set-fullscreen` in `desktop/src-tauri/capabilities/default.json` —
  tools/check_window_fullscreen.mjs pins the sequence. Links that
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
- **Sites whose terms forbid scraping are not scraped, and MusicBrainz is read
  through its documented API.** RateYourMusic and Album of the Year were
  REMOVED on 2026-10-12: their terms forbid being scraped, and the
  cookie/challenge machinery two scraped hosts needed — the FlareSolverr
  sidecar, the borrowed browser session, the stored session cookies — was
  unmaintainable, which is why the rules above it (an unbounded solve, a real
  browser for a scraped host, a signed-in session to read a RYM page) are gone
  with them. MusicBrainz is the only genre and web-rating source, reached over
  its own public API with its 1 req/s etiquette; no new path may scrape a site
  whose terms forbid it.
