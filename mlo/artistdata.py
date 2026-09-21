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
                              "title": "Foo", "updated": "...",
                              "image_size": "1200x1200",
                              "description_source": "wikipedia",
                              "description_source_url": "https://...",
                              "description_title": "Foo"}}

Both the image and the description of one folder share that single entry, so
clearing the image never discards the description's provenance — which is why
the description's provider is recorded under ``description_*``: the plain
``source`` above belongs to the image.

Image rules: the bytes must be a real image (Pillow), then they are cropped to
``artist_image_aspect`` ("W:H", shipped as 1:1) when ``artist_image_crop`` is on
AND covers are configured with an aspect at all (``cover_crop_enabled``) — the
crop happens whenever the file's own ratio is further than ASPECT_TOLERANCE from
the configured one, so encoder rounding is left alone instead of being cropped
again on every fetch. Resized so the longest side is
``artist_image_target_size`` only when that is > 0 (0 keeps the provider's
native size; a small image is NEVER upscaled, never rejected and has no
minimum-resolution requirement), and re-encoded at ``cover_jpeg_quality`` (PNG
when ``fmt="png"``) with metadata dropped. The written ``WxH`` is recorded as
``image_size`` in the provenance entry: that is the only evidence the audit has
that a file on disk was enlarged after this app wrote it. Writes are atomic and
the other ``artist.*`` variant is removed.

Script 19 (``run_optimize_artist_images``) applies the same policy to the artist
images already in the library, so a photo the provider stored oversized or
off-aspect — or a file some other tool enlarged afterwards — is brought back to
the configured size, aspect and format. Nothing here ever upscales, and a file
that already passes is left byte for byte alone, so a run over a healthy library
changes nothing.

ponytail: the crop/resize/encode path here is a small local PIL routine rather
than mlo.images._resize_and_crop_image / _prepare_image_streamlined, which are
cover-file (path in, path out) helpers driven by ``cover_*`` config keys; the
policy above now reads the ``artist_image_*`` keys (with ``cover_crop_enabled``
as the "covers have an aspect at all" master switch), so if the two ever need to
share more than that, route this through those helpers instead of growing this
one.
"""
import io
import json
import os
import re
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
# The naming script's trailing MusicBrainz id: "[a16371b9-…]", "(a16371b9)"
# and the 8-char short form from `short_folder_names` all match.
_MBID_SUFFIX_RE = re.compile(r"\s*[\[(][0-9a-f][0-9a-f-]{6,34}[0-9a-f][\])]\s*$", re.I)
# A bare id (the `mb:<MBID>` form) with no brackets around it.
_MBID_ONLY_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Image extensions the artist image may carry on disk.
ARTIST_IMAGE_EXTS = (".jpg", ".jpeg", ".png")
DESCRIPTION_NAME = "description.txt"
# Provenance map for artist images + album/artist descriptions.
PROVENANCE_NAME = "artwork.json"

# The aspect artist images must have, as "width:height". The fetch
# (save_image), the audit (mlo.grader's artist image check) and script 19 all
# read the configured ``artist_image_aspect``, and this is what that key is
# validated back to when it holds something unparsable.
DEFAULT_ASPECT = "1:1"
# How far a stored image may sit from the configured aspect before it is a real
# mismatch. 2%: a 1000x1010 crop is encoder rounding (a rounded-off centre
# crop), anything past that is a different shape — the same bar the audit
# judges and the fix crops to, so the three can never disagree.
ASPECT_TOLERANCE = 0.02
# Longest side an artist image may have when ``artist_image_target_size`` is 0
# (= keep the provider's native size). Every provider this app fetches from
# serves well under this, so a bigger file is an outside source's oversize or a
# hand-placed scan rather than something the fetch wrote.
DEFAULT_MAX_SIDE = 2000
# The size artistdata records in provenance ("WxH") and reads back for the
# audit's upscale test.
IMAGE_SIZE_KEY = "image_size"

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


def strip_mbid_suffix(name):
    """*name* without the trailing MusicBrainz id in brackets.

    The naming script builds artist folders as "Slowdive [a16371b9-…]" (and
    ``short_folder_names`` truncates the id to 8 chars), so a lookup by the
    artist's NAME must strip that suffix or it finds nothing and every artist
    image / description silently misses the folder the artist page reads."""
    return _MBID_SUFFIX_RE.sub("", str(name or "")).strip()


def folder_mbid(name):
    """The full MusicBrainz artist id inside a folder name, or "".

    Only a full uuid is useful: `short_folder_names` truncates the suffix to 8
    chars, and a truncated id cannot key a provider lookup — the caller then
    falls back to a name search as before. `ref` may also be the artist PAGE's
    own `mb:<MBID>` form, which carries the id without brackets."""
    ref = str(name or "").strip()
    if ref.lower().startswith("mb:"):
        ref = ref[3:]
        return ref.lower() if _MBID_ONLY_RE.match(ref) else ""
    m = _MBID_SUFFIX_RE.search(ref)
    if not m:
        return ""
    digits = re.sub(r"[^0-9a-f]", "", m.group(0), flags=re.I).lower()
    if len(digits) != 32:
        return ""
    # Providers key on the canonical dashed form; the brackets may carry it
    # either way (a copied id, or the naming script's own spelling).
    return (f"{digits[:8]}-{digits[8:12]}-{digits[12:16]}-"
            f"{digits[16:20]}-{digits[20:]}")


def artist_dir(cfg, artist):
    """The library folder of *artist*, or None when it does not exist.

    Names with a path separator (or a bare "." / "..") are refused: the artist
    comes from a provider payload / the UI and must never escape the library
    root. Lookup is case-insensitive, the way the rest of the library handles
    Windows paths, and tolerates the naming script's MBID suffix (see
    :func:`strip_mbid_suffix`) — an artist's folder is named after their id."""
    name = str(artist or "").strip()
    if not name or name in (".", ".."):
        return None
    if any(sep in name for sep in ("/", "\\")) or ":" in name:
        return None
    root = library_root(_music_folder(cfg))
    if not root:
        return None
    want = {name.lower(), strip_mbid_suffix(name).lower()}
    try:
        entries = os.listdir(root)
    except OSError:
        entries = []
    # Real on-disk spelling first: the folder name is what the UI shows and
    # what gets written into provenance, so return what the disk says.
    fallback = None
    for entry in entries:
        if not os.path.isdir(os.path.join(root, entry)):
            continue
        if entry == name:
            return os.path.join(root, entry)
        if fallback is None and ({entry.lower(), strip_mbid_suffix(entry).lower()} & want):
            fallback = os.path.join(root, entry)
    if fallback:
        return fallback
    direct = os.path.join(root, name)
    return direct if os.path.isdir(direct) else None


