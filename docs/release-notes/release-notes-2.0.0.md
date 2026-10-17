# la musica 2.0.0 — the import and identity release, and a full-codebase audit

Two things happened between 1.3.0 (2026-10-12) and this release. The import
pipeline was rebuilt around what a file's identity actually is — where it came
from, which release it is, which of its values a machine may state and which
one is only ever the user's word — and the whole tree was then audited, slice
by slice, against the contract in `docs/OPTIMIZATION-GRADING-SPEC.md`. Every
defect the audit proved is fixed here, and each fix is listed in the last
section with the evidence that found it.

61 rules were added to the spec in this window (`R391`–`R438`, plus `R6a`,
`R20`, `R38`, `R38a`, `R40`, `R78b`, `R92`, `R93`, `R262`, `R321`, `R358b`).
The registry now holds 25 scripts and 73 grading keys (64 checks and 9
file-category toggles) — the counts this file and the README state are the
counts the app reports.

## RateYourMusic and Album of the Year are gone; MusicBrainz is the source

Both scraped hosts' terms forbid being scraped, and the machinery a scraped
host needed — the Cloudflare solver sidecar, the stored session cookies, the
borrowed browser session, the challenge solver — was unmaintainable. They were
removed on 2026-10-12 together with every `RATEYOURMUSIC_*` tag in the app's
vocabulary, `grade_check_rym_links` and its `RYM_LINK` issue code, the
Dependencies row, and the `cf_solver_url` / `cf_solver_autostart` /
`cf_solver_timeout` / `cf_browser_fetch` settings (a config that still holds
one has them dropped on load). MusicBrainz — over its documented public API,
at its 1 req/s etiquette — is the only genre and web-rating source, and a
file still carrying a `RATEYOURMUSIC_*` tag fails `grade_check_excess_tags`
until the strip pass removes it. **This release also deletes the last
remainder of that machinery: the `flaresolverr` service, its
`MLO_SOLVER_URL` variable and the `depends_on` that made the documented
install wait on it were still in both compose files**, so `docker compose up
-d --build` pulled a ~300 MB third-party browser container for a feature that
no longer exists — and a blocked pull of it took the whole install down.

## Genres and web ratings follow the source's own tiering

- **A track's genres come from ONE tier, never a blend**: the recording's own
  genres first (its work's only when the recording states none), else the
  release GROUP's (never the specific release's edition-level ones), else the
  artist's. The first tier that states anything IS the list (`R38a`).
- **GENRE holds the names a source stated, in the source's own order.** MusicBrainz
  publishes genres by vote count, and `mb_genre_count` (2) caps the list
  WITHOUT sorting it, so a file stores the top two in MusicBrainz's order. A
  manually entered list is stored as typed. The parent idea is gone: no name is
  dropped for sitting under another one, and `GENRE_STRUCTURE` fails only a
  repeated name (`R37`, `R38`, `R39`, `R40`).
- **A web rating is ONE source's own number.** `WEBRATING` / `ALBUMWEBRATING`
  carry the first rung that answered down the owner's ladder — track
  (recording, then its work), then the release group, then the artist; an album
  reads the group's, then the artist's — and the number is converted by
  TRUNCATING to the app's 0.05 grid, so the one-decimal readout equals the
  source's own reading. Its shape is graded and canonicalised through one rule
  (`WEBRATING_FORMAT`, `canonical_rating`), never "fixed" by a guess
  (`R358`, `R358a`, `R358b`).
- **The MusicBrainz link IS the release change, in both menus** (`R396`,
  `R397`). The wizard's Links step writes the id and the release's own
  `.mb_expected` tracklist; the Library's Import menu lists every selected
  album on the release its files name now. A manifest whose `release_id`
  differs from the albums' tracks is STALE and script 15 refetches it — without
  that rule a re-point left the old release's tracklist recorded.
- **A disc subfolder belongs to its album** (`R336`): `CD1/`, `Disc 2/`, `1/`
  inside a release folder are the SAME release, and `mlo.discs` is the ONE rule
  the grader, the library builder and the artist payload all read. Measured on
  a 2-disc release: reading only the album's own folder listed disc 1's tracks
  plus a phantom `CD2` album.

## The import: nine steps, a graph, and one value that is only ever yours

