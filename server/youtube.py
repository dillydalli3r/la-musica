"""YouTube music-video acquisition (yt-dlp).

Used through yt-dlp's Python API when `yt_dlp` is importable (a normal
install, or the vendored `.dependencies/yt-dlp vX` folder); otherwise the
pinned yt-dlp binary in .dependencies is driven through run_tool() with the
same flags. One quality policy for both paths:

* ``bv*+ba/b`` — best video-only stream + best audio-only stream, merged into
  MKV. Never ``best``/``bestvideo``: a single-stream selector can silently
  return the 360p progressive variant when it ranks first, and it never picks
  the separate high-resolution video stream.
* ``-S vbr:max,abr:max,res:max`` — highest video bitrate, then highest audio
  bitrate, then highest resolution.
* ``youtube_max_height`` (0 = best) is the only way a lower resolution is
  picked, and it is an explicit user setting.

Merging is done with the app's own ffmpeg (``.dependencies``), never with
whatever build happens to be on PATH: an older PATH ffmpeg mis-detects yt-dlp's
HLS fragments (MPEG-TS content named .mp4) and the merge of an otherwise
perfect download fails.

Search uses yt-dlp's public ``ytsearch`` (no API key, no scraping of private
endpoints): the official / ``- Topic`` artist channel is preferred, and
lyric / cover / tribute / karaoke re-uploads are rejected. When the track's
length is known it is a hard ±5 s filter — that is what makes the rejection
safe, since only a length match proves the upload is the song itself.
"""

import json
import os
import re
import sys

from mlo.subproc import run_tool
from mlo.tools import detect_all_tools, python_pkg_path

# Highest-quality format policy (see module docstring). The selector is a
# constant so no caller can pass something weaker in.
FORMAT_SELECTOR = "bv*+ba/b"
FORMAT_SORT = "vbr:max,abr:max,res:max"
OUTTMPL = "%(title).120B [%(id)s].%(ext)s"
SEARCH_RESULTS = 10
DURATION_TOLERANCE = 5.0
_DOWNLOAD_TIMEOUT = 4 * 60 * 60
_PROBE_TIMEOUT = 5 * 60

# Cookies: yt-dlp's two paths (the importable module and the vendored binary)
# must honour the SAME setting, so the option is built here once and read by
# both. A cookie jar is a credential — YouTube answers "Sign in to confirm
# your age" and throttles an anonymous IP — and the only jar the app ever
# reads is its own (`<music>/.mlo/data/cookies.txt`, written by Settings →
# Videos through server/api_youtube.py): a user-typed path would go stale the
# moment the library moved, and nothing else in the app writes one.
# `COOKIES_BROWSERS` is also what mlo/config.py validates the setting against
# and what the settings field lists, so the three cannot drift apart.
COOKIES_MODES = ("none", "file", "browser")
COOKIES_BROWSERS = ("chrome", "chromium", "edge", "firefox", "brave", "opera",
                    "safari", "vivaldi", "whale")

def cookies_path():
    """The one cookie jar the app owns: <music>/.mlo/data/cookies.txt.

    Imported lazily: mlo.paths resolves the music folder from the config, and
    server/youtube.py is imported by modules that only want the constants.
    """
    from mlo.paths import app_data_dir
    return os.path.join(app_data_dir(), "cookies.txt")

def cookies_mode(config):
    """The configured mode, always one of COOKIES_MODES."""
    mode = str((config or {}).get("youtube_cookies_mode", "none") or "none")
    mode = mode.strip().lower()
    return mode if mode in COOKIES_MODES else "none"

def cookies_browser(config):
    """The configured browser, always one of COOKIES_BROWSERS."""
    name = str((config or {}).get("youtube_cookies_browser", "chrome") or "")
    name = name.strip().lower()
    return name if name in COOKIES_BROWSERS else "chrome"

