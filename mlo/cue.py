"""CUE sheet formatter."""
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from .paths import fsync_dir
from .stats import (
    new_stats, _make_pbar, _pbar_skip, _pbar_update, _walk_files, _diff_bytes,
    _collect_targets, worker_count,
)
from .subproc import run_tool
from .ui import print_header, log

# INDEX lines with their NUMBER kept. mlo.discs' own CUE_INDEX_RE reads only
# INDEX 01 — the app's canonical "where a track starts", which is all a
# tracklist needs. A SPLIT needs INDEX 00 as well, because the two index
# points are what decide which side of a pregap the gap audio lands on, so the
# number is captured here rather than widening that module's regex (every
# other caller in the app would inherit a second group it does not want).
_CUE_INDEX_RE = re.compile(
    r"^\s*INDEX\s+(\d{1,2})\s+(\d{1,3}):(\d{2}):(\d{2})",
    re.MULTILINE | re.IGNORECASE,
)

def canonical_cue_text(content, keep_empty_lines, keep_other_lines,
                       file_type, append_final_newline):
    """Return the canonical (normalized) form of a cue sheet's text.

    Pure function with no side effects: LF line endings, DISCID hex
    normalization, quoted FILE lines with the configured type, TRACK/INDEX
    normalization, structural directives preserved, REM comments stripped
    (unless keep_other_lines), blank-line collapsing, and no trailing blank
    lines. ``append_final_newline`` optionally adds a single trailing LF.
    """
    file_type = str(file_type).upper()
    if file_type not in ("WAVE", "MP3"):
        file_type = "WAVE"

    original = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = original.split("\n")

    discid_line = None
    formatted = []

    for line in lines:
        stripped = line.strip()

        if not stripped:
            if keep_empty_lines:
                formatted.append("")
            continue

        upper = stripped.upper()

        if upper.startswith("REM DISCID"):
            if discid_line is None:
                parts = stripped.split(None, 2)
                if len(parts) >= 3:
                    raw = parts[2].strip()
                    hex_code = re.sub(r"[^0-9A-Fa-f]", "", raw).upper()[:8]
                    discid_line = (
                        f"REM DISCID {hex_code}"
                        if hex_code
                        else f"REM DISCID {raw.upper()}"
                    )
                else:
                    discid_line = "REM DISCID"

        elif upper == "FILE" or upper.startswith("FILE "):
            m = re.search(r'"([^"]*)"', stripped)
            if m:
                name = m.group(1)
            else:
                parts = stripped.split(None, 2)
                if len(parts) >= 2:
                    name = parts[1].strip().strip("'\"")
                else:
                    formatted.append(stripped)
                    continue
            formatted.append(f'FILE "{name}" {file_type}')

        elif upper.startswith("TRACK"):
            parts = stripped.split(None, 2)
            if len(parts) >= 3:
                formatted.append(f"  TRACK {parts[1].zfill(2)} {parts[2].upper()}")
            else:
                formatted.append(f"  {stripped}")

        elif upper.startswith("INDEX"):
            parts = stripped.split(None, 2)
            if len(parts) >= 3:
                tp = parts[2].split(":")
                if len(tp) == 3:
                    try:
                        formatted.append(
                            f"    INDEX {parts[1].zfill(2)} "
                            f"{int(tp[0]):02d}:{int(tp[1]):02d}:{int(tp[2]):02d}"
                        )
                    except ValueError:
                        formatted.append(f"    {stripped}")
                else:
                    formatted.append(f"    {stripped}")
            else:
                formatted.append(f"    {stripped}")

        else:
            # When keep_other_lines is off (default per request), only
            # REM DISCID, FILE, TRACK and INDEX are kept. All other
            # structural directives (PERFORMER, TITLE, CATALOG, ISRC,
            # SONGWRITER, PREGAP, POSTGAP, FLAGS, REM other, etc.) are
            # dropped to produce a minimal canonical sheet.
            if keep_other_lines:
                formatted.append(stripped)
            # else: drop the line (only the 4 types above are kept)

    if discid_line:
        formatted.insert(0, discid_line)

    new_content = "\n".join(formatted)

    new_content = re.sub(r"\n{3,}", "\n\n", new_content)

    # No trailing blank / whitespace-only lines AND no trailing newline
    # byte at all (empty cue -> empty string).
    new_content = new_content.rstrip()
    if new_content and append_final_newline:
        new_content += "\n"

    return new_content


