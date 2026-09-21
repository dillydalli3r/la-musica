# Third-party notices — la musica

la musica is MIT-licensed (see `LICENSE`). It is only possible because of the
following projects. This file credits them and their licenses, as their
licenses require. Each entry lists the project, what la musica uses it for,
and its license.

None of these are shipped in this repository: the app downloads them on demand
into `.dependencies/`, and `mlo/fetchdeps.py` copies each archive's
`LICENSE` / `COPYING` / `NOTICE` / `README` next to the installed binaries, so
the licence text travels with the tool (GPL-2.0 §1 and LGPL-2.1 §1 ask for
exactly that).

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
  and verification helpers. **GPL-2.0-or-later** (the GPL v2-or-later program
  header in the bundled `License.txt`). It bundles further components, whose
  licences that same file reproduces: **hdcd.dll** (MIT-style, HDCD is a
  registered trademark of Microsoft), **unrar.dll** (freeware, © Alexander
  Roshal — may be used freely to handle RAR archives), **unrar.cs** wrapper
  (Schematrix / Michael A. McCloskey), **ALACDotNet.cs** (MIT-style, © David
  Hammerton), **libFLAC** (BSD-3-Clause), **MAC_SDK** (Monkey's Audio SDK
  licence agreement, © Matthew T. Ashland), **libwavpack** (BSD-3-Clause),
  **FFmpeg.AutoGen** (**LGPL-3.0**). The release also ships **TagLibSharp.dll**
  (**LGPL-2.1**) and **Newtonsoft.Json.dll** (**MIT**), and the project's own
  `DeviceId.dll`, `Freedb.dll` and `ProgressODoom.dll`, which carry no separate
  notice and are covered by the CUETools licence above.
- **Logchecker** — <https://github.com/OPSnet/Logchecker> — rip log grading
  (EAC/XLD checksums and scores). **MIT**.
- **AudioAuditor** — <https://github.com/Angel2mp3/AudioAuditor> — audio
  authenticity auditing. **GPL-3.0**.
