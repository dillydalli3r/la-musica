"""Optional third-party dependencies.

Centralises feature detection so the rest of the package can rely on
HAS_MUTAGEN / HAS_PIL and the mutagen classes (None when not installed).
It also answers the other question the UI has to ask before it offers
anything: what can THIS build actually do (capabilities()).
"""
import os
import subprocess

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


try:
    from mutagen.flac import FLAC
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    from mutagen.mp3 import MP3
    from mutagen.id3 import (TXXX, USLT, COMM, UFID, Encoding, TextFrame,
                             Frames, APIC)
    from mutagen.mp4 import MP4, MP4FreeForm, MP4Cover
    from mutagen.flac import Picture
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False
    FLAC = OggVorbis = OggOpus = MP3 = MP4 = MP4FreeForm = None
    TXXX = USLT = COMM = UFID = Encoding = TextFrame = Frames = APIC = None
    MP4Cover = Picture = None


try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# --------------------------------------------------------------------------- #
# What this build can do
# --------------------------------------------------------------------------- #
# The app has two kinds of feature. Tagging, the API, library browsing,
# streaming and playlists run inside this Python process and work wherever
# Python runs. Everything else — transcoding, video, the audits, ReplayGain,
# beets, AcoustID, Soulseek — is another program started by this one, and a
# mobile sandbox may forbid that outright: iOS refuses to exec anything, even
# a binary the app itself ships. Those two cases need different words in the
# UI. "Not installed yet" offers an Install button; "this device cannot start
# a program at all" must not, because no download can fix it.
#
# The split is DERIVED, never a platform table: "is the tool here" comes from
# the real detection (bundled dir, .dependencies, PATH — see mlo.tools) and
# "can a program be started at all" from a real spawn. An Android build with
# a bundled ffmpeg and an iOS build with nothing installed get different
# answers from the same code, which is the point.

# feature key -> (label, alternatives, how to get it). `alternatives` is an OR
# of ANDs: the feature works when every tool of ANY one group is present
# (loudness takes rsgain, or ffmpeg for the EBU R128 measurement; image
# conversion takes any one of the three encoders). The tools listed under a
# feature are what the Dependencies table rows are matched on.
_TOOL_FEATURES = (
    ("transcode", "Transcoding / format export",
     (("ffmpeg",),), "install ffmpeg from Dependencies"),
    ("video", "Video (probe, remux, thumbnails)",
     (("ffmpeg",),), "install ffmpeg from Dependencies"),
    ("loudness", "Loudness and ReplayGain measurement",
     (("rsgain",), ("ffmpeg",)), "install rsgain from Dependencies"),
    ("accuraterip", "AccurateRip verification (CUETools)",
     (("cuetools", "ffmpeg"),), "install CUETools and ffmpeg from Dependencies"),
    ("audit", "Audio audit (AudioAuditor spectral pass)",
     (("audioauditor",),), "install AudioAuditor from Dependencies"),
    ("logchecker", "Rip-log checking (Logchecker)",
     (("logchecker", "php"),), "install Logchecker and PHP from Dependencies"),
    ("beets", "beets tagging",
     (("beets",),), "install beets from Dependencies"),
    ("acoustid", "AcoustID fingerprinting (fpcalc)",
     (("chromaprint",),), "install Chromaprint from Dependencies"),
    ("images", "Image conversion (JXL / JPEG / PNG)",
     (("libjxl",), ("libjpeg_turbo",), ("oxipng",)),
     "install libjxl, libjpeg-turbo or oxipng from Dependencies"),
    ("soulseek", "Soulseek downloads (slskd)",
     (("slskd",),), "install slskd from Dependencies"),
    ("keybpm", "Key & BPM detection (librosa)",
     (("librosa",),), "install librosa from Dependencies"),
)

# Features that never leave this process. They work wherever Python runs —
# on a phone, a laptop or a server — and the only thing that can take them
# away is a missing Python package (mutagen is not optional for the rest of
# the app either).
_INPROCESS_FEATURES = (
    ("core", "Tag read/write, library browsing, streaming, playlists",
     "bundled with the app", ("mutagen",)),
    ("lyrics", "Lyrics fetching", "built in (needs a network, no tool)", ()),
)

