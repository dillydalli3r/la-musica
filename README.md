# la musica

**v3.20.2** — a self-hosted app that *manages, optimizes, audits, grades and
plays* your music library, from the browser, a desktop window or a phone.

**la musica** (formerly Music Library Optimizer) is a FastAPI backend plus a
React UI over the proven `mlo` engine. It plays music **and** music videos (with
karaoke-synced lyrics), keeps manual and smart playlists, likes and favourites,
fetches artist artwork and biographies, walks a multi-source lyrics chain, caches
music for offline playback, exports to a device, and drives a managed Soulseek
client whose auto-importer verifies what it downloaded. All app state — config,
playlists, favourites, the beets library, the Soulseek config, measured loudness,
caches, and the runtime-installed external toolchain — lives in one hidden `.mlo`
folder inside your music directory.

Release notes for this version are in `local/release-notes-3.13.0.md` (older ones
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
bind mount that points at your library (committed as the placeholder
`./music:/music`; replace it with an absolute host path such as
`/path/to/your/music:/music` or `D:/Music:/music` — compose creates an empty
`./music` if it does not exist, and an empty folder reads as an empty library).
What matters:

- Image `ghcr.io/dillydalli3r/la-musica:latest`, container `la-musica`, port
  `8000`, pinned compose project name. `MLO_MUSIC_FOLDER=/music` is the only
  environment variable it needs; `MLO_SERVER_HOST=0.0.0.0` makes the published
  port reachable and switches the login gate on for non-local clients.
- Volumes: `/music` holds the library **and** all app state — `data`, `downloads`,
  `trash` and the runtime-installed toolchain (`/music/.mlo/tools`) — so one
  bind mount carries the library, the settings and the tools, and they survive a
  rebuild together. No second volume is needed; a `lamusica-dependencies` volume
  from an older compose file is simply unused.
- It runs unprivileged as `mlo`, uid/gid **1000**, `HOME=/home/mlo`, so the
  bind-mounted folder must be writable by uid 1000 or `.mlo` cannot be created.
  The healthcheck probes `http://127.0.0.1:8000/api/health` (30 s interval).
- A `watchtower` service is on by default, checking every
  `WATCHTOWER_POLL_INTERVAL` seconds (300). It updates the **image**, so a local
  `--build` is replaced by the published one on the first check; to keep building
  from source, start only the app (`docker compose up -d lamusica`).
- `ffmpeg`, `flac`, `libjxl`, `jpegtran`, `fpcalc` and `rsgain` come from apt in
  the image — as do the runtimes the Windows-only tools run on: `mono-runtime`
  (plus `libgdiplus` and mono's System.Drawing assembly, which CUETools'
  verification loads) and `php-cli`. `oxipng`, `slskd`, `AudioAuditor` and
  `Logchecker` then install at runtime from their upstream Linux builds, and so
  does `CUETools` — upstream's Windows zip, run through the mono launcher the
  installer writes. AccurateRip generation, the Logchecker grade and the
  AudioAuditor audit therefore work in Docker like they do on Windows;
  `GET /api/capabilities` says what a given server can actually do.

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
fifteen tools — `ffmpeg`, `flac`, `libjxl`, `libjpeg-turbo` (`jpegtran`),
`oxipng`, `rsgain`, `AudioAuditor`, `Logchecker`, `php`,
`CUETools`, `chromaprint` (`fpcalc`, optional — AcoustID), `librosa`, `beets`,
`slskd`, `yt-dlp` — and shows each one's installed, pinned and upstream version.
They live in **`<music>/.mlo/tools`**, inside the library, so they travel with
the music folder (and with the one mount a container has); a copy installed by
an older release beside the app is still *read*, so an upgrade does not report
every tool missing — the row says which folder it found it in.
Per platform a row is `deps` (the installer fetches it) or `unsupported` (no
build here, with the reason); a distro-owned tool is a `system` row whose button
copies the package manager's command, which is the only kind of row that cannot
be fetched in-app. Every `deps` row has its own **Install** / **Update** button,
and the page-level **Update all** presses every missing or outdated row at once.
A press fetches the NEWEST release the tool's own publisher has — GitHub,
PyPI for the pip packages (`librosa`, `beets`, `yt-dlp` on Linux) and
windows.php.net for `php` — while the pinned version is what a FIRST install
fetches when no upstream answer is reachable; a row already at the newest known
version is a no-op, and a copy newer than upstream is never downgraded (a
PATH-installed `ffmpeg` from scoop is updated into the app's own toolchain
folder, not in place). `libjxl` and `libjpeg-turbo` are downloadable rows on
Linux too: libjxl's static `tar.lz` and libjpeg-turbo's official `.deb` are
unpacked by the installer (a raw-LZMA reader and an `ar` walk, no `lzip`/`dpkg`
needed), with the `.deb`'s shared libraries kept beside a launcher.
`dependencies_auto_update` (off by default) installs missing or outdated tools
in the background.

### Configuration & credentials

- **First run asks six things and nothing else**: the music folder, your account
  (the login gate), the external tools (one download), the source keys and
  cookies, the Soulseek login and sharing, done. Every quality knob —
  the grading switches, the audit and verification options, the script chain,
  the naming, the interface — is NOT asked: the shipped defaults ARE the strict
  ones, and each setting is one click away in Settings afterwards (nothing a
  shorter wizard drops loses its only editor).
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
  `acoustid_api_key` (lookups) plus `acoustid_user_key` (submitting fingerprints
  to AcoustID — a different key from the same account), `ai_base_url` /
  `ai_api_key` / `ai_model` (script 17 and the genre ranking),
  `soulseek_username` / `soulseek_password`.
- YouTube downloads send **your own cookies** when you tell them to.
  `youtube_cookies_mode` is `none` (anonymous — the default, nothing is sent),
  `file` (the jar saved on **Settings → Videos**: paste a `cookies.txt` or drop
  the file on the box, and the app writes it to
  `<music>/.mlo/data/cookies.txt` — the one path it owns, never one you type) or
  `browser` (yt-dlp reads `youtube_cookies_browser`'s own cookie store). It is
  what opens age-gated and members-only videos and stops the throttling a fresh
  IP gets, and both yt-dlp paths — the importable module and the pinned binary —
  honour it.
- `GET /api/sources/health` lists every external source the app can ask (six
  lyrics, six advisory, eleven genre, four metadata, ten discover, one links —
  38 rows) with what each needs (`?probe=1` tests them). The RateYourMusic
  cookie is only needed for the *scrape* fallback, because MusicBrainz states
  the RYM album/artist page as a URL relation for well-known releases.
- …and seven more rows of kind `credentials`: the saved logins themselves
  (Discogs token, Last.fm key, Spotify client pair, AcoustID key, Soulseek
  account, AI provider, this server's own password). Those ask the provider's
  OWN credential endpoint — Discogs `/oauth/identity`, Last.fm
  `chart.gettoptags`, Spotify `POST /api/token`, one AcoustID lookup — so a
  REFUSED key is reported in the provider's words instead of leaving its
  source row looking green (Discogs' browse endpoint answers anonymously, so a
  discarded token used to leave no trace at all). `?kind=credentials` returns
  them alone; Settings → Sources and the setup wizard show them as their own
  group with a Test button.

## What the app does

### Library, search and identity

Artists → albums → tracks, with live grade/audit badges, a search box, a *fail
only* filter, bulk tag tools, custom tag columns and sortable/resizable columns
(year, grade, audit, genre, advisory, duration, bitrate, dynamic range…); music
videos are first-class tracks — and a web/digital album can fetch its own: the
album header's film button (or a track's "…" menu) searches YouTube through
yt-dlp, drops the file into the album folder and tags it as that track's music
video, so the matching panel is only needed for videos it did not download.
An album's cover carries its own badges — the
measured dynamic range (top-left) and, down the bottom-left corner as three
left-aligned rows, the medium, every country it was released in and the release's
format/bitrate (`CD` / `US, CA` / `FLAC 16/44.1`) — and every track row offers the
same "…" menu wherever it is listed: tagging (the tag editor, genre and advisory
imports), its scripts (lyrics, re-audit, ReplayGain, re-encode, re-grade),
credits, the stored readout and the track's own editor. A filled heart is drawn
whether or not the row is hovered: a favourite is a state, not an action.

The top search bar searches the library (with
`composer:`, `person:`, `genre:` and `tag:` prefixes) or MusicBrainz, and
`/mb/search` is a full in-app MusicBrainz browser (artists, release groups,
releases, recordings) with *Auto-import* and *Add to library* actions — and it
shows an entity's **alias in your locale** in parentheses beside its name
(`宇多田ヒカル (Hikaru Utada)`), picked from MusicBrainz's own aliases by the same
"Preferred locale for aliases" setting the beets import translates names with:
an exact locale wins over a regional or script cousin (`en` before `en_PH`,
`ja` before `ja-Latn`), then MusicBrainz's primary alias, and a search-hint
alias is never shown — and an alias is only shown when the name is NOT
already written in your own script: an English reader is never handed
`Radiohead (レディオヘッド)` just because MusicBrainz flags that alias
primary, while a Japanese reader is, and the mirror holds too (a Japanese
name is not romanized for a `ja` reader). Every alias rides along in the request that was already
being made, so nothing costs an extra MusicBrainz call. Entity pages carry
grading and auditing detail, MusicBrainz + RateYourMusic links, cover
upload/search, Wikipedia descriptions, manual tag editing, a lyrics editor, and a
**Credits** view built from MusicBrainz `artist-rels` (falling back to the file's
own PERFORMER/COMPOSER/… tags, and saying so). **Add to library answers at
the press**: the framework album (the folder the naming script names, with the
release's own tracklist and the release-group cover), its wish and the kick of
the one queue are what the reply carries, and everything the reply does not
need — the release resolution for an id-only add, and the album page's provider
content (cover candidates, links, metadata) — happens after the reply, on the
same daemon threads the rest of the app uses. A bare-release add that used to
sit on the button for 13 s of page-content fetches now answers in about a
second. The waiting itself is the Soulseek queue's: an add records a wish and
starts the worker's own pass (`POST /api/library/add` never searches), and the
album shows one tile for the whole acquisition — the framework album the add
created is the folder the import lands in, so it is never listed beside a
second folder it is about to become. The cover finder ranks every
candidate with the album's OWN identity — a karaoke/tribute row, another artist's
release or a different album can no longer win, an image whose size was never
measured cannot beat the `cover_target_size` floor, and the autonomous Covers
step refuses to store a below-target cover — and it WRITES the winner by default
(the release-group reference ranks first among the candidates); `cover_review` on
stages them instead, for a library that would rather pick by eye. The album's reference cover is the **release group's**
(`coverartarchive.org/release-group/<rg>/front-500`) — the image the finder shows
beside the candidates, the wizard's preview, and what a candidate is preferred
against; one release's own sleeve is a single edition's art, not the album's, and
an album that carries only its release id gets the group read anyway (the group
is read from the release, `spec R163`).
**The cover you pick is the cover that is written**: the pick is fetched from the
very URL you chose, and when that URL cannot be fetched the write says so and
writes nothing rather than letting another provider's image stand in for it (an
Apple storefront URL that answers empty is retried as the same artwork's largest
copy, which is the same picture, not another cover). Each cached image belongs to
the URL that served it, so a cover fetched for one album can never be handed to
another album's write, and every candidate's thumbnail is asked about with that
row's own artist and title — never the open album's release group.
**Recommended (Local)** and
**Home** are computed locally from the library's own tags (genre and family,
mood, energy, era, artist) — no provider, no model, no network.

**LIBRARY → BROWSE** turns the library into a query: 120 fields (tags, ratings,
grades, audit verdicts, technical facts, audio analysis, library/album/artist
facts) with the operations each field declares, a value control per type, match
all/any, a live "matches N tracks · M albums" count, sorting and grouping, and a
facet rail that narrows with multi-select. The same engine evaluates **smart
playlists** (`kind: smart` stores the filter, not the rows) and the rule editor
in a playlist reads the same field catalogue — one list, no drift. A saved rule
naming a field the catalogue no longer lists keeps its raw key and legacy ops, so
nothing anyone saved is rewritten.

**Ratings** are half stars in the UI and Picard's 0–10 in the store: click a
star's left half for a half star, click the set value to clear, arrow keys nudge.
They appear on library rows (both views), album rows, the album header average,
the album/track page headers and the player bar, and the query engine compares
in star space, so `rating >= 4` and the rating facet mean the same thing the
stars show. `write_rating_tags` decides whether the file's own `RATING` tag is
written too. Home carries a **Your ratings** shelf of the releases you have
rated, highest first.

**The library's five views** are Grid, Compact, Albums, Artists and Tracks —
each one draws rows, each with its own sort, column set and (for Tracks) a star
rating column of its own. The toolbar's filter menu carries the presets (*All*,
*Failing*, *CD rips*, *Digital*, *Instrumental*, *Music videos*, *No lyrics*)
plus two facets with live counts: **star rating** (Any / Rated / Unrated — over
your own ratings; an album counts as rated when its own folder rating is set or
any track in it is rated, an artist when any of its albums is, and nothing is an
average) and **advisory** (Any / Explicit / Clean, where Explicit is
`ITUNESADVISORY` 1 and Clean is everything that does not flag it — the clean
edition and the tracks nothing marked). The chip names every filter in force and
marks itself while any is on. A table filtered down to nothing says so in the
table instead of drawing a header over blank space.