def cookie_opts(config):
    """yt-dlp Python-API cookie options for *config*; {} when off.

    `none` adds NOTHING (never a null option yt-dlp would treat as a value),
    and a mode whose input is not there yet adds nothing either: a `file` mode
    with no jar would otherwise fail every download with yt-dlp's own "does
    not exist" instead of just running without cookies.
    """
    mode = cookies_mode(config)
    if mode == "file":
        path = cookies_path()
        return {"cookiefile": path} if os.path.isfile(path) else {}
    if mode == "browser":
        # A 1-tuple is yt-dlp's documented short form: the profile, keyring
        # and container are optional, so the app never has to guess a profile.
        return {"cookiesfrombrowser": (cookies_browser(config),)}
    return {}

def cookie_args(config):
    """The binary's cookie flags for *config*; [] when off (see cookie_opts)."""
    mode = cookies_mode(config)
    if mode == "file":
        path = cookies_path()
        return ["--cookies", path] if os.path.isfile(path) else []
    if mode == "browser":
        return ["--cookies-from-browser", cookies_browser(config)]
    return []

# Channel names that mean "this is the artist's own upload".
_OFFICIAL_HINTS = re.compile(r"\b(topic|vevo|official)\b", re.IGNORECASE)
# Re-uploads that are not the release: the audio is either the same master
# with a still frame (lyric video) or a different recording entirely.
_REJECT_RX = re.compile(
    r"\b(lyrics?|lyric video|cover|covered|tribute|karaoke|reaction|"
    r"nightcore|slowed|sped up|8d audio)\b",
    re.IGNORECASE,
)

_ytdlp_module = None
_ytdlp_tried = False


# ----------------------------------------------------------------------
# Availability
# ----------------------------------------------------------------------
def _load_ytdlp():
    """The ``yt_dlp`` module, from site-packages or the vendored pip folder.

    Imported once; a missing module is remembered so availability checks stay
    free. The vendored folder is prepended to sys.path, like the other
    vendored pip packages (see mlo.tools.python_pkg_path).
    """
    global _ytdlp_module, _ytdlp_tried
    if _ytdlp_tried:
        return _ytdlp_module
    _ytdlp_tried = True
    yt_dlp = None
    try:
        import yt_dlp  # noqa: A004
    except ImportError:
        folder = python_pkg_path("yt-dlp")
        if folder:
            sys.path.insert(0, folder)
            try:
                import yt_dlp  # noqa: A004
            except ImportError:
                yt_dlp = None
    _ytdlp_module = yt_dlp
    return _ytdlp_module


def _binary_exe():
    """Vendored / PATH yt-dlp executable, or None."""
    return (detect_all_tools().get("yt-dlp") or {}).get("ytdlp_exe")


def _ffmpeg_location():
    """Directory yt-dlp must use for merging.

    Never left to yt-dlp's PATH lookup: the app ships its own ffmpeg (which is
    the build every other video path uses and the only one a fresh install
    has), and a system build can differ in ways that break the merge — an
    older ffmpeg mis-detects an HLS fragment named .mp4 as MP4 ("moov atom not
    found") and the merge of an otherwise perfect download fails.
    """
    ffmpeg = (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")
    return os.path.dirname(ffmpeg) if ffmpeg else None


def ytdlp_available(config=None):
    """True when a usable yt-dlp is present (Python module or binary)."""
    if _load_ytdlp() is not None:
        return True
    exe = _binary_exe()
    return bool(exe and os.path.isfile(exe))


def enabled(config):
    """Whether YouTube downloads are on (Settings → Videos).

    The acquisition branch (server.soulseek_auto's YouTube route) asks this
    BEFORE it searches anything: with the switch off, `best_candidate` returns
    None for every track, and a caller that cannot see the switch would report
    "not found on YouTube" about a release it was never allowed to look for.
    """
    return bool((config or {}).get("youtube_enabled", True))


def _format_selector(config):
    """Highest-quality selector; only ``youtube_max_height`` caps it."""
    try:
        cap = int((config or {}).get("youtube_max_height", 0) or 0)
    except (TypeError, ValueError):
        cap = 0
    if cap > 0:
        # The documented yt-dlp idiom: the cap applies to the video side
        # (audio-only streams have no height) and to the combined fallback.
        return f"bv*[height<={cap}]+ba/b[height<={cap}]"
    return FORMAT_SELECTOR


def _ydl_opts(config, outtmpl=None, flat=False):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "format": _format_selector(config),
        "format_sort": FORMAT_SORT.split(","),
        "merge_output_format": "mkv",
        "socket_timeout": 30,
        "retries": 3,
        "extractor_retries": 2,
    }
    if flat:
        opts.update({
            "skip_download": True,
            "extract_flat": "in_playlist",
            "ignoreerrors": True,
        })
    if outtmpl:
        opts["outtmpl"] = outtmpl
    location = _ffmpeg_location()
    if location:
        opts["ffmpeg_location"] = location
    opts.update(cookie_opts(config))
    return opts


