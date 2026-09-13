"""Lyrics formatting, LRC/embedded conversion and MEDIA/SOURCE normalization."""
import os
import re
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

from .audio import AudioFile
from .config import should_write_audio_tag
from .paths import AUDIO_EXTS, DEFAULT_DIGITAL_SOURCE
from .stats import (
    new_stats, _make_pbar, _pbar_skip, _pbar_update, _diff_bytes,
    _walk_files, is_audio_file, _find_albums, _clean_set, _summarize_values,
    _collect_targets, worker_count,
)
from .ui import print_header, log, c, Color, log_file_result
import tempfile

TIMESTAMP_RE = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:\.(\d+))?\]")
WORD_TS_RE = re.compile(r"<(\d{1,2}):(\d{1,2})(?:\.(\d+))?>")


# Remove a SPACE (not a newline) directly after a timestamp. Using \s+ here
# would also consume the newline of a timestamp-only line, gluing
# "[00:00.00]" onto the following line as "[00:00.00][00:45.53]..." which
# ESLyrics on foobar2000 cannot parse.
SPACE_AFTER_TS_RE = re.compile(r"(\[\d{2}:\d{2}\.\d{2,3}\])[ \t]+")
# Enhanced LRC: space after word-level <mm:ss.xx> timestamps.
WORD_SPACE_AFTER_TS_RE = re.compile(r"(<\d{2}:\d{2}\.\d{2,3}>)[ \t]+")



LRC_META_RE = re.compile(
    r"^\s*\[(?:ar|ti|al|by|au|la|offset|length|re|ve):.*\]\s*$",
    re.IGNORECASE,
)


# A line carrying two or more timestamps. ESLyrics on foobar2000 cannot
# parse "[a]text[b]more" on one line (it shows a duplicated line), and the
# old space-after-timestamp bug glued whole lines together exactly like
# this. Split every timestamp boundary onto its own line.
_TS_TOKEN_RE = re.compile(r"\[\d{1,2}:\d{2}(?:\.\d+)?\]")


def _split_merged_ts(line):
    """Split a timestamp-run line into one line per timestamp.

    '[00:00.00][00:45.53]Stretching, filing[00:46.86]Against her skin'
      -> ['[00:00.00]', '[00:45.53]Stretching, filing',
          '[00:46.86]Against her skin']
    Only lines that START with a timestamp are considered.
    """
    if not line.startswith("["):
        return [line]
    matches = list(_TS_TOKEN_RE.finditer(line))
    if len(matches) < 2:
        return [line]
    out = []
    for i, m in enumerate(matches):
        next_start = matches[i + 1].start() if i + 1 < len(matches) else len(line)
        out.append(line[m.start():m.end()] + line[m.end():next_start])
    return out


def _part_stamp(part):
    """The leading timestamp of a _split_merged_ts part, if any."""
    m = _TS_TOKEN_RE.match(part)
    return m.group(0) if m else None


def _stamp_only(part):
    """True when a _split_merged_ts part is a bare timestamp that labels
    no text of its own."""
    m = _TS_TOKEN_RE.match(part)
    return m is not None and not part[m.end():].strip()


