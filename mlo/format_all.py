"""Format All — final pass to ensure every file/tag is correctly formatted.

This script runs at the end of Run All / Optimize Selected and dynamically
detects what needs formatting, fixing only what is incorrect:

* .accurip — each line trimmed of leading/trailing spaces/tabs, only outer
  blank lines (top/bottom) removed, middle blanks preserved. Per user spec.
* .cue — canonical_cue_text
* .lrc / embedded LYRICS — canonical_lyrics + format_lyrics_text
* Audio tags — leading/trailing spaces and blank lines stripped, and the tags
  that have a canonical spelling written in it (mlo.tagtext: MEDIA, SOURCE,
  RELEASETYPE, RELEASESTATUS, AUDIT, RELEASECOUNTRY, SCRIPT, MOOD), plus the
  tags this app would never write: `excess_tags` — anything outside the shared
  vocabulary, a `COMMENT` carrying a value and the alias family's own excess
  (R16b) — deleted by `strip_excess_tags`, the ONE stripper the Tag hygiene
  script (mlo.taghygiene, script 23) runs on its own, scoped to what the user
  pressed, when an album's junk tags are to be cleared without this pass.

It is intentionally non-destructive: it only rewrites files that are not
already in canonical form, and it never regenerates .accurip via CUETools
(it just trims). Use Generate AccurateRip to recreate content.
"""

import os
import io
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .accurip import _canonical_accurip_text
from .audio import AudioFile
from .autotag import genre_count, trim_genres
from .config import _ext_to_audio_type, should_write_audio_tag
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


# One album cover is prepared once per run and reused by every track of that
# album (`cover_cache`, built per run in run_format_all). An album's tracks are
# formatted by DIFFERENT pool threads, so the cache is a plain dict mutated
# from all of them and needs a guard — and two tracks of one album can miss
# together, so a miss is single-flight through a per-ALBUM lock: the second
# thread in finds the entry already published rather than reading and preparing
# the same cover again.
#
# The single-flight lock is per album, not one for the whole cache. The
# critical section is a MISS — a cover read plus the Pillow decode/resize/encode,
# the most expensive thing this pass does besides the audio I/O itself — and one
# process-wide lock held across it made every other album's tracks queue behind
# it, so the pool's lanes could never prepare two albums' covers at once. Two
# albums share no cover file; only the same-album case needs exclusion. Locks
# are created on first use, as in mlo.fetchdeps.install_lock.
_cover_cache_lock = threading.Lock()   # guards cover_cache and _cover_load_locks
_cover_load_locks = {}                 # album_dir -> that album's prepare lock


