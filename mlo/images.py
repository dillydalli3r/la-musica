"""Image optimization: JPEG XL conversion, lossless in-place, and reverse."""
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import DEFAULT_CONFIG
from .containers import (
    _read_jxl_tags, _write_jxl_tags, _read_jpeg_xmp_tags, _insert_jpeg_xmp,
    _read_png_text, _strip_png_encoder_tags, _inject_png_text, _encoder_dict,
    _identity_missing, _quality_meets,
)
from .deps import HAS_PIL, Image
try:
    from PIL import ImageOps
except ImportError:
    ImageOps = None
from .subproc import run_tool
from .paths import (
    VALID_EXTENSIONS, ALL_IMAGE_EXTS, LOSSLESS_IMAGE_EXTS, CONVERTIBLE_EXTENSIONS,
    JPEG_QUALITY_MARKER, PNG_OPTIMIZATION_LEVEL, DEPS_DIR, LIB_AUDIO_EXTS,
    library_root,
)
from .stats import (
    new_stats, _make_pbar, _pbar_skip, _pbar_update, _diff_bytes,
    _existing_size, _safe_remove, _walk_files, _collect_targets, worker_count,
    tool_threads,
)
from .tools import detect_all_tools, _version_is_older
from .ui import log, fmt_size, print_header, c, Color

def _artist_image_stems():
    """The app's artist-image stems.

    mlo.artistdata owns what counts as an artist image (ARTIST_IMAGE_STEMS);
    asking it here keeps this pass from becoming a second list that can drift
    from the one has_image() reads.
    """
    try:
        from .artistdata import ARTIST_IMAGE_STEMS
        return tuple(ARTIST_IMAGE_STEMS)
    except Exception:
        return ("artist",)


def _artist_image_guard(music_folder):
    """A predicate: True when an image is artist artwork, never a cover.

    Artist artwork lives directly in an artist folder — ``<library>/Artists/
    <Artist>/artist.jpg``, the one place has_image() looks. The cover-rename
    map ranks the single best image of every folder and renames it to
    cover.*, so an artist folder's only image won that ranking and became
    ``cover.jpg``: the artist page and the artist-image grade then reported
    the image as missing, with the file renamed out from under them. The
    app's own stems cover the name; the FOLDER rule covers an artist image
    the library holds in another container (``artist.webp``), which script 19
    converts rather than renames.
    """
    stems = _artist_image_stems()
    root = library_root(music_folder)
    root_key = os.path.normcase(os.path.normpath(root)) if root else None

    def _is_artist_image(path):
        if os.path.splitext(os.path.basename(path))[0].lower() in stems:
            return True
        if not root_key:
            return False
        folder = os.path.normcase(os.path.normpath(os.path.dirname(path)))
        # The library root itself, or one level under it (an artist folder).
        if folder == root_key:
            return True
        return os.path.normcase(os.path.normpath(os.path.dirname(folder))) == root_key

    return _is_artist_image


def _exif_transposed(img):
    """*img* with its EXIF orientation applied, or *img* unchanged.

    A cover whose orientation tag says "rotate" stores wrong-axis dimensions;
    cropping/resizing before applying it produces rotated or mis-cropped art.
    """
    if ImageOps is None:
        return img
    try:
        if img.getexif().get(0x0112):
            return ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img


def _side_temp(src_path, tag, ext):
    """A working file for one conversion pass: BESIDE *src_path*, and hidden.

    Every pass here hands a tool or Pillow a file to write and then moves the
    result onto the real one, so the working file must share the folder (the
    tools' output has to land on the same volume as the file it replaces) and
    it must be recognisable:

    * it is dot-prefixed ``.mlo_tmp_``, so the library scan and the grader
      ignore it — an older name like ``cover.jpg.no_meta.jpg`` was left in the
      album as non-hidden junk by a killed run and was counted as a stray
      image, and
    * ``mlo.atomic`` knows the prefix, so the startup sweep
      (``server.interrupt_recovery``) deletes whatever an interrupted run
      abandoned and says so in the log.

    The extension stays LAST (tools that pick a format from the file name —
    cjxl, jpegtran, oxipng, ffmpeg — still see the extension they need).
    """
    folder = os.path.dirname(str(src_path)) or "."
    base = os.path.basename(str(src_path))
    return os.path.join(folder, f".mlo_tmp_{tag}_{base}")


def _strip_jpeg_metadata(input_path, output_path):
    try:
        with open(input_path, "rb") as f:
            data = f.read()

        if not data.startswith(b"\xff\xd8"):
            return False

        out = bytearray(b"\xff\xd8")
        i = 2
        n = len(data)

        while i < n:
            while i < n and data[i] == 0xFF:
                i += 1
            if i >= n:
                break

            marker = data[i]
            i += 1

            if marker == 0xD8:
                continue

            if marker == 0xD9:
                out.extend(b"\xff\xd9")
                break

            if 0xD0 <= marker <= 0xD7:
                out.extend(b"\xff")
                out.extend(bytes([marker]))
                continue

            if marker == 0xDA:
                out.extend(b"\xff\xda")
                if i + 1 >= n:
                    return False
                length = (data[i] << 8) | data[i + 1]
                out.extend(data[i:i + length])
                i += length
                out.extend(data[i:])
                break

            if i + 1 >= n:
                return False

            length = (data[i] << 8) | data[i + 1]

            if 0xE0 <= marker <= 0xEF or marker == 0xFE:
                i += length
                continue

            out.extend(b"\xff")
            out.extend(bytes([marker]))
            out.extend(data[i:i + length])
            i += length

        with open(output_path, "wb") as f:
            f.write(out)

        return True
    except Exception:
        return False


def _png_has_alpha(filepath):
    if not HAS_PIL:
        return False
    try:
        with Image.open(filepath) as img:
            # Header only. Mode and the tRNS transparency flag are read at
            # open(), and the file handle is released by this `with` block,
            # not by load() — which decoded every PNG in the library (even the
            # ones this run then skips) to answer a question the header
            # already answers.
            return (
                img.mode in ("RGBA", "LA")
                or (img.mode == "P" and "transparency" in img.info)
            )
    except Exception:
        return False


def _flatten_png_alpha(src_path, dst_path):
    if not HAS_PIL:
        return False
    try:
        with Image.open(src_path) as img:
            img.convert("RGB").save(dst_path, format="PNG")
        return True
    except Exception:
        return False


def _upright_png(src_path):
    """*src_path*'s pixels with its EXIF orientation applied, as a PNG path.

    Returns None when there is nothing to do (no orientation tag, no Pillow,
    an unreadable image). Used before cjxl on JPEG sources: the metadata strip
    that feeds cjxl removes the APP1 segment holding the orientation, so a
    cover whose tag says "rotate" would be encoded — and, with the source
    deleted right after, permanently stored — on its side. PNG keeps the
    decoded pixels exact, so this path adds no generation loss.
    """
    if not HAS_PIL:
        return None
    try:
        with Image.open(src_path) as img:
            if not img.getexif().get(0x0112):
                return None
            img.load()
            out = _side_temp(src_path, "upright", ".png")
            _exif_transposed(img).convert("RGB").save(out, format="PNG")
            return out
    except Exception:
        return None


def _unique_target_path(src_path, out_path, target_ext):
    """Destination for *out_path* that will not clobber an unrelated file.

    Returns *out_path* when it is free (or is the source itself), otherwise the
    first free ``stem_N`` sibling, or None when ten candidates all exist.
    Shared by the conversion paths so a cover.jxl -> cover.jpg pass cannot
    destroy an existing cover.jpg.
    """
    if os.path.normcase(os.path.normpath(out_path)) == os.path.normcase(os.path.normpath(src_path)):
        return out_path
    if not os.path.exists(out_path):
        return out_path
    out_dir = os.path.dirname(out_path)
    base = os.path.splitext(os.path.basename(out_path))[0]
    for i in range(1, 11):
        alt = os.path.join(out_dir, f"{base}_{i}{target_ext}")
        if not os.path.exists(alt):
            return alt
    return None


def _jpeg_write_quality(src_path, dst_path, config, did_cover=False):
    """The JPEG quality this module writes *dst_path* with.

    Shared by the writer and by the ENCODER_QUALITY marker so the tag records
    the quality the pixels actually got: cover art (a cover-named file, or one
    the cover pass cropped/resized) uses `cover_jpeg_quality`, anything else
    `images_jpeg_quality`.
    """
    try:
        dst_base = os.path.splitext(os.path.basename(dst_path).lower())[0]
        src_base = os.path.splitext(os.path.basename(src_path).lower())[0]
        is_cover_file = (dst_base in ("cover", "front", "folder")
                         or src_base in ("cover", "front", "folder"))
        if (did_cover or is_cover_file) and config and "cover_jpeg_quality" in config:
            q = int(config.get("cover_jpeg_quality", DEFAULT_CONFIG["cover_jpeg_quality"]))
        else:
            q = int(config.get("images_jpeg_quality", DEFAULT_CONFIG["images_jpeg_quality"])) if config else 95
        return max(70, min(100, q))
    except Exception:
        return 95


def _prepare_image_streamlined(src_path, dst_path, config, remove_alpha=False):
    """Streamlined Pillow pre-processing: alpha removal + cover crop/resize in one open.

    Combines the previously separate _flatten_png_alpha and _resize_and_crop_image
    passes into a single Image.open → convert → crop → resize → save, so a
    PNG→JXL with crop+alpha or a JPEG→JXL with crop does not pay two Pillow
    decode/encode cycles. Returns True when a new file was written to dst_path.
    """
    if not HAS_PIL or not src_path or not dst_path:
        return False
    try:
        with Image.open(src_path) as img:
            try:
                img.load()
            except Exception:
                pass
            img = _exif_transposed(img)
            # 1) Alpha handling (PNG only)
            if remove_alpha and img.mode in ("RGBA", "LA", "P"):
                if img.mode == "P" and "transparency" not in img.info and img.mode not in ("RGBA", "LA"):
                    pass
                else:
                    try:
                        # Flatten to RGB with white background for RGBA/LA, or convert P
                        if img.mode == "P":
                            img = img.convert("RGBA")
                        if img.mode in ("RGBA", "LA"):
                            bg = Image.new("RGB", img.size, (255, 255, 255))
                            if img.mode == "LA":
                                img = img.convert("RGBA")
                            bg.paste(img, mask=img.split()[-1])
                            img = bg
                        else:
                            img = img.convert("RGB")
                    except Exception:
                        try:
                            img = img.convert("RGB")
                        except Exception:
                            return False
            elif img.mode not in ("RGB", "RGBA", "LA", "P"):
                # Ensure a saveable mode for other formats
                try:
                    img = img.convert("RGB")
                except Exception:
                    pass
            elif img.mode == "P":
                try:
                    img = img.convert("RGB")
                except Exception:
                    pass

            # 2) Cover crop/resize (if enabled)
            did_cover = False
            if config is not None:
                ext = os.path.splitext(src_path)[1].lower()
                per_enabled = True
                if ext in (".jpg", ".jpeg"):
                    per_enabled = bool(config.get("cover_jpeg_enabled", True))
                elif ext == ".png":
                    per_enabled = bool(config.get("cover_png_enabled", True))
                elif ext == ".jxl":
                    per_enabled = bool(config.get("cover_jxl_enabled", True))
                if per_enabled:
                    re_en = bool(config.get("cover_resize_enabled", DEFAULT_CONFIG["cover_resize_enabled"]))
                    cr_en = bool(config.get("cover_crop_enabled", DEFAULT_CONFIG["cover_crop_enabled"]))
                    try:
                        thr = float(config.get("cover_crop_threshold", DEFAULT_CONFIG["cover_crop_threshold"]) or 0.05)
                    except (TypeError, ValueError):
                        thr = 0.05
                    thr = max(0.0, min(0.5, thr))
                    tgt = _get_cover_target_size(ext, config) if re_en else 0
                    if (re_en and tgt > 0) or cr_en:
                        # Use a temp in-memory crop/resize via _resize_and_crop_image logic
                        # but operate on the already-opened img to avoid re-opening
                        # Create a temp file for the helper to read from — instead, do it inline
                        w, h = img.size
                        need_crop = False
                        if cr_en and thr >= 0:
                            try:
                                ratio = w / h if h else 1.0
                                if abs(ratio - 1.0) > thr:
                                    need_crop = True
                            except Exception:
                                need_crop = False
                        force_exact = bool(config.get("cover_force_exact_size", DEFAULT_CONFIG["cover_force_exact_size"]))
                        if force_exact and re_en and tgt > 0 and w != h:
                            try:
                                if abs(w / h - 1.0) > thr:
                                    need_crop = True
                            except Exception:
                                need_crop = True
                        need_resize = False
                        if re_en and tgt > 0:
                            if not need_crop:
                                if w > tgt or h > tgt:
                                    need_resize = True
                            else:
                                sq = min(w, h)
                                if sq > tgt:
                                    need_resize = True
                        if need_crop or need_resize:
                            # Perform crop to threshold (not square) inline
                            if need_crop:
                                if w > h:
                                    target_w = int(h * (1.0 + thr))
                                    target_w = max(h, min(target_w, w))
                                    left = (w - target_w) // 2
                                    img = img.crop((left, 0, left + target_w, h))
                                else:
                                    target_h = int(w * (1.0 + thr))
                                    target_h = max(w, min(target_h, h))
                                    top = (h - target_h) // 2
                                    img = img.crop((0, top, w, top + target_h))
                            if need_resize:
                                try:
                                    resample = Image.Resampling.LANCZOS
                                except AttributeError:
                                    resample = Image.LANCZOS
                                img = img.resize((tgt, tgt), resample)
                            did_cover = True

            # Save to dst_path with appropriate format (infer from dst ext, fallback to src)
            ext_dst = os.path.splitext(dst_path)[1].lower() or os.path.splitext(src_path)[1].lower()
            save_kwargs = {}
            if ext_dst in (".jpg", ".jpeg"):
                if img.mode in ("RGBA", "LA", "P"):
                    try:
                        bg = Image.new("RGB", img.size, (255, 255, 255))
                        if img.mode == "LA":
                            img = img.convert("RGBA")
                        bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                        img = bg
                    except Exception:
                        img = img.convert("RGB")
                elif img.mode != "RGB":
                    img = img.convert("RGB")
                save_kwargs["format"] = "JPEG"
                save_kwargs["quality"] = _jpeg_write_quality(
                    src_path, dst_path, config, did_cover)
                save_kwargs["optimize"] = True
                # jpeg_progressive was honoured only by the in-place jpegtran
                # path (-progressive), so a cover CONVERTED to .jpg came out
                # baseline while the setting asked for progressive.
                if config is None:
                    save_kwargs["progressive"] = bool(
                        DEFAULT_CONFIG["jpeg_progressive"])
                else:
                    save_kwargs["progressive"] = bool(
                        config.get("jpeg_progressive",
                                   DEFAULT_CONFIG["jpeg_progressive"]))
            elif ext_dst == ".png":
                save_kwargs["format"] = "PNG"
                save_kwargs["optimize"] = True
            else:
                # For JXL temp or other, save as PNG to preserve lossless
                save_kwargs["format"] = "PNG"
                save_kwargs["optimize"] = True

            img.save(dst_path, **save_kwargs)
            return True
    except Exception:
        return False
    return False


