"""Format All — final pass to ensure every file/tag is correctly formatted.

This script runs at the end of Run All / Optimize Selected and dynamically
detects what needs formatting, fixing only what is incorrect:

* .accurip — each line trimmed of leading/trailing spaces/tabs, only outer
  blank lines (top/bottom) removed, middle blanks preserved. Per user spec.
* .cue — canonical_cue_text
* .lrc / embedded LYRICS — canonical_lyrics + format_lyrics_text
* Audio tags — leading/trailing spaces and blank lines stripped, and the tags
  that have a canonical spelling written in it (mlo.tagtext: MEDIA, SOURCE,
  RELEASETYPE, RELEASESTATUS, AUDIT, RELEASECOUNTRY, SCRIPT, MOOD)

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
from .autotag import genre_count, trim_genres
from .config import should_write_audio_tag
from .cue import canonical_cue_text
from .deps import HAS_PIL, Image
from .images import _exif_transposed
from .lyrics import _canonical_lyrics, format_lyrics_text
from .paths import AUDIO_EXTS, IMAGE_EXTS, fsync_dir
from .stats import _collect_targets, _walk_files, new_stats, _make_pbar, worker_count
from .tagtext import canonical_text
from .ui import print_header, log, c, Color

# Canonical on-disk cover names (the grader's COVER_NAMES) plus the
# front/folder aliases script 2 treats as the album cover (mlo.images):
# with rename_to_cover off the art is called "folder.jpg", and the embed
# pass found nothing while the album was graded with a cover.
_COVER_NAMES = {"cover.jpg", "cover.jpeg", "cover.png", "cover.jxl"}
_COVER_STEMS = ("cover", "front", "folder")


def _find_cover_file(album_dir):
    """Path of the album's cover image, canonical cover.* first."""
    try:
        names = os.listdir(album_dir)
    except OSError:
        return None
    alias = None
    for f in names:
        low = f.lower()
        if low in _COVER_NAMES:
            return os.path.join(album_dir, f)
        if alias is None:
            stem, ext = os.path.splitext(low)
            if stem in _COVER_STEMS and ext in IMAGE_EXTS:
                alias = os.path.join(album_dir, f)
    return alias


def cover_mime(ext):
    """Embedded-art MIME type for a cover file extension."""
    ext = (ext or "").lower()
    if ext == ".jxl":
        return "image/jxl"
    return "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"


