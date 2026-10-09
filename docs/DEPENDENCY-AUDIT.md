# Dependency audit — la musica 4.9.3

Scope: every dependency the **shipped** app pulls in — the Python runtime
(`server/requirements.txt`), the two npm packages (`web/`, `desktop/`), the
Tauri Rust crates (`desktop/src-tauri/Cargo.toml`), the vendored native/pip
tools (`mlo/fetchdeps.py`, installed under `<music>/.mlo/tools`), and the apt
packages the container installs. Method: read the manifests, then follow every
import/call site; a dependency is "load-bearing" only where a call site exists.

This is a record, not a wish list. "Action" says what happened in the change
that added this file; the rest is staged and the reason is stated.

## 1. Python runtime

| Package | Ver | Load-bearing at | Verdict |
|---|---|---|---|
| fastapi, uvicorn | 0.141.1 / 0.52.4 | 35 routers; container CMD | **Keep** |
| websockets | 17.1 | uvicorn's WS backend (no direct import) | **Keep** |
| python-multipart | 0.0.32 | `File`/`Form` routes | **Keep** (FastAPI requires it) |
| ~~aiofiles~~ | 25.1.0 | **nowhere** | **REMOVED** — dead wheel |
| httpx | 0.28.1 | the one shared provider client (`httpclient.py`) | **Keep** (measured −31 % run) |
| mutagen | 1.48.1 | the whole tag layer (`mlo/audio.py`, `containers.py`, `flac.py`) | **Keep** (Rust `lofty` is the port target) |
| Pillow | 12.3.0 | cover/artist decode-crop-resize (7 modules) | **Keep** (Rust `image` is the port target) |
| numpy | 2.5.2 | `mlo/dr.py` (block math), `audiometa.py`/`moods.py` (DSP) | **Keep, but no longer required for DR** |
| threadpoolctl | 3.6.0 | `mlo/stats.py` — caps BLAS/OpenMP pools | **Keep** (follows numpy) |
| cryptography | 50.0.1 | Web Push (P-256 + AES-GCM) | **Keep** (stdlib cannot) |
| eac-logchecker | 0.8.1 | `mlo/discs.py` — EAC log checksum | **Keep** (swap staged; needs the reference to pin parity) |

Transitive weight worth knowing: `numpy` ships a 19.5 MB OpenBLAS DLL and its
own thread pools (why `threadpoolctl` exists); `cryptography` is a native
extension; the **vendored** `librosa 0.11.0` tree drags scipy, scikit-learn,
numba + llvmlite (LLVM), soundfile and cffi; the **vendored** `beets 2.4.0`
tree drags numpy, lap, jellyfish, musicbrainzngs and friends.

Undeclared/vestigial: `tqdm` (optional import in `mlo/deps.py`; the server
nulls it in `server/ws.py`) — left as an optional CLI nicety, not declared.
`pystray`/PIL in `dev.py` — dev tray only.

## 2. Vendored native / pip tools

All are launched only through `mlo/subproc.run_tool`, discovered by
`mlo/tools.py`, installed under `<music>/.mlo/tools`.

| Tool | Kind | Invoked from | Rust replaceable? |
|---|---|---|---|
| flac, metaflac | C | `mlo/flac.py` | No |
| ffmpeg, ffprobe | C | ~12 modules (decode/encode/probe) | No |
| libjxl (cjxl/djxl), libjpeg-turbo (jpegtran) | C++ / C | `mlo/images.py` | No |
| **oxipng** | **Rust** | `mlo/images.py` | **already Rust** |
| rsgain | C++ | `mlo/loudness.py` | No (spec pins its numbers) |
| AudioAuditor | .NET | `mlo/audit.py` | No |
| CUETools (mono), Logchecker (php) | .NET / PHP | `mlo/accurip.py`, `mlo/discs.py` | Not without a port |
| chromaprint (fpcalc) | C++ | `mlo/acoustid.py` | No |
| beets, yt-dlp | Python (vendored) | `server/beetscfg.py`, `mlo/lyrics_providers.py`, `server/youtube.py` | **No equivalent upstream** |
| librosa | Python (vendored) | `mlo/moods.py`, `mlo/audiometa.py` | No drop-in; spec pins its numbers |
| eac-logchecker | Python | `mlo/discs.py` | Yes, small — staged |
| simple-dr-meter | Python (vendored) | **test oracle only** | Kept: it is the independent reference the DR parity test measures against |

## 3. npm

`web/`: react/react-dom, react-router-dom, @tanstack/react-query, zustand,
lucide-react, @tauri-apps/api + plugin-notification (dynamically imported in the
shell). Dev: vite, typescript, oxlint, tailwindcss 3, postcss, autoprefixer,
@vitejs/plugin-react. All used. Postcss + autoprefixer exist only for Tailwind
3; Tailwind 4's Vite plugin removes both (staged — theme migration).