def _process_cue_file(args):
    (filename, keep_empty_lines, keep_other_lines, file_type,
     append_final_newline, force) = args
    tmp_path = None

    try:
        original_size = os.path.getsize(filename)

        # Safety: read full file for NUL check, not just 4k prefix (binary audio after 4k would be mis-detected)
        with open(filename, "rb") as raw:
            data = raw.read()
        if b"\x00" in data:
            return (filename, False, "binary file skipped (not a cue)", 0, 0)

        # Decoded from the bytes just read: the text-mode open that used to
        # follow read the same bytes off the disk a second time for every cue
        # sheet. Decoding the buffer is the same text (a BOM is stripped by
        # utf-8-sig either way) — and it no longer lets universal-newline
        # translation hide a CRLF sheet from the comparison below, which is
        # what the "unchanged" check is there to catch.
        try:
            original_content = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            # Non-UTF-8 sheets are left byte-for-byte untouched: the canonical
            # text below is written back as UTF-8, so the old latin-1 fallback
            # decoded a CP1252/Shift-JIS sheet to mojibake and persisted that
            # mojibake, destroying the original encoding (latin-1 decodes ANY
            # byte string, so it always "succeeded").
            return (filename, False, "non-UTF-8 cue left untouched", 0, 0)

        # Keep the raw text for the "unchanged" comparison so CRLF-only
        # files still get normalized to LF.
        raw_content = original_content

        new_content = canonical_cue_text(
            original_content, keep_empty_lines, keep_other_lines,
            file_type, append_final_newline,
        )

        if new_content == raw_content and not force:
            return (filename, False, None, 0, 0)

        fd, tmp_path = tempfile.mkstemp(
            prefix=".cue_tmp_",
            suffix=".cue",
            dir=os.path.dirname(filename),
        )

        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_content)
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass

        os.replace(tmp_path, filename)
        fsync_dir(os.path.dirname(filename))
        tmp_path = None

        final_size = os.path.getsize(filename)
        b_rem, b_add = _diff_bytes(original_size, final_size)

        return (filename, True, None, b_rem, b_add)

    except Exception as e:
        return (filename, False, f"{type(e).__name__}: {e}", 0, 0)

    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def run_format_cues(config):
    target = config["music_folder"]
    keep_empty = config.get("keep_empty_cue_lines", False)
    keep_other = config.get("keep_other_cue_lines", False)
    file_type = config.get("cue_file_type", "WAVE").upper()
    if file_type not in ("WAVE", "MP3"):
        file_type = "WAVE"
    append_final_newline = config.get("append_final_newline", False)
    force = config.get("force_cue", False)
    stats = new_stats()

    print_header("CUE Formatter")
    log(
        f"keep empty={'on' if keep_empty else 'off'} · "
        f"keep other={'on' if keep_other else 'off'} · "
        f"force={'on' if force else 'off'}"
    )
    log(f"target: {target}")

    # First pass: deterministically rename multi-CD cues to CD-N.cue
    # (based on their FILE entries), then fix stale FILE names, then re-collect.
    # Both steps are config-gated and make no assumptions.
    targets = config.get("targets")
    cues = _collect_targets(targets, (".cue",))
    if targets is None:
        cues = sorted(_walk_files(target, (".cue",)))

    from .discs import (
        album_discs, rename_cues_for_discs, rename_logs_for_discs,
        fix_cue_filenames,
    )

    def _repair_album(album_dir):
        """Rename + repair ONE album folder's sidecars -> (renamed, lines).

        The lines come back in the order this album's own pass produced them;
        the caller logs whole albums in album order, so the stream is the same
        one the old album-at-a-time loop printed (nothing else writes between
        these lines: the formatting pass and its progress bar come after).

        The album's disc mapping is derived ONCE and handed to both renaming
        helpers: each of them used to call `album_discs` for itself — a
        listing plus a `disc_of_filename` per audio file — so a library-wide
        run paid for the same mapping three times per album.
        """
        lines = []
        renamed = False
        discs = album_discs(album_dir)
        for old, new in rename_cues_for_discs(album_dir, discs, config=config):
            renamed = True
            lines.append(f"cue renamed: {old} -> {new}")
        for old, new in rename_logs_for_discs(album_dir, discs, config=config):
            renamed = True
            lines.append(f"log renamed: {old} -> {new}")
        for note in fix_cue_filenames(album_dir, config=config):
            lines.append(note)
            renamed = renamed or "->" in note
        return renamed, lines

    # One album folder is independent of every other (the rename/repair
    # helpers only ever touch the folder they are handed), so the pass that
    # used to walk the library folder by folder on the runner thread runs in
    # lanes now — the same pool the canonicalisation pass below already uses.
    album_dirs = sorted({os.path.dirname(c) for c in cues})
    renamed_any = False
    if album_dirs:
        lanes = worker_count(config, maximum=16, items=len(album_dirs))
        with ThreadPoolExecutor(max_workers=lanes) as ex:
            # ex.map yields in album order, so the log lines below land in the
            # order the serial pass emitted them (same lines, same order).
            for renamed, lines in ex.map(_repair_album, album_dirs):
                for line in lines:
                    log(line)
                renamed_any = renamed_any or renamed
    if renamed_any:
        # Re-collect by walking each original album folder: explicit
        # targets may have pointed at a now-renamed .cue file. Capture the
        # folder list BEFORE clearing cues — it is derived from them.
        album_dirs = sorted({os.path.dirname(c) for c in cues})
        cues = []
        for album_dir in album_dirs:
            if os.path.isdir(album_dir):
                cues.extend(
                    os.path.join(album_dir, f)
                    for f in sorted(os.listdir(album_dir))
                    if f.lower().endswith(".cue")
                )
        cues = sorted(set(cues))

    if not cues:
        log("No .cue files found.")
        return stats

    threads = worker_count(config, maximum=64, items=len(cues))
    counts = {"ok": 0, "skip": 0, "fail": 0}

    with ThreadPoolExecutor(max_workers=threads) as ex:
        futures = {
            ex.submit(
                _process_cue_file,
                (f, keep_empty, keep_other, file_type, append_final_newline,
                 force),
            ): f
            for f in cues
        }

        pbar = _make_pbar(len(futures), "CUEs")

        for fut in as_completed(futures):
            fn, ok, err, br, ba = fut.result()

            if err:
                stats["total_scanned"] += 1
                stats["error_count"] += 1
                stats["errors"].append((fn, err))
                _pbar_update(pbar, counts, kind="fail")
                continue

            if not ok:
                stats["skipped_count"] += 1
                _pbar_skip(pbar, counts)
                continue

            stats["total_scanned"] += 1
            stats["modified_count"] += 1
            stats["total_bytes_removed"] += br
            stats["total_bytes_added"] += ba
            _pbar_update(pbar, counts, kind="ok")

        if pbar:
            pbar.close()

    return stats


