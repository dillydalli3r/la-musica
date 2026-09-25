# la musica

**v4.1.4** — a self-hosted app that *manages, optimizes, audits, grades and plays* your music library, from a browser, a desktop window or a phone.

**la musica** (formerly Music Library Optimizer) is a FastAPI backend plus a React UI over the `mlo` engine: music and music videos (karaoke-synced lyrics), playlists, likes and favourites, artist artwork and biographies, a multi-source lyrics chain, MusicBrainz/Discogs/AcoustID identity, and a managed Soulseek client whose auto-importer verifies what it downloads. It grades the library (69 checks), audits it, and runs an installable toolchain — all local.

State — config, playlists, the beets library, Soulseek config, caches, runtime-installed tools — lives in one hidden `.mlo` folder beside your music.

- Release notes: `docs/release-notes/release-notes-<version>.md` (pre-3.14 under `local/`, 3.14–4.1.2 in the repo root).
- iPhone/iPad: the sideloaded build installs through **SideStore** (or AltStore) — add
  `https://github.com/dillydalli3r/la-musica/releases/latest/download/source.json`
  as a source once and it always offers the newest IPA. The file is generated for every
  release by `.github/workflows/release.yml`.
- Grading and optimization contract (check ids, defaults, presets, runbook): [`docs/OPTIMIZATION-GRADING-SPEC.md`](docs/OPTIMIZATION-GRADING-SPEC.md). This README and the GitHub description follow it.

## Quick start

Docker is the only supported **server** install; every client connects to it over the network.

### Docker (recommended)

```bash
docker compose up -d --build     # build from source and start
docker compose logs -f           # follow the backend log
docker compose pull              # fetch the prebuilt GHCR image instead
docker compose down              # stop and remove
# open http://localhost:8000 — your library is mounted at /music
```

- **Edit one line first**: the bind mount, committed as `./music:/music`. Use an absolute host path (`/path/to/music:/music`, `D:/Music:/music`) — compose would otherwise create an empty `./music` and read that.
- Image `ghcr.io/dillydalli3r/la-musica:latest`, container `la-musica`, port `8000`. `MLO_MUSIC_FOLDER=/music` is the only env var it needs; `MLO_SERVER_HOST=0.0.0.0` makes the published port reachable (and turns the login gate on).
- `/music` holds the library **and** all app state (`data`, `downloads`, `trash`, tools), so one mount carries everything.
- Unprivileged `mlo`, uid/gid **1000**: the mounted folder must be writable by uid 1000, or `.mlo` cannot be created. Healthcheck: `http://127.0.0.1:8000/api/health`, 30 s.
- `watchtower` is on (`WATCHTOWER_POLL_INTERVAL`, 300 s) and replaces a `--build` image with the published one; to keep building from source, start only the app: `docker compose up -d lamusica`.
- Tools install themselves at runtime from **Settings → Dependencies** (apt rows: `ffmpeg`, `flac`, `libjxl`, `jpegtran`, `fpcalc`, `rsgain`, plus `mono-runtime` with `libgdiplus` and `php-cli` for CUETools/AudioAuditor and Logchecker). `GET /api/capabilities` says what a given server can do.

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

