"""Filesystem locations and format constants.

When frozen (PyInstaller) SCRIPT_DIR points at the exe's folder so that
config.json and the .dependencies toolchain live next to the executable.
"""
import errno
import json
import os
import re
import shutil
import sys
import tempfile
import time

# Marker written next to a PATH-installed CLI; first line is the folder
# that holds config.json and .dependencies.
HOME_MARKER = "mlo-home.txt"


def _resolve_script_dir():
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    marker = os.path.join(base, HOME_MARKER)
    if os.path.isfile(marker):
        try:
            with open(marker, "r", encoding="utf-8-sig") as f:
                home = f.readline().strip()
            if home and os.path.isdir(home):
                return home
        except OSError:
            pass
    return base


SCRIPT_DIR = _resolve_script_dir()


CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")


# Legacy locations (repo-local) that predate the Data folder; a stub
# config.json left behind by the migration keeps pointing at the music dir.
LEGACY_DATA_DIR = os.path.join(SCRIPT_DIR, "server", "data")


# Layout inside the music folder: <music>/Artists (library) and ONE hidden
# <music>/.mlo folder holding everything else — data/ (all app state),
# downloads/ (completed transfers: one queue for the appliance, shared by
# every user), incomplete/ (in-flight ones), trash/<user>/ (the
# remove-from-library bin, one per user) and tools/ (the external tools the
# installer fetches — see tools_dir).
# The old top-level <music>/.mlo_data is now only a migration source
# (LEGACY_MLO_DATA_DIR_NAME below).
ARTISTS_DIR_NAME = "Artists"
MLO_DIR_NAME = ".mlo"
MLO_DATA_DIR_NAME = "data"
DOWNLOADS_DIR_NAME = "downloads"
INCOMPLETE_DIR_NAME = "incomplete"
TRASH_DIR_NAME = "trash"
TOOLS_DIR_NAME = "tools"

# The pre-.mlo state dir inside the music folder: still read by the
# migration, still skipped by the scanner, never written to.
LEGACY_MLO_DATA_DIR_NAME = ".mlo_data"


def _warn_if_temp_folder(mf):
    """A music folder inside the OS temp dir is never a real library.

    Tests redirect the music folder with MLO_MUSIC_FOLDER or by rewriting the
    stub config.json; when either leaks into a real run, the app happily
    stages downloads, imports and grading inside a throwaway directory and
    nothing says so. One line on stderr beats debugging where the bytes went."""
    try:
        temp = os.path.realpath(tempfile.gettempdir())
        if os.path.commonpath([temp, os.path.realpath(mf)]) == temp:
            sys.stderr.write(
                f"warning: music folder {mf!r} is inside the OS temp directory — "
                "downloads, imports and grading would run on a throwaway folder "
                "(a test redirect left in config.json?)\n")
    except (OSError, ValueError):
        pass


def read_music_folder_guess():
    """Best-effort music folder from whichever config file exists.

    Used to locate the state directory before and after the migration: the
    legacy repo-local config.json knows the music folder, and after the
    move a stub remains behind at the legacy path for the same purpose."""
    mf = os.environ.get("MLO_MUSIC_FOLDER")
    if mf:
        _warn_if_temp_folder(mf)
        return mf
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            mf = (json.load(f) or {}).get("music_folder")
        if mf:
            _warn_if_temp_folder(mf)
            return str(mf)
    except Exception:
        pass
    return None


def _scope(music_folder=None):
    """Music folder owning the app state, or None while unconfigured."""
    return music_folder or read_music_folder_guess()


def library_root(music_folder=None):
    """The library itself: <music folder>/Artists (None = no music folder).

    This is THE base every library path is built on: the naming script only
    names the path *inside* this folder, so organize/tagging, the grader's
    naming check and the beets ``directory:`` all join their relative path
    onto it. The music folder ROOT holds only the app state dirs and whatever
    the organizer has not filed yet — the Soulseek share is THIS folder, not
    the root (server.soulseek.share_dirs) — so a library path must never be
    built from the music folder itself."""
    mf = _scope(music_folder)
    return os.path.join(mf, ARTISTS_DIR_NAME) if mf else None


def mlo_root(music_folder=None):
    """Parent of the transient dirs: <music folder>/.mlo.

    None while no music folder is known — there is no folder to hang
    downloads/trash off yet, so callers keep their own repo-local fallback."""
    mf = _scope(music_folder)
    return os.path.join(mf, MLO_DIR_NAME) if mf else None


def app_data_dir(music_folder=None):
    """The folder holding ALL app state: <music folder>/.mlo/data.

    The music folder is formatted as <music>/Artists (the library) + the
    single hidden <music>/.mlo (this folder, downloads, trash). Falls back
    to the legacy repo-local server/data only while no music folder is
    configured (fresh setup)."""
    mf = _scope(music_folder)
    if mf:
        return os.path.join(mf, MLO_DIR_NAME, MLO_DATA_DIR_NAME)
    return LEGACY_DATA_DIR


