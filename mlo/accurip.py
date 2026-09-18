"""AccurateRip .accurip file generation for CD rips via CUETools CLI only.

CUETools is the only tool used to generate .accurip files.  The file
format is the exact verbose log produced by ArCueDotNet.exe -v :

    [CUETools log; Date: 8/24/2026 8:11:34 PM; Version: 2.1.6]
    [CTDB TOCID: ...] found.
    Track | CTDB Status
      1   | (7901/7940) Accurately ripped
    [AccurateRip ID: 0014f184-00dfd375-b30a560e] found.
    Track   [  CRC   |   V2   ] Status
     01     [9593efc1|43d3ab48] (200+200/1513) Accurately ripped
    Offsetted by -762:
     01     [20358bfb] (006/1513) Accurately ripped
    ...
    Track Peak [ CRC32  ] [W/O NULL] ...
     01   98.8 [79F63527] [6C73A707]

The data is NOT derived from the rip .log's Copy CRC – it comes wholly
from decoding the audio and querying the AccurateRip/CTDB databases via
CUETools.

Because the 2.1.6 ArCueDotNet.exe build has no FLAC decoder, audio is
transcoded to temporary WAV (ffmpeg) and a patched cue is fed to
ArCueDotNet.  This still uses CUETools for the CRC + database lookup;
ffmpeg is only a lossless transport to WAV (which ArCueDotNet does
understand) and is required for speed – no CRC is computed in Python.

Files are named per the disc-pattern (default CD-{n}.accurip) so they
participate in the same deterministic rename as .log/.cue.
"""

import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from .audio import AudioFile
from .discs import album_discs, _disc_pattern_for, _disc_expected_name, disc_of_filename, CUE_FILE_RE
from .paths import AUDIO_EXTS
from .stats import (is_audio_file, _collect_targets, _walk_files, new_stats,
                    _make_pbar, _pbar_skip, _pbar_update, worker_count)
from .subproc import run_tool
from .ui import log, c, Color, print_header


# ----------------------------------------------------------------------
# Helpers – cue discovery
# ----------------------------------------------------------------------
def _find_cue_for_disc(album_dir, disc_num, discs, pattern):
    """Return path to the cue belonging to disc_num or None."""
    expected = _disc_expected_name(pattern, disc_num, ".cue")
    p = os.path.join(album_dir, expected)
    if os.path.isfile(p):
        return p
    # Fallback: scan cues and map FILE entries -> disc via exact basename
    known = {}
    for d, paths in (discs or {}).items():
        for pp in paths:
            known[os.path.basename(pp).lower()] = d
    cues = [f for f in os.listdir(album_dir) if f.lower().endswith(".cue")]
    for cf in sorted(cues):
        path = os.path.join(album_dir, cf)
        try:
            txt = open(path, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        file_discs = set()
        for m in CUE_FILE_RE.finditer(txt):
            raw = m.group(1).replace("/", "\\").split("\\")[-1]
            d = known.get(raw.lower())
            if d is not None:
                file_discs.add(d)
        if len(file_discs) == 1 and disc_num in file_discs:
            return path
        # single-disc album with one cue: that cue belongs to the sole disc
        if not file_discs and len(discs or {}) == 1 and len(cues) == 1:
            return path
    # last resort: any cue exists → use first (single-disc fallback)
    if cues and (not discs or len(discs) == 1):
        return os.path.join(album_dir, sorted(cues)[0])
    return None


def _patched_cue_for_temp(original_text, discs_wav_map):
    """Return a cue text where FILE lines point to the WAV basenames in discs_wav_map.

    discs_wav_map: {lowercase original basename -> wav basename}
    """
    out_lines = []
    for line in original_text.splitlines():
        m = CUE_FILE_RE.match(line.rstrip("\n"))
        if m:
            ref = m.group(1)
            base = ref.replace("/", "\\").split("\\")[-1]
            wav = discs_wav_map.get(base.lower())
            if wav:
                # keep any directory part of original ref (should be none) but replace basename
                head = ref[: len(ref) - len(base)] if base else ""
                new_ref = head + wav
                line = line.replace(f'"{ref}"', f'"{new_ref}"', 1)
        out_lines.append(line)
    return "\n".join(out_lines) + "\n"


# ----------------------------------------------------------------------
# WAV conversion via ffmpeg (lossless transport only – not a CRC tool)
# ----------------------------------------------------------------------
def _convert_to_wavs(ffmpeg_exe, track_paths, tmp_dir, config):
    """Decode each track to WAV in tmp_dir, keeping its own channel layout
    and sample rate (no upmix/resample — the WAV must be the source audio).

    Returns {original basename lower -> wav basename} on success.
    Parallelised; on any failure raises.
    """
    # Build tasks: (src, dst)
    tasks = []
    name_map = {}
    used = set()
    for src in track_paths:
        base = os.path.basename(src)
        stem = os.path.splitext(base)[0]
        wav_base = stem + ".wav"
        i = 2
        while wav_base.lower() in used:
            wav_base = f"{stem}_{i}.wav"
            i += 1
        used.add(wav_base.lower())
        tasks.append((src, os.path.join(tmp_dir, wav_base)))
        name_map[base.lower()] = wav_base

    workers = worker_count(config, default=4, maximum=8, items=len(tasks))
    errors = []

    def _one(pair):
        src, dst = pair
        proc = run_tool(
            [ffmpeg_exe, "-v", "error", "-i", src, "-f", "wav", "-acodec", "pcm_s16le", dst],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        if proc.returncode != 0 or not os.path.isfile(dst):
            return (src, proc.stderr or f"ffmpeg rc={proc.returncode}")
        return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, t): t for t in tasks}
        for fut in as_completed(futs):
            err = fut.result()
            if err:
                errors.append(err)

    if errors:
        raise RuntimeError("; ".join(f"{os.path.basename(s)}: {e[:120]}" for s, e in errors[:3]))
    return name_map