# --------------------------------------------------------------------------- #
# images
# --------------------------------------------------------------------------- #
def _unsupported_image_files(folder):
    """The ``artist.*`` files in *folder* whose container the library does not
    read (``artist.webp``, ``artist.gif``, ``artist.bmp`` …) and that decode as
    images — the files script 19 converts.

    They have to decode to count: an ``artist.txt`` beside the artwork is not a
    wrong-format image, and reporting it as one would send the user looking for a
    converter."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in sorted(names):
        stem, ext = os.path.splitext(name)
        if stem.lower() not in ARTIST_IMAGE_STEMS or ext.lower() in ARTIST_IMAGE_EXTS:
            continue
        path = os.path.join(folder, name)
        if os.path.isfile(path) and decode_size(path) is not None:
            yield path


def unsupported_image(folder):
    """The first such file, or None — what the audit reports as the folder's
    wrong-format artist image."""
    return next(_unsupported_image_files(folder), None)


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


def parse_aspect(value):
    """The width/height ratio in *value* ("1:1", "4x5", "16/9"), or None.

    None means "not a ratio this app can use", which is the answer both the
    config validator (it stores the default) and the audit (it does not claim an
    aspect is wrong) need. A bare number is refused on purpose: the setting says
    width:height, and the settings field's own pattern accepts nothing else."""
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return None
    for sep in ("x", "/", "×"):
        text = text.replace(sep, ":")
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        ratio = float(parts[0]) / float(parts[1])
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if not (0.01 <= ratio <= 100.0):
        return None
    return ratio


