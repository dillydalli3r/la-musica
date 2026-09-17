"""Artist artwork and descriptions stored INSIDE the library.

On-disk layout (everything under the configured music folder)::

    <music>/Artists/<Artist>/artist.jpg      # artist image (artist.png when
    <music>/Artists/<Artist>/artist.png      #   the source is a PNG / fmt="png")
    <music>/Artists/<Artist>/description.txt # artist description, UTF-8 text
    <album folder>/description.txt           # album description, UTF-8 text
    <music>/.mlo/data/artwork.json           # provenance for BOTH

``artwork.json`` is a flat map keyed by the LOWERCASED, forward-slashed folder
path::

    {"f:/music/artists/foo": {"kind": "artist", "source": "deezer",
                              "source_url": "https://...", "label": "Deezer",
                              "fetched": "2026-01-01T00:00:00Z",
                              "title": "Foo", "updated": "..."}}

Both the image and the description of one folder share that single entry, so
clearing the image never discards the description's provenance.

Image rules: the bytes must be a real image (Pillow), then they are cropped to
the configured cover aspect when ``artist_image_crop`` is on AND covers are
configured to be squarish (``cover_crop_enabled``) — the cover pipeline's
near-square tolerance (``cover_crop_threshold``) applies, so an already-square
photo is left untouched — resized so the longest side is
``artist_image_target_size`` only when that is > 0 (0 keeps the provider's
native size; a small image is NEVER upscaled, never rejected and has no
minimum-resolution requirement), and re-encoded at ``cover_jpeg_quality`` (PNG
when ``fmt="png"``) with metadata dropped. Writes are atomic and the other
``artist.*`` variant is removed.

ponytail: the crop/resize/encode path here is a small local PIL routine rather
than mlo.images._resize_and_crop_image / _prepare_image_streamlined, which are
cover-file (path in, path out) helpers driven by ``cover_*`` config keys; the
crop policy above now reads those keys, so if the two ever need to share more
than that, route this through those helpers instead of growing this one.
"""
import io
import json
import os
import tempfile
import threading
import time

from .deps import HAS_PIL, Image

try:
    from PIL import ImageOps
except ImportError:
    ImageOps = None

from .paths import MLO_DATA_DIR_NAME, MLO_DIR_NAME, app_data_dir, library_root

# Stems an artist image may use ("artist.jpg", "Artist.PNG", ...).
ARTIST_IMAGE_STEMS = ("artist",)
# Image extensions the artist image may carry on disk.
ARTIST_IMAGE_EXTS = (".jpg", ".jpeg", ".png")
DESCRIPTION_NAME = "description.txt"
# Provenance map for artist images + album/artist descriptions.
PROVENANCE_NAME = "artwork.json"

# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def _music_folder(cfg):
    """The configured music folder, or None."""
    return str(cfg.get("music_folder")) if cfg and cfg.get("music_folder") else None


def _norm_key(folder):
    """Provenance key for *folder*: forward slashes, lowercased (Windows)."""
    return os.path.normpath(str(folder)).replace("\\", "/").lower()


def _provenance_path(cfg, folder=None):
    """<music>/.mlo/data/artwork.json.

    Falls back to the ``.mlo`` dir of the library *folder* hands us when the
    caller has no config (a cfg-less call must still find the map it wrote),
    then to the legacy app-data dir."""
    mf = _music_folder(cfg)
    if mf:
        return os.path.join(app_data_dir(mf), PROVENANCE_NAME)
    here = str(folder or "")
    while here:
        parent = os.path.dirname(here)
        if parent == here:
            break
        cand = os.path.join(parent, MLO_DIR_NAME, MLO_DATA_DIR_NAME)
        if os.path.isdir(cand):
            return os.path.join(cand, PROVENANCE_NAME)
        here = parent
    return os.path.join(app_data_dir(None), PROVENANCE_NAME)


# The provenance map is ONE file shared by every artist/album folder, and the
# library payload asks for a folder's entry once per album. Without a cache
# that is N reads+parses of the same JSON per payload build (quadratic on big
# libraries), so the parsed map is memoised on (path, mtime, size) — a write
# through save_image/write_description replaces the file, which changes the
# mtime and invalidates the entry.
_MAP_CACHE = {"key": None, "data": {}}
_MAP_LOCK = threading.Lock()