# The default/admin scope's name on disk: the rows and sessions that carry
# `""` (an install that never claimed a user, and everything written before
# users existed). A path segment is never left empty, so this scope gets a
# literal folder of its own instead of the base folder's trailing separator.
DEFAULT_SCOPE = "default"


def user_segment(user):
    """The path segment for a user scope: `user`, or `default` for `""`.

    A username reaches this function from a session, and a session's username
    can come from `POST /api/auth/setup`, which is PUBLIC while no password is
    set yet. So this is a trust boundary, not a formatting helper: a name that
    is not one plain segment (`..`, `a/b`, `C:`, a NUL) would walk out of the
    folder it names — `trash_dir(mf, "..")` is `<mf>/.mlo/trash/..`, i.e.
    `<mf>/.mlo`, and the trash page's own "delete" would then remove the app's
    data directory. Anything unsafe falls back to the default scope instead of
    being honoured, and `server.auth.user_problem` refuses such a name at the
    door so it can never become a session in the first place.
    """
    text = str(user or "").strip()
    if not text:
        return DEFAULT_SCOPE
    if text in (".", "..") or "\x00" in text:
        return DEFAULT_SCOPE
    if any(c in text for c in "/\\:"):
        return DEFAULT_SCOPE
    return text


def slug_name(name, limit=60):
    """A user-typed name reduced to ONE safe path segment — the rule both
    named file stores under `app_data_dir` share (imported EQ profiles in
    `mlo.eq`, saved export configs in `mlo.exportconfigs`), so a name written
    once reads back under the same file.

    Letters, digits, ``-``, ``_`` and ``.`` survive; every other character
    becomes ``_``, runs collapse, and the result is trimmed of leading/trailing
    separators. ``""`` when nothing survives (a name of only punctuation).
    """
    text = re.sub(r"[^0-9A-Za-z._ -]+", "_", str(name or "").strip())
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._-")
    return text[:limit]


def safe_segment(value, what="id"):
    """``(segment, None)`` when *value* is a usable file stem, else
    ``(None, error)``.

    The value comes from a URL and from a user-editable config, so anything
    that could name a file outside the folder it belongs in is REFUSED rather
    than sanitized: a caller that asked for ``../../config`` gets told no
    instead of silently reading or deleting something in the data dir.
    """
    raw = str(value or "").strip()
    if not raw:
        return None, f"no {what} given"
    if any(sep in raw for sep in ("/", "\\")) or ".." in raw or "\x00" in raw:
        return None, f"invalid {what}: {raw!r}"
    stem = slug_name(raw)
    if not stem:
        return None, f"invalid {what}: {raw!r}"
    return stem, None


# The old name, kept because the tests and a sibling module import it.
_user_segment = user_segment


def trash_root(music_folder=None):
    """The bin's own directory: <music folder>/.mlo/trash.

    The PARENT of the per-user bins, and the thing a music-folder change has
    to carry: moving `trash_dir()` alone would carry the `default` scope and
    strand every named user's bin in the old folder.
    """
    root = mlo_root(music_folder)
    return os.path.join(root, TRASH_DIR_NAME) if root else None


def downloads_dir(music_folder=None):
    """Where slskd saves COMPLETED files: <music folder>/.mlo/downloads
    (None = no music folder; the caller decides the fallback).

    NOT scoped by user, deliberately: slskd is one queue for the whole
    appliance, and `soulseek._incomplete_dir` derives its staging dir as this
    folder's SIBLING — a per-user segment here would nest `incomplete/`
    inside `downloads/`, where the listing and the import sweep would treat
    it as another album folder.
    """
    root = mlo_root(music_folder)
    return os.path.join(root, DOWNLOADS_DIR_NAME) if root else None


def incomplete_dir(music_folder=None):
    """Where slskd stages IN-FLIGHT transfers: <music folder>/.mlo/incomplete.

    A SIBLING of downloads/, not a `.incomplete` child of it. Partial files
    used to land inside the folder the app lists, imports from and shares, so
    every consumer had to remember to skip a dot-dir that was really a second
    download tree; two top-level folders make "completed" and "still coming"
    impossible to confuse. slskd is pointed at this path explicitly and
    validates it at boot (see server.soulseek._ensure_dirs)."""
    root = mlo_root(music_folder)
    return os.path.join(root, INCOMPLETE_DIR_NAME) if root else None


