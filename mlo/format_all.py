"""Format All — final pass to ensure every file/tag is correctly formatted.

This script runs at the end of Run All / Optimize Selected and dynamically
detects what needs formatting, fixing only what is incorrect:

* .accurip — each line trimmed of leading/trailing spaces/tabs, only outer
  blank lines (top/bottom) removed, middle blanks preserved. Per user spec.
* .cue — canonical_cue_text
* .lrc / embedded LYRICS — canonical_lyrics + format_lyrics_text
* Audio tags — leading/trailing spaces and blank lines stripped

It is intentionally non-destructive: it only rewrites files that are not
already in canonical form, and it never regenerates .accurip via CUETools
(it just trims). Use Generate AccurateRip to recreate content.
"""

import os
import io
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from .accurip import _canonical_accurip_text
from .audio import AudioFile
from .config import should_write_audio_tag
from .cue import canonical_cue_text
from .deps import HAS_PIL, Image
from .lyrics import _canonical_lyrics, format_lyrics_text
from .paths import AUDIO_EXTS
from .stats import _collect_targets, _walk_files, new_stats, _make_pbar, worker_count
from .ui import print_header, log, c, Color

# Canonical on-disk cover names (mirrors the grader's COVER_NAMES).
_COVER_NAMES = {"cover.jpg", "cover.jpeg", "cover.png", "cover.jxl"}


def _find_cover_file(album_dir):
    try:
        for f in os.listdir(album_dir):
            if f.lower() in _COVER_NAMES:
                return os.path.join(album_dir, f)
    except OSError:
        pass
    return None


def _prepare_embedded_cover(album_dir, cfg):
    """(data, mime) of the album cover, prepared for embedding.

    JPEG embeds are re-encoded at embed_cover_jpeg_quality (only JPEG
    honors a quality setting — PNG/lossless embeds are lossless by
    definition). When embed_cover_resolution is set, art larger than the
    cap is downscaled, aspect ratio preserved. Returns None when the
    album has no on-disk cover to embed.
    """
    cover_path = _find_cover_file(album_dir)
    if not cover_path:
        return None
    try:
        with open(cover_path, "rb") as f:
            data = f.read()
    except OSError:
        return None

    ext = os.path.splitext(cover_path)[1].lower()
    if ext == ".jxl":
        return (data, "image/jxl")
    mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"

    if not HAS_PIL:
        return (data, mime)
    quality = int(cfg.get("embed_cover_jpeg_quality") or 90)
    resolution = int(cfg.get("embed_cover_resolution") or 0)
    try:
        img = Image.open(io.BytesIO(data))
        if resolution > 0 and max(img.size) > resolution:
            # thumbnail() never upscales and keeps the aspect ratio
            img = img.convert("RGB") if mime == "image/jpeg" else img
            img.thumbnail((resolution, resolution), Image.LANCZOS)
        if mime != "image/jpeg":
            return (data, mime)
        out = io.BytesIO()
        img.convert("RGB").save(
            out, "JPEG", quality=quality,
            progressive=bool(cfg.get("jpeg_progressive", True)),
        )
        return (out.getvalue(), "image/jpeg")
    except Exception:
        return (data, mime)


def _format_embedded_covers(path, cfg, cover_cache):
    """Embedded-art pass driven by the embed_covers setting.

    OFF (default): audio files carry no embedded art — any pictures are
    removed. ON: the album's on-disk cover is embedded into every track
    (replacing whatever art is already there). Both directions only
    rewrite files that actually change.
    """
    try:
        af = AudioFile(path)
        if af.audio is None or af.kind in ("video", "aac"):
            return (path, False, None)
        pics = af.embedded_pictures()
        if not bool(cfg.get("embed_covers", False)):
            if not pics:
                return (path, False, None)
            if not af.remove_embedded_pictures():
                return (path, False, af.error or "could not remove embedded art")
            return (path, True, None)

        album_dir = os.path.dirname(path)
        if album_dir not in cover_cache:
            cover_cache[album_dir] = _prepare_embedded_cover(album_dir, cfg)
        prep = cover_cache[album_dir]
        if not prep:
            return (path, False, None)  # no on-disk cover — leave audio untouched
        data, mime = prep
        if len(pics) == 1 and pics[0][0] == mime and pics[0][1] == data:
            return (path, False, None)  # exactly this art is already embedded
        if pics and not af.remove_embedded_pictures():
            return (path, False, af.error or "could not replace embedded art")
        if not af.add_embedded_picture(data, mime):
            return (path, False, af.error or "could not embed cover")
        return (path, True, None)
    except Exception as e:
        return (path, False, str(e))


