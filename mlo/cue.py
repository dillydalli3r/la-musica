"""CUE sheet formatter."""
import os
import re
import subprocess
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from .atomic import replace_locked
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

        replace_locked(tmp_path, filename)
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


# Tags that describe ONE track, and so must not be copied from the whole-disc
# image onto every split track. Everything else (ALBUM, ALBUMARTIST, DATE,
# GENRE, the MusicBrainz release ids, RELEASECOUNTRY, …) is album-level and is
# carried over, so a split track keeps the identity the rip arrived with
# instead of being re-identified from scratch.
_PER_TRACK_TAGS = frozenset({
    "TITLE", "TITLESORT", "TRACKNUMBER", "TRACKTOTAL", "TOTALTRACKS",
    "MUSICBRAINZ_TRACKID", "MUSICBRAINZ_RELEASETRACKID",
    "ACOUSTID_ID", "ACOUSTID_FINGERPRINT", "ISRC",
    "RATING", "LYRICS", "LYRICIST", "COMMENT", "MOOD", "ENERGY",
    "BPM", "KEY", "INITIALKEY", "DYNAMIC RANGE",
})


def _split_one(args):
    """Write ONE track of an image rip -> (path, error); path "" on failure.

    *tags* is the FULL tag set the split track should carry: the image's own
    album-level tags, with this track's number/title filled in. ffmpeg gets
    only the two keys it can safely write (an arbitrary MusicBrainz value in a
    `-metadata` argument is one shell-level escaping problem the tag writer
    below does not have); the rest land through `AudioFile`, the app's one
    canonical tag writer, after the audio exists.
    """
    ffmpeg, image, dest, start, dur, track_no, title, tags, threads = args
    cmd = [
        ffmpeg, "-y", "-v", "error", "-nostdin",
        # -ss BEFORE -i so ffmpeg seeks instead of decoding the disc from the
        # top once per track. Input seeking is sample-accurate (accurate_seek
        # is the default), which is also why this is a decode/re-encode and
        # never a "-c:a copy": a stream copy cuts at a frame boundary and can
        # shift a track by up to one frame (~93 ms at 44.1 kHz). FLAC -> FLAC
        # is lossless either way, so nothing is lost but a little CPU.
        "-ss", f"{start:.3f}", "-i", image,
        "-t", f"{dur:.3f}", "-map", "0:a:0", "-map", "0:v?",
        # Cover art rides along (an attached picture is a video stream in
        # FLAC); everything else is written deliberately below, so ffmpeg must
        # not inherit the image's per-track tags into every split track.
        "-c:a", "flac", "-c:v", "copy", "-map_metadata", "-1",
        "-metadata", f"TRACKNUMBER={track_no}",
    ]
    if title:
        cmd += ["-metadata", f"TITLE={title}"]
    if threads:
        # Each split job gets its share of the process's worker budget; four
        # decoders each claiming every core is the one place in the engine
        # that used to bypass `tool_threads`.
        cmd += ["-threads", str(int(threads))]
    cmd.append(dest)
    try:
        run_tool(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                 text=True, encoding="utf-8", errors="replace", timeout=1800)
    except Exception as e:  # noqa: BLE001 — any failure is "this track failed"
        return "", f"{os.path.basename(dest)}: {e}"
    if not os.path.isfile(dest) or not os.path.getsize(dest):
        return "", f"{os.path.basename(dest)}: ffmpeg wrote no file"
    # The album-level identity, through the ONE tag writer (canonical spelling,
    # atomic swap, a queued write when the file is held): a split track must
    # not depend on the later chain to learn which release it belongs to.
    try:
        from .audio import AudioFile
        af = AudioFile(dest)
        af.defer_save(True)
        for key, value in tags.items():
            if value is None or str(value).strip() == "":
                continue
            af.set_tag(key, value)
        if af.defer_save(False) is False:
            raise RuntimeError("the tag write did not land")
    except Exception as e:  # noqa: BLE001 — an untagged track is a failed split
        try:
            os.remove(dest)
        except OSError:
            pass
        return "", f"{os.path.basename(dest)}: tags failed ({e})"
    return dest, ""


def _image_folders(folder, depth=0):
    """Every folder under *folder* that holds a cue plus an audio image.

    A one-disc rip keeps both at the album's top level, while a multi-disc
    release that arrived as `CD1/CD1.flac` + `CD1/CD1.cue`, `CD2/…` keeps them
    one level down. The splitter used to list only the folder it was handed,
    so those discs were never split. This walks the album's own subfolders
    (bounded to three levels, and never into the app's hidden state folders)
    and answers each folder that carries the pair.

    A folder WITH a cue is treated as a complete disc folder and is not walked
    further: its subfolders are artwork or scans, not another disc. A folder
    WITHOUT one is walked, which is how `CD1/`, `CD2/` are found.
    """
    from .paths import LIB_AUDIO_EXTS

    if not folder or not os.path.isdir(folder) or depth > 3:
        return []
    try:
        entries = sorted(os.listdir(folder))
    except OSError:
        return []
    cues = [e for e in entries if e.lower().endswith(".cue")]
    audio = [e for e in entries
             if os.path.splitext(e)[1].lower() in LIB_AUDIO_EXTS
             and os.path.isfile(os.path.join(folder, e))]
    found = [folder] if (cues and audio) else []
    # A disc folder is usually a leaf, but an album can also hold a bonus
    # image at its top level BESIDE CD1/CD2. The walk therefore continues
    # through the children either way; a child with no cue pair contributes
    # nothing.
    for name in entries:
        if name.startswith("."):
            continue
        child = os.path.join(folder, name)
        if os.path.isdir(child):
            found.extend(_image_folders(child, depth + 1))
    return found


