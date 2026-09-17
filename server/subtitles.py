"""Subtitle support for music videos.

Two sources are handled and both are served to the browser as WebVTT (the
only subtitle format <track> accepts):
  * subtitles muxed into the container (mp4 mov_text / mkv subrip / ass …),
    extracted on demand with ffmpeg;
  * external sidecar files next to the video ("Video.en.srt",
    "Video.vtt", …) — .vtt is passed through, .srt is converted.
"""
import os
import re
import threading

from mlo.tools import detect_all_tools
from mlo.remux import _ffprobe_json, _stream_info

_lock = threading.Lock()
_cache = {}  # _stat_key(video) -> muxed stream list


def _stat_key(path):
    """Cache key that changes when the video itself is replaced.

    Sidecars are deliberately NOT cached: they appear and disappear beside
    the file without touching its stat, and a stale listing would both hide
    a new .srt and resurrect a deleted one."""
    try:
        st = os.stat(path)
        return (os.path.normcase(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (os.path.normcase(path), 0, 0)


def _ffprobe_exe():
    return (detect_all_tools().get("ffmpeg") or {}).get("ffprobe_exe")


def _ffmpeg_exe():
    return (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")


def _sidecars(path):
    """External subtitle files beside the video: same stem + optional
    language suffix (.en.srt) plus any .srt/.vtt that starts with it."""
    base = os.path.splitext(path)[0].lower()
    out = []
    folder = os.path.dirname(path) or "."
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        low = name.lower()
        if not (low.endswith(".srt") or low.endswith(".vtt")):
            continue
        full = os.path.join(folder, name)
        stem = os.path.splitext(name)[0].lower()
        if stem == os.path.basename(base).lower() or stem.startswith(os.path.basename(base).lower() + "."):
            lang = None
            m = re.search(r"\.([a-z]{2,3}(?:[-_][a-z]{2,4})?)$", stem)
            if m:
                lang = m.group(1)
            out.append({"file": full, "name": name, "language": lang})
    return out


def list_subtitles(path):
    """Muxed subtitle streams + external sidecars for a video file."""
    key = _stat_key(path)
    with _lock:
        muxed = _cache.get(key)
    if muxed is None:
        muxed = []
        ffprobe = _ffprobe_exe()
        if ffprobe:
            info = _stream_info(path, ffprobe)
            if info:
                subs = info[2] or []
                # re-probe for titles/languages only when there are subs to name
                for n, codec in enumerate(subs):
                    muxed.append({"n": n, "codec": codec, "title": f"Track {n + 1} ({codec})"})
        with _lock:
            _cache[key] = muxed
            if len(_cache) > 256:  # ponytail: crude bound, entries are tiny
                _cache.clear()
    return {"muxed": list(muxed), "sidecars": _sidecars(path)}


_SRT_TIME = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")


def _srt_to_vtt(text):
    """Minimal SRT → WebVTT conversion: drop cue numbers, normalize the
    timing line, add the WEBVTT header."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = ["WEBVTT", ""]
    for block in re.split(r"\n{2,}", text):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if _SRT_TIME.search(lines[0]) is None and len(lines) >= 2 and _SRT_TIME.search(lines[1]):
            lines = lines[1:]  # drop the numeric cue counter
        lines = [_SRT_TIME.sub(lambda m: f"{m.group(1)}:{m.group(2)}:{m.group(3)}.{m.group(4)}", ln) for ln in lines]
        out.extend(lines)
        out.append("")
    return "\n".join(out)


def vtt_for(path, muxed_n=None, sidecar=None):
    """WebVTT bytes for the given muxed stream index or sidecar filename."""
    if sidecar:
        side = next((s for s in _sidecars(path) if os.path.basename(s["file"]) == sidecar), None)
        if not side:
            raise FileNotFoundError(f"no sidecar subtitle {sidecar!r}")
        with open(side["file"], "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        if side["file"].lower().endswith(".srt"):
            text = _srt_to_vtt(text)
        return text.encode("utf-8")

    if muxed_n is None:
        raise ValueError("muxed_n or sidecar required")
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not available")
    from mlo.subproc import run_tool

    proc = run_tool(
        [ffmpeg, "-v", "error", "-i", path, "-map", f"0:s:{muxed_n}", "-f", "webvtt", "-"],
        capture_output=True, timeout=120,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError((proc.stderr or b"").decode("utf-8", "replace")[:300] or "no subtitle output")
    return proc.stdout