def _reformat_ts(m, precision=2):
    """Reformat a [mm:ss.xxx] timestamp to the requested precision,
    carrying correctly: [01:59.999] at precision 2 becomes [02:00.00]."""
    mins = int(m.group(1))
    secs = int(m.group(2))
    ms = m.group(3)

    if ms is None:
        ms_ms = 0
    else:
        try:
            ms_ms = int(ms[:3].ljust(3, "0")[:3])
        except ValueError:
            ms_ms = 0

    total_ms = (mins * 60 + secs) * 1000 + ms_ms
    unit_ms = 10 ** (3 - precision)
    # Round-half-up in integer milliseconds (Decimal quantize is a no-op
    # for positive exponents when built from an int).
    total_ms = ((total_ms + unit_ms // 2) // unit_ms) * unit_ms

    mm, rem = divmod(total_ms, 60000)
    ss, cc = divmod(rem, 1000)
    return f"[{mm:02d}:{ss:02d}.{cc // unit_ms:0{precision}d}]"


def _reformat_word_ts(m, precision=2):
    """Reformat a <mm:ss.xxx> word timestamp (Enhanced LRC)."""
    mins = int(m.group(1))
    secs = int(m.group(2))
    ms = m.group(3)
    if ms is None:
        ms_ms = 0
    else:
        try:
            ms_ms = int(ms[:3].ljust(3, "0")[:3])
        except ValueError:
            ms_ms = 0
    total_ms = (mins * 60 + secs) * 1000 + ms_ms
    unit_ms = 10 ** (3 - precision)
    total_ms = ((total_ms + unit_ms // 2) // unit_ms) * unit_ms
    mm, rem = divmod(total_ms, 60000)
    ss, cc = divmod(rem, 1000)
    return f"<{mm:02d}:{ss:02d}.{cc // unit_ms:0{precision}d}>"


def format_lyrics_text(text, precision=2, strip_metadata=True,
                       collapse_blank_lines=True,
                       lrc_enhanced_enabled=True,
                       lrc_enhanced_word_sync=True,
                       lrc_extended_enabled=True,
                       lrc_add_zero_timestamp=False,
                       lrc_zero_timestamp_blank=False,
                       cfg=None):
    """
    Cleans lyrics:
    - POSIX newlines
    - normalized timestamps ([mm:ss.xx] and Enhanced <mm:ss.xx>)
    - no space directly after timestamps
    - one line per timestamp: stacked timestamps are split up (unless Extended)
    - a [00:00.00] stacked in front of other stamps is dropped
    - timestamp-only lines lend their stamps to the next untimed line
    - no LRC metadata lines (unless Enhanced word-sync lines)
    - no duplicate blank lines

    The result is idempotent: cleaning already-clean lyrics is a no-op.
    lrc_enhanced_* and lrc_extended_enabled gate Enhanced/Extended features;
    cfg dict overrides when supplied.
    """
    if not text:
        return text
    # cfg overrides when supplied (used by grader + _format_for_storage)
    if cfg is not None:
        lrc_enhanced_enabled = cfg.get("lrc_enhanced_enabled", lrc_enhanced_enabled)
        lrc_enhanced_word_sync = cfg.get("lrc_enhanced_word_sync", lrc_enhanced_word_sync)
        lrc_extended_enabled = cfg.get("lrc_extended_enabled", lrc_extended_enabled)
        lrc_add_zero_timestamp = cfg.get("lrc_add_zero_timestamp", lrc_add_zero_timestamp)
        lrc_zero_timestamp_blank = cfg.get("lrc_zero_timestamp_blank", lrc_zero_timestamp_blank)
        # precision/strip/collapse may also be in cfg when called via grader
        try:
            precision = int(cfg.get("lrc_timestamp_precision", precision))
        except Exception:
            pass
        strip_metadata = cfg.get("lrc_strip_metadata", strip_metadata)
        collapse_blank_lines = cfg.get("lrc_collapse_blank_lines", collapse_blank_lines)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    precision = 3 if int(precision) == 3 else 2
    text = TIMESTAMP_RE.sub(
        lambda match: _reformat_ts(match, precision), text
    )
    if lrc_enhanced_enabled and lrc_enhanced_word_sync:
        text = WORD_TS_RE.sub(
            lambda m: _reformat_word_ts(m, precision), text
        )
        text = WORD_SPACE_AFTER_TS_RE.sub(r"\1", text)
    text = SPACE_AFTER_TS_RE.sub(r"\1", text)

    zero_ts = f"[00:00.{'0' * precision}]"
    lines = []
    pending_stamps = []

    for ln in text.split("\n"):
        s = ln.strip()

        if not s:
            lines.append("")
            pending_stamps = []
            continue

        if strip_metadata and LRC_META_RE.match(s):
            # Enhanced lines with word timestamps are not metadata
            if not (lrc_enhanced_enabled and WORD_TS_RE.search(s)):
                continue

        # Split merged "[a][b]text" lines — respect Extended flag
        if lrc_extended_enabled:
            parts = [s]
        else:
            parts = _split_merged_ts(s)

        # A [00:00.00] stacked directly in front of other timestamps is
        # a start-of-file marker that labels no text of its own — unless the
        # compatibility zero-timestamp option is enabled (then it is intentional).
        if not lrc_add_zero_timestamp:
            while (len(parts) > 1 and _stamp_only(parts[0])
                   and _part_stamp(parts[0]) == zero_ts):
                parts = parts[1:]

        # Timestamp-only line: hold the stamps for the next untimed
        # text line (dropped for good at a blank line, at EOF, or when
        # the next line carries timestamps of its own).
        if parts and all(_stamp_only(p) for p in parts):
            pending_stamps.extend(_part_stamp(p) for p in parts)
            continue

        if pending_stamps and not _TS_TOKEN_RE.search(s):
            # Untimed text right after stray stamps: each stamp labels
            # that text as a line of its own.
            for ts in pending_stamps:
                lines.append(f"{ts}{s}")
            pending_stamps = []
            continue

        # Whatever this line is, it is timed: stray stamps die here.
        pending_stamps = []

        # Stamps stacked in front of a text part each label that text
        # as a line of their own; a trailing run labels nothing and is
        # dropped.
        stacked = []
        for part in parts:
            if _stamp_only(part):
                stacked.append(_part_stamp(part))
                continue
            if stacked:
                m = _TS_TOKEN_RE.match(part)
                body = part[m.end():].rstrip() if m else part.rstrip()
                for ts in stacked:
                    lines.append(f"{ts}{body}")
                stacked = []
            lines.append(part.rstrip())

    cleaned = lines
    if collapse_blank_lines:
        cleaned = []
        prev_blank = False
        for line in lines:
            is_blank = line == ""
            if is_blank and prev_blank:
                continue
            cleaned.append(line)
            prev_blank = is_blank

    while cleaned and cleaned[0] == "":
        cleaned.pop(0)
    while cleaned and cleaned[-1] == "":
        cleaned.pop()

    # Compatibility: ensure first lyric line is [00:00.00] when enabled,
    # or remove it when disabled — always as a blank line, and only if the
    # first lyric doesn't already match the desired state. This handles the
    # 1px threshold case correctly: if the image/lyric already fits, leave it.
    if cleaned:
        # Find first non-blank, non-metadata lyric line
        first_idx = None
        for idx, ln in enumerate(cleaned):
            s = ln.strip()
            if not s:
                continue
            if strip_metadata and LRC_META_RE.match(s):
                if lrc_enhanced_enabled and WORD_TS_RE.search(s):
                    pass
                else:
                    continue
            first_idx = idx
            break
        if first_idx is not None:
            first_line = cleaned[first_idx]
            if lrc_add_zero_timestamp:
                if lrc_zero_timestamp_blank:
                    # Blank leader: ensure a bare [00:00.00] line exists.
                    if not first_line.strip().startswith(zero_ts):
                        cleaned.insert(first_idx, zero_ts)
                elif not first_line.strip().startswith(zero_ts):
                    # Tight leader: "[00:00.00]" + the first lyric's text.
                    # A timed first line keeps its own timestamp (the leader
                    # is prepended); an untimed one is replaced by it.
                    m = TIMESTAMP_RE.match(first_line.strip())
                    body = first_line.strip()[m.end():].strip() if m else first_line.strip()
                    leader = f"{zero_ts}{body}" if body else zero_ts
                    if m:
                        cleaned.insert(first_idx, leader)
                    else:
                        cleaned[first_idx] = leader
            elif first_line.strip() == zero_ts:
                # Leader removal only drops a BARE zero line. A tight zero
                # ("[00:00.00]text") is a legitimately timed first line —
                # stripping it would leave untimed text in a synced LRC.
                cleaned.pop(first_idx)

    return "\n".join(cleaned)


def _lrc_for(audio_path):
    return os.path.splitext(audio_path)[0] + ".lrc"


def _canonical_lyrics(text, append_final_newline=False):
    """Canonicalize lyrics for storage.

    - CRLF -> LF.
    - Leading and trailing blank / whitespace-only lines are removed.
    - Every line is right-trimmed (no trailing spaces).
    - The result has NO trailing newline at all - no byte is wasted on a
      trailing newline, and there is never a blank last line.

    Returns "" when the input is empty or only blank lines.
    """
    if not text:
        return ""
    lines = [
        ln.rstrip()
        for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    result = "\n".join(lines)
    if result and append_final_newline:
        result += "\n"
    return result


def _zero_target_allows(cfg, is_for_lrc: bool) -> bool:
    """Whether the zero-timestamp feature should apply for LRC file vs embedded tag.

    cfg lrc_zero_timestamp_target: EMBEDDED, LRC, or BOTH (default BOTH).
    is_for_lrc True = .lrc sidecar, False = embedded tag.
    """
    try:
        target = str(cfg.get("lrc_zero_timestamp_target", "BOTH")).upper()
    except Exception:
        target = "BOTH"
    if target == "LRC":
        return is_for_lrc
    if target == "EMBEDDED":
        return not is_for_lrc
    return True  # BOTH or unknown


def _atomic_write_text(path, text):
    """Atomic write with fsync to avoid corruption on crash/power loss."""
    import os as _os, tempfile as _tf
    tmp = None
    try:
        fd, tmp = _tf.mkstemp(prefix=".lrc_tmp_", suffix=".lrc", dir=_os.path.dirname(path) or ".")
        with _os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            try:
                f.flush()
                _os.fsync(f.fileno())
            except Exception:
                pass
        _os.replace(tmp, path)
        try:
            d_fd = _os.open(_os.path.dirname(path) or ".", _os.O_DIRECTORY)
            try:
                _os.fsync(d_fd)
            finally:
                _os.close(d_fd)
        except Exception:
            pass
        return True
    except Exception:
        try:
            if tmp and _os.path.exists(tmp):
                _os.remove(tmp)
        except Exception:
            pass
        raise


def _format_for_storage(text, cfg, optimize=True, is_for_lrc=False):
    """Format lyrics using the persisted exact-output choices.

    is_for_lrc distinguishes .lrc sidecar vs embedded tag for the
    zero-timestamp target filter (lrc_zero_timestamp_target).
    Zero is always a blank line when enabled.
    """
    source = text
    if optimize:
        # Gate zero timestamp by target
        eff_zero = bool(cfg.get("lrc_add_zero_timestamp", False)) and _zero_target_allows(cfg, is_for_lrc)
        # Build a cfg view with effective zero flag so format_lyrics_text sees the filtered value
        cfg_view = dict(cfg)
        cfg_view["lrc_add_zero_timestamp"] = eff_zero
        source = format_lyrics_text(
            text,
            precision=cfg.get("lrc_timestamp_precision", 2),
            strip_metadata=cfg.get("lrc_strip_metadata", True),
            collapse_blank_lines=cfg.get("lrc_collapse_blank_lines", True),
            lrc_enhanced_enabled=cfg.get("lrc_enhanced_enabled", True),
            lrc_enhanced_word_sync=cfg.get("lrc_enhanced_word_sync", True),
            lrc_extended_enabled=cfg.get("lrc_extended_enabled", True),
            lrc_add_zero_timestamp=eff_zero,
            cfg=cfg_view,
        )
    return _canonical_lyrics(
        source,
        append_final_newline=cfg.get("append_final_newline", False),
    )


def _process_lyrics_for_audio(audio_path, cfg):
    af = AudioFile(audio_path)

    if af.audio is None:
        return ("fail", 0, 0, f"load: {af.error}")

    modified = False
    original_size = os.path.getsize(audio_path)

    lrc_path = _lrc_for(audio_path)
    lrc_exists = os.path.exists(lrc_path)
    lyrics_format = cfg.get("lyrics_format", "EMBEDDED").upper()
    force = cfg.get("force_lyrics", False)

    # set_lyrics / delete_lyrics mutate the in-memory tag even when the
    # save fails; remember to re-open the file from disk before the
    # INSTRUMENTAL decision below reflects anything but persisted state.
    lyrics_touched = False

    # Clean embedded lyrics (no trailing newline / blank lines).
    can_write_lyrics = should_write_audio_tag(cfg, "LYRICS", filepath=audio_path)
    can_write_instr = should_write_audio_tag(cfg, "INSTRUMENTAL", filepath=audio_path)
    if (force or cfg.get("optimize_embedded_lyrics", True)) and can_write_lyrics:
        cur = af.get_lyrics()
        if cur:
            cleaned = _format_for_storage(cur, cfg, optimize=True, is_for_lrc=False)
            if cleaned != cur:
                lyrics_touched = True
                if af.set_lyrics(cleaned):
                    modified = True

    # Clean existing LRC file (no trailing newline / blank lines).
    if lrc_exists and (force or cfg.get("optimize_lrc", True)):
        try:
            with open(lrc_path, "r", encoding="utf-8", errors="replace") as f:
                lrc_content = f.read()

            final = _format_for_storage(
                lrc_content, cfg, optimize=cfg.get("optimize_lrc", True), is_for_lrc=True
            )

            if final != lrc_content:
                _atomic_write_text(lrc_path, final)
                modified = True

        except Exception as e:
            return ("fail", 0, 0, f"lrc clean: {e}")

    # Post-cleaning state used by the conversion step below. The
    # optimize_* flags gate in-place cleaning only; whatever a
    # conversion writes is always canonical, so the graded format
    # check passes afterwards.
    embedded_raw = af.get_lyrics()
    embedded_canonical = (
        _format_for_storage(embedded_raw, cfg, optimize=True, is_for_lrc=False)
        if embedded_raw and str(embedded_raw).strip() else None
    )

    lrc_raw = None
    if lrc_exists:
        try:
            with open(lrc_path, "r", encoding="utf-8", errors="replace") as f:
                lrc_raw = f.read()
        except Exception as e:
            return ("fail", 0, 0, f"lrc read: {e}")

        if not lrc_raw.strip() and lyrics_format == "EMBEDDED":
            # Empty sidecar with lyrics living in the tag: remove the
            # stray file instead of embedding nothing.
            try:
                os.remove(lrc_path)
                lrc_exists = False
                modified = True
            except OSError:
                pass

    lrc_canonical = (
        _format_for_storage(lrc_raw, cfg, optimize=True, is_for_lrc=True)
        if lrc_raw and lrc_raw.strip() else None
    )

    # Conversion between LRC and embedded lyrics.
    # Respect per-filetype LYRICS toggle for any embedded tag writes and
    # lrc_zero_timestamp_target (EMBEDDED/LRC/BOTH) for zero insertion.
    if lyrics_format == "EMBEDDED" and lrc_canonical:
        if can_write_lyrics:
            # Destination is embedded tag — format lrc_raw for embedded target
            dest_for_embedded = _format_for_storage(lrc_raw, cfg, optimize=True, is_for_lrc=False)
            if embedded_canonical != dest_for_embedded:
                # Embed first; only delete the sidecar once the lyrics are
                # safely inside the tag (a failed write must never destroy
                # the only copy).
                lyrics_touched = True
                if not af.set_lyrics(dest_for_embedded):
                    return ("fail", 0, 0, f"embed lyrics: {af.error}")
            try:
                os.remove(lrc_path)
                lrc_exists = False
                modified = True
            except OSError as e:
                return ("fail", 0, 0, f"lrc delete: {e}")
        else:
            # LYRICS disabled for this filetype — leave both as is
            pass

    elif lyrics_format == "LRC" and embedded_canonical:
        # Destination is .lrc sidecar — format embedded_raw for LRC target
        dest_for_lrc = _format_for_storage(embedded_raw, cfg, optimize=True, is_for_lrc=True)
        # .lrc file write is always allowed (sidecar), but embedded delete is gated
        try:
            _atomic_write_text(lrc_path, dest_for_lrc)
        except Exception as e:
            return ("fail", 0, 0, f"lrc write: {e}")
        if can_write_lyrics:
            lyrics_touched = True
            if not af.delete_lyrics():
                return ("fail", 0, 0, f"delete embedded lyrics: {af.error}")
        lrc_exists = True
        modified = True

    elif lyrics_format == "BOTH":
        # Keep lyrics in both places, reconciled to one canonical text
        # (the embedded tag wins a disagreement - players read it).
        # Respect target: format for each destination separately.
        try:
            if lrc_canonical and not embedded_canonical:
                if can_write_lyrics:
                    dest_for_embedded = _format_for_storage(lrc_raw, cfg, optimize=True, is_for_lrc=False)
                    lyrics_touched = True
                    if not af.set_lyrics(dest_for_embedded):
                        return ("fail", 0, 0, f"embed lyrics: {af.error}")
                    modified = True

            elif embedded_canonical and lrc_canonical != embedded_canonical:
                # Write embedded's text formatted for LRC target
                dest_for_lrc = _format_for_storage(embedded_raw, cfg, optimize=True, is_for_lrc=True)
                _atomic_write_text(lrc_path, dest_for_lrc)
                lrc_exists = True
                modified = True

            elif embedded_canonical and not lrc_canonical:
                dest_for_lrc = _format_for_storage(embedded_raw, cfg, optimize=True, is_for_lrc=True)
                _atomic_write_text(lrc_path, dest_for_lrc)
                lrc_exists = True
                modified = True

        except Exception as e:
            return ("fail", 0, 0, f"both sync: {e}")

    notes = []

    # set_lyrics / delete_lyrics mutate the in-memory tag even when the
    # save fails, so re-read from disk before deciding on INSTRUMENTAL.
    if lyrics_touched:
        af = AudioFile(audio_path)
        if af.audio is None:
            return ("fail", 0, 0, f"reload: {af.error}")

    # INSTRUMENTAL=1 with lyrics present is contradictory: flip it to 0.
    inst = af.get_tag("INSTRUMENTAL")
    if (cfg.get("fix_instrumental_from_lyrics", True)
            and can_write_instr
            and inst is not None and str(inst).strip() == "1"):
        embedded_now = bool(af.get_lyrics() and str(af.get_lyrics()).strip())
        lrc_now = os.path.exists(lrc_path)

        if embedded_now or lrc_now:
            if af.set_tag("INSTRUMENTAL", "0"):
                modified = True
                notes.append("instrumental cleared")

    final_size = os.path.getsize(audio_path)
    b_rem, b_add = _diff_bytes(original_size, final_size)

    if modified:
        notes.append("lyrics processed")
        return ("modified", b_rem, b_add, "; ".join(notes))

    return ("unchanged", 0, 0, "no changes")


def _normalize_album_media_source(args):
    # args is (album_dir, default_source) or (album_dir, default_source, config)
    if len(args) == 3:
        album_dir, default_source, cfg = args
    else:
        album_dir, default_source = args
        cfg = None

    try:
        files = sorted(f for f in os.listdir(album_dir) if is_audio_file(f))

        if not files:
            return (album_dir, "skipped", 0, 0, "no audio files")

        entries = []
        media_values = []
        source_values = []

        for fn in files:
            path = os.path.join(album_dir, fn)
            af = AudioFile(path)

            if af.audio is None:
                return (
                    album_dir,
                    "failed",
                    0,
                    0,
                    f"cannot read {fn}: {af.error}",
                )

            media_val = af.get_tag("MEDIA")
            source_val = af.get_tag("SOURCE")

            media_clean = str(media_val).strip() if media_val is not None else ""
            source_clean = str(source_val).strip() if source_val is not None else ""

            if media_clean:
                media_values.append(media_clean)

            if source_clean:
                source_values.append(source_clean)

            entries.append((path, af, source_clean))

        media_summary = _summarize_values(media_values)
        digital = media_summary == "Digital Media"

        modified_files = 0
        bytes_removed = 0
        bytes_added = 0

        if digital:
            # Respect the new fill_empty_source toggle (default False = keep empty)
            if cfg is not None and not cfg.get("fill_empty_source", False):
                # Do not auto-fill empty SOURCE; keep it empty (per user request)
                # Still need to handle the case where SOURCE is present but inconsistent?
                # For now, just don't fill.
                pass
            else:
                clean_sources = _clean_set(source_values)

                if len(clean_sources) == 1:
                    fill_source = next(iter(clean_sources))
                elif clean_sources:
                    fill_source = sorted(clean_sources)[0]
                else:
                    fill_source = default_source or DEFAULT_DIGITAL_SOURCE

                for path, af, source_clean in entries:
                    if not source_clean:
                        # Respect per-filetype MEDIA_SOURCE toggle
                        if cfg is not None and not should_write_audio_tag(cfg, "SOURCE", filepath=path):
                            continue
                        original_size = os.path.getsize(path)

                        if not af.set_tag("SOURCE", fill_source):
                            return (
                                album_dir,
                                "failed",
                                0,
                                0,
                                f"failed writing SOURCE in {os.path.basename(path)}: {af.error}",
                            )

                        final_size = os.path.getsize(path)
                        b_rem, b_add = _diff_bytes(original_size, final_size)

                        modified_files += 1
                        bytes_removed += b_rem
                        bytes_added += b_add

        elif media_summary == "CD" and cfg is not None and cfg.get("strip_source_on_cd", True):
            # CD must never carry SOURCE (per user request, on by default) — strip it
            for path, af, source_clean in entries:
                if source_clean:
                    if not should_write_audio_tag(cfg, "SOURCE", filepath=path):
                        continue
                    original_size = os.path.getsize(path)
                    if not af.delete_tag("SOURCE"):
                        return (
                            album_dir,
                            "failed",
                            0,
                            0,
                            f"failed removing SOURCE in {os.path.basename(path)}: {af.error}",
                        )
                    final_size = os.path.getsize(path)
                    b_rem, b_add = _diff_bytes(original_size, final_size)
                    modified_files += 1
                    bytes_removed += b_rem
                    bytes_added += b_add

        elif media_summary == "CD":
            # CD with strip_source_on_cd off — leave SOURCE as-is (grading will still fail it if present)
            pass

        else:
            for path, af, source_clean in entries:
                if source_clean:
                    if cfg is not None and not should_write_audio_tag(cfg, "SOURCE", filepath=path):
                        continue
                    original_size = os.path.getsize(path)

                    if not af.delete_tag("SOURCE"):
                        return (
                            album_dir,
                            "failed",
                            0,
                            0,
                            f"failed removing SOURCE in {os.path.basename(path)}: {af.error}",
                        )

                    final_size = os.path.getsize(path)
                    b_rem, b_add = _diff_bytes(original_size, final_size)

                    modified_files += 1
                    bytes_removed += b_rem
                    bytes_added += b_add

        if modified_files:
            return (
                album_dir,
                "modified",
                bytes_removed,
                bytes_added,
                f"{modified_files} file(s) normalized",
            )

        return (album_dir, "unchanged", 0, 0, "already correct")

    except Exception as e:
        return (album_dir, "failed", 0, 0, str(e))


def _normalize_media_source_library(config, stats):
    """
    Album-level MEDIA/SOURCE enforcement:
    - Digital Media albums must have SOURCE populated.
    - Non-Digital Media albums must not have SOURCE.
    """
    if not config.get("normalize_media_source", True):
        return stats

    folder = config["music_folder"]
    default_source = config.get("digital_media_source_value", DEFAULT_DIGITAL_SOURCE)

    if not os.path.isdir(folder):
        return stats

    albums = _find_albums(folder)

    if config.get("targets") is not None:
        target_files = _collect_targets(config["targets"], AUDIO_EXTS)
        albums = sorted({os.path.dirname(f) for f in target_files})

    if not albums:
        return stats

    counts = {"ok": 0, "skip": 0, "fail": 0}
    workers = worker_count(config, default=16, maximum=16, items=len(albums))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_normalize_album_media_source, (a, default_source, config)): a
            for a in albums
        }

        pbar = _make_pbar(len(futures), "MEDIA/SOURCE", unit="album")

        for fut in as_completed(futures):
            album = futures[fut]

            try:
                path, status, b_rem, b_add, info = fut.result()
            except Exception as e:
                stats["total_scanned"] += 1
                stats["error_count"] += 1
                stats["errors"].append((album, str(e)))
                _pbar_update(pbar, counts, kind="fail")
                continue

            if status in ("unchanged", "skipped"):
                stats["skipped_count"] += 1
                log_file_result(album, "skip", info=info or "unchanged")
                _pbar_skip(pbar, counts)
                continue

            stats["total_scanned"] += 1

            if status == "modified":
                stats["modified_count"] += 1
                stats["total_bytes_removed"] += b_rem
                stats["total_bytes_added"] += b_add
                log_file_result(album, "ok", b_rem, b_add)
                _pbar_update(pbar, counts, kind="ok")
            else:
                stats["error_count"] += 1
                stats["errors"].append((path, info))
                log_file_result(album, "fail", info=info)
                _pbar_update(pbar, counts, kind="fail")

        if pbar:
            pbar.close()

    return stats


def run_format_lyrics(config):
    folder = config["music_folder"]
    stats = new_stats()

    print_header("Lyrics Formatter + MEDIA/SOURCE Normalizer")
    log(f"folder: {folder}")
    log(
        f"LRC={config.get('optimize_lrc', True)} · "
        f"embedded={config.get('optimize_embedded_lyrics', True)} · "
        f"format={config.get('lyrics_format', 'EMBEDDED').upper()} · "
        f"media/source={config.get('normalize_media_source', True)} · "
        f"src fallback={config.get('digital_media_source_value', DEFAULT_DIGITAL_SOURCE)} · "
        f"INST auto-fix=on"
    )

    if not os.path.isdir(folder):
        log(c(f"ERROR: folder does not exist: {folder}", Color.RED))
        return stats

    targets = config.get("targets")
    files = _collect_targets(targets, AUDIO_EXTS)
    if targets is None:
        files = sorted(_walk_files(folder, AUDIO_EXTS))
    # Deduplicate in case targets contained both album and its tracks (e.g. Select All)
    # _collect_targets already uses a set, but be extra safe for case-insensitive FS
    if len(files) != len(set(os.path.normcase(p) for p in files)):
        seen = {}
        for p in files:
            seen[os.path.normcase(p)] = p
        files = sorted(seen.values())

    if files:
        threads = worker_count(
            config, default=(os.cpu_count() or 1) * 3,
            maximum=64, items=len(files)
        )
        counts = {"ok": 0, "skip": 0, "fail": 0}

        with ThreadPoolExecutor(max_workers=threads) as ex:
            futures = {ex.submit(_process_lyrics_for_audio, p, config): p for p in files}
            pbar = _make_pbar(len(futures), "Lyrics")

            for fut in as_completed(futures):
                p = futures[fut]

                try:
                    status, b_rem, b_add, info = fut.result()
                except Exception as e:
                    stats["total_scanned"] += 1
                    stats["error_count"] += 1
                    stats["errors"].append((p, str(e)))
                    _pbar_update(pbar, counts, kind="fail")
                    continue

                if status == "unchanged":
                    stats["skipped_count"] += 1
                    log_file_result(p, "skip", info="unchanged")
                    _pbar_skip(pbar, counts)
                    continue

                stats["total_scanned"] += 1

                if status == "modified":
                    stats["modified_count"] += 1
                    stats["total_bytes_removed"] += b_rem
                    stats["total_bytes_added"] += b_add
                    log_file_result(p, "ok", b_rem, b_add)
                    _pbar_update(pbar, counts, kind="ok")
                else:
                    stats["error_count"] += 1
                    stats["errors"].append((p, info))
                    log_file_result(p, "fail", info=info)
                    _pbar_update(pbar, counts, kind="fail")

            if pbar:
                pbar.close()
    else:
        log("No audio files found for lyrics processing.")

    # Album-level MEDIA/SOURCE enforcement.
    _normalize_media_source_library(config, stats)

    return stats


# ---------------------------------------------------------------------------- #
# Enhanced LRC (ELRC) word-level sync. Shared by the player-bar AI sync
# (server/ai.py) and the transliterate/translate script (mlo/lyrics_xlit.py)
# so every lyric variant — original, romanized, translated — carries the
# same kind of word-level timings.
# ---------------------------------------------------------------------------- #

# CJK ranges: hiragana, katakana, CJK punctuation, ideographs, compat
# ideographs, hangul. Japanese/Chinese/Korean text has no spaces, so
# word-level sync sweeps the line per character instead.
_CJK_CHAR_RE = re.compile(
    r"[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)


def _elrc_split_words(body):
    """Tokenize one line for word-level timing. Space-delimited words for
    Latin text; CJK runs sweep per character (latin/digit runs inside a
    CJK piece stay glued together, e.g. "カラオケKEIKO" -> カ ラ オ ケ KEIKO)."""
    words = []
    for piece in body.split():
        if not _CJK_CHAR_RE.search(piece):
            words.append(piece)
            continue
        buf = ""
        for ch in piece:
            if _CJK_CHAR_RE.match(ch):
                if buf:
                    words.append(buf)
                    buf = ""
                words.append(ch)
            else:
                buf += ch
        if buf:
            words.append(buf)
    return words or [body]


def _syllabify_token(word):
    """Split one word token into syllables (rule-based).

    CJK text: every character is its own syllable (one kana = one mora).
    Latin text: maximal vowel runs are syllable nuclei; the consonant
    cluster between two nuclei splits before a single consonant (to-xic),
    between a pair (af-ter), before a digraph (ma-chine) and after the
    first consonant of a longer cluster (mon-ster). "y" is a vowel except
    word-initially, and a mid-run y starts a new nucleus (be-yond, ka-yak)
    unless it ends the run (boy, play). Punctuation never splits. Returns
    [word] unchanged when no split is found — an imperfect fallback, the
    AI audio alignment is what produces real syllable timings.
    """
    if not word:
        return [word]
    if _CJK_CHAR_RE.search(word):
        return list(word)
    m = re.match(r"^(\W*)(.*?)(\W*)$", word, re.UNICODE)
    lead, core, trail = m.group(1), m.group(2), m.group(3)
    if len(core) <= 3:
        return [word]
    low = core.lower()
    vowels = set("aeiouyàáâãäåæèéêëìíîïòóôõöøùúûüýÿ")
    # vowel runs: index/one-past-end pairs into `core`
    runs = []
    start = None
    for i, ch in enumerate(low):
        is_v = ch in vowels and not (ch == "y" and i == 0)
        if is_v and start is None:
            start = i
        elif not is_v and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(low)))
    # a mid-run y starts (prev char is a vowel: be-yond, ka-yak) or closes
    # (prev char is a consonant: try-ing) its own nucleus
    split_runs = []
    for a, b in runs:
        a0 = a
        if low[a0] == "y" and b - a0 > 1 and low[a0 + 1] in vowels:
            split_runs.append((a0, a0 + 1))
            a0 += 1
        i = a0
        prev_vowel = False
        while i < b:
            if low[i] == "y" and i > a0 and i < b - 1:
                if prev_vowel:
                    split_runs.append((a0, i))
                    a0 = i
                else:
                    split_runs.append((a0, i + 1))
                    a0 = i + 1
            prev_vowel = low[i] in vowels
            i += 1
        split_runs.append((a0, b))
    runs = split_runs
    if len(runs) <= 1:
        return [word]
    digraphs = ("ch", "sh", "th", "ph", "wh", "ck", "ng", "gh", "qu")
    cuts = []
    for (a1, _b1), (_a2, _b2) in zip(runs, runs[1:]):
        # cluster between the end of this run and the start of the next
        c_start = _b1
        c_end = _a2
        n = c_end - c_start
        if n <= 0:
            cut = c_start
        elif n == 1:
            cut = c_start  # open syllable: to-xic
        elif n == 2:
            frag = low[c_start:c_end]
            cut = c_start if frag in digraphs else c_start + 1  # ma-chine / af-ter
        else:
            cut = c_start + 1  # keep the first consonant with the left nucleus
        cuts.append(cut)
    syls = []
    prev = 0
    for cut in cuts:
        syls.append(core[prev:cut])
        prev = cut
    syls.append(core[prev:])
    parts = ([lead] if lead else []) + syls + ([trail] if trail else [])
    # glue punctuation onto their neighbours so it never stands alone
    merged = [parts[0]]
    for piece in parts[1:]:
        if not re.search(r"\w", piece) and len(piece) <= 2 and len(merged) > 1:
            merged[-1] += piece
        else:
            merged.append(piece)
    return merged if any(p.strip() for p in merged) else [word]


# Syllable-level evidence in ELRC: two word tags glued together with no
# whitespace between them ("<00:10.69>try<00:11.10>ing") — word-level
# canonical form always separates word tags with a single space.


def _body_has_glued_tags(body):
    """True when the line carries syllable-level (glued) word tags."""
    ms = list(WORD_TS_RE.finditer(body))
    for a, b in zip(ms, ms[1:]):
        seg = body[a.end():b.start()]
        if seg == "" or (seg == seg.strip() and bool(seg)):
            return True
    return False


def _cjk_tag_coverage(body):
    """Fraction of CJK characters that sit directly behind a word tag.
    For CJK, one tag per character IS syllable (mora) level — kana carry
    exactly one mora each — so full per-character coverage satisfies the
    syllable requirement even though the tags are space-separated."""
    tag_ends = {m.end() for m in WORD_TS_RE.finditer(body)}
    total = covered = 0
    for i, ch in enumerate(body):
        if _CJK_CHAR_RE.match(ch):
            total += 1
            if i in tag_ends:
                covered += 1
    return covered / total if total else 0.0


def sync_level_of(text):
    """Sync granularity carried by a lyrics text: 'plain', 'line', 'word'
    or 'syllable'. Glued word tags anywhere mean syllable level; CJK text
    whose characters are all tagged counts as syllable too (one kana = one
    mora), everything else word-tagged is 'word'."""
    raw = str(text or "")
    if not raw.strip():
        return "plain"
    has_word = WORD_TS_RE.search(raw)
    if not has_word:
        return "line" if TIMESTAMP_RE.search(raw) else "plain"
    if _body_has_glued_tags(raw):
        return "syllable"
    # CJK: full per-character tag coverage on the CJK-bearing lines
    cjk_total = cjk_covered = 0
    for line in raw.splitlines():
        if not WORD_TS_RE.search(line):
            continue
        bare = WORD_TS_RE.sub("", line)
        for i, ch in enumerate(bare):
            if _CJK_CHAR_RE.match(ch):
                cjk_total += 1
                if i in {m.end() for m in WORD_TS_RE.finditer(line)}:
                    cjk_covered += 1
    if cjk_total and cjk_covered / cjk_total >= 0.9:
        return "syllable"
    return "word"


def text_meets_sync_level(text, level):
    """True when `text` carries at least the required sync granularity
    (lrc_sync_level: SYLLABLE / WORD / LINE)."""
    level = str(level or "SYLLABLE").upper()
    lv = sync_level_of(text)
    if level == "WORD":
        return lv in ("word", "syllable")
    if level == "SYLLABLE":
        return lv == "syllable"
    return lv in ("line", "word", "syllable")


def elrc_word_sync(lrc_text, max_line_spread_s=6.0, min_word_span_s=0.18,
                   level="word"):
    """Turn line-synced LRC into word- or syllable-synced ELRC.

    Each line's time slot runs from its own timestamp to the next line's
    timestamp (capped at max_line_spread_s). Word start times are spread
    across the slot proportionally to word length; with level="syllable"
    each word's span is further divided across its syllables and the
    syllable tags are glued together inside the word (the canonical
    syllable-ELRC form). Word-tagged input is upgraded to syllable level;
    already-syllable lines pass through untouched. Empty/instrumental
    lines pass through unchanged. CJK text sweeps per character (a kana
    character is one syllable, so CJK output is the same at both levels).
    """
    line_re = re.compile(r"^(?:\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\])+(.*)$")
    all_times_re = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
    word_tag_re = re.compile(r"<(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?>")
    syllables = str(level or "word").lower() == "syllable"

    def ts_to_s(mm, ss, frac="0"):
        return int(mm) * 60 + int(ss) + int((frac or "0").ljust(2, "0")[:2]) / 100.0

    def fmt_ts(t):
        mm = int(t // 60)
        ss = int(t % 60)
        frac = round((t - math.floor(t)) * 100)
        if frac >= 100:
            frac = 99
        return f"[{mm:02d}:{ss:02d}.{frac:02d}]"

    def word_span_syllables(start, end, word_text):
        """Glued <t>syl<t>syl pieces covering [start, end)."""
        syls = _syllabify_token(word_text)
        weights = [max(len(s), 1) for s in syls]
        total = sum(weights)
        span = max(end - start, 0.05)
        cursor = start
        pieces = []
        for s, wt in zip(syls, weights):
            share = span * (wt / total)
            pieces.append(f"<{fmt_ts(cursor)[1:-1]}>{s}")
            cursor += share
        return "".join(pieces)

    rows = []
    for raw in (lrc_text or "").splitlines():
        stamps = all_times_re.findall(raw)
        body = all_times_re.sub("", raw).strip()
        if not stamps:
            rows.append((None, raw.strip()))
            continue
        t = ts_to_s(stamps[-1][0], stamps[-1][1], stamps[-1][2])
        rows.append((t, body))

    out = []
    n = len(rows)
    for i, (t, body) in enumerate(rows):
        if t is None:
            out.append(body)
            continue
        if not body:
            # empty (instrumental) line: canonical form, no trailing space
            out.append(f"{fmt_ts(t)}".rstrip())
            continue
        if word_tag_re.search(body):
            if not syllables or _body_has_glued_tags(body):
                # already word-synced (or already syllable-synced):
                # keep as-is
                out.append(f"{fmt_ts(t)}{body}".rstrip())
                continue
            # word-level input + syllable target: upgrade in place. Merge
            # the tagged pieces back into words (a piece NOT ending in
            # whitespace continues the previous word — glued syllables),
            # then re-split each word's own time span into glued syllables.
            ms = list(word_tag_re.finditer(body))
            words = []  # [start_of_first_tag, text]
            for m, nxt in zip(ms, ms[1:] + [None]):
                seg = body[m.end():nxt.start() if nxt else len(body)]
                prev_open = words and words[-1][1] and not words[-1][1][-1].isspace()
                if prev_open:
                    words[-1][1] += seg
                else:
                    words.append([ts_to_s(m.group(1), m.group(2), m.group(3)), seg])
            rebuilt = []
            for j, (w_start, seg) in enumerate(words):
                stripped = seg.rstrip()
                if not stripped:
                    continue
                if j + 1 < len(words):
                    w_end = words[j + 1][0]
                else:
                    w_end = w_start + max(0.4, 0.16 * len(stripped))
                rebuilt.append((w_start, w_end, stripped))
            pieces = [word_span_syllables(a, b, w) for a, b, w in rebuilt]
            out.append(fmt_ts(t) + " ".join(pieces))
            continue
        # resolve the line's end: next timed line, capped spread
        end = None
        for j in range(i + 1, n):
            if rows[j][0] is not None and rows[j][0] > t:
                end = min(rows[j][0], t + max_line_spread_s)
                break
        if end is None:
            end = t + min(max_line_spread_s, max(1.5, 0.32 * len(body.split())))
        words = _elrc_split_words(body)
        weights = [max(len(w), 1) for w in words]
        total = sum(weights)
        span = max(end - t, min_word_span_s * len(words))
        cursor = t
        pieces = []
        for w, wt in zip(words, weights):
            share = span * (wt / total)
            if syllables:
                pieces.append(word_span_syllables(cursor, cursor + share, w))
            else:
                pieces.append(f"<{fmt_ts(cursor)[1:-1]}>{w}")
            cursor += share
        # canonical spacing: no space after the line stamp; single spaces
        # between word tags (the trailing word keeps its punctuation);
        # syllable tags inside a word are glued together with no space
        out.append(fmt_ts(t) + " ".join(pieces))
    return "\n".join(out)