- **The wizard is nine steps** — Select → Links → Match → Covers → Genres →
  **Ratings** → Lyrics → Advisory → Finish — and **leaving a step SAVES it**:
  every jump writes the step it came from (`R417`). The Finish step ticks the
  chain's scripts; its **Close** settles and files the album and runs NOTHING.
- **The chain is a graph.** Every script that reads or writes the album's own
  files runs one at a time; the artist-art and submission steps (19, 22, 25,
  26) run beside the spine; and an import's advisory fetch is a NODE of the
  graph after script 13, because an advisory is never decided before the words
  it reads (`R388`, `R405`). A run reads each file once and each folder once
  per chain, and ONE MusicBrainz browse answers a whole album's genres and
  ratings (`R399`, `R400`). Queued imports run on lanes, one chain per album
  (`R403`). A pass that depends on one GLOBAL interval must not be laned: the
  advisory fetch is serial on purpose, measured at 36.8 s on one lane against
  48.3 s on eight (`R404`).
- **`SOURCE` is the user's own word.** Three writers that used to invent it are
  gone: the importer now writes only a value the user stated (the wizard's
  Match field, the album page) and reports what the app knows — the release's
  own store URLs, the provider the acquisition stated — as a SUGGESTION that
  nothing writes. An unattended import therefore lands a Digital Media album
  with no `SOURCE` and the `source` family's prompt is what asks for it, its
  row naming the value the app knows and saying it is the user's to state
  (`R398`, `R438`).
- **Nothing an import did not write is deleted.** The one tag this app used to
  delete — `SOURCE` from a CD — now reads one switch, `strip_source_on_cd`,
  which ships OFF; the grade's "SOURCE present but MEDIA is not Digital Media"
  issue is gated on the same switch, and the digital fill and the Soulseek
  stamp stay fill-only (`R398`).
- **An upload into an existing album fills THAT album** — fill-only, `kept`
  naming what it would not give up, no `(2)` folder opened — and the Match
  step's writer never sends `null`/`""` for a per-track tag on a row with no
  release match (`/api/mb/assign` reads those as DELETE, and that stripped the
  TITLE of exactly the extra tracks a user had added). `TRACKTOTAL` is the
  file's OWN disc's count, never both discs'.
- **Manual Soulseek downloads are picked file by file.** The downloads tab's
  picker expands an entry to per-file checkboxes with tags, the identify dialog
  opens each row on the release the files name, prefills the MusicBrainz search
  from ARTIST+ALBUM, and offers AcoustID detection or a pasted link (`R433`).
- **Ratings are a step and a policy family of their own**: MusicBrainz's
  per-recording score per track plus the release group's for the album, written
  fill-only, shown per track with the tier that answered.

## Metrics, submissions, and the surfaces around them

- **MusicBrainz submissions write your ratings back** (`R434`, `R436`): OAuth2
  with the PKCE verifier kept server-side, ONE request per album, values
  snapped to MusicBrainz's 20-point grid, a value that would snap to 0 never
  sent. Rating a track queues its album by itself while logged in, and
  **Upload all ratings** queues every album the library has a rating for — both
  report per album what was sent and skipped.