# (ok, reason) for "can this process start another program at all", measured
# once and cached — the answer cannot change while the process lives.
_SPAWN_CACHE = None


def _probe_spawn():
    """(ok, reason): a REAL spawn, so a sandbox that refuses one says so.

    `/bin/sh` exists on every POSIX system and `cmd.exe` on every Windows one,
    and neither is ever the thing the app is missing. A sandboxed iOS app gets
    EPERM/EACCES here for exactly the call that succeeds on a desktop, which is
    the distinction this whole report exists to make. Never raises.
    """
    cmd = ([os.environ.get("COMSPEC", "cmd.exe"), "/c", "exit", "0"]
           if os.name == "nt" else ["/bin/sh", "-c", "true"])
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=15)
    except Exception as e:
        return False, (f"the OS does not let an app start another program "
                       f"({e.__class__.__name__})")
    if proc.returncode != 0:
        return False, (f"the OS refused to start another program "
                       f"(exit {proc.returncode})")
    return True, None


def can_spawn():
    """(ok, reason) for "can this process start another program at all"."""
    global _SPAWN_CACHE
    if _SPAWN_CACHE is None:
        _SPAWN_CACHE = _probe_spawn()
    return _SPAWN_CACHE


def _has_tool(key, tools):
    """Whether tool *key* is usable here.

    A detected binary is one source; the pip-vendored tools (beets, librosa)
    live in .dependencies as packages rather than as programs, and a Python
    package is asked for its own way.
    """
    if key in tools:
        return True
    if key == "mutagen":
        return HAS_MUTAGEN
    from .tools import python_pkg_path

    return python_pkg_path(key) is not None


def capabilities(cfg=None) -> dict:
    """What THIS build can do, per feature.

    Each feature is `{"label", "available", "reason", "how", "tools"}`:
    `reason` is null when it works, says what is missing when the installer can
    fix it, and says why nothing on this device ever could when the platform
    forbids it. `tools` names the Dependencies rows the feature needs, so the
    table can mark exactly those rows.

    Two extra keys describe the build rather than a feature: `can_spawn` (the
    measurement the tool-backed rows depend on) and `installable` (whether the
    Dependencies installer can help at all — there is nothing to install on a
    device that cannot start a program).

    Cheap and offline: the tool detection is cached by mlo.tools, the spawn
    probe runs once per process, and `cfg` is accepted for callers that
    already have the config but nothing here depends on it yet.
    """
    from .tools import detect_all_tools

    tools = detect_all_tools()
    spawn_ok, spawn_reason = can_spawn()
    out = {}

    for key, label, alternatives, how in _TOOL_FEATURES:
        needed = sorted({t for group in alternatives for t in group})
        # A tool that cannot be STARTED is not available, however present it
        # looks: on a platform where spawning is refused, a bundled ffmpeg is
        # a file, not a feature.
        available = spawn_ok and any(all(_has_tool(t, tools) for t in group)
                                     for group in alternatives)
        if available:
            reason = None
        elif not spawn_ok:
            # No download can help: every one of these features is a program
            # this process has to start.
            reason = spawn_reason
        else:
            missing = sorted({t for t in needed if not _has_tool(t, tools)})
            reason = f"not installed ({', '.join(missing)})"
        out[key] = {"label": label, "available": available, "reason": reason,
                    "how": None if available else how, "tools": needed}

    for key, label, how, needs in _INPROCESS_FEATURES:
        missing = [t for t in needs if not _has_tool(t, tools)]
        out[key] = {
            "label": label,
            "available": not missing,
            "reason": (f"not installed ({', '.join(missing)})" if missing else None),
            "how": how,
            "tools": [],
        }

    out["can_spawn"] = {"label": "Starting external programs",
                        "available": spawn_ok, "reason": spawn_reason,
                        "how": None, "tools": []}
    out["installable"] = spawn_ok
    return out