# --------------------------------------------------------------------------- #
# Image rips — one .flac for a whole disc, split into one file per track
# --------------------------------------------------------------------------- #

def _index_seconds(match):
    """A cue INDEX position (mm:ss:ff, 75 frames to the second) in seconds."""
    return int(match.group(2)) * 60 + int(match.group(3)) + int(match.group(4)) / 75.0


def _cue_sheet(text):
    """A cue sheet's FILE references and TRACK rows, in sheet order.

    -> (files, tracks), where a track is
    {position, title, file, start, pregap}: `file` is the FILE reference the
    track sits in, and `start`/`pregap` are its INDEX 01/00 in seconds (None
    when the sheet states none). Read with the same per-line regexes
    `mlo.discs.cue_track_rows` uses, so the two cannot disagree about what a
    sheet says — only the INDEX number is captured (see _CUE_INDEX_RE).
    """
    from .discs import CUE_FILE_RE, CUE_TRACK_LINE_RE, CUE_TITLE_LINE_RE
    files, tracks = [], []
    for line in text.splitlines():
        m = CUE_FILE_RE.match(line)
        if m:
            files.append(m.group(1))
            continue
        m = CUE_TRACK_LINE_RE.match(line)
        if m:
            tracks.append({"position": int(m.group(1)), "title": "",
                           "file": files[-1] if files else "",
                           "start": None, "pregap": None})
            continue
        m = _CUE_INDEX_RE.match(line)
        if m and tracks:
            num = int(m.group(1))
            key = "start" if num == 1 else ("pregap" if num == 0 else None)
            if key and tracks[-1][key] is None:
                tracks[-1][key] = _index_seconds(m)
            continue
        m = CUE_TITLE_LINE_RE.match(line)
        if m and tracks and not tracks[-1]["title"]:
            # A TITLE after a TRACK line is that track's; one before any TRACK
            # line is the album's, which belongs to no row (see cue_track_rows).
            tracks[-1]["title"] = m.group(1).strip()
    return files, tracks


