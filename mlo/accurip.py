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

import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from .audio import AudioFile
from .discs import album_discs, _disc_pattern_for, _disc_expected_name, CUE_FILE_RE
from .paths import AUDIO_EXTS, app_data_dir, fsync_dir
from .stats import (is_audio_file, _collect_targets, new_stats,
                    _make_pbar, _pbar_skip, _pbar_update, worker_count)
from .subproc import run_tool
from .tagtext import canonical_text
from .ui import log, c, Color, print_header

# ----------------------------------------------------------------------
# Freshness: what a .accurip was generated FROM
# ----------------------------------------------------------------------
# The verdicts in a .accurip are CRCs of the disc's AUDIO, so the staleness
# test has to be about the audio. It used to be "is any track newer than the
# .accurip", which is wrong in the expensive direction: every tag write bumps
# a track's mtime, scripts 10 and 21 write tags AFTER 9 in the run order, so
# on the next run the whole disc looked re-ripped — re-decoded to WAV and
# re-verified through CUETools, every run, for ever. FLAC stores the MD5 of
# its own audio stream (STREAMINFO), which a tag rewrite does not change and a
# re-rip or re-encode does: that is the identity. The map below is what each
# .accurip was built from, kept in the app's data folder (this library's, like
# the audit evidence); a track whose identity cannot be read falls back to the
# mtime rule, so nothing is ever assumed fresh on a weaker test than before.
_IDENTITY_NAME = "accurip_evidence.json"
# {normcase(abspath(.accurip)): {track_path: "flac:<md5>"}}
_IDENTITIES: dict = {}


def _audio_identity(path):
    """*path*'s audio identity — "" when this container cannot answer."""
    try:
        af = AudioFile(path)
        sig = getattr(getattr(af, "audio", None), "info", None)
        sig = getattr(sig, "md5_signature", 0)
        if sig:
            return f"flac:{sig}"
    except Exception:
        pass
    return ""


def _identity_path(config):
    try:
        return os.path.join(app_data_dir(config.get("music_folder")),
                            _IDENTITY_NAME)
    except Exception:
        return ""


def _load_identities(config):
    """The stored map; entries whose .accurip is gone are dropped."""
    path = _identity_path(config)
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and os.path.exists(k)}


def _save_identities(config, evidence):
    """Atomic write of the map; never raises (it is only a shortcut)."""
    path = _identity_path(config)
    if not path:
        return
    tmp = None
    try:
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".accurip_evidence_", suffix=".json",
                                   dir=d)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(evidence, f)
            try:
                f.flush()
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except OSError:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


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
    # A converted album's cue still names the file it was ripped from
    # (album.ape for album.flac), so a reference whose basename is not in the
    # map is retried by stem. A stem shared by two tracks proves nothing and
    # is left alone: guessing there puts one track's audio under another's.
    by_stem = {}
    for src_base, wav in discs_wav_map.items():
        by_stem.setdefault(os.path.splitext(src_base)[0], set()).add(wav)
    out_lines = []
    for line in original_text.splitlines():
        m = CUE_FILE_RE.match(line.rstrip("\n"))
        if m:
            ref = m.group(1)
            base = ref.replace("/", "\\").split("\\")[-1]
            wav = discs_wav_map.get(base.lower())
            if not wav:
                cands = by_stem.get(os.path.splitext(base.lower())[0])
                wav = next(iter(cands)) if cands and len(cands) == 1 else None
            if wav:
                # keep any directory part of original ref (should be none) but replace basename
                head = ref[: len(ref) - len(base)] if base else ""
                new_ref = head + wav
                line = line.replace(f'"{ref}"', f'"{new_ref}"', 1)
        out_lines.append(line)
    return "\n".join(out_lines) + "\n"


def _cue_all_files_to(cue_text, wav):
    """Point every FILE line of `cue_text` at one WAV basename.

    For a disc whose audio is a single image file per disc: the cue's TRACK
    and INDEX lines are the disc's layout and cannot be rebuilt from the audio
    alone, so every reference — stale or not — has exactly one possible
    answer, and it is this file.
    """
    out_lines = []
    for line in cue_text.splitlines():
        m = CUE_FILE_RE.match(line.rstrip("\n"))
        if m:
            ref = m.group(1)
            line = line.replace(f'"{ref}"', f'"{wav}"', 1)
        out_lines.append(line)
    return "\n".join(out_lines) + "\n"