# ----------------------------------------------------------------------
# Invoke ArCueDotNet
# ----------------------------------------------------------------------
def _run_arcue(arcue_exe, cue_path, cwd, timeout=120):
    """Run ArCueDotNet <cue> and return stdout log text.

    Raises on failure.
    """
    # Use non-verbose mode to match CUETools GUI output (desktop reference:
    # no [ CTDBID ] verbose list, but includes [  LOG   ] column when .log present).
    # Previous -v gave extra CTDBID list (32206 bytes vs 27728) that desktop 2.2.6 does not emit.
    cmd = [arcue_exe, cue_path]
    proc = run_tool(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, cwd=cwd,
    )
    # ArCueDotNet returns 0 even when some tracks are No match – it still prints the log.
    # Only treat as error when no log header was emitted.
    # ArCueDotNet writes the log to stdout; in some builds it also mirrors to stderr – combine.
    combined = proc.stdout or ""
    if not combined and proc.stderr:
        combined = proc.stderr
    if not combined or "[CUETools log;" not in combined:
        raise RuntimeError(proc.stderr[:500] or proc.stdout[:500] or f"ArCueDotNet rc={proc.returncode} produced no log")
    return combined


def _generate_via_cuetools(ffmpeg_exe, arcue_exe, album_dir, disc_num, track_paths, cue_path, config):
    """Generate the CUETools verification log for one disc via ArCueDotNet.

    Uses a temp dir with WAVs + patched cue, invokes ArCueDotNet -v, captures
    the verbose log.  Returns the raw log text (as CUETools emitted it).
    """
    # If cue_path is None, we create a minimal cue synthesising TRACKs from sorted track_paths
    tmp_dir = tempfile.mkdtemp(prefix="mlo_accurip_")
    try:
        # Decode to WAVs
        name_map = _convert_to_wavs(ffmpeg_exe, track_paths, tmp_dir, config)

        if cue_path and os.path.isfile(cue_path):
            raw_cue = open(cue_path, "r", encoding="utf-8", errors="replace").read()
            patched = _patched_cue_for_temp(raw_cue, name_map)
        else:
            # Synthesize minimal cue (no REM DISCID – CUETools will compute TOC from file order)
            # Sort track_paths to deterministic order
            from .discs import _track_num_of, _file_track_number
            def _tn(p):
                try:
                    n = _track_num_of(p)
                    if n is not None:
                        return n
                    return _file_track_number(p) or 999
                except Exception:
                    return 999
            sorted_paths = sorted(track_paths, key=_tn)
            lines = []
            for idx, tp in enumerate(sorted_paths, 1):
                wav = name_map.get(os.path.basename(tp).lower(), os.path.splitext(os.path.basename(tp))[0] + ".wav")
                lines.append(f'FILE "{wav}" WAVE')
                lines.append(f'  TRACK {idx:02d} AUDIO')
                lines.append('    INDEX 01 00:00:00')
            patched = "\n".join(lines) + "\n"

        cue_tmp = os.path.join(tmp_dir, f"CD-{disc_num}.cue")
        # Write patched cue as UTF-8 without BOM; ArCueDotNet handles it
        with open(cue_tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(patched)

        # Copy .log file(s) so ArCueDotNet can emit [  LOG   ] column (desktop 2.2.6 reference has it)
        # Without the log in temp, Track Peak lacks LOG column (as in Program.accurip vs desktop).
        try:
            for lf in os.listdir(album_dir):
                if lf.lower().endswith(".log"):
                    try:
                        shutil.copy2(os.path.join(album_dir, lf), os.path.join(tmp_dir, lf))
                    except Exception:
                        pass
        except Exception:
            pass

        log_text = _run_arcue(arcue_exe, cue_tmp, cwd=tmp_dir, timeout=int(config.get("audit_per_file_timeout_s", 120) or 120) if config else 120)
        # Normalise line endings to \n but preserve every line's content exactly (no trimming of alignment spaces)
        log_text = log_text.replace("\r\n", "\n").replace("\r", "\n")
        # Ensure file ends with newline
        if not log_text.endswith("\n"):
            log_text += "\n"
        return log_text
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


# ----------------------------------------------------------------------
# Public helpers – .accurip status parsing (used by grader/audit)
# ----------------------------------------------------------------------
_AR_ID_RE = re.compile(r"\[AccurateRip ID:\s*([0-9a-fA-F\-]+)\]", re.IGNORECASE)
_TRACK_AR_RE = re.compile(r"^\s*0*(\d+)\s+\[[0-9a-fA-F]+\|[0-9a-fA-F]+\]\s*\([^\)]+\)\s*(.+)$")


def parse_accurip_status(text):
    """Return AccurateRip status for a CUETools .accurip log per http://cue.tools/wiki/CUETools_log.

    Spec: http://cue.tools/wiki/CUETools_log#AccurateRip_Section

      Header: ``[CUETools log; Date: ...; Version: ...]``
      CTDB TOCID / Track | CTDB Status ... (ignored for AR)
      ``[AccurateRip ID: <id>] found.``
      ``Track   [  CRC   |   V2   ] Status``
      `` 01     [aaaaaaaa|bbbbbbbb] (V1+V2/Y) Accurately ripped``
      `` 01     [aaaaaaaa|bbbbbbbb] (V1/Y) Accurately ripped`` (pre-2.1.4)
      `` 01     [aaaaaaaa|bbbbbbbb] (0/Y) No match``
      `` 01     [aaaaaaaa|bbbbbbbb] (0/Y) No match (V2 was not tested)``
      Offsetted blocks: ``Offsetted by N:`` + single-CRC lines (alternate pressings)
      Footer: ``Track Peak [ CRC32 ] ...``

    Returns (status, detail) where status in ('REAL','FAKE','NONE'):

      REAL – every track in the primary (zero-offset) AccurateRip block is
             ``Accurately ripped`` (spec: ``Your rip matches database records for this track``)
      FAKE – at least one track in the primary block is ``No match`` / ``No match (V2 was not tested)``
             (spec: ``No CRC match``) – rip does not match any DB record at zero offset
      NONE – no .accurip, empty, not a CUETools log, no ``AccurateRip ID``,
             or ``Track not present in AccurateRip database`` / ``disk not present`` –
             cannot verify (spec: ``disk not present in database`` / not in AR DB)
    """
    if not text or not text.strip():
        return ("NONE", "empty")
    if "[CUETools log;" not in text:
        return ("NONE", "not a CUETools log")
    m_id = _AR_ID_RE.search(text)
    if not m_id:
        low = text.lower()
        # Spec: ``disk not present in database`` or ``Track not present in AccurateRip database``
        if "not present" in low and "accuraterip" in low:
            return ("NONE", "AccurateRip disk/track not present in database")
        if "not found" in low and "accuraterip" in low:
            return ("NONE", "AccurateRip ID not found")
        if "accuraterip" not in low:
            return ("NONE", "no AccurateRip ID")
        return ("NONE", "no AccurateRip ID")
    start = m_id.end()
    # Per spec the header is ``Track   [  CRC   |   V2   ] Status`` (2.1.4+) or ``Track   [ CRC    ] Status`` (single CRC offsetted)
    header_pos = text.find("Track   [", start)
    if header_pos == -1:
        header_pos = text.find("Track   [", m_id.start())
    block_start = header_pos
    end_markers = ["Offsetted by", "Track Peak", "[CTDB TOCID"]
    block_end = len(text)
    for marker in end_markers[0:2]:
        idx = text.find(marker, block_start + 1)
        if idx != -1 and idx < block_end:
            block_end = idx
    block = text[block_start:block_end] if block_start != -1 else text[start:block_end]
    low_block = block.lower()
    # Global pre-check: ``Track not present in AccurateRip database`` inside primary block means NONE, not FAKE
    # (spec distinguishes not-present from No match). Keep block-level string for fallback.
    tracks_found = 0
    any_no_match = False
    any_not_present = False
    all_accurate = True
    for line in block.splitlines():
        m = _TRACK_AR_RE.match(line)
        if m:
            tracks_found += 1
            status = m.group(2).strip().lower()
            # Spec: ``Track not present in AccurateRip database`` -> not in DB -> NONE
            if "not present" in status:
                any_not_present = True
                all_accurate = False
                continue
            if "accurately ripped" not in status:
                all_accurate = False
                # Spec: ``No match`` / ``No match (V2 was not tested)`` -> FAKE
                if "no match" in status or "mismatch" in status:
                    any_no_match = True
                else:
                    # Any other non-accurate status is also a mismatch
                    any_no_match = True
            # else: accurately ripped -> ok (spec may have "or (N/Y) differs" for CTDB, not AR)
    if tracks_found == 0:
        if "not present" in low_block and "accuraterip" in low_block:
            return ("NONE", "Track not present in AccurateRip database")
        if "no match" in low_block:
            return ("FAKE", "AccurateRip No match in .accurip")
        if "accurately ripped" in low_block:
            return ("REAL", None)
        # No parsable track lines and no spec phrase -> unparsable -> NONE
        return ("NONE", "no track status")
    # Prefer NOT PRESENT (NONE) over FAKE? If any track is not present, that track cannot be verified;
    # for strict auditing, missing AR entry should be treated as NONE (required -> audit FAIL as missing).
    # But if another track is FAKE, FAKE takes precedence for reporting.
    if any_no_match:
        return ("FAKE", "AccurateRip No match in .accurip")
    if any_not_present:
        return ("NONE", "Track not present in AccurateRip database")
    if all_accurate and tracks_found > 0:
        return ("REAL", None)
    return ("NONE", "unparsable")


def parse_accurip_per_track(text):
    """Per-track AccurateRip status from the primary (zero-offset) block.

    Returns dict {track_number: status} where status in ('REAL','FAKE','NONE').
    Track numbers are 1-based ints as found in the ``Track   [ CRC | V2 ]`` table.
    Covers spec cases: ``Accurately ripped`` → REAL, ``No match`` / ``No match (V2 was not tested)`` → FAKE,
    ``Track not present in AccurateRip database`` → NONE.

    If the log has no AccurateRip ID or no parsable primary block, returns {}.
    Offsetted by ... blocks are ignored (alternate pressings per spec).
    """
    if not text or "[CUETools log;" not in text:
        return {}
    m_id = _AR_ID_RE.search(text)
    if not m_id:
        return {}
    start = m_id.end()
    header_pos = text.find("Track   [", start)
    if header_pos == -1:
        header_pos = text.find("Track   [", m_id.start())
    if header_pos == -1:
        return {}
    block_start = header_pos
    block_end = len(text)
    for marker in ("Offsetted by", "Track Peak", "[CTDB TOCID"):
        # Only first two terminate primary, but include third as safety
        if marker in ("Offsetted by", "Track Peak"):
            idx = text.find(marker, block_start + 1)
            if idx != -1 and idx < block_end:
                block_end = idx
    block = text[block_start:block_end]
    per = {}
    for line in block.splitlines():
        m = _TRACK_AR_RE.match(line)
        if not m:
            continue
        try:
            tn = int(m.group(1))
        except ValueError:
            continue
        status_raw = m.group(2).strip().lower()
        if "not present" in status_raw:
            per[tn] = "NONE"
        elif "accurately ripped" in status_raw:
            per[tn] = "REAL"
        elif "no match" in status_raw:
            per[tn] = "FAKE"
        elif "mismatch" in status_raw:
            per[tn] = "FAKE"
        else:
            # Unknown status → treat as FAKE if not empty, else NONE
            per[tn] = "FAKE" if status_raw else "NONE"
    return per


def _canonical_accurip_text(content, keep_empty_lines=False, keep_other_lines=False, append_final_newline=None):
    """Canonical .accurip text per user spec: trim each line, trim outer blanks only.

    - Delete all leading/trailing spaces/tabs on each line (``line.strip(" \\t")``)
    - Only delete blank lines at the top and bottom of the file; preserve all
      blank lines in the middle (no collapsing of consecutive blanks in the body).
      This removes the extra blank line at the bottom similar to ``canonical_cue_text``
      which does ``rstrip()`` — the file must not end with an empty line.
    - Final newline is controlled by ``append_final_newline`` (like ``_canonical_lyrics``
      and ``canonical_cue_text``); when False (default) the file has **no** trailing
      newline byte, matching ``.cue`` default. When True, exactly one LF is appended.
    This is intentionally *not* preserving table-alignment leading spaces — per
    user request for optimization, the file is still valid for parsing.
    Runs directly after generation and is used for grading.
    """
    if content is None:
        return ""
    # Normalise line endings first
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    # Strip each line (leading/trailing spaces/tabs only, not other whitespace)
    stripped = [ln.strip(" \t") for ln in lines]
    # Remove blank lines only at top and bottom (preserve middle blanks verbatim)
    # Respects keep_empty_accurip_lines (like keep_empty_cue_lines)
    if not keep_empty_lines:
        while stripped and stripped[0] == "":
            stripped.pop(0)
        while stripped and stripped[-1] == "":
            stripped.pop()
    result = "\n".join(stripped)
    # Mimic cue/lyrics final-line handling: no trailing blank line, optional single LF
    # Only strip trailing blank lines when not keeping empty lines (default)
    if not keep_empty_lines:
        result = result.rstrip("\r\n")
    # Re-apply outer logic after rstrip in case it created a new trailing blank
    # (e.g., "a\nb\n " -> "a\nb" after per-line strip + join is already clean, rstrip is no-op)
    if append_final_newline is None:
        # Caller didn't specify — default to no trailing newline like .cue/.lrc default (append_final_newline False)
        # Keep backward compat: if caller expects old unconditional "\n", they should pass True explicitly
        # For now, default to False to remove the extra blank line at bottom
        append_final_newline = False
    if result and append_final_newline:
        result += "\n"
    return result


def run_generate_accurip(config):
    """Generate CD-{n}.accurip via CUETools CLI for every MEDIA=CD disc.

    Respects write_accurip_files + force_accurip.  The CSV log path is the
    disc-pattern (default CD-{n}) so it follows the same rename as .log/.cue.
    Returns stats dict (total_scanned / modified_count / skipped / errors).
    """
    folder = config["music_folder"]
    force = config.get("force_accurip", False)
    write_files = config.get("write_accurip_files", True)

    stats = new_stats()
    print_header("AccurateRip (.accurip) Generator — CUETools")
    log(f"music folder: {folder} · write .accurip files: {write_files} · force: {force}")

    if not os.path.isdir(folder):
        log(c(f"ERROR: folder does not exist: {folder}", Color.RED))
        return stats

    from .tools import detect_all_tools
    tools = detect_all_tools()
    ffmpeg_exe = (tools.get("ffmpeg") or {}).get("ffmpeg_exe")
    if not ffmpeg_exe or not os.path.isfile(ffmpeg_exe):
        log(c("ERROR: ffmpeg not found — needed to transport FLAC → WAV for CUETools", Color.RED))
        log(c("Install via Dependencies → ffmpeg or place ffmpeg.exe in .dependencies/ffmpeg v*/", Color.YELLOW))
        return stats

    cuetools = tools.get("cuetools") or {}
    arcue_exe = cuetools.get("arcue_exe")
    # Fallback: try direct exe paths for both 2.1.6 (ArCueDotNet) and 2.2.6 (CUETools.ARCUE)
    if not arcue_exe or not os.path.isfile(arcue_exe):
        for cand_name in ("CUETools.ARCUE.exe", "ArCueDotNet.exe"):
            cand = os.path.join(cuetools.get("dir", ""), cand_name)
            if os.path.isfile(cand):
                arcue_exe = cand
                break
    if not arcue_exe or not os.path.isfile(arcue_exe):
        # Last resort scan
        try:
            d = cuetools.get("dir", "")
            if d and os.path.isdir(d):
                for entry in os.listdir(d):
                    low = entry.lower()
                    if "arcue" in low and low.endswith(".exe"):
                        cand = os.path.join(d, entry)
                        if os.path.isfile(cand):
                            arcue_exe = cand
                            break
        except Exception:
            pass
    if not arcue_exe or not os.path.isfile(arcue_exe):
        log(c("ERROR: CUETools ARCUE (ArCueDotNet/CUETools.ARCUE) not found — needed for AccurateRip verification", Color.RED))
        log(c("Install via Dependencies → CUETools or place CUETools.ARCUE.exe in .dependencies/CUETools v*/", Color.YELLOW))
        return stats
    log(f"cuetools: {arcue_exe} · v{cuetools.get('version')} · ffmpeg: {ffmpeg_exe}")

    targets = config.get("targets")
    files = _collect_targets(targets, AUDIO_EXTS) if targets is not None else None
    if targets is not None and files is not None:
        album_dirs = sorted({os.path.dirname(f) for f in files})
    else:
        from .stats import _find_albums
        album_dirs = _find_albums(folder)

    if not album_dirs:
        log("No albums found.")
        return stats

    # Filter to CD albums only (any track's MEDIA==CD)
    cd_albums = []
    for ad in album_dirs:
        try:
            has_cd = False
            for f in os.listdir(ad):
                if not f.lower().endswith(AUDIO_EXTS):
                    continue
                try:
                    af = AudioFile(os.path.join(ad, f))
                    if str(af.get_tag("MEDIA") or "").strip() == "CD":
                        has_cd = True
                        break
                except Exception:
                    continue
            if has_cd:
                cd_albums.append(ad)
        except OSError:
            continue

    if not cd_albums:
        log("No CD albums (MEDIA=CD) found for AccurateRip.")
        return stats

    log(f"found {len(cd_albums)} CD album(s) for AccurateRip (CUETools)")

    pattern = _disc_pattern_for(config)
    # One tick per album: the CUETools pass is this script's slow part and the
    # UI header follows this bar — without one the header sat frozen on the
    # previous script's numbers for the whole run.
    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(len(cd_albums), "AccurateRip", unit="album")
    for album_dir in cd_albums:
        discs = album_discs(album_dir)
        if not discs:
            # Single-disc fallback
            try:
                aud = [os.path.join(album_dir, f) for f in os.listdir(album_dir) if is_audio_file(f)]
                logs = [f for f in os.listdir(album_dir) if f.lower().endswith(".log")]
                cues = [f for f in os.listdir(album_dir) if f.lower().endswith(".cue")]
                if aud and (logs or cues):
                    discs = {1: aud}
                else:
                    stats["skipped_count"] += 1
                    _pbar_skip(pbar, counts)
                    continue
            except OSError:
                stats["skipped_count"] += 1
                _pbar_skip(pbar, counts)
                continue

        # Automatic rename for .accurip to CD-{n}.accurip (per user: CD-$(n) scheme applies)
        # This runs before generation so legacy names like App.accurip become CD-1.accurip
        try:
            from .discs import rename_accurip_for_discs
            rename_accurip_for_discs(album_dir, discs, log_fn=lambda m: log(f"  {m}"), config=config)
        except Exception:
            pass

        for disc_num, track_paths in sorted(discs.items()):
            accurip_path = os.path.join(album_dir, _disc_expected_name(pattern, disc_num, ".accurip"))
            # Skip if exists and not forced and already a correctly formatted CUETools log
            if os.path.exists(accurip_path) and not force:
                try:
                    existing = open(accurip_path, "r", encoding="utf-8", errors="replace").read()
                    if existing and "[CUETools log;" in existing:
                        # Old 2.1.6 -v verbose files contain [ CTDBID ] list; new 2.2.6 without -v does not.
                        # Also old files lack [  LOG   ] column when a .log is present.
                        has_ctdbid = "[ CTDBID ]" in existing
                        has_log_col = "[  LOG   ]" in existing
                        try:
                            log_exists = os.path.isfile(os.path.join(album_dir, _disc_expected_name(pattern, disc_num, ".log")))
                            if not log_exists:
                                # Fallback: any .log in folder means we expect LOG column
                                log_exists = any(f.lower().endswith(".log") for f in os.listdir(album_dir))
                        except Exception:
                            log_exists = False
                        is_old_version = "Version: 2.1.6" in existing and str(cuetools.get("version")) == "2.2.6"
                        needs_regen = False
                        if has_ctdbid:
                            needs_regen = True
                        elif log_exists and not has_log_col:
                            needs_regen = True
                        elif is_old_version:
                            needs_regen = True
                        if not needs_regen:
                            stats["skipped_count"] += 1
                            continue
                    if existing and existing.strip():
                        # legacy synthetic verified file – regenerate via CUETools for correct format
                        pass
                    else:
                        # empty – regenerate
                        pass
                except OSError:
                    pass
                # if not a correctly formatted CUETools log, we will regenerate

            if not write_files:
                stats["skipped_count"] += 1
                continue

            cue_path = _find_cue_for_disc(album_dir, disc_num, discs, pattern)
            if cue_path is not None and not os.path.isfile(cue_path):
                cue_path = None
            if cue_path is None:
                # No cue found – synthesize a minimal cue from the track order so CUETools can still verify.
                # This is required for automatic .accurip generation on albums where the cue is missing
                # but MEDIA=CD; the synthetic cue will list the WAV transports in track-number order.
                log(c(f"  {os.path.basename(album_dir)} disc {disc_num}: no cue found – synthesizing minimal cue for CUETools", Color.YELLOW))

            # Sort tracks deterministically
            from .discs import _track_num_of, _file_track_number
            def _tn(p):
                try:
                    n = _track_num_of(p)
                    if n is not None:
                        return n
                    return _file_track_number(p) or 999
                except Exception:
                    return 999
            track_paths = sorted(track_paths, key=_tn)

            try:
                content = _generate_via_cuetools(ffmpeg_exe, arcue_exe, album_dir, disc_num, track_paths, cue_path, config)
                # Format directly after generation per user spec: trim each line, trim outer blanks only
                # No extra blank line at bottom — like .cue's rstrip(), final newline only if append_final_newline
                # Respects keep_empty_accurip_lines (like keep_empty_cue_lines)
                content = _canonical_accurip_text(
                    content,
                    keep_empty_lines=config.get("keep_empty_accurip_lines", False),
                    append_final_newline=config.get("append_final_newline", False),
                )
            except Exception as e:
                stats["error_count"] += 1
                stats["errors"].append((accurip_path, str(e)[:300]))
                log(c(f"  failed {os.path.basename(album_dir)} CD-{disc_num}: {e}", Color.RED))
                continue

            # Atomic write with fsync to avoid corruption on crash/power loss
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(prefix=".accurip_tmp_", suffix=".accurip", dir=album_dir)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                    f.write(content)
                    try:
                        f.flush()
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                os.replace(tmp, accurip_path)
                try:
                    d_fd = os.open(album_dir, os.O_DIRECTORY)
                    try:
                        os.fsync(d_fd)
                    finally:
                        os.close(d_fd)
                except Exception:
                    pass
                stats["modified_count"] += 1
                stats["total_scanned"] += 1
                # Log short summary – parse status for nice output
                st, _ = parse_accurip_status(content)
                col = Color.GREEN if st == "REAL" else (Color.RED if st == "FAKE" else Color.YELLOW)
                log(f"  {os.path.basename(album_dir)}: {os.path.basename(accurip_path)} ({len(track_paths)} tracks) → {c(st, col)}")
            except Exception as e:
                stats["error_count"] += 1
                stats["errors"].append((accurip_path, str(e)))
                log(c(f"  failed write {os.path.basename(accurip_path)}: {e}", Color.RED))
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
        _pbar_update(pbar, counts)

    try:
        pbar.close()
    except Exception:
        pass
    return stats