def _json_line(text):
    """Last JSON object in yt-dlp's output (progress lines are disabled, so
    normally the whole stream; older builds print one object per video)."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def _watch_url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else None


def _achieved(info):
    """(height, abr, format_id) that will actually be downloaded.

    Read from the formats yt-dlp requested (the merged video+audio pair), not
    from the top-level fields — those describe the best *available* stream,
    which is not necessarily the one selected.
    """
    fmts = info.get("requested_formats") or [info]
    heights = [f.get("height") for f in fmts if f.get("height")]
    abrs = [f.get("abr") or f.get("vbr") for f in fmts
            if f.get("acodec") not in (None, "none")]
    height = max(heights) if heights else info.get("height")
    abr = max((a for a in abrs if a), default=None) or info.get("abr")
    return height, abr, info.get("format_id")


def _probe_media(path):
    """(height, abr) read from the downloaded file — fallback for builds whose
    JSON report carries no stream detail."""
    from mlo.remux import _ffprobe_json
    ffprobe = (detect_all_tools().get("ffmpeg") or {}).get("ffprobe_exe")
    if not ffprobe:
        return None, None
    data = _ffprobe_json(ffprobe, path)
    if not data:
        return None, None
    height, abr = None, None
    for st in data.get("streams") or []:
        if st.get("codec_type") == "video" and st.get("height"):
            height = max(height or 0, int(st["height"])) or None
        elif st.get("codec_type") == "audio":
            try:
                abr = max(abr or 0, float(st.get("bit_rate") or 0) / 1000) or None
            except (TypeError, ValueError):
                pass
    return height, abr


# ----------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------
def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _mentions(haystack, needle):
    """Normalized containment; also matches run-together channel names
    ("RickAstleyVEVO" for artist "Rick Astley")."""
    hay, need = _norm(haystack), _norm(needle)
    if not hay or not need:
        return False
    return need in hay or need.replace(" ", "") in hay.replace(" ", "")


def _flat_info(url, config, playlist=False):
    """Flat (no download, no per-entry page) yt-dlp report → `(info, error)`.

    The ONE flat probe. The track search below reads its results with these
    flags and this module's two paths (the importable `yt_dlp` module, else the
    vendored binary driven through ``run_tool`` with the same flags), and
    ``flat_playlist`` reads a playlist with the same ones — so a search hit and
    a playlist entry can never be read differently. `playlist` turns yt-dlp's
    `noplaylist` off, which is the whole difference: a playlist URL expands
    into its entries instead of being treated as the one video it points at,
    and `--yes-playlist`/`noplaylist: False` are the module's and the binary's
    spelling of that same flag.

    `error` is yt-dlp's own last words ("" when it answered), so a caller that
    must report a failure can say what happened rather than that nothing did.
    """
    mod = _load_ytdlp()
    if mod is not None:
        opts = _ydl_opts(config, flat=True)
        if playlist:
            opts["noplaylist"] = False
        try:
            with mod.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
        if not isinstance(info, dict):
            return None, "yt-dlp produced no video info"
        return info, ""

    exe = _binary_exe()
    if not exe or not os.path.isfile(exe):
        return None, "yt-dlp is not installed"
    cmd = [exe, "--dump-single-json", "--flat-playlist", "--no-warnings",
           "--no-progress"]
    if playlist:
        cmd.append("--yes-playlist")
    try:
        proc = run_tool(
            [*cmd, *cookie_args(config), url],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=_PROBE_TIMEOUT,
        )
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if proc.returncode != 0:
        tail = "; ".join((proc.stderr or "").strip().splitlines()[-3:])
        return None, tail or f"exit {proc.returncode}"
    info = _json_line(proc.stdout)
    if not isinstance(info, dict):
        return None, "yt-dlp produced no video info"
    return info, ""


def _search(query, config):
    """Flat ytsearch results for *query*; [] when nothing usable."""
    info, _error = _flat_info(f"ytsearch{SEARCH_RESULTS}:{query}", config)
    return [e for e in ((info or {}).get("entries") or []) if e]


def flat_playlist(url, config=None):
    """The flat `(title, entries)` of a YouTube / YouTube Music playlist URL.

    `entries` are yt-dlp's own — ``{id, url, title, duration, channel`` /
    ``uploader}`` — read through the SAME module-or-binary probe and the SAME
    cookie jar a track search uses. `title` is the playlist's own name (yt-dlp's
    `title` field), so an import can name the playlist the way the service does
    instead of guessing one.

    Raises RuntimeError with yt-dlp's words when the probe could not answer at
    all, which is deliberately not `_search`'s "[] for a search that found
    nothing": a caller importing a playlist has to tell "the playlist lists
    nothing" (its own answer) from "yt-dlp never ran" (the app's installation),
    and it can only do that if the failure is a raise.
    """
    info, error = _flat_info(url, config, playlist=True)
    if info is None:
        raise RuntimeError(error or "yt-dlp produced no playlist info")
    return (str(info.get("title") or "").strip(),
            [e for e in (info.get("entries") or []) if e])


def _score(entry, artist, title, want_seconds):
    """Preference score for one search hit; None = rejected.

    Rejected: hits whose length misses the known track length by more than
    ±5 s (that filter is skipped when no length is known), and lyric / cover /
    tribute / karaoke re-uploads — unless they come from the artist's own
    channel, which may legitimately title its upload "(Lyric Video)".
    """
    channel = entry.get("channel") or entry.get("uploader") or ""
    hit_title = entry.get("title") or ""
    dur = entry.get("duration")
    if want_seconds:
        try:
            if not dur or abs(float(dur) - float(want_seconds)) > DURATION_TOLERANCE:
                return None
        except (TypeError, ValueError):
            return None
    official = _mentions(channel, artist)
    if _REJECT_RX.search(hit_title) and not official:
        return None

    score = 0.0
    if official:
        score += 4.0
    if _OFFICIAL_HINTS.search(channel):
        score += 2.0
    if _mentions(hit_title, artist):
        score += 1.0
    if _mentions(hit_title, title):
        score += 2.0
    if want_seconds and dur:
        try:
            score -= abs(float(dur) - float(want_seconds)) / 10.0
        except (TypeError, ValueError):
            pass
    try:
        score += min(float(entry.get("view_count") or 0) / 1e7, 1.0)
    except (TypeError, ValueError):
        pass
    return score


def _probe_formats(url, config):
    """(height, abr) the format selector would take for *url*."""
    mod = _load_ytdlp()
    if mod is not None:
        try:
            with mod.YoutubeDL(_ydl_opts(config)) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            return None, None
    else:
        exe = _binary_exe()
        if not exe or not os.path.isfile(exe):
            return None, None
        try:
            proc = run_tool(
                [exe, "-j", "--no-warnings", "--no-progress",
                 "-f", _format_selector(config), "-S", FORMAT_SORT,
                 *cookie_args(config), url],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=_PROBE_TIMEOUT,
            )
        except Exception:
            return None, None
        info = _json_line(proc.stdout)
    if not isinstance(info, dict):
        return None, None
    height, abr, _fmt = _achieved(info)
    return height, abr


def best_candidate(artist, title, want_seconds=None, config=None):
    """Best YouTube match for one track, or None.

    Returns {id, url, title, duration, channel, height, abr} — height/abr are
    the streams the download would take, so a caller can tell the user what
    quality it is about to fetch.
    """
    if not enabled(config):
        return None
    query = " - ".join(
        p for p in (str(artist or "").strip(), str(title or "").strip()) if p)
    if not query:
        return None
    scored = []
    for entry in _search(query, config):
        score = _score(entry, artist, title, want_seconds)
        if score is not None:
            scored.append((score, entry))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    entry = scored[0][1]
    url = entry.get("url") or _watch_url(entry.get("id"))
    if not url:
        return None
    height, abr = _probe_formats(url, config)
    return {
        "id": entry.get("id"),
        "url": url,
        "title": entry.get("title"),
        "duration": entry.get("duration"),
        "channel": entry.get("channel") or entry.get("uploader"),
        "height": height,
        "abr": abr,
    }


# ----------------------------------------------------------------------
# Download
# ----------------------------------------------------------------------
def _resolve_output(info, dest_dir):
    """Path of the finished download, or None.

    yt-dlp's own filepath first (it is rewritten to the merged file), then the
    newest file below *dest_dir* carrying " [<video id>]" in its name — the
    id is part of OUTTMPL precisely so this stays resolvable.
    """
    for rd in info.get("requested_downloads") or []:
        path = rd.get("filepath")
        if path and os.path.isfile(path):
            return path
    for key in ("_filename", "filepath"):
        path = info.get(key)
        if path and os.path.isfile(path):
            return path
    vid = info.get("id")
    if vid:
        try:
            names = os.listdir(dest_dir)
        except OSError:
            names = []
        hits = [os.path.join(dest_dir, n) for n in names if f"[{vid}]" in n]
        hits = [h for h in hits if os.path.isfile(h)]
        if hits:
            return max(hits, key=os.path.getmtime)
    return None


def _download_python(mod, url, outtmpl, config):
    try:
        with mod.YoutubeDL(_ydl_opts(config, outtmpl=outtmpl)) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        raise RuntimeError(f"yt-dlp failed: {e}")
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp returned no video info")
    return info


def _download_binary(url, outtmpl, config):
    exe = _binary_exe()
    if not exe or not os.path.isfile(exe):
        raise RuntimeError("yt-dlp is not installed — install it under Dependencies")
    cmd = [exe, "-f", _format_selector(config), "-S", FORMAT_SORT,
           "--merge-output-format", "mkv", "--no-playlist", "--no-warnings",
           "--no-progress", "-o", outtmpl]
    location = _ffmpeg_location()
    if location:
        cmd += ["--ffmpeg-location", location]
    cmd += cookie_args(config)
    cmd += ["-j", "--no-simulate", url]
    try:
        proc = run_tool(cmd, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=_DOWNLOAD_TIMEOUT)
    except Exception as e:
        raise RuntimeError(f"yt-dlp failed: {e}")
    if proc.returncode != 0:
        tail = "; ".join((proc.stderr or "").strip().splitlines()[-3:])
        raise RuntimeError(f"yt-dlp failed: {tail or f'exit {proc.returncode}'}")
    info = _json_line(proc.stdout)
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp produced no video info")
    return info


def download(url, dest_dir, config=None):
    """Download *url* at the best quality available, merged into MKV.

    Returns {path, container, height, abr, format_id} with the streams that
    were actually fetched. Raises RuntimeError when YouTube downloads are
    disabled, yt-dlp is missing, or the download fails. Callers hand the path
    to mlo.remux for the library's FLAC-audio normalisation.
    """
    if not enabled(config):
        raise RuntimeError("YouTube downloads are disabled in Settings")
    dest_dir = os.path.abspath(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    outtmpl = os.path.join(dest_dir, OUTTMPL)

    mod = _load_ytdlp()
    if mod is not None:
        info = _download_python(mod, url, outtmpl, config)
    else:
        info = _download_binary(url, outtmpl, config)

    path = _resolve_output(info, dest_dir)
    if not path:
        raise RuntimeError("yt-dlp finished but produced no file")
    height, abr, format_id = _achieved(info)
    if not height or not abr:
        probed_h, probed_abr = _probe_media(path)
        height = height or probed_h
        abr = abr or probed_abr
    return {
        "path": path,
        "container": os.path.splitext(path)[1].lstrip(".").lower(),
        "height": height,
        "abr": abr,
        "format_id": format_id,
    }