- **Scrobbling is a percentage of the track**: `lastfm_scrobble_percent` (70,
  clamped to Last.fm's own 50 % floor), with a refusal queued and retried,
  never dropped.
- **Settings is eight tabs of rows** built on one field renderer
  (`CfgFields.tsx`, `R435`), the wizard is five steps, and the phone layout
  passes the responsive check that used to fail 15 times on `/settings`.
- **Home, online** carries *Trending now*, *New & notable* and a
  library-seeded *For you*, each shelf its own request, the whole section gone
  when Discovery is off (`R407`).
- **Rips: 100 % logs are the default for importing and grading alike**
  (`R402`). A log the scorer cannot read is accepted on its per-track CRCs
  (`R425`); a scored log below the floor is rejected.
- **A folder the layout scan may name is settled in one run** (`R437`):
  removal is asked of every entry it would take — junk by the stray rule, or
  the file the app itself wrote for that folder — and audio, artwork, a disc
  folder or `.cue`/`.log`/`.accurip` refuse, with the row saying what stayed
  and why.
- **Lyric search asks the primary credit** after the stored name (`R431`), so a
  file whose `ARTIST` is a whole credit ("X feat. Y & Z") still finds its words.

## The audit: what it found, and what is fixed here

Ten audits ran over the tree in parallel — the HTTP trust boundary, every
atomic write and the playback-versus-write contract, the import pipeline, the
script graph, grading against the spec, the frontend's playback and window
plumbing, the desktop shell and the release workflows, the frontend↔backend
route contract, the README's own claims, and the audio engine. The route
contract is clean in the direction that matters (every path the frontend
builds resolves to a mounted route, with the right method, and the WebSocket
message types agree); the locale bundles are in exact parity (502 keys in all
six languages, no missing key, no placeholder mismatch); the fullscreen
unmaximize/maximize sequence, the one-media-session rule and the
`CREATE_NO_WINDOW` rule all hold.

These are the defects it proved, all fixed:

| # | Where | The defect |
|---|---|---|
| 1 | `mlo/cli.py` | The console menu numbered **Run All / Run Custom / Dependencies 16, 17 and 19 — which are script ids** — and the script dispatch was tested FIRST, so those three entries were dead: pressing "Run All" ran Mood & Energy, "Run Custom" ran the transliteration script, "Dependencies" ran the artist-image script. Renumbered 27–30 and answered before the script lookup. |
| 2 | `web/src/components/PlayerBar.tsx` | **Emptying the queue left the track sounding forever.** The reload effect returned early when the queue ran out, so the element was never paused while the bar flipped to "Nothing playing" with its transport disabled — and the media-session handlers stayed registered, so a lock-screen play restarted the removed track. |
| 3 | `server/api_media.py` | `SharedFileResponse` handed `close` to a `BackgroundTask` that nothing runs (it answers the ASGI messages itself), so every stream and download leaked a file handle until a cyclic-GC pass — and on Windows the tag swap's moved-aside inode stayed on disk with it. |
| 4 | `mlo/audio.py` | `_pending_ops` was REPLACED per call, so a deferred batch (`/api/mb/assign`) whose single flush was refused queued only its LAST field and reported the rest as written. |
| 5 | `server/api_mb.py` | `defer_save(False)`'s verdict was discarded, so a batch that did not land answered `{"ok": true}` with an empty `errors` list. |
| 6 | `mlo/pending.py` | The drain's remove/bump acted on a whole entry by key without checking it was still the one it applied — an edit recorded while the replay was running was silently deleted. |
| 7 | `mlo/genres.py` | Unrecognised names were appended after recognised ones BEFORE the cap, so the source's order was not kept and the cap could drop the first name it stated (`["Nonsense","rock"]` at `mb_genre_count = 1` stored "Rock"). |
| 8 | `mlo/lyrics.py` | The `.lrc` sidecar's swap was a bare `os.replace`, bypassing the one retrying rename every other library write uses. |
| 9 | `server/artcache.py` | Image redirects were followed by httpx with the allowlist checked only on the URL the caller named, so an allowlisted CDN's 302 to `http://169.254.169.254/…` was fetched and relayed. Every hop must now land on a public host. |
| 10 | `mlo/scripts.py` | The grade (4) did not declare its dependency on audit (6) although it grades the `AUDIT` tag, so a reordered Run All graded a stale audit. |
| 11 | `web/src/lib/updater.ts` | A failed event subscription left the update row on "Downloading…" forever, with no error and no button. |
| 12 | `docker-compose.yml`, `docker-compose.dev.yml` | The FlareSolverr service, its `MLO_SOLVER_URL` and the `depends_on` outlived the scraped sources they served. |
| 13 | `README.md`, spec §3/§4 | The headline grading count said 69 (and the spec 71) where the registry holds 73. |

Reported but deliberately NOT changed, with the reasoning:

- **`?token=` is accepted from the query string on every route**, not only the
  media and WebSocket ones that need it (a webview cannot set a header). It is
  a real exposure — a token in a URL reaches access logs — but the frontend
  builds its `<img>` and `<audio>` sources that way for every client that is
  not same-origin, so narrowing the read would break covers and playback in
  every Tauri build. A cookie already does the job on the web app, and nothing
  is appended there.
- **The import DELETES arrived MusicBrainz alias tags** (`TITLEALIAS` and its
  locale-suffixed spellings) as a fifth family, alongside the four the contract
  names. It is deliberate — "the arrived ALIAS tags are the import's to
  replace", issue #59, pinned by `tools/test_import_pipeline.py` — but on an
  import that holds NO release nothing re-writes them, and the album then fails
  `grade_check_alias_needed`. The owner's call, not a silent change.
- **The auto-import paths never pass the acquisition's provider** to the SOURCE
  suggestion, so the "the network that served the download is offered as the
  suggestion" half of R438 is only true for the wizard's own route.
- **The Python and Rust dynamic-range meters compute in different precisions**
  (float32 views against f64), so a value within ~1e-7 of an `x.5` rounding
  boundary could differ by one integer depending on whether the helper binary
  is present. `tools/test_dynamic_range.py` pins the two on its fixtures and
  passes; the divergence is theoretical until a fixture lands on a boundary.
- **A scoped chain's drop invalidates a concurrent unscoped run's library album
  list.** Traced: the consequence is a re-walk (a lost memo), not wrong work —
  `_scan_hit` answers nothing for a cleared list and the walker resolves the
  library again.
- **An import's advisory step is spliced at the FRONT of a chain that carries
  no script 13** — ahead of the instrumentals pass whose `INSTRUMENTAL` tag is
  the advisory ladder's first move (`mlo.advisory._is_instrumental`). The
  placement deliberately preserves what the step did as a pre-chain stage, and
  `tools/test_import_pipeline.py` pins it, so it is a design decision rather
  than a slip: an import configured without script 13 can rate an instrumental
  track as if nothing had stated it. Worth the owner's ruling — anchoring the
  step on the instrumentals node when 13 is absent is a two-line change and
  leaves every chain that carries 13 exactly as it is.

## The second wave

Six more slices were audited once the first ten were in — the third-party
integrations, state and persistence, the server-side player, the dev harness,
secret handling, and the page components. Four of its findings are fixed here
because each broke a promise the app makes out loud:

- **MusicBrainz's one-request-per-second etiquette was INERT on the success
  path.** `mb_get` stamped its throttle only when a request was retried or
  failed, so every successful read went out as fast as the socket answered: an
  artist walk, a rated-library pass and the genre taxonomy all burst, which
  MetaBrainz answers with 429/503 — or blocks the user agent for. Measured with
  a stubbed transport: four reads used to fire with gaps `[0.0, 0.0, 0.0] s`;
  they are `[1.0, 1.0, 1.001]` now.
- **Rating submissions posted with no throttle at all**, one request per album
  back to back — the write half of the same budget. Both now take ONE slot
  (`integrations.mb_reserve_slot`), whatever direction they travel in.
- **A revoked Last.fm session was queued as though it were an outage**: every
  play was refused and spooled for ever while Settings still said "Connected
  as …". Last.fm's error 9/26 now clears the stored session and asks for the
  reconnect that is the only fix, and queues nothing.
- **The scrobble spool's read-modify-write had no lock**, so "a refused
  scrobble is queued and retried, never dropped" was false under concurrency —
  exactly during the outage it exists for. Verified with two threads.

The harness defects are the same class of broken promise: six browser checks
defaulted to the owner's LIVE install on port 8000 (one of them deletes through
`/api/trash/delete`), seven suites reported "cannot run here" as a PASS, and
twelve reported a missing `flac.exe` as a FAIL instead of the contract's exit
2. All are fixed; the FLAC case is verified both ways on one machine.