def _image_for(folder, audio, tracks):
    """The one audio file a sheet's tracks all sit in — the image rip.

    None when the sheet names more than one FILE (that is a per-track rip
    already, nothing to split), names none, or names something that matches no
    file while the folder holds more than one candidate to spend.
    """
    from .naming import cue_ref_names, name_key
    refs = {str(t.get("file") or "") for t in tracks}
    refs.discard("")
    if len(refs) != 1:
        return None
    wanted = set(cue_ref_names(next(iter(refs))))
    for name in audio:
        if name_key(name) in wanted:
            return os.path.join(folder, name)
    # A sheet whose FILE name no longer matches anything — the image was
    # renamed after the rip — is usable only when there is exactly one audio
    # file it could mean.
    if len(audio) == 1:
        return os.path.join(folder, audio[0])
    return None


def _track_name(track, stem, prefix=""):
    """The file name one split track is written as.

    `stem` (the image's own name) is the fallback when the sheet states no
    TITLE; `prefix` is the image stem with a "-" when the folder holds more
    than one image, which is what tells two discs' "01 …" apart — and, being
    a "CD1-"/"disc1-" shape, it is also what the naming script's own disc
    reader picks the disc number out of.
    """
    from .naming import sanitize_segment
    title = sanitize_segment(str(track.get("title") or "")).strip(" ._")
    head = f"{prefix}{int(track['position']):02d}"
    name = f"{head} {title}.flac" if title else f"{head} {stem}_{int(track['position']):02d}.flac"
    # The whole name, not just its parts: `head` and the extension are added
    # AROUND the title, so a paragraph-long TITLE has to give way inside the
    # name the filesystem is asked to create — nine characters of numbering and
    # the ".flac" stay, the title's tail is what is cut (the shared rule in
    # mlo.naming, which every other writer names files with).
    return sanitize_segment(name)


def _split_one(args):
    """Write ONE track of an image rip -> (path, error); path "" on failure."""
    ffmpeg, image, dest, start, dur, tags = args
    cmd = [
        ffmpeg, "-y", "-v", "error", "-nostdin",
        # -ss BEFORE -i so ffmpeg seeks instead of decoding the disc from the
        # top once per track. Input seeking is sample-accurate (accurate_seek
        # is the default), which is also why this is a decode/re-encode and
        # never a "-c:a copy": a stream copy cuts at a frame boundary and can
        # shift a track by up to one frame (~93 ms at 44.1 kHz). FLAC -> FLAC
        # is lossless either way, so nothing is lost but a little CPU.
        "-ss", f"{start:.3f}", "-i", image,
        "-t", f"{dur:.3f}", "-map", "0:a:0", "-c:a", "flac",
    ]
    for key, value in tags.items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd.append(dest)
    try:
        run_tool(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                 text=True, encoding="utf-8", errors="replace", timeout=1800)
    except Exception as e:  # noqa: BLE001 — any failure is "this track failed"
        return "", f"{os.path.basename(dest)}: {e}"
    if not os.path.isfile(dest) or not os.path.getsize(dest):
        return "", f"{os.path.basename(dest)}: ffmpeg wrote no file"
    return dest, ""