def trash_dir(music_folder=None, user=""):
    """Remove-from-library bin: <music folder>/.mlo/trash/<user> (None = no
    music folder).

    The empty scope — an unclaimed install, and everything written before
    users existed — is the literal `default` segment, so a bin path is never
    empty and one user's entries never surface in another's (each bin carries
    its own origin manifest). A bin written before users existed sits at
    `<music>/.mlo/trash` and is moved into the `default` segment on the next
    start (see mlo.config._adopt_legacy_trash)."""
    root = trash_root(music_folder)
    return os.path.join(root, user_segment(user)) if root else None


# The bin's origin manifest: server/main.py writes and reads the same file
# (same name, same schema — {"version": 1, "entries": {name: {origin, at}}}),
# because the Trash page restores an entry to the path recorded here.
TRASH_MANIFEST_NAME = ".mlo_manifest.json"


def _trash_manifest_path(bin_dir):
    return os.path.join(bin_dir, TRASH_MANIFEST_NAME)


def _trash_manifest_entries(bin_dir):
    """{entry name: {"origin", "at"}}; a missing or corrupt manifest reads as
    empty — entries trashed before the file existed are a normal state."""
    try:
        with open(_trash_manifest_path(bin_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def trash_path(path, music_folder=None, user="") -> str:
    """Move one FILE OR FOLDER into the app's trash bin; returns its new path
    ("" on failure). Nothing is deleted — this is the same destination and
    origin manifest the removal routes ("Remove from library", "remove empty
    artist") use, so the Trash page lists the entry and can put it back where
    it came from.

    A same-named entry already in the bin gets the "(2)" suffix the album move
    uses, so a second conversion of a regenerated source never overwrites the
    master already in there. A file keeps its extension in that suffix
    ("song (2).flac"); a folder is plain "name (2)", the way a removal names
    one.
    """
    bin_dir = trash_dir(music_folder, user)
    src = os.path.abspath(path)
    if not bin_dir:
        return ""
    is_dir = os.path.isdir(src)
    if not is_dir and not os.path.isfile(src):
        return ""
    try:
        os.makedirs(bin_dir, exist_ok=True)
    except OSError:
        return ""
    name = os.path.basename(src)
    stem, ext = (name, "") if is_dir else os.path.splitext(name)
    dest = os.path.join(bin_dir, name)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(bin_dir, f"{stem} ({n}){ext}")
        n += 1
    if not move_path(src, dest):
        return ""
    entries = _trash_manifest_entries(bin_dir)
    entries[os.path.basename(dest)] = {
        "origin": src.replace("\\", "/"),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        tmp = _trash_manifest_path(bin_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "entries": entries}, f)
        os.replace(tmp, _trash_manifest_path(bin_dir))
    except OSError:
        # The file IS in the bin either way; without a record the Trash page
        # asks for a destination instead of guessing one.
        pass
    return dest


def trash_file(path, music_folder=None, user="") -> str:
    """Move one FILE into the app's trash bin; returns its new path ("" on
    failure). The file half of :func:`trash_path`, which owns the bin, the
    collision naming and the origin manifest."""
    if not os.path.isfile(os.path.abspath(path)):
        return ""
    return trash_path(path, music_folder, user)


def previous_state_dirs(music_folder=None):
    """State dirs of the PREVIOUS music folder, as recorded in the stub.

    These belong to a DIFFERENT install location (a music-folder change, or a
    scope handed in through MLO_MUSIC_FOLDER), so the migration copies them
    instead of moving — see mlo.config._migrate_to_data_dir."""
    out = []
    mf = _scope(music_folder)
    if not mf:
        return out
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            prev = str((json.load(f) or {}).get("music_folder") or "").strip()
    except Exception:
        prev = ""
    if prev and os.path.normcase(os.path.abspath(prev)) != os.path.normcase(os.path.abspath(mf)):
        out.append(os.path.join(prev, "Data"))
        out.append(os.path.join(prev, LEGACY_MLO_DATA_DIR_NAME))
        out.append(os.path.join(prev, MLO_DIR_NAME, MLO_DATA_DIR_NAME))
    return out


def legacy_state_dirs(music_folder=None):
    """Every dir that may hold pre-migration app state, best first.

    `<music>/Data` is the original layout, `<music>/.mlo_data` the one
    shipped just before the single-.mlo layout; a music-folder change (or an
    MLO_MUSIC_FOLDER override) can leave state in the previous folder's
    Data/.mlo_data/.mlo/data (see previous_state_dirs); server/data holds
    state from before the Data folder."""
    out = []
    mf = _scope(music_folder)
    if mf:
        out.append(os.path.join(mf, "Data"))
        out.append(os.path.join(mf, LEGACY_MLO_DATA_DIR_NAME))
        out.extend(previous_state_dirs(mf))
    out.append(LEGACY_DATA_DIR)
    return out


# The tools folder from BEFORE the move: <app folder>/.dependencies. Nothing
# writes here any more — the tools travel with the music folder now (tools_dir)
# — but it is still READ, so an install that predates the move keeps finding
# the tools it already has instead of being told they are gone. It is also what
# tools_dir() answers while no music folder is configured: a fresh install has
# nowhere else to put them yet.
DEPS_DIR = os.path.join(SCRIPT_DIR, ".dependencies")


def tools_dir(music_folder=None):
    """The folder holding the external tools: <music folder>/.mlo/tools.

    The tools belong with the rest of the app's things, and the music folder is
    where those live: as a constant beside the app they stayed behind whenever
    the music folder moved, and an install whose app folder is read-only (a
    packaged app) could not install a tool at all. Derived from the music folder
    on every call for exactly that reason — a music-folder change must not leave
    a stale path behind.

    The pre-move root while no music folder is known (see legacy_tools_dir):
    that is the one folder an unconfigured install can still write to.
    """
    root = mlo_root(music_folder)
    return os.path.join(root, TOOLS_DIR_NAME) if root else DEPS_DIR


def legacy_tools_dir():
    """The pre-move tools root: <app folder>/.dependencies (read-only now).

    Detection and the installer's "is it already installed?" question read it
    (tools_dirs), so a tool installed before the move still works and its row
    says which folder it came from. No install writes here again.
    """
    return DEPS_DIR


def tools_dirs(music_folder=None):
    """Every folder a tool install may be in, the current one first.

    One entry when the two are the same folder, which is what an install with
    no music folder configured sees (both functions answer DEPS_DIR then).
    """
    current = tools_dir(music_folder)
    if os.path.normcase(os.path.abspath(current)) == os.path.normcase(
            os.path.abspath(DEPS_DIR)):
        return [current]
    return [current, DEPS_DIR]


def ensure_data_dirs():
    """Create the tools folder and verify the app folder is writable (so
    config.json can be saved there). Both the portable and the installed
    versions keep everything in their own folder.

    Returns None when OK, or a human-readable error string when the folder
    is not writable.
    """
    try:
        os.makedirs(tools_dir(), exist_ok=True)
        probe = os.path.join(SCRIPT_DIR, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("")
        try:
            os.remove(probe)
        except OSError:
            pass
        return None
    except OSError as e:
        return f"{e}"


AUDIO_EXTS = (".flac", ".ogg", ".opus", ".aac", ".m4a", ".mp3",
              # WAV/AIFF are library AUDIO when the codec target is one of
              # them (library_codec = wav/aiff): a converted file nothing
              # grades, tags or scans is not a library file, and the whole
              # point of that setting is a PCM library. They were export-only
              # before, which is why they were absent here.
              ".wav", ".aif", ".aiff")

# Music-video containers the library treats as first-class tracks. They
# appear in album tracklists (a disc of music videos is still a disc),
# play in the app's video player, and can be tagged / graded / remuxed.
# MP4/M4V are already taggable via mutagen; the rest are read and written
# through ffprobe/ffmpeg (see mlo.audio).
LIB_VIDEO_EXTS = (
    ".mp4", ".m4v", ".mkv", ".webm", ".mov",
    ".vob", ".mpg", ".mpeg", ".m2v", ".ts", ".m2ts", ".mts",
    ".avi", ".wmv", ".flv", ".ogv", ".3gp", ".3g2",
)

# Audio (+ music-video) extensions the library treats as tracks. MP4/M4A
# carry taggable AAC or FLAC audio, so album discovery and grading see them
# as music — a folder with only music videos is still an album, and music
# videos inside an album don't fail the "disallowed file types" check.
# Engine scripts (audit/DR/etc.) keep walking the stricter AUDIO_EXTS.
LIB_AUDIO_EXTS = AUDIO_EXTS + LIB_VIDEO_EXTS


def is_video_file(path) -> bool:
    """Whether *path* is a music-video container the library supports."""
    return os.path.splitext(str(path))[1].lower() in LIB_VIDEO_EXTS


# Disc structure folders the library recognizes: a folder holding one of these
# is a DVD-Video (VIDEO_TS) or Blu-ray (BDMV) rip rather than a stray folder,
# and its streams are put back in playback order before the remux
# (mlo.videodisc). This is the vocabulary the layout scan judges a folder by,
# next to LIB_VIDEO_EXTS for files.
LIB_VIDEO_DISC_DIRS = ("VIDEO_TS", "BDMV")
LIB_VIDEO_DISC_NAMES = frozenset(d.upper() for d in LIB_VIDEO_DISC_DIRS)


def is_video_disc_dir(path) -> bool:
    """Whether *path* is one of the disc structure folders above."""
    return os.path.basename(os.path.normpath(str(path))).upper() in LIB_VIDEO_DISC_NAMES


IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".jxl")

# All image types that Pillow can read and that we can convert to JPEG/PNG
# Lossy types: jpg, jpeg, webp (lossy), avif, heic, heif
# Lossless types: png, bmp, gif, tiff, tif, webp (lossless), ppm, pgm, pbm, avif (lossless)
ALL_IMAGE_EXTS = (
    ".jpg", ".jpeg", ".png", ".jxl",
    ".bmp", ".gif", ".tiff", ".tif", ".webp", ".avif", ".heic", ".heif",
    ".ppm", ".pgm", ".pbm", ".svg",
)

# Lossless source types that we consider for lossless-to-PNG conversion
LOSSLESS_IMAGE_EXTS = (".png", ".bmp", ".gif", ".tiff", ".tif", ".ppm", ".pgm", ".pbm")

VALID_EXTENSIONS = IMAGE_EXTS
CONVERTIBLE_EXTENSIONS = ALL_IMAGE_EXTS

# Sidecar track covers: for a track like "01 - Song.flac" a sidecar cover
# is an image file in the same album folder with the same basename but an
# image extension, e.g. "01 - Song.jpg" / ".png" / ".jxl". This lets a few
# tracks have their own art while the rest fall back to the album cover.*.
SIDECAR_COVER_EXTS = IMAGE_EXTS

# Text sidecars the app itself writes into the library: the album's
# "<album>/description.txt" (album page → fetch album description) and the
# artist's "<artist>/description.txt". Both are legitimate library files, so
# grading and the layout scanner must never report them as strays. The
# artist's "artist.jpg"/"artist.png" sit at artist level as well (see
# mlo.artistdata); artwork.json is app state and lives under .mlo/data.
ALBUM_SIDECAR_NAMES = ("description.txt",)


def get_sidecar_cover_path(album_dir, track_filename):
    """Return the sidecar cover path for a track if it exists, else None.

    Checks for an image file in *album_dir* whose basename matches the track's
    basename (without extension) and whose extension is in SIDECAR_COVER_EXTS.
    Case-insensitive, first match wins in SIDECAR_COVER_EXTS order.
    """
    base = os.path.splitext(track_filename)[0]
    # Also handle track_filename that may already be a full path
    base = os.path.basename(base)
    # The case-insensitive fallback needs the directory listing; fetch it at
    # most once per call instead of once per candidate extension.
    listing = None
    for ext in SIDECAR_COVER_EXTS:
        cand = os.path.join(album_dir, base + ext)
        # Case-insensitive check on Windows, but be explicit for cross-platform
        if os.path.isfile(cand):
            return cand
        # Try case-insensitive match if exact case fails (e.g. .JPG vs .jpg)
        if listing is None:
            try:
                listing = os.listdir(album_dir)
            except OSError:
                listing = []
        want = (base + ext).lower()
        for f in listing:
            if f.lower() == want:
                cand2 = os.path.join(album_dir, f)
                if os.path.isfile(cand2):
                    return cand2
    return None


def is_sidecar_cover_file(album_dir, filename, all_track_basenames=None):
    """Whether *filename* in *album_dir* is a sidecar track cover.

    *filename* is a single filename (e.g. "01 - Song.jpg"). It is a sidecar
    if its basename (without ext) matches any track basename in the album
    (minus extension) and its extension is an image type. The album's
    standard cover.* files are *not* considered sidecars.
    """
    low = filename.lower()
    if low in ("cover.jpg", "cover.jpeg", "cover.png", "cover.jxl"):
        return False
    base, ext = os.path.splitext(filename)
    if ext.lower() not in SIDECAR_COVER_EXTS:
        return False
    if all_track_basenames is None:
        return True  # conservative: any image that is not cover.* could be sidecar
    return base.lower() in {b.lower() for b in all_track_basenames}


# Per-track cover manifest: one image can be the art of several tracks
# without copying bytes. A dotfile so grading (which ignores hidden names)
# never sees it as library content or as stray artwork.
TRACK_COVERS_FILE = ".mlo_covers.json"


def _track_covers_path(album_dir):
    return os.path.join(album_dir, TRACK_COVERS_FILE)


def _album_file(album_dir, filename):
    """Path of *filename* inside *album_dir* when it exists (case-insensitive
    on any filesystem), else None."""
    name = os.path.basename(str(filename or ""))
    if not name:
        return None
    cand = os.path.join(album_dir, name)
    if os.path.isfile(cand):
        return cand
    low = name.lower()
    try:
        for f in os.listdir(album_dir):
            if f.lower() == low and os.path.isfile(os.path.join(album_dir, f)):
                return os.path.join(album_dir, f)
    except OSError:
        pass
    return None


# album dir -> (stamp, mapping). The grader asks for the same album's map once
# per track (sidecar cover check + stray-image scan), so without this an
# n-track album parsed the same JSON n+1 times; the stamp makes a write by this
# process — or another one — visible immediately.
_TRACK_COVERS_CACHE = {}


def load_track_covers(album_dir):
    """The album's track -> image filename map; {} when missing or malformed."""
    path = _track_covers_path(album_dir)
    try:
        stat = os.stat(path)
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    cached = _TRACK_COVERS_CACHE.get(path)
    if cached and cached[0] == stamp:
        return dict(cached[1])
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        tracks = data.get("tracks")
    except Exception:
        _TRACK_COVERS_CACHE[path] = (stamp, {})
        return {}
    if not isinstance(tracks, dict):
        _TRACK_COVERS_CACHE[path] = (stamp, {})
        return {}
    out = {str(k): str(v) for k, v in tracks.items() if k and v}
    _TRACK_COVERS_CACHE[path] = (stamp, out)
    return dict(out)


def save_track_covers(album_dir, mapping):
    """Atomically write the manifest; an empty mapping removes the file, so an
    album with no per-track art leaves nothing behind."""
    mapping = {str(k): str(v) for k, v in (mapping or {}).items() if k and v}
    dest = _track_covers_path(album_dir)
    _TRACK_COVERS_CACHE.pop(dest, None)
    if not mapping:
        try:
            os.remove(dest)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True
    try:
        fd, tmp = tempfile.mkstemp(prefix=".mlo_covers_", suffix=".tmp", dir=album_dir)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "tracks": mapping}, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
        fsync_dir(album_dir)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


# The MusicBrainz release's own tracklist, recorded at import time. An album
# imported PARTIALLY (one track of twelve) has no other way to know what is
# missing — the files on disk only describe themselves. Same dotfile
# treatment as the covers manifest: grading ignores hidden names, so this is
# never read as library content or as stray artwork.
EXPECTED_TRACKS_FILE = ".mlo_expected.json"


def _expected_tracks_path(album_dir):
    return os.path.join(album_dir, EXPECTED_TRACKS_FILE)


def _clean_expected(tracks):
    """Normalise a release tracklist to [{disc, position, title,
    recording_mbid}], dropping entries with no usable position."""
    rows = []
    for t in tracks or []:
        if not isinstance(t, dict):
            continue
        try:
            disc = int(t.get("disc") or 1)
            pos = int(t.get("position") or 0)
        except (TypeError, ValueError):
            continue
        if pos <= 0:
            continue
        rows.append({
            "disc": max(1, disc),
            "position": pos,
            "title": str(t.get("title") or ""),
            "recording_mbid": str(t.get("recording_mbid") or "") or None,
        })
    return rows


def load_expected_tracks(album_dir):
    """The recorded release tracklist as {"release_id", "tracks"}; empty when
    the album has no manifest or it is malformed."""
    empty = {"release_id": None, "tracks": []}
    try:
        with open(_expected_tracks_path(album_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return empty
    rows = _clean_expected(data.get("tracks"))
    rid = data.get("release_id")
    return {"release_id": str(rid) if rid else None, "tracks": rows}


def save_expected_tracks(album_dir, release_id, tracks):
    """Atomically write the release tracklist; an empty list removes the
    file, so a fully-present album with no manifest leaves nothing behind."""
    rows = _clean_expected(tracks)
    dest = _expected_tracks_path(album_dir)
    if not rows:
        try:
            os.remove(dest)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True
    try:
        fd, tmp = tempfile.mkstemp(prefix=".mlo_expected_", suffix=".tmp", dir=album_dir)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "release_id": str(release_id or "") or None,
                       "tracks": rows}, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
        fsync_dir(album_dir)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


# A FRAMEWORK album: the folder "Add to library" creates the moment the user
# asks for a MusicBrainz release, before any audio exists. It holds the
# release's own tracklist (the manifest above), the release-group cover and
# this marker, which is what every reader keys on: the library scan lists the
# folder as PENDING while it is here, and the import that fills the folder
# clears it. Same dotfile treatment as the manifest, so grading never reads it
# as library content or as stray artwork.
PENDING_FILE = ".mlo_pending.json"


def _pending_path(album_dir):
    return os.path.join(album_dir, PENDING_FILE)


def load_pending(album_dir):
    """The framework marker as a dict, or None when the folder is not one."""
    try:
        with open(_pending_path(album_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def save_pending(album_dir, info):
    """Atomically write the framework marker; a falsy *info* clears it."""
    if not info:
        return clear_pending(album_dir)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".mlo_pending_", suffix=".tmp", dir=album_dir)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dict(info, version=1), fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _pending_path(album_dir))
        fsync_dir(album_dir)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def clear_pending(album_dir):
    """Remove the framework marker; True when the folder holds none after."""
    try:
        os.remove(_pending_path(album_dir))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


# Windows codes a move retries instead of giving up on: 32 (sharing
# violation — something still holds the file) and 33 (lock violation).
_LOCK_WINERRORS = (32, 33)
# winerror 17 ERROR_NOT_SAME_DEVICE / errno EXDEV: the one case where a move
# legitimately copies bytes instead of renaming.
_CROSS_DEVICE_WINERRORS = (17,)
_CROSS_DEVICE_ERRNOS = (errno.EXDEV,)

# Every rename goes through this name so a single-volume test can stand in
# for a cross-volume move.
_replace = os.replace


class _SizeMismatch(OSError):
    """A copy landed with the wrong byte count: redo the copy, keep the source."""


def _is_locked(exc):
    """Whether *exc* is a transient sharing/lock violation worth a retry."""
    if isinstance(exc, (PermissionError, _SizeMismatch)):
        return True
    if getattr(exc, "winerror", None) in _LOCK_WINERRORS:
        return True
    return getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM, errno.EBUSY, errno.ETXTBSY)


def _is_cross_device(exc):
    """Whether *exc* says the rename crossed volumes (winerror 17 / EXDEV)."""
    return (getattr(exc, "winerror", None) in _CROSS_DEVICE_WINERRORS
            or getattr(exc, "errno", None) in _CROSS_DEVICE_ERRNOS)


def _verify_copy(src, dst, is_dir):
    """Raise _SizeMismatch unless *dst* holds every byte of *src*."""
    if not is_dir:
        got, want = os.path.getsize(dst), os.path.getsize(src)
        if got != want:
            raise _SizeMismatch(f"{dst} has {got} bytes, expected {want}")
        return
    for root, _dirs, files in os.walk(src):
        for name in files:
            here = os.path.join(root, name)
            there = os.path.join(dst, os.path.relpath(here, src))
            try:
                got = os.path.getsize(there)
            except OSError:
                raise _SizeMismatch(f"{there} is missing from the copy") from None
            want = os.path.getsize(here)
            if got != want:
                raise _SizeMismatch(f"{there} has {got} bytes, expected {want}")


def _discard(path, is_dir):
    """Best-effort removal of a half-made copy; never touches the source."""
    try:
        if is_dir:
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.remove(path)
    except OSError:
        pass


def fsync_dir(path):
    """Best-effort durable directory entry after an os.replace.

    On POSIX the rename itself only survives a power cut once the parent
    directory is fsynced. Windows has no such call — ``os.O_DIRECTORY`` does
    not exist there, so the ``os.open(dir, os.O_DIRECTORY)`` every caller
    used raised AttributeError per written file, which the blanket ``except
    Exception`` around it swallowed: one failed call each time, and the
    durability step the code reads as performed. Returns True when the
    directory really was synced.
    """
    flags = getattr(os, "O_DIRECTORY", None)
    if flags is None:
        return False
    fd = None
    try:
        fd = os.open(path or ".", flags)
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _copy_across_volumes(src, dst, step):
    """Copy *src* to a temp name beside *dst*, verify it, place it, then remove
    the source. The source is untouched until the destination holds every byte,
    so a failed copy can never lose data."""
    is_dir = os.path.isdir(src)
    tmp = f"{dst}.mlo-tmp-{os.getpid()}-{os.urandom(4).hex()}"

    def copy():
        if is_dir:
            shutil.copytree(src, tmp, copy_function=shutil.copy2, dirs_exist_ok=True)
        else:
            shutil.copy2(src, tmp)
        _verify_copy(src, tmp, is_dir)
        return tmp

    try:
        step(copy, f"copy {src} -> {tmp}")
        step(lambda: _replace(tmp, dst), f"place {tmp} -> {dst}")
    except OSError:
        _discard(tmp, is_dir)
        raise
    step(lambda: shutil.rmtree(src) if is_dir else os.remove(src), f"remove {src}")


def move_path(src, dst, *, attempts=40, delay=0.5, log=None) -> bool:
    """Move *src* to *dst* without ever degrading a rename into a silent copy.

    Exists because shutil.move() did exactly that on a real import: the album
    rename hit WinError 32 (a file inside was still open by slskd), so
    shutil.move quietly fell back to copytree() + rmtree() — the album ended up
    duplicated in the library AND in the download folder, and deleting the
    still-open file raised. Here a same-volume move is os.replace() and nothing
    else, retried on sharing/lock violations; only a genuine cross-volume error
    (winerror 17 / EXDEV) switches to a size-verified copy whose source is
    removed last.

    *attempts* tries, *delay* seconds apart. *log(msg)* hears about the first
    retry and about giving up. Returns True only when *dst* exists and *src* is
    gone (or *src* and *dst* are the same path in a different case).
    """
    src, dst = os.fspath(src), os.fspath(dst)
    attempts = max(1, int(attempts))
    same_path = os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst))
    said = set()

    def say(key, msg):
        if log is None or key in said:
            return
        said.add(key)
        try:
            log(msg)
        except Exception:
            pass

    # A move implies the destination's FOLDER: without this, a fresh install
    # (no Artists/ yet) cannot import its first album — the rename fails
    # ENOENT and surfaces as the misleading "a file inside it is still in
    # use". Every caller places something AT *dst*, so the parent is required
    # either way; creating it here is strictly more forgiving than failing.
    parent = os.path.dirname(os.path.abspath(dst))
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            say("fail", f"could not create {parent}: {exc}")
            return False

    def step(run, what):
        for i in range(1, attempts + 1):
            try:
                return run()
            except OSError as exc:
                if i >= attempts or not _is_locked(exc):
                    raise
                say("retry", f"{what}: {exc} (attempt {i}/{attempts})")
                if delay:
                    time.sleep(delay)

    def move():
        try:
            _replace(src, dst)
            return
        except OSError as exc:
            if not _is_cross_device(exc):
                raise
        _copy_across_volumes(src, dst, step)

    try:
        step(move, f"move {src} -> {dst}")
    except OSError as exc:
        say("fail", f"move {src} -> {dst} gave up after {attempts} attempt(s): {exc}")
        return False
    return os.path.exists(dst) and (same_path or not os.path.exists(src))


