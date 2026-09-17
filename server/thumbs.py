"""Video frame rendering for the scrub previews.

`/api/videos/thumb` asks for one frame of a library video and gets a JPEG
back. Two things make that affordable while the user drags the scrubber:

  * the seek happens BEFORE `-i`, so ffmpeg does not decode the file up to
    *t* (a keyframe seek, orders of magnitude cheaper on a long video), and
  * every rendered frame is cached on disk under
    `<music>/.mlo/data/thumbs/`, keyed by path + mtime + width + the whole
    second of *t* — the scrubber asks for the second, not the microsecond, so
    dragging across one second re-renders nothing.

There is deliberately NO global lock: two requests for different frames run
their own ffmpeg and write their own temp file, and the finished file is moved
into place with `os.replace` (atomic), so a reader never sees a half-written
JPEG. Two requests for the SAME frame may both render it — a wasted encode,
not a corrupted cache.
"""
import hashlib
import os
import subprocess
import tempfile

from mlo.paths import app_data_dir, is_video_file

MAX_WIDTH = 1920
MIN_WIDTH = 32
DEFAULT_WIDTH = 320

# How far short of the container's stated length the end clamp stops: seeking
# to the exact end yields an empty output (see `clamp_time`).
_END_MARGIN = 0.1

# ponytail: a fixed 300 MB ceiling with an oldest-first prune after each
# encode; make it a config knob only if a library ever needs a bigger cache.
_CACHE_BYTES = 300 * 1024 * 1024
_CACHE_KEEP = 0.8


class ThumbError(Exception):
    """Rendering failed — `status` is the HTTP code the route should answer."""

    def __init__(self, message, status=500):
        super().__init__(message)
        self.status = status


def _ffmpeg():
    from mlo.tools import detect_all_tools

    exe = (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")
    if not exe:
        raise ThumbError(
            "ffmpeg not installed — install it under Dependencies for "
            "scrub previews", 503)
    return exe


def cache_dir(music_folder=None):
    """`<music>/.mlo/data/thumbs` — the app's own state dir for frames."""
    if music_folder is None:
        try:
            from mlo.config import load_config

            music_folder = load_config().get("music_folder") or None
        except Exception:
            music_folder = None
    d = app_data_dir(music_folder)
    return os.path.join(d, "thumbs") if d else None


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# path -> (mtime, seconds). ffprobe is a process spawn, so the length of each
# file is read once (the same laziness as `/api/videos/meta`).
_DURATION = {}


def _duration(path, music_folder=None):
    """The file's container length in seconds, or None when unknown."""
    key = os.path.normcase(path)
    hit = _DURATION.get(key)
    mtime = _mtime(path)
    if hit and hit[0] == mtime:
        return hit[1]
    seconds = None
    try:
        from mlo.remux import _stream_info
        from mlo.tools import detect_all_tools

        ffprobe = (detect_all_tools().get("ffmpeg") or {}).get("ffprobe_exe")
        info = _stream_info(path, ffprobe) if ffprobe else None
        if info and info[3]:
            seconds = float(info[3])
    except Exception:
        seconds = None
    _DURATION[key] = (mtime, seconds)
    return seconds


def clamp_time(path, t, music_folder=None):
    """The frame time to render: never negative, never past the end.

    The cap keeps a small margin off the container's stated length: seeking to
    the exact end lands past the last frame, and ffmpeg then writes nothing at
    all (a 500 for a request that is merely asking for the tail of the video).
    """
    try:
        t = float(t)
    except (TypeError, ValueError):
        t = 0.0
    if t != t or t < 0:            # NaN or a negative request
        t = 0.0
    end = _duration(path, music_folder)
    if end:
        t = min(t, max(0.0, float(end) - _END_MARGIN))
    return t


def cache_key(path, t, w):
    """`sha1(path|mtime|w|t_bucket)` — one entry per whole second of *t*."""
    bucket = int(t)
    raw = "%s|%s|%d|%d" % (os.path.normcase(os.path.abspath(path)),
                           repr(_mtime(path)), int(w), bucket)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def cached_thumb(path, t, w, music_folder=None):
    """The cached frame's path, or None when it has not been rendered yet."""
    d = cache_dir(music_folder)
    if not d:
        return None
    fp = os.path.join(d, cache_key(path, t, w) + ".jpg")
    return fp if os.path.isfile(fp) else None


def thumb_file(path, t=0.0, w=DEFAULT_WIDTH, music_folder=None):
    """The JPEG frame of *path* at *t* seconds — cached, rendered on a miss."""
    if not is_video_file(path):
        raise ThumbError("not a video file", 400)
    if not os.path.isfile(path):
        raise ThumbError("file not found", 404)
    try:
        w = int(w)
    except (TypeError, ValueError):
        w = DEFAULT_WIDTH
    w = max(MIN_WIDTH, min(w, MAX_WIDTH))
    t = clamp_time(path, t, music_folder)

    hit = cached_thumb(path, t, w, music_folder)
    if hit:
        return hit

    d = cache_dir(music_folder)
    if not d:
        raise ThumbError("no cache folder for this music folder", 500)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        raise ThumbError("could not create the thumb cache: %s" % e, 500)

    cmd = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
        # -ss BEFORE -i: keyframe seek. The frame is then decoded from there,
        # which is what makes dragging the scrubber affordable.
        "-ss", "%.3f" % t,
        "-i", path,
        "-frames:v", "1",
        "-vf", "scale=%d:-2" % w,
        "-f", "image2", "-",
    ]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # CREATE_NO_WINDOW: a piped ffmpeg flashes a console on Windows.
            creationflags=0x08000000 if os.name == "nt" else 0)
    except Exception as e:
        raise ThumbError("ffmpeg failed to start: %s" % e, 500)
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise ThumbError("ffmpeg could not render that frame: %s"
                         % (detail.splitlines()[-1] if detail else "no output"),
                         500)

    fp = os.path.join(d, cache_key(path, t, w) + ".jpg")
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(proc.stdout)
        os.replace(tmp, fp)            # atomic: readers see a whole JPEG
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ThumbError("could not cache the frame: %s" % e, 500)
    _prune(d)
    return fp


def _prune(d):
    """Drop the oldest frames once the cache outgrows its ceiling."""
    try:
        entries = []
        total = 0
        for name in os.listdir(d):
            if not name.endswith(".jpg") and not name.endswith(".part"):
                continue
            fp = os.path.join(d, name)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, fp))
            total += st.st_size
    except OSError:
        return
    if total <= _CACHE_BYTES:
        return
    keep = _CACHE_BYTES * _CACHE_KEEP
    for mtime, size, fp in sorted(entries):
        if total <= keep:
            break
        try:
            os.remove(fp)
            total -= size
        except OSError:
            pass