- **rsgain** — <https://github.com/complexlogic/rsgain> — ReplayGain 2.0
  tagging. **BSD-2-Clause**.
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
| [mutagen](https://github.com/quodlibet/mutagen) | GPL-2.0-or-later | audio tag read/write (imported in-process — see below) |
| [Pillow](https://github.com/python-pillow/Pillow) | HPND (MIT-CMU) | image processing |
| [websockets](https://github.com/python-websockets/websockets) | BSD-3-Clause | progress relay |
| [python-multipart](https://github.com/kludex/python-multipart) | Apache-2.0 | uploads |
| [aiofiles](https://github.com/Tinche/aiofiles) | Apache-2.0 | async file IO |
| [librosa](https://github.com/librosa/librosa) (+ numpy/scipy) | ISC | BPM/key analysis |

### In-process copyleft: mutagen and Unidecode

**mutagen (GPL-2.0-or-later)** is imported *in-process* by la musica — `mlo/deps.py`
reads and writes the tags of every processed file — and is additionally vendored
inside the beets tree. la musica itself is MIT, but GPL-2.0-or-later code linked into
a program makes the distributed bundle a combined work: shipping it requires the
GPL-2.0 text and a written offer for the corresponding source, and that code (and
the work it is part of) has to stay under the GPL when redistributed. The GPL text
ships in the installed mutagen package (`COPYING`/`LICENSE`, copied into
`.dependencies/` by the installer); anyone redistributing a bundle containing it
has to pass that on. A bundle that cannot be GPL must replace mutagen with a
permissive tag library.

**Unidecode (GPL-2.0-or-later)**, pulled in by the vendored beets tree, carries
the same requirement for the beets bundle.

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
| [Tauri API](https://github.com/tauri-apps/tauri) | MIT / Apache-2.0 (lock spells it `Apache-2.0 OR MIT`) | desktop shell |
| [@tauri-apps/plugin-notification](https://github.com/tauri-apps/plugins-workspace) | MIT OR Apache-2.0 | OS notifications on the desktop and mobile shells (the web build loads it dynamically, only inside a Tauri webview) |

## Bundled dependency trees

beets and librosa are installed with `pip install --target`, so their whole
dependency tree lands in `.dependencies/`. Those transitive packages are listed
here with the licence each one declares (`License-Expression` in its
`.dist-info/METADATA`):

### beets tree

| Project | License |
| --- | --- |
| beets | MIT |
| colorama | BSD-3-Clause |
| confuse, filetype, jellyfish, mediafile, platformdirs, PyYAML | MIT |
| lap, musicbrainzngs | BSD-2-Clause |
| numpy | BSD-3-Clause (bundles 0BSD, MIT, Zlib and CC0-1.0 parts) |
| mutagen | **GPL-2.0-or-later** |
| Unidecode | **GPL-2.0-or-later** |
| typing_extensions | PSF-2.0 |

### librosa tree

| Project | License |
| --- | --- |
| librosa | ISC |
| audioread, charset-normalizer, narwhals, urllib3 | MIT |
| cffi | MIT-0 |
| cloudpickle, idna, joblib, lazy-loader, pooch, pycparser, scikit-learn, scipy, soundfile, threadpoolctl | BSD-3-Clause |
| decorator, llvmlite, numba | BSD-2-Clause (llvmlite also Apache-2.0 WITH LLVM-exception) |
| numpy | BSD-3-Clause (bundles 0BSD, MIT, Zlib and CC0-1.0 parts) |
| msgpack, requests | Apache-2.0 |
| packaging | Apache-2.0 OR BSD-2-Clause |
| certifi | MPL-2.0 |
| **soxr** | **LGPL-2.1-or-later** — the native libsoxr resampler it ships |
| typing_extensions, standard-aifc, standard-chunk, standard-sunau, audioop-lts | PSF-2.0 |

## Desktop shell (Rust crates)

The Tauri shell in `desktop/src-tauri` compiles a locked crate graph
(`desktop/src-tauri/Cargo.lock`, 500 package entries — 499 registry crates
plus the `mlo-desktop` root package). Direct dependencies:

| Crate | License |
| --- | --- |
| tauri, tauri-build, tauri-plugin-dialog, tauri-plugin-autostart, tauri-plugin-notification | MIT OR Apache-2.0 (tauri-plugin-notification declares `Apache-2.0 OR MIT`) |
| serde, serde_json | MIT OR Apache-2.0 |

**What the notification plugin pulls in** (new in 3.0.0). `tauri-plugin-notification`
2.4.0 is what raises the OS notifications on every target, and it is the only
part of the lock that exists for that job: **notify-rust** 4.18.0 (MIT OR
Apache-2.0) with **mac-notification-sys** 0.6.15 on macOS and
**tauri-winrt-notification** 0.7.3 (MIT OR Apache-2.0) with the `windows` /
`windows-version` crates on Windows, plus **zbus** / **zvariant** (and their
`async-*`, `enumflags2`, `ordered-stream`, `uds_windows`, `endi` … helpers) for
the Linux desktop-bus path. It also depends on `serde_repr`, `time` and `url`,
which the rest of the graph already carried. `tauri-plugin-autostart` brings
**auto-launch** 0.5.0 (MIT) with `dirs`/`winreg`, and `tauri-plugin-dialog`
brings **tauri-plugin-fs**, both also already part of the shipped shell.

The rest of the graph was read from the licence each crate declares in its
registry manifest. The 432 locked crates present in this checkout's cargo cache
(500 entries total) resolve to:

| License(s) | Crates |
| --- | --- |
| MIT OR Apache-2.0 (and the equivalent spellings) | 281 |
| MIT | 78 |
| Unicode-3.0 | 18 |
| Zlib OR Apache-2.0 OR MIT (both orderings) | 18 |
| Unlicense OR MIT (one crate spells it `Unlicense/MIT`) | 11 |
| BSD-2-Clause / BSD-3-Clause (some additionally OR MIT/Apache-2.0) | 10 |
| **MPL-2.0** (file-level copyleft, unmodified) | 5 — cssparser, cssparser-macros, dtoa-short, option-ext, selectors |
| Apache-2.0 WITH LLVM-exception OR MIT/Apache-2.0 | 4 |
| (MIT OR Apache-2.0) AND Unicode-3.0 | 1 — unicode-ident |
| 0BSD or CC0-1.0 OR MIT-0 OR Apache-2.0 | 2 |
| ISC | 1 — libloading |
| Zlib | 1 — foldhash |
| Apache-2.0 | 2 — sync_wrapper, tao |

No GPL or AGPL crate is anywhere in the graph, and no crate in the cache
declares no licence at all. The remaining lock entries (68 of the 500) are the
platform bindings for targets this build does not use (gtk/atk/cairo/dbus/
gdk-pixbuf on Linux, core-*/objc2 on macOS, `r-efi` for UEFI targets, older
duplicate versions, build-only crates); they are not part of the shipped
Windows shell, and each publishes its own licence inside its crate archive —
`cargo metadata` over `Cargo.lock` lists them exactly. `r-efi` (the one entry
in the wider graph that offers `LGPL-2.1-or-later`) is used under its MIT/
Apache-2.0 option.

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
- **Cover Art Archive** — <https://coverartarchive.org> — release cover art
  (data CC-BY-SA / CC0, served by MetaBrainz).
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
- **Discogs** — <https://www.discogs.com/developers> — genre and release
  lookups (requires your own API token). **Last.fm** —
  <https://www.last.fm/api> — genre and tag lookups (own API key).
- **Spotify** — <https://developer.spotify.com> — catalogue search (requires
  your own client credentials; optional). **Bandcamp** —
  <https://bandcamp.com> — release links.
- **covers.musichoarders.xyz** — <https://covers.musichoarders.xyz> — the
  community cover search the app meta-searches.
- **Soulseek** — <https://www.slsknet.org> — the file-sharing network this app
  searches through slskd. Soulseek™ is a trademark of Soulseek LLC; this
  project is not affiliated with it or with slskd.
- **NetEase Cloud Music** (<https://music.163.com>), **QQ Music**
  (<https://y.qq.com>), **Kugou** (<https://www.kugou.com>), **Kuwo**
  (<https://www.kuwo.cn>) — additional lyrics sources in the provider chain;
  lyrics remain the property of their respective rights holders.

These same credits, with each entry's licence, are rendered inside the app
(`web/public/credits.json` — the bottom-left corner of the window and Settings
→ *Credits*), so the list is visible where it is used and not only here.

If you believe a credit is missing or wrong, please open an issue.