def split_image_rip(folder, config=None, log_fn=None):
    """Split every whole-disc image rip in *folder* into one FLAC per track.

    An IMAGE RIP is one audio file for a whole disc plus the .cue describing
    it: a single FILE reference and one TRACK per song. Most CD rips arrive
    that way, and until now the app kept the image whole, so every later step
    (the naming script, per-track tags, the grader's per-track checks and
    AccurateRip) saw one file where it expected an album.

    Boundaries are the sheet's own INDEX 01 points, the convention every CD
    splitter uses: track N runs from its INDEX 01 to track N+1's, so a pregap
    belongs to the track in front of it. Track 1 always starts at 0, so a
    hidden track before its INDEX 01 stays with track 1. A sheet stating no
    INDEX 01s, or ones that do not increase, is left alone — there is nothing
    to cut it up with.

    All or nothing, per image: if any track fails to write, the tracks already
    written are removed and the image stays, because a half-split album is
    worse than an intact one. On success the image itself goes to the app's
    Trash (never a bare delete) and the .cue is left exactly as it is — the
    app already reads an image-style sheet whose FILE no longer exists
    (`mlo.discs.fix_cue_filenames` patches that reference, and
    `_cue_matches_disc` is pregap-tolerant by design).

    Returns the paths written; [] when the folder holds no image rip, or none
    could be split (no INDEX 01s, no ffmpeg, unreadable durations).
    """
    from .audio import AudioFile
    from .discs import read_cue_text
    from .paths import LIB_AUDIO_EXTS, trash_file
    from .tools import detect_all_tools

    def say(line):
        if log_fn is not None:
            try:
                log_fn(line)
            except Exception:
                pass

    if not folder or not os.path.isdir(folder):
        return []
    try:
        entries = sorted(os.listdir(folder))
    except OSError:
        return []
    cues = [e for e in entries if e.lower().endswith(".cue")]
    audio = [e for e in entries
             if os.path.splitext(e)[1].lower() in LIB_AUDIO_EXTS
             and os.path.isfile(os.path.join(folder, e))]
    if not cues or not audio:
        return []
    ffmpeg = str((detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe") or "")
    if not ffmpeg or not os.path.isfile(ffmpeg):
        return []

    albums = []   # (image path, tracks, starts)
    claimed = set()
    for cue in cues:
        _files, tracks = _cue_sheet(read_cue_text(os.path.join(folder, cue)))
        if len(tracks) < 2:
            continue
        starts = [t["start"] for t in tracks]
        if any(s is None for s in starts):
            continue
        if any(b <= a for a, b in zip(starts, starts[1:])):
            continue
        image = _image_for(folder, audio, tracks)
        if not image or image in claimed:
            continue
        claimed.add(image)
        albums.append((image, tracks, starts))
    if not albums:
        return []

    multi = len(albums) > 1
    written, failures = [], []
    for image, tracks, starts in albums:
        try:
            total = float(AudioFile(image).audio.info.length)
        except Exception:
            total = 0.0
        if total <= starts[-1]:
            say(f"image rip not split: {os.path.basename(image)} — its cue's "
                "last track starts past the end of the audio")
            continue
        stem = os.path.splitext(os.path.basename(image))[0]
        prefix = f"{stem}-" if multi else ""
        jobs = []
        for i, track in enumerate(tracks):
            start = 0.0 if i == 0 else starts[i]
            end = starts[i + 1] if i + 1 < len(starts) else total
            if end <= start:
                continue
            tags = {"TRACKNUMBER": str(track["position"])}
            if track["title"]:
                tags["TITLE"] = track["title"]
            jobs.append((ffmpeg, image, os.path.join(folder, _track_name(track, stem, prefix)),
                         start, end - start, tags))
        if not jobs:
            continue
        lanes = worker_count(config, maximum=4, items=len(jobs))
        with ThreadPoolExecutor(max_workers=lanes) as ex:
            results = list(ex.map(_split_one, jobs))
        errs = [e for _p, e in results if e]
        made = [p for p, _e in results if p]
        if errs:
            for p in made:
                try:
                    os.remove(p)
                except OSError:
                    pass
            failures.extend(errs)
            say(f"image rip not split: {os.path.basename(image)} — {errs[0]}")
            continue
        moved = trash_file(image, music_folder=(config or {}).get("music_folder") or "",
                           user=(config or {}).get("auth_username") or "")
        if not moved:
            # The image outlives its split, so undoing the split is the only
            # way to leave the folder as it was found.
            for p in made:
                try:
                    os.remove(p)
                except OSError:
                    pass
            failures.append(f"{os.path.basename(image)}: could not be moved to the Trash")
            say(f"image rip not split: {os.path.basename(image)} — it could not "
                "be moved to the Trash, so the split was undone")
            continue
        written.extend(made)
        say(f"image rip split: {os.path.basename(image)} -> {len(made)} track(s)")
    return written