# ----------------------------------------------------------------------
# WAV conversion via ffmpeg (lossless transport only – not a CRC tool)
# ----------------------------------------------------------------------
def _transport_codec(src):
    """The pcm codec for one track's WAV transport, or (None, reason).

    The transported WAV is what ArCueDotNet CRCs, so it must be the source
    audio itself: a fixed pcm_s16le changed every sample of a 24-bit rip, so
    the whole disc came back "No match" from AccurateRip — a false verdict on
    perfectly good audio (and audit_require_accuraterip then wrote FAKE for
    it). Only a depth PCM cannot carry is refused, and the caller reports
    that disc as unverifiable rather than as not-in-database.
    """
    bits = 0
    try:
        info = getattr(getattr(AudioFile(src), "audio", None), "info", None)
        bits = int(getattr(info, "bits_per_sample", 0) or 0)
    except Exception:
        bits = 0
    if bits <= 16:
        # Unknown depth (0) stays 16-bit: a CD-DA rip is 16/44.1, which is
        # also the only depth AccurateRip has entries for.
        return "pcm_s16le", None
    if bits <= 24:
        return "pcm_s24le", None
    if bits <= 32:
        return "pcm_s32le", None
    return None, f"{bits}-bit audio"


def _wav_transport_timeout(src):
    """Timeout for one track's lossless WAV transport, scaled by duration.

    A fixed 120 s fails a long or high-resolution track spuriously — the
    decode is I/O bound (~4x realtime at worst) and the disc is then thrown
    away for a slow disk, not for bad audio.
    """
    timeout = 120
    try:
        info = getattr(getattr(AudioFile(src), "audio", None), "info", None)
        seconds = float(getattr(info, "length", 0) or 0)
        if seconds > 0:
            timeout = int(max(120.0, seconds * 4.0 + 60.0))
    except Exception:
        pass
    return timeout