def _load_map(cfg, folder=None):
    path = _provenance_path(cfg, folder)
    try:
        st = os.stat(path)
        key = (os.path.normcase(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _MAP_LOCK:
        if _MAP_CACHE["key"] == key:
            return _MAP_CACHE["data"]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    with _MAP_LOCK:
        _MAP_CACHE.update(key=key, data=data)
    return data


def _save_map(cfg, data, folder=None):
    """Atomic write of the whole map; an empty map leaves no file behind."""
    path = _provenance_path(cfg, folder)
    if not data:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".artwork_", suffix=".tmp",
                                   dir=os.path.dirname(path))
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_provenance(folder, cfg=None) -> dict:
    """Provenance entry recorded for *folder* ({} when never written)."""
    entry = _load_map(cfg, folder).get(_norm_key(folder))
    return dict(entry) if isinstance(entry, dict) else {}


def write_provenance(folder, patch, kind="artist", cfg=None) -> dict:
    """Merge *patch* into *folder*'s provenance entry; returns the entry.

    ``updated`` is stamped on every write, ``fetched`` only the first time (or
    when the patch names a different source), so a re-fetch of unchanged data
    is distinguishable from the original fetch.
    """
    key = _norm_key(folder)
    data = _load_map(cfg, folder)
    entry = data.get(key) if isinstance(data.get(key), dict) else {}
    patch = {k: v for k, v in dict(patch or {}).items() if v is not None}
    if patch.get("source") and patch["source"] != entry.get("source"):
        patch.setdefault("fetched", _now())
    entry.update(patch)
    entry.setdefault("fetched", _now())
    entry["kind"] = kind or entry.get("kind") or "artist"
    entry["updated"] = _now()
    data[key] = entry
    _save_map(cfg, data, folder)
    return dict(entry)


def _drop_provenance(folder, cfg=None):
    key = _norm_key(folder)
    data = _load_map(cfg, folder)
    if key in data:
        del data[key]
        _save_map(cfg, data, folder)


def artist_dir(cfg, artist):
    """The library folder of *artist*, or None when it does not exist.

    Names with a path separator (or a bare "." / "..") are refused: the artist
    comes from a provider payload / the UI and must never escape the library
    root. Lookup is case-insensitive, the way the rest of the library handles
    Windows paths."""
    name = str(artist or "").strip()
    if not name or name in (".", ".."):
        return None
    if any(sep in name for sep in ("/", "\\")) or ":" in name:
        return None
    root = library_root(_music_folder(cfg))
    if not root:
        return None
    low = name.lower()
    try:
        entries = os.listdir(root)
    except OSError:
        entries = []
    # Real on-disk spelling first: the folder name is what the UI shows and
    # what gets written into provenance, so return what the disk says.
    fallback = None
    for entry in entries:
        if entry == name and os.path.isdir(os.path.join(root, entry)):
            return os.path.join(root, entry)
        if entry.lower() == low and fallback is None and os.path.isdir(os.path.join(root, entry)):
            fallback = os.path.join(root, entry)
    if fallback:
        return fallback
    direct = os.path.join(root, name)
    return direct if os.path.isdir(direct) else None


# --------------------------------------------------------------------------- #
# images
# --------------------------------------------------------------------------- #
def _image_files(folder):
    """Existing ``artist.*`` image files in *folder*, preferred order first."""
    order = {ext: i for i, ext in enumerate(ARTIST_IMAGE_EXTS)}
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        stem, ext = os.path.splitext(name)
        if stem.lower() in ARTIST_IMAGE_STEMS and ext.lower() in order:
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                out.append((order[ext.lower()], path))
    out.sort()
    return [p for _, p in out]


def image_path(folder):
    """The artist image in *folder*, or None."""
    files = _image_files(folder)
    return files[0] if files else None


def has_image(folder) -> bool:
    """Whether *folder* holds an artist image."""
    return image_path(folder) is not None


def clear_image(folder) -> bool:
    """Remove every ``artist.*`` image from *folder*; True when one went away."""
    removed = False
    for path in _image_files(folder):
        try:
            os.remove(path)
            removed = True
        except OSError:
            pass
    return removed


def _flatten(img):
    """*img* in a saveable mode, alpha flattened onto white for opaque output."""
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _prepare(img, crop, target, threshold=0.0):
    """The artist image transform: crop to the configured aspect, then
    downscale only.

    The "configured aspect ratio" is the cover policy's: square, enforced with
    the same near-square tolerance covers use (`cover_crop_threshold`), so a
    1200×1200 photo is left alone while a 16:9 still gets its centre square.
    Nothing is ever upscaled and no minimum size is imposed — a 200px artist
    photo is a perfectly good artist photo.
    """
    if ImageOps is not None:
        try:
            img = ImageOps.exif_transpose(img) or img
        except Exception:
            pass
    if crop:
        w, h = img.size
        side = min(w, h)
        deviation = (abs(w - h) / float(max(w, h))) if max(w, h) else 0.0
        if side > 0 and w != h and deviation > threshold:
            left, top = (w - side) // 2, (h - side) // 2
            img = img.crop((left, top, left + side, top + side))
    if target > 0:
        w, h = img.size
        longest = max(w, h)
        if longest > target:  # never upscale
            scale = target / float(longest)
            size = (max(1, round(w * scale)), max(1, round(h * scale)))
            resample = getattr(Image, "Resampling", Image).LANCZOS
            img = img.resize(size, resample)
    return img


def _target_size(cfg):
    try:
        return max(0, min(4000, int(cfg.get("artist_image_target_size") or 0)))
    except (TypeError, ValueError):
        return 0


def _crop_policy(cfg):
    """(crop, threshold) for artist images from the cover settings.

    `artist_image_crop` is the artist-specific switch; the *aspect* it crops to
    is whatever the cover pipeline is configured to enforce — when the user
    turned square covers off, artist images keep their native aspect too
    instead of being silently squared. The threshold is the cover pipeline's
    near-square tolerance, so "already square enough" images are not touched.
    """
    crop = bool(cfg.get("artist_image_crop", True)) and bool(cfg.get("cover_crop_enabled", True))
    try:
        threshold = max(0.0, min(0.5, float(cfg.get("cover_crop_threshold") or 0.0)))
    except (TypeError, ValueError):
        threshold = 0.0
    return crop, threshold


def save_image(folder, data, cfg, source=None, source_url=None, kind="artist",
               fmt=None, label=None, title=None) -> str or None:
    """Normalize *data* into *folder* as ``artist.jpg`` / ``artist.png``.

    Returns the written path, or None when the bytes are not a decodable image,
    *folder* does not exist or Pillow is unavailable."""
    if not HAS_PIL or not data or not folder or not os.path.isdir(folder):
        return None
    cfg = cfg or {}
    png = str(fmt or "").lower() == "png"
    try:
        with Image.open(io.BytesIO(data)) as raw:
            raw.load()
            img = raw.copy()
    except Exception:
        return None
    try:
        crop, threshold = _crop_policy(cfg)
        img = _prepare(img, crop, _target_size(cfg), threshold)
        if not png:
            img = _flatten(img)
    except Exception:
        return None

    ext = ".png" if png else ".jpg"
    dest = os.path.join(folder, "artist" + ext)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".artist_", suffix=ext, dir=folder)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "wb") as fh:
            if png:
                img.save(fh, "PNG", optimize=True)
            else:
                try:
                    quality = max(70, min(100, int(cfg.get("cover_jpeg_quality") or 90)))
                except (TypeError, ValueError):
                    quality = 90
                img.save(fh, "JPEG", quality=quality, optimize=True)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    # Drop the other variant so the folder never holds two artist images.
    low = dest.lower()
    for path in _image_files(folder):
        if path.lower() != low:
            try:
                os.remove(path)
            except OSError:
                pass
    write_provenance(folder, {"source": source, "source_url": source_url,
                              "label": label, "title": title},
                     kind=kind, cfg=cfg)
    return dest