`desktop/`: **only `@tauri-apps/cli` is used** (it is the `tauri` script). The
three runtime deps (`@tauri-apps/api`, plugin-dialog, plugin-notification) were
declared but the desktop has no JS of its own (`tauri.conf.json` points
`frontendDist` at `web/dist`). **REMOVED**, and their `package-lock.json`
entries with them.

Lockfile drift: `web/package-lock.json` root `version` said 3.11.0 while
`package.json` said 4.9.3. **Fixed.**

## 4. Rust (Tauri shell)

tauri 2 (`tray-icon`, `image-png`), tauri-build, tauri-plugin-dialog /
-autostart / -notification / -single-instance, parking_lot, and iOS-only
objc2 / block2 / objc2-foundation. ~500 transitive crates (wry, tao,
gtk/webkit on Linux). Nothing removable — this is a Tauri v2 shell, and the
iOS crates are `cfg`-gated so they compile only there. `-single-instance` is
the one that is not about a feature the user asks for but about a hazard: two
shells would mean two local backends over one library.

## 5. Container (apt)

ffmpeg, flac, libjxl-tools, libjpeg-turbo-progs, libchromaprint-tools, rsgain,
php-cli, mono-runtime, libgdiplus, libmono-system-drawing4.0-cil, libsndfile1,
libgomp1, libicu76, ca-certificates. `libsndfile1`/`libgomp1` exist for the
vendored librosa (soundfile + numba); they leave when the DSP does.

## 6. Where the time actually goes (measured, not guessed)

`tools/perf_after_local.json` (12 albums × 5 tracks × 2 s, local chain,
20.09 s):

| Pass | Seconds | Share |
|---|---|---|
| 16 Mood & Energy (librosa) | 8.95 | 44.6 % |
| 6 Audit library (AudioAuditor CLI) | 3.21 | 16.0 % |
| 12 Key & BPM (librosa) | 2.39 | 11.9 % |
| 7 DR & ReplayGain (rsgain + block math) | 2.20 | 10.9 % |
| everything else (subprocess + orchestration) | 3.34 | 16.6 % |

`tools/perf_http_*.json` isolate a pure network effect: sharing one
`httpx.Client` took script 8 from 106.5 s to 73.1 s (−31 %).

The lesson for "rewrite the scripts in Rust": the native tools are already
C/C++/Rust binaries driven by subprocess, and the network is the network. The
Python-bound CPU is the **DSP** (moods/key/DR) and the **per-file double work**
(each container is re-parsed by mutagen ~14–16× and re-decoded 4–6× per run).
Those are the targets; a Rust orchestrator around the same subprocesses would
change nothing.

## 7. Actions in this change

1. `aiofiles` removed from `server/requirements.txt` (no importer).
2. The three unused `desktop/package.json` deps removed, lockfile updated.
3. `web/package-lock.json` version drift fixed.
4. **`mlo-audio`**: a zero-dependency Rust crate (`rust/`) whose first pass is
   the DR block math — it spawns the app's ffmpeg and reads the PCM itself, so
   the decoded track never travels into Python. `mlo/dr.py` prefers it and
   falls back to numpy; the DR suite pins their integer parity on stereo, mono,
   96 kHz, silent, single-block and undecodable fixtures, and proves the
   numpy-less case now measures. Built in a Docker stage and copied onto PATH.
5. `mlo/loudness.py`'s DR gate accepts either engine, and says which one ran.

## 8. Staged, with the reason

| Item | Why not now |
|---|---|
| Port `moods.py` / `audiometa.py` (the 56 % DSP slice) to Rust | Large DSP: chroma-CQT, HPSS, mel/onset, tempo autocorrelation. The spec pins the numbers these write (MOOD/ENERGY/INITIALKEY/BPM), so it needs a parity oracle per feature, not a rewrite. This is the next stage, and `mlo-audio` is its home. |
| Fuse the per-run re-reads/re-decodes | An engine-level change (one parse and one decode per file per run) across many passes; independent of language. |
| mutagen → `lofty`, Pillow → `image`, httpx → `reqwest` | Each rewrites a whole layer (tagging, media ops, provider client) and its tests; the deps are healthy today. |
| eac-logchecker → in-app checksum | Needs the reference implementation installed to pin parity (it is not, on a dev box). |
| Tailwind 4 (drops postcss + autoprefixer) | Theme migration across the UI; a frontend change, not a dependency one. |
| FastAPI/uvicorn → axum, or granian | The largest possible change (35 routers, every API test); no measured need. |