def _get_cover_target_size(ext, config):
    """Return per-format cover target size (0 = use global).

    If the per-format override is >0 it wins, otherwise the global
    cover_target_size is used. Values are clamped to 0-4000.
    """
    if config is None:
        config = {}
    ext = (ext or "").lower()
    try:
        global_size = int(config.get("cover_target_size", DEFAULT_CONFIG["cover_target_size"]) or 0)
    except (TypeError, ValueError):
        global_size = 0
    global_size = max(0, min(4000, global_size))
    per_size = 0
    try:
        if ext in (".jpg", ".jpeg"):
            per_size = int(config.get("cover_jpeg_target_size", 0) or 0)
        elif ext == ".png":
            per_size = int(config.get("cover_png_target_size", 0) or 0)
        elif ext == ".jxl":
            per_size = int(config.get("cover_jxl_target_size", 0) or 0)
    except (TypeError, ValueError):
        per_size = 0
    if per_size > 0:
        return max(0, min(4000, per_size))
    return global_size


def _resize_and_crop_image(src_path, dst_path, target_size, crop_enabled, crop_threshold, config=None):
    """Resize and/or center-crop a cover image via Pillow.

    * If *crop_enabled* and aspect deviation > *crop_threshold*, center-crop
      the longer side to a square. When ``cover_force_exact_size`` is True
      and a resize target is set, the image is *always* center-cropped to a
      square before resizing, guaranteeing exactly ``target_size``×``target_size``
      output (e.g. 1000×1000) regardless of threshold or original aspect.
    * Then if *target_size* >0 (and resize is enabled in *config*) and the
      (possibly cropped) image is not already *target_size* x *target_size*,
      resize with LANCZOS to that square.

    Returns True when a new file was written to *dst_path*, False when no
    changes were needed or on error. Errors are handled gracefully.
    """
    if not HAS_PIL:
        return False
    if not src_path or not dst_path:
        return False
    try:
        try:
            thr = float(crop_threshold) if crop_threshold is not None else 0.05
        except (TypeError, ValueError):
            thr = 0.05
        thr = max(0.0, min(0.5, thr))
        try:
            tsize = int(target_size) if target_size else 0
        except (TypeError, ValueError):
            tsize = 0
        tsize = max(0, min(4000, tsize))
        # Respect global toggle inside helper as well
        resize_enabled = tsize > 0
        if config is not None and not config.get("cover_resize_enabled", DEFAULT_CONFIG["cover_resize_enabled"]):
            resize_enabled = False
            # still allow crop even when resize disabled
        try:
            with Image.open(src_path) as img:
                try:
                    img.load()
                except Exception:
                    pass
                img = _exif_transposed(img)
                w, h = img.size
                if w <= 0 or h <= 0:
                    return False
                need_crop = False
                if crop_enabled and thr >= 0:
                    try:
                        ratio = w / h if h != 0 else 1.0
                    except Exception:
                        ratio = 1.0
                    deviation = abs(ratio - 1.0)
                    if deviation > thr:
                        need_crop = True
                # Force exact size: when resize is wanted, ensure we will crop
                # (if not already) to meet the threshold — but do NOT force
                # automatically to 1:1; crop just enough to bring deviation
                # within thr (user request: crop to threshold, not to square).
                # The threshold already controls how square it must be.
                force_exact = bool(config.get("cover_force_exact_size", DEFAULT_CONFIG["cover_force_exact_size"])) if config else False
                if force_exact and resize_enabled and tsize > 0 and w != h:
                    # Only force crop if still outside threshold (not unconditional to square)
                    try:
                        ratio_f = w / h if h else 1.0
                        if abs(ratio_f - 1.0) > thr:
                            need_crop = True
                    except Exception:
                        need_crop = True
                # Never upscale: only downscale if larger than target
                need_resize = False
                if resize_enabled and tsize > 0:
                    if not need_crop:
                        if w > tsize or h > tsize:
                            need_resize = True
                    else:
                        sq = min(w, h)
                        if sq > tsize:
                            need_resize = True
                        # if square already <= target, keep cropped square as-is (no upscale)
                    # Force exact also needs resize if square side != target even when
                    # no crop was otherwise needed but the image is not square — the
                    # crop above already set need_crop, so the square branch applies.
                if not need_crop and not need_resize:
                    return False
                img_to_save = img
                if need_crop:
                    # Crop to threshold, not to square: reduce longer side just enough
                    # to bring |w/h -1| <= thr. For w>h, new_w = int(h*(1+thr)); for h>w, new_h = int(w*(1+thr)).
                    try:
                        if w > h:
                            target_w = int(h * (1.0 + thr))
                            target_w = max(h, min(target_w, w))
                            left = (w - target_w) // 2
                            top = 0
                            right = left + target_w
                            bottom = h
                        else:
                            target_h = int(w * (1.0 + thr))
                            target_h = max(w, min(target_h, h))
                            left = 0
                            top = (h - target_h) // 2
                            right = w
                            bottom = top + target_h
                    except Exception:
                        # Fallback to square on error
                        if w > h:
                            left = (w - h) // 2
                            top = 0
                            right = left + h
                            bottom = h
                        else:
                            left = 0
                            top = (h - w) // 2
                            right = w
                            bottom = top + w
                    try:
                        img_to_save = img.crop((left, top, right, bottom))
                    except Exception:
                        return False
                    w, h = img_to_save.size
                if need_resize:
                    try:
                        try:
                            resample = Image.Resampling.LANCZOS
                        except AttributeError:
                            resample = Image.LANCZOS
                        img_to_save = img_to_save.resize((tsize, tsize), resample)
                    except Exception:
                        try:
                            img_to_save = img_to_save.resize((tsize, tsize), Image.LANCZOS)
                        except Exception:
                            return False
                    w, h = img_to_save.size
                ext_dst = os.path.splitext(dst_path)[1].lower()
                save_kwargs = {}
                dst_dir = os.path.dirname(dst_path)
                if dst_dir and not os.path.exists(dst_dir):
                    try:
                        os.makedirs(dst_dir, exist_ok=True)
                    except OSError:
                        pass
                try:
                    if ext_dst in (".jpg", ".jpeg"):
                        if img_to_save.mode in ("RGBA", "LA", "P"):
                            if img_to_save.mode == "P":
                                try:
                                    img_to_save = img_to_save.convert("RGBA")
                                except Exception:
                                    pass
                            if img_to_save.mode in ("RGBA", "LA"):
                                try:
                                    bg = Image.new("RGB", img_to_save.size, (255, 255, 255))
                                    if img_to_save.mode == "LA":
                                        img_to_save = img_to_save.convert("RGBA")
                                    bg.paste(img_to_save, mask=img_to_save.split()[-1])
                                    img_to_save = bg
                                except Exception:
                                    img_to_save = img_to_save.convert("RGB")
                            else:
                                img_to_save = img_to_save.convert("RGB")
                        elif img_to_save.mode != "RGB":
                            try:
                                img_to_save = img_to_save.convert("RGB")
                            except Exception:
                                pass
                        save_kwargs["format"] = "JPEG"
                        try:
                            # For cover resize, use cover_jpeg_quality if present, else images_jpeg_quality
                            if config and "cover_jpeg_quality" in config:
                                q = int(config.get("cover_jpeg_quality", DEFAULT_CONFIG["cover_jpeg_quality"]))
                            else:
                                q = int(config.get("images_jpeg_quality", DEFAULT_CONFIG["images_jpeg_quality"])) if config else 95
                            q = max(70, min(100, q))
                        except Exception:
                            q = 95
                        save_kwargs["quality"] = q
                        save_kwargs["optimize"] = True
                        try:
                            save_kwargs["subsampling"] = 0
                        except Exception:
                            pass
                    elif ext_dst == ".png":
                        save_kwargs["format"] = "PNG"
                        save_kwargs["optimize"] = True
                    elif ext_dst == ".jxl":
                        # Pillow JXL plugin may not be available; fallback to PNG
                        try:
                            save_kwargs["format"] = "JPEGXL"
                        except Exception:
                            save_kwargs["format"] = "PNG"
                            save_kwargs["optimize"] = True
                    else:
                        save_kwargs["format"] = "PNG"
                        save_kwargs["optimize"] = True
                    img_to_save.save(dst_path, **save_kwargs)
                    if img_to_save is not img:
                        try:
                            img_to_save.close()
                        except Exception:
                            pass
                    return True
                except Exception:
                    return False
        except Exception:
            return False
    except Exception:
        return False


def _rename_to_cover(filepath, new_ext=None):
    out_dir = os.path.dirname(filepath)
    ext = new_ext if new_ext else os.path.splitext(filepath)[1]
    cover_path = os.path.join(out_dir, "cover" + ext)

    if filepath == cover_path:
        return filepath

    # os.replace, not "remove the old cover, then rename": the delete-then-
    # rename pair lost the existing cover whenever the rename itself failed
    # (a player holding cover.jpg open, a read-only file), leaving the album
    # with no cover at all. os.replace overwrites atomically, and it is also
    # what makes a case-only rename (COVER.jpg -> cover.jpg) work on a
    # case-insensitive filesystem. A failed replace keeps the source where it
    # is — the caller carries on with the path it already had.
    try:
        os.replace(filepath, cover_path)
    except OSError:
        return filepath
    return cover_path


