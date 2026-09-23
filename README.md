# la musica

**v3.23.0** — a self-hosted app that *manages, optimizes, audits, grades and plays* your music library, from a browser, a desktop window or a phone.

**la musica** (formerly Music Library Optimizer) is a FastAPI backend plus a React UI over the `mlo` engine: music and music videos (karaoke-synced lyrics), manual and smart playlists, likes and favourites, artist artwork and biographies, a multi-source lyrics chain, offline caching, device export, and a managed Soulseek client whose auto-importer verifies what it downloaded.
All state — config, playlists, favourites, the beets library, Soulseek config, measured loudness, caches, runtime-installed tools — lives in one hidden `.mlo` folder in your music directory.

Release notes: `release-notes-<version>.md` in the repository root (releases before 3.14 are under `local/`). The grading and optimization contract — check ids, defaults, presets, audit workflow, runbook — is [`docs/OPTIMIZATION-GRADING-SPEC.md`](docs/OPTIMIZATION-GRADING-SPEC.md).

## Quick start

Docker is the only supported **server** installation: a container needs no Python or Node toolchain on the host, and every client connects to it over the network.

### Docker (recommended)

```bash
docker compose up -d --build     # build from source and start
docker compose logs -f           # follow the backend log
docker compose pull              # fetch the prebuilt GHCR image instead
docker compose down              # stop and remove
# open http://localhost:8000 — your library is mounted at /music
```

`docker-compose.yml` ships one line you must edit: the bind mount, committed as `./music:/music`. Replace it with an absolute host path (`/path/to/your/music:/music`, `D:/Music:/music`); compose creates an empty `./music` if it is missing, and that reads as an empty library.

- Image `ghcr.io/dillydalli3r/la-musica:latest`, container `la-musica`, port `8000`, pinned compose project name. `MLO_MUSIC_FOLDER=/music` is the only environment variable it needs; `MLO_SERVER_HOST=0.0.0.0` makes the published port reachable and switches the login gate on for non-local clients.
- `/music` holds the library **and** all app state (`data`, `downloads`, `trash`, runtime tools at `/music/.mlo/tools`), so one bind mount carries library, settings and tools; a `lamusica-dependencies` volume from an older compose file is unused.
- Runs unprivileged as `mlo`, uid/gid **1000**, `HOME=/home/mlo`: the bind-mounted folder must be writable by uid 1000 or `.mlo` cannot be created. Healthcheck: `http://127.0.0.1:8000/api/health` every 30 s.
- `watchtower` is on by default (`WATCHTOWER_POLL_INTERVAL`, 300 s) and updates the **image**, so a local `--build` is replaced by the published one; to keep building from source run only the app: `docker compose up -d lamusica`.
- From apt: `ffmpeg`, `flac`, `libjxl`, `jpegtran`, `fpcalc`, `rsgain`, plus the runtimes the Windows-only tools need — `mono-runtime` (with `libgdiplus` and mono's System.Drawing, which CUETools' verification loads) and `php-cli`. Installed at runtime from upstream Linux builds: `oxipng`, `slskd`, `AudioAuditor`, `Logchecker`, `CUETools`, so AccurateRip generation, the
Logchecker grade and the AudioAuditor audit work in Docker as on Windows; `GET /api/capabilities` says what a given server can do.

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