### Discover, recommendations and watching

**DISCOVER** browses genres across every configured provider (MusicBrainz,
Deezer, iTunes, TheAudioDB, Last.fm, ListenBrainz, Discogs, Wikidata, Wikipedia,
Spotify, RateYourMusic, Bandcamp) with a library/online/both scope and an
albums/artists/tracks switch. Every row names its source, whether you already own
it, and what else holds it; each source's outcome is shown as a chip — *0
answered*, *skipped: needs a key*, *failed: the provider's own words* — never
swallowed. Owned rows open the real page; everything else offers **Add to
library**, which adds it to the queue and starts searching for its audio at once. **RECOMMENDED
(ONLINE)** takes a seed (the whole library, one of its genres, or the album /
artist / track page it sits on) and explains its basis on screen — the online
half of the pair of shelves an album, artist or track page shows, whose
**RECOMMENDED (LOCAL)** half is scored from the library's own tags. On an
entity shelf MusicBrainz answers through the entity's OWN genres
(`Genre: Shoegaze (MusicBrainz)`, and `More release groups by …` for the
artist's catalogue), Spotify adds the artist's albums and top tracks when its
credentials are saved, Apple's keyless search and Deezer's similar-artist feed
stand beside them — and a source that truly has nothing to say is reported in
its own words, never hidden.

**WATCHED ARTISTS** keeps a MusicBrainz artist under watch: policy (`new_only` /
`backfill`), the release types worth taking, an allow/never list of specific
releases, a per-cycle cap and auto-add. Each check queues a few release groups
into the download queue — never a discography — and the page reports real scheduling
("last check 3h ago (12 total) · next in 34m", or plainly that nothing is
scheduled yet). Watches inherit the whole acquisition chain below.

### Player

A persistent player bar (queue, drag-reorder, shuffle, repeat-one, speed, sleep
timer, ReplayGain, visualizer, app-wide volume) plus a fullscreen player with
animated karaoke lyrics. The fullscreen view is a two-column layout on a wide
window (cover + controls, lyrics beside it) and a scrolling single column on a
narrow or heavily zoomed one, where the lyrics pane keeps a real minimum height
instead of being squeezed under the fold. Nothing floats over the art as a
panel: the lyrics pane, the metadata block, the transport and the top bar draw
**no** background, border or blur of their own, and what keeps their text
readable is the ink itself — its colour is **derived from the cover** (the
cover's average colour, the same value the ambience is painted from): a dark
cover gets the white table and **nothing at all behind the text**, a bright one
flips to near-black ink over a lift built from the cover's own colour, never a
grey scrim (`spec R52c`, `npInk`). The glyph shadow flips with the ink — a dark
halo under light text, a light one under dark. Measured on three covers (dark,
mid-grey and a white one, in `tools/check_np_metadata_contrast.cjs`): every
metadata tier at 4.5:1 and the title at 3:1 or better, on both polarities. The player's floating menus (the display options, the
queue) keep a frosted panel, because a menu is a menu. Both lyric surfaces — this
pane and the right-docked sidebar viewer — carry the same size control: `−`, a
percentage you can type into, `+`, 5 % a press (85-160 %), remembered per
surface because a 380 px sidebar and a full-screen pane want different numbers.
The pane is a control you own rather than a consequence of the tags: one
microphone toggle in the player's own control row (beside the queue, the
visualizer and the display options — and the same button on the player bar for
the sidebar pane) shows and hides it, the toggle only appears while the current
track actually has lyrics, and hiding is seamless — the pane keeps its scroll
position, its zoom and its sung line while its box animates away and the cover
glides back to the middle. Nothing is ever drawn on top of the artwork. Both
lyric surfaces also carry the same **offset** control — `−`, the pending shift
in seconds, `+`, Save — for lyrics that arrive early or late against the track:
the step is a tenth of a second, the shift previews live (the highlight follows
the buttons), and Save writes it into the track's own lyrics, wherever they
live (the `.lrc` beside the file and/or its `LYRICS` tag). Every timestamp in
the text moves by that amount; a line with no timestamp is left exactly as it
was, and a stamp that would land before the start of the file clamps at
`[00:00.00]` instead of going negative. The same details the library rows offer
are one click from the player itself: the bar's own **ⓘ** and the fullscreen
player's options menu both open the track's **Details & credits** — the stored
readout, the audit verdicts and the MusicBrainz credits — without leaving what
you are listening to.
ReplayGain is applied through the WebAudio gain stage in
**track**, **album** or **off** mode (`replaygain_mode`) with a preamp
(`replaygain_preamp_db`, ±24 dB); a file without ReplayGain tags is measured on
the fly with ffmpeg's EBU R128 meter when `replaygain_analyze_missing` is on
(default), cached in `.mlo/data/replaygain.json`, with clip protection. The gain
is on the element before the first sample, and the on-demand measurement is
bounded to one second so the click is never held behind a decode: a track whose
measurement only finishes after that has it ramped in the moment it lands,
instead of playing the whole song at unity. Music videos carry the same gain,
and album mode on an album with no `REPLAYGAIN_ALBUM_GAIN` says in the readout
that each track fell back to its own gain rather than implying album
normalisation. Pressing play on what is already playing starts it over (the
album card, the album header, an artist's play all, a row and a queue row),
while the transport's play after a pause resumes where it stopped. Music
videos play at the correct aspect ratio, incompatible codecs are transcoded to
fragmented MP4 (`GET /api/videos/stream?transcode=1`), and the keyboard shortcuts
(`F`, `/`, `?`, Space, ← →, `[` `]`, `0`) live in
`web/src/components/Shortcuts.tsx`.

### Import

Drag & drop uploads, a watched import folder, staged `.mlo/downloads` or the
Soulseek paths, all through one pipeline (`server/imports.py`). **AcoustID
fingerprint matching** tells you which release the *audio* is, not what the tags
claim (needs `acoustid_api_key` and `fpcalc`). Accepting a match writes the
Picard-compatible pair `ACOUSTID_ID` + `ACOUSTID_FINGERPRINT` in one save and
reads it back to prove it landed; a container the app cannot tag is named per
file instead of failing silently, and a single identified track is enough to
decide an album. The wizard's **Submit to AcoustID** action then publishes the
pair the files already carry to AcoustID's database — a two-press confirm, and
it needs `acoustid_user_key` (a *user* key from the same account; the
application key can only look up). The wizard's eight steps are **Select & separate → Links → Match → Covers →
Genres → Lyrics → Advisory → Finish**, and *Finish* runs the scripts you ticked
on this album — the import chain's own ids, in its own order, until you change
them. There is **no "re-run the import chain" button** on that step: that press
went back through the whole import, which re-fetches the links, genres, cover art
and advisory and *empties* the four families an import decides first — it undid
the work just done by hand in those eight steps (`spec R9`). An album you
finished by hand is finished; any single script can still be re-run from the
album page, and Run All over the library lives on the Optimization page. The
**import script chain** then runs
(default `import_scripts`, i.e. `DEFAULT_CHAIN`, which *is* `run_all_order` —
one list, in `mlo/config.py`, so a script added to Run All can never be missing
from an import); `import_auto_scripts` turns it off, `import_scripts` replaces it
outright. An import decides four families for itself and **replaces** what the
download arrived carrying — the lyric the fetch found, the release's genres, the
advisory pipeline's rating, and the album's own cover art (the art a peer baked
into the files goes with it) — in `server/imports.py::drop_arrived_values`, the
import's first tag-writing pass. It has to be the first one: every writer for
those four *fills* an empty slot rather than replacing a full one, which is what
keeps a user's own edit, and an import empties the slots before those writers
run. Everything else an import touches still only ever **fills**, so what you
typed in the wizard survives; a family you kept in `import_review_families` (and
one in review mode) is left exactly as it arrived, and
`import_keep_synced_lyrics` is the lyric family's one exception — on, a track
whose lyric already carries timestamps keeps it. **Bulk import** queues several
albums with `import_bulk_concurrency` (2 by default, 1–8).

**An import finishes on its own, and only asks when the answer is yours.**
`import_autonomy` ships as `automatic`: the pipeline decides every family it can
— links, cover, genres, lyrics, advisory, artist image and descriptions — runs
the configured chain, and then *reports* what no source could supply; it never
stops to ask. A lyrics search that comes back empty settles the track instead of
parking the album: with no lyrics from any configured provider and no source
saying the track has vocals, it is marked `INSTRUMENTAL=1` with the app's own
provenance (`lyrics-none`, `spec R162`) — so a library of instrumentals imports
to a graded album rather than to a queue of questions, and a track whose lyrics
*are* found is never touched. What is left after all of that is genuine: a
family you kept for yourself (`import_review_families`, `cover_review`, or
`import_autonomy: "review"`), or one no source could state at all — and THAT is
what the notification menu and the Soulseek page's *Needs you* row carry
(`spec R121`). **While an album waits on you it is parked**: a library-wide
`Run All` skips it and logs which albums it left alone, so scripts cannot
rewrite a release you are halfway through answering (`spec R161`); the wizard's
own steps are the press that finishes it, and each family's own button in the tag
menu re-decides one thing by hand through the same code the automatic path runs.

**An import never loses the album it is working on.** The chain is scoped to one
album, and *Beets tagging* (script 14) moves that album into the library under
the naming script and renames every file in it — so the app follows it: every
later script acts on the folder the album is in *now*, and where the chain ended
is what the queue row, the album page and the notification all name
(`spec R164`). Measured on a real import: the report used to name the folder
beets left, which the very same step's organize pass had already renamed, so
every script after it ran against the emptied download folder — Format all and
Grade both reporting nothing while the import looked like a success. The album's
own files travel with it too: the cover the import fetched, its description, its
expected-tracklist manifest and the rip's CUE/LOG all move with the audio
wherever it lands, and a file whose name the album folder already holds is never
overwritten — you are told which one stayed (`spec R165`). In practice an
automatic import lands a graded album **with its cover art**, instead of handing
you a release to finish by hand over artwork the app had just fetched.

**When the edition the app picked is not on the network, it does not give the
album up.** *Add to library* on a release group tries that group's ranked
editions IN ORDER — the best pressing first, then the next, **three by default**
(`soulseek_fallback_candidates`, 1–10; `1` is the best edition only) — and each
of those searches gets its own window before it counts as not found and the walk
moves on (`soulseek_search_timeout_seconds`, **60 s**, per candidate). Editions
that would be the SAME search are skipped, not re-asked: separate MusicBrainz
releases really do share a catalog number (one CD issued under two labels —
`GED 24425` beside `GED24425` — a reissue, a country variant printed unchanged),
and the number is what a CD search is keyed on, so the second of those could only
find the folders the first already found (`spec R169`; the walk names what it
skipped, and an edition stating no number at all is always still worth asking
for). A release
group with fewer editions than that simply ends at the end of its own list, and
nothing waits for an edition that does not exist. It never looks stuck while it
does: the album's row and the queue say where the search is (*"release 2 of 3"*),
and a search that lands an edition other than the best says which one it was. If
none of them is there, the release is **not dropped and its album is not
removed**: it moves to the queue's own **Background** list — one row per release,
however many editions it is trying — and keeps being searched on the worker's
schedule until one lands.

The **Genres** step asks the whole configured chain with **one button**: every
source the app knows, asked in the order `genre_sources` lists them —
RateYourMusic first, then MusicBrainz, then the rest (ListenBrainz, iTunes,
Last.fm, TheAudioDB, Wikidata, Bandcamp, Discogs, Deezer, Spotify) — merged
per track, each source asked at the finest level it has: **MusicBrainz falls
back inside its own answer** — a track whose recording carries genres keeps
them, and anything it does not state is filled from the release, then the
release **group**, then the **artist** — so a MusicBrainz genre is usable even
when that particular track has none, and the answer says which level spoke
(`genre_cascade`'s `levels`/`source`). The same ladder runs for the other
per-track sources (ListenBrainz per recording → release group → artist, iTunes
and TheAudioDB per track, Last.fm `track.getTopTags` → `artist.getTopTags`),
while Bandcamp, Discogs and Deezer only state an album's genres and Spotify an
artist's — a source that cannot answer per track answers nothing rather than
passing an album's list off as a track's. And the chain **stops as soon as a
track's list is complete**, so the
later sources cost nothing on an album the first two can answer and still answer
for a pressing nothing else knows. The list is the PRIORITY list: the small tray
next to that button (and Settings → Import) ticks sources in and out, and a
ticking is saved in the chain's own order, so what you see top-to-bottom is what
is asked first. A source that needs a credential is named as such and stays
tickable — an uncredentialed source is skipped before any request and reported
by name in the answer's `notes`, never silently dropped. The same chain runs
wherever genres are imported (an album's `…` menu, a selection, the batch menu)
and — with no click at all — on every import: `server/imports.py::_stamp_release`
asks it for each album that lands.

The **Advisory** step resolves `ITUNESADVISORY` from every applicable source —
Deezer and Spotify by ISRC (every ISRC the file states *and* every one
MusicBrainz holds for its recording), Apple's explicit-edition album route and
Apple's exact-title song search — merged so explicit anywhere wins, and derives
`ALBUMITUNESADVISORY` from the per-track values with script 8's own rule (the
*Fetch / refresh advisory rating* action derives it too, so a manual fetch never
leaves the album tag stale). A source that STATED a value ends the question: it
is written as it stands, with that source's own provenance, and a stated `0` is
final — nothing re-opens it. The configured AI provider answers what nothing else
could: it is asked ONLY when every source came up with nothing at all (when
`advisory_ai_classify` is on) and it is fed the track's own words (embedded
lyrics first, else the `.lrc` sidecar). It is asked about the SONG, not its
vocabulary: `1` is profanity that is excessive or a slur or a very strong word,
or graphic sex/violence/drug use, while a mild word in passing — a lone `ass`,
`damn` or `hell`, an idiom, a quote, a word ordinary in another language — is
`0`, and lyrics in any language or script are judged in that language. Per source
the STRONGEST answer wins — every ISRC is asked, so a later pressing's explicit
answer is not lost to an earlier clean one. Each track's value names the source
behind it, and the reply carries every answer it weighed beside it; a track no
source could state anything about is not invented — `mlo/advisory.py`
decides (instrumental → the configured AI → `advisory_fallback`) and reports the
stage it used. What an IMPORT fetches on its
own never re-asks a track that already holds 0/1/2: it reports the value back
with its provenance — `the file's own tag, not re-checked` — because nobody
pressed anything there. Every action a person presses asks the providers again
(the `force` request), and each surface has exactly one: *Fetch / refresh
advisory rating* in the tag menu, the wizard's *Auto-import advisory for all
tracks*, the metadata review's check and the track page's check; even then an
invented fallback never overwrites a stored rating. The two
tags answer to their own switches:
`advisory_auto_fetch` for the per-track rating, *Auto Album Advisory*
(`auto_advisory`) for the album tag script 8 derives.

### Soulseek & the download queue

A managed slskd instance (autostart, shares = the library folder
`<music>/Artists`, a share rescan scheduled whenever the library changes), with
search & download UI, a live status
dot, share browsing, bulk and whole-user downloads, transfer-level clearing and
staging management (`GET /api/soulseek/staging`). **Transfer progress is
pushed, not polled**: the same rows `GET /api/soulseek/downloads` returns —
and every live job's own progress block — also ride the progress WebSocket as
`{"type":"transfers"}` frames (`server/main.py`; the page draws its Downloads
tab, Auto-import card and queue-row bars from them, `spec R120`). The cadence
follows the bytes: 0.4 s while a transfer is in progress or a job is running,
5 s otherwise, nothing at all when nothing changed and no slskd request while
no client is connected. Measured against a 2 MB/s transfer on a scratch
instance (a fake slskd serving slskd's own transfer tree, the real backend and
the real UI): the bar moved every **0.40 s** and what it showed was **0.17 s**
old on average (p90 0.31 s), where the **3 s** poll it replaced gave **3.02 s**
and **1.48 s** (p90 2.79 s); a transfer finishing left the Active list
**0.38 s** after slskd reported it; the frames cost ~370 bytes each (2.5 a
second while bytes move, 0 when a queue has settled). The page's own poll of
that query stays as a 30 s fallback for a dead socket. Progress is status, not
an outcome — those frames never reach the notification tray or the OS
notifications.
**An import that needs a hand is an outcome, and it is announced in three
places** (`spec R121`, `spec R166`): an album the pipeline could not finish
raises `import_needs_data`, which the desktop/mobile shell raises as a system
notification and the bell's tray keeps, arrives in-app on the same event
channel, and appears on the Soulseek page's **finished row for that release** —
which carries the warning "*Needs data: …*" naming the missing families in the
app's own words, beside *Enter manually* (the import wizard at that album's
first missing step, `/import?album=…&step=…`) and *Mark complete* (stop asking;
a later import of the album asks again only if something is still missing) —
and on the album page itself, which shows the same sentence with the same link.
**Nothing about the album waits**: it is in the library, graded like any other,
and the release reads as finished rather than as a stall. Only a genuine wait —
a *review* import stopped before its chain ran, or a disc structure whose main
feature is unpicked — sits in the *Needs you* section, and only those are
skipped by a library-wide Run All. It fires on that state only: never for
progress, and never twice for the same album unless what is missing changes. **Is the port open?** — the
tab's *Test port* action (`GET /api/soulseek/port-check`) answers it with five
rows that each say what they prove: a real TCP connection to the listen port
here (plus a bind test to tell "nothing is listening" from "something else holds
it"), what the ROUTER itself lists for that port with its own words and the
lease, the LAN-vs-WAN address shape (a CGNAT is named as one instead of being
blamed on a firewall), a self-connect through the public address (refused ⇒
"unknown", because a router without NAT hairpinning refuses it while the port may
still be open) and slskd's own signed-in state. A definite answer about the
internet needs a probe from outside this network, which the app does not ship —
and the panel says so. Which EDITION is fetched is the release-choice policy's (`mlo/release_choice.py`), and it is the same policy the release-group page shows: official editions ahead of everything else, the configured medium order (CD, then the other physical media, digital last), the original ahead of a reissue — and a **box set below the album itself**, whether it is the DVD/Blu-ray bundle (media this library cannot use) or disc after disc of the same record, so a five-disc anniversary box no longer outranks the plain CD it contains. A
COMPRESSED derivative of a disc sorts below the disc's own streams too
(`prefer_disc_streams`, on by default): an edition that names itself a BDRip,
a DVDRip or an x264 re-encode ranks under a remux or a full disc of the same
release group, and a folder holding a `VIDEO_TS`/`BDMV` structure beside a
700 MB re-encode is remuxed as the disc's own single title, the derivative
left where it is. Turn the setting off and the disc handling goes with it —
those files take the ordinary per-file path and the other rules decide, exactly
as they did before any of this existed.
**The original pressing wins the date, exactly**: an edition is scored by how
close it sits to the release group's own first release date, the penalty for
distance is strictly increasing in the gap — never flat, so two reissues a
decade apart are not a tie that MusicBrainz's listing order gets to settle —
and among editions of the same year the one that states its release date **in
full** (`YYYY-MM-DD`) beats one that states only its month or its year, because
the album folder is named after that date.

**Where a release is fetched from is decided by the release itself**, from the
recordings' own `video` flag and the medium MusicBrainz publishes: a music-video
release on **Digital Media** cannot be on Soulseek as a folder — no disc, no log,
no CRC — so it is fetched from YouTube with yt-dlp *inside the same auto-import
job*, with no search at all. One query per track through the filter the film
button already uses (achieved length, lyric/cover/tribute rows refused), into
`<downloads>/YouTube/<Artist - Album>`, renamed to `<disc>-<NN> <title>` before
the import so nothing is named after an upload title, and then the SAME import
the Soulseek path runs: MB stamping, `MEDIA=Digital Media`, `SOURCE=YouTube`
(the closed vocabulary, so the grader's Digital Media rule is satisfied), the
naming script, and the configured post-import chain in the background. A
music-video collection is routinely a dozen separate uploads, so a partial
result is a normal outcome — the album imports from what came back and every
missing track is named in the log and counted in the job's result — and finding
NOTHING ends on the same wish offer an empty search does, never a silent
success. A music video on a DISC (DVD, Blu-ray, VHS, Video CD — what
`mlo.release_choice.is_video_format` classifies) keeps the Soulseek path byte for
byte, an audio release is never routed anywhere, and a medium that is unstated or
unknown is never guessed at.

The **auto-importer** searches
each release by what can only point at THAT release: a physical pressing (CD
included) is searched by its catalog number and barcode
(`soulseek_auto_physical_queries`) and, when it states neither, by its label and
country — never by a broad artist/title/album query, which drowns the result
list in other pressings. Digital Media may be searched broadly
(`soulseek_auto_digital_queries`, `artist album year`); `soulseek_auto_cd_queries`
still overrides the physical default for a CD a user sets it for. Search terms
are stripped of the punctuation and typographic marks no share folder carries
(full-width `！`, quotes, brackets) while non-Latin script is kept, and a release
whose titles are in another locale opens extra searches using its MusicBrainz
aliases in the configured `locale` (`ぴーなた` → `pinata`). It gates a CD
candidate on its rip log *before* requesting any album byte
(`soulseek_auto_log_min_score`, default 100), ranks candidates towards the copy
that arrives fastest — lossless first, then the match score, then the peer's own
advertised speed and queue — and downloads up to **three candidates of one
release at once** (`soulseek_candidate_slots`), from three DIFFERENT peers (two
folders of one peer are two copies on one machine, so the second keeps its place
and comes back in a later batch): the first that passes the same
verification becomes the import and the others are cancelled and swept, so a
peer that stalls does not cost the whole album. The next candidate of that
release is only asked for when one of the three lands or is rejected — the app
never has more than that many of one release's peers transferring, however many
the search turned up. It verifies completeness
(`soulseek_auto_complete_ratio`) and losslessness, and cleans up everything a
rejected candidate left behind.

**Two limits, and what happens over them.** `soulseek_search_concurrency`
(default 3) is how many *releases* the pipeline works on at once, and
`soulseek_candidate_slots` (default 3) is how many *candidates of one release*
download at once; **the app enforces both itself** — the fourth release is never
refused, it takes its place in the queue's **Waiting** group (with its position,
and cancellable there without ever starting a byte) and starts by itself the
moment one of the running releases finishes. Releases whose ranked-edition walk
is spent are their own **Background** group, one row per release: they are still
being searched, just not right now. slskd's own
`soulseek_download_slots` is the OUTER ceiling on the transfers that produces,
which is why it defaults to 9 — the product of the two (3 × 3) — and a config
that sets fewer slots than its two limits need gets each release's candidate
batch narrowed to fit (`slots ÷ releases`) instead of queueing inside slskd. The
Queue tab's header reads all three back.

A release is imported **once**, into one album folder: the destination is
checked against the release's own MusicBrainz ids, so a download of an album the
library already holds is refused with a sentence instead of appearing beside it
as `… (2)` — and two jobs heading for one folder serialize on that folder while
their chain runs scoped to it, never over the library. That claim belongs to the
download **job**, and it is carried onto the background chain the import starts
(`server.script_runners.claim_paths`, `server.soulseek_auto._start_import_chain`):
the album stays locked from the first tag write through the last script — and so
does the folder the download came from, so nothing imports or clears it while
the chain is still finishing that release. The album page's chain press (the
tag-actions entry) on such an album is therefore answered at once, naming the
job that holds it, instead of starting a second chain over the same files. The bar takes **any MusicBrainz link**: a release, a
release-group (the group's ranked editions are walked, R150), an artist (its
discography is queued in the background, the albums appearing as they are
created) or a recording — paste the page URL or the bare MBID, and it goes
through the same `POST /api/library/add` the MusicBrainz pages' own *Add to
library* uses, with a bare MBID resolved server-side so both paths agree about
what was asked (`spec R170`). Adding a release to the
library **starts its search immediately** and puts it on
the **download queue**, Notifications cover the whole way in, switchable in Settings → Notifications (on by default): the add itself, the download starting and finishing, and the import starting and finishing with the chain's own summary. which is the one surface for wanted releases: a row shows
what the search is doing, and it stays **In progress** — never Completed — while
the import chain it started is still running over the album. The durable
behaviour behind it (re-searching on
`wishes_interval_hours`, default 6, with retry backoff) keeps looking **until the
release is found or the user cancels it** — a release nobody is sharing this week
is not abandoned. A release that already failed is not re-attempted on the next
pass: the retry decision comes from the recorded attempt and backoff state. When
the import lands, the downloaded copy is deleted
(`soulseek_clear_downloads`, ON — the import MOVES the album into the library, so
the download dir is only staging; a failed import keeps its files so its retry
does not download them again), and a terminal failure removes the framework album
the add created. The add's page prefetch runs in the background, so an import can
finish the album while it is still fetching: a folder that holds audio is never
re-marked pending, which is what keeps a filled album from reading as a wish
again. *Import all completed*
imports every finished download **sequentially**, with cancel finishing the album
in flight (`GET /api/soulseek/import-all/status`).