def aspect_policy(cfg):
    """The width/height artist images must have, or None when no aspect is
    enforced at all.

    ``artist_image_crop`` is the artist-specific switch and ``cover_crop_enabled``
    the "covers have an aspect in the first place" master switch — with either
    off, artist images keep whatever shape their source had, which is what they
    did before ``artist_image_aspect`` existed. When they are on, the configured
    aspect (default 1:1) is what the fetch crops to and what the audit judges.
    """
    crop = bool(cfg.get("artist_image_crop", True)) and bool(cfg.get("cover_crop_enabled", True))
    if not crop:
        return None
    return parse_aspect(cfg.get("artist_image_aspect")) or parse_aspect(DEFAULT_ASPECT)


def _target_size(cfg):
    try:
        return max(0, min(4000, int(cfg.get("artist_image_target_size") or 0)))
    except (TypeError, ValueError):
        return 0


def image_policy(cfg):
    """(aspect, tolerance, target, max_side) for artist images.

    *aspect* is None when no aspect is enforced; *target* is 0 when the
    provider's native size is kept, and *max_side* is then the ceiling a stored
    image still may not exceed. One function feeds the fetch, the audit and the
    fix, so none of them can judge a file by different numbers.
    """
    target = _target_size(cfg)
    return (aspect_policy(cfg), ASPECT_TOLERANCE, target,
            target if target > 0 else DEFAULT_MAX_SIDE)


def aspect_deviation(size, aspect):
    """How far *size*'s width/height sits from *aspect*, relative to *aspect*.

    0.0 when either is unknown/unusable — a degenerate size is not a wrong
    aspect, and every caller treats 0.0 as "leave it alone"."""
    w, h = size
    if not aspect or not w or not h:
        return 0.0
    return abs((float(w) / float(h)) - aspect) / aspect


def _crop_to_aspect(img, aspect):
    """*img* centre-cropped to *aspect* (width/height); never padded."""
    w, h = img.size
    if not aspect or not w or not h:
        return img
    current = float(w) / float(h)
    if current > aspect:  # too wide: cut the sides
        new_w = max(1, int(round(h * aspect)))
        left = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    new_h = max(1, int(round(w / aspect)))  # too tall: cut top and bottom
    top = (h - new_h) // 2
    return img.crop((0, top, w, top + new_h))


def _downscale(img, longest):
    """*img* scaled so its longest side is *longest*; never upscaled."""
    w, h = img.size
    if longest <= 0 or max(w, h) <= longest:
        return img
    scale = longest / float(max(w, h))
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    resample = getattr(Image, "Resampling", Image).LANCZOS
    return img.resize(size, resample)


def _prepare(img, aspect, target, tolerance=ASPECT_TOLERANCE):
    """The artist image transform: rotate to the stored orientation, crop to
    the configured aspect, then downscale only.

    Nothing is ever upscaled and no minimum size is imposed — a 200px artist
    photo is a perfectly good artist photo, and the audit reports one as a note
    rather than a failure for exactly that reason.
    """
    if ImageOps is not None:
        try:
            img = ImageOps.exif_transpose(img) or img
        except Exception:
            pass
    if aspect and aspect_deviation(img.size, aspect) > tolerance:
        img = _crop_to_aspect(img, aspect)
    if target > 0:
        img = _downscale(img, target)
    return img


def decode_size(path):
    """(width, height) of the image at *path*, or None when it does not decode.

    The one decoder the audit and the fix share: both judge the real pixels of
    the file, never its name or its suffix."""
    if not HAS_PIL or not path or not os.path.isfile(path):
        return None
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:
        return None


def _parse_size(text):
    """(w, h) out of the recorded "WxH", or None."""
    try:
        w, h = str(text or "").lower().split("x", 1)
        size = (int(w), int(h))
    except (TypeError, ValueError):
        return None
    return size if size[0] > 0 and size[1] > 0 else None


def recorded_size(folder, cfg=None):
    """The (width, height) artistdata last wrote for *folder*, or None.

    None is the honest answer for every file this app did not write (and for
    entries from before the size was recorded): nothing to compare against, so
    the audit claims nothing about how the file got there."""
    return _parse_size(read_provenance(folder, cfg).get(IMAGE_SIZE_KEY))