def prepare_cover_bytes(data, mime, cfg):
    """(data, mime) of cover bytes prepared for embedding.

    The cover policy the on-disk cover is held to (mlo.images.
    ``_resize_and_crop_image``, driven by cover_crop_enabled /
    cover_crop_threshold / cover_force_exact_size + cover_target_size)
    applies to EMBEDDED art too, so a track does not carry the raw
    non-square original the grader fails on disk. It runs FIRST, then the
    embed resolution cap: JPEG embeds are re-encoded at
    embed_cover_jpeg_quality (only JPEG honors a quality setting — PNG/
    lossless embeds are lossless by definition) and anything larger than
    embed_cover_resolution is downscaled, aspect ratio preserved.
    Shared by the optimizer and the exporter, so both apply identical
    rules to on-disk covers *and* to art taken out of a source file's own
    tags; a cfg without the policy keys (the exporter's own options) keeps
    them off, so its documented quality/resolution behaviour is unchanged.
    """
    if not HAS_PIL or mime == "image/jxl":
        return (data, mime)
    quality = int(cfg.get("embed_cover_jpeg_quality") or 90)
    resolution = int(cfg.get("embed_cover_resolution") or 0)
    try:
        img = Image.open(io.BytesIO(data))
        img = _exif_transposed(img)
        resized = False
        try:
            threshold = max(0.0, min(0.5, float(cfg.get("cover_crop_threshold") or 0.0)))
        except (TypeError, ValueError):
            threshold = 0.0
        force_exact = bool(cfg.get("cover_force_exact_size"))
        try:
            target = int(cfg.get("cover_target_size") or 0) if force_exact else 0
        except (TypeError, ValueError):
            target = 0
        width, height = img.size
        if (height and abs(width / height - 1.0) > threshold
                and (cfg.get("cover_crop_enabled") or (force_exact and target > 0))):
            # Center-crop the longer side just inside the threshold — the
            # same geometry images._resize_and_crop_image uses, so the
            # embedded art and the file on disk are the same square.
            if width > height:
                side = max(height, min(int(height * (1.0 + threshold)), width))
                left = (width - side) // 2
                img = img.crop((left, 0, left + side, height))
            else:
                side = max(width, min(int(width * (1.0 + threshold)), height))
                top = (height - side) // 2
                img = img.crop((0, top, width, top + side))
            resized = True
            width, height = img.size
        if target > 0 and max(width, height) > target:
            # force_exact wants exactly target x target; never upscale.
            img = img.convert("RGB") if mime == "image/jpeg" else img
            img = img.resize((target, target), Image.LANCZOS)
            resized = True
        if resolution > 0 and max(img.size) > resolution:
            # thumbnail() never upscales and keeps the aspect ratio
            img = img.convert("RGB") if mime == "image/jpeg" else img
            img.thumbnail((resolution, resolution), Image.LANCZOS)
            resized = True
        if mime != "image/jpeg":
            if not resized:
                return (data, mime)
            out = io.BytesIO()
            img.save(out, "PNG", optimize=True)
            return (out.getvalue(), mime)
        out = io.BytesIO()
        img.convert("RGB").save(
            out, "JPEG", quality=quality,
            progressive=bool(cfg.get("jpeg_progressive", True)),
        )
        return (out.getvalue(), "image/jpeg")
    except Exception:
        return (data, mime)