def _image_rips(folder):
    """The usable image rips in *folder* (including its disc subfolders).

    -> [{folder, image, cue, tracks, starts}], where `starts` are the cue's
    INDEX 01 points in seconds. One image is claimed by one sheet, and a sheet
    with fewer than two tracks, no INDEX 01s, or non-increasing points is not
    a split candidate (there is nothing to cut it up with).
    """
    from .discs import read_cue_text
    from .paths import LIB_AUDIO_EXTS

    out = []
    for where in _image_folders(folder):
        try:
            entries = sorted(os.listdir(where))
        except OSError:
            continue
        audio = [e for e in entries
                 if os.path.splitext(e)[1].lower() in LIB_AUDIO_EXTS
                 and os.path.isfile(os.path.join(where, e))]
        claimed = set()
        for cue in [e for e in entries if e.lower().endswith(".cue")]:
            cue_path = os.path.join(where, cue)
            _files, tracks = _cue_sheet(read_cue_text(cue_path))
            if len(tracks) < 2:
                continue
            starts = [t["start"] for t in tracks]
            if any(s is None for s in starts):
                continue
            if any(b <= a for a, b in zip(starts, starts[1:])):
                continue
            image = _image_for(where, audio, tracks)
            if not image or image in claimed:
                continue
            claimed.add(image)
            out.append({"folder": where, "image": image, "cue": cue_path,
                        "tracks": tracks, "starts": starts})
    return out


def image_rip_info(folder, config=None):
    """What a folder holds that a split would act on — the read-only shape the
    album page asks for before offering the action, and the same records
    `split_image_rip` works from.

    -> {"rip": bool, "images": [{folder, image, cue, tracks, duration}],
        "tracks": total}
    A folder that is not an image rip answers `{"rip": False, "images": [],
    "tracks": 0}`; nothing here writes, moves or trashes anything.
    """
    from .audio import AudioFile

    rips = _image_rips(folder) if folder and os.path.isdir(folder) else []
    images = []
    for rec in rips:
        try:
            duration = round(float(AudioFile(rec["image"]).audio.info.length), 3)
        except Exception:
            duration = None
        images.append({
            "folder": rec["folder"],
            "image": rec["image"],
            "cue": rec["cue"],
            "tracks": len(rec["tracks"]),
            "duration": duration,
        })
    return {"rip": bool(images), "images": images,
            "tracks": sum(i["tracks"] for i in images)}


def _cue_file_name(name):
    """One FILE reference as a cue sheet spells it (quotes escaped)."""
    return '"' + str(name).replace("\\", "/").replace('"', '\\"') + '"'


