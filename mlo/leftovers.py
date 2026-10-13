"""Album leftovers: files whose album is not in the folder they sit in.

The library's shape is ``<music>/Artists/<Artist>/<Album>/<files>``. An artist
folder holds the artist's OWN two files (``artist.jpg``, ``description.txt``,
written by ``mlo.artistdata``) and album folders; everything a rip leaves —
``.cue``/``.log``/``.accurip``/``.lrc`` sidecars, the album cover, the release
manifest ``.mb_expected``, the import marker ``.mlo_importing.json`` — belongs
INSIDE an album folder, beside that album's own audio.

The same file one level up describes an album that is not there: the album was
moved, renamed or removed and its sidecars stayed behind. The owner's report
(2026-10-13) is exactly that shape — an artist folder holding a gone album's
``CD-1.cue``/``.log``/``.accurip``, ``cover.jpg``, ``description (2).txt``, a
``.mb_expected`` and a ``.mlo_importing.json``, with nothing in the app saying a
word about any of it (the layout scan reads an artist folder's loose files as
"the artist's own content — expected", and the empty-folder walk exempts a
folder whose CHILDREN hold audio).

ONE predicate, so the surfaces cannot disagree:

  * ``mlo.layout`` — script 20, the scan that reports AND fixes — names each
    leftover with a Trash fix; that is where "getting rid of them" happens, and
    the Trash page can put every one of them back;
  * the grade charges ``LEFT_OVER_FILES`` for the folder, so the Library lists
    it as a failing row instead of a tidy artist folder;
  * an import's own finish sweeps the folder it wrote into, so the debris of an
    earlier attempt does not outlive the next one.

Nothing here is ever audio: an audio file in an artist folder is its own,
long-standing finding (``audio_in_artist``), not a leftover.
"""
from __future__ import annotations

import os
import time

from .discs import is_disc_parent
from .paths import (ALBUM_COVER_NAMES, EXPECTED_TRACKS_FILE, IMPORTING_FILE,
                    LIB_AUDIO_EXTS, album_sidecar_of, trash_path)

__all__ = [
    "ARTIST_FILES", "IMPORT_MARKER_GRACE_S", "folder_is_album", "holds_audio",
    "import_running", "is_leftover", "leftover_names", "sweep",
]

# The artist folder's own files: never leftovers, wherever they sit.
ARTIST_FILES = frozenset({
    "artist.jpg", "artist.jpeg", "artist.png", "artist.webp", "artist.jxl",
    "description.txt",
})

# The rip's own sidecars. `.lrc` counts: a lyrics sidecar is named after a
# track, so beside no audio it is nobody's.
RIP_SIDECAR_EXTS = (".cue", ".log", ".accurip", ".lrc")

# How old an import marker has to be before this module will touch it. A live
# import rewrites its marker as the session advances, and the app clears it when
# the session ends; one that has sat untouched for this long is an import that
# died rather than one that is running.
# honey: a marker idle for six hours is taken for dead — an import that really
# runs that long without a single session write is not a shape the wizard
# produces; raise the grace if one ever is.
IMPORT_MARKER_GRACE_S = 6 * 3600


def holds_audio(names) -> bool:
    """Whether a folder's own listing holds audio (its own, not a child's)."""
    return any(os.path.splitext(n)[1].lower() in LIB_AUDIO_EXTS for n in names)


def _subtree_has_audio(folder, depth: int = 2) -> bool:
    """Whether audio sits under *folder* (the layout's own nesting: disc folders
    inside an album)."""
    base = folder.count(os.sep)
    for root, dirs, names in os.walk(folder):
        if root.count(os.sep) - base > depth:
            dirs[:] = []
            continue
        if holds_audio(names):
            return True
    return False