def get_track_cover(album_dir, track_filename):
    """The image that is this track's art, or None.

    Manifest entry first (only when the image file is really there), then the
    same-stem sidecar probe; None means the caller falls back to cover.*.
    """
    name = os.path.basename(str(track_filename))
    low = name.lower()
    for track, image in load_track_covers(album_dir).items():
        if track.lower() == low:
            found = _album_file(album_dir, image)
            if found:
                return found
    return get_sidecar_cover_path(album_dir, name)


def set_track_covers(album_dir, tracks, image_filename):
    """Point every track in *tracks* at ONE image already in the album folder.

    No bytes are copied, linked or duplicated — the tracks share the file.
    A missing image is refused (returns the unchanged map) rather than stored
    as a dangling entry. Returns the resulting map.
    """
    mapping = load_track_covers(album_dir)
    found = _album_file(album_dir, image_filename)
    if not found:
        return mapping
    image = os.path.basename(found)
    for t in tracks or []:
        name = os.path.basename(str(t))
        if name:
            mapping[name] = image
    save_track_covers(album_dir, mapping)
    return mapping


def clear_track_covers(album_dir, tracks=None):
    """Drop *tracks* from the manifest (None = drop every entry). The image
    files themselves are never deleted. Returns the resulting map."""
    if tracks is None:
        save_track_covers(album_dir, {})
        return {}
    mapping = load_track_covers(album_dir)
    drop = {os.path.basename(str(t)).lower() for t in tracks}
    out = {k: v for k, v in mapping.items() if k.lower() not in drop}
    save_track_covers(album_dir, out)
    return out


