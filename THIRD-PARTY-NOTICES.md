# Third-party notices — la musica

la musica is MIT-licensed (see `LICENSE`). It is only possible because of the
following projects. This file credits them and their licenses, as their
licenses require. Each entry lists the project, what la musica uses it for,
and its license.

- **slskd** — <https://github.com/slskd/slskd> — the Soulseek™ client daemon
  (searching, downloading, sharing). **AGPL-3.0**.
- **beets** — <https://beets.io> — MusicBrainz-tagged imports with
  Picard-parity tagging. **MIT**.
- **FFmpeg** — <https://ffmpeg.org> (BtbN GPL builds) — media remuxing,
  transcoding, decode verification. **LGPL-2.1-or-later / GPL-2.0-or-later**
  depending on build flags (the bundled builds are GPL); numerous
  contributor licenses apply.
- **flac (libFLAC + tools)** — <https://github.com/xiph/flac> — FLAC
  encode/decode/verify. **BSD-3-Clause** (libFLAC), **GPL-2.0-or-later**
  (command-line tools).
- **CUETools** — <https://github.com/gchudov/cuetools.net> — CUE handling
  and verification helpers. **LGPL-2.1** (some components GPL).
- **Logchecker** — <https://github.com/OPSnet/Logchecker> — rip log grading
  (EAC/XLD checksums and scores). **MIT**.
- **AudioAuditor** — <https://github.com/Angel2mp3/AudioAuditor> — audio
  authenticity auditing. **GPL-3.0**.
- **rsgain** — <https://github.com/complexlogic/rsgain> — ReplayGain 2.0
  tagging. **BSD-2-Clause**.
- **simple-dr-meter** — <https://github.com/magicgoose/simple-dr-meter> —
  dynamic range analysis. See the project repository for licensing.
- **PHP** — <https://www.php.net> — runs the Logchecker phar. **PHP License
  v3.01**.
- **libjxl (JPEG XL)** — <https://github.com/libjxl/libjxl> — JPEG XL cover
  art support. **BSD-3-Clause** (parts Apache-2.0).
- **libjpeg-turbo** — <https://github.com/libjpeg-turbo/libjpeg-turbo> —
  JPEG image processing. **BSD-3-Clause / IJG / zlib**.
- **oxipng** — <https://github.com/oxipng/oxipng> — lossless PNG
  optimization. **MIT**.
- **librosa** — <https://librosa.org> — BPM / key analysis and mood
  classification. **ISC**.
- **Chromaprint / fpcalc** — <https://github.com/acoustid/chromaprint> —
  audio fingerprinting for AcoustID release matching during import.
  **LGPL-2.1-or-later**.

## Python packages

| Project | License | Use |
| --- | --- | --- |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | HTTP API |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | HTTP server |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | HTTP client |
| [mutagen](https://github.com/quodlibet/mutagen) | GPL-2.0-or-later | audio tag read/write |
| [Pillow](https://github.com/python-pillow/Pillow) | HPND (MIT-CMU) | image processing |
| [websockets](https://github.com/python-websockets/websockets) | BSD-3-Clause | progress relay |
| [python-multipart](https://github.com/kludex/python-multipart) | Apache-2.0 | uploads |
| [aiofiles](https://github.com/Tinche/aiofiles) | Apache-2.0 | async file IO |
| [pystray](https://github.com/moses-palmer/pystray) | LGPL-3.0 | system tray |
| [librosa](https://github.com/librosa/librosa) (+ numpy/scipy) | ISC | BPM/key analysis |

## Frontend packages

| Project | License | Use |
| --- | --- | --- |
| [React](https://react.dev) | MIT | UI framework |
| [React Router](https://github.com/remix-run/react-router) | MIT | navigation |
| [TanStack Query](https://github.com/TanStack/query) | MIT | data fetching |
| [Zustand](https://github.com/pmndrs/zustand) | MIT | state |
| [lucide-react](https://github.com/lucide-icons/lucide) | ISC | icons |
| [Tailwind CSS](https://github.com/tailwindlabs/tailwindcss) | MIT | styling |
| [Vite](https://github.com/vitejs/vite) | MIT | build tool |
| [TypeScript](https://github.com/microsoft/TypeScript) | Apache-2.0 | language |
| [Tauri API](https://github.com/tauri-apps/tauri) | MIT / Apache-2.0 | desktop shell |

## Fonts

- **Inter** — <https://rsms.me/inter/> — **SIL Open Font License 1.1**.

## Also

- **MusicBrainz** — <https://musicbrainz.org> — metadata (data licensed
  CC-BY-SA / CC0). **LRCLIB** — <https://lrclib.net> — synced lyrics.
  Soulseek™ is a trademark of Soulseek LLC; this project is not affiliated
  with it or with slskd.

External services queried by the discovery provider layer (no SDK or code of
theirs is bundled — results are fetched over their public HTTP APIs and cached
locally):

- **ListenBrainz** — <https://listenbrainz.org> (MetaBrainz) — sitewide
  listening charts. Data **CC0 / CC-BY-SA**.
- **Deezer** — <https://developers.deezer.com/api> — catalogue search,
  similarity and artist imagery.
- **iTunes Search API** — <https://performance-partners.apple.com/search-api> —
  catalogue fallback and artwork. Apple and iTunes are trademarks of Apple Inc.
- **TheAudioDB** — <https://www.theaudiodb.com> — artist biographies and
  press images.
- **Wikipedia / Wikidata** — <https://www.wikipedia.org> — artist and album
  descriptions. Text is **CC BY-SA 4.0**; the app stores an extract plus a
  link back to the source article.
- **AcoustID** — <https://acoustid.org> — fingerprint lookups (requires your
  own free application key; the service is non-commercial use only).
- **NetEase Cloud Music**, **Kugou**, **lyrics.ovh** — additional lyrics
  sources in the provider chain; lyrics remain the property of their
  respective rights holders.

If you believe a credit is missing or wrong, please open an issue.
