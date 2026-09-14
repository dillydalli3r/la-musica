"""Filesystem locations and format constants.

When frozen (PyInstaller) SCRIPT_DIR points at the exe's folder so that
config.json and the .dependencies toolchain live next to the executable.
"""
import json
import os
import sys

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


# Legacy locations (repo-local) that predate the .data folder; a stub
# config.json left behind by the migration keeps pointing at the music dir.
LEGACY_DATA_DIR = os.path.join(SCRIPT_DIR, "server", "data")


def read_music_folder_guess():
    """Best-effort music folder from whichever config file exists.

    Used to locate the .data directory before and after the migration: the
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


def app_data_dir(music_folder=None):
    """The folder holding ALL app state: <music folder>/Data.

    The music folder is formatted as <music>/Artists (the library) +
    <music>/Data (this folder). Falls back to the legacy repo-local
    server/data only while no music folder is configured (fresh setup)."""
    mf = music_folder or read_music_folder_guess()
    if mf:
        return os.path.join(mf, "Data")
    return LEGACY_DATA_DIR


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


SKIP_DIRS = {".dependencies", ".mlo_trash", ".data", "data", "__pycache__", "$RECYCLE.BIN",
             "System Volume Information", ".git", ".thumbnails", ".tmp"}


JPEG_QUALITY_MARKER = 1


PNG_OPTIMIZATION_LEVEL = 2


DEFAULT_DIGITAL_SOURCE = "Digital"