def _convert_to_wavs(ffmpeg_exe, track_paths, tmp_dir, config):
    """Decode each track to WAV in tmp_dir, keeping its own channel layout,
    sample rate and bit depth (no upmix/resample/truncation — the WAV must be
    the source audio).

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
        codec, reason = _transport_codec(src)
        if codec is None:
            return (src, f"cannot transport to WAV without changing the "
                         f"samples ({reason})")
        proc = run_tool(
            [ffmpeg_exe, "-v", "error", "-i", src, "-f", "wav", "-acodec", codec, dst],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            timeout=_wav_transport_timeout(src),
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


def _synthesized_cue(track_paths, name_map):
    """A one-track-per-file cue for `track_paths`, in the order given.

    CUETools builds the disc's TOC from a cue sheet, so when the album has
    none — or has one that names audio this disc does not hold — the track
    order is the only TOC evidence there is, and this cue is that order.
    """
    lines = []
    for idx, tp in enumerate(track_paths, 1):
        base = os.path.basename(tp)
        wav = name_map.get(base.lower(), os.path.splitext(base)[0] + ".wav")
        lines.append(f'FILE "{wav}" WAVE')
        lines.append(f'  TRACK {idx:02d} AUDIO')
        lines.append('    INDEX 01 00:00:00')
    return "\n".join(lines) + "\n"


def _cue_unresolved_refs(cue_text, folder):
    """The FILE references in `cue_text` that name no file in `folder`."""
    unresolved = []
    for line in cue_text.splitlines():
        m = CUE_FILE_RE.match(line.rstrip("\n"))
        if not m:
            continue
        ref = m.group(1)
        base = ref.replace("/", "\\").split("\\")[-1]
        if not base or not os.path.isfile(os.path.join(folder, base)):
            unresolved.append(base or ref)
    return unresolved


def _generate_via_cuetools(ffmpeg_exe, arcue_exe, album_dir, disc_num, track_paths, cue_path, config):
    """Generate the CUETools verification log for one disc via ArCueDotNet.

    Uses a temp dir with WAVs + patched cue, invokes ArCueDotNet -v, captures
    the verbose log.  Returns the raw log text (as CUETools emitted it).
    """
    tmp_dir = tempfile.mkdtemp(prefix="mlo_accurip_")
    try:
        # Decode to WAVs
        name_map = _convert_to_wavs(ffmpeg_exe, track_paths, tmp_dir, config)

        # The cue drives the TOC, so the order it is built from must be the
        # disc's track order, not the caller's listdir order.
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

        patched = None
        if cue_path and os.path.isfile(cue_path):
            raw_cue = open(cue_path, "r", encoding="utf-8", errors="replace").read()
            patched = _patched_cue_for_temp(raw_cue, name_map)
            unresolved = _cue_unresolved_refs(patched, tmp_dir)
            if unresolved and len(name_map) == 1:
                # A one-file disc: every FILE line can only mean that file,
                # and the cue's TRACK/INDEX lines are the disc's layout, which
                # no synthesized cue can recover. Repoint instead of discarding.
                patched = _cue_all_files_to(patched, next(iter(name_map.values())))
                unresolved = _cue_unresolved_refs(patched, tmp_dir)
            if unresolved:
                # A cue that names audio this disc does not hold — a stale
                # name left by a rename, or one album-wide cue on a multi-disc
                # set — leaves ArCueDotNet hunting for files that are not
                # there. Its fallback is to guess among the folder's audio,
                # and it gives up ("unable to locate the audio files") as soon
                # as the folder holds more files than the cue names. The temp
                # WAVs are the audio this disc does have, so build from them.
                log(c(f"  disc {disc_num}: cue {os.path.basename(cue_path)} names "
                      f"{len(unresolved)} file(s) that are not on this disc "
                      f"({', '.join(unresolved[:2])}) – synthesizing minimal cue",
                      Color.YELLOW))
                patched = None
        if patched is None:
            patched = _synthesized_cue(track_paths, name_map)

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
# The table header. The words are stable across CUETools versions, the
# spacing is not: 2.1.4+ prints ``Track   [  CRC   |   V2   ] Status`` and
# 2.0.9/2.1.2 prints ``Track [ CRC ] Status``, so only the words are matched.
_AR_TABLE_RE = re.compile(r"^\s*Track\s*\[[^\]]*\]\s*Status\s*$", re.MULTILINE)
# One row of that table: the track number, its CRC (V2 is absent before
# 2.1.4, so the second CRC is optional), the confidence in parentheses and
# the verdict. The confidence is optional too — the verdict is what is
# classified, and a row with no confidence still says one.
_TRACK_AR_RE = re.compile(
    r"^\s*0*(\d+)\s+\[\s*[0-9a-fA-F]{8}\s*(?:\|\s*[0-9a-fA-F]{8}\s*)?\]"
    r"(?:\s*\(([^)]*)\))?\s*(.*)$"
)
# A row of the EAC-style table at the end of the log: peak, CRC32 of the
# decoded audio, then the same CRC with null samples excluded. The ``--``
# row is the whole disc, the numbered ones are the tracks.
_TRACK_CRC_RE = re.compile(r"^\s*(--|\d+)\s+[\d.]+\s+\[\s*([0-9a-fA-F]{8})\s*\]")


def parse_accurip_track_crcs(text):
    """{track number: CRC32} from the EAC-style table of a CUETools log.

    That CRC is the CRC-32 of the track's decoded 16-bit PCM — the value
    ``mlo.discs._audio_crc32`` recomputes from the audio on disk. It is the
    only content in an .accurip that ties the file to one disc's audio, which
    is what a rename of a file with no disc number in its name has to go on.
    The ``--`` (whole disc) row is not a track and is left out.
    """
    crcs = {}
    for line in (text or "").splitlines():
        m = _TRACK_CRC_RE.match(line)
        if not m or m.group(1) == "--":
            continue
        crcs[int(m.group(1))] = m.group(2).upper()
    return crcs


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
      `` 01     [aaaaaaaa] (V/Y) Accurately ripped`` (2.0.9/2.1.2: no V2 column,
      and the header is then ``Track [ CRC ] Status``) – both layouts are read,
      and a row whose confidence is missing is read too
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
        return ("NONE", "no AccurateRip ID")
    start = m_id.end()
    # A header this parser does not recognise must not lose the table: the
    # rows are self-describing, so the block falls back to everything the ID
    # line introduces and the terminators below still close it.
    header = _AR_TABLE_RE.search(text, start)
    block_start = header.start() if header else start
    end_markers = ["Offsetted by", "Track Peak", "[CTDB TOCID"]
    block_end = len(text)
    for marker in end_markers[0:2]:
        idx = text.find(marker, block_start + 1)
        if idx != -1 and idx < block_end:
            block_end = idx
    block = text[block_start:block_end]
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
            status = m.group(3).strip().lower()
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
        # The AR ID line ends with ``disk not present in database.`` when the
        # disc is not in the database at all, and that sentence — not the
        # missing table — is the reason there is nothing to score.
        if "not present in database" in low_block:
            return ("NONE", "AccurateRip disk not present in database")
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

    The table header is matched by its words, not its column spacing: a log
    whose layout this parser has not seen keeps its per-track verdicts instead
    of collapsing to the album-level answer. Rows are still read after the
    AccurateRip ID line, so CTDB statuses (a different section, and a
    different database) are never mistaken for AccurateRip ones.

    If the log has no AccurateRip ID or no parsable primary block, returns {}.
    Offsetted by ... blocks are ignored (alternate pressings per spec).
    """
    if not text or "[CUETools log;" not in text:
        return {}
    m_id = _AR_ID_RE.search(text)
    if not m_id:
        return {}
    start = m_id.end()
    header = _AR_TABLE_RE.search(text, start)
    block_start = header.start() if header else start
    block_end = len(text)
    for marker in ("Offsetted by", "Track Peak"):
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
        status_raw = m.group(3).strip().lower()
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
      newline byte, matching ``.cue`` default. When True the text ends with exactly
      one LF — never a second one, so canonicalising an already-canonical file is a
      no-op (grading compares the stored file with this very function).
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
    # Only when the text does not already end with one: with
    # keep_empty_accurip_lines on, the log's own trailing empty line is kept,
    # and appending a second LF to it grew the file by a line on EVERY pass —
    # script 10 rewrote it forever and grading, which compares the stored bytes
    # with exactly this function, could never accept it.
    if result and append_final_newline and not result.endswith("\n"):
        result += "\n"
    return result