- `python -m server.main` reads `server_host` / `server_port` from the config and binds there, so the module, the Settings page and the login gate agree. A hand-typed `uvicorn --host` bypasses the config: bind through the config or set `auth_mode: required`.
- The music folder is picked in the UI (**Settings → General → Music folder → Change…**, or setup wizard step 1); `GET /api/fs/dirs` browses the server's folders and flags the ones holding audio. Choosing one *moves* the app state (`<music>/.mlo`) into it and never moves music files; `MLO_MUSIC_FOLDER` pins the choice instead.
- Install the toolchain from **Settings → Dependencies**: `ffmpeg`, `flac`, `libjxl`, `libjpeg-turbo` (`jpegtran`), `oxipng`, `rsgain`, `AudioAuditor`, `Logchecker`, `php`, `CUETools`, `chromaprint` (`fpcalc`, optional — AcoustID), `librosa`, `beets`, `slskd`, `yt-dlp`. They live in **`<music>/.mlo/tools`**, inside the library, so they travel with the music folder; each row
shows installed, pinned and upstream versions, and a copy an older release installed beside the app is still *read*.
- A row is `deps` (installer fetches it), `unsupported` (no build here, with the reason) or `system` (the button copies the package manager's command). Every `deps` row has **Install** / **Update**, and **Update all** presses every missing or outdated row.
- A press fetches the NEWEST publisher release — GitHub, PyPI for the pip packages (`librosa`, `beets`, `yt-dlp` on Linux), windows.php.net for `php`; the pinned version is what a FIRST install fetches when no upstream answer is reachable. An up-to-date row is a no-op, a newer-than-upstream copy is never downgraded, and a PATH-installed `ffmpeg` is updated into the app's own
toolchain folder rather than in place. `libjxl`'s static `tar.lz` and libjpeg-turbo's official `.deb` are unpacked by the installer (raw-LZMA reader, `ar` walk, no `lzip`/`dpkg`). `dependencies_auto_update` (off) installs missing or outdated tools in the background.

### Configuration & credentials

- **First run asks six things**: the music folder, your account (the login gate), the external tools (one download), the source keys and cookies, the Soulseek login and sharing. Every quality knob — grading switches, audit and verification options, the script chain, naming, interface — is NOT asked: the shipped defaults ARE the strict ones, and each setting is one click away in
Settings.
- Config: `<music>/.mlo/data/config.json`, edited through Settings (`GET/POST /api/config`; `GET /api/config/defaults` is what *Reset to defaults* writes back). Seeded from the environment by `MLO_MUSIC_FOLDER`, `MLO_SERVER_HOST` / `MLO_SERVER_PORT` (they feed `server_host` / `server_port`) and `MLO_VERSION`.
- Keys worth knowing: `music_folder`, `server_host`, `server_port`, `auth_mode`, `mb_genre_count`, `genre_sources`, `run_all_order`, `import_scripts`, `import_auto_scripts`, `naming_script`, `lyrics_format`, `cover_target_size`, `embed_covers`, `playback_eq_profile`, `dependencies_auto_update`, `ui_locale`; every key that changes a grade is listed in
[`docs/OPTIMIZATION-GRADING-SPEC.md`](docs/OPTIMIZATION-GRADING-SPEC.md#9-config-keys-that-change-a-grade).
- Credentials sit in that same file **in clear** — it is your server's. All optional: `spotify_client_id` / `spotify_client_secret` (advisory, genre by ISRC), `discogs_token`, `lastfm_api_key`, `rym_cookie`, `acoustid_api_key` (lookups) plus `acoustid_user_key` (submitting fingerprints), `ai_base_url` / `ai_api_key` / `ai_model` (script 17, genre ranking), `soulseek_username` /
`soulseek_password`.
- `youtube_cookies_mode`: `none` (default, anonymous), `file` (the jar saved on **Settings → Videos** — paste a `cookies.txt` or drop the file; the app writes `<music>/.mlo/data/cookies.txt`, the one path it owns) or `browser` (yt-dlp reads `youtube_cookies_browser`'s store). Both yt-dlp paths (importable module and pinned binary) honour it.
- `GET /api/sources/health` lists every external source (six lyrics, six advisory, eleven genre, four metadata, ten discover, one links — 38 rows) with what each needs; `?probe=1` tests them. The RateYourMusic cookie is needed only for the *scrape* fallback. Seven more rows of kind `credentials` are the saved logins (Discogs token, Last.fm key, Spotify pair, AcoustID key,
Soulseek account, AI provider, this server's password); they ask the provider's OWN endpoint (Discogs `/oauth/identity`, Last.fm `chart.gettoptags`, Spotify `POST /api/token`, one AcoustID lookup), so a refused key is reported in the provider's words. `?kind=credentials` returns them alone; Settings → Sources and the setup wizard show them as their own group with a Test button.

## What the app does

### Library, search and identity

- Artists → albums → tracks, with live grade/audit badges, a search box, a *fail only* filter, bulk tag tools, custom tag columns and sortable/resizable columns (year, grade, audit, genre, advisory, duration, bitrate, dynamic range…).
- Music videos are first-class tracks. A web/digital album can fetch its own: the album header's film button (or a track's "…" menu) searches YouTube through yt-dlp, drops the file into the album folder and tags it as that track's video.
- Album cover badges: measured album dynamic range (`ADR`, top-left); bottom-left as three rows, the medium, every release country and the format/bitrate (`CD` / `US, CA` / `FLAC 16/44.1`).
- Every track row offers the same "…" menu: tagging (tag editor, genre and advisory imports), its scripts (lyrics, re-audit, ReplayGain, re-encode, re-grade), credits, the stored readout and the track's own editor.
- The top search bar searches the library (`composer:`, `person:`, `genre:`, `tag:` prefixes) or MusicBrainz; `/mb/search` is a full in-app MusicBrainz browser (artists, release groups, releases, recordings) with *Auto-import* and *Add to library*.
- An entity's **alias in your locale** is shown in parentheses beside its name (`宇多田ヒカル (Hikaru Utada)`), picked by the "Preferred locale for aliases" setting: exact locale first (`en` before `en_PH`, `ja` before `ja-Latn`), then MusicBrainz's primary alias. Search-hint aliases are never shown, and an alias appears only when the name is not already in your own script.
- Entity pages: grading and auditing detail, MusicBrainz + RateYourMusic links, cover upload/search, Wikipedia descriptions, manual tag editing, a lyrics editor, and a **Credits** view from MusicBrainz `artist-rels` (falling back to the file's own PERFORMER/COMPOSER/… tags). *Add to library* answers at the press — the reply carries the framework album (the folder the naming
script names, the release's tracklist, the release-group cover), its wish and the queue kick — and the album shows one tile for the whole acquisition.
- The cover finder ranks candidates with the album's OWN identity (a karaoke/tribute row, another artist's release or a different album cannot win; an unmeasured image cannot beat the `cover_target_size` floor) and WRITES the winner by default; `cover_review` stages them instead. The reference cover is the **release group's**
(`coverartarchive.org/release-group/<rg>/front-500`), fetched from the URL you chose — a URL that cannot be fetched writes nothing.
- **Recommended (Local)** and **Home** are computed locally from the library's own tags (genre and family, mood, energy, era, artist) — no provider, no model, no network.
- **LIBRARY → BROWSE** turns the library into a query: 120 fields (tags, ratings, grades, audit verdicts, technical facts, audio analysis, library/album/artist facts) with the operations each field declares, a value control per type, match all/any, a live "matches N tracks · M albums" count, sorting, grouping and a multi-select facet rail. The same engine evaluates **smart
playlists** (`kind: smart` stores the filter, not the rows) and the playlist rule editor.
- **Ratings** are half stars in the UI and Picard's 0–10 in the store (click a star's left half for a half star, click the set value to clear, arrow keys nudge), shown on library rows, album rows, the album header average, page headers and the player bar; the query engine compares in star space, so `rating >= 4` means what the stars show. `write_rating_tags` decides whether the
file's own `RATING` tag is written.
- **Five views** — Grid, Compact, Albums, Artists, Tracks — each with its own sort and column set. The filter menu carries the presets (*All*, *Failing*, *CD rips*, *Digital*, *Instrumental*, *Music videos*, *No lyrics*) plus two facets with live counts: **star rating** (Any / Rated / Unrated) and **advisory** (Any / Explicit / Clean, where Explicit is `ITUNESADVISORY` 1).

### Discover, recommendations and watching

**DISCOVER** browses genres across every configured provider (MusicBrainz, Deezer, iTunes, TheAudioDB, Last.fm, ListenBrainz, Discogs, Wikidata, Wikipedia, Spotify, RateYourMusic, Bandcamp) with a library/online/both scope and an albums/artists/tracks switch. Every row names its source, whether you own it and what else holds it; each source's outcome is a chip — *0 answered*,  *skipped: needs a key*, *failed: the provider's own words*. Owned rows open the real page; everything else offers **Add to library**, which queues it and starts searching for its audio at once.

**RECOMMENDED (ONLINE)** takes a seed (the whole library, one of its genres, or the album / artist / track page it sits on); its **RECOMMENDED (LOCAL)** half is scored from the library's own tags. On an entity shelf MusicBrainz answers through the entity's OWN genres (`Genre: Shoegaze (MusicBrainz)`, `More release groups by …`), Spotify adds the artist's albums and top tracks
when its credentials are saved, and Apple's keyless search and Deezer's similar-artist feed stand beside them.

**WATCHED ARTISTS** keeps a MusicBrainz artist under watch: policy (`new_only` / `backfill`), the release types worth taking, an allow/never list of specific releases, a per-cycle cap and auto-add. Each check queues a few release groups into the download queue — never a discography — and the page reports real scheduling ("last check 3h ago (12 total) · next in 34m"). Watches
inherit the acquisition chain below.

### Player

A persistent player bar (queue, drag-reorder, shuffle, repeat-one, speed, sleep timer, ReplayGain, visualizer, app-wide volume) plus a fullscreen player with animated karaoke lyrics: two columns on a wide window (cover + controls, lyrics beside it) and, on a phone, a compact header (cover, title, transport, seek) with the lyrics pane taking the rest of the screen as the ONE
scrolling surface.

- Play state comes from the media element's own events, so a pause the OS makes (a backgrounded iPhone, an interruption, a headset button) is what the bar shows. The favourite sits at the player's bottom-left on a phone and in the transport row above `lg`.
- The lyrics pane, the metadata block, the transport and the top bar draw **no** background, border or blur of their own; the ink is **derived from the cover** (`npInk`) — a dark cover gets white text over nothing, a bright one flips to near-black, never a grey scrim. Contrast is checked in `tools/check_np_metadata_contrast.cjs` (metadata 4.5:1, title 3:1). The player's
floating menus keep a frosted panel.
- Both lyric surfaces (the pane and the right-docked sidebar viewer) carry the same size control (`−`, a percentage you can type into, `+`, 5 % a press, 85-160 %, remembered per surface) and the same **offset** control (`−`, the pending shift, `+`, Save): the step is a tenth of a second, the shift previews live, and Save writes it into the track's own lyrics wherever they live
(the `.lrc` beside the file and/or its `LYRICS` tag). A stamp before the start of the file clamps at `[00:00.00]`.
- The pane is a control you own: one microphone toggle in the player's own control row (beside the queue, the visualizer and the display options, and the same button on the player bar for the sidebar pane), shown only while the current track has lyrics. The bar's **ⓘ** and the fullscreen player's options menu open the track's **Details & credits** (stored readout, audit
verdicts, MusicBrainz credits).
- ReplayGain runs through the WebAudio gain stage in **track**, **album** or **off** mode (`replaygain_mode`) with a preamp (`replaygain_preamp_db`, ±24 dB). A file without ReplayGain tags is measured on the fly with ffmpeg's EBU R128 meter when `replaygain_analyze_missing` is on (default), cached in `.mlo/data/replaygain.json`, with clip protection; music videos carry the same
gain, and album mode on an album with no `REPLAYGAIN_ALBUM_GAIN` says each track fell back to its own gain.
- **Equalizer** (sidebar → MAINTAIN → **Equalizer**): an Equalizer APO / Peace profile applies to what the player plays (`playback_eq_profile`, "" = off — one key, so every client of the server hears the same curve). Pick a built-in preset, import APO/Peace text or drop a file, or search **AutoEq**'s 6000+ measured headphones by model and import the correction as a profile. The curve is drawn from the browser's own biquad response and every band has a draggable handle plus typeable Fc / Gain / Q fields, a per-band on/off, an add/remove row and a preamp; edits are audible live and stored by **Save** (or **Save as** for a preset-derived curve). The graph is ReplayGain gain → EQ preamp → the profile's own bands → the analyser, so the meters and the ambience read the equalized signal — and the SAME profiles are what an export bakes in (`export_eq_profile`).
- Pressing play on what is already playing starts it over; the transport's play after a pause resumes where it stopped. Music videos play at the correct aspect ratio, incompatible codecs are transcoded to fragmented MP4 (`GET /api/videos/stream?transcode=1`), and the keyboard shortcuts (`F`, `/`, `?`, Space, ← →, `[` `]`, `0`) live in `web/src/components/Shortcuts.tsx`.

### Import

Drag & drop uploads, a watched import folder, staged `.mlo/downloads` or the Soulseek paths — all through one pipeline (`server/imports.py`).

- **AcoustID fingerprint matching** tells you which release the *audio* is, not what the tags claim (needs `acoustid_api_key` and `fpcalc`): accepting a match writes the Picard-compatible pair `ACOUSTID_ID` + `ACOUSTID_FINGERPRINT` in one save and reads it back. The wizard's **Submit to AcoustID** action publishes the pair the files already carry (two-press confirm; needs
`acoustid_user_key` — a *user* key, where the application key can only look up).
- The wizard's eight steps are **Select & separate → Links → Match → Covers → Genres → Lyrics → Advisory → Finish**; *Finish* runs the scripts you ticked on this album. Any single script can still be re-run from the album page, and Run All over the library lives on the Optimization page.
- The **import script chain** — default `import_scripts`, i.e. `DEFAULT_CHAIN`, which *is* `run_all_order` (one list in `mlo/config.py`, so a script added to Run All can never be missing from an import) — runs after the wizard; `import_auto_scripts` turns it off, `import_scripts` replaces it outright.
- An import decides four families for itself and **replaces** what the download arrived carrying — the lyric the fetch found, the release's genres, the advisory pipeline's rating and the album's own cover art — in `server/imports.py::drop_arrived_values`, its first tag-writing pass. Everything else it touches only ever **fills**, so what you typed in the wizard survives; a
family kept in `import_review_families` is left as it arrived, and `import_keep_synced_lyrics` is the lyric family's exception.
- **Bulk import** queues several albums with `import_bulk_concurrency` (2 by default, 1–8).
- **An import finishes on its own, and only asks when the answer is yours.** `import_autonomy` ships as `automatic`: the pipeline decides every family it can and *reports* what no source could supply; a track with no lyrics from any provider and nothing saying it has vocals is settled as `INSTRUMENTAL=1` with the app's own provenance (`lyrics-none`). What is left is genuine — a
family you kept (`import_review_families`, `cover_review`, or `import_autonomy: "review"`), or one no source could state at all — and THAT is what the notification menu and the Soulseek page's *Needs you* row carry. A waiting album is **parked**: a library-wide `Run All` skips it and logs which albums it left alone.
- **An import never loses the album it is working on.** *Beets tagging* (script 14) moves the album and renames its files, so every later script acts on the folder the album is in *now*, and where the chain ended is what the queue row, the album page and the notification name. The album's own files travel with it (the fetched cover, its description, the expected-tracklist
manifest, the rip's CUE/LOG) and a file whose name the album folder already holds is never overwritten.
- **The cover a framework album carries is a stand-in**: the artist's real cover is fetched over it, and the stand-in is dropped only once that cover is written — if nothing clears the cover minimum the stand-in STAYS and the step's note says so. An import that cannot place every file is **reported, not aborted**, with the files left in the download folder named in the job's
line.
- **When the edition the app picked is not on the network, it does not give the album up.** *Add to library* on a release group tries that group's ranked editions IN ORDER — best pressing first — up to `soulseek_fallback_candidates` editions (1–10, 5 by default; `1` is the best edition only), each getting its own window (`soulseek_search_timeout_seconds`, 60 s, 5–300). Editions
that would be the SAME search are skipped, not re-asked (separate releases really do share a catalog number). If none is there the release is **not dropped**: it moves to the queue's **Background** list, one row per release, and keeps being searched on the worker's schedule.
- **A wish or a followed artist takes a copy only while one exists in a lossless format.** `soulseek_auto_lossy_policy` ships as **`never`** — an unattended acquisition that finds only MP3 leaves the release waiting, and the row says "a lossless copy is preferred, so nothing was downloaded" — and `best` takes the best-ranked lossy copy instead, named as lossy in the job's log,
its queue row and the notification. The Soulseek page **always asks**. A download queued from that page imports itself and runs the chain, through the same `import_autonomy` / `manual_import_enabled` gate every unattended import passes.
- The **Genres** step asks the whole configured chain with **one button**: every source the app knows, in the order `genre_sources` lists them — RateYourMusic first, then MusicBrainz, then the rest (ListenBrainz, iTunes, Last.fm, TheAudioDB, Wikidata, Bandcamp, Discogs, Deezer, Spotify) — merged per track. **MusicBrainz falls back inside its own answer**: a recording's genres
are kept and anything it does not state is filled from the release, then the release **group**, then the **artist**, and the answer says which level spoke (`genre_cascade`'s `levels` /`source`). The same ladder runs for the other per-track sources (Last.fm `track.getTopTags` → `artist.getTopTags`); Bandcamp, Discogs and Deezer only state an album's genres and Spotify an
artist's. The chain **stops as soon as a track's list is complete**, and the list is the PRIORITY list — the tray next to that button (and Settings → Import) ticks sources in and out in the chain's own order. An uncredentialed source is skipped before any request and reported by name in the answer's `notes`. The same chain runs wherever genres are imported and, with no click at
all, on every import.
- The **Advisory** step resolves `ITUNESADVISORY` from every applicable source — Deezer and Spotify by ISRC (every ISRC the file states *and* every one MusicBrainz holds), Apple's explicit-edition album route and Apple's exact-title song search — merged so explicit anywhere wins, and derives `ALBUMITUNESADVISORY` from the per-track values with script 8's own rule. A stated
value ends the question and a stated `0` is final. The configured AI provider answers what nothing else could, asked ONLY when every source came up empty (`advisory_ai_classify` on) and fed the track's own words (embedded lyrics first, else the `.lrc` sidecar): `1` is profanity that is excessive or a slur or a very strong word, or graphic sex/violence/drug use; a mild word in
passing is `0`. `mlo/advisory.py` decides (instrumental → the configured AI → `advisory_fallback`), an import never re-asks a track that already holds 0/1/2, and every action a person presses asks the providers again (the `force` request). The two tags answer to their own switches: `advisory_auto_fetch` for the per-track rating, *Auto Album Advisory* (`auto_advisory`) for the
album tag script 8 derives.

### Soulseek & the download queue

A managed slskd instance (autostart, shares = the library folder `<music>/Artists`, a share rescan scheduled whenever the library changes), with search & download UI, a live status dot, share browsing, bulk and whole-user downloads, transfer-level clearing and staging management (`GET /api/soulseek/staging`).

- **Transfer progress is pushed, not polled**: the rows `GET /api/soulseek/downloads` returns, and every live job's progress block, also ride the progress WebSocket as `{"type":"transfers"}` frames (`server/main.py`) — 0.4 s cadence while bytes move or a job runs, 5 s otherwise, nothing when nothing changed and no slskd request while no client is connected. The page's own poll
stays as a 30 s fallback for a dead socket; these frames never reach the notification tray.
- **An import that needs a hand is an outcome, and it is announced wherever you are**: an album the pipeline could not finish raises `import_needs_data`, which the desktop/mobile shell raises as a system notification, the bell's tray keeps, the in-app event channel delivers, and the Soulseek page's finished row shows as "*Needs data: …*" naming the missing families, beside  *Enter manually* (the import wizard at that album's first missing step, `/import?album=…&step=…`) and *Mark complete* (stop asking; a later import asks again only if something is still missing). **Nothing about the album waits**: it is in the library, graded like any other. Only a genuine wait — a *review* import stopped before its chain ran, or a disc structure whose main
feature is unpicked — sits in the *Needs you* section, and only those are skipped by a library-wide Run All.
- **Is the port open?** The tab's *Test port* action (`GET /api/soulseek/port-check`) answers with five rows that each say what they prove: a real TCP connection to the listen port here (plus a bind test to tell "nothing is listening" from "something else holds it"), what the router itself lists for that port with its own words and the lease, the LAN-vs-WAN address shape (a
CGNAT named as one instead of being blamed on a firewall), a self-connect through the public address (refused ⇒ "unknown", because a router without NAT hairpinning refuses it while the port may still be open) and slskd's own signed-in state. A definite answer needs a probe from outside this network, which the app does not ship.
- The listen port is opened on the router by the app (`soulseek_upnp`, ON): slskd has no UPnP/NAT-PMP option, so `mlo/portmap.py` does it, UPnP IGD first and NAT-PMP behind it, reporting a mapping as made only when the gateway confirms it.
- **Which edition is fetched** is the release-choice policy's (`mlo/release_choice.py`), the same policy the release-group page shows: official editions first, then the configured medium order (CD, then the other physical media, digital last), the original ahead of a reissue, and a **box set below the album itself**. A COMPRESSED derivative sorts below the disc's own streams
too (`prefer_disc_streams`, on by default), and a folder holding a `VIDEO_TS`/`BDMV` structure beside a 700 MB re-encode is remuxed as the disc's own single title.
- **The original pressing wins the date, exactly**: an edition is scored by how close it sits to the release group's first release date, and among editions of the same year the one that states its date **in full** (`YYYY-MM-DD`) beats one that states only its month or year, because the album folder is named after that date.
- **Where a release is fetched from is decided by the release itself**, from the recordings' own `video` flag and the medium MusicBrainz publishes: a music-video release on **Digital Media** cannot be on Soulseek as a folder (no disc, no log, no CRC), so it is fetched from YouTube with yt-dlp *inside the same auto-import job*, with no search at all. One query per track through
the filter the film button uses (achieved length, lyric/cover/tribute rows refused), into `<downloads>/YouTube/<Artist - Album>`, renamed to `<disc>-<NN> <title>` before the import, then the SAME import the Soulseek path runs: MB stamping, `MEDIA=Digital Media`, `SOURCE=YouTube`, the naming script, and the configured post-import chain in the background. Finding NOTHING ends on
the same wish offer an empty search does. A music video on a DISC (DVD, Blu-ray, VHS, Video CD — what `mlo.release_choice.is_video_format` classifies) keeps the Soulseek path byte for byte, an audio release is never routed anywhere, and an unstated or unknown medium is never guessed at.
- **The auto-importer searches each release by what can only point at THAT release**: a physical pressing (CD included) by its catalog number and barcode (`soulseek_auto_physical_queries`) and, when it states neither, by its label and country — never a broad artist/title/album query, which drowns the result list in other pressings. Digital Media may be searched broadly
(`soulseek_auto_digital_queries`, `artist album year`); `soulseek_auto_cd_queries` still overrides the physical default for a CD a user sets it for. Search terms are stripped of punctuation and typographic marks no share folder carries (full-width `！`, quotes, brackets) while non-Latin script is kept, and a release whose titles are in another locale opens extra searches using
its MusicBrainz aliases in the configured `locale` (`ぴーなた` → `pinata`). It gates a CD candidate on its rip log *before* requesting any album byte (`soulseek_auto_log_min_score`, default 100), ranks candidates towards the copy that arrives fastest (lossless first, then match score, then the peer's advertised speed and queue), and downloads up to three candidates of one release
at once (`soulseek_candidate_slots`, 3) from three DIFFERENT peers (two folders of one peer are two copies on one machine, so the second keeps its place for a later batch): the first that passes the same verification becomes the import, the others are cancelled and swept. It verifies completeness (`soulseek_auto_complete_ratio`) and losslessness.
- **Two limits, and what happens over them.** `soulseek_search_concurrency` (default 3) is how many *releases* the pipeline works on at once; `soulseek_candidate_slots` (default 3) is how many *candidates of one release* download at once, and **the app enforces both itself** — the fourth release is never refused, it takes its place in the queue's **Waiting** group (with its
position, cancellable there without ever starting a byte) and starts by itself when a running release finishes. Releases whose ranked-edition walk is spent are their own **Background** group, one row per release. slskd's own `soulseek_download_slots` (default 9 — the product of the two) is the OUTER ceiling, and a config with fewer slots than its two limits need gets each
release's candidate batch narrowed to fit (`slots ÷ releases`). The Queue tab's header reads all three back.
- A release is imported **once**, into one album folder: the destination is checked against the release's own MusicBrainz ids, so a download of an album the library already holds is refused with a sentence instead of appearing beside it as `… (2)`, and two jobs heading for one folder serialize on it. That claim belongs to the download **job** and is carried onto the background
chain the import starts (`server.script_runners.claim_paths`, `server.soulseek_auto._start_import_chain`): the album stays locked from the first tag write through the last script, and so does the folder the download came from.
- The bar takes **any MusicBrainz link**: a release, a release-group (its ranked editions are walked), an artist (its discography is queued in the background, the albums appearing as they are created) or a recording — paste the page URL or the bare MBID, resolved server-side, and it goes through the same `POST /api/library/add` the MusicBrainz pages' own *Add to library* uses.
Adding a release **starts its search immediately** and puts it on the **download queue**; Notifications cover the add, the download starting and finishing, and the import starting and finishing, switchable in Settings → Notifications (on by default). The row stays **In progress** — never Completed — while the import chain it started is still running. Re-searching on
`wishes_interval_hours` (default 6) with retry backoff keeps looking **until the release is found or the user cancels it**. When the import lands the downloaded copy is deleted (`soulseek_clear_downloads`, ON — the import MOVES the album into the library, so the download dir is only staging; a failed import keeps its files), and a terminal failure removes the framework album
the add created. *Import all completed* imports every finished download **sequentially**, with cancel finishing the album in flight (`GET /api/soulseek/import-all/status`).
- **The search asks more than one pressing.** Every add records the release group's ranked editions on its wish — best first, deduped by folded catalog number, so two releases printed with `GED 24425` and `GED24425` are ONE search and not two spent windows — and walks them one at a time inside the one wish. A walk that is spent is not a give-up: the release moves to the queue's **Background** section and is re-walked from the best edition on the worker's own ticks. A release is still **ONE row** whatever the walk does, and that row says which edition it is asking with its own badge — `release 2 of 3`, with what already came back empty in its tooltip.
- **What counts as a good rip log is 100, everywhere it is asked**: the acquisition gate (`soulseek_auto_log_min_score` — a CD candidate's `.log` must score 100 in Logchecker and its checksum must verify before its audio is queued), the grading check (`grade_log_score_threshold`) and the audit verdict (`audit_log_score_threshold`) all ship that way, and a CD candidate carrying
no log at all is offered through an explicit confirm.
- **Two independent size caps**, both 5 GB by default and both editable in Settings → Storage (`soulseek_cache_cap_gb`, `trash_cap_gb` — 0 turns one off; one store filling up never eats the other's room). The Soulseek one covers the download dir plus the `incomplete` sibling slskd stages partials in; the trash one covers `<music folder>/.mlo/trash` across every per-user bin.
Over its cap a store is emptied **oldest entry first** until it fits, and what is **in use is never touched**: a path an import or a script run holds (`server/job_locks`, and the report names that job), or anything a transfer still running is writing into (its peer, its folder and, for a loose partial, its file name — read from slskd's transfer list). An entry that will not
delete is kept and reported; the pass runs in the background and announces itself with one notification naming what was freed, and everything it KEPT is still restorable to where it came from.
- **Clear all** empties the *queued/waiting* work in one press — every release that has not started goes, before it downloads a byte — and does NOT touch a release already RUNNING (that is a cancel, on its own row), a finished row, or anything in your library. **Select** turns the rows into checkboxes: *Cancel selected* cancels exactly the ticked rows in one call, and *Import /
commit selected* does what each ticked row is for (an album sitting finished in the download folder is imported, a settled row is taken off the list). Waiting releases are their own group, in the order they will start, each with its position.

### Optimization — the 21 scripts

Optimization → *Run All* executes `run_all_order`, shipped as **11 → 3 → 14 → 15 → 2 → 1 → 13 → 18 → 17 → 8 → 5 → 19 → 6 → 7 → 9 → 12 → 16 → 10 → 20 → 21 → 4** — everything that moves a file first, everything that reads it last. Every script also runs on its own, on a selection, or with its force flag from the *Re-run & overwrite* menu. A library-wide run holds every folder it
walks (the library root and any album filed elsewhere in the music folder, since the sweeps start at `music_folder`), while an album-scoped run holds only its own album; a script that **moves** an album takes that claim with it (`server.job_locks.move`).

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
| 19 | Optimize artist images | Crops `Artists/<Artist>/artist.*` to `artist_image_aspect`, downscales to `artist_image_target_size` (never upscales), re-encodes as `artist.jpg`/`artist.png` |
| 20 | Optimize library layout | Walks the music folder's shape and — with `layout_apply` (ON) — SETTLES what it can prove: a name whose case differs from `naming_script` is renamed to the script's spelling, audio outside any album folder is moved into the one its own tags name, and what is excess goes to the Trash. Nothing is deleted, a destination that already holds a file is refused rather than overwritten, and only paths inside the music folder are touched. Writes `.mlo/data/layout_report.json` (`scanned_at` + per-row `fixes`) |
| 21 | Fix AcoustID pairs | Completes a half-written AcoustID pair: an `ACOUSTID_ID` with no `ACOUSTID_FINGERPRINT` gets the local `fpcalc` fingerprint, a fingerprint with no id gets the lookup. Both halves present (or none) is left alone |

**What script 17 transliterates and translates is decided from evidence**: the rule (`mlo/lyrics_xlit.xlit_needs`, asked by the script *and* by the grader) reads, in order, the track's own `LANGUAGE` tag — which an import fills from **MusicBrainz's release text representation** (`text-representation.language`, e.g. `jpn`, written only into an empty tag) — then the lyrics' own
script (kana is Japanese, hangul is Korean; Cyrillic or Han states nothing) — then the function words of the languages the app knows. When nothing can say what the lyrics are in, script 17 asks the configured AI **one question about that track** and stores the answer in `LANGUAGE`. Codes that state nothing (`mul`, `und`, `zxx`) are asked past rather than obeyed, and the run's
summary reports `language stored N` beside its transforms.

Force flags, one per script: `force_lyrics`, `force_cue`, `force_tracklist`, `force_reencode_flac`, `force_reencode_images`, `force_audit`, `force_accurip`, `force_dr_replaygain`, `force_audiometa`, `force_mood`, `force_auto_tag`, `force_xlit`, `force_publish`. A run's force selection is *authoritative and complete*: the flags it does not name are turned off, so unchecking a
script in the one-shot **Force** menu really turns it off rather than falling back to a saved switch — and a caller with no selection of its own omits it entirely (every import path does). Script 20 is the one key that turns work OFF (`layout_apply`): untick it and the layout pass reports without renaming or moving anything. Scripts whose feature has its own off switch are
skipped rather than run as no-ops: `dr_replaygain_enabled` (7), `audiometa_enabled` (12), `mood_enabled` (16), `lyrics_xlit_enabled` / `lyrics_translate_enabled` (17), `lrclib_auto_publish` (18), `acoustid_enabled` (21).

Script 6 keeps an evidence record per file — size, mtime and, where the container states one, the audio identity a tag write cannot move (FLAC's STREAMINFO MD5) — so a second run over an unchanged album re-audits **nothing** instead of re-decoding every file; a re-encoded track is audited again, a tag write is not. The decoder is deliberately **not** shared between scripts:
each decodes for its own question (rsgain's EBU R128, the DR meter's per-channel PCM, librosa's mono 22.05 kHz).

By default the optimizer does **not** embed cover art — it removes it, and covers live on disk as `cover.*` plus per-track sidecars; Settings → *Embedded covers* (`embed_covers`, `embed_cover_jpeg_quality`, `embed_cover_resolution`) makes script 10 embed the album cover into every track (FLAC picture, MP3 APIC, MP4 `covr`, OGG/Opus `METADATA_BLOCK_PICTURE`). Script 14 and  *Organize* apply `naming_script`, shipped as `Artist [mbid]/[Type] date - date - Album {country - media - catalog} [label] [release] [releasegroup]/1-01 Title [recording] [releasegroup].flac`; `short_folder_names` trims the UUIDs to 8 characters for paths that need the room.

### Grading

**68 checks** across tracks, albums, artist folders and folders, all toggleable on the **Grading** page with a live filter, enable/disable-all and the **Strict / Balanced / Relaxed** presets. Every check is on in the factory defaults — `grade_check_audit` (the AUDIT-tag requirement) included — and **Balanced** is the pre-3.7.0 answer in one click. A verdict is binary: `PASS`
only when every enabled check passes, otherwise `FAIL` with the failed checks itemized, and every problem the page lists is charged. The summary counts checks (`summary_pass` / `summary_total`) and reports `albums_passed` / `albums_failed`, plus `albums_audit_failed` for albums that pass every check while their audit is FAKE/Mix (badged red on the Audit column). A check that
raises counts as *could not be evaluated* and fails. Two checks follow the codec target: `grade_check_lossless_source` stands down (and is not counted) when `library_codec` is an uncompressed container (`wav`/`aiff`) or `keep`, and `grade_check_cd_format` exempts a file that already is the configured lossy target. Artist folders are graded on image and description alone, by
`grade_artist()`.

- A tag **value** has one canonical form (`mlo/tagtext.py`), applied on every write (beets import, wizard, auto-import chain, every script, a manual edit) and re-applied library-wide by **Format all** (script 10): closed-vocabulary tags (`MEDIA`, `SOURCE`, `RELEASETYPE`, `RELEASESTATUS`, `AUDIT`, `RELEASECOUNTRY`, `SCRIPT`, `MOOD`) are spelled the way the app stores them and
spacing is collapsed, while an unknown value is left alone rather than coerced. `RELEASECOUNTRY` holds EVERY country the release's own events state (`US; CA; XE`), earliest first, as repeated container fields. `grade_check_tag_case` and `grade_check_tag_spaces` fail what those writers would have fixed; free text (`TITLE`, `ALBUM`, `ARTIST`, `LABEL`, the lyrics) is never
touched, which is what keeps `AC/DC` and `k.d. lang` intact. Genres are stored broad-first: `Rock / Shoegaze / Dream Pop`.
- A tag with more than one answer is a LIST, stored as repeated container fields (one comment / frame / atom per value, `; `-joined on read): the credit roles (performer, producer, engineer, mixer, arranger, conductor, the work's songwriters), the track's ISRCs, the release's countries and the genres. Only `RELEASECOUNTRY` and `LABEL` reduce to their first value, to keep one
album's path deterministic; `%genre%` is that whole list in the beets import and in the organizer, and a music video's container holds one string per key, so there the list is stored `; `-joined.
- Every check id, what it asserts, its default, the presets, the audit workflow, tag families, quality bars, score semantics and a runbook: [`docs/OPTIMIZATION-GRADING-SPEC.md`](docs/OPTIMIZATION-GRADING-SPEC.md).

### Export, playlists, offline

**Export** writes a playlist, albums, artists, tracks or the whole library as MP3 (VBR/CBR or custom), AAC, Opus, Vorbis, WAV, AIFF, ALAC, WavPack, WMA or a bit-exact `copy`, in the shipped `albumartist_album_disc` layout — ALBUMARTIST / Album / `1-01 Title`, the disc number included, so a two-disc album keeps its discs apart and a compilation stays one folder — or `album`,
`flat`, `mirror`, or `custom`, a structure you type in the same Picard-style tag grammar the library's own naming script uses (`export_codec`, `export_structure`, `export_structure_script`, `export_subfolder`; the Export page previews a custom one and refuses a field the app does not know).

- **Where it goes**: `export_target` is `zip` (the client downloads one archive — the only mode a browser can honour, and the default) or `server`, a folder the machine running the app can see, picked with the drive list and the free-space readout (`export_dest`).
- **What gets copied** is `export_copy_files`, ticked one by one and named per file by the run's `excluded` report: the tracks (always the default), the covers/artwork, the `.lrc` lyrics, the `.cue` sheets, the rip's `.log`/`.accurip`, the album's own `description.txt`, its checksum lists (`.md5`/`.sfv`/`.ffp`/`.torrent`), its notes and scans (`.txt`/`.nfo`/`.url`/`.pdf`), the
playlists the album carries and anything else it holds that the app cannot classify (subfolders are reported, never walked). A run nobody asked anything of writes the tracks alone — the cover travels EMBEDDED — and an empty selection is refused rather than writing an empty folder; `export_sidecars` (the switch this replaced) still resolves to the classic set it always copied,
and `export_playlists` writes a fresh `.m3u8` per album plus `all.m3u8`. ID3v2.3 plus optional ID3v1, `export_manifest` writes a `checksums.sha256` beside them, and every written file is re-opened and verified. Sync mode (`export_prune`) removes audio the run did not write; exporting *into* the music folder is refused.
- **ReplayGain** is a mode, not a checkbox: `export_replaygain_mode` is `off`, `tags` (measure and write `REPLAYGAIN_*`) or `apply`, which rewrites the audio so the files themselves are level — the ALBUM gain for a whole-album selection, the track gain otherwise, with the `REPLAYGAIN_*` tags stripped because a player would otherwise apply the gain twice.
- **An equalizer** rides along: `export_eq_profile` selects one of the built-in curves or a profile imported from **Equalizer APO / Peace EQ** (`Preamp:`, `Filter N: … PK|LS|HS|LP|HP Fc … Gain … Q …`, `GraphicEQ:` band lists — pasted or uploaded on the Export page or edited band by band on the **Equalizer** page, stored under `<music>/.mlo/data/eq/`). The order is ReplayGain gain → EQ preamp → EQ filters → encoder, the curve
touches the EXPORTED copies only, and anything the profile cannot be rendered from (`Include:`, unknown constructs) is reported instead of silently dropped. Both processing modes need a real codec.
- **Playlists** are manual (drag-reorder, favourites, `.m3u8` import/export) or smart, driven by saved grade/audit/tag filters.
- **Offline**: "Download" caches a track's audio in the service worker's media cache and warms the album/artist payloads around it, so the UI opens and a downloaded album plays with the server down. In the Tauri shells, where no service worker runs, a smaller JSON cache (GETs only, 512 KiB per entry, 3 MiB total) and `blob:` playback cover the same case, and an **Offline** pill
says when a stored answer is being shown. Writes, Soulseek, imports and exports still need the server.
- **What a downloaded copy HOLDS, and which copy PLAYS**: `download_codec` ships as **`copy`** (the cached bytes are the library file's own) and a codec target re-encodes the track for that device's cache only (`download_bitrate` is that target's rate: kbps for MP3/AAC/Opus, Vorbis' 0-10 scale for Ogg, 0 for the codec's own default; the library file is never touched, and the
bulk transfer route stands aside with a 409 while a rendition is configured). `playback_source` ships as **`stream`** — the player asks the server for the library file even when a copy is downloaded, or plays the copy instead (`downloaded`), one resolver deciding it for every surface; a stream that a copy exists for carries `nocache=1` so a cache-first service worker cannot
answer it with the very bytes the setting asked to avoid. The cache key holds no session token, so a download survives a re-login.

### Notifications and languages

The backend publishes every settled outcome on `/ws/events` — `wish_found`, `wish_failed`, `wish_not_found`, `download_started`, `download_done` (a Soulseek job beginning and landing), `upload_started` (a peer starting to download from your share), `download_failed`, `import_ready`, `import_needs_data`, `script_done`, `script_failed`, `grade_done`, `update_available`. Each
client keeps that socket open and raises an OS notification (Tauri's plugin on desktop and mobile, the Web Notification API in the browser) with an in-app toast when permission is refused. The tray keeps every kind; `notify_wish_found`, `notify_download_done`, `notify_import_ready`, `notify_soulseek_download_start` and `notify_soulseek_upload_start` decide which kinds are
published, and `?since=` replays what the client missed — the in-memory ring plus the durable log beside the app state, so a client that was closed for a day (a desktop or mobile shell, which no push service can reach) still finds its notifications on the next open.

**And it is remote push too** — a client that is not open gets woken. The server signs with VAPID and encrypts each message (RFC 8291 `aes128gcm`), keeps one row per subscribed device, and prunes a device the push service reports gone (404/410; anything else leaves it alone). `import_done` is among the kinds push carries. The signing key lives in `webpush.json` beside the state
and deliberately **not** in the config, because `GET /api/config` hands the whole config to every signed-in client. Settings → Notifications shows a switch per device with a **Send a test notification** button and says what the platform can do instead of offering a switch it cannot honour: the desktop shell notifies only while it runs, iPhone and iPad need la musica on the Home
Screen (iOS 16.4+), and a plain-http page has no push at all. The UI ships in six languages — English, Español, Français, Deutsch, 日本語, Português (Brasil) — picked as this browser's own choice (`localStorage: mlo.locale`), then the server's `ui_locale`, then `navigator.language`, then English; the deeper tool pages are still English.

## Clients

The same React build runs in five targets, and **every one is a client of a server you run**: the Docker container on `127.0.0.1:8000`, a machine on the LAN, or a Tailscale name. No shell carries Python, a sidecar or a port of its own; each talks HTTP to the address it was set up with (`localStorage: mlo.server`).

| Target | Built by | Artifact | What it is |
| --- | --- | --- | --- |
| Browser | the server (`web/dist`) | — | the app itself, over HTTP/S |
| Windows / macOS / Linux | `npx tauri build` | `.msi` + NSIS `.exe`, `.app` + `.dmg`, `.deb` + `.AppImage` | client of a server you run |
| Android | `npx tauri android build --apk --debug` | debug-signed APK | client of a server you run |
| iOS | `npx tauri ios build --target aarch64 --no-sign` | unsigned IPA | client of a server you run |

Both halves are built locally — `cd web && npm install && npm run build` (the `web/dist` the shell wraps), then `cd ../desktop && npm install && npx tauri build`. `.github/workflows/desktop.yml` builds the three desktop bundles (`npx tauri build --bundles …`), `mobile.yml` the Android APK and the iOS IPA, and `release.yml` — on a `v*` tag — attaches all of them plus the Windows
zip and the GHCR image.

- **Desktop** keeps only the shell furniture: a tray icon (Open la musica / Auto-start on login / Exit), a native folder picker and the window. Quitting it stops nothing but itself — the server's lifecycle belongs to whoever runs the container — and mobile compiles the same crate without the tray and the folder picker.
- **Capabilities are the server's, not the app's.** `GET /api/capabilities` (`platform`, `backend`, `python` plus the per-feature rows) is derived from what the server can actually do — can it spawn a subprocess, are ffmpeg/flac/fpcalc/rsgain present — so a full container and a NAS container report different lists, and the Dependencies page and both setup wizards read that
report instead of offering an Install button that cannot succeed.
- A shell that has never been set up opens **its own wizard** (`web/src/pages/ClientSetup.tsx`: Server → Account → Notifications → Done) before anything else. *Test* probes `${address}/api/health` with a 3 s deadline and **Next** stays disabled until a real la musica `version` answered; the address is saved per device as `localStorage: mlo.server`, and Settings → Security
offers *Run setup again* for every client (the browser included).
- Both mobile artifacts are **sideload builds, signed by nobody here**: the APK is a debug build (the SDK debug keystore), the IPA is built `--no-sign`, and a plain-http server needs the platform's exemption (`desktop/src-tauri/Info.plist` sets `NSAllowsArbitraryLoadsInWebContent`; `mobile.yml` flips `usesCleartextTraffic` for Android). The iOS plist also declares
`UIBackgroundModes: audio` — the reason playback survives the app leaving the foreground — and `mobile.yml` reads both that key and the ATS exemption back out of the built `.app`, so a merge that drops either one fails the build. Android's WebView has no equivalent switch: playback continues while the process lives, and the OS may reclaim a backgrounded app.
`tools/make_sidestore_source.py` writes the `source.json` the release publishes, so a sideloading tool gets the name, version, bundle id, icon and download URL from the IPA itself.

## Terminal entry point

```bash
python -m mlo
```

That is the classic console menu — scripts 1–21, Run All, the config editor and the dependency table. It is **not** stdlib-only: `mlo` imports `mutagen` for every tag operation, so run it from the same environment that has `server/requirements.txt` installed (script 14 additionally needs `server/beetscfg` plus a vendored beets, and scripts 9/10 their optional modules). A
missing module makes its script unavailable and fails loudly instead of reporting a clean "0 processed" run.

## Security & accounts

Everything the API can do — read the library, rewrite tags, move and delete files, start downloads — is one password away from anyone who can reach the port. That is what the gate covers; it is not a UI lock. The top bar's rightmost control is the account itself: it names the user this client is signed in as, switches between the server's users (the login screen's own sign-in,
then a reload), and signs out.

| Key | Default | What it does |
| --- | --- | --- |
| `auth_mode` | `auto` | `auto` = gate ON when `server_host` is not loopback; `required` = gate ON always; `off` = gate off for loopback only |
| `server_host` | `127.0.0.1` | where the server binds — and the address the gate reads |
| `server_port` | `8000` | the port |
| `auth_username` | `""` | the name shown on the login screen; `POST /api/auth/login` accepts it as an optional `username` |
| `auth_password_hash` | `""` | PBKDF2-HMAC-SHA256, written as `pbkdf2$<rounds>$<salt-hex>$<hash-hex>` |
| `auth_session_days` | `30` | how long a session stays valid |
| `server_public_url` | `""` | the address clients should dial when it is not the page's own origin |

`auto` follows the bind: `127.0.0.1`, `::1` and `localhost` keep a single-user install password-free; anything else asks for a login — **from clients**. Who counts as a client is decided per request from the peer address, because the machine running the server is not one: loopback, one of this machine's own addresses and the gateway of the server's own container are let in,
while a phone or a laptop on the network signs in. `X-Forwarded-For` is never trusted, and `off` on a non-loopback bind is treated as `required` (with a startup warning).

- **A login**: PBKDF2-HMAC-SHA256, 600 000 rounds, random salt, constant-time comparison, minimum 8 characters. `auth.db` also holds a `users` table; a session carries the username it was opened for, and every playlist, like, favourite and trash folder (`<music>/.mlo/trash/<user>/`) is scoped by it — downloads stay one shared queue, because slskd is. Settings → Security adds
and removes users (the last one is refused), and five consecutive failures make an address wait 30 s, doubling to 15 minutes.
- **Sessions** are 32-byte tokens; only their SHA-256 is stored, in `<music>/.mlo/data/auth.db`. The token travels as `Authorization: Bearer`, as the HttpOnly `mlo_session` cookie, or as `?token=` for the WebSockets (which close with code **4401** when it is missing, expired or revoked). **Without a session** only `/api/health`, `/api/auth/status`, `/api/auth/login`,
`/api/auth/setup` and the static shell answer; every other route answers `428` (`{"needs_setup": true}`) until `POST /api/auth/setup` sets the password.
- **First run over the network**: start bound to the address you will use (`server_host: 0.0.0.0`, or `MLO_SERVER_HOST=0.0.0.0` in the container), open the app from a client and claim the server with a password; each client signs in once and keeps its own session.

### The honest limits

- **One password, one user.** No roles, no permissions, no signup — every session is the owner, and two people sharing a server share the password.
- **The app does not terminate TLS.** No certificate handling anywhere; over plain HTTP the token and the password travel in the clear. Put it behind a reverse proxy (nginx, Caddy, Traefik) or a mesh VPN when it leaves the LAN, and use `server_public_url` to name the https address clients should dial.
- **The gate follows the configured `server_host`, not the socket.** The Docker image seeds that key from `MLO_SERVER_HOST`/`MLO_SERVER_PORT`, so config, bind address and gate cannot disagree there. Starting the app by hand with a different `uvicorn --host` does disagree — set `server_host` (or `auth_mode: required`) to match.
- **A closed client is reached only where the platform allows it.** A browser with the push switch on (secure context, permission) is woken by the server's own Web Push; the desktop and mobile shells have no service worker, so they notify while they run and catch up from the server's notification log on reopen (its newest 400 frames). And **anything already on the host** — another local user, a container neighbour — reads `.mlo/data/auth.db` and the config: the gate defends the network boundary, not a hostile local account.
- **The Windows-only tools run in Docker through their runtimes** — CUETools and AudioAuditor on the mono runtime the image installs (with `libgdiplus` and mono's System.Drawing, which CUETools' verification loads), Logchecker on its `php-cli`. They install at runtime from their Linux builds like every other tool, so AccurateRip generation, the Logchecker grade and the
AudioAuditor audit work there; `GET /api/capabilities` says what a given server can actually do.

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
| `POST /api/export`, `GET /api/export/codecs` `…/drives` `…/defaults` `…/files` | multi-format export plus its codec table, drives, saved defaults and the file families the run's `copy_files` selection names |
| `GET/POST/PATCH/DELETE /api/wishes…`, `POST /api/wishes/{id}/search` `…/search-all` `…/reconcile` `…/import` | the wishlist and its worker |
| `GET /api/sources/health` `…/{id}`, `GET /api/capabilities`, `GET /api/dependencies` | every external source with its `needs`/`configured` state (`?probe=1`); what this server can run; the tool table with installed/pinned/upstream versions |
| `GET/POST/DELETE /api/youtube/cookies` | the yt-dlp cookie jar: which mode is on and what the file holds, save a pasted/dropped `cookies.txt` (validated as a Netscape cookie file first), delete it |
| `POST /api/mb/match` `…/assign` `…/auto-import` `…/advisory/fetch`, `POST /api/genres/import`, `GET /api/genres/facets` | MusicBrainz matching, tag writes, queued downloads, advisory resolution; genre import and Genres-page facets |
| `POST /api/import/upload` `…/commit` `…/acoustid` `…/finish` `…/bulk`, `POST /api/lyrics/auto` `…/write` `…/embed` `…/wordsync`, `GET /api/lyrics/find` `…/providers` | the import pipeline, its fingerprint step, the chain and the bulk queue; the lyrics chain, previews and writes |
| `GET /api/cover/search` `…/sources`, `POST /api/cover` `…/fromurl` | cover meta-search, upload and save-as-cover |
| `POST /api/soulseek/download-bulk` `…/download-user` `…/search/cancel`, `GET /api/soulseek/ready`, `POST …/import-one` `…/import-all` `GET …/import-all/status` `POST …/import-all/cancel` | Soulseek downloads, the ready list and the sequential importer |
| `GET /api/soulseek/port-check` | the listen port's own check (the tab's "Test port"): a real TCP connection to the port here, what the router holds for it (`GetSpecificPortMappingEntry`, the gateway's own words), the LAN/WAN address shape (a CGNAT or double NAT setup named as such), a connection from here to the public address, and slskd's login — every row states what it proves and what it cannot |
| `WS /ws/progress` `WS /ws/events` | live script progress, the live transfer/job frames (`{"type":"transfers"}`) and the daemon's state frames; the notification channel (every published kind — `wish_found`, `download_started`, `download_done`, `upload_started`, `import_ready`, `script_done`, `grade_done`, `update_available`, …), `?since=` replays the 100-event ring |

## Tests & development

```bash
python tools/make_test_library.py   # synthetic library for end-to-end runs
python tools/test_remux.py          # video remux suite (VOB/MKV/AVI/WebM fixtures)
python tools/test_lyrics_xlit.py    # script 17: alignment, romanization, cache (offline)
python tools/test_script_menus.py   # every script menu agrees (numbers, labels, Run All order)
python tools/smoke_api.py           # route smoke test against a running backend
python tools/check_versions.py      # the release gate: every version string agrees
```

- The 113 `tools/test_*.py` suites are standalone scripts (`python tools/test_x.py`; `sys.exit(2)` means "skipped", e.g. a missing toolchain) and mostly run offline with providers stubbed. The frontend gate is `cd web && npx tsc -b && npx oxlint && npm run build`, plus `node tools/test_i18n.cjs` for the locale bundles.
- The browser checks need a running backend serving `web/dist` and Playwright (`npm i -D playwright`): `tools/check_menus.cjs` walks every route and `tools/check_responsive.cjs` re-measures every top-level route at 390×780, 834×1112 and 1440×900. They exit **2** when Playwright is not resolvable, so a missing browser is never mistaken for a defect.
- `tools/check_versions.py` compares the app version in `mlo/__init__.py`, `tauri.conf.json`, `Cargo.toml`, both `package.json`s, the Dockerfile's `MLO_VERSION`, this README's header and `desktop/README.md`. CI (`.github/workflows/ci.yml`) runs the Python suites, the frontend gate and `cargo check` for the Tauri shell on every push and PR; the checks needing a live backend are
manual.

## Licence, credits and donations

MIT — see [`LICENSE`](LICENSE). The services and projects the app is built on are listed in the app's bottom-left credits popover, in [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) and in `web/public/credits.json`. `/donations` carries two addresses with a copy button each — Litecoin `LRisZa9HYBKE2sUc3VELYZq2WnyYtG6Jvu`, Bitcoin `bc1qf2snsus59ydvmk8rwp09e698gxjdlmyxyrnycu`
— a photograph of the maintainer's cat with one line under it (*Donate to feed my cat*), and **nothing is gated behind it**.

Legacy v1 (the Tkinter app, CLI and PyInstaller/Inno packaging) is archived on the `archive/legacy-v1.7` branch.
