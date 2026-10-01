"""Shared helpers for the API router modules.

These lived in ``server/main.py`` until the route clusters moved out into
their own modules: the music-folder guards every path-taking endpoint uses,
the library-changed hook the import/remove/organize paths call, and the one
audio-file test the trash, MusicBrainz and import surfaces share.

Nothing here imports ``server.main`` at import time, so a router module can
use these without a circular import.
"""
import os

from fastapi import HTTPException

from mlo.naming import sanitize_segment
from mlo.paths import SKIP_DIRS


def load_config():
    """The app's live configuration.

    Read THROUGH ``server.main`` at call time rather than captured at import:
    the app module is what the test harnesses and tools rebind to point a run
    at a scratch music folder, and a router that had captured the function
    would keep reading the developer's real config behind their back — the
    same reason ``server.api_imports`` reaches into main for the folder guards.
    """
    from server.main import load_config as live
    return live()


def _music_folder(cfg=None):
    cfg = cfg or load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    return folder


def _in_music_folder(p, folder):
    """Path-containment guard shared by every path-taking endpoint.

    Compares normalized absolute paths at directory boundaries so a
    sibling like C:\\Music2 is never treated as being inside C:\\Music.
    """
    try:
        # realpath: a symlink inside the library pointing out must not pass.
        ap = os.path.realpath(os.path.normpath(p))
        af = os.path.realpath(os.path.normpath(folder))
    except (OSError, ValueError, TypeError):
        return False
    if ap == af:
        return True
    # Paths on different drives (C: vs F:, or two UNC shares) have no common
    # ancestor at all; commonpath raises ValueError there, which means
    # "outside" — not a 500 out of every path-taking endpoint.
    if os.path.normcase(os.path.splitdrive(ap)[0]) != os.path.normcase(os.path.splitdrive(af)[0]):
        return False
    try:
        common = os.path.commonpath([ap, af])
    except (OSError, ValueError, TypeError):
        return False
    return os.path.normcase(common) == os.path.normcase(af)


def _allow_staged(p, staged):
    """The import wizard's opt-in allowance for a path that is NOT in the
    library yet.

    The wizard's per-track steps run on albums the library tree does not list
    — the folder the user pointed it at, a finished download, a staged batch —
    so "inside the music folder" is the wrong test for those calls. It is
    opt-in per request (`staged=true`), and the path still has to EXIST: a
    library-facing call that does not ask for it stays exactly as strict, and
    nothing gains a read of something that is not there.
    """
    return bool(staged) and os.path.exists(p)


def _guard_folder(p, staged=False, what="file", folder=None):
    """The music-folder guard, with the wizard's staged allowance folded in.

    `what` names the thing in the 400 ("file", "folder", "album", "path") and
    `folder` overrides the music folder for the routes that already carry one.
    """
    if _in_music_folder(p, folder or _music_folder()):
        return
    if _allow_staged(p, staged):
        return
    raise HTTPException(400, f"{what} outside music folder")


def _skip_names():
    return {d.lower() for d in SKIP_DIRS}


def is_audio_file(name):
    """Library tracks: audio + music-video containers (mlo.paths definition,
    so organize / genre import / MB matching treat videos as tracks too)."""
    from mlo.paths import LIB_AUDIO_EXTS
    return os.path.splitext(name)[1].lower() in LIB_AUDIO_EXTS


def re_safe_filename(name):
    """Older, weaker spelling of the app's ONE filename rule.

    Kept as a name (the routes below and their tests call it) but it is now
    exactly mlo.naming.sanitize_segment: a character that is invalid in a name
    becomes "_", a trailing dot/space becomes "_" instead of hiding until the
    filesystem drops it, and a reserved device name gets a trailing "_". Two
    conventions for one character set is how a path the app WROTE stops being
    a path the app can find again."""
    return sanitize_segment(name)