def _save_normalized(folder, img, ext, cfg):
    """Write *img* as ``artist<ext>`` in *folder*, atomically, and drop the
    other variant so the folder never holds two artist images.

    The one writer both the fetch and script 19 go through, so an image the
    optimizer re-encodes carries exactly the format and quality policy a fetched
    one does (PNG when *ext* is ``.png``, otherwise ``cover_jpeg_quality``).
    Returns the path, or None when the write failed."""
    dest = os.path.join(folder, "artist" + ext)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".artist_", suffix=ext, dir=folder)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "wb") as fh:
            if ext == ".png":
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
    low = dest.lower()
    for path in _image_files(folder):
        if path.lower() != low:
            try:
                os.remove(path)
            except OSError:
                pass
    # A file in another container (artist.webp) is not an extension _image_files
    # knows, so it needs its own sweep: the folder must not end up with the new
    # artist.jpg AND the old file the artist page cannot show.
    for path in _unsupported_image_files(folder):
        try:
            os.remove(path)
        except OSError:
            pass
    return dest


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
        aspect, tolerance, target, _max_side = image_policy(cfg)
        img = _prepare(img, aspect, target, tolerance)
        if not png:
            img = _flatten(img)
    except Exception:
        return None

    dest = _save_normalized(folder, img, ".png" if png else ".jpg", cfg)
    if not dest:
        return None
    # The size this app wrote is the only evidence the audit has that a file on
    # disk was enlarged afterwards (a file bigger than what the app stored had
    # pixels invented for it) — see mlo.grader's artist image check.
    write_provenance(folder, {"source": source, "source_url": source_url,
                              "label": label, "title": title,
                              IMAGE_SIZE_KEY: f"{img.size[0]}x{img.size[1]}"},
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


def optimize_artist_image(folder, cfg, path=None) -> dict:
    """Re-fit *folder*'s stored artist image to the configured policy.

    The remedy for what mlo.grader's artist image check reports: the image is
    decoded, cropped to ``artist_image_aspect`` and downscaled to
    ``artist_image_target_size`` (never upscaled, and never past the size this
    app itself recorded writing — a file something else enlarged loses the
    invented pixels), then re-encoded as ``artist.jpg`` / ``artist.png`` at the
    cover JPEG quality.

    Returns ``{"path", "before", "after", "changed", "error", "reason"}``.
    ``changed`` is False whenever the file already satisfies the policy, which is
    what makes a re-run free: the pixels are re-encoded only when the size, the
    aspect or the suffix actually has to move. A file that does not decode is
    reported and left alone — its bytes are the only copy there is."""
    cfg = cfg or {}
    # A file in a container the library does not read (artist.webp) counts as
    # this pass's business too: it decodes, so the fix converts it into the
    # artist.jpg / artist.png the library and the artist page ask for.
    src = path or image_path(folder) or unsupported_image(folder)
    out = {"path": src, "before": None, "after": None, "changed": False,
           "error": None, "reason": ""}
    if not src or not os.path.isfile(src):
        out["path"] = None
        out["reason"] = "no artist image"
        return out
    try:
        before = decode_size(src)
    except Exception:
        before = None
    if before is None:
        out["error"] = "does not decode — re-fetch it"
        return out
    out["before"] = before

    aspect, tolerance, _target, max_side = image_policy(cfg)
    recorded = recorded_size(folder, cfg)
    ceiling = min(max_side, max(recorded)) if recorded else max_side
    png = os.path.splitext(src)[1].lower() == ".png"
    ext = ".png" if png else ".jpg"
    try:
        with Image.open(src) as raw:
            raw.load()
            img = raw.copy()
        img = _prepare(img, aspect, ceiling, tolerance)
        if not png:
            img = _flatten(img)
    except Exception as e:
        out["error"] = f"could not be re-encoded: {e}"
        return out

    after = img.size
    if after == before and os.path.splitext(src)[1].lower() == ext:
        out["after"] = after
        out["reason"] = f"{before[0]}x{before[1]} already matches the policy"
        return out

    dest = _save_normalized(folder, img, ext, cfg)
    if not dest:
        out["error"] = "could not be written"
        return out
    write_provenance(folder, {IMAGE_SIZE_KEY: f"{after[0]}x{after[1]}"}, cfg=cfg)
    out.update({
        "path": dest, "after": after, "changed": True,
        "reason": (f"{os.path.basename(src)} {before[0]}x{before[1]} -> "
                   f"{os.path.basename(dest)} {after[0]}x{after[1]}"),
    })
    return out


def artist_folders(cfg, targets=None):
    """Every artist folder holding an artist image, sorted by name.

    The direct children of the library root are the artists (an artist's albums
    live under them). *targets* limits the answer to the artist folders those
    paths sit in, so a run started from an album never sweeps the whole
    library."""
    root = library_root(_music_folder(cfg))
    if not root or not os.path.isdir(root):
        return []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    if targets is None:
        return [os.path.join(root, n) for n in names
                if os.path.isdir(os.path.join(root, n))
                and has_image(os.path.join(root, n))]
    out = []
    for target in targets or ():
        folder = _artist_folder_of(target, root)
        if folder and folder not in out:
            out.append(folder)
    return sorted(out)


def _artist_folder_of(path, root):
    """The artist folder *path* sits in, or None when it is outside the library.

    A targeted run hands over album folders or files; the artist folder is the
    child of the library root that holds them."""
    if not path:
        return None
    cur = os.path.normpath(os.path.abspath(str(path)))
    if os.path.isfile(cur):
        cur = os.path.dirname(cur)
    root = os.path.normpath(os.path.abspath(root))
    while cur and os.path.normcase(cur) != os.path.normcase(root):
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            return None
        if os.path.normcase(parent) == os.path.normcase(root):
            return cur if os.path.isdir(cur) else None
        cur = parent
    return None


def run_optimize_artist_images(config):
    """Script 19 — re-fit every stored artist image to the configured policy.

    An artist image arrives through the provider chain (or from a hand-placed
    file, or from an older release of this app) and then sits on disk for as long
    as the library does: nothing else re-reads it. This is that pass. Each artist
    folder's image is cropped to ``artist_image_aspect``, downscaled to
    ``artist_image_target_size`` (0 = the provider's native size, above which the
    DEFAULT_MAX_SIDE ceiling still applies), re-encoded at the library's cover
    quality and reported as before -> after. It never upscales, never deletes a
    corrupt file and leaves a conforming image byte for byte alone, so running it
    again — alone or as part of Run All — changes nothing."""
    from .stats import _make_pbar, _pbar_skip, _pbar_update, new_stats
    from .ui import Color, c, log, print_header

    config = config or {}
    stats = new_stats()
    print_header("Optimize artist images")
    aspect, tolerance, target, max_side = image_policy(config)
    log(f"library: {library_root(_music_folder(config)) or '(no music folder configured)'}")
    log(f"policy: aspect {aspect or 'native (crop off)'} ±{tolerance:.0%} · "
        f"size {target or 'native'} px (ceiling {max_side} px)")
    if not HAS_PIL:
        log(c("Pillow is not available — artist images cannot be inspected.", Color.RED))
        return stats

    folders = artist_folders(config, config.get("targets"))
    if not folders:
        log("No artist images found.")
        return stats

    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(len(folders), "Artist images", unit="image")
    for folder in folders:
        where = os.path.basename(folder.rstrip("\\/")) or folder
        try:
            res = optimize_artist_image(folder, config)
        except Exception as e:
            res = {"path": None, "before": None, "after": None,
                   "changed": False, "error": str(e), "reason": ""}
        stats["total_scanned"] += 1
        if res["error"]:
            stats["error_count"] += 1
            stats["errors"].append((res["path"] or folder, res["error"]))
            log(c(f"{where}: {res['error']}", Color.YELLOW))
            _pbar_update(pbar, counts, kind="fail")
        elif res["before"] is None:
            # No image at all is the fetch's job, not this pass's.
            stats["skipped_count"] += 1
            _pbar_skip(pbar, counts)
        elif res["changed"]:
            stats["modified_count"] += 1
            log(c(f"{where}: {res['reason']}", Color.GREEN))
            _pbar_update(pbar, counts)
        else:
            stats["unchanged_count"] += 1
            _pbar_update(pbar, counts)
    if pbar:
        pbar.close()
    log(c(f"artist images: {stats['modified_count']} re-encoded · "
          f"{stats['unchanged_count']} already match · "
          f"{stats['skipped_count']} without an image · "
          f"{stats['error_count']} failed",
          Color.GREEN if not stats["error_count"] else Color.YELLOW))
    return stats


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
    # The description's provider belongs under the `description_*` keys: this
    # entry is shared with the folder's artist image, and writing the plain
    # "source" here renamed the IMAGE's provider to the text's one (the image
    # payload reads `source` straight out of the same entry).
    write_provenance(folder, {"description_source": source,
                              "description_source_url": source_url},
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