def _rewrite_cue_for_split(cue_path, tracks, written):
    """Point a sheet at the SPLIT tracks instead of the trashed image.

    The original sheet said `FILE "Album.flac"` once and gave every TRACK an
    absolute INDEX 01. After a split that reference names a file the album no
    longer holds, and each track is now its own file that starts at zero, so the
    sheet is rewritten as a valid per-track sheet: one FILE block per written
    track, its own TITLE/PERFORMER/ISRC kept, INDEX 01 reset to 00:00:00. The
    album-level header (PERFORMER, TITLE, CATALOG, REM) is preserved.

    Returns True when the sheet was rewritten. A failure is not fatal to the
    split (the audio is already written and correct); the caller logs it.
    """
    from .discs import read_cue_text

    if not cue_path or not os.path.isfile(cue_path) or len(tracks) != len(written):
        return False
    text = read_cue_text(cue_path)
    header, blocks, current = [], [], None
    track_line = re.compile(r"^\s*TRACK\s+\d+", re.IGNORECASE)
    drop = re.compile(r"^\s*(FILE|INDEX|PREGAP|POSTGAP)\b", re.IGNORECASE)
    for raw in text.splitlines():
        if track_line.match(raw):
            current = [raw]
            blocks.append(current)
        elif current is not None:
            if not drop.match(raw):
                current.append(raw)
        elif not re.match(r"^\s*FILE\b", raw, re.IGNORECASE):
            header.append(raw)
    if len(blocks) != len(written):
        return False
    out = [line for line in header if line.strip()]
    for block, path in zip(blocks, written):
        body = [line for line in block if line.strip()]
        out.append(f'FILE {_cue_file_name(os.path.basename(path))} WAVE')
        out.extend(body)
        out.append("    INDEX 01 00:00:00")
    new_text = "\n".join(out).rstrip() + "\n"
    if new_text == text:
        return True
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".cue_split_", suffix=".cue",
                                        dir=os.path.dirname(cue_path))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new_text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
        replace_locked(tmp_path, cue_path)
        fsync_dir(os.path.dirname(cue_path))
        tmp_path = None
        return True
    except Exception:
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def split_image_rip(folder, config=None, log_fn=None, on_split=None):
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
    Trash (never a bare delete) and the .cue is repointed at the split tracks
    (one FILE block per track, INDEX 01 reset to zero — see
    `_rewrite_cue_for_split`), so the default `grade_check_cue_files` check
    keeps passing instead of reporting a FILE the album no longer holds.

    Album-level tags on the image (ALBUM, ALBUMARTIST, DATE, GENRE, the
    MusicBrainz release ids, …) are carried onto every split track through the
    app's canonical tag writer; track-level ones are replaced by the cue's own
    number/title. Embedded cover art is copied along when the image had it.

    *on_split* is an optional ``fn(image_path, written_paths)`` called after a
    successful split, before the function moves on to the next image. The HTTP
    split uses it to repoint playlist entries that held the trashed image; the
    import funnel does not need it.

    Returns the paths written; [] when the folder holds no image rip, or none
    could be split (no INDEX 01s, no ffmpeg, unreadable durations).
    """
    from .audio import AudioFile
    from .discs import disc_dir_number, disc_number_of_path
    from .paths import trash_file
    from .stats import tool_threads
    from .tools import detect_all_tools

    def say(line):
        if log_fn is not None:
            try:
                log_fn(line)
            except Exception:
                pass

    if not folder or not os.path.isdir(folder):
        return []
    rips = _image_rips(folder)
    if not rips:
        return []
    ffmpeg = str((detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe") or "")
    if not ffmpeg or not os.path.isfile(ffmpeg):
        return []

    # How many images share ONE folder: only those need a `<stem>-` prefix, so
    # two discs' "01 …" tracks cannot collide. Images in different disc
    # folders are already told apart by their folder.
    per_folder = {}
    for rec in rips:
        per_folder[rec["folder"]] = per_folder.get(rec["folder"], 0) + 1

    written, failures = [], []
    for rec in rips:
        image, tracks, starts = rec["image"], rec["tracks"], rec["starts"]
        where = rec["folder"]
        try:
            source = AudioFile(image)
            total = float(source.audio.info.length)
            source_tags = source.all_tags() or {}
        except Exception:
            total, source_tags = 0.0, {}
        if total <= starts[-1]:
            say(f"image rip not split: {os.path.basename(image)} — its cue's "
                "last track starts past the end of the audio")
            continue
        # Album-level identity travels to every split track; track-level tags
        # are replaced by this track's own. ReplayGain is per FILE (a short
        # track has a different loudness), so the image's values must not be
        # copied onto tracks that were never measured.
        carry = {key: value for key, value in source_tags.items()
                 if str(key).upper() not in _PER_TRACK_TAGS
                 and not str(key).upper().startswith("REPLAYGAIN")}
        disc_no = (disc_dir_number(os.path.basename(where))
                   or disc_number_of_path(image))
        if disc_no:
            carry["DISCNUMBER"] = str(disc_no)
        stem = os.path.splitext(os.path.basename(image))[0]
        prefix = f"{stem}-" if per_folder.get(where, 1) > 1 else ""
        jobs = []
        for i, track in enumerate(tracks):
            start = 0.0 if i == 0 else starts[i]
            end = starts[i + 1] if i + 1 < len(starts) else total
            if end <= start:
                continue
            tags = dict(carry)
            tags["TRACKNUMBER"] = str(track["position"])
            tags["TRACKTOTAL"] = str(len(tracks))
            if track["title"]:
                tags["TITLE"] = track["title"]
            jobs.append((ffmpeg, image, os.path.join(where, _track_name(track, stem, prefix)),
                         start, end - start, str(track["position"]),
                         track["title"] or "", tags))
        if not jobs:
            continue
        lanes = worker_count(config, maximum=4, items=len(jobs))
        threads = tool_threads(config, lanes) if lanes > 0 else 0
        with ThreadPoolExecutor(max_workers=lanes) as ex:
            # The ffmpeg lanes are capped at their share of the process's
            # thread budget: without `-threads` each of the four decoders
            # claimed every core, the one place in the engine that bypassed
            # `tool_threads`.
            args = [(a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7], threads)
                    for a in jobs]
            results = list(ex.map(_split_one, args))
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
        if _rewrite_cue_for_split(rec["cue"], tracks, made):
            say(f"cue repointed: {os.path.basename(rec['cue'])} -> {len(made)} per-track FILE blocks")
        else:
            say(f"warning: {os.path.basename(rec['cue'])} still names the image "
                f"({os.path.basename(image)}); it could not be repointed")
        say(f"image rip split: {os.path.basename(image)} -> {len(made)} track(s)")
        if on_split is not None:
            try:
                on_split(image, made)
            except Exception:
                # A consumer's own bookkeeping must never undo a finished
                # split (the audio is correct and the image is already gone).
                traceback.print_exc()
    return written