def segment_artist_image(cfg, artist, data, source=None, source_url=None,
                         fmt=None, label=None, title=None):
    """Save *data* as *artist*'s image; None when the artist folder is absent."""
    folder = artist_dir(cfg, artist)
    if not folder:
        return None
    return save_image(folder, data, cfg, source=source, source_url=source_url,
                      fmt=fmt, label=label, title=title)


# --------------------------------------------------------------------------- #
# descriptions
# --------------------------------------------------------------------------- #
def description_path(folder) -> str:
    """Path of *folder*'s ``description.txt`` (the on-disk name when present)."""
    try:
        for name in os.listdir(folder):
            if name.lower() == DESCRIPTION_NAME:
                path = os.path.join(folder, name)
                if os.path.isfile(path):
                    return path
    except OSError:
        pass
    return os.path.join(folder, DESCRIPTION_NAME)


def read_description(folder) -> str:
    """The stored description, or "" when absent/unreadable."""
    try:
        with open(description_path(folder), "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""
    except UnicodeDecodeError:
        return ""


def has_description(folder) -> bool:
    """Whether *folder* holds a non-blank description."""
    return bool(read_description(folder).strip())


def normalize_description(text) -> str:
    """Collapse blank-line runs, strip trailing spaces, ensure one final \\n."""
    lines = [ln.rstrip() for ln in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out, blanks = [], 0
    for ln in lines:
        if ln:
            blanks = 0
            out.append(ln)
            continue
        blanks += 1
        if blanks <= 1:  # at most one blank line in a row
            out.append("")
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + "\n" if out else ""


def write_description(folder, text, cfg=None, source=None, source_url=None,
                      kind="artist") -> str:
    """Atomically store *text* as *folder*'s description; "" when rejected.

    Whitespace-only text is rejected rather than blanking an existing file."""
    body = normalize_description(text)
    if not body.strip() or not folder or not os.path.isdir(folder):
        return ""
    dest = description_path(folder)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".description_", suffix=".tmp", dir=folder)
    except OSError:
        return ""
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        os.replace(tmp, dest)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return ""
    write_provenance(folder, {"source": source, "source_url": source_url},
                     kind=kind, cfg=cfg)
    return dest


def delete_description(folder, kind="artist", cfg=None) -> bool:
    """Remove *folder*'s description and its provenance entry."""
    path = description_path(folder)
    removed = False
    try:
        os.remove(path)
        removed = True
    except OSError:
        pass
    if removed:
        _drop_provenance(folder, cfg)
    return removed