def _format_accurip_file(path, cfg=None, force=False):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            original = f.read()
    except OSError as e:
        return (path, False, str(e))
    # Use same canonical logic as grader and generator — respects keep_empty and append
    # Default False removes the extra blank line at bottom, matching .cue's rstrip() handling
    append = bool((cfg or {}).get("append_final_newline", False))
    keep_empty = bool((cfg or {}).get("keep_empty_accurip_lines", False))
    canonical = _canonical_accurip_text(original, keep_empty_lines=keep_empty, append_final_newline=append)
    expected = canonical
    if not force and original == expected:
        return (path, False, None)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".accurip_fmt_", suffix=".accurip", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(expected)
            try:
                out.flush()
                os.fsync(out.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
        # Ensure directory entry is durable
        try:
            d_fd = os.open(os.path.dirname(path) or ".", os.O_DIRECTORY)
            try:
                os.fsync(d_fd)
            finally:
                os.close(d_fd)
        except Exception:
            pass
        return (path, True, None)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except:
            pass
        return (path, False, str(e))


def _format_cue_file(path, cfg, force=False):
    try:
        with open(path, "rb") as raw:
            data = raw.read()
        if b"\x00" in data:
            return (path, False, None)
        if data.startswith(b"\xef\xbb\xbf"):
            # BOM will be stripped, so needs formatting
            pass
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                original = f.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="latin-1", newline="") as f:
                original = f.read()
        canonical = canonical_cue_text(
            original,
            keep_empty_lines=cfg.get("keep_empty_cue_lines", False),
            keep_other_lines=cfg.get("keep_other_cue_lines", False),
            file_type=cfg.get("cue_file_type", "WAVE"),
            append_final_newline=cfg.get("append_final_newline", False),
        )
        if not force and canonical == original:
            return (path, False, None)
        tmp = None
        fd, tmp = tempfile.mkstemp(prefix=".cue_fmt_", suffix=".cue", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(canonical)
            try:
                out.flush()
                os.fsync(out.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
        try:
            d_fd = os.open(os.path.dirname(path) or ".", os.O_DIRECTORY)
            try:
                os.fsync(d_fd)
            finally:
                os.close(d_fd)
        except Exception:
            pass
        return (path, True, None)
    except Exception as e:
        return (path, False, str(e))


def _lrc_expected(original, cfg, is_lrc_file=True):
    """Compute canonical expected text for LRC sidecar or embedded lyrics."""
    from mlo.lyrics import format_lyrics_text as flt, _canonical_lyrics as cl
    target = "LRC" if is_lrc_file else "EMBEDDED"
    eff_zero = bool(cfg.get("lrc_add_zero_timestamp", False)) and cfg.get("lrc_zero_timestamp_target", "BOTH") in (target, "BOTH")
    return cl(
        flt(
            original,
            precision=int(cfg.get("lrc_timestamp_precision", 2) or 2),
            strip_metadata=cfg.get("lrc_strip_metadata", True),
            collapse_blank_lines=cfg.get("lrc_collapse_blank_lines", True),
            lrc_enhanced_enabled=bool(cfg.get("lrc_enhanced_enabled", True)),
            lrc_enhanced_word_sync=bool(cfg.get("lrc_enhanced_word_sync", True)),
            lrc_extended_enabled=bool(cfg.get("lrc_extended_enabled", True)),
            lrc_add_zero_timestamp=eff_zero,
            lrc_zero_timestamp_blank=bool(cfg.get("lrc_zero_timestamp_blank", False)),
        ),
        append_final_newline=cfg.get("append_final_newline", False),
    )


def _format_lrc_file(path, cfg, force=False):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            original = f.read()
        if not original.strip():
            return (path, False, None)
        expected = _lrc_expected(original, cfg, is_lrc_file=True)
        if not force and original == expected:
            return (path, False, None)
        # If already canonical but force=False, skip. With force, rewrite anyway.
        tmp = None
        fd, tmp = tempfile.mkstemp(prefix=".lrc_fmt_", suffix=".lrc", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(expected)
            try:
                out.flush()
                os.fsync(out.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
        try:
            d_fd = os.open(os.path.dirname(path) or ".", os.O_DIRECTORY)
            try:
                os.fsync(d_fd)
            finally:
                os.close(d_fd)
        except Exception:
            pass
        return (path, True, None)
    except Exception as e:
        return (path, False, str(e))


def _format_audio_tags(path, cfg, force=False):
    try:
        af = AudioFile(path)
        if af.audio is None:
            return (path, False, None)
        changed = False
        af.defer_save(True)
        for key, val in list(af.all_tags().items()):
            if val is None:
                continue
            raw = str(val)
            # Skip tags that the config says must not be written — and also not graded —
            # so leaving them unformatted is consistent with grading, and avoids wasted writes.
            if not should_write_audio_tag(cfg, key, filepath=path):
                continue
            if key.upper() in ("LYRICS", "UNSYNCEDLYRICS"):
                try:
                    expected = _lrc_expected(raw, cfg, is_lrc_file=False)
                    if expected != raw:
                        if af.set_tag(key, expected):
                            changed = True
                        else:
                            af.defer_save(False)
                            return (path, False, af.error or "set_tag failed")
                    continue
                except Exception:
                    pass
            # For all other tags, trim each line and remove ALL blank lines
            stripped_lines = [ln.strip(" \t") for ln in raw.split("\n")]
            fixed_lines = [ln for ln in stripped_lines if ln != ""]
            if not fixed_lines and raw.strip() == "":
                fixed = ""
            else:
                fixed = "\n".join(fixed_lines)
            if fixed != raw:
                if af.set_tag(key, fixed):
                    changed = True
                else:
                    af.defer_save(False)
                    return (path, False, af.error or "set_tag failed")
        # Optimization leaves only tags this app (and its graders) understand:
        # anything outside TAG_MAP plus the encoder identity tags is removed.
        if cfg.get("strip_unknown_tags", False):
            from .audio import TAG_MAP
            allowed = {k.upper() for k in TAG_MAP} | {
                "ENCODER_PROGRAM", "ENCODER_QUALITY", "ENCODER_VERSION"}
            for key in list(af.all_tags().keys()):
                if str(key).upper() not in allowed:
                    try:
                        if af.delete_tag(key):
                            changed = True
                    except Exception:
                        pass
        af.defer_save(False)
        if changed:
            return (path, True, None)
        return (path, False, None)
    except Exception as e:
        return (path, False, str(e))


def run_format_all(config):
    """Final formatting pass — dynamically detects and fixes incorrect formatting.

    Covers .accurip (trim lines, outer blanks only), .cue, .lrc, and audio tags.
    Intended to run at the end of Run All so grading will pass without needing
    per-type forced runs. Only rewrites files that are not already canonical.
    """
    folder = config["music_folder"]
    stats = new_stats()
    print_header("Format All (Final Pass)")
    log(f"music folder: {folder} · detects incorrect formatting and fixes only what needs it")

    if not os.path.isdir(folder):
        log(c(f"ERROR: folder does not exist: {folder}", Color.RED))
        return stats

    targets = config.get("targets")
    # Collect all relevant files
    audio_files = _collect_targets(targets, AUDIO_EXTS) if targets is not None else None
    if targets is not None and audio_files is not None:
        # Derive album dirs from targets for sidecar collection
        album_dirs = sorted({os.path.dirname(f) for f in audio_files})
        # Collect sidecars only in those album dirs
        accurip_files = []
        cue_files = []
        lrc_files = []
        for ad in album_dirs:
            try:
                for f in os.listdir(ad):
                    full = os.path.join(ad, f)
                    low = f.lower()
                    if low.endswith(".accurip"):
                        accurip_files.append(full)
                    elif low.endswith(".cue"):
                        cue_files.append(full)
                    elif low.endswith(".lrc"):
                        lrc_files.append(full)
            except OSError:
                continue
        # Also collect audio files themselves for tag formatting
        audio_to_check = audio_files
    else:
        # Full library walk
        accurip_files = sorted(_walk_files(folder, (".accurip",)))
        cue_files = sorted(_walk_files(folder, (".cue",)))
        lrc_files = sorted(_walk_files(folder, (".lrc",)))
        audio_to_check = sorted(_walk_files(folder, AUDIO_EXTS))

    total_tasks = len(accurip_files) + len(cue_files) + len(lrc_files) + 2 * len(audio_to_check)
    if total_tasks == 0:
        log("No files found to format.")
        return stats

    log(f"found {len(accurip_files)} .accurip, {len(cue_files)} .cue, {len(lrc_files)} .lrc, {len(audio_to_check)} audio files")

    # Per-family force switches (same keys the individual scripts use) — a
    # forced family is rewritten even when it is already canonical.
    force = {
        "accurip": bool(config.get("force_accurip", False)),
        "cue": bool(config.get("force_cue", False)),
        "lrc": bool(config.get("force_lyrics", False)),
        "tags": bool(config.get("force_auto_tag", False)),
    }

    # Use thread pool for I/O-bound formatting
    workers = worker_count(config, default=8, maximum=16, items=total_tasks)
    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(total_tasks, "Formatting", unit="file")

    def _report(fn, ok, err):
        if err:
            stats["error_count"] += 1
            stats["errors"].append((fn, err))
            log(c(f"  ✕ {os.path.basename(fn)}: {err}", Color.RED))
        elif ok:
            stats["modified_count"] += 1
            stats["total_scanned"] += 1
            log(f"  ✓ {os.path.relpath(fn, folder) if os.path.commonpath([folder, fn])==folder else fn} → formatted")
        else:
            stats["skipped_count"] += 1

    with ThreadPoolExecutor(max_workers=workers) as ex:
        # .accurip
        futures = {}
        for f in accurip_files:
            fut = ex.submit(_format_accurip_file, f, config, force["accurip"])
            futures[fut] = f
        for fut in as_completed(futures):
            fn, ok, err = fut.result()
            _report(fn, ok, err)
            if err:
                counts["fail"] += 1
            elif ok:
                counts["ok"] += 1
                if pbar:
                    try: pbar.update(1)
                    except: pass
            else:
                counts["skip"] += 1
                if pbar:
                    try: pbar.update(1)
                    except: pass
        # .cue
        futures = {}
        for f in cue_files:
            fut = ex.submit(_format_cue_file, f, config, force["cue"])
            futures[fut] = f
        for fut in as_completed(futures):
            fn, ok, err = fut.result()
            _report(fn, ok, err)
            if err:
                counts["fail"] += 1
            elif ok:
                counts["ok"] += 1
                if pbar:
                    try: pbar.update(1)
                    except: pass
            else:
                counts["skip"] += 1
                if pbar:
                    try: pbar.update(1)
                    except: pass
        # .lrc
        futures = {}
        for f in lrc_files:
            fut = ex.submit(_format_lrc_file, f, config, force["lrc"])
            futures[fut] = f
        for fut in as_completed(futures):
            fn, ok, err = fut.result()
            _report(fn, ok, err)
            if err:
                counts["fail"] += 1
            elif ok:
                counts["ok"] += 1
                if pbar:
                    try: pbar.update(1)
                    except: pass
            else:
                counts["skip"] += 1
                if pbar:
                    try: pbar.update(1)
                    except: pass
        # audio tags
        futures = {}
        for f in audio_to_check:
            fut = ex.submit(_format_audio_tags, f, config, force["tags"])
            futures[fut] = f
        for fut in as_completed(futures):
            fn, ok, err = fut.result()
            # Only log when actually changed to avoid noise; tagged files are many
            if err:
                # Don't spam for unreadable files
                counts["fail"] += 1
                if pbar:
                    try: pbar.update(1)
                    except: pass
                continue
            if ok:
                stats["modified_count"] += 1
                stats["total_scanned"] += 1
                log(f"  ✓ {os.path.relpath(fn, folder) if os.path.commonpath([folder, fn])==folder else fn} → tags trimmed")
                counts["ok"] += 1
            else:
                stats["skipped_count"] += 1
                counts["skip"] += 1
            if pbar:
                try: pbar.update(1)
                except: pass
        # embedded cover art — remove (default) or embed the album cover
        cover_cache = {}
        futures = {}
        for f in audio_to_check:
            fut = ex.submit(_format_embedded_covers, f, config, cover_cache)
            futures[fut] = f
        for fut in as_completed(futures):
            fn, ok, err = fut.result()
            if err:
                counts["fail"] += 1
                stats["error_count"] += 1
                stats["errors"].append((fn, err))
                log(c(f"  ✕ {os.path.basename(fn)}: {err}", Color.RED))
                if pbar:
                    try: pbar.update(1)
                    except: pass
                continue
            if ok:
                stats["modified_count"] += 1
                stats["total_scanned"] += 1
                action = "cover embedded" if config.get("embed_covers") else "embedded art removed"
                log(f"  ✓ {os.path.relpath(fn, folder) if os.path.commonpath([folder, fn])==folder else fn} → {action}")
                counts["ok"] += 1
            else:
                stats["skipped_count"] += 1
                counts["skip"] += 1
            if pbar:
                try: pbar.update(1)
                except: pass

    if pbar:
        try: pbar.close()
        except: pass

    log(f"Format All: {counts['ok']} formatted, {counts['skip']} already correct, {counts['fail']} errors")
    return stats
