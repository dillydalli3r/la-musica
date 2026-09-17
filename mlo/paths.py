"""Filesystem locations and format constants.

When frozen (PyInstaller) SCRIPT_DIR points at the exe's folder so that
config.json and the .dependencies toolchain live next to the executable.
"""
import errno
import json
import os
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
# downloads/ (completed transfers), incomplete/ (in-flight ones), trash/.
# The old top-level <music>/.mlo_data is now only a migration source
# (LEGACY_MLO_DATA_DIR_NAME below).
ARTISTS_DIR_NAME = "Artists"
MLO_DIR_NAME = ".mlo"
MLO_DATA_DIR_NAME = "data"
DOWNLOADS_DIR_NAME = "downloads"
INCOMPLETE_DIR_NAME = "incomplete"
TRASH_DIR_NAME = "trash"

# The pre-.mlo state dir inside the music folder: still read by the
# migration, still skipped by the scanner, never written to.
LEGACY_MLO_DATA_DIR_NAME = ".mlo_data"


def read_music_folder_guess():
    """Best-effort music folder from whichever config file exists.

    Used to locate the state directory before and after the migration: the
    legacy repo-local config.json knows the music folder, and after the
    move a stub remains behind at the legacy path for the same purpose."""
    mf = os.environ.get("MLO_MUSIC_FOLDER")
    if mf:
        return mf
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            mf = (json.load(f) or {}).get("music_folder")
        if mf:
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
    onto it. The music folder ROOT stays free for the app state dirs (and
    for the Soulseek share), so a library path must never be built from the
    music folder itself."""
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


def downloads_dir(music_folder=None):
    """Where slskd saves COMPLETED files: <music folder>/.mlo/downloads
    (None = no music folder; the caller decides the fallback)."""
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


def trash_dir(music_folder=None):
    """Remove-from-library bin: <music folder>/.mlo/trash (None = no music
    folder)."""
    root = mlo_root(music_folder)
    return os.path.join(root, TRASH_DIR_NAME) if root else None


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


DEPS_DIR = os.path.join(SCRIPT_DIR, ".dependencies")


def ensure_data_dirs():
    """Create .dependencies/ next to the app and verify the app folder is
    writable (so config.json can be saved there). Both the portable and the
    installed versions keep everything in their own folder.

    Returns None when OK, or a human-readable error string when the folder
    is not writable.
    """
    try:
        os.makedirs(DEPS_DIR, exist_ok=True)
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


AUDIO_EXTS = (".flac", ".ogg", ".opus", ".aac", ".m4a", ".mp3")

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


def load_track_covers(album_dir):
    """The album's track -> image filename map; {} when missing or malformed."""
    try:
        with open(_track_covers_path(album_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        tracks = data.get("tracks")
    except Exception:
        return {}
    if not isinstance(tracks, dict):
        return {}
    return {str(k): str(v) for k, v in tracks.items() if k and v}


def save_track_covers(album_dir, mapping):
    """Atomically write the manifest; an empty mapping removes the file, so an
    album with no per-track art leaves nothing behind."""
    mapping = {str(k): str(v) for k, v in (mapping or {}).items() if k and v}
    dest = _track_covers_path(album_dir)
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
        os.replace(tmp, dest)
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
        os.replace(tmp, dest)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


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


JPEG_QUALITY_MARKER = 1


PNG_OPTIMIZATION_LEVEL = 2


DEFAULT_DIGITAL_SOURCE = "Digital"