**The search asks more than one pressing.** Every add records the release
group's ranked editions on its wish — best first, deduped by folded catalog
number, so two releases printed with `GED 24425` and `GED24425` are ONE search
and not two spent windows (`spec R150`, `R169`, `R175`) — and the search walks
them one at a time inside the one wish: each candidate gets
`soulseek_search_timeout_seconds` of quiet (60 by default, 5–300) before the walk
moves on, up to `soulseek_fallback_candidates` editions (5 by default, 1 = the
best one and nothing behind it). A group with fewer eligible editions simply ends
at the end of its own list — no error, no empty slot — and a walk that is spent
is not a give-up: the release moves to the queue's **Background** section, keeps
its framework album and its place in the pipeline, and is re-walked from the best
edition on the worker's own ticks until something lands or the user cancels it
(`spec R151`–`R153`). A release is still **ONE row** whatever the walk does, and
that row says which edition it is asking with its own badge — `release 2 of 3`,
the server's wording, with the edition and what already came back empty in its
tooltip (`spec R176`). **What counts as a good rip log is 100, everywhere it is
asked**: the acquisition gate (`soulseek_auto_log_min_score` — a CD candidate's
`.log` must score 100 in Logchecker and its checksum must verify before its audio
is even queued), the grading check (`grade_log_score_threshold`) and the audit
verdict (`audit_log_score_threshold`) all ship that way, and a CD candidate that
carries no log at all is offered through an explicit confirm instead of being
accepted quietly (`spec R174`).