def resolve_arcue_exe(tools=None):
    """The ArCueDotNet / CUETools.ARCUE executable, or None when absent.

    Shared with the audit: a missing generator is the one honest reason an
    album has no .accurip, and the audit must not read "no tool could make
    one" as "the rip did not match the database".
    """
    if tools is None:
        from .tools import detect_all_tools
        tools = detect_all_tools()
    cuetools = tools.get("cuetools") or {}
    exe = cuetools.get("arcue_exe")
    if exe and os.path.isfile(exe):
        return exe
    # Direct exe paths for both 2.1.6 (ArCueDotNet) and 2.2.6 (CUETools.ARCUE).
    d = cuetools.get("dir") or ""
    for cand_name in ("CUETools.ARCUE.exe", "ArCueDotNet.exe"):
        cand = os.path.join(d, cand_name) if d else ""
        if cand and os.path.isfile(cand):
            return cand
    # Last resort: any *arcue*.exe in the tool folder.
    try:
        if d and os.path.isdir(d):
            for entry in sorted(os.listdir(d)):
                low = entry.lower()
                if "arcue" in low and low.endswith(".exe"):
                    cand = os.path.join(d, entry)
                    if os.path.isfile(cand):
                        return cand
    except OSError:
        pass
    return None


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

    _IDENTITIES.clear()
    _IDENTITIES.update(_load_identities(config))

    if not os.path.isdir(folder):
        log(c(f"ERROR: folder does not exist: {folder}", Color.RED))
        return stats

    from .tools import detect_all_tools
    tools = detect_all_tools()
    ffmpeg_exe = (tools.get("ffmpeg") or {}).get("ffmpeg_exe")
    if ffmpeg_exe and not os.path.isfile(ffmpeg_exe):
        ffmpeg_exe = None
    cuetools = tools.get("cuetools") or {}
    arcue_exe = resolve_arcue_exe(tools)

    # Both tools are installed from the same page, so a run missing both names
    # both: reporting only the first made the user install ffmpeg, run the
    # script again, and only then learn CUETools was missing too.
    missing = []
    if not ffmpeg_exe:
        missing.append(("ffmpeg", "decodes each track to the WAV that CUETools "
                                  "verifies", ".dependencies/ffmpeg v*/ffmpeg.exe"))
    if not arcue_exe:
        missing.append(("CUETools", "computes the track CRCs and queries the "
                                    "AccurateRip database",
                        ".dependencies/CUETools v*/CUETools.ARCUE.exe"))
    if missing:
        log(c(f"ERROR: AccurateRip cannot run — {len(missing)} required tool(s) "
              f"missing: " + ", ".join(name for name, _w, _d in missing), Color.RED))
        for name, why, where in missing:
            log(c(f"  {name} ({where}) — {why}", Color.RED))
        log(c("Install " + " and ".join(f"'{n}'" for n, _w, _d in missing) +
              " from the Dependencies page (Settings → Dependencies), then run "
              "AccurateRip again.", Color.YELLOW))
        stats["error_count"] += 1
        stats["errors"].append(("AccurateRip",
                                "missing tool(s): " + ", ".join(n for n, _w, _d in missing)))
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
                    # Compared through the canonical spelling: a library
                    # another tagger wrote as "cd" names the same medium, and
                    # mlo.tagtext is the one rule for what "CD" means.
                    if canonical_text("MEDIA", af.get_tag("MEDIA")) == "CD":
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
            # Single-disc fallback: an album whose tracks carry no D-TT prefix
            # is one disc. It used to need a .log or a .cue to qualify, which
            # skipped exactly the albums whose sidecars are missing — the ones
            # this pass can still verify, because a cue is synthesized from
            # the track order when there is none.
            try:
                aud = [os.path.join(album_dir, f) for f in os.listdir(album_dir) if is_audio_file(f)]
                if aud:
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
            # What this disc's audio IS right now: the identity a generated
            # .accurip is recorded against, and what the freshness test below
            # compares. "" for any track means the disc cannot be answered for
            # this way (see the fallback in the test).
            disc_key = os.path.normcase(os.path.abspath(accurip_path))
            disc_ids = {tp: _audio_identity(tp) for tp in track_paths}
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
                        # The .accurip describes the AUDIO it was generated
                        # from: a re-rip (or a re-encode) of any track makes
                        # the stored verdict stale, and its text markers still
                        # look perfectly current. That is checked against each
                        # track's own audio identity, NOT its mtime — a tag
                        # write moves the mtime without touching the audio,
                        # and the scripts after this one in a run write tags,
                        # so the mtime rule re-decoded and re-verified every
                        # disc on every run. A track whose identity cannot be
                        # read (a container without one) keeps the mtime rule.
                        if disc_ids and all(disc_ids.values()):
                            if _IDENTITIES.get(disc_key) != disc_ids:
                                needs_regen = True
                        else:
                            newest_track = 0.0
                            for tp in track_paths:
                                try:
                                    newest_track = max(newest_track, os.path.getmtime(tp))
                                except OSError:
                                    pass
                            try:
                                if newest_track > os.path.getmtime(accurip_path) + 1.0:
                                    needs_regen = True
                            except OSError:
                                pass
                        if not needs_regen:
                            stats["skipped_count"] += 1
                            continue
                except OSError:
                    pass
                # Empty, or not a correctly formatted CUETools log we can
                # trust: regenerate it through CUETools.

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

            # The disc's track order is applied inside _generate_via_cuetools,
            # where the cue that carries it is built.
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
                fsync_dir(album_dir)
                if disc_ids and all(disc_ids.values()):
                    # What this file was verified against, for the next run's
                    # freshness test — the audio, not the container's mtime.
                    _IDENTITIES[disc_key] = dict(disc_ids)
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
    _save_identities(config, _IDENTITIES)
    return stats