def folder_is_album(folder: str, entries=None) -> bool:
    """Whether *folder* is an ALBUM — so nothing inside it is a leftover.

    Two ways to be one, and they are the library's own: audio sits directly in
    it, or its children are the DISC folders of a multi-disc release
    (``mlo.discs.is_disc_parent``, the one rule the staging walk, the mover and
    the wizard share) and one of them holds audio. Without the second half a
    multi-disc album — whose ``CD-1.cue``/``CD-1.log`` sit at the album's own
    level, beside ``CD1``/``CD2`` — would report its own rip sheets as
    leftovers.
    """
    if entries is None:
        try:
            entries = os.listdir(folder)
        except OSError:
            return False
    entries = list(entries)
    if holds_audio(entries):
        return True
    subs = [n for n in entries if os.path.isdir(os.path.join(folder, n))]
    if not subs or not is_disc_parent(subs):
        return False
    return any(_subtree_has_audio(os.path.join(folder, n)) for n in subs)


def is_leftover(name: str) -> bool:
    """Whether *name* is album debris when it sits in a folder that is not an
    album (an artist folder, or any folder holding no audio of its own)."""
    low = str(name or "").lower()
    if not low or low in ARTIST_FILES:
        return False
    if low in (EXPECTED_TRACKS_FILE, IMPORTING_FILE):
        return True
    if low in ALBUM_COVER_NAMES:
        return True
    if os.path.splitext(low)[1] in RIP_SIDECAR_EXTS:
        return True
    # A NUMBERED copy of the artist's own description ("description (2).txt"):
    # the app writes one `description.txt` atomically, so a numbered one came
    # from a file manager or a sync client. `album_sidecar_of` is the app's one
    # reader of that name family.
    if album_sidecar_of(name) and low != "description.txt":
        return True
    return False


def _marker_is_live(path: str, now: float) -> bool:
    """Whether an import marker is young enough to belong to a running session."""
    try:
        return (now - os.path.getmtime(path)) < IMPORT_MARKER_GRACE_S
    except OSError:
        return True  # cannot be read: leave it alone


def import_running(folder: str, entries=None, now: float | None = None) -> bool:
    """Whether *folder* carries an import marker young enough to be a LIVE one.

    The marker is written while a session runs and cleared when it ends, so a
    folder holding a fresh one is a folder an import is working in: grading and
    the layout both leave it alone rather than reporting the process."""
    if entries is None:
        try:
            entries = os.listdir(folder)
        except OSError:
            return False
    if IMPORTING_FILE not in entries:
        return False
    return _marker_is_live(os.path.join(folder, IMPORTING_FILE),
                           time.time() if now is None else now)


def leftover_names(folder: str, entries=None, now: float | None = None) -> list:
    """The leftovers in *folder*, sorted — its own listing unless *entries*.

    A folder that IS an album (:func:`folder_is_album` — audio of its own, or
    disc folders under it) keeps its own sidecars: nothing there is a leftover.
    A live import marker is not one either (see :data:`IMPORT_MARKER_GRACE_S`).
    """
    if entries is None:
        try:
            entries = os.listdir(folder)
        except OSError:
            return []
    entries = list(entries)
    now = time.time() if now is None else now
    if folder_is_album(folder, entries):
        return []
    if import_running(folder, entries, now):
        # An import is working IN this folder: the files beside its marker are
        # that import's, however little audio is there yet.
        return []
    return sorted(name for name in entries if is_leftover(name))


def sweep(folder: str, *, entries=None, music_folder: str = "", user: str = "",
          now: float | None = None, log=None) -> list:
    """Move every leftover in *folder* to the app's Trash. Returns their names.

    Every removal goes through ``mlo.paths.trash_path``: the same bin, the same
    origin manifest and the same restore path the "remove from library" routes
    use, so nothing is destroyed and the Trash page can put each file back where
    it came from. Anything that cannot be moved is left and reported.
    """
    names = leftover_names(folder, entries=entries, now=now)
    moved = []
    for name in names:
        src = os.path.join(folder, name)
        try:
            dest = trash_path(src, music_folder or None, user)
        except Exception:  # a bin that cannot be made is not a reason to fail a run
            dest = ""
        if dest:
            moved.append(name)
        elif log is not None:
            log(f"  could not move {src} to the Trash")
    return moved