def _cover_load_lock(album_dir):
    """The single-flight lock for ONE album's cover preparation."""
    with _cover_cache_lock:
        lock = _cover_load_locks.get(album_dir)
        if lock is None:
            lock = _cover_load_locks[album_dir] = threading.Lock()
        return lock


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
        # Single-flight per album (see _cover_load_lock): the other tracks of
        # this album are being formatted right now, and each of them asked for
        # this same cover before any of them had it.
        with _cover_cache_lock:
            hit = album_dir in cover_cache
            prep = cover_cache.get(album_dir)
        if not hit:
            with _cover_load_lock(album_dir):
                with _cover_cache_lock:
                    if album_dir in cover_cache:
                        hit = True
                        prep = cover_cache[album_dir]
                if not hit:
                    prep = _prepare_embedded_cover(album_dir, cfg)
                    with _cover_cache_lock:
                        cover_cache[album_dir] = prep
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
    # format_lyrics_text / _canonical_lyrics are already bound at module scope
    # (see the imports at the top): the local re-import that used to sit here
    # ran on every lyrics-bearing file of the library, for two names it could
    # not make cheaper.
    target = "LRC" if is_lrc_file else "EMBEDDED"
    eff_zero = bool(cfg.get("lrc_add_zero_timestamp", False)) and cfg.get("lrc_zero_timestamp_target", "BOTH") in (target, "BOTH")
    return _canonical_lyrics(
        format_lyrics_text(
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


def _format_lrc_file(path, cfg):
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
        # A forced run re-runs the canonicalisation, but a sidecar that already
        # holds exactly what this pass would write is left alone: rewriting it
        # stored the same bytes under a new timestamp (and on a forced
        # library-wide run, that was every sidecar).
        if original == expected:
            return (path, False, None)
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


def excess_tags(af, cfg=None):
    """Every tag *af* should not carry: ``[tag name]``, in the order read.

    The list is the grader's own, so a strip can never leave what the grade
    flags (or delete what it requires):

      * a NAME outside the shared vocabulary — TAG_MAP, the encoder identity
        tags, beets'/Picard's spellings, the alias families and the app's own
        override (`mlo.grader.tag_key_allowed`),
      * a `COMMENT` that carries a value, the one name the vocabulary HOLDS
        whose value nothing in this pipeline writes
        (`mlo.grader.tag_value_excess`),
      * an ALIAS tag nothing needs — the name is one the configured locale
        already reads, it is spelled for a locale the app does not write, it is
        a second spelling of the same alias, or its value is the name itself
        (`mlo.grader.alias_file_excess`, spec R16a/R16b). The family is in the
        vocabulary — the app writes it — so without this rule a stale alias
        would live on the file forever while the grade kept failing it.

    Gated by `strip_unknown_tags`, the switch that decides whether this app
    leaves only the tags its own writers produce: with it off nothing here is
    excess (which is also why the excess-tag grade stands down with it).
    ONE predicate for the two callers: Format All's tag pass below and the Tag
    hygiene script (`mlo.taghygiene`, script 23), which is this same strip on
    its own, scoped to what the user pressed.
    """
    if not (cfg or {}).get("strip_unknown_tags", True):
        return []
    from .grader import alias_file_excess, tag_key_allowed, tag_value_excess
    out = []
    for key, value in list(af.all_tags().items()):
        if tag_key_allowed(key) and not tag_value_excess(key, value):
            continue
        out.append(key)
    out.extend(key for key, _why in alias_file_excess(af, cfg))
    return out


def strip_excess_tags(af, cfg=None):
    """Delete `excess_tags` from *af*; the tag names that actually went.

    Compares the file's own tags before and after rather than trusting each
    `delete_tag`'s return: a container that matched nothing to delete (a
    spelling the tag API does not map onto a frame) must not be reported as a
    removal, and the caller's flush is then a no-op instead of a rewrite.
    Nothing is ever written here — a file whose list is empty is not touched
    at all.
    """
    names = excess_tags(af, cfg)
    if not names:
        return []
    before = {str(k).upper() for k in (af.all_tags() or {})}
    for key in names:
        try:
            af.delete_tag(key)
        except Exception:
            pass
    after = {str(k).upper() for k in (af.all_tags() or {})}
    return sorted(before - after)


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
        # The filetype the write gate derives from `path` is the same for every
        # tag of this file — the gate used to re-split the path on each of the
        # ~20-30 calls the loop below makes, and once more for GENRE.
        ftype = _ext_to_audio_type(os.path.splitext(path)[1])
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
            if not should_write_audio_tag(cfg, key, filepath=path, filetype=ftype):
                continue
            if key.upper() in ("LYRICS", "UNSYNCEDLYRICS"):
                # Same gate script 1 uses (mlo/lyrics.py:465) — Format All runs
                # last, so it must not override optimize_embedded_lyrics.
                # Forcing the script re-runs the canonicalisation, but a text
                # that is ALREADY the text this pass would write is left
                # alone: writing it back rewrote the whole file (with
                # flac_no_padding on, every tag write is a full re-encode) to
                # store the same bytes — and on a forced run that was every
                # track of the library.
                if not (force or cfg.get("optimize_embedded_lyrics", True)):
                    continue
                try:
                    expected = _lrc_expected(raw, cfg, is_lrc_file=False)
                    if expected != raw:
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
            # `canonicalized` above still counts what a write WOULD fix, but
            # only a real difference is written: a forced run re-runs the
            # canonicalisation over the whole library and used to rewrite (and
            # with flac_no_padding, whole-file re-encode) every track that was
            # already canonical — the same value, byte for byte.
            if fixed != values:
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
        if should_write_audio_tag(cfg, "GENRE", filepath=path, filetype=ftype):
            try:
                trimmed = trim_genres(af, genre_count(cfg))
            except Exception:
                trimmed = 0
            if trimmed:
                changed = True
        # Optimization leaves only tags this app (and its graders) understand:
        # anything outside the shared vocabulary — TAG_MAP, the encoder
        # identity tags, beets/Picard's own spellings and the app's
        # AUDIOAUDITOR_OVERRIDE — is removed, and so is a non-empty COMMENT and
        # the alias family's own excess (spec R16b); `excess_tags` is that one
        # list and `strip_excess_tags` the one deletion, shared with the Tag
        # hygiene script (mlo.taghygiene, script 23) so the two can never
        # disagree about what "excess" means. The predicates come from the
        # grader (mlo.grader.tag_key_allowed / tag_value_excess /
        # alias_file_excess) for the same reason.
        if strip_excess_tags(af, cfg):
            changed = True
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
    # forced family is re-scanned even when the setting says skip it. (The
    # .lrc family has no switch of its own any more: a forced run re-runs the
    # canonicalisation and a sidecar already holding it is left as it is, so
    # "rewrite anyway" had nothing to rewrite — see _format_lrc_file.)
    force = {
        "accurip": bool(config.get("force_accurip", False)),
        "cue": bool(config.get("force_cue", False)),
        "tags": bool(config.get("force_auto_tag", False)),
    }

    # Use thread pool for I/O-bound formatting
    workers = worker_count(config, maximum=16, items=total_tasks)
    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(total_tasks, "Formatting", unit="file")

    def _report(fn, ok, err):
        # Every sidecar this run looked at counts as scanned, whatever the
        # verdict — modified + skipped + errors is then the number of files the
        # pass examined, which is what the summary line claims.
        stats["total_scanned"] += 1
        if err:
            stats["error_count"] += 1
            stats["errors"].append((fn, err))
            log(c(f"  ✕ {os.path.basename(fn)}: {err}", Color.RED))
        elif ok:
            stats["modified_count"] += 1
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
            fut = ex.submit(_format_lrc_file, f, config)
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
            # ONE verdict per FILE: the tag pass and the art pass write the
            # same container, so a file whose tags were trimmed IS formatted
            # even when its art needed no change. Counting the two passes
            # separately reported one file twice — formatted once, "already
            # correct" once — which is how a file this run HAD rewritten came
            # out counted as skipped, and how "N formatted, M already correct"
            # could describe more files than were looked at.
            tag_ok, tag_err = tag_res
            cover_ok, cover_err = cover_res
            err = tag_err or cover_err
            if err:
                counts["fail"] += 1
                stats["error_count"] += 1
                stats["errors"].append((fn, err))
                log(c(f"  ✕ {os.path.basename(fn)}: {err}", Color.RED))
            elif tag_ok or cover_ok:
                # Only the success line names the file RELATIVE to the folder,
                # so only the success branch pays the two path operations —
                # this used to be computed for every file, error path included,
                # where the log only ever uses the basename.
                rel = (os.path.relpath(fn, folder)
                       if os.path.commonpath([folder, fn]) == folder else fn)
                counts["ok"] += 1
                stats["modified_count"] += 1
                actions = []
                if tag_ok:
                    actions.append("tags trimmed")
                if cover_ok:
                    actions.append("cover embedded" if config.get("embed_covers")
                                   else "embedded art removed")
                extra = ""
                if genres_trimmed:
                    extra += f" ({genres_trimmed} extra genre value(s) dropped)"
                if tags_canonicalized:
                    extra += f" ({tags_canonicalized} tag value(s) canonicalized)"
                log(f"  ✓ {rel} → {' + '.join(actions)}{extra}")
            else:
                counts["skip"] += 1
                stats["skipped_count"] += 1
            stats["total_scanned"] += 1
            if genres_trimmed:
                # The canonical sweep's own count: what the per-track genre
                # cap actually removed from the library in this run.
                stats["genres_trimmed"] += genres_trimmed
            if tags_canonicalized:
                # …and how many values the canonical VALUE rule rewrote.
                stats["tags_canonicalized"] += tags_canonicalized
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