- `python -m server.main` reads `server_host` / `server_port` from the config and binds there, so module, Settings page and login gate agree. A hand-typed `uvicorn --host` bypasses the config — bind through the config.
- The music folder is picked in the UI (**Settings → General → Music folder**, or wizard step 1); `GET /api/fs/dirs` browses the server's folders. Choosing one *moves* the app state (`<music>/.mlo`) into it.
- Install the toolchain from **Settings → Dependencies**: `ffmpeg`, `flac`, `libjxl`, `jpegtran`, `oxipng`, `rsgain`, `AudioAuditor`, `Logchecker`, `php`, `CUETools`, `fpcalc` (optional), `librosa`, `beets`, `slskd`, `yt-dlp`. Each row shows installed, pinned and upstream versions; a copy an older release installed beside the app is still read.
- A row is `deps` (installer fetches it), `unsupported` (no build here, with the reason) or `system` (the button copies the package manager's command). Every `deps` row has **Install** / **Update**; **Update all** presses every missing or outdated row.
- A press fetches the newest publisher release (GitHub, PyPI, windows.php.net); the pinned version is what a first install fetches when nothing upstream is reachable. An updated row installs into a *new* folder, so a running install is never rewritten in place.
- `dependencies_auto_update` (off) installs missing or outdated tools on boot.

### Configuration & credentials

- **First run asks six things**: music folder, account (the login gate), external tools, source keys and cookies, Soulseek login and sharing. Everything else has a shipped default, editable in Settings.
- Config: `<music>/.mlo/data/config.json`, edited through Settings (`GET/POST /api/config`; `GET /api/config/defaults` is what *Reset to defaults* writes). Seeded from the environment by `MLO_MUSIC_FOLDER`, `MLO_SERVER_HOST` / `MLO_SERVER_PORT`, `MLO_AUTH_MODE`, `MLO_LOCALE`.
- Keys worth knowing: `music_folder`, `server_host`, `server_port`, `auth_mode`, `mb_genre_count`, `genre_sources`, `run_all_order`, `import_scripts`, `naming_script`, `lyrics_format`, `cover_target_size`, `embed_covers`, `playback_eq_profile`, `library_codec`, `auto_import_medium_order`, `playlist_import_parent_albums` / `playlist_import_unmatched` / `playlist_import_create_empty` (defaults `false` / `skip` / `true`), `cookie_notes` (the per-cookie comments, written by the cookie routes rather than by a Settings row). The ones that change a grade are in [the spec](docs/OPTIMIZATION-GRADING-SPEC.md#9-config-keys-that-change-a-grade).
- Credentials sit in that file **in clear** — it is your server's. All optional: `spotify_client_id` / `spotify_client_secret`, `discogs_token`, `lastfm_api_key`, `rym_cookie`, `acoustid_api_key` (`acoustid_user_key` too, for submitting fingerprints), `slskd_username` / `slskd_password`.
- `youtube_cookies_mode`: `none` (default), `file` (a jar saved on **Settings → Videos**; the app owns `<music>/.mlo/data/cookies.txt`) or `browser`. The jar keeps only the cookies a YouTube request is sent (`youtube.com`, `googlevideo.com`, `google.com`, `googleapis.com`) — a browser profile's export holds every site, and the import drops the rest — and each cookie can carry a comment, stored in `cookie_notes` and written into the file as `#` comment lines. The same import box (and the same per-cookie comments) serve the RateYourMusic cookie on **Settings → Discovery**, and both are on **Settings → Sources** and in the wizard's Keys step.
- `GET /api/sources/health` lists every external source (38 rows) with what each needs; `?probe=1` tests them against the provider's own endpoint, so a refused key is reported in the provider's words.

## What the app does

### Library, search and identity

- Artists → albums → tracks, with live grade/audit badges, a *fail only* filter, bulk tag tools, custom tag columns and sortable/resizable columns.
- Music videos are first-class tracks; a web/digital album can fetch its own through yt-dlp.
- Album badges: measured dynamic range (`ADR`), the medium, every release country, format/bitrate.
- Every track row has the same "…" menu: tag editor, genre/advisory imports, its scripts, credits, the stored readout. The script entries are **generated from the script registry**, not typed into the menu: `GET /api/script-menu` lists every script with the entity kinds it applies from, its Run All slot, its feature switch and its force flag, so an album's menu offers all **21** scripts and a track row or playlist selection the **10** file-scoped ones (`tools/test_script_menu.py` fails a registry script with no applicability).
- The top search bar searches the library (`composer:`, `person:`, `genre:`, `tag:`) or MusicBrainz; `/mb/search` is a full in-app MusicBrainz browser with *Auto-import* and *Add to library* — and on a release group's page **every edition row adds THAT edition** (the row's own release id, not the group and not the policy's pick), with the spinner on the row you pressed.
- An entity's alias in your locale is shown beside its name (`宇多田ヒカル (Hikaru Utada)`), exact locale first, then MusicBrainz's primary alias.
- Entity pages: grading and audit detail, MB/RYM links, cover upload and search, Wikipedia descriptions, tag editing, a lyrics editor, a **Credits** view from MusicBrainz `artist-rels`.
- The cover finder ranks candidates with the album's own identity and writes the winner by default (`cover_review` asks first); a URL that cannot be fetched writes nothing.
- **Recommended (Local)** and **Home** are computed from the library's own tags — no provider, no model, no network.
- **LIBRARY → BROWSE** is a query builder over 120 fields with the operations each declares, match all/any and a live count; a saved one is a **smart playlist** (the filter, not the rows).
- **Ratings** are half stars in the UI, Picard's 0–10 in the store, written to the file's `RATING` tag.
- **Five views** — Grid, Compact, Albums, Artists, Tracks — each with its own sort and columns; filter presets (*Failing*, *CD rips*, *Instrumental*, …) plus facets with live counts.
- **Podcasts are a series, not an album.** MusicBrainz links an episode's release group *part of* a series of type Podcast (the episode's own primary type stays *Broadcast* — there is no Podcast release-group type), and the app records that identity on the files (`PODCASTSERIES`, `PODCASTSERIESMBID`, `PODCASTEPISODE`), so every surface answers from a scan and never asks MusicBrainz again: a **Podcasts** shelf on Home (one card per series, its newest episode, nothing at all when the library holds no podcast), a series page (`/podcast/<series>`, `GET /api/podcasts?series=…`), a **Podcasts** preset beside the Library's other filters, and the artist page's own *Podcast* bucket instead of *Other*. Grading does not hold an episode to music-CD expectations: the music-only checks (lyrics, RYM links, album description, the three genre checks) are off for a folder whose files carry the podcast series, and a file without it is graded exactly as before.
- **The accent colour is yours, per device.** Settings → Appearance offers 15 presets plus a custom colour (a native picker and a hex box taking `#rgb`/`#rrggbb`); the choice lives in `localStorage["mlo.accent"]`, and the app derives the accent, its soft twin and the ink drawn on it (WCAG contrast decides between white and black) so a typed colour is legible rather than guessed. The canvas surfaces (visualizer, EQ curve) repaint as soon as it changes — no reload.

### Discover, recommendations and watching

- **DISCOVER** browses genres across every configured provider (MusicBrainz, Deezer, iTunes, TheAudioDB, Last.fm, ListenBrainz, Discogs, Wikidata, Wikipedia, Spotify, RateYourMusic, Bandcamp) with a library/online/both scope.
- **RECOMMENDED (ONLINE)** takes a seed (the library, a genre, or the page you are on); its **(LOCAL)** half is scored from the library's own tags and needs no network.
- **WATCHED ARTISTS** keeps an artist under watch: policy, release types, allow/never lists, a per-cycle cap and auto-add. Each check queues release groups into the download queue.

### Player

A persistent player bar (queue, drag-reorder, shuffle, repeat-one, speed, sleep timer, ReplayGain, visualizer, app-wide volume) plus a fullscreen player with animated karaoke lyrics.

- Play state comes from the media element's own events — BOTH of them: its `play` writes the state as its `pause` clears it, so an OS-made pause and a track change's own hand-over are both what the bar shows, and a REFUSED `play()` (a platform that will not start audio, a stream that fails to load) says so instead of leaving a bar that claims a track which never began.
- The lyrics pane, metadata block, transport and top bar draw no background or blur of their own; the ink is derived from the cover (`npInk`), so a bright cover flips to near-black text.
- Both lyric surfaces share the same size control (`−`, a typeable percentage, `+`, 5 % a press, 85–160 %, remembered per surface) and the same offset control (`−`, pending shift, `+`, Save).
- The pane is a control you own: one microphone toggle in the player's control row, shown only while the track has lyrics, and it does the same thing at every width — below `md` it used to unfold the phone's whole compact block instead of showing the lyrics (so a phone could not show a pane, and the zoom/offset controls never mounted there); the pick is remembered per device either way.
- Lyrics come from the `.lrc` beside the file and/or its `LYRICS` tag; a stamp before the file's start clamps at `[00:00.00]`. Where a surface is about ONE track's own lyrics (its page, its details row, its lyrics views and the import wizard's Lyrics step) it also says **which kind** they are — **Synced** (the `.lrc` or embedded lyrics carry timestamps) or **Plain** — from the file's own stored truth (`lyrics_kind` in the payload); with `lyrics_allow_plain` off (the shipped default) a plain lyric is shown as the failing state it is, with the reason naming the setting, and a track with no lyrics shows no kind at all. The mark stays on a single track's own surfaces — **never on a list of tracks** (an album's tracklist, the library's rows), where it is only clutter.
- ReplayGain runs through the WebAudio gain stage in **track**, **album** or **off** mode (`replaygain_mode`, `replaygain_preamp_db`, ±24 dB); a file without tags is measured on the fly when `replaygain_measure_missing` is on.
- **Equalizer** (sidebar → MAINTAIN): an Equalizer APO / Peace profile applies to playback (`playback_eq_profile`, `""` = off, so every client of the server hears the same curve). Built-in presets, APO/Peace/AutoEq import, a live curve editor.
- Pressing play on what is already playing restarts it; a video is transcoded to fragmented MP4 when its codec needs it (`GET /api/videos/stream?transcode=1`).
- **A page uses the window it is given.** Every page shell caps at 1600 px (Browse and Export always did); the old 1152 px reading cap stranded ~400 px of a 1568 px window. Home also refreshes itself on a 60 s interval — a plain poll of the server's cached payload, so it costs one small GET — and the Library's toolbar carries a name search plus an A–Z menu beside the view switcher (both compose with the current sort and with each other, in all five views).
- **Genres are stored lower-case and printed as a reader writes them**: "progressive rock" shows as "Progressive Rock", `r&b` as "R&B" (`titleCaseGenre`) — the filter buckets, the tag values and the `genre:"…"` query keep the lower-case form they match on. The family the app derives is the part that has to be right first: new wave is rock (not electronic), no wave is experimental, new romantic is pop.
- **An artist's discography opens on Album, EP, Single**, then everything else — a compound label sorted by its primary type, so "Album + Live" sits with the albums instead of leading a page of live records.
- **The fullscreen pane gets out of the way, and stays legible over any cover.** The pointer is hidden after ~3 s of stillness — only on a device with a fine pointer, never during a drag, with a menu or dialog open, or while a control is under it (those rows opt back out in CSS) — and the next move, click, key or wheel brings it straight back. The seek and volume sliders and the lyrics zoom/offset chips take their colours from the cover's own ink polarity (`--seek-track`/`--seek-fill`/`--seek-thumb`/`--seek-ring`), so nothing is a fixed grey that vanishes on a bright cover; `tools/check_np_metadata_contrast.cjs` measures the ratios from the rendered pixels on four covers.
- **Music keeps playing when the app is not in front.** The shell owns the iOS audio session (`desktop/src-tauri/src/ios_audio.rs`): the category is taken once at setup, playback ACTIVATES the session, and it is re-asserted at the transitions where iOS takes a backgrounded app's session away (background, becoming active, an interruption ending with `ShouldResume`, the audio server restarting). It is deliberately never re-configured or handed back while the app lives — 4.0.3 did both on every play/pause and the webview's own element was interrupted by the app's category call landing mid-start, which is "pressing play just pauses it immediately" (R268). While the player is playing and the app is in the background, the shell also renders a generated half-second of dither — ±1 LSB, about −90 dBFS, inaudible and deliberately not digital silence — through an `AVAudioPlayer` on that session: iOS keeps a backgrounded app alive because it is PRODUCING audio, and this app's music is decoded by WebKit's process, so without it the app process looks idle and gets suspended — taking the music and the lock-screen star's command handler with it (R288). It renders nothing in the foreground, and what it is doing is readable in the app itself (Settings → Downloads & playback → Playback diagnostics). The audible trade is stated rather than hidden: a player this app interrupted does not resume by itself when you pause la musica.
- **The phone hears the volume slider, and the audio graph can always start.** `HTMLMediaElement.volume` is read-only on iOS, so the app's level goes onto a volume stage inside the WebAudio graph every attached element is routed through (`applyVolume`); the shared `AudioContext` is resumed by the first touch/key press anywhere in the app, and again whenever the platform parks it (a state change — while the page is on screen, and also while it is hidden if a track is still playing, which is the case the platform parks it in — or the app coming back). Cleartext media is allowed by the blanket `NSAllowsArbitraryLoads` key beside the web-content one — the media loader reads a different key from the page's own fetches — and `NSLocalNetworkUsageDescription` is what lets iOS 14+ even ask about a server on the LAN.
- **On iOS the system's star is the app's favourite**: `MPRemoteCommandCenter.likeCommand` is wired by the shell (`desktop/src-tauri/src/ios_like.rs`, iOS only — the webview's Media Session API has no like action), pressing it runs the same like the app's own hearts do, and the OS star is filled while the track is favourited (`desktop/README.md` has the wiring). A press that arrives while the webview is parked (the lock screen — where this star is actually used) is REMEMBERED and re-sent the next time the app is active, and forgotten the moment the app answers with a state of its own: one press, one like.

### Import

Drag & drop uploads, a watched folder, staged `.mlo/downloads` or Soulseek paths — all through one pipeline (`server/imports.py`).

- **An archive imports like the folder it contains.** Drop or pick a `.zip`, `.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`, `.tar.xz`/`.txz`, `.7z` or `.rar` and it is unpacked into a staging folder (`POST /api/import/unpack`, discarded with `…/unpack/discard`) before the ordinary album detection runs — a rip in a zip and the same rip as a folder give the same album folder, the same partial marking and the same sheets. Unpacking is treated as untrusted input: a member with an absolute path, a `..` segment, a drive/UNC prefix, a symlink, a device or a fifo is **refused before anything is written**, naming the member; `.7z`/`.rar` say they need 7-Zip when it is not installed; a nested archive is not unpacked. A drop takes one file, several, nested folders, an archive or a mix in one gesture, and in the desktop shell an OS drop resolves through the server-side scan path (a phone cannot read the server's disk, and says so).
- **A digital release settles itself.** For a Digital Media import, `SOURCE` comes from the release's own store links (Bandcamp, Qobuz, Deezer, Spotify, Apple Music, Amazon, Tidal, 7digital, Juno, Beatport, HDtracks, ProStudioMasters, SoundCloud, YouTube, archive.org, OTOTOY, mora, e-onkyo, Presto, Boomkat, Napster, Traxsource, Bleep) or from the provider the files came from — and when nothing states it, the **Match** step asks for it once (`POST /api/import/source`, suggested answer `digital_media_source_value`, shipped `Digital`; an unattended import asks through the same prompt mechanism). The Lyrics formatter (script 1) runs as part of the import, and a lyric that arrives untimed or still fails the app's own grade is removed **with its sidecar and its derived translations, and the count reported**, but only when the chain's fetch step will replace it (never with script 13 out of the chain, a kept family, or `lyrics_allow_plain` on). The album description is fetched by the import's own metadata step, and when nothing is found the import says so and points at the album page.

- **AcoustID fingerprint matching** identifies the release from the *audio*, not the tags (needs `acoustid_api_key` and `fpcalc`); accepting a match writes `ACOUSTID_ID` + `ACOUSTID_FINGERPRINT` in one save. **Script 21 (Fix AcoustID pairs) completes or CREATES that pair from the file itself**: the recording comes from the file's own `ACOUSTID_ID`, then `MUSICBRAINZ_TRACKID`, then the single bracketed UUID the naming script wrote into its name, with the fingerprint taken locally and the AcoustID service asked only for a half pair that names no recording — so the grader's *Missing ACOUSTID_ID (run Fix AcoustID pairs)* is repairable even for a file whose name already stated its recording. It runs on any selection through `POST /api/run {ids:[21], targets:[…]}`. **Script 22 (Submit fingerprints (AcoustID))** sends a file's fingerprint with its MusicBrainz recording id back to AcoustID's public database — from the wizard's AcoustID step, an entity menu, or Run All once it is ticked; a pair AcoustID already links is never re-sent, and it needs `acoustid_user_key`.
- **A single song out of a rip is imported into the album that rip is.** Drop one track (or upload it) and the import resolves where it belongs — the album the library already holds for that release id, else the album the file's own `ALBUM` tag names, else the name you gave — and MOVES it into that folder without overwriting anything already there (**fill, never replace**: the rip's `.cue`/`.log` stay). The rip's own sheets then supply the tracklist the file alone cannot: the `.cue` (or the `.log`'s TOC) becomes the folder's `.mlo_expected.json`, so the album reads as **partial** with its missing rows — the album page, the grade and the AccurateRip pass all know which tracks are not there, script 9 leaves a partial disc's `.accurip` exactly as it is, and a CD rule that cannot apply says why instead of failing on a slice. A file that names no album is imported as its own album, exactly as before.
- **A playlist from a streaming service.** Playlists → *Import from a streaming service* (`POST /api/playlists/import/streaming`) reads one **Deezer**, **Spotify**, **YouTube Music** or **Apple Music** playlist URL, matches every track against your library and reports what it found row by row (and why not, for the ones it did not) — *Check* is the same read with `dry_run`, writing nothing. What it creates is the app's own playlist, in the service's order and deduplicated, remembering where it came from (`origin`, `origin_url`). Three keys decide the queueing: `playlist_import_parent_albums` (off by default — on, every imported track whose album you do not have queues that parent **album** through the normal add-by-name path), `playlist_import_unmatched` (`skip` by default, or `wish` to also queue each unmatched **track** by name) and `playlist_import_create_empty` (on by default, so a playlist that matched nothing is still created and the attempt is visible).
- The wizard's eight steps: **Select & separate → Links → Match → Covers → Genres → Lyrics → Advisory → Finish**; *Finish* runs the scripts you ticked.
- The **import script chain** (`import_scripts`, i.e. `DEFAULT_CHAIN` = `run_all_order`, one list in `mlo/config.py`) runs after the wizard; `import_auto_scripts` turns it off for imports.
- An import decides four families for itself — lyrics, genre, advisory, embedded cover — and replaces what arrived (`server/imports.py::drop_arrived_values`); a family in `import_review_families` is left as it arrived.
- **Bulk import** queues several albums with `import_bulk_concurrency` (2 by default, 1–8).
- **An import finishes on its own and only asks when the answer is yours.** `import_autonomy` ships as `automatic`: it decides every family it can and reports what no source could supply; what waits is the family you kept, the cover when `cover_review` is on, or something no source could state. Those are what the notification menu and the Soulseek page's *Needs you* row carry. A waiting album stays **parked** (a library-wide Run All skips it).
- **An import never loses the album it is working on.** *Beets tagging* (script 14) moves and renames it, so later scripts act on the folder it is in now; the chain's end is what the queue row and album page report.
- **The cover a framework album carries is a stand-in**: the real cover is fetched over it, and the stand-in is dropped only once that cover is written.
- **When the picked edition is not on the network, the album is not given up.** *Add to library* tries the release group's ranked editions in order, up to `soulseek_fallback_candidates` (1–10, 5 by default); if none is there, the release moves to the queue's **Background** list and keeps being searched.
- **A wish or a followed artist takes only a lossless copy.** `soulseek_auto_lossy_policy` ships as `never`; the Soulseek page always asks.
- The **Genres** step asks the whole chain with one button, in `genre_sources` order (RateYourMusic first), cascading track → release → release group → artist; the answer says which level spoke.
- The **Advisory** step resolves `ITUNESADVISORY` from every applicable source (Deezer/Spotify by ISRC, Apple's routes), merged so a stated value ends the question; the configured AI answers only when everything else came up empty (`advisory_ai_classify`).

### Soulseek & the download queue

A managed slskd instance (autostart, shares = `<music>/Artists`, a rescan whenever the library changes), with search and download UI, a live status dot, bulk and whole-user downloads.

- **Transfer progress is pushed**, not polled: `GET /api/soulseek/downloads` rows and every live job ride the progress WebSocket as `{"type":"transfers"}` frames (0.4 s cadence), with a 30 s fallback for a dead socket. They never reach the notification tray.
- **An import that needs a hand is an outcome**, announced wherever you are: `import_needs_data` raises a system notification, a tray entry, an in-app event and a *Needs you* row.
- **Is the port open?** *Test port* (`GET /api/soulseek/port-check`) answers with six rows that each say what they prove: a real TCP connection, a bind test, whether the host publishes the very port the daemon holds (in a container — measured from inside, with the app's own published port as the control), the gateway's own mapping entry, the LAN/WAN address shape (CGNAT named as such), a public-address self-connect (refused ⇒ unknown) and slskd's signed-in state.
- The listen port is opened on the router by the app (`soulseek_upnp`, ON) — UPnP IGD first, NAT-PMP behind it, reported as made only when the gateway confirms it, and never replacing a forward the router already holds. A container can only see Docker's bridge, so it asks the router you name in **Router IP for port mapping** (`soulseek_router_ip`, blank = auto-detect); without that it says so instead of guessing.
- **The sharing card never calls a share browsable while the forward it asked for is missing, or while nothing actually listens.** Search, login and slskd's own index all work with the listen port closed, so "other users can search, browse and download them" is exactly what a peer's *Browse* contradicts: with automatic port opening ON and a gateway that answered `no_gateway`/`unsupported`/`refused`/`error`, the audit reports `listen_unconfirmed` with the gateway's own words and a hint written for the install running (a container is told the forward goes to the HOST's LAN address; a bare-metal install is told which port to forward). **In a container the two halves of that remedy are told apart by measurement**: `soulseek_port._publish_check` reads the host's own port list from inside (a published port is accepted on the container's gateway, the app's own served port is the control, so a refusal is only called a missing publish line when this host demonstrably hands published ports back), the audit's `listen_unpublished` names the compose line AND the pin (`ports: "<port>:<port>"`, `MLO_SOULSEEK_LISTEN_PORT`) when it is missing, and a measured pass clears the compose file and leaves the router — at the address the internet actually sees for this host, since a VPN or a second router in front changes which address peers dial. A mapping the router CONFIRMED still has to have something accepting on the port — the audit's verdict is the port check's own listen row, so a dead listener is never `ok` however green the index is — while a transfer in slskd's upload tree clears the MAPPING case on evidence (a peer really connected back), because nothing inside a container can read the router's table. **And an import that ran no script chain still asks slskd to re-index** (`import_auto_scripts` off, every script held for review, or a review stop), so an album that just joined the library is in the share instead of waiting for some later run to refresh it.
- **A download queued from the Soulseek page imports itself, whole or not at all.** The three page Download buttons record what they queued (peer + remote files, kept beside the app state so a backend restart between the press and the arrival does not forget it) and the album is imported through the same importer the Import button uses, once the WHOLE intended file set has arrived at the intended sizes — a folder holding part of it, or one of its files still being written, is a download still coming, so an album is never chained and graded from a partial set. A re-press MERGES with an in-flight download (slskd's enqueue does not dedupe on its own), and the still-downloading guard matches a transfer by its peer AND its remote folder path, never by a folder's leaf name — every peer and every batch has a folder of any given name. Two switches decide whether the automatic import runs (`import_autonomy` in "review", or `manual_import_enabled` off, leaves the album in the download folder with its ready-to-import row and says so once).
- **Which edition is fetched** is `mlo/release_choice.py`, the same policy the release-group page shows: official editions first, then the configured medium order (`auto_import_medium_order`, shipped as **CD, Vinyl, Cassette, Other, DVD, Blu-ray, VHS, Video CD, LaserDisc, Digital Media** — the video carriers above digital, so a music video on a disc outranks the same video as a download, and digital last because a download carries no pressing to match; an install still holding the OLD shipped list follows the new one, while a list you edited is kept exactly as saved), then the earliest date (and a comment-free title over a commented one when everything else ties). **A box set sorts below the album it contains** — an edition carrying video media *beside* the album's own medium, or three discs or more — while a single-disc DVD (whose own medium *is* the video) is ranked by the medium order like any other. Disc streams are preferred (`prefer_disc_streams`), and a disc structure beside a re-encode is remuxed as the disc's own title.
- **The original pressing wins the date, exactly**: the EARLIEST edition the group offers is the one fetched, and a full `YYYY-MM-DD` beats a bare year when two editions could be the same day. A pressing that predates the group's stated first release date is still the earlier record.
- **A comment in the title loses the tie**: when two editions are otherwise equal, the one with no MusicBrainz disambiguation comment wins, so the plain pressing beats `(BMG Club edition)`, `(CB 811)` and the like — a comment is MusicBrainz saying that edition needed distinguishing.
- **Where a release is fetched from decides itself, and either network falls through to the other.** A music video on **Digital Media or Web** cannot be a Soulseek folder, so it comes from YouTube first (achieved length, lyric/cover/tribute rows refused) into `<downloads>/YouTube/<Artist - Album>`, then the same import — and a track YouTube has no usable upload for is asked of the network per track before it is given up on. A music video on a **disc** keeps the Soulseek path first, and a search that comes back empty is followed by the same per-track YouTube pass before the album is parked on the wish offer. A Web release whose YouTube half cannot run — `youtube_enabled` off, or yt-dlp missing — is **searched for on Soulseek instead of failing**, and when slskd itself cannot answer that is the reason a track's miss reports ("Soulseek is not running", "Soulseek is not logged in"), never "no copy", which would be a claim about a search nobody ran. An audio release is never routed anywhere but Soulseek whatever its medium, and the SOURCE tag is stamped per file because an album can be part YouTube, part peers.
- **A video YouTube does not have comes from Soulseek**: a track with no usable upload (or one yt-dlp could not deliver) is looked for on the network as the plain video file it is — one search of the track's artist and title, the first copy whose container the library supports and whose name carries that artist and title, from the fastest peer. Single-video grabs (`POST /api/videos/download-youtube`) answer `queued: true` and leave the transfer to the app's own Downloads rather than holding the request open; a track neither source has is reported naming both, and one that no source can serve never stops the rest of the album from importing.
- **The auto-importer searches by what can only point at that release**: a physical pressing by catalog number and barcode (`soulseek_auto_physical_queries`), else by label and country; digital by `soulseek_auto_digital_queries`. Search terms are stripped of punctuation and resolved through MusicBrainz aliases (`ぴーなた` → `pinata`).
- **A release is also searched by its MBIDs, by default.** With `soulseek_auto_mbid_queries` (on) the default query set of a release carries its own MusicBrainz id, the recording ids of its first `soulseek_auto_mbid_tracks` tracks (4, 1–10) and each of those tracks' own "artist title" — deduped against everything already rendered and posted in the SAME parallel batch, so the extra reach costs no extra wait. The tracklist comes from the release payload the app already holds: no additional MusicBrainz request.
- **One track can be searched for on its own, by name or by pasting its MBID.** The Soulseek page's search box accepts a UUID (chip + hint), resolves it with the cached MusicBrainz client and runs each of that track's queries as its own slskd search AT ONCE (one merged, deduped result list, one poll key, one stop), and any library track's own menu offers "Search Soulseek for this track" — nothing is added to the wishes or the queue unless you download something.
- **Failed means given up.** A failed attempt whose wish the worker will search again by itself lives in the queue's **Background** section (with the failure's own sentence and the next-search clock), a terminal failure keeps its Failed row, and a settled job never draws a second row for a wish that still exists — the wish's own row is where its state lives. `wishes_interval_hours` ships as **1**: an hour between two ATTEMPTS of the same release, each of which still walks every ranked candidate.
- **The queue's finished sections clear themselves when a new run starts** (a job start, a bulk enqueue, a page download, an accepted prompt, the worker's own pass) — live work, background wishes, retryable failures and *Needs you* rows are left alone, and a job that finishes after the clear stays visible.
- **Cancel stops the network work at once.** The queue row's Cancel (and the Auto-import Stop) drops the job's outstanding slskd searches on the spot, cancels its transfers, and either aborts the import at a safe point or says it will stop after the current step; the row is gone the moment the press lands, and a cancelled *Add to library* takes its framework album and its wish with it.
- **One port, one number.** `MLO_SOULSEEK_LISTEN_PORT` seeds the app's own `soulseek_listen_port` at startup (the same way `MLO_MUSIC_FOLDER` does), docker-compose publishes it as the same value, and a port changed in the UI while the environment pins it is refused with the pin named — a share peers can see the SIZE of and never connect to is a forward pointing at a closed port.

- A CD candidate's `.log` is gated *before* any album byte is requested (`soulseek_auto_log_min_score`, default 100), and candidates are ranked towards the copy that arrives fastest — up to `soulseek_candidate_slots` (3) at once from three different peers; the first that verifies becomes the import, the others are cancelled.
- **Two limits**: `soulseek_search_concurrency` (releases at once, default 3) and `soulseek_candidate_slots` (candidates of one release, default 3). Over them, work waits (slskd's own `soulseek_download_slots` is never touched); the Queue tab's header reads all three back.
- A release is imported **once**, into one album folder: the destination is checked against the release's MusicBrainz ids, so a download of an album you already hold is refused with a sentence instead of appearing as `… (2)`. The album stays locked from the first tag write through the last script.
- The bar takes **any MusicBrainz link**: a release, a release-group (its editions are walked), an artist (its discography is queued in the background) or a recording.
- A wish keeps looking every `wishes_interval_hours` (6) with retry backoff **until it is found or cancelled**. Notifications cover the add, the download and the import. When the import lands, the downloaded copy is deleted (`soulseek_clear_downloads`, ON).
- **The search asks more than one pressing**: every add records the group's ranked editions, deduped by folded catalog number, so `GED 24425` and `GED24425` are one search. The recorded list is a snapshot of the editions, never of the order: each entry carries its own facts (date, status, medium, track count, comment, country) and the walk re-ranks them at every attempt, so a release queued before a rule changed is searched by the rule in force now.
- **What counts as a good rip log is 100 wherever it is asked**: the acquisition gate, the grading check and the audit. A release with no log is offered through an explicit confirm.
- **Two independent size caps**, both 5 GB by default (`soulseek_cache_cap_gb`, `trash_cap_gb`, 0 = off). Over its cap a store is emptied oldest-first, and what is in use is never touched (`server/job_locks`); a kept file is reported and still restorable.
- **Clear all** empties queued/waiting work in one press and never touches a running release (that is a cancel, on its own row) or a finished one. Waiting releases are their own group, in the order they will start.

### Optimization — the 22 scripts

Optimization → *Run All* executes `run_all_order`, shipped as **11 → 3 → 14 → 15 → 2 → 1 → 13 → 18 → 17 → 8 → 5 → 19 → 6 → 7 → 9 → 12 → 16 → 10 → 20 → 21 → 4**: everything that moves a file first, everything that reads it last. Every script also runs on its own, from the menu or an album page. A library-wide run claims the paths it walks; an album-scoped run holds only its own album, and a script that moves an album takes the claim with it (`server.job_locks.move`).

**22 (Submit fingerprints) is opt-in**: it publishes fingerprints to AcoustID's public database, so it is left out of the shipped order and offered as an individual script instead (`server.script_runners.OPT_IN_SCRIPTS`).

| # | Script | What it does |
| --- | --- | --- |
| 1 | Format lyrics | Canonical embedded LYRICS / `.lrc` (padding, blank lines, zero-timestamp rule, Enhanced/Extended word-sync); MEDIA/SOURCE normalization |
| 2 | Format CUEs | Canonical CUE text, `FILE`-line fixes, `CD-N` sheet renaming |
| 3 | Optimize FLACs | Re-encode at `library_codec_quality`, strip padding/CUESHEET/APPLICATION and non-canonical tags, convert non-conforming files to `library_codec` |
| 4 | Grade | The full grading battery (see the spec) |
| 5 | Process images | Covers resized/cropped (`cover_target_size`, default 1200; per-format targets), JPEG/PNG/JXL optimization |
| 6 | Audit library | AudioAuditor detectors + CD `.log` CRC verification → `AUDIT` |
| 7 | DR & ReplayGain | In-process loudness-war DR tags + rsgain ReplayGain (album gain, FLAC and MP4 alike) |
| 8 | Auto tagging | `ITUNESADVISORY`, `INSTRUMENTAL`, `MOOD`, `ENERGY`, `GENRE` |
| 9 | AccurateRip | CUETools `.accurip` generation and verification; regenerated only when a track's audio changed, and never for a partial album (its stored file describes the whole disc) |
| 10 | Format all | Final canonical pass: `.accurip`/`.cue`/`.lrc`/tag trim + the embedded-cover policy |
| 11 | Remux videos (MKV) | Any video container → MKV, video copied bit-exact where possible, audio to FLAC, chapters kept |
| 12 | Key & BPM | librosa-backed `INITIALKEY` + `BPM` |
| 13 | Fetch lyrics | The configured synced-lyrics chain into the configured format |
| 14 | Beets tagging | Managed beets (Picard parity) with the naming script and work/movement tags |
| 15 | Release tracklist | Writes `.mlo_expected.json` (the release's own tracklist) into the album folder |
| 16 | Mood & Energy | The mood classifier alone (`MOOD` + `ENERGY`) |
| 17 | Lyrics transliterate (AI) | `TRANSLITERATION-<LANG>-LATN` / `TRANSLATION-<LANG>` tags and sidecars, re-synced at `lrc_sync_level` |
| 18 | Publish lyrics (LRCLIB) | Submits this library's lyrics for recordings LRCLIB lacks (`lrclib_auto_publish`, `force_publish`) |
| 19 | Optimize artist images | Crops `Artists/<Artist>/artist.*` to `artist_image_aspect`, downscales to `artist_image_target_size` (never upscales) |
| 20 | Optimize library layout | Settles what it can prove with `layout_apply`: case-correct names, audio outside an album folder, empty albums, stray sidecars (including numbered duplicates like `description (2).txt`) |
| 21 | Fix AcoustID pairs | Completes OR creates the pair from the file itself: the recording comes from `ACOUSTID_ID`, then `MUSICBRAINZ_TRACKID`, then the one bracketed UUID in its own file name; the fingerprint is taken locally (`fpcalc`) and the AcoustID service is asked only for a half pair that names no recording |
| 22 | Submit fingerprints (AcoustID) | Gives AcoustID the fingerprint + the MusicBrainz recording id a file states: the recording from `ACOUSTID_ID`, then `MUSICBRAINZ_TRACKID`, then the recording UUID in the file name; the fingerprint from `ACOUSTID_FINGERPRINT`, or taken locally (`fpcalc`) when the file carries none. Nothing already linked is re-sent (the app asks the service what it holds and records what it submitted under `<music>/.mlo/data`), a file that names no recording is skipped by name, and it needs `acoustid_user_key`. **Opt-in** — never in the shipped Run All order, because it publishes to a public database |

**What script 17 transliterates is decided from evidence**: the rule (`mlo/lyrics_xlit.xlit_needs`) reads the track's own `LANGUAGE` tag, then the script of its lyrics (kana is Japanese, hangul Korean; a statement in Cyrillic or Han states nothing), then the function words of the languages the app knows. When nothing can say, script 17 asks the configured AI one question about that track and stores the answer.

Force flags, one per script (`force_lyrics`, `force_cue`, `force_tracklist`, `force_reencode_flac`, `force_reencode_images`, `force_audit`, `force_accurip`, `force_dr_replaygain`, `force_audiometa`, `force_mood`, `force_auto_tag`, `force_xlit`, `force_publish`, …): a script in the one-shot **Force** menu really turns its switch off rather than falling back to a saved one. Script 20 is the one key that turns work *off* (`layout_apply`).

Scripts that are pure no-ops when their feature is unpicked are skipped rather than run: `dr_replaygain_enabled` (7), `audiometa_enabled` (12), `mood_enabled` (16), `lyrics_xlit_enabled` / `lyrics_translate_enabled` (17), `lrclib_auto_publish` (18), `acoustid_enabled` (21, 22).

Script 6 keeps an evidence record per file (size, mtime and, where the container states one, an audio identity a tag write cannot move — FLAC's STREAMINFO MD5), so a second run over an unchanged album re-audits nothing.

By default the optimizer does **not** embed cover art — it removes it, and covers live on disk as `cover.*` plus per-track sidecars. Settings → *Embedded covers* (`embed_covers`, `embed_cover_jpeg_quality`, `embed_cover_resolution`) makes script 10 embed them.

### Grading

**69 checks** across tracks, albums, artist folders and folders, each toggleable on the **Grading** page with a live filter and the **Strict / Balanced / Relaxed** presets. Every check is on in the factory defaults; an album passes only when every enabled check passes, and the summary counts checks (`summary_pass` / `summary_total`) plus `albums_passed` / `albums_failed`.

- A check that raises counts as *could not be evaluated* and fails.
- Two checks follow the codec target: `grade_check_lossless_source` stands down when `library_codec` is uncompressed (`wav`/`aiff`) or `keep`, and `grade_check_cd_format` applies where a CD is expected.
- A tag **value** has one canonical form (`mlo/tagtext.py`), applied on every write and re-applied library-wide by script 10: closed-vocabulary tags are cased and folded, spacing collapsed, and an unknown value left alone. `RELEASECOUNTRY` holds every country the release states, earliest first. Genres are stored broad-first (`Rock / Shoegaze / Dream Pop`).
- A tag with more than one answer is a LIST, stored as repeated container fields (one per value, `; `-joined on read): credit roles, the track's performers, and so on.
- Home and the Library open with the verdict and, when it fails, what fails: one row per album or track, each linking to the thing it names, capped at 12 with the rest a `+N` link into the Library's *Failing* filter. Past three findings the list folds behind **Read more — N findings** (the summary line is always first and is never folded).
- Every check id, what it asserts, its default, the presets, the audit workflow, tag families, quality bars and a runbook: [`docs/OPTIMIZATION-GRADING-SPEC.md`](docs/OPTIMIZATION-GRADING-SPEC.md).

### Export, playlists, offline

**Export** writes a playlist, albums, artists, tracks or the whole library as MP3 (VBR/CBR or custom), AAC, Opus, Vorbis, WAV, AIFF, ALAC, WavPack, WMA or a bit-exact `copy`, in the shipped `albumartist_album_disc` layout — or `flat`, `mirror` or a structure you type in the same Picard-style grammar the naming script uses (`export_codec`, `export_structure`, `export_subfolder`; the page previews one).

- **Where it goes**: `export_target` is `zip` (one archive the client downloads — the only mode a browser can honour, and the default) or `server`, a folder the machine can see, picked with the drive list and free-space readout.
- **What gets copied** is `export_copy_files`, ticked one by one and named per file by the run's `excluded` report: tracks, covers, `.lrc`, `.cue`, `.log`/`.accurip`, `description.txt`, checksums, other text, playlists, and anything the album holds that the app cannot classify (subfolders are reported, never walked). A run nobody asked anything of writes the tracks alone; an empty selection is refused.
- IDs are ID3v2.3 plus optional ID3v1; `export_manifest` writes `checksums.sha256`; `export_playlists` writes an `.m3u8` per album plus `all.m3u8`; every written file is re-opened and verified. Sync mode (`export_prune`) removes audio the source no longer has.
- **A run can be cancelled**: it ends at the next file boundary, what it wrote stays, and the finishing passes are skipped. Two exports cannot fight over one destination — the export holds the drive it writes into, so the second answers 409.
- **ReplayGain** is a mode: `export_replaygain_mode` is `off`, `tags` (measure and write `REPLAYGAIN_*`) or `apply`, which rewrites the audio so the files themselves are level — the album gain for a whole album, the track gain otherwise.
- **An equalizer** rides along: `export_eq_profile` selects a built-in curve or a profile imported from Equalizer APO / Peace / AutoEq (`Preamp:`, `Filter N: … PK|LS|HS|LP|HP Fc … Gain … Q …`, `GraphicEQ:`, `FilterCurve:`), applied to the exported copies only; anything unrenderable (`Include:`, unknown constructs) is reported instead of silently dropped. Both processing modes need a real codec.
- **Playlists** are manual (drag-reorder, favourites, `.m3u8` import/export) or smart, driven by saved grade/audit/tag filters.
- **Offline**: "Download" caches a track in the service worker's media cache and warms its album/artist payloads, so the UI opens and a downloaded album plays with the server down. In the Tauri shells a small filesystem layer does the same job.
- `download_codec` ships as **`copy`** (the cached bytes are the library's own); a codec target re-encodes for that device's cache only. `playback_source` ships as **`stream`** — the player asks the server even when a copy is downloaded, or plays the copy (`downloaded`).

### Notifications and languages

The backend publishes every settled outcome on `/ws/events` — `wish_found`, `wish_failed`, `wish_not_found`, `download_started`, `download_done`, `upload_started`, `import_needs_data`, and the rest. Every client keeps that socket open and raises an OS notification (Tauri's plugin on desktop and mobile, the Web Notification API in the browser) with an in-app toast when permission is refused.

- Kinds are switchable per client (`notify_wish_found`, `notify_download_done`, …); `?since=` replays what a client missed, from the in-memory ring plus the durable log beside the app state.
- **Remote push too**: the server signs with VAPID, encrypts each message (RFC 8291 `aes128gcm`), keeps one row per subscribed device and prunes a device the push service reports gone. The key pair lives outside the config, because `GET /api/config` hands the whole config to every signed-in client. Settings → Notifications shows a switch per device with *Send a test notification*.
- The UI ships in six languages — English, Español, Français, Deutsch, 日本語, Português (Brasil) — chosen per browser (`localStorage: mlo.locale`), then the server's `ui_locale`, then the browser's own.

## Clients

The same React build runs in five targets, and **every one is a client of a server you run**: the Docker container on `127.0.0.1:8000`, a machine on the LAN, or a Tailscale name. No shell carries Python, a sidecar or a port of its own.

| Target | Built by | Artifact | What it is |
| --- | --- | --- | --- |
| Browser | the server (`web/dist`) | — | the app itself, over HTTP/S |
| Windows / macOS / Linux | `npx tauri build` | `.msi` + NSIS `.exe`, `.app` + `.dmg`, `.deb` + `.AppImage` | client of a server you run |
| Android | `npx tauri android build --apk --debug` | debug-signed APK | client of a server you run |
| iOS | `npx tauri ios build --target aarch64 --no-sign` | unsigned IPA | client of a server you run |

The iOS IPA is distributed as a **SideStore/AltStore source**: add
`https://github.com/dillydalli3r/la-musica/releases/latest/download/source.json`
to the sideloading tool once — the source names the app, its bundle id, its icon
and its size, and its stable URL always resolves to the newest release's IPA
(`tools/make_sidestore_source.py` builds it, and `.github/workflows/release.yml`
fails the release if no IPA was produced to build it from).

Both halves are built locally — `cd web && npm install && npm run build`, then `cd ../desktop && npm install && npx tauri build`. `.github/workflows/desktop.yml` builds the three desktop bundles.

- **Desktop** keeps only the shell furniture: a tray icon (Open la musica / Auto-start on login / Exit), a native folder picker and the window. Quitting it stops nothing but itself.
- **Capabilities are the server's, not the app's**: `GET /api/capabilities` is derived from what the server can actually do (subprocess, ffmpeg/flac/fpcalc/rsgain present), so a client shows what will work rather than offering an Install button that cannot succeed.
- A shell that has never been set up opens **its own wizard** (Server → Account → Notifications → Done); *Test* probes `${address}/api/health` with a 3 s deadline and **Next** stays disabled until a real server answers.
- Both mobile artifacts are **sideload builds, signed by nobody here**: the APK is a debug build, the IPA is built `--no-sign`, and a plain-http server needs the platform's exemption (`desktop/src-tauri/Info.plist`, which also sets `UIBackgroundModes: audio`). `mobile.yml` reads both back out of the built `.app`, so a merge that drops either fails the build. `tools/make_sidestore_source.py` writes the `source.json` the release publishes.
- **Every dialog is a bottom sheet on a phone.** Below 640 px the one `Modal` shell (the only dialog shell the app has) becomes a full-width sheet anchored to the bottom edge, rounded at the top, with the header and footer pinned around a scrolling body; the safe-area insets are the sheet's own padding and the on-screen keyboard is measured from the visualViewport, so the confirm row is never behind it. From 640 px up it is the centred panel the desktop pages were laid out for. Menus, flyouts and popovers are clamped in the viewport by the `Popover` primitive and scroll inside it, on both axes.

## Terminal entry point

```bash
python -m mlo
```

The classic console menu — scripts 1–22, Run All, the config editor and the dependency table. It is **not** stdlib-only: `mlo` imports `mutagen` for every tag operation, so run it from the same environment as the server. A missing module makes its script unavailable and fails loudly instead of reporting a clean "0 processed" run.

## Security & accounts

Everything the API can do — read the library, rewrite tags, move and delete files, start downloads — is one password away from anyone who can reach the port. The top bar's rightmost control is the account menu (change password, sign out everywhere).

| Key | Default | What it does |
| --- | --- | --- |
| `auth_mode` | `auto` | `auto` = gate ON when `server_host` is not loopback; `required` = always; `off` = loopback only |
| `server_host` | `127.0.0.1` | where the server binds — and the address the gate reads |
| `server_port` | `8000` | the port |
| `auth_username` | `""` | the name shown on the login screen |
| `auth_password_hash` | `""` | PBKDF2-HMAC-SHA256, `pbkdf2$<rounds>$<salt-hex>$<hash-hex>` |
| `auth_session_days` | `30` | how long a session stays valid |
| `server_public_url` | `""` | the address clients should dial when it is not the page's own origin |

`auto` follows the bind: `127.0.0.1`, `::1` and `localhost` keep a single-user install password-free; anything else asks for a login — **from clients**, decided per request from the peer address, so the machine running the server never has to sign in while a phone does. `X-Forwarded-For` is never trusted, and `off` on a non-loopback bind is treated as `required`.

- **A login**: PBKDF2-HMAC-SHA256, 600 000 rounds, random salt, constant-time comparison, minimum 8 characters. `auth.db` also holds a `users` table; a session carries its username, and every playlist, like, favourite and trash folder is that user's. Five consecutive failures make an address wait 30 s, doubling to 15 minutes.
- **Sessions** are 32-byte tokens; only their SHA-256 is stored, in `<music>/.mlo/data/auth.db`. The token travels as `Authorization: Bearer`, as the HttpOnly `mlo_session` cookie, or as `?token=` for the WebSockets (which close with code **4401** when it expires).
- `/api/auth/setup` and the static shell answer while `428` (`{"needs_setup": true}`) is in force; every other route refuses until `POST /api/auth/setup` sets the password.
- **First run over the network**: start bound to the address you will use (`server_host: 0.0.0.0`, or `MLO_SERVER_HOST=0.0.0.0` in the container), open the app from a client and claim the server; each client signs in once and keeps its own session.

### The honest limits

- **One password, one user.** No roles, no permissions, no signup — two people sharing a server share the password.
- **The app does not terminate TLS.** Over plain HTTP the token and password travel in the clear. Put it behind a reverse proxy or a mesh VPN when it leaves the LAN, and set `server_public_url`.
- **The gate follows the configured `server_host`, not the socket.** The Docker image seeds that key from `MLO_SERVER_HOST`, so config, bind address and gate cannot disagree there; a hand-started `uvicorn --host` can make them.
- **A closed client is reached only where the platform allows it.** A browser with the push switch on is woken by the server's own Web Push; the desktop and mobile shells have no service worker and notify while they run.
- **The Windows-only tools run in Docker through their runtimes** — CUETools and AudioAuditor on the image's mono runtime, Logchecker on its `php-cli`. `GET /api/capabilities` says what a given server can actually do.
- **An archive inside an archive is not unpacked.** A nested archive stays a file in the tree (it is not audio and not a sidecar), so a doubly-wrapped rip imports nothing until the inner one is unpacked by hand.
- **`.7z` and `.rar` need 7-Zip on the server.** Without it the app says so by name and unpacks nothing; `.zip` and the `tar` family are read without any external tool.
- **Submitting AcoustIDs needs a user API key** (`acoustid_user_key` — the read side only needs the client key), and it publishes to AcoustID's public database, which is why script 22 is opt-in and never in the shipped Run All order.
- **A podcast is a MusicBrainz series, not a release-group type.** MusicBrainz has no "Podcast" release-group type, so an episode's own type stays what MusicBrainz says (usually *Broadcast*) and the app records the series it is `part of`; an episode MusicBrainz has not linked to a series is just a release like any other.
- **A drop on a phone can only send what the phone holds.** A client that cannot read the server's filesystem says so instead of offering a folder path it cannot resolve.

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
mlo/         core engine (imports mutagen for every tag operation): grader, audit,
             flac, images, lyrics (word-sync + the provider chain), lyrics_xlit,
             genres, moods, artistdata, acoustid, cue, accurip, loudness, autotag,
             remux, naming, discs, stats
desktop/     Tauri v2 shell — desktop bundles plus Android/iOS clients
tools/       test-library generator and test suites; docs/ holds the contract
```

Where the app stores what it fetches (all under `<music>/.mlo/`):

| File | What it holds |
| --- | --- |
| `data/config.json`, `data/playlists.db`, `data/auth.db` | all settings; playlists, likes and favourites (per user); sessions and user rows (the token's SHA-256, never the token) |
| `data/artwork.json`, `data/metadata_review.json` | provenance for artist images/descriptions; staged metadata candidates |
| `data/replaygain.json`, `data/audit_evidence.json` | on-demand ReplayGain measurements (invalidated on size/mtime change); what each `AUDIT` verdict was proved on |
| `data/rym_cache/`, `data/lyrics_ai_cache/`, `data/wishes.db`, `data/update_check.json` | RYM genre pages (30 days); AI answers by prompt hash; the wishlist; the last release check (6 h) |
| `downloads/`, `trash/<user>/`, `Artists/<Artist>/artist.jpg` | Soulseek staging; deleted files per user; artist image and description |
| `<album>/description.txt`, `<album>/.mlo_expected.json` | album description; the release's own tracklist (script 15) |

## API overview (selected)

| Endpoint | Purpose |
| --- | --- |
| `GET /api/library` | tag-rich library tree (grades, audits, tags, tech info; gzipped) |
| `GET /api/health` `GET /api/version` | liveness; `{version, latest, update_available, release_url, checked_at, source}` cached 6 h, `latest: null` when GitHub is unreachable |
| `GET /api/auth/status`, `POST /api/auth/setup` `…/login` `…/logout` `…/password` `…/revoke-all`, `GET …/sessions`, `GET/POST /api/auth/users`, `DELETE …/{name}` | the gate's state (`required`, `has_password`, `username`, `host`, `public_url`, `session_days`), sign-in and out, sessions, users |
| `GET /api/library/layout`, `GET /api/library/layout/report` | read-only layout scan (misplaced audio, stray and duplicate sidecars, empty albums, `wrong_case`); the same scan as script 20, plus the stored report |
| `GET /api/storage` | one disk snapshot for the Home card: the volume, the library, the app's own footprint, the bin and the transfer folders — the card reads **three metrics** from it (Library, App, and their total), and every byte count is an int |
| `GET /api/album` `GET /api/artist` `GET /api/artist/artwork`, `GET /api/stream` `GET /api/videos/stream` `…/meta` `…/thumb` | entity details, stored artist image/description and the artist's own grade; audio and video streaming |
| `GET /api/podcasts?series=…` | one podcast series (from the files' own `PODCASTSERIES` tags) and every episode the library holds, newest first — 404 for a series it holds none of |
| `GET /api/script-menu` | every optimization script with the entity kinds it applies from, its Run All slot, its feature switch and its force flag — the details menu's one source (an album's menu = all 22, a track row or playlist = the 11 file-scoped ones) |
| `GET /api/tags` `POST /api/tags/bulk` `…/videos/tag`, `POST /api/run`, `POST /api/organize` | per-track tag view; bulk tag surgery; video tag writes; run scripts 1–22; apply the naming script (dry run supported) |
| `POST /api/export`, `GET /api/export/codecs` `…/drives` `…/defaults` `…/files`, `POST /api/export/cancel` | multi-format export, its codec table, drives, saved defaults, the file families `copy_files` names, and cancel |
| `GET/POST/PATCH/DELETE /api/wishes…`, `POST /api/wishes/{id}/search` `…/search-all` `…/reconcile` `…/import` | the wishlist and its worker |
| `GET /api/sources/health` `…/{id}`, `GET /api/capabilities`, `GET /api/dependencies` | every external source with its `needs`/`configured` state (`?probe=1`); what this server can run; the tool table |
| `GET/POST/DELETE /api/youtube/cookies` | the yt-dlp cookie jar: which mode is on, save a pasted/dropped `cookies.txt`, delete it |
| `GET /api/cookies/{source}` `POST …/{source}/comments` | one per-cookie view per cookie credential (`youtube`, `rym`): domain, path, name, expiry, comment — never a cookie value — and the note the user writes against one cookie |
| `POST /api/mb/match` `…/assign` `…/auto-import` `…/advisory/fetch`, `POST /api/genres/import`, `GET /api/genres/facets` | MusicBrainz matching, tag writes, queued downloads, advisory resolution; genre import and facets |
| `POST /api/import/upload` `…/commit` `…/acoustid` `…/acoustid/submit` `…/finish` `…/bulk`, `POST /api/lyrics/auto` `…/write` `…/embed` `…/wordsync`, `GET /api/lyrics/find` `…/providers` | the import pipeline, its fingerprint step, the chain and the bulk queue; the lyrics chain |
| `POST /api/import/unpack` `…/unpack/discard`, `POST /api/import/source` | unpack a dropped or picked archive into staging (and drop the staging afterwards), and write the answer to "where did this digital release come from" onto every track that lacks a `SOURCE` |
| `POST /api/playlists/import/streaming` | one playlist from Deezer, Spotify, YouTube Music or Apple Music: the matched/unmatched report, the created playlist (`origin`, `origin_url`) and `dry_run` for the dialog's *Check* |
| `GET /api/cover/search` `…/sources`, `POST /api/cover` `…/fromurl` | cover meta-search, upload and save-as-cover |
| `POST /api/soulseek/download-bulk` `…/download-user` `…/search/cancel`, `GET /api/soulseek/ready`, `POST …/import-one` `…/import-all`, `GET/POST …/import-all/status` `…/cancel` | Soulseek downloads, the ready list and the sequential importer |
| `GET /api/soulseek/port-check` | the listen port's own check (a TCP connect, a bind test, whether the host publishes that very port — read from inside a container, with the app's own published port as the control — the router's mapping entry, the address shape, a public-address self-connect, slskd's state) |
| `WS /ws/progress` `WS /ws/events` | live script progress, transfer/job frames and daemon state; every published notification kind, with `?since=` replay |

## Tests & development

```bash
python tools/make_test_library.py   # synthetic library for end-to-end runs
python tools/perf_pipeline.py       # per-script/per-phase wall clock over a scratch library
python tools/test_remux.py          # video remux suite (VOB/MKV/AVI/WebM fixtures)
python tools/test_lyrics_xlit.py    # script 17: alignment, romanization, cache (offline)
python tools/test_script_menus.py   # every script menu agrees (numbers, labels, Run All order)
python tools/smoke_api.py           # route smoke test against a running backend
python tools/check_versions.py      # the release gate: every version string agrees
```

- The 124 `tools/test_*.py` suites are standalone scripts (`python tools/test_x.py`; `sys.exit(2)` means "skipped", e.g. a missing toolchain) and mostly run offline with providers stubbed. The frontend gate is `cd web && npx tsc -b && npx oxlint && npm run build`.
- The browser checks need a running backend serving `web/dist` and Playwright (`npm i -D playwright`): `tools/check_menus.cjs` walks every route (its `sheet`, `pagemenu`, `flyout` and `lyrics` groups), `tools/check_responsive.cjs` re-measures every top-level route — and every dialog's sheet/panel form — at 390×780, 834×1112 and 1440×900, `tools/check_np_metadata_contrast.cjs` samples the fullscreen player's rendered pixels over four covers, and `tools/check_ui.cjs` drives the client-side flows. Two checkers need no backend: `node tools/check_accent.mjs` (the accent maths against `tools/fixtures/accent.json`) and `node tools/check_script_menu.mjs` (the generated details menu). A third does its own: `node tools/check_library_az.mjs` stands `tools/fixtures/library-az.json` up as the API itself — the Library's name box and A–Z rail across all five views, and the album page's wrapped recommendation shelf, measured at 1440 and 390 px.
- `tools/check_versions.py` compares the app version in `mlo/__init__.py`, `tauri.conf.json`, `Cargo.toml`, both `package.json`s, the Dockerfile's `MLO_VERSION`, this README's header and `desktop/README.md`. CI runs the Python suites and the web build.

## Licence, credits and donations

MIT — see [`LICENSE`](LICENSE). The services and projects the app is built on are in the app's bottom-left credits popover, in [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) and in `web/public/credits.json`. `/donations` carries two addresses (a photograph of the maintainer's cat with one line under it), and **nothing is gated behind it**.

Legacy v1 (the Tkinter app, CLI and PyInstaller/Inno packaging) is archived on the `archive/legacy-v1.7` branch.