The download dir is also capped: **two independent size caps**, both 5 GB by
default and both editable in Settings → Storage (`soulseek_cache_cap_gb`,
`trash_cap_gb` — 0 turns one off; one store filling up never eats the other's
room). The Soulseek one covers the download dir plus the `incomplete` sibling
slskd stages partials in, the trash one covers `<music folder>/.mlo/trash`
across every per-user bin; over its cap, a store is emptied **oldest entry
first** — the same entries the Downloads and Trash pages delete, one at a time —
until it fits, and the pass stops there. What is **in use is never touched**: a
path an import or a script run holds (`server/job_locks`, and the report names
that job), or anything a transfer still running is writing into (its peer, the
folder it is writing in and, for a loose partial, its file name — read from
slskd's own transfer list). An entry that will not delete is kept and reported,
so the cap simply stays over until the transfer ends. A trash entry the cap
deletes loses its origin record with it, exactly as the Trash page's own Delete
does, so everything the prune KEPT is still restorable to where it came from.
The pass runs in the background (a worker that starts with the app and ticks
every few minutes), so the caps hold on an install nobody is watching, and each
prune announces itself with one notification naming what was freed.

The queue's own controls follow from those two limits. **Clear all** empties the
*queued/waiting* work in one press — every release that has not started goes,
before it downloads a byte — and it does NOT touch a release that is already
RUNNING (that is a cancel, on its own row), a finished row, or anything in your
library. **Select** turns the rows into checkboxes: *Cancel selected* cancels
exactly the ticked rows in one call (the ids the server names them by, so a tick
stays on the row you picked while the list refreshes underneath it), and *Import
/ commit selected* does what each ticked row is for — an album sitting finished
in the download folder is imported, a settled row is taken off the list — with
the count of what it acted on reported back. Waiting releases are their own
group, in the order they will start, each with its position.

The **listen port** is opened on the router by the app itself
(`soulseek_upnp`, ON): slskd has no UPnP/NAT-PMP option — upstream closed the
request unimplemented — so `mlo/portmap.py` does it, trying UPnP IGD first and
NAT-PMP behind it. A mapping is only reported as made when the gateway confirms
it, and the Soulseek page says which of the two answered (or the router's own
words when it refuses) instead of only writing a port number into slskd's
config.

### Optimization — the 21 scripts

Optimization → *Run All* executes `run_all_order`, shipped as **11 → 3 → 14 → 15
→ 2 → 1 → 13 → 18 → 17 → 8 → 5 → 19 → 6 → 7 → 9 → 12 → 16 → 10 → 20 → 21 → 4** —
everything that moves a file first, everything that reads it last. Every script
also runs on its own, on a selection, or with its force flag from the *Re-run &
overwrite* menu. A library-wide run holds every folder it walks — the library
root and any album filed elsewhere in the music folder, since the sweeps start at
`music_folder` — so nothing else can rewrite one of those albums underneath it,
while an album-scoped run (an import chain, a press) holds only its own album.
A script that **moves** an album takes that claim with it: the folder the audio
left is freed and the album's new folder is locked before the next script writes
a byte into it (`server.job_locks.move`).

| # | Script | What it does |
| --- | --- | --- |
| 1 | Format lyrics | Canonical embedded LYRICS / `.lrc` (padding, blank lines, zero-timestamp rule, Enhanced/Extended LRC word-sync); MEDIA/SOURCE normalization |
| 2 | Format CUEs | Canonical CUE text, `FILE`-line fixes, `CD-N` sheet renaming |
| 3 | Optimize FLACs | Re-encode at `library_codec_quality`, strip padding/CUESHEET/APPLICATION and tags outside the canonical set, convert non-conforming files to `library_codec` (what `library_codec_optimize` permits) |
| 4 | Grade | The full grading battery (see the spec) |
| 5 | Process images | Covers resized/cropped (`cover_target_size`, default 1200; per-format targets), JPEG/PNG/JXL optimization |
| 6 | Audit library | AudioAuditor detectors + CD `.log` CRC verification → `AUDIT` |
| 7 | DR & ReplayGain | in-process loudness-war DR tags + rsgain ReplayGain (album gain, FLAC and MP4 alike; rsgain measures, the app writes) |
| 8 | Auto tagging | `ITUNESADVISORY`, `INSTRUMENTAL`, `MOOD`, `ENERGY`, `GENRE` |
| 9 | AccurateRip | CUETools `.accurip` generation and verification; an existing file is regenerated only when a track's audio changed (tag writes do not count) |
| 10 | Format all | Final canonical pass: `.accurip`/`.cue`/`.lrc`/tag trim + the embedded-cover policy |
| 11 | Remux videos (MKV) | Any video container → MKV, video copied bit-exact when possible, audio to FLAC, chapters kept |
| 12 | Key & BPM | librosa-backed `INITIALKEY` + `BPM` |
| 13 | Fetch lyrics | The configured synced-lyrics chain into the configured format |
| 14 | Beets tagging | Managed beets (Picard parity) with the naming script and work/movement tags |
| 15 | Release tracklist | Writes `.mlo_expected.json` (the release's own tracklist) into the album folder |
| 16 | Mood & Energy | The mood classifier alone (`MOOD` + `ENERGY`) |
| 17 | Lyrics transliterate (AI) | `TRANSLITERATION-<LANG>-LATN` / `TRANSLATION-<LANG>` tags and sidecars, re-synced at `lrc_sync_level` |
| 18 | Publish lyrics (LRCLIB) | Submits this library's lyrics for recordings LRCLIB does not have (`lrclib_auto_publish`, `force_publish`) |
| 19 | Optimize artist images | Crops `Artists/<Artist>/artist.*` to `artist_image_aspect`, downscales to `artist_image_target_size` (never upscales, and back to the size it recorded writing when a file was enlarged afterwards), re-encodes as `artist.jpg`/`artist.png` |
| 20 | Scan library layout | Walks the music folder's shape and — with `layout_apply` (ON) — FIXES the three unambiguous findings: a name whose letter case differs from `naming_script` is renamed to the script's spelling, audio sitting outside any album folder is moved into the one its own tags name, and an artist folder with no album goes to the Trash. Everything else (stray files, unexpected folders, empty albums, unreadable albums) is reported, never guessed at: nothing is deleted, a destination that already holds a file is refused rather than overwritten, and only paths inside the music folder are ever touched. Writes `.mlo/data/layout_report.json` (`scanned_at` + per-row `fixes` included) — the Library page's warning and the Optimization panel's report read it instead of walking again |
| 21 | Fix AcoustID pairs | Completes a half-written AcoustID pair: an `ACOUSTID_ID` with no `ACOUSTID_FINGERPRINT` gets the local `fpcalc` fingerprint, a fingerprint with no id gets the lookup. Both halves present (or none) is left alone — it is the fixer for the grading failure *Missing ACOUSTID_FINGERPRINT (incomplete AcoustID pair)*, and it writes only the half that is missing |

**What script 17 transliterates and translates is decided from evidence, not
from the letters alone** (`spec R167`). The one rule
(`mlo/lyrics_xlit.xlit_needs` — asked by the script *and* by the grader, so the
two can never disagree) reads, in order: the track's own `LANGUAGE` tag, which
an import fills from **MusicBrainz's release text representation**
(`text-representation.language`, e.g. `jpn`, normalized to the app's own codes;
written only into an empty tag, so your own value stands) — then the lyrics'
own script (kana is Japanese, hangul is Korean, and Cyrillic or Han states
nothing, which is exactly the case a declared language settles) — then the
function words of the languages the app knows. A declared language is what
separates two Latin-script languages, which a script test cannot: a Turkish
track with no English stopwords in it used to read as "English" and got no
translation, and it does now. When nothing can say what the lyrics are in,
script 17 asks the configured AI **one question about that track** and stores
the answer in the `LANGUAGE` tag, so the question is paid once per track and
the grader reads a stored fact instead of asking a model anything. Codes that
state nothing (`mul` — a compilation, `und`, `zxx`) are asked past rather than
obeyed. The run's summary reports `language stored N` beside its transforms.

Force flags, one per script: `force_lyrics`, `force_cue`, `force_tracklist`,
`force_reencode_flac`, `force_reencode_images`, `force_audit`, `force_accurip`,
`force_dr_replaygain`, `force_audiometa`, `force_mood`, `force_auto_tag`,
`force_xlit`, `force_publish`. A run's force selection is *authoritative and
complete*: the flags it does not name are turned off, so unchecking a script in
the one-shot **Force** menu really turns it off rather than falling back to a
saved switch — and a caller that has no selection of its own omits it entirely
(every import path does), which is what leaves the saved switches in charge.
Script 20 is the one key that turns work OFF (`layout_apply`): untick it and the
layout pass reports without renaming or moving anything. Scripts whose feature
has its own off switch are skipped rather than run as no-ops:
`dr_replaygain_enabled` (7), `audiometa_enabled` (12), `mood_enabled` (16),
`lyrics_xlit_enabled` / `lyrics_translate_enabled` (17), `lrclib_auto_publish`
(18), `acoustid_enabled` (21).

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

**68 checks** across tracks, albums, artist folders and folders, all toggleable
on the **Grading** page with a live filter, enable/disable-all and the **Strict /
Balanced / Relaxed** presets. Grading ships **strict**: every check is on in the
factory defaults — `grade_check_audit` (the AUDIT-tag requirement) included, and
every file category admitted — and the **Balanced** preset is those defaults as
they were before 3.7.0 (audit tag not required) for anyone who wants the old
answer in one click. A verdict is binary: an album is `PASS` only when every enabled check
passes, otherwise `FAIL` with the failed checks itemized — and every problem the
page lists is charged, so an album can never show *N problems to fix* beside a
green dot (a CD verdict whose own evidence nothing established is a failed
check of its own, not a note beside a pass). The summary counts
checks (`summary_pass` / `summary_total`) and reports `albums_passed` /
`albums_failed`, plus `albums_audit_failed` for albums that pass every check
while their audit is FAKE/Mix (the library badges those red on the Audit column).
A check that raises counts as *could not be evaluated* and fails, so the
percentage always covers every enabled check. Two checks follow the codec target:
`grade_check_lossless_source` stands down (and is not counted) when `library_codec`
is an uncompressed container (`wav`/`aiff`) or `keep`, and `grade_check_cd_format`
exempts a file that already is the configured lossy target. Artist folders are graded on
exactly two things — image and description — by `grade_artist()`.

A tag **value** has one canonical form (`mlo/tagtext.py`), applied on every
write — the beets import, the wizard, the auto-import chain, every script and a
manual edit — and re-applied over a whole library by **Format all** (script 10):
the tags whose value set is closed (`MEDIA`, `SOURCE`, `RELEASETYPE`,
`RELEASESTATUS`, `AUDIT`, `RELEASECOUNTRY`, `SCRIPT`, `MOOD`) are spelled the way
the app stores them, spacing is collapsed, and a value the vocabulary does not
know is left alone rather than coerced. `RELEASECOUNTRY` holds EVERY country the
release's own events state (`US; CA; XE`), earliest event first, as repeated
container fields — a file already carrying one of them is completed rather than
left short, and the album badge, the queue rows and the naming script's
first-value rule all read that one form. `grade_check_tag_case` and
`grade_check_tag_spaces` fail what those writers would have fixed; free text
(`TITLE`, `ALBUM`, `ARTIST`, `LABEL`, the lyrics) is never touched, which is what
keeps `AC/DC` and `k.d. lang` intact. Genres are stored broad-first too:
`Rock / Shoegaze / Dream Pop`, the family and then what names the music.

A tag with more than one answer is a LIST, stored as repeated container fields
(one comment / frame / atom per value, `; `-joined on read) — the credit roles
(performer, producer, engineer, mixer, arranger, conductor, the work's
songwriters), the track's ISRCs, the release's countries and the genres — and a
list is DATA: a release with two engineers is written with both, and a file that
already states one keeps it and gains the other rather than being cut down to
one value. A music video's container holds one string per key, so there the same
list is stored `; `-joined; `%genre%` is that whole list both in the beets
import and in the organizer (only `RELEASECOUNTRY` and `LABEL` reduce to their
first value, to keep one album's path deterministic).

**The specification** — every check id, what it asserts, its default, the
presets, the audit workflow, tag families, quality bars, score semantics and a
runbook — is
[`docs/OPTIMIZATION-GRADING-SPEC.md`](docs/OPTIMIZATION-GRADING-SPEC.md).

### Export, playlists, offline

**Export** writes a playlist, albums, artists, tracks or the whole library as MP3
(VBR/CBR or custom), AAC, Opus, Vorbis, WAV, AIFF, ALAC, WavPack, WMA or a
bit-exact `copy`, in the shipped `albumartist_album_disc` layout —
ALBUMARTIST / Album / `1-01 Title`, the disc number included, so a two-disc
album keeps its discs apart and a compilation stays one folder — or `album`,
`flat`, `mirror`, or `custom`, a structure you type in the same Picard-style
tag grammar the library's own naming script uses (`export_codec`,
`export_structure`, `export_structure_script`, `export_subfolder`; the Export
page previews a custom one and refuses a field the app does not know). **Where it goes is a
choice**: `export_target` is `zip` — the client downloads one archive (the only
mode a browser can honour, and the default) — or `server`, a folder the machine
running the app can see, picked with the drive list and the free-space readout
(`export_dest`). Covers, ID3v2.3 plus optional ID3v1, `.m3u8` playlists, `.lrc`,
`.cue`, `.log`, `.accurip`, descriptions and the artist image travel with the
files, `export_manifest` writes a `checksums.sha256` beside them, and every
written file is re-opened and verified. Sync mode (`export_prune`) removes audio
the run did not write; exporting *into* the music folder is refused.

**ReplayGain** is a mode, not a checkbox: `export_replaygain_mode` is `off`,
`tags` (the portable choice — measure and write `REPLAYGAIN_*`) or `apply`, which
rewrites the audio so the files themselves are level — the ALBUM gain for a
whole-album selection (so the tracks keep their relative balance), the track gain
otherwise, applied in the same encode, with the `REPLAYGAIN_*` tags stripped
because a player would otherwise apply the gain twice. **An equalizer** rides
along: `export_eq_profile` selects one of the built-in curves or a profile
imported from **Equalizer APO / Peace EQ** (`Preamp:`, `Filter N: … PK|LS|HS|LP|HP
Fc … Gain … Q …`, `GraphicEQ:` band lists — pasted or uploaded on the Export
page, stored under `<music>/.mlo/data/eq/`). The order is ReplayGain gain → EQ
preamp → EQ filters → encoder, the curve touches the EXPORTED copies only, and
anything the profile cannot be rendered from (`Include:`, unknown constructs) is
reported instead of silently dropped. Both processing modes need a real codec —
a copied stream cannot be filtered — and say so. **Playlists** are manual
(drag-reorder, favourites, `.m3u8` import/export) or smart, driven by saved
grade/audit/tag filters.

**Offline**: "Download" caches a track's audio in the service worker's media
cache and warms the album/artist payloads around it, so the UI opens and a
downloaded album plays with the server down. In the Tauri shells, where no service
worker runs, a smaller JSON cache (GETs only, 512 KiB per entry, 3 MiB total) and
`blob:` playback cover the same case, and an **Offline** pill says when a stored
answer is being shown. Writes, Soulseek, imports and exports still need the
server.

**What a downloaded copy HOLDS, and which copy PLAYS** are two settings
(*Settings → Downloads & playback*, R171–R173): `download_codec` ships as
**`copy`** — the cached bytes are the library file's own, so a track is never
downloaded in a codec it is not already in — and a codec target instead
re-encodes the track for that device's cache only (`download_bitrate` is that
target's rate: kbps for MP3/AAC/Opus, Vorbis' 0-10 scale for Ogg, 0 for the
codec's own default; the library file is never touched, and the bulk transfer
route stands aside with a 409 while a rendition is configured, because it frames
each file's size up front). `playback_source` ships as **`stream`**: the player
asks the server for the library file even when a copy is downloaded, or plays the
downloaded copy instead (`downloaded`) — one resolver decides it for every
surface, the copy is played whenever the server cannot be reached (a preference
never strands the player), and a stream that a copy exists for carries
`nocache=1` so a cache-first service worker cannot answer it with the very bytes
the setting asked to avoid. The cache key holds no session token, so a download
survives a re-login — which is what makes `blob:` playback work in the shells,
where every media URL carries one.

### Notifications and languages

The backend publishes every settled outcome on `/ws/events` — `wish_found`,
`wish_failed`, `wish_not_found`, `download_started` and `download_done` (a
Soulseek job beginning and landing), `upload_started` (a peer starting to
download from your share), `download_failed`, `import_ready`, `import_needs_data`,
`script_done`, `script_failed`, `grade_done` and `update_available`; each client
keeps that socket open and raises an OS notification
(Tauri's plugin on desktop and mobile, the Web Notification API in the browser)
with an in-app toast when permission is refused. The tray keeps every kind;
`notify_wish_found`, `notify_download_done`, `notify_import_ready`,
`notify_soulseek_download_start` and `notify_soulseek_upload_start` decide which
kinds are published, and `?since=` replays the 100-event ring so a client that
reconnects does not miss one. This is **not** remote push. The UI ships in six
languages — English, Español,
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

That is the classic console menu — scripts 1–21, Run All, the config editor and
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
- **The Windows-only tools run in Docker through their runtimes** — CUETools
  and AudioAuditor on the mono runtime the image installs (with `libgdiplus`
  and mono's System.Drawing, which CUETools' verification loads), Logchecker on
  its `php-cli`. They install at runtime from their Linux builds like every
  other tool, so AccurateRip generation, the Logchecker grade and the
  AudioAuditor audit work there; `GET /api/capabilities` says what a given
  server can actually do.

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
| `GET /api/library/layout`, `GET /api/library/layout/report` | read-only layout scan (misplaced audio, stray files, empty albums, `wrong_case`); the same scan as a script (20) plus the last one stored under `.mlo/data` (`scanned_at`, stale when it describes another music folder); `GET /api/home` and `GET /api/recommend` back the Home shelves and **Recommended (Local)** |
| `GET /api/storage` | one disk snapshot for the Home card: the volume, the library, the **app's own footprint** (`app_total` = state + bin + transfers + tools, with `dependencies` measured where it lives), the bin and the transfer folders — every figure the OS refused is `null`, never 0 |
| `GET /api/album` `GET /api/artist` `GET /api/artist/artwork`, `GET /api/stream` `GET /api/videos/stream` `GET /api/videos/meta` `GET /api/videos/thumb` | entity details, stored artist image/description + provenance and the artist's own grade; audio/video streaming (Range; `?transcode=1`), codec probe, scrub frames |
| `GET /api/tags` `POST /api/tags/bulk` `…/videos/tag`, `POST /api/run`, `POST /api/organize` | per-track tag read view; bulk tag surgery; video tag writes; run scripts 1–21; apply the naming script (dry-run supported) |
| `POST /api/export`, `GET /api/export/codecs` `…/drives` `…/defaults` | multi-format export plus its codec table, drives and saved defaults |
| `GET/POST/PATCH/DELETE /api/wishes…`, `POST /api/wishes/{id}/search` `…/search-all` `…/reconcile` `…/import` | the wishlist and its worker |
| `GET /api/sources/health` `…/{id}`, `GET /api/capabilities`, `GET /api/dependencies` | every external source with its `needs`/`configured` state (`?probe=1`); what this server can run; the tool table with installed/pinned/upstream versions |
| `GET/POST/DELETE /api/youtube/cookies` | the yt-dlp cookie jar: which mode is on and what the file holds, save a pasted/dropped `cookies.txt` (validated as a Netscape cookie file first), delete it |
| `POST /api/mb/match` `…/assign` `…/auto-import` `…/advisory/fetch`, `POST /api/genres/import`, `GET /api/genres/facets` | MusicBrainz matching, tag writes, queued downloads, advisory resolution; genre import and Genres-page facets |
| `POST /api/import/upload` `…/commit` `…/acoustid` `…/finish` `…/bulk`, `POST /api/lyrics/auto` `…/write` `…/embed` `…/wordsync`, `GET /api/lyrics/find` `…/providers` | the import pipeline, its fingerprint step, the chain and the bulk queue; the lyrics chain, previews and writes |
| `GET /api/cover/search` `…/sources`, `POST /api/cover` `…/fromurl` | cover meta-search, upload and save-as-cover |
| `POST /api/soulseek/download-bulk` `…/download-user` `…/search/cancel`, `GET /api/soulseek/ready`, `POST …/import-one` `…/import-all` `GET …/import-all/status` `POST …/import-all/cancel` | Soulseek downloads, the ready list and the sequential importer |
| `GET /api/soulseek/port-check` | the listen port's own check (the tab's "Test port"): a real TCP connection to the port here, what the router holds for it (`GetSpecificPortMappingEntry`, the gateway's own words), the LAN/WAN address shape (a CGNAT or double NAT setup named as such), a connection from here to the public address, and slskd's login — every row states what it proves and what it cannot, and a definite answer about the internet needs a probe from outside, which this app does not ship |
| `WS /ws/progress` `WS /ws/events` | live script progress, the live transfer/job frames (`{"type":"transfers"}`, `spec R120`) and the daemon's state frames; the notification channel (every published kind — `wish_found`, `download_started`, `download_done`, `upload_started`, `import_ready`, `script_done`, `grade_done`, `update_available`, …), `?since=` replays the 100-event ring |

## Tests & development

```bash
python tools/make_test_library.py   # synthetic library for end-to-end runs
python tools/test_remux.py          # video remux suite (VOB/MKV/AVI/WebM fixtures)
python tools/test_lyrics_xlit.py    # script 17: alignment, romanization, cache (offline)
python tools/test_script_menus.py   # every script menu agrees (numbers, labels, Run All order)
python tools/smoke_api.py           # route smoke test against a running backend
python tools/check_versions.py      # the release gate: every version string agrees
```

- The 84 `tools/test_*.py` suites are standalone scripts (`python tools/test_x.py`;
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
`bc1qf2snsus59ydvmk8rwp09e698gxjdlmyxyrnycu` — a photograph of the maintainer's
cat with one line under it (*Donate to feed my cat*), and **nothing is gated
behind it**.

Legacy v1 (the Tkinter app, CLI and PyInstaller/Inno packaging) is archived on
the `archive/legacy-v1.7` branch.