# ".mlo" hides the whole app folder (data/ included); ".mlo_data" stays
# listed so an install that has not migrated yet is hidden too.
SKIP_DIRS = {".dependencies", ".mlo", ".mlo_data", ".mlo_trash", ".data", "data", "__pycache__", "$RECYCLE.BIN",
             "System Volume Information", ".git", ".thumbnails", ".tmp"}


def prune_empty_dirs(root):
    """Delete every directory under *root* that holds nothing at all.

    Organize and removal empty folders and never remove them, so the library
    grows artist/album/disc shells that only the layout report complains about.
    `rmdir` bottom-up is the whole rule: it succeeds only on a directory that
    holds NOTHING — a stray file, a cover or a dot-dir keeps itself and every
    parent alive — and *root* itself is never touched, so a caller can hand
    over the library without losing the folder it starts from. SKIP_DIRS
    entries (app state) and dot-dirs are left standing, and that includes
    everything below them: the walk is pruned top-down so it never even looks
    inside.

    Returns the removed paths, deepest first, so the caller can log a count.
    A directory that cannot be listed or removed is skipped rather than
    raised: housekeeping must not fail the run that triggered it.
    """
    if not root or not os.path.isdir(root):
        return []
    # topdown keeps the walk out of dot-dirs/SKIP_DIRS; the list is built
    # parents-first, so it is walked in REVERSE to empty a chain bottom-up
    empties = []
    for base, dirs, _files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
        empties += [os.path.join(base, d) for d in dirs]
    removed = []
    for path in reversed(empties):
        try:
            os.rmdir(path)
        except OSError:
            continue
        removed.append(path)
    return removed


JPEG_QUALITY_MARKER = 1


PNG_OPTIMIZATION_LEVEL = 2


DEFAULT_DIGITAL_SOURCE = "Digital"