def _process_image_to_jxl(args):
    # Support optional config for cover resize/crop (backwards compatible)
    if isinstance(args, (list, tuple)) and len(args) == 11:
        (
            cjxl_path,
            djxl_path,
            jxl_version,
            src_path,
            threads_to_use,
            effort,
            force,
            rename_to_cover,
            remove_alpha,
            enc,
            config,
        ) = args
    else:
        (
            cjxl_path,
            djxl_path,
            jxl_version,
            src_path,
            threads_to_use,
            effort,
            force,
            rename_to_cover,
            remove_alpha,
            enc,
        ) = args
        config = None
    enabled = enc.get("jxl") or {}

    src_path = os.path.normpath(src_path)
    ext = os.path.splitext(src_path)[1].lower()
    out_dir = os.path.dirname(src_path)

    final_out_path = (
        os.path.join(out_dir, "cover.jxl")
        if rename_to_cover
        else os.path.splitext(src_path)[0] + ".jxl"
    )

    if os.path.normpath(src_path) == os.path.normpath(final_out_path):
        temp_out_path = _side_temp(final_out_path, "reencode", ".tmp")
    else:
        temp_out_path = _side_temp(final_out_path, "reencode", ".tmp")

    temp_files = []
    # JPEG XL distance from config (0.0 lossless)
    try:
        _dist = float(config.get("jpegxl_distance", 0.0)) if config else 0.0
        _dist = max(0.0, min(2.0, _dist))
        dist_s = ("%f" % _dist).rstrip("0").rstrip(".") if _dist != 0 else "0"
    except Exception:
        dist_s = "0"

    try:
        try:
            original_size = os.path.getsize(src_path)
        except OSError as e:
            return (src_path, "failed", 0, 0, f"Cannot stat source: {e}")

        existing_dest_size = _existing_size(final_out_path, src_path)

        if ext == ".jxl" and not force:
            # If cover resize/crop would modify this JXL, don't skip
            _cover_needs = False
            if config is not None and HAS_PIL:
                try:
                    per_en = True
                    if ext in (".jpg", ".jpeg"):
                        per_en = bool(config.get("cover_jpeg_enabled", True))
                    elif ext == ".png":
                        per_en = bool(config.get("cover_png_enabled", True))
                    elif ext == ".jxl":
                        per_en = bool(config.get("cover_jxl_enabled", True))
                    if per_en:
                        re_en = bool(config.get("cover_resize_enabled", DEFAULT_CONFIG["cover_resize_enabled"]))
                        cr_en = bool(config.get("cover_crop_enabled", DEFAULT_CONFIG["cover_crop_enabled"]))
                        tgt = _get_cover_target_size(ext, config) if re_en else 0
                        if (re_en and tgt > 0) or cr_en:
                            # Need to inspect actual dimensions to decide if update needed
                            try:
                                with Image.open(src_path) as _im:
                                    _w, _h = _im.size
                                    force_exact = bool(config.get("cover_force_exact_size", DEFAULT_CONFIG["cover_force_exact_size"]))
                                    if cr_en:
                                        _ratio = _w / _h if _h else 1.0
                                        if abs(_ratio - 1.0) > float(config.get("cover_crop_threshold", DEFAULT_CONFIG["cover_crop_threshold"]) or 0.05):
                                            _cover_needs = True
                                    if force_exact and re_en and tgt > 0 and _w != _h:
                                        _cover_needs = True
                                    if re_en and tgt > 0 and (_w > tgt or _h > tgt):
                                        _cover_needs = True
                            except Exception:
                                # If PIL can't open JXL (e.g. omu PNG-JXL that djxl also fails),
                                # don't assume it needs cover — let the tag/effort check decide.
                                # Previously this incorrectly forced a decode attempt that then fails.
                                pass
                except Exception:
                    pass
            if not _cover_needs:
                q, v, p = _read_jxl_tags(src_path)
                if not _identity_missing(enabled, q, v, p):
                    try:
                        if _quality_meets(enabled, q, effort) and not _version_is_older(v, jxl_version):
                            log(f"[jxl skip] {os.path.basename(src_path)} already at q={q} v={v} (need q>={effort} v>={jxl_version})")
                            return (
                                src_path,
                                "unchanged",
                                0,
                                0,
                                f"skipped (q={q}, v={v})",
                            )
                        else:
                            log(f"[jxl re-encode] {os.path.basename(src_path)} q={q} < {effort} or v {v} older than {jxl_version}")
                    except (ValueError, TypeError) as e:
                        log(f"[jxl check error] {e}")
                        pass
                else:
                    log(f"[jxl re-encode] {os.path.basename(src_path)} identity missing (q={q} v={v} p={p})")

        _safe_remove(temp_out_path)

        input_for_cjxl = src_path
        use_strip_all = True
        force_no_reconstruction = False
        jpeg_stripped = None

        if ext in (".jpg", ".jpeg"):
            stripped_jpeg = _side_temp(src_path, "no_meta", ".jpg")
            temp_files.append(stripped_jpeg)

            if _strip_jpeg_metadata(src_path, stripped_jpeg):
                input_for_cjxl = stripped_jpeg
                use_strip_all = False
                jpeg_stripped = stripped_jpeg
            else:
                _safe_remove(stripped_jpeg)
                temp_files.remove(stripped_jpeg)
                force_no_reconstruction = True

        elif ext == ".png":
            # Streamlined: alpha + cover crop/resize in one Pillow pass when either is needed
            if HAS_PIL and (remove_alpha or (config is not None and (config.get("cover_crop_enabled") or config.get("cover_resize_enabled")))):
                has_alpha = _png_has_alpha(src_path) if remove_alpha else False
                # Check if cover handling would be needed (peek, don't open twice)
                cover_needed = False
                if config is not None:
                    per_en = bool(config.get("cover_png_enabled", True)) if ext == ".png" else True
                    if per_en and (config.get("cover_resize_enabled") or config.get("cover_crop_enabled")):
                        try:
                            with Image.open(src_path) as _im:
                                _w, _h = _im.size
                                if config.get("cover_crop_enabled") and abs(_w/_h - 1.0) > float(config.get("cover_crop_threshold", DEFAULT_CONFIG["cover_crop_threshold"]) or 0.05):
                                    cover_needed = True
                                if config.get("cover_resize_enabled") and _get_cover_target_size(ext, config) > 0 and (_w > _get_cover_target_size(ext, config) or _h > _get_cover_target_size(ext, config)):
                                    cover_needed = True
                        except Exception:
                            cover_needed = True
                if has_alpha or cover_needed:
                    streamlined_tmp = _side_temp(src_path, "streamlined", ".png")
                    _safe_remove(streamlined_tmp)
                    temp_files.append(streamlined_tmp)
                    if _prepare_image_streamlined(src_path, streamlined_tmp, config, remove_alpha=has_alpha):
                        input_for_cjxl = streamlined_tmp
                    else:
                        _safe_remove(streamlined_tmp)
                        temp_files.remove(streamlined_tmp)
                        # Fallback to old separate handling if streamlined fails
                        if has_alpha:
                            stripped_png = _side_temp(src_path, "no_alpha", ".png")
                            temp_files.append(stripped_png)
                            if _flatten_png_alpha(src_path, stripped_png):
                                input_for_cjxl = stripped_png
                            else:
                                _safe_remove(stripped_png)
                                temp_files.remove(stripped_png)

        elif ext == ".jxl":
            decoded_jpeg = _side_temp(src_path, "decoded", ".jpg")
            temp_files.append(decoded_jpeg)

            try:
                djxl_result = run_tool(
                    [djxl_path, src_path, decoded_jpeg],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except Exception as e:
                return (src_path, "failed", 0, 0, f"djxl (JPEG) error: {e}")

            if (
                djxl_result.returncode == 0
                and os.path.exists(decoded_jpeg)
                and os.path.getsize(decoded_jpeg) > 0
            ):
                stripped_jpeg = _side_temp(src_path, "no_meta", ".jpg")
                temp_files.append(stripped_jpeg)

                if _strip_jpeg_metadata(decoded_jpeg, stripped_jpeg):
                    input_for_cjxl = stripped_jpeg
                    use_strip_all = False
                else:
                    input_for_cjxl = decoded_jpeg
                    force_no_reconstruction = True
            else:
                _safe_remove(decoded_jpeg)
                temp_files.remove(decoded_jpeg)

                decoded_png = _side_temp(src_path, "decoded", ".png")
                temp_files.append(decoded_png)

                try:
                    djxl_result = run_tool(
                        [djxl_path, src_path, decoded_png],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )
                except Exception as e:
                    return (src_path, "failed", 0, 0, f"djxl (PNG) error: {e}")

                if (
                    djxl_result.returncode != 0
                    or not os.path.exists(decoded_png)
                    or os.path.getsize(decoded_png) == 0
                ):
                    err = djxl_result.stderr.decode("utf-8", errors="replace").strip()
                    # Try ffmpeg as fallback for JXLs that djxl cannot decode (e.g. omu cover.jxl)
                    # ffmpeg also uses libjxl but with different build options
                    try:
                        from .tools import detect_all_tools as _detect
                        _tools = _detect()
                        _ffmpeg = (_tools.get("ffmpeg") or {}).get("ffmpeg_exe")
                        if _ffmpeg:
                            fb_tmp = _side_temp(src_path, "ffmpeg", ".png")
                            temp_files.append(fb_tmp)
                            fb_res = run_tool([_ffmpeg, "-y", "-i", src_path, fb_tmp],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                            if fb_res.returncode == 0 and os.path.exists(fb_tmp) and os.path.getsize(fb_tmp) > 0:
                                input_for_cjxl = fb_tmp
                                # Continue to cover handling below (don't return)
                            else:
                                _safe_remove(fb_tmp)
                                if fb_tmp in temp_files:
                                    temp_files.remove(fb_tmp)
                                raise RuntimeError(err)
                        else:
                            raise RuntimeError(err)
                    except Exception:
                        # Fallback to XMP-only update when pixel decode truly impossible
                        try:
                            _write_jxl_tags(src_path, effort, jxl_version, enabled)
                            return (src_path, "modified", 0, 0, f"updated XMP tags only (cannot decode JXL pixels: {err[:40]})")
                        except Exception:
                            pass
                        return (src_path, "skipped", 0, 0, f"skipped (cannot decode JXL: {err[:80]})")

                input_for_cjxl = decoded_png

                if remove_alpha and HAS_PIL:
                    has_alpha = _png_has_alpha(decoded_png)
                    if has_alpha:
                        flat_png = _side_temp(decoded_png, "no_alpha", ".png")
                        temp_files.append(flat_png)

                        if _flatten_png_alpha(decoded_png, flat_png):
                            input_for_cjxl = flat_png

        # Cover resize / crop (before cjxl) — uses temp copy to keep original safe
        if config is not None and HAS_PIL:
            try:
                # Determine if cover handling should be attempted
                # Use input_for_cjxl extension for target, but also respect src ext
                ext_for_cover = os.path.splitext(input_for_cjxl)[1].lower()
                if not ext_for_cover:
                    ext_for_cover = ext
                per_enabled = True
                if ext_for_cover in (".jpg", ".jpeg"):
                    per_enabled = bool(config.get("cover_jpeg_enabled", True))
                elif ext_for_cover == ".png":
                    per_enabled = bool(config.get("cover_png_enabled", True))
                elif ext_for_cover == ".jxl":
                    per_enabled = bool(config.get("cover_jxl_enabled", True))
                if per_enabled:
                    resize_enabled_cfg = bool(config.get("cover_resize_enabled", DEFAULT_CONFIG["cover_resize_enabled"]))
                    crop_enabled_cfg = bool(config.get("cover_crop_enabled", DEFAULT_CONFIG["cover_crop_enabled"]))
                    crop_thr_cfg = float(config.get("cover_crop_threshold", DEFAULT_CONFIG["cover_crop_threshold"]) or 0.05)
                    target_for_cover = _get_cover_target_size(ext_for_cover, config) if resize_enabled_cfg else 0
                    # Also handle fallback to src ext target if input is decoded temp with different ext
                    if target_for_cover == 0 and resize_enabled_cfg and ext_for_cover != ext:
                        alt_target = _get_cover_target_size(ext, config)
                        if alt_target > 0:
                            target_for_cover = alt_target
                    if (resize_enabled_cfg and target_for_cover > 0) or crop_enabled_cfg:
                        # A lossless intermediate: cjxl is about to encode the
                        # pixels, so a JPEG temp here would insert a whole lossy
                        # generation into a cover whose source is deleted right
                        # after — while the decoded pixels are still in hand.
                        tmp_ext = ".png" if ext_for_cover in (".jpg", ".jpeg") else ext_for_cover
                        resized_cover_tmp = _side_temp(input_for_cjxl, "cover_resized", tmp_ext)
                        _safe_remove(resized_cover_tmp)
                        did_resize = _resize_and_crop_image(
                            input_for_cjxl, resized_cover_tmp,
                            target_for_cover if resize_enabled_cfg else 0,
                            crop_enabled_cfg, crop_thr_cfg, config
                        )
                        if did_resize and os.path.exists(resized_cover_tmp) and os.path.getsize(resized_cover_tmp) > 0:
                            temp_files.append(resized_cover_tmp)
                            input_for_cjxl = resized_cover_tmp
                            log(f"[cover] resized/cropped {os.path.basename(src_path)} -> {target_for_cover}x{target_for_cover}" if resize_enabled_cfg and target_for_cover else f"[cover] cropped {os.path.basename(src_path)}")
                        else:
                            _safe_remove(resized_cover_tmp)
            except Exception as e:
                log(f"[cover warn] {src_path}: {e}")

        # The strip above dropped APP1 along with every other metadata segment,
        # and the cover pass only applies an EXIF rotation when it actually
        # re-writes the image. A JPEG cover whose tag says "rotate" would
        # otherwise reach cjxl on its side and then lose its original — so the
        # rotation is applied to the pixels here, read from the ORIGINAL (the
        # stripped copy has no tags left to read it from; its pixels are
        # identical).
        if jpeg_stripped is not None and input_for_cjxl == jpeg_stripped:
            upright = _upright_png(src_path)
            if upright:
                temp_files.append(upright)
                input_for_cjxl = upright

        cmd = [
            cjxl_path,
            input_for_cjxl,
            temp_out_path,
            "-d", dist_s,
            "-e", str(effort),
            "--num_threads", str(threads_to_use),
            "--container=1",
        ]

        if use_strip_all:
            cmd.extend(["-x", "strip=all"])

        if force_no_reconstruction:
            cmd.append("--allow_jpeg_reconstruction=0")

        try:
            result = run_tool(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            return (src_path, "failed", 0, 0, f"cjxl error: {e}")

        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="replace").strip()
            return (src_path, "failed", 0, 0, f"cjxl rc={result.returncode}: {err}")

        if not os.path.exists(temp_out_path):
            return (src_path, "failed", 0, 0, "Output not created")

        try:
            _write_jxl_tags(temp_out_path, effort, jxl_version, enabled)
        except Exception as e:
            log(f"[jxl tag warn] {src_path}: {e}")

        final_size = os.path.getsize(temp_out_path)

        # Secure the new file at its final path FIRST (atomic overwrite),
        # then remove the source. If the rename fails, the original is
        # still intact and the finally block cleans up the temp output.
        try:
            os.replace(temp_out_path, final_out_path)
        except OSError as e:
            return (src_path, "failed", 0, 0, f"Couldn't rename: {e}")

        if os.path.normpath(src_path) != os.path.normpath(final_out_path):
            try:
                os.remove(src_path)
            except OSError as e:
                log(f"[cleanup warn] could not remove {src_path}: {e}")

        b_rem, b_add = _diff_bytes(original_size, final_size, existing_dest_size)

        return (
            final_out_path,
            "modified",
            b_rem,
            b_add,
            f"{fmt_size(original_size)} -> {fmt_size(final_size)}",
        )

    finally:
        for f in temp_files:
            _safe_remove(f)
        _safe_remove(temp_out_path)


def _process_jpeg_in_place(args):
    if isinstance(args, (list, tuple)) and len(args) == 8:
        (jpegtran_exe, ljt_version, filepath, force, rename_to_cover,
         progressive, enc, config) = args
    else:
        (jpegtran_exe, ljt_version, filepath, force, rename_to_cover,
         progressive, enc) = args
        config = None
    enabled = enc.get("jpeg") or {}

    filename = os.path.basename(filepath)
    temp_path = _side_temp(filepath, "opttmp", ".jpg")
    _cover_resized_tmp = None
    _input_for_jpegtran = filepath

    try:
        original_size = os.path.getsize(filepath)
    except OSError as e:
        return (filename, "failed", 0, 0, f"cannot stat: {e}")

    existing_dest_size = 0
    if rename_to_cover:
        cover_path = os.path.join(os.path.dirname(filepath), "cover.jpg")
        existing_dest_size = _existing_size(cover_path, filepath)

    # Cover-aware skip: don't skip if cover resize/crop needed
    _cover_needs = False
    if not force and config is not None and HAS_PIL:
        try:
            ext_cov = os.path.splitext(filepath)[1].lower()
            per_en = True
            if ext_cov in (".jpg", ".jpeg"):
                per_en = bool(config.get("cover_jpeg_enabled", True))
            elif ext_cov == ".png":
                per_en = bool(config.get("cover_png_enabled", True))
            elif ext_cov == ".jxl":
                per_en = bool(config.get("cover_jxl_enabled", True))
            if per_en:
                re_en = bool(config.get("cover_resize_enabled", DEFAULT_CONFIG["cover_resize_enabled"]))
                cr_en = bool(config.get("cover_crop_enabled", DEFAULT_CONFIG["cover_crop_enabled"]))
                tgt_cov = _get_cover_target_size(ext_cov, config) if re_en else 0
                if (re_en and tgt_cov > 0) or cr_en:
                    try:
                        with Image.open(filepath) as _im:
                            _w, _h = _im.size
                            force_exact_j = bool(config.get("cover_force_exact_size", DEFAULT_CONFIG["cover_force_exact_size"]))
                            if cr_en:
                                _ratio = _w / _h if _h else 1.0
                                if abs(_ratio - 1.0) > float(config.get("cover_crop_threshold", DEFAULT_CONFIG["cover_crop_threshold"]) or 0.05):
                                    _cover_needs = True
                            if force_exact_j and re_en and tgt_cov > 0 and _w != _h:
                                _cover_needs = True
                            if re_en and tgt_cov > 0 and (_w > tgt_cov or _h > tgt_cov):
                                _cover_needs = True
                    except Exception:
                        pass
        except Exception:
            pass
    if not force and not _cover_needs:
        q, v, p = _read_jpeg_xmp_tags(filepath)
        if not _identity_missing(enabled, q, v, p):
            try:
                if _quality_meets(enabled, q, JPEG_QUALITY_MARKER) and not _version_is_older(v, ljt_version):
                    return (
                        filename,
                        "unchanged",
                        0,
                        0,
                        f"skipped (q={q}, v={v})",
                    )
            except (ValueError, TypeError):
                pass

    _safe_remove(temp_path)

    # Cover resize / crop via Pillow before jpegtran (temp copy to keep original safe)
    _cover_resized_tmp = None
    _input_for_jpegtran = filepath
    if config is not None and HAS_PIL:
        try:
            ext_cov = os.path.splitext(filepath)[1].lower()
            per_en = True
            if ext_cov in (".jpg", ".jpeg"):
                per_en = bool(config.get("cover_jpeg_enabled", True))
            elif ext_cov == ".png":
                per_en = bool(config.get("cover_png_enabled", True))
            elif ext_cov == ".jxl":
                per_en = bool(config.get("cover_jxl_enabled", True))
            if per_en:
                re_en = bool(config.get("cover_resize_enabled", DEFAULT_CONFIG["cover_resize_enabled"]))
                cr_en = bool(config.get("cover_crop_enabled", DEFAULT_CONFIG["cover_crop_enabled"]))
                thr_cov = float(config.get("cover_crop_threshold", DEFAULT_CONFIG["cover_crop_threshold"]) or 0.05)
                tgt_cov = _get_cover_target_size(ext_cov, config) if re_en else 0
                if (re_en and tgt_cov > 0) or cr_en:
                    _cover_resized_tmp = _side_temp(filepath, "cover_resized", ".jpg")
                    _safe_remove(_cover_resized_tmp)
                    did = _resize_and_crop_image(filepath, _cover_resized_tmp, tgt_cov if re_en else 0, cr_en, thr_cov, config)
                    if did and os.path.exists(_cover_resized_tmp) and os.path.getsize(_cover_resized_tmp) > 0:
                        _input_for_jpegtran = _cover_resized_tmp
                        log(f"[cover] resized/cropped {os.path.basename(filepath)} -> {tgt_cov}x{tgt_cov}" if re_en and tgt_cov else f"[cover] cropped {os.path.basename(filepath)}")
                    else:
                        _safe_remove(_cover_resized_tmp)
                        _cover_resized_tmp = None
                        _input_for_jpegtran = filepath
        except Exception as e:
            log(f"[cover warn] {filepath}: {e}")
            if _cover_resized_tmp:
                _safe_remove(_cover_resized_tmp)
                _cover_resized_tmp = None
            _input_for_jpegtran = filepath

    cmd = [
        jpegtran_exe,
        "-copy", "none",
        "-optimize",
    ]
    if progressive:
        cmd.append("-progressive")
    cmd.extend(["-outfile", temp_path, _input_for_jpegtran])

    try:
        result = run_tool(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        if result.returncode != 0:
            err = (result.stderr or "").strip()
            return (filename, "failed", 0, 0, f"jpegtran failed: {err}")

        if not os.path.exists(temp_path):
            return (filename, "failed", 0, 0, "jpegtran produced no output")

        os.replace(temp_path, filepath)
        temp_path = None

        try:
            _insert_jpeg_xmp(
                filepath,
                JPEG_QUALITY_MARKER,
                ljt_version,
                "libjpeg-turbo/jpegtran",
                enabled,
            )
        except Exception as e:
            log(f"[jpeg tag warn] {filepath}: {e}")

        if rename_to_cover:
            filepath = _rename_to_cover(filepath, ".jpg")

        final_size = os.path.getsize(filepath)
        b_rem, b_add = _diff_bytes(original_size, final_size, existing_dest_size)

        info = f"{original_size // 1024} KB -> {final_size // 1024} KB"

        if rename_to_cover and os.path.basename(filepath) == "cover.jpg":
            info += " (cover.jpg)"

        return (filepath, "modified", b_rem, b_add, info)

    except Exception as e:
        return (filename, "failed", 0, 0, f"exception: {e}")

    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        try:
            _safe_remove(_cover_resized_tmp)
        except Exception:
            pass


def _process_png_in_place(args):
    if isinstance(args, (list, tuple)) and len(args) == 9:
        (
            oxipng_exe,
            oxipng_version,
            filepath,
            force,
            rename_to_cover,
            remove_alpha,
            optimization_level,
            enc,
            config,
        ) = args
    else:
        (
            oxipng_exe,
            oxipng_version,
            filepath,
            force,
            rename_to_cover,
            remove_alpha,
            optimization_level,
            enc,
        ) = args
        config = None
    enabled = enc.get("png") or {}

    filename = os.path.basename(filepath)
    temp_path = _side_temp(filepath, "opttmp", ".png")
    _cover_resized_tmp = None
    _flat_alpha_tmp = None
    _input_for_oxipng = filepath

    try:
        original_size = os.path.getsize(filepath)
    except OSError as e:
        return (filename, "failed", 0, 0, f"cannot stat: {e}")

    existing_dest_size = 0
    if rename_to_cover:
        cover_path = os.path.join(os.path.dirname(filepath), "cover.png")
        existing_dest_size = _existing_size(cover_path, filepath)

    # Alpha is not part of the ENCODER identity the skip check reads, so a
    # tagged PNG would keep its transparency forever: the flatten pass below
    # only runs when the file is processed. Probe once, here, and reuse the
    # answer for that pass.
    has_alpha = _png_has_alpha(filepath) if (remove_alpha and HAS_PIL) else False

    # Cover-aware skip check
    _cover_needs = False
    if not force and config is not None and HAS_PIL:
        try:
            ext_cov = os.path.splitext(filepath)[1].lower()
            per_en = True
            if ext_cov in (".jpg", ".jpeg"):
                per_en = bool(config.get("cover_jpeg_enabled", True))
            elif ext_cov == ".png":
                per_en = bool(config.get("cover_png_enabled", True))
            elif ext_cov == ".jxl":
                per_en = bool(config.get("cover_jxl_enabled", True))
            if per_en:
                re_en = bool(config.get("cover_resize_enabled", DEFAULT_CONFIG["cover_resize_enabled"]))
                cr_en = bool(config.get("cover_crop_enabled", DEFAULT_CONFIG["cover_crop_enabled"]))
                tgt_cov = _get_cover_target_size(ext_cov, config) if re_en else 0
                if (re_en and tgt_cov > 0) or cr_en:
                    try:
                        with Image.open(filepath) as _im:
                            _w, _h = _im.size
                            force_exact_j = bool(config.get("cover_force_exact_size", DEFAULT_CONFIG["cover_force_exact_size"]))
                            if cr_en:
                                _ratio = _w / _h if _h else 1.0
                                if abs(_ratio - 1.0) > float(config.get("cover_crop_threshold", DEFAULT_CONFIG["cover_crop_threshold"]) or 0.05):
                                    _cover_needs = True
                            if force_exact_j and re_en and tgt_cov > 0 and _w != _h:
                                _cover_needs = True
                            if re_en and tgt_cov > 0 and (_w > tgt_cov or _h > tgt_cov):
                                _cover_needs = True
                    except Exception:
                        pass
        except Exception:
            pass
    if not force and not _cover_needs and not has_alpha:
        tags = _read_png_text(filepath)
        q = tags.get("ENCODER_QUALITY")
        v = tags.get("ENCODER_VERSION")

        if not _identity_missing(enabled, q, v):
            try:
                if _quality_meets(enabled, q, optimization_level) and not _version_is_older(v, oxipng_version):
                    return (
                        filename,
                        "unchanged",
                        0,
                        0,
                        f"skipped (q={q}, v={v})",
                    )
            except (ValueError, TypeError):
                pass

    _safe_remove(temp_path)

    if has_alpha:
        flat_path = _side_temp(filepath, "no_alpha", ".png")
        _safe_remove(flat_path)

        try:
            # Feed the flattened copy to oxipng; the original is only
            # replaced by the existing success path below, so a later
            # failure leaves the source (and its transparency) intact.
            if _flatten_png_alpha(filepath, flat_path) and os.path.getsize(flat_path) > 0:
                _flat_alpha_tmp = flat_path
                _input_for_oxipng = flat_path
            else:
                _safe_remove(flat_path)
        except Exception:
            _safe_remove(flat_path)

    # Cover resize / crop before oxipng (temp copy)
    if config is not None and HAS_PIL:
        try:
            ext_cov = os.path.splitext(filepath)[1].lower()
            per_en = True
            if ext_cov in (".jpg", ".jpeg"):
                per_en = bool(config.get("cover_jpeg_enabled", True))
            elif ext_cov == ".png":
                per_en = bool(config.get("cover_png_enabled", True))
            elif ext_cov == ".jxl":
                per_en = bool(config.get("cover_jxl_enabled", True))
            if per_en:
                re_en = bool(config.get("cover_resize_enabled", DEFAULT_CONFIG["cover_resize_enabled"]))
                cr_en = bool(config.get("cover_crop_enabled", DEFAULT_CONFIG["cover_crop_enabled"]))
                thr_cov = float(config.get("cover_crop_threshold", DEFAULT_CONFIG["cover_crop_threshold"]) or 0.05)
                tgt_cov = _get_cover_target_size(ext_cov, config) if re_en else 0
                if (re_en and tgt_cov > 0) or cr_en:
                    _cover_resized_tmp = _side_temp(filepath, "cover_resized", ".png")
                    _safe_remove(_cover_resized_tmp)
                    did = _resize_and_crop_image(_input_for_oxipng, _cover_resized_tmp, tgt_cov if re_en else 0, cr_en, thr_cov, config)
                    if did and os.path.exists(_cover_resized_tmp) and os.path.getsize(_cover_resized_tmp) > 0:
                        _input_for_oxipng = _cover_resized_tmp
                        log(f"[cover] resized/cropped {os.path.basename(filepath)} -> {tgt_cov}x{tgt_cov}" if re_en and tgt_cov else f"[cover] cropped {os.path.basename(filepath)}")
                    else:
                        _safe_remove(_cover_resized_tmp)
                        _cover_resized_tmp = None
                        if _flat_alpha_tmp:
                            _input_for_oxipng = _flat_alpha_tmp
                        else:
                            _input_for_oxipng = filepath
                elif _flat_alpha_tmp:
                    _input_for_oxipng = _flat_alpha_tmp
                else:
                    _input_for_oxipng = filepath
        except Exception as e:
            log(f"[cover warn] {filepath}: {e}")
            if _cover_resized_tmp:
                _safe_remove(_cover_resized_tmp)
                _cover_resized_tmp = None
            _input_for_oxipng = _flat_alpha_tmp or filepath
    elif _flat_alpha_tmp:
        _input_for_oxipng = _flat_alpha_tmp
    else:
        _input_for_oxipng = filepath

    cmd = [
        oxipng_exe,
        "-o", str(optimization_level),
        "--strip", "safe",
        "--force",
        "--out", temp_path,
        _input_for_oxipng,
    ]

    try:
        result = run_tool(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        if result.returncode != 0:
            err = (result.stderr or "").strip()
            # If cover resize was applied, ensure temp is cleaned
            if _cover_resized_tmp:
                _safe_remove(_cover_resized_tmp)
            return (filename, "failed", 0, 0, f"oxipng failed: {err}")

        if not os.path.exists(temp_path):
            try:
                shutil.copy2(_input_for_oxipng, temp_path)
            except Exception:
                shutil.copy2(filepath, temp_path)

        os.replace(temp_path, filepath)
        temp_path = None

        try:
            _strip_png_encoder_tags(filepath)
        except Exception as e:
            log(f"[png strip warn] {filepath}: {e}")

        try:
            _inject_png_text(
                filepath,
                _encoder_dict("oxipng", optimization_level,
                              oxipng_version, enabled),
            )
        except Exception as e:
            log(f"[png tag warn] {filepath}: {e}")

        if rename_to_cover:
            filepath = _rename_to_cover(filepath, ".png")

        final_size = os.path.getsize(filepath)
        b_rem, b_add = _diff_bytes(original_size, final_size, existing_dest_size)

        info = f"{original_size // 1024} KB -> {final_size // 1024} KB"

        if rename_to_cover and os.path.basename(filepath) == "cover.png":
            info += " (cover.png)"

        return (filepath, "modified", b_rem, b_add, info)

    except Exception as e:
        return (filename, "failed", 0, 0, f"exception: {e}")

    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        try:
            _safe_remove(_cover_resized_tmp)
        except Exception:
            pass
        try:
            if _flat_alpha_tmp:
                _safe_remove(_flat_alpha_tmp)
        except Exception:
            pass


def _process_jxl_in_place(args):
    if isinstance(args, (list, tuple)) and len(args) == 10:
        (
            cjxl_path,
            djxl_path,
            jxl_version,
            src_path,
            threads_to_use,
            effort,
            force,
            rename_to_cover,
            enc,
            config,
        ) = args
    else:
        (
            cjxl_path,
            djxl_path,
            jxl_version,
            src_path,
            threads_to_use,
            effort,
            force,
            rename_to_cover,
            enc,
        ) = args
        config = None
    enabled = enc.get("jxl") or {}

    filename = os.path.basename(src_path)
    temp_out_path = _side_temp(src_path, "opttmp", ".jxl")
    temp_files = []
    try:
        _dist2 = float(config.get("jpegxl_distance", 0.0)) if config else 0.0
        _dist2 = max(0.0, min(2.0, _dist2))
        dist_s = ("%f" % _dist2).rstrip("0").rstrip(".") if _dist2 != 0 else "0"
    except Exception:
        dist_s = "0"

    try:
        try:
            original_size = os.path.getsize(src_path)
        except OSError as e:
            return (filename, "failed", 0, 0, f"cannot stat: {e}")

        existing_dest_size = 0
        if rename_to_cover:
            cover_path = os.path.join(os.path.dirname(src_path), "cover.jxl")
            existing_dest_size = _existing_size(cover_path, src_path)

        # Cover-aware skip
        _cover_needs = False
        if not force and config is not None and HAS_PIL:
            try:
                ext_cov = os.path.splitext(src_path)[1].lower()
                per_en = True
                if ext_cov in (".jpg", ".jpeg"):
                    per_en = bool(config.get("cover_jpeg_enabled", True))
                elif ext_cov == ".png":
                    per_en = bool(config.get("cover_png_enabled", True))
                elif ext_cov == ".jxl":
                    per_en = bool(config.get("cover_jxl_enabled", True))
                if per_en:
                    re_en = bool(config.get("cover_resize_enabled", DEFAULT_CONFIG["cover_resize_enabled"]))
                    cr_en = bool(config.get("cover_crop_enabled", DEFAULT_CONFIG["cover_crop_enabled"]))
                    tgt_cov = _get_cover_target_size(ext_cov, config) if re_en else 0
                    if (re_en and tgt_cov > 0) or cr_en:
                        try:
                            with Image.open(src_path) as _im:
                                _w, _h = _im.size
                                force_exact = bool(config.get("cover_force_exact_size", DEFAULT_CONFIG["cover_force_exact_size"]))
                                if cr_en:
                                    _ratio = _w / _h if _h else 1.0
                                    if abs(_ratio - 1.0) > float(config.get("cover_crop_threshold", DEFAULT_CONFIG["cover_crop_threshold"]) or 0.05):
                                        _cover_needs = True
                                if force_exact and re_en and tgt_cov > 0 and _w != _h:
                                    _cover_needs = True
                                if re_en and tgt_cov > 0 and (_w > tgt_cov or _h > tgt_cov):
                                    _cover_needs = True
                        except Exception:
                            pass
            except Exception:
                pass
        if not force and not _cover_needs:
            q, v, p = _read_jxl_tags(src_path)
            if not _identity_missing(enabled, q, v, p):
                try:
                    if _quality_meets(enabled, q, effort) and not _version_is_older(v, jxl_version):
                        return (
                            filename,
                            "unchanged",
                            0,
                            0,
                            f"skipped (q={q}, v={v})",
                        )
                except (ValueError, TypeError):
                    pass

        decoded_jpeg = _side_temp(src_path, "decoded", ".jpg")
        temp_files.append(decoded_jpeg)

        input_for_cjxl = None
        use_strip_all = True
        force_no_reconstruction = False

        try:
            djxl_result = run_tool(
                [djxl_path, src_path, decoded_jpeg],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            return (filename, "failed", 0, 0, f"djxl (JPEG) error: {e}")

        if (
            djxl_result.returncode == 0
            and os.path.exists(decoded_jpeg)
            and os.path.getsize(decoded_jpeg) > 0
        ):
            stripped_jpeg = _side_temp(src_path, "no_meta", ".jpg")
            temp_files.append(stripped_jpeg)

            if _strip_jpeg_metadata(decoded_jpeg, stripped_jpeg):
                input_for_cjxl = stripped_jpeg
                use_strip_all = False
            else:
                input_for_cjxl = decoded_jpeg
                force_no_reconstruction = True
        else:
            _safe_remove(decoded_jpeg)
            temp_files.remove(decoded_jpeg)

            decoded_png = _side_temp(src_path, "decoded", ".png")
            temp_files.append(decoded_png)

            try:
                djxl_result = run_tool(
                    [djxl_path, src_path, decoded_png],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except Exception as e:
                return (filename, "failed", 0, 0, f"djxl (PNG) error: {e}")

            if (
                djxl_result.returncode != 0
                or not os.path.exists(decoded_png)
                or os.path.getsize(decoded_png) == 0
            ):
                err = djxl_result.stderr.decode("utf-8", errors="replace").strip()
                return (filename, "failed", 0, 0, f"djxl decode failed: {err}")

            input_for_cjxl = decoded_png

        # Cover resize / crop on the decoded input before re-encoding
        if config is not None and HAS_PIL and input_for_cjxl:
            try:
                ext_for_cover = os.path.splitext(input_for_cjxl)[1].lower()
                if not ext_for_cover:
                    ext_for_cover = os.path.splitext(src_path)[1].lower()
                per_en = True
                if ext_for_cover in (".jpg", ".jpeg"):
                    per_en = bool(config.get("cover_jpeg_enabled", True))
                elif ext_for_cover == ".png":
                    per_en = bool(config.get("cover_png_enabled", True))
                elif ext_for_cover == ".jxl":
                    per_en = bool(config.get("cover_jxl_enabled", True))
                # Also check src ext per-format as fallback
                src_ext = os.path.splitext(src_path)[1].lower()
                src_per_en = True
                if src_ext in (".jpg", ".jpeg"):
                    src_per_en = bool(config.get("cover_jpeg_enabled", True))
                elif src_ext == ".png":
                    src_per_en = bool(config.get("cover_png_enabled", True))
                elif src_ext == ".jxl":
                    src_per_en = bool(config.get("cover_jxl_enabled", True))
                if per_en or src_per_en:
                    re_en = bool(config.get("cover_resize_enabled", DEFAULT_CONFIG["cover_resize_enabled"]))
                    cr_en = bool(config.get("cover_crop_enabled", DEFAULT_CONFIG["cover_crop_enabled"]))
                    thr_cov = float(config.get("cover_crop_threshold", DEFAULT_CONFIG["cover_crop_threshold"]) or 0.05)
                    tgt_cov = _get_cover_target_size(ext_for_cover, config) if re_en else 0
                    if tgt_cov == 0 and re_en:
                        # fallback to src target
                        tgt_cov = _get_cover_target_size(src_ext, config)
                    if (re_en and tgt_cov > 0) or cr_en:
                        resized_tmp = _side_temp(input_for_cjxl, "cover_resized", ext_for_cover)
                        _safe_remove(resized_tmp)
                        did = _resize_and_crop_image(input_for_cjxl, resized_tmp, tgt_cov if re_en else 0, cr_en, thr_cov, config)
                        if did and os.path.exists(resized_tmp) and os.path.getsize(resized_tmp) > 0:
                            temp_files.append(resized_tmp)
                            input_for_cjxl = resized_tmp
                            log(f"[cover] resized/cropped {os.path.basename(src_path)} -> {tgt_cov}x{tgt_cov}" if re_en and tgt_cov else f"[cover] cropped {os.path.basename(src_path)}")
                        else:
                            _safe_remove(resized_tmp)
            except Exception as e:
                log(f"[cover warn] {src_path}: {e}")

        cmd = [
            cjxl_path,
            input_for_cjxl,
            temp_out_path,
            "-d", dist_s,
            "-e", str(effort),
            "--num_threads", str(threads_to_use),
            "--container=1",
        ]

        if use_strip_all:
            cmd.extend(["-x", "strip=all"])

        if force_no_reconstruction:
            cmd.append("--allow_jpeg_reconstruction=0")

        try:
            result = run_tool(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            return (filename, "failed", 0, 0, f"cjxl error: {e}")

        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="replace").strip()
            return (filename, "failed", 0, 0, f"cjxl rc={result.returncode}: {err}")

        if not os.path.exists(temp_out_path):
            return (filename, "failed", 0, 0, "Output not created")

        try:
            _write_jxl_tags(temp_out_path, effort, jxl_version, enabled)
        except Exception as e:
            log(f"[jxl tag warn] {src_path}: {e}")

        final_size = os.path.getsize(temp_out_path)
        os.replace(temp_out_path, src_path)
        temp_out_path = None

        if rename_to_cover:
            src_path = _rename_to_cover(src_path, ".jxl")

        final_size = os.path.getsize(src_path)
        b_rem, b_add = _diff_bytes(original_size, final_size, existing_dest_size)

        info = f"{original_size // 1024} KB -> {final_size // 1024} KB"

        if rename_to_cover and os.path.basename(src_path) == "cover.jxl":
            info += " (cover.jxl)"

        return (src_path, "modified", b_rem, b_add, info)

    finally:
        for f in temp_files:
            _safe_remove(f)
        _safe_remove(temp_out_path)


def _cover_rank(path):
    """Rank one album-cover candidate: cover-named first, then the biggest scan.

    ONE rule for both places that decide which image is a folder's cover — the
    map that tells the in-place workers which file to rename to cover.* and the
    end-of-run rename pass — because two rankings could pick different files
    for the same folder and the two passes then fought over the name.
    """
    base = os.path.splitext(os.path.basename(path).lower())[0]
    if base in ("cover", "front", "folder"):
        score = 3
    elif "cover" in base or "front" in base:
        score = 2
    elif base.startswith(("01", "1", "scan")):
        score = 1
    else:
        score = 0
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0  # vanished/locked file: rank it last, don't abort
    return (score, size, os.path.basename(path).lower())


def _twin_present(src_path, out_path):
    """Whether *out_path* already exists as the same-stem twin of *src_path*.

    The JXL -> original pass deletes its source once it has written the
    same-stem file, so finding that file already there means the reverse
    conversion has already happened (or an unrelated file of the same name is
    in the way). _unique_target_path would mint "cover_1.jpg" — a redundant
    second copy of the same art — so the callers treat this as a done-state
    and leave both files alone.
    """
    return (os.path.normcase(os.path.normpath(out_path))
            != os.path.normcase(os.path.normpath(src_path))
            and os.path.exists(out_path))


def _process_jxl_back_to_original(args):
    if isinstance(args, (list, tuple)) and len(args) == 14:
        (
            djxl_path,
            jpegtran_exe,
            ljt_version,
            oxipng_exe,
            oxipng_version,
            src_path,
            rename_to_cover,
            remove_alpha,
            remove_alpha_pil,
            force,
            progressive,
            optimization_level,
            enc,
            config,
        ) = args
    else:
        (
            djxl_path,
            jpegtran_exe,
            ljt_version,
            oxipng_exe,
            oxipng_version,
            src_path,
            rename_to_cover,
            remove_alpha,
            remove_alpha_pil,
            force,
            progressive,
            optimization_level,
            enc,
        ) = args
        config = None

    temp_files = []

    try:
        try:
            original_size = os.path.getsize(src_path)
        except OSError as e:
            return (src_path, "failed", 0, 0, f"cannot stat: {e}")

        decoded_jpeg = _side_temp(src_path, "decoded", ".jpg")
        temp_files.append(decoded_jpeg)

        try:
            djxl_result = run_tool(
                [djxl_path, src_path, decoded_jpeg],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            return (src_path, "failed", 0, 0, f"djxl (JPEG) error: {e}")

        if (
            djxl_result.returncode == 0
            and os.path.exists(decoded_jpeg)
            and os.path.getsize(decoded_jpeg) > 0
        ):
            out_path = (
                os.path.join(os.path.dirname(src_path), "cover.jpg")
                if rename_to_cover
                else os.path.splitext(src_path)[0] + ".jpg"
            )
            if not force and _twin_present(src_path, out_path):
                return (src_path, "skipped", 0, 0,
                        f"skipped ({os.path.basename(out_path)} already present)")
            alt_out = _unique_target_path(src_path, out_path, ".jpg")
            if alt_out is None:
                return (src_path, "failed", 0, 0,
                        f"target exists: {os.path.basename(out_path)}")
            out_path = alt_out

            existing_dest_size = _existing_size(out_path, src_path)

            stripped_jpeg = _side_temp(src_path, "no_meta", ".jpg")
            temp_files.append(stripped_jpeg)

            if _strip_jpeg_metadata(decoded_jpeg, stripped_jpeg):
                input_file = stripped_jpeg
            else:
                input_file = decoded_jpeg

            # Cover resize / crop before re-encoding JPEG
            if config is not None and HAS_PIL:
                try:
                    out_ext = os.path.splitext(out_path)[1].lower() if out_path else ".jpg"
                    per_en = True
                    if out_ext in (".jpg", ".jpeg"):
                        per_en = bool(config.get("cover_jpeg_enabled", True))
                    elif out_ext == ".png":
                        per_en = bool(config.get("cover_png_enabled", True))
                    elif out_ext == ".jxl":
                        per_en = bool(config.get("cover_jxl_enabled", True))
                    # also check src JXL per-format as fallback
                    src_per_en = bool(config.get("cover_jxl_enabled", True))
                    if per_en or src_per_en:
                        re_en = bool(config.get("cover_resize_enabled", DEFAULT_CONFIG["cover_resize_enabled"]))
                        cr_en = bool(config.get("cover_crop_enabled", DEFAULT_CONFIG["cover_crop_enabled"]))
                        thr_cov = float(config.get("cover_crop_threshold", DEFAULT_CONFIG["cover_crop_threshold"]) or 0.05)
                        tgt_cov = _get_cover_target_size(out_ext, config) if re_en else 0
                        if tgt_cov == 0 and re_en:
                            # fallback to src JXL target if output per-format not set
                            tgt_cov = _get_cover_target_size(".jxl", config)
                        if (re_en and tgt_cov > 0) or cr_en:
                            resized_jpeg = input_file + ".cover_resized.tmp.jpg"
                            _safe_remove(resized_jpeg)
                            did = _resize_and_crop_image(input_file, resized_jpeg, tgt_cov if re_en else 0, cr_en, thr_cov, config)
                            if did and os.path.exists(resized_jpeg) and os.path.getsize(resized_jpeg) > 0:
                                temp_files.append(resized_jpeg)
                                input_file = resized_jpeg
                                log(f"[cover] resized/cropped {os.path.basename(src_path)} -> {tgt_cov}x{tgt_cov}" if re_en and tgt_cov else f"[cover] cropped {os.path.basename(src_path)}")
                            else:
                                _safe_remove(resized_jpeg)
                except Exception as e:
                    log(f"[cover warn] {src_path}: {e}")

            if jpegtran_exe:
                optimized = _side_temp(src_path, "optimized", ".jpg")
                temp_files.append(optimized)

                try:
                    jpeg_cmd = [
                        jpegtran_exe,
                        "-copy", "none",
                        "-optimize",
                        "-outfile", optimized,
                        input_file,
                    ]
                    if progressive:
                        jpeg_cmd.insert(4, "-progressive")
                    result = run_tool(
                        jpeg_cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                    )

                    final_input = (
                        optimized
                        if result.returncode == 0 and os.path.exists(optimized)
                        else input_file
                    )
                except Exception:
                    final_input = input_file
            else:
                final_input = input_file

            # Secure the decoded output first, then drop the source JXL.
            try:
                os.replace(final_input, out_path)
            except OSError as e:
                return (src_path, "failed", 0, 0, f"Couldn't write output: {e}")

            try:
                os.remove(src_path)
            except OSError as e:
                log(f"[cleanup warn] could not remove {src_path}: {e}")

            if jpegtran_exe:
                enc_ver = ljt_version
                program = "libjpeg-turbo/jpegtran"
                quality = JPEG_QUALITY_MARKER
            else:
                enc_ver = "decoded"
                program = "djxl/decoded"
                quality = 0

            try:
                _insert_jpeg_xmp(out_path, quality, enc_ver, program,
                                 enc.get("jpeg") or {})
            except Exception as e:
                log(f"[jpeg tag warn] {out_path}: {e}")

            final_size = os.path.getsize(out_path)
            b_rem, b_add = _diff_bytes(original_size, final_size, existing_dest_size)

            info = f"JXL -> JPEG: {original_size // 1024} KB -> {final_size // 1024} KB"

            if rename_to_cover:
                info += " (cover.jpg)"

            return (out_path, "modified", b_rem, b_add, info)

        _safe_remove(decoded_jpeg)
        temp_files.remove(decoded_jpeg)

        decoded_png = _side_temp(src_path, "decoded", ".png")
        temp_files.append(decoded_png)

        try:
            djxl_result = run_tool(
                [djxl_path, src_path, decoded_png],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            return (src_path, "failed", 0, 0, f"djxl (PNG) error: {e}")

        if (
            djxl_result.returncode != 0
            or not os.path.exists(decoded_png)
            or os.path.getsize(decoded_png) == 0
        ):
            err = djxl_result.stderr.decode("utf-8", errors="replace").strip()
            return (src_path, "failed", 0, 0, f"djxl decode failed: {err}")

        out_path = (
            os.path.join(os.path.dirname(src_path), "cover.png")
            if rename_to_cover
            else os.path.splitext(src_path)[0] + ".png"
        )
        if not force and _twin_present(src_path, out_path):
            return (src_path, "skipped", 0, 0,
                    f"skipped ({os.path.basename(out_path)} already present)")
        alt_out = _unique_target_path(src_path, out_path, ".png")
        if alt_out is None:
            return (src_path, "failed", 0, 0,
                    f"target exists: {os.path.basename(out_path)}")
        out_path = alt_out

        existing_dest_size = _existing_size(out_path, src_path)

        input_file = decoded_png

        if remove_alpha and remove_alpha_pil:
            has_alpha = _png_has_alpha(decoded_png)
            if has_alpha:
                flat_png = _side_temp(decoded_png, "no_alpha", ".png")
                temp_files.append(flat_png)

                if _flatten_png_alpha(decoded_png, flat_png):
                    input_file = flat_png

        # Cover resize / crop before re-encoding PNG
        if config is not None and HAS_PIL:
            try:
                out_ext = os.path.splitext(out_path)[1].lower() if out_path else ".png"
                per_en = True
                if out_ext in (".jpg", ".jpeg"):
                    per_en = bool(config.get("cover_jpeg_enabled", True))
                elif out_ext == ".png":
                    per_en = bool(config.get("cover_png_enabled", True))
                elif out_ext == ".jxl":
                    per_en = bool(config.get("cover_jxl_enabled", True))
                src_per_en = bool(config.get("cover_jxl_enabled", True))
                if per_en or src_per_en:
                    re_en = bool(config.get("cover_resize_enabled", DEFAULT_CONFIG["cover_resize_enabled"]))
                    cr_en = bool(config.get("cover_crop_enabled", DEFAULT_CONFIG["cover_crop_enabled"]))
                    thr_cov = float(config.get("cover_crop_threshold", DEFAULT_CONFIG["cover_crop_threshold"]) or 0.05)
                    tgt_cov = _get_cover_target_size(out_ext, config) if re_en else 0
                    if tgt_cov == 0 and re_en:
                        tgt_cov = _get_cover_target_size(".jxl", config)
                    if (re_en and tgt_cov > 0) or cr_en:
                        resized_png = input_file + ".cover_resized.tmp.png"
                        _safe_remove(resized_png)
                        did = _resize_and_crop_image(input_file, resized_png, tgt_cov if re_en else 0, cr_en, thr_cov, config)
                        if did and os.path.exists(resized_png) and os.path.getsize(resized_png) > 0:
                            temp_files.append(resized_png)
                            input_file = resized_png
                            log(f"[cover] resized/cropped {os.path.basename(src_path)} -> {tgt_cov}x{tgt_cov}" if re_en and tgt_cov else f"[cover] cropped {os.path.basename(src_path)}")
                        else:
                            _safe_remove(resized_png)
            except Exception as e:
                log(f"[cover warn] {src_path}: {e}")

        if oxipng_exe:
            optimized = _side_temp(src_path, "optimized", ".png")
            temp_files.append(optimized)

            try:
                result = run_tool(
                    [
                        oxipng_exe,
                        "-o", str(optimization_level),
                        "--strip", "safe",
                        "--force",
                        "--out", optimized,
                        input_file,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )

                final_input = (
                    optimized
                    if result.returncode == 0 and os.path.exists(optimized)
                    else input_file
                )
            except Exception:
                final_input = input_file
        else:
            final_input = input_file

        # Secure the decoded output first, then drop the source JXL.
        try:
            os.replace(final_input, out_path)
        except OSError as e:
            return (src_path, "failed", 0, 0, f"Couldn't write output: {e}")

        try:
            os.remove(src_path)
        except OSError as e:
            log(f"[cleanup warn] could not remove {src_path}: {e}")

        try:
            _strip_png_encoder_tags(out_path)
        except Exception:
            pass

        if oxipng_exe:
            enc_ver = oxipng_version
            program = "oxipng"
            quality = optimization_level
        else:
            enc_ver = "decoded"
            program = "djxl/decoded"
            quality = 0

        try:
            _inject_png_text(
                out_path,
                _encoder_dict(program, quality, enc_ver,
                              enc.get("png") or {}),
            )
        except Exception as e:
            log(f"[png tag warn] {out_path}: {e}")

        final_size = os.path.getsize(out_path)
        b_rem, b_add = _diff_bytes(original_size, final_size, existing_dest_size)

        info = f"JXL -> PNG: {original_size // 1024} KB -> {final_size // 1024} KB"

        if rename_to_cover:
            info += " (cover.png)"

        return (out_path, "modified", b_rem, b_add, info)

    except Exception as e:
        return (src_path, "failed", 0, 0, f"exception: {e}")

    finally:
        for f in temp_files:
            _safe_remove(f)


def _process_convert_image(args):
    """Convert any image to JPEG (lossy) or PNG (lossless) via Pillow.

    Args: (src_path, target_ext, rename_to_cover, config)
    target_ext is ".jpg" or ".png" (lowercase).
    Uses images_jpeg_quality for JPEG, handles cover crop/resize via _prepare_image_streamlined,
    and writes encoder tags. Removes source after successful conversion.
    """
    if len(args) == 4:
        src_path, target_ext, rename_to_cover, config = args
    else:
        src_path, target_ext, rename_to_cover = args[:3]
        config = args[3] if len(args) > 3 else None
    # (oxipng_exe, version, optimization_level): the lossless optimiser a
    # converted PNG is handed to (see the .png branch below). All None when
    # the caller passed none.
    png_tool = (tuple(args[4:7]) + (None, None, None))[:3]
    target_ext = target_ext.lower()
    if target_ext == ".jpeg":
        target_ext = ".jpg"
    src_path = os.path.normpath(src_path)
    out_dir = os.path.dirname(src_path)
    base = os.path.splitext(os.path.basename(src_path))[0]
    # Determine final output path (cover handling)
    if rename_to_cover:
        final_out = os.path.join(out_dir, "cover" + target_ext)
        if os.path.normcase(os.path.normpath(src_path)) == os.path.normcase(os.path.normpath(final_out)):
            # Already at cover path but wrong ext? Need temp
            final_out = os.path.join(out_dir, "cover" + target_ext)
            # If src is already cover.jpg etc with same ext, skip
            if os.path.normcase(os.path.normpath(src_path)) == os.path.normcase(os.path.normpath(final_out)):
                return (src_path, "skipped", 0, 0, "already correct format")
    else:
        final_out = os.path.splitext(src_path)[0] + target_ext
        if os.path.normcase(os.path.normpath(src_path)) == os.path.normcase(os.path.normpath(final_out)):
            return (src_path, "skipped", 0, 0, "already correct format")
        # Handle collision when two files with same base but different ext convert to same target (e.g., cover.bmp + cover.tiff -> cover.png)
        alt_out = _unique_target_path(src_path, final_out, target_ext)
        if alt_out is None:
            return (src_path, "skipped", 0, 0, f"target exists: {os.path.basename(final_out)}")
        final_out = alt_out

    # Avoid clobbering existing cover
    if rename_to_cover and os.path.exists(final_out) and os.path.normcase(os.path.normpath(src_path)) != os.path.normcase(os.path.normpath(final_out)):
        # Find alternative name if cover exists and src is not the cover
        alt = os.path.join(out_dir, base + target_ext)
        if not os.path.exists(alt):
            final_out = alt
        else:
            # Also handle collision for cover case
            for i in range(1, 11):
                alt2 = os.path.join(out_dir, f"{base}_{i}{target_ext}")
                if not os.path.exists(alt2):
                    final_out = alt2
                    break

    # The temp keeps the target extension: _prepare_image_streamlined picks the
    # output format from the dst extension, so a bare ".tmp" would silently
    # write PNG bytes into a .jpg file.
    temp_out = _side_temp(final_out, "convert", target_ext)
    try:
        original_size = os.path.getsize(src_path)
    except OSError as e:
        return (src_path, "failed", 0, 0, f"cannot stat: {e}")
    existing_dest_size = 0
    if os.path.exists(final_out):
        try:
            existing_dest_size = os.path.getsize(final_out)
        except OSError:
            existing_dest_size = 0

    # Use Pillow conversion (handles BMP, GIF, TIFF, WEBP, AVIF, etc.)
    if not HAS_PIL:
        return (src_path, "failed", 0, 0, "Pillow not installed (needed for conversion)")

    # Use streamlined helper which handles alpha, crop, resize, and save with correct format/quality
    ok = _prepare_image_streamlined(src_path, temp_out, config, remove_alpha=False)
    if not ok or not os.path.exists(temp_out) or os.path.getsize(temp_out) == 0:
        return (src_path, "failed", 0, 0, "Pillow conversion failed")

    # For JPEG, ensure quality and progressive handling already done via _prepare_image_streamlined
    # For PNG, ensure optimization
    # Write encoder tags
    try:
        from .containers import _encoder_dict
        enc = config.get("encoder_tags") or {} if config else {}
        if target_ext == ".jpg":
            # The marker records the quality the pixels were written with (the
            # same rule the writer just applied), not the global one: tagging a
            # cover written at cover_jpeg_quality as images_jpeg_quality made
            # the skip check compare the wrong number.
            q = _jpeg_write_quality(src_path, temp_out, config)
            # _prepare_image_streamlined already wrote with quality, just add tag
            try:
                from .containers import _insert_jpeg_xmp
                _insert_jpeg_xmp(temp_out, q, "pillow", "pillow/jpeg", enc.get("jpeg") or {})
            except Exception:
                pass
        elif target_ext == ".png":
            # A converted PNG is handed to the lossless optimiser when it is
            # installed: stamping it "pillow/png level N" made the in-place
            # oxipng pass skip the file forever (its skip check compares the
            # level and ignores the program name), so png_optimization_level
            # never reached a converted PNG. _process_png_in_place is the
            # existing pass for exactly that, and it writes the oxipng
            # identity itself.
            stamped = False
            if png_tool[0] and os.path.isfile(png_tool[0]):
                try:
                    _n, status, _br, _ba, _info = _process_png_in_place((
                        png_tool[0], png_tool[1], temp_out, False, False,
                        False, png_tool[2], enc, None))
                    stamped = status in ("modified", "unchanged")
                except Exception as e:
                    log(f"[convert png warn] {src_path}: {e}")
            if not stamped:
                # No optimiser available (or it failed): record what actually
                # wrote the pixels, so no later pass believes a level was
                # applied that never ran.
                try:
                    from .containers import _inject_png_text
                    try:
                        lvl = int(config.get("png_optimization_level", 6)) if config else 6
                    except Exception:
                        lvl = 6
                    _inject_png_text(temp_out, _encoder_dict("pillow/png", lvl, "pillow", enc.get("png") or {}))
                except Exception:
                    pass
    except Exception as e:
        log(f"[convert tag warn] {src_path}: {e}")

    final_size = os.path.getsize(temp_out)
    # Handle concurrent collision: if final_out already exists (e.g., cover.bmp + cover.tiff both -> cover.png
    # but second task's check at creation time saw no file, now first task has created it), find alternative
    # Also handle WinError 32 where file is being used by another concurrent task
    if os.path.exists(final_out):
        base2 = os.path.splitext(os.path.basename(final_out))[0]
        dir2 = os.path.dirname(final_out)
        if os.path.normcase(os.path.normpath(final_out)) != os.path.normcase(os.path.normpath(src_path)):
            for i in range(1, 11):
                alt = os.path.join(dir2, f"{base2}_{i}{target_ext}")
                if not os.path.exists(alt):
                    final_out = alt
                    break
            else:
                return (src_path, "failed", 0, 0, f"target exists: {os.path.basename(final_out)}")
    try:
        os.replace(temp_out, final_out)
    except OSError as e:
        # If WinError 32 (file in use) or FileExistsError due to concurrent creation, try alternative
        if getattr(e, 'winerror', None) == 32 or "being used by another process" in str(e) or os.path.exists(final_out):
            base2 = os.path.splitext(os.path.basename(final_out))[0]
            dir2 = os.path.dirname(final_out)
            for i in range(1, 11):
                alt = os.path.join(dir2, f"{base2}_{i}{target_ext}")
                if not os.path.exists(alt):
                    try:
                        os.replace(temp_out, alt)
                        final_out = alt
                        break
                    except OSError:
                        continue
            else:
                return (src_path, "failed", 0, 0, f"could not rename: {e} (target collision)")
        else:
            return (src_path, "failed", 0, 0, f"could not rename: {e}")
    if os.path.normcase(os.path.normpath(src_path)) != os.path.normcase(os.path.normpath(final_out)):
        try:
            os.remove(src_path)
        except OSError as e:
            log(f"[cleanup warn] could not remove {src_path}: {e}")

    b_rem, b_add = _diff_bytes(original_size, final_size, existing_dest_size)
    return (final_out, "modified", b_rem, b_add, f"{os.path.basename(src_path)} -> {os.path.basename(final_out)} {original_size//1024}KB->{final_size//1024}KB")


def run_process_images(config):
    # `.get` with the shipped default, like every other option below: a
    # partial cfg (a test, a helper scoping one album) must not KeyError here.
    effort = config.get("jpegxl_effort", DEFAULT_CONFIG["jpegxl_effort"])
    target_dir = config.get("music_folder", "")

    reencode_images = config.get("reencode_images", True)
    reencode_to_jxl = config.get("reencode_to_jxl", DEFAULT_CONFIG["reencode_to_jxl"])
    convert_jxl_back = config.get("convert_jxl_back", DEFAULT_CONFIG["convert_jxl_back"])
    rename_to_cover = config.get("rename_to_cover", True)
    remove_alpha = config.get("remove_alpha", True)
    progressive = config.get("jpeg_progressive", True)
    optimization_level = config.get("png_optimization_level",
                                    PNG_OPTIMIZATION_LEVEL)
    force = config.get("force_reencode_images", False)
    enc = config.get("encoder_tags") or {}

    stats = new_stats()

    if not reencode_images:
        # NOT an abort: the cover-rename pass at the end of this function moves
        # a file, it does not encode one, and gating it on reencode_images left
        # folders without a cover.* (and per-track sidecars unrenamed) whenever
        # the user turned re-encoding off. No tasks are built below.
        log(c("Image re-encoding is off — running the cover rename pass only.",
              Color.YELLOW))

    tools = detect_all_tools()
    jxl_tool = tools.get("libjxl")

    if not jxl_tool:
        log(c("WARNING: Could not auto-detect libjxl in .dependencies folder.", Color.RED))
        log(f"Expected a folder like: {os.path.join(DEPS_DIR, 'libjxl v0.12.0')}")
        log("JPEG XL conversion is disabled for this run — in-place lossless")
        log("JPEG/PNG optimization and the cover rename pass still run.")
        cjxl_path = None
        djxl_path = None
        jxl_version = None
        reencode_to_jxl = False
        convert_jxl_back = False
    else:
        cjxl_path = jxl_tool["cjxl_exe"]
        djxl_path = jxl_tool["djxl_exe"]
        jxl_version = jxl_tool["version"]

        if not djxl_path:
            log(c("WARNING: djxl.exe not found in libjxl folder.", Color.RED))
            log("JXL decode-dependent steps are disabled for this run.")
            djxl_path = None

    ljt = tools.get("libjpeg_turbo")
    ox = tools.get("oxipng")

    if not HAS_PIL and remove_alpha:
        log(c("WARNING: Pillow not found. Alpha removal will be skipped.", Color.YELLOW))
        log("Install it with: pip install Pillow")

    print_header("Image Processing")

    if not reencode_images:
        mode = "cover rename only (re-encoding is off)"
    elif convert_jxl_back and reencode_to_jxl:
        mode = "JXL -> original (reverse only; other files untouched)"
    elif convert_jxl_back:
        mode = "JXL -> original + in-place lossless JPEG/PNG optimization"
    elif reencode_to_jxl:
        mode = f"JPEG XL conversion (effort {effort})"
    elif config.get("images_convert_to_jpeg"):
        try:
            q = int(config.get("images_jpeg_quality", DEFAULT_CONFIG["images_jpeg_quality"]))
            q = max(70, min(100, q))
        except Exception:
            q = 95
        mode = f"Convert to JPEG (lossy, q={q}) + lossless JPEG/PNG"
    elif config.get("images_convert_lossless_to_png"):
        mode = "Convert lossless to PNG + lossless JPEG/PNG"
    else:
        mode = "Lossless in-place JPEG/PNG/JXL"

    log(f"mode: {mode}")
    log(f"target: {target_dir}")

    # Determine extensions to scan — include convertible types when conversion is enabled
    convert_to_jpeg = bool(config.get("images_convert_to_jpeg", DEFAULT_CONFIG["images_convert_to_jpeg"]))
    convert_to_png = bool(config.get("images_convert_lossless_to_png", False))
    scan_exts = VALID_EXTENSIONS
    if convert_to_jpeg or convert_to_png:
        # Include all convertible types so BMP/GIF/TIFF/WEBP etc. are found
        scan_exts = CONVERTIBLE_EXTENSIONS
        # Also include VALID_EXTENSIONS (already in CONVERTIBLE, but ensure)
        scan_exts = tuple(sorted(set(scan_exts) | set(VALID_EXTENSIONS)))
    targets = config.get("targets")
    files = _collect_targets(targets, scan_exts)
    if targets is None:
        files = sorted(_walk_files(target_dir, scan_exts))
    # Deduplicate case-insensitive (Windows) and normcase for WinError 32
    if len(files) != len(set(os.path.normcase(p) for p in files)):
        seen = {}
        for p in files:
            seen[os.path.normcase(p)] = p
        files = sorted(seen.values())

    log(f"found {len(files)} image file(s) for processing")
    for f in files[:10]:
        log(f"  - {os.path.relpath(f, target_dir) if target_dir in f else f}")
    if len(files) > 10:
        log(f"  ... and {len(files)-10} more")

    if not files:
        log("No image files found.")
        return stats

    cpu_count = os.cpu_count() or 1
    est_workers = worker_count(config, default=cpu_count, items=len(files))
    # Each cjxl lane's own thread count. The pool above runs *est_workers*
    # files at once, so this must be the worker budget divided among them —
    # NOT cpu_count, which made the pass occupy every core however low the
    # Worker threads setting was (where 0 = derive it from the CPU, the two
    # are the same number and nothing changes).
    threads_per_file = tool_threads(config, est_workers)

    # With rename_to_cover, at most ONE image per folder may take the cover
    # name; every other image keeps its own basename. Otherwise front/back/
    # booklet scans would all write to the same cover.* file and clobber
    # each other (losing every image but the last).
    # Artist artwork is never a folder's cover: see _artist_image_guard.
    _is_artist_image = _artist_image_guard(config.get("music_folder"))
    cover_map = {}
    if rename_to_cover:
        groups = {}
        for f in files:
            if _is_artist_image(f):
                continue
            groups.setdefault(os.path.dirname(f), []).append(f)
        for folder, group in groups.items():
            cover_map[folder] = max(group, key=_cover_rank)

    def _renames(f):
        return rename_to_cover and f == cover_map.get(os.path.dirname(f))

    tasks = []
    # Track reserved conversion targets to avoid collisions when multiple files with same base
    # (e.g., cover.bmp + cover.tiff both → cover.png) would overwrite each other
    reserved_targets = set(os.path.normcase(p) for p in files)

    for f in (files if reencode_images else []):
        ext = os.path.splitext(f)[1].lower()

        if convert_jxl_back and ext == ".jxl":
            # JXL files go back to their original format. This branch only
            # catches .jxl so a reverse-only run (convert_jxl_back on,
            # reencode_to_jxl off) still runs the normal in-place lossless
            # pass on JPEG/PNG below — previously those files were left
            # untouched entirely, so covers were never resized/optimized.
            tasks.append((
                _process_jxl_back_to_original,
                (
                    djxl_path,
                    ljt["jpegtran_exe"] if ljt else None,
                    ljt["version"] if ljt else None,
                    ox["oxipng_exe"] if ox else None,
                    ox["version"] if ox else None,
                    f,
                    _renames(f),
                    remove_alpha,
                    HAS_PIL,
                    force,
                    progressive,
                    optimization_level,
                    enc,
                    config,
                ),
                f,
            ))

        elif reencode_to_jxl and not convert_jxl_back:
            tasks.append((
                _process_image_to_jxl,
                (
                    cjxl_path,
                    djxl_path,
                    jxl_version,
                    f,
                    threads_per_file,
                    effort,
                    force,
                    _renames(f),
                    remove_alpha,
                    enc,
                    config,
                ),
                f,
            ))

        else:
            # Default cover policy: covers should end up as .jpg (1200x1200
            # 90% per defaults). When convert-to-JPEG is on, route cover
            # images from other formats (PNG/JXL/WEBP/...) through the
            # JPEG converter instead of optimizing them in place, so e.g. a
            # cover.png becomes cover.jpg. Non-cover images keep their
            # lossless in-place path.
            if convert_to_jpeg and ext not in (".jpg", ".jpeg"):
                _base = os.path.splitext(os.path.basename(f).lower())[0]
                _is_coverish = _renames(f) or _base in ("cover", "front", "folder") or ("cover" in _base or "front" in _base)
                if _is_coverish:
                    tasks.append((
                        _process_convert_image,
                        (f, ".jpg", _renames(f), config,
                         (ox or {}).get("oxipng_exe"),
                         (ox or {}).get("version"),
                         optimization_level),
                        f,
                    ))
                    continue
            if ext in (".jpg", ".jpeg"):
                if ljt:
                    tasks.append((
                        _process_jpeg_in_place,
                        (
                            ljt["jpegtran_exe"],
                            ljt["version"],
                            f,
                            force,
                            _renames(f),
                            progressive,
                            enc,
                            config,
                        ),
                        f,
                    ))
                else:
                    stats["skipped_count"] += 1

            elif ext == ".png":
                if ox:
                    tasks.append((
                        _process_png_in_place,
                        (
                            ox["oxipng_exe"],
                            ox["version"],
                            f,
                            force,
                            _renames(f),
                            remove_alpha,
                            optimization_level,
                            enc,
                            config,
                        ),
                        f,
                    ))
                else:
                    stats["skipped_count"] += 1

            elif ext == ".jxl":
                if cjxl_path is None:
                    # JXL tools unavailable — keep the file for the rename pass
                    stats["skipped_count"] += 1
                else:
                    tasks.append((
                        _process_jxl_in_place,
                        (
                            cjxl_path,
                            djxl_path,
                            jxl_version,
                            f,
                            threads_per_file,
                            effort,
                            force,
                            _renames(f),
                            enc,
                            config,
                        ),
                        f,
                    ))

            else:
                # Other convertible types (BMP, GIF, TIFF, WEBP, AVIF, etc.)
                # Handle convert-to-JPEG (lossy) and convert-lossless-to-PNG per config
                convert_to_jpeg = bool(config.get("images_convert_to_jpeg", DEFAULT_CONFIG["images_convert_to_jpeg"]))
                convert_to_png = bool(config.get("images_convert_lossless_to_png", False))
                # Determine if this file should be converted
                is_lossless_src = ext in LOSSLESS_IMAGE_EXTS
                target_ext = None
                if convert_to_jpeg and ext not in (".jpg", ".jpeg"):
                    # Convert any non-JPEG to JPEG (lossy) — uses images_jpeg_quality
                    target_ext = ".jpg"
                elif convert_to_png and is_lossless_src and ext != ".png":
                    # Convert lossless types (BMP, GIF, TIFF, etc.) to PNG
                    target_ext = ".png"
                elif convert_to_png and ext in (".webp", ".avif", ".heic", ".heif") and ext != ".png":
                    # For webp/avif/heic, check if they are lossless variants? Convert to PNG as lossless
                    # We treat them as convertible to PNG when that option is on
                    target_ext = ".png"
                if target_ext:
                    # Respect FORCE: if not force and target already exists with correct format, skip
                    if not force:
                        # Check if a file with the target name already exists (e.g., cover.jpg for cover.bmp)
                        # For convert-to-JPEG/PNG, the target is base + new ext or cover + new ext
                        base = os.path.splitext(os.path.basename(f))[0]
                        if _renames(f):
                            tentative = os.path.join(os.path.dirname(f), "cover" + target_ext)
                        else:
                            tentative = os.path.splitext(f)[0] + target_ext
                        # Also check alternative names (cover_1 etc.) if tentative exists due to collision handling
                        # If tentative exists and is a valid image, consider it already converted and skip
                        if os.path.exists(tentative):
                            # Check if tentative is a valid image (not a temp file)
                            try:
                                if os.path.getsize(tentative) > 0:
                                    # Consider already converted, skip unless force
                                    stats["skipped_count"] += 1
                                    continue
                            except OSError:
                                pass
                        # Also check for alternative with suffix if tentative exists
                        # We already handle collision at worker, but for FORCE=False we should skip if any converted version exists
                        for i in range(1, 11):
                            alt = os.path.join(os.path.dirname(f), f"{base}_{i}{target_ext}")
                            if os.path.exists(alt):
                                try:
                                    if os.path.getsize(alt) > 0:
                                        stats["skipped_count"] += 1
                                        target_ext = None
                                        break
                                except OSError:
                                    pass
                        if target_ext is None:
                            continue
                    # Check for target collision with already-reserved files (e.g., cover.bmp + cover.tiff → cover.png)
                    # Reserve the target so subsequent files with same base don't clobber
                    base = os.path.splitext(os.path.basename(f))[0]
                    # Determine final target path as _process_convert_image would (respecting rename_to_cover)
                    if _renames(f):
                        tentative = os.path.join(os.path.dirname(f), "cover" + target_ext)
                    else:
                        tentative = os.path.splitext(f)[0] + target_ext
                    norm_tent = os.path.normcase(tentative)
                    if norm_tent in reserved_targets:
                        # Find alternative with suffix
                        for i in range(1, 11):
                            alt = os.path.join(os.path.dirname(f), f"{base}_{i}{target_ext}")
                            if os.path.normcase(alt) not in reserved_targets and not os.path.exists(alt):
                                tentative = alt
                                break
                        else:
                            stats["skipped_count"] += 1
                            continue
                    reserved_targets.add(norm_tent)
                    # Also reserve the tentative if we used alternative
                    reserved_targets.add(os.path.normcase(tentative))
                    # Use Pillow conversion via streamlined helper
                    tasks.append((
                        _process_convert_image,
                        (f, target_ext, _renames(f), config,
                         (ox or {}).get("oxipng_exe"),
                         (ox or {}).get("version"),
                         optimization_level),
                        f,
                    ))
                else:
                    stats["skipped_count"] += 1

    log(f"prepared {len(tasks)} task(s) from {len(files)} file(s) (mode: {mode})")

    if tasks:
        workers = worker_count(config, default=cpu_count, items=len(tasks))
        counts = {"ok": 0, "skip": 0, "fail": 0}

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(fn, args): src
                for (fn, args, src) in tasks
            }

            pbar = _make_pbar(len(future_map), "Images")

            for future in as_completed(future_map):
                src = future_map[future]

                try:
                    src_path, status, b_rem, b_add, info = future.result()
                except Exception as e:
                    stats["total_scanned"] += 1
                    stats["error_count"] += 1
                    stats["errors"].append((src, str(e)))
                    _pbar_update(pbar, counts, kind="fail")
                    continue

                if status in ("unchanged", "skipped"):
                    stats["skipped_count"] += 1
                    _pbar_skip(pbar, counts)
                    continue

                stats["total_scanned"] += 1

                if status == "modified":
                    stats["modified_count"] += 1
                    stats["total_bytes_removed"] += b_rem
                    stats["total_bytes_added"] += b_add
                    _pbar_update(pbar, counts, kind="ok")
                else:
                    stats["error_count"] += 1
                    stats["errors"].append((src_path, info))
                    _pbar_update(pbar, counts, kind="fail")

            if pbar:
                pbar.close()

    # ------------------------------------------------------------------ #
    # Cover-rename pass: skipped/unchanged files never got renamed by the
    # workers (they only rename when actually re-encoding). Ensure every
    # folder has a cover.<ext> — one file per folder only.
    # ------------------------------------------------------------------ #
    renamed = 0
    if rename_to_cover:
        from collections import defaultdict
        by_folder = defaultdict(list)
        for f in files:
            if os.path.exists(f):
                by_folder[os.path.dirname(f)].append(f)
        for folder, group in by_folder.items():
            names = os.listdir(folder)
            # Never overwrite an existing cover.* — it may be a different
            # image the workers already renamed into place.
            has_cover = any(
                os.path.splitext(n.lower())[0] == "cover"
                for n in names
            )
            if has_cover:
                continue

            # A per-track sidecar ("01 - Song.jpg" beside "01 - Song.flac")
            # is that track's art: renaming it to cover.* makes
            # get_track_cover() fall back to the album cover and silently
            # lose the per-track mapping. Its stem matching a track's means
            # it is never a candidate, so a folder holding only sidecars
            # (the common single-track release) keeps every one of them.
            # Artist artwork in an artist folder is not a candidate either:
            # renaming it to cover.* is what made has_image() report the
            # artist image as missing.
            track_stems = {os.path.splitext(n)[0].lower() for n in names
                           if n.lower().endswith(LIB_AUDIO_EXTS)}
            group = [f for f in group
                     if os.path.splitext(os.path.basename(f))[0].lower()
                     not in track_stems and not _is_artist_image(f)]
            if not group:
                continue

            candidate = max(group, key=_cover_rank)
            cand_ext = os.path.splitext(candidate)[1]
            target = os.path.join(folder, "cover" + cand_ext)
            if os.path.normcase(os.path.normpath(candidate)) == os.path.normcase(os.path.normpath(target)):
                continue
            try:
                # os.replace, not remove-then-rename: the two calls left a
                # window with no cover at all if the process died between
                # them (the has_cover check above already guarantees the
                # target is free, so this only overwrites a race).
                os.replace(candidate, target)
                stats["total_scanned"] += 1
                stats["modified_count"] += 1
                renamed += 1
            except OSError as e:
                stats["error_count"] += 1
                stats["errors"].append((candidate, f"cover rename failed: {e}"))
    if renamed:
        log(f"cover-rename pass: {renamed} image(s) renamed to cover.*")

    return stats