def _prepare_embedded_cover(album_dir, cfg):
    """(data, mime) of the album's on-disk cover, prepared for embedding.

    Returns None when the album has no cover.* file to embed.
    """
    cover_path = _find_cover_file(album_dir)
    if not cover_path:
        return None
    try:
        with open(cover_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return prepare_cover_bytes(data, cover_mime(os.path.splitext(cover_path)[1]), cfg)


def _format_embedded_covers(path, cfg, cover_cache, af=None):
    """Embedded-art pass driven by the embed_covers setting.

    OFF (default): audio files carry no embedded art — any pictures are
    removed. ON: the album's on-disk cover is embedded into every track
    (replacing whatever art is already there). Both directions only
    rewrite files that actually change.

    *af* is an already-open handle — the fused pass opens each file once
    (see _format_audio_file); art writes go through _save_container(), so
    unlike the tag pass this one writes the container itself.
    """
    try:
        af = af or AudioFile(path)
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
        # Replacing art is TWO mutations of one container — strip the old
        # picture, then add the new one. Each wrote the whole file for itself
        # and the tag pass below wrote a third time; deferring both into one
        # flush saves a full container rewrite per re-covered file, and the
        # tag pass only writes again when a tag actually changed.
        af.defer_save(True)
        if pics and not af.remove_embedded_pictures():
            af.defer_save(False)
            return (path, False, af.error or "could not replace embedded art")
        if not af.add_embedded_picture(data, mime):
            af.defer_save(False)
            return (path, False, af.error or "could not embed cover")
        if af.defer_save(False) is False:
            # A failed flush is an error, never a reported success: the art is
            # not on disk and grading would keep failing on the old picture.
            return (path, False, af.error or "could not write embedded art")
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
        fsync_dir(os.path.dirname(path))
        return (path, True, None)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except:
            pass
        return (path, False, str(e))


def _format_cue_file(path, cfg, force=False):
    # FILE references are repointed ONCE per album folder by run_format_all
    # before any sheet is submitted (see the .cue block there): doing it here
    # re-read and re-matched every sheet in the folder once per sheet.
    try:
        with open(path, "rb") as raw:
            data = raw.read()
        if b"\x00" in data:
            return (path, False, None)
        # Decoded from the bytes just read. Opening the file again for
        # utf-8-sig — and a third time for latin-1 — read the very same bytes
        # off the disk twice more; decoding the buffer is the same text.
        try:
            original = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            original = data.decode("latin-1")
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
        fsync_dir(os.path.dirname(path))
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
        with open(path, "rb") as raw:
            data = raw.read()
        if b"\x00" in data:
            return (path, False, None)
        # Never errors="replace": this text is written back, so a mis-decode
        # would permanently corrupt a CP1252/Shift-JIS sidecar into U+FFFD.
        # No latin-1 fallback either — latin-1 decodes ANY byte string, so a
        # non-UTF-8 sidecar would decode to mojibake and be rewritten as UTF-8,
        # destroying the original encoding. Leave such files untouched.
        try:
            original = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return (path, False, None)
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
        fsync_dir(os.path.dirname(path))
        return (path, True, None)
    except Exception as e:
        return (path, False, str(e))


def _trim_tag_lines(raw):
    """One tag value with each line trimmed and every blank line removed."""
    lines = [ln.strip(" \t") for ln in str(raw).split("\n")]
    return "\n".join(ln for ln in lines if ln != "")


def _format_tag_values(key, raw):
    """The value script 10 would WRITE for one stored *raw* value.

    Lines trimmed and blank ones dropped (as before), then the canonical
    spelling and spacing the write path applies for this tag (mlo.tagtext) —
    the same rule set_tag uses, so the comparison below and the write can
    never disagree about whether a file needs rewriting.
    """
    return canonical_text(key, _trim_tag_lines(raw))


def _format_audio_tags(path, cfg, force=False, af=None):
    """Trim every tag's lines, drop blank ones, canonicalise the tags that have
    a canonical spelling (mlo.tagtext) and cap GENRE at `mb_genre_count`.
    Returns (path, ok, err, genres_trimmed, canonicalized).

    *af* is an already-open handle (the fused pass opens the file once);
    the caller then owns nothing about it — this pass still flushes its own
    deferred write.
    """
    try:
        af = af or AudioFile(path)
        if af.audio is None:
            return (path, False, None, 0, 0)
        changed = False
        # How many values this pass rewrote INTO canonical form (spelling or
        # spacing) — the count the run reports, the way genres_trimmed counts
        # the genre cap.
        canonicalized = 0
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
                # Same gate script 1 uses (mlo/lyrics.py:465) — Format All runs
                # last, so it must not override optimize_embedded_lyrics.
                if not (force or cfg.get("optimize_embedded_lyrics", True)):
                    continue
                try:
                    expected = _lrc_expected(raw, cfg, is_lrc_file=False)
                    if force or expected != raw:
                        if af.set_tag(key, expected):
                            changed = True
                        else:
                            af.defer_save(False)
                            return (path, False, af.error or "set_tag failed", 0, 0)
                    continue
                except Exception:
                    pass
            # For all other tags, trim each line and remove ALL blank lines.
            # A repeated tag (three GENREs, say) is rewritten as the WHOLE
            # list it holds: all_tags() shows the repeats "; "-joined, and
            # feeding that one string back to set_tag wrote a single value
            # literally named "Rock; Pop" — the other two genres were gone
            # from the file. tag_values() gives the pieces back.
            values = af.tag_values(key) or [raw]
            # Lines trimmed, blank ones dropped, and the tags that HAVE a
            # canonical spelling/spacing rewritten here (mlo.tagtext) — the
            # same rule set_tag applies, so a value this decides is "already
            # right" is one set_tag would not have changed either.
            fixed = [_format_tag_values(key, v) for v in values]
            canonicalized += sum(1 for before, after in zip(values, fixed)
                                 if before != after)
            if force or fixed != values:
                if af.set_tag(key, fixed if len(fixed) > 1 else fixed[0]):
                    changed = True
                else:
                    af.defer_save(False)
                    return (path, False, af.error or "set_tag failed", 0, 0)
        # The per-track genre cap (`mb_genre_count`) is swept here too, over
        # the whole library, so an existing album that carries more genres than
        # the setting allows is fixed by running this one script — the same cap
        # (and the same canonicalization: the family lands last and unknown
        # spellings are resolved) the import and Auto tagging apply, from one
        # config key. GENRE goes through the write gate the loop above applies
        # to it.
        trimmed = 0
        if should_write_audio_tag(cfg, "GENRE", filepath=path):
            try:
                trimmed = trim_genres(af, genre_count(cfg))
            except Exception:
                trimmed = 0
            if trimmed:
                changed = True
        # Optimization leaves only tags this app (and its graders) understand:
        # anything outside the shared vocabulary — TAG_MAP, the encoder
        # identity tags, beets/Picard's own spellings and the app's
        # AUDIOAUDITOR_OVERRIDE — is removed. The predicate comes from the
        # grader (mlo.grader.tag_key_allowed) so the strip pass and the
        # excess-tag grade can never disagree about what "excess" means.
        # Default True to match DEFAULT_CONFIG (mlo/config.py:821) — a partial
        # cfg (tests, smoke suites) must strip like the shipped app does.
        if cfg.get("strip_unknown_tags", True):
            from .grader import tag_key_allowed
            for key in list(af.all_tags().keys()):
                if tag_key_allowed(key):
                    continue
                try:
                    if af.delete_tag(key):
                        changed = True
                except Exception:
                    pass
        # The one container write of this pass: a failed flush (a read-only
        # file, a full disk) must NOT be reported as "formatted" — the tags
        # never reached disk and grading would keep failing on them.
        if af.defer_save(False) is False:
            return (path, False, af.error or "tag write failed", 0, 0)
        if changed:
            return (path, True, None, trimmed, canonicalized)
        return (path, False, None, trimmed, canonicalized)
    except Exception as e:
        return (path, False, str(e), 0, 0)


def _format_audio_file(path, cfg, force, cover_cache):
    """Tag pass + embedded-art pass over ONE open handle.

    The two passes used to run as separate futures, so every file was
    opened and its container parsed twice per Format All run. Runs the
    cover pass first (its art write is immediate either way), then the tag
    pass, which owns the deferred write of this file.
    Returns ((tag_ok, err), (cover_ok, err), genres_trimmed, canonicalized).
    """
    try:
        af = AudioFile(path)
    except Exception as e:
        return (path, (False, str(e)), (False, str(e)), 0, 0)
    covers = _format_embedded_covers(path, cfg, cover_cache, af=af)
    tags = _format_audio_tags(path, cfg, force, af=af)
    return (path, (tags[1], tags[2]), (covers[1], covers[2]), tags[3], tags[4])


def run_format_all(config):
    """Final formatting pass — dynamically detects and fixes incorrect formatting.

    Covers .accurip (trim lines, outer blanks only), .cue, .lrc, and audio tags.
    Intended to run at the end of Run All so grading will pass without needing
    per-type forced runs. Only rewrites files that are not already canonical.
    """
    folder = config["music_folder"]
    stats = new_stats()
    # Extra GENRE values this run dropped to reach `mb_genre_count`; always
    # present so a caller reads a count, not a missing key.
    stats["genres_trimmed"] = 0
    # Tag values this run rewrote into their canonical spelling or spacing
    # (mlo.tagtext) — always present for the same reason.
    stats["tags_canonicalized"] = 0
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
        # ONE walk of the library, bucketed by extension: the four
        # _walk_files passes below walked every directory four times for the
        # same answers. The bucket lists stay sorted exactly as before.
        buckets = {".accurip": [], ".cue": [], ".lrc": []}
        audio_to_check = []
        for f in sorted(_walk_files(folder, AUDIO_EXTS + (".accurip", ".cue", ".lrc"))):
            low = os.path.splitext(f)[1].lower()
            if low in AUDIO_EXTS:
                audio_to_check.append(f)
            else:
                buckets[low].append(f)
        accurip_files = buckets[".accurip"]
        cue_files = buckets[".cue"]
        lrc_files = buckets[".lrc"]

    # Script 1 gates .lrc cleaning on optimize_lrc (mlo/lyrics.py:475) and
    # Format All runs last, so the same key is honoured here — otherwise a
    # "Optimize LRC = off" setting gets silently overridden.
    if not (config.get("force_lyrics", False) or config.get("optimize_lrc", True)):
        lrc_files = []

    total_tasks = len(accurip_files) + len(cue_files) + len(lrc_files) + len(audio_to_check)
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
        # Repoint stale FILE references once per ALBUM folder, before any of
        # its sheets is submitted: _format_cue_file used to do it for itself,
        # so a folder with N sheets re-read and re-matched all of them N times
        # (and a sheet could be formatted before a sibling's repair landed).
        try:
            from .discs import fix_cue_filenames
            for ad in sorted({os.path.dirname(f) or "." for f in cue_files}):
                try:
                    fix_cue_filenames(ad, config=config)
                except Exception:
                    pass
        except Exception:
            pass
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
        # audio tags + embedded art: ONE open per file (see _format_audio_file)
        cover_cache = {}
        futures = {}
        for f in audio_to_check:
            fut = ex.submit(_format_audio_file, f, config, force["tags"], cover_cache)
            futures[fut] = f
        for fut in as_completed(futures):
            fn, tag_res, cover_res, genres_trimmed, tags_canonicalized = fut.result()
            # Tags: only log when actually changed to avoid noise; tagged
            # files are many, and unreadable ones must not spam.
            ok, err = tag_res
            if err:
                counts["fail"] += 1
            elif ok:
                stats["modified_count"] += 1
                stats["total_scanned"] += 1
                extra = ""
                if genres_trimmed:
                    extra += f" ({genres_trimmed} extra genre value(s) dropped)"
                if tags_canonicalized:
                    extra += f" ({tags_canonicalized} tag value(s) canonicalized)"
                log(f"  ✓ {os.path.relpath(fn, folder) if os.path.commonpath([folder, fn])==folder else fn} → tags trimmed{extra}")
                counts["ok"] += 1
            else:
                stats["skipped_count"] += 1
                counts["skip"] += 1
            if genres_trimmed:
                # The canonical sweep's own count: what the per-track genre
                # cap actually removed from the library in this run.
                stats["genres_trimmed"] += genres_trimmed
            if tags_canonicalized:
                # …and how many values the canonical VALUE rule rewrote.
                stats["tags_canonicalized"] += tags_canonicalized
            # Embedded art — removed (default) or the album cover embedded.
            ok, err = cover_res
            if err:
                counts["fail"] += 1
                stats["error_count"] += 1
                stats["errors"].append((fn, err))
                log(c(f"  ✕ {os.path.basename(fn)}: {err}", Color.RED))
            elif ok:
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
    trimmed_total = stats.get("genres_trimmed", 0)
    if trimmed_total:
        # What the canonical genre sweep removed, against the one knob
        # (Settings → Import) that defines it.
        cap = genre_count(config)
        log(f"  GENRE: {trimmed_total} extra genre value(s) trimmed — genres per track is {cap} (Settings → Import)")
    canon_total = stats.get("tags_canonicalized", 0)
    if canon_total:
        # The canonical VALUE rule's own count (mlo.tagtext): values that were
        # spelled or spaced differently from what every writer now stores.
        log(f"  TAGS: {canon_total} value(s) rewritten to their canonical "
            f"spelling/spacing")
    return stats