Two page-level defects in the same wave are fixed as well, both reachable by
ordinary use:

- **The Soulseek review tab CRASHED** when an album could not be imported: the
  failed list read `x.album` while the server appends `{path, reason}`, so
  `undefined.split()` threw inside render and the page unmounted into the app's
  error screen. (The screen one line below explains the file-in-use case it
  could never reach.)
- **"Reset to defaults" + Save dropped a configured install into the first-run
  wizard.** The reset copies `DEFAULT_CONFIG`, which carries
  `first_run_done: false`, and the keep-guard read the FORM rather than the
  live config — a map that holds neither `first_run_done` nor `music_folder`.
  Reproduced before the fix and confirmed after, in a browser on a scratch
  library: the old build lands on `/setup` with the settings surface gone, the
  new one keeps both keys.

Reported, not fixed — each is a behaviour change worth its own pass:

- **`server/playlists.py`** — an interrupted `_ensure_user_key` rebuild leaves
  `likes_user` behind, and every later call then fails with "table likes_user
  already exists": playlists, hearts and favourites are unreachable until the
  table is dropped by hand. Reproduced in a scratch database.
- **`mlo/config.py` + `mlo/paths.py`** — a scratch scope MOVES the checkout's
  own `server/data` into the scratch folder, so the documented cleanup (delete
  the scratch folder) loses it. The comment two lines above states the opposite
  intent.
- **Four watchers in `server/main.py` never stop**, and the Soulseek page
  watcher can start a NEW import after the shutdown window has opened.
- **`server/mb_ratings_sync.py`** — queued rating uploads are lost on any
  failure or exit (an in-memory set cleared before the POST), the queue submits
  as the default user so a named user's album rating is never uploaded, and the
  worker has no stop.
- **`server/soulseek_auto.py`** — a job cancelled while waiting for its album
  folder never releases the claim, so the next job for that album waits an hour
  and then fails, on every retry, until the app restarts; and a cancelled job
  can still add the wish it was about to ask about.
- **The desktop shell grants its privileged commands to ANY loopback page**
  (`capabilities/default.json`'s `http://127.0.0.1:*` list with `csp: null`),
  and the shell drops its probe listener before the child binds the port — so a
  local process that wins the race holds them.
- **`GET /api/config` returns every stored third-party secret** (Soulseek
  password, AI key, Spotify/Last.fm/MusicBrainz/AcoustID/Discogs credentials)
  to any client holding a session; its own docstring says it returns "minus the
  one secret in it". Fixing it means masking the keys AND teaching
  `POST /api/config` to ignore the sentinel, which is a UI-visible change.
- Smaller ones: a yt-dlp failure is reported as "no acceptable YouTube match
  found"; `thumbs` caches a failed probe for ever; a Spotify token is not keyed
  to its credentials and a 401 is never retried; `ai.py` retries 401/403/429;
  and `docs/RUST-TUI-PROMPT.md` still lists RateYourMusic as a live service.
- **Five import/export flows discard a write's own failures.** `/api/mb/assign`
  answers `{ok: false, errors: […]}` with HTTP 200 when a locked or read-only
  file refused its tags (the route says so itself: "callers show `errors`"), and
  the wizard's four Save buttons and the Soulseek "Tags written" toast report
  success anyway, clear the edited mark and advance. `/api/run`'s per-script
  failures are discarded the same way by the wizard's Finish step ("album
  graded") and the Library's batch menu.
- **`server/api_storage.py` measures the default transfer folders, not the
  configured `soulseek_download_dir`**, so the Storage card's downloads row and
  `app_total` are short by the whole download cache on any install that moved
  it — while the Downloads page, the staging routes and the cap sweep all act
  on the real folder.
- **The ratings heal can revert the user's newest verdict**: `map_for`'s
  `UPDATE OR REPLACE` moves a stale pre-move row onto a path that already holds
  a newer rating, and SQLite resolves that conflict by DELETING the newer row.

## Verification

Every Python suite and every Node suite in `tools/` runs for this release on
the frozen tree, together with the web gate (`tsc -b`, `oxlint src`,
`npm run build`) and `check_versions.py v2.0.0` (twelve copies, tag matches).
The changes this audit made were re-run against the suites that import them.

The two defects whose mechanism needed a runtime answer were driven for real:
the emptied queue in a browser, on a scratch library — the OLD build keeps
sounding (`paused=false t=10.34`) while the bar says "Nothing playing", the new
one pauses — and the CLI menu through a dispatch stub (`27` runs the whole
chain, `16` runs Mood & Energy). Beyond those, the four second-wave fixes carry
their own reproductions: the MusicBrainz read and write budgets were measured
on a stubbed transport (`[0.0, 0.0, 0.0] s` before, `[1.0, 1.0, 1.001]` after,
one shared sequence of writes and reads), the spool race with two threads (a
scrobble queued during an in-flight flush survives it), the revoked Last.fm
session through the real route on a scratch config (`queued: false`,
`reconnect: true`, session cleared, `connected: false`), and the harness skip
rule both ways (twelve suites pass with flac present; one exits 2 with
`.dependencies` hidden and PATH stripped of flac).

Where a defect could not be answered by running it, the report says so: the
rest of the audit is a source read, and every claim in it is a traced call
path, not a guess.