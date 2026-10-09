"""Multi-CD support: disc mapping, deterministic CD-N naming and
per-disc rip-log scoring.

Discs are identified from the track filename convention "D-TT Title"
(e.g. "2-03 Track.flac" -> disc 2) which this library uses throughout.
Logs and cues are renamed to CD-1.log / CD-2.cue ... using only
content-derived evidence, never order or fuzzy matching:

  cues   - the FILE entries inside a cue reference exact track
           filenames, so the referenced disc is exact.
  logs   - in order of preference:
             1. an explicit disc number in the current filename
                (CD-2.log, "Disc 2.log", "2 - Album.log"),
             2. the trivial single-disc case (one disc, one log),
             3. a unique total-duration match between the log's TOC
                (EAC prints per-track lengths in CD sectors) and the
                actual audio durations of exactly one disc.
           Anything ambiguous is left untouched - grading will flag the
           missing LOG_GRADE instead of guessing.

Per-disc rip-log scoring: OPSnet Logchecker (PHP) scores each log directly
via `php logchecker.phar analyze --no_text <log>` and returns the 0-100 score.
No stub audio needed — Logchecker parses the log text itself.
"""
import os
import re
import subprocess
import tempfile
import shutil
import threading

from .atomic import replace_locked
from .audio import AudioFile
from .config import should_write_audio_tag
from . import naming
from .paths import (AUDIO_EXTS, fsync_dir, expected_tracks_state,
                    load_expected_tracks)
from .stats import is_audio_file
from .subproc import run_tool
from .tagtext import is_cd_media

# Optional: EAC checksum verifier (pypi eac-logchecker)
try:
    import eac_logchecker  # type: ignore
    HAS_EAC_CHECKER = True
except ImportError:
    eac_logchecker = None  # type: ignore
    HAS_EAC_CHECKER = False

# "1-01 Title.flac" / "12-03 Title.flac" -> disc number
DISC_PREFIX_RE = re.compile(r"^(\d{1,2})\s*-\s*\d{2}(?:\s|\.|$)")

# Explicit disc numbers in log filenames: CD-2.log, CD2.log, Disc 02.log,
# "2 - Album.log", "(2).log" ...
LOG_NAME_DISC_RE = re.compile(
    r"(?:^|[\s_(-])(?:cd|disc)[\s_-]?(\d{1,2})(?=$|[\s._)\]-])"
    r"|^(\d{1,2})\s*[-._\s]"
    r"|(?:^|\s)\((\d{1,2})\)(?=$|[\s._)\]-])",
    re.IGNORECASE,
)

CUE_FILE_RE = re.compile(r'^\s*FILE\s+"([^"]+)"', re.IGNORECASE)

# Cue sheet structure: "  TRACK 01 AUDIO" and "    INDEX 01 03:45:60"
# (mm:ss:ff, 75 frames per second).
CUE_TRACK_RE = re.compile(r"^\s*TRACK\s+\d{1,2}\b", re.MULTILINE | re.IGNORECASE)
CUE_INDEX_RE = re.compile(
    r"^\s*INDEX\s+01\s+(\d{1,3}):(\d{2}):(\d{2})", re.MULTILINE | re.IGNORECASE
)

# A TOC playtime, in CD sectors (75/s). The FRAMES separator is the ripper's
# own spelling: EAC writes "0:00.00" / "3:13.27" (mm:ss.ff), XLD writes
# "00:00:00" / "03:41:34" (mm:ss:ff). Reading only the EAC dot left every XLD
# log with NO TOC at all — its total-duration disc match (rename_logs_for_discs,
# the audit's orphan mapping), its per-track playtimes and the playtime guard
# log_crc_map keeps were all silently blind. Both separators are the same time.
_TOC_TIME = r"\d+:\d{2}[.:]\d{2}"

# EAC TOC rows: "     1  |  0:00.00  |  3:13.27  | ..." (length column),
# XLD TOC rows: "     1  | 00:00:00 | 03:41:34 | ..." (length column).
TOC_ROW_RE = re.compile(
    rf"^\s*\d+\s*\|\s*{_TOC_TIME}\s*\|\s*(\d+):(\d{{2}})[.:](\d{{2}})\s*\|",
    re.MULTILINE,
)

# Per-track CRC-32 checksums in rip logs (hex, 8 digits):
#   EAC: "Test CRC 3F2A51A2" / "Copy CRC 3F2A51A2" / "Accurately ripped
#        (confidence 10)  [3F2A51A2]"
#   XLD: "CRC32 hash : 3F2A51A2" — the decoded PCM's CRC-32, EAC's "Copy CRC" —
#        written a second time as "CRC32 hash (test run) : 3F2A51A2" and a
#        third, DIFFERENT "CRC32 hash (skip zero) : ..." that is never read
#        (it skips zero samples, so it describes no file on disk).
COPY_CRC_RE = re.compile(r"^Copy CRC\s+([0-9A-Fa-f]{8})")
TEST_CRC_RE = re.compile(r"^Test CRC\s+([0-9A-Fa-f]{8})")
XLD_CRC_RE = re.compile(r"^CRC32 hash\s*:\s*([0-9A-Fa-f]{8})")
XLD_TEST_CRC_RE = re.compile(r"^CRC32 hash\s+\(test run\)\s*:\s*([0-9A-Fa-f]{8})")
ACCURATE_CRC_RE = re.compile(r"\[([0-9A-Fa-f]{8})\]")

# A cue sheet's own tracklist: "  TRACK 01 AUDIO", the TITLE/PERFORMER lines
# that belong to it, and the FILE line that names the audio it describes. The
# TOC of an EAC/XLD log states the same running order for a rip whose cue is
# gone: "    1  |  0:00.00  |  3:13.27  | ..." (track, start, LENGTH). Both are
# read with the sheet/log readers below so a single song imported out of a rip
# can be placed on the album's real tracklist (mlo.discs.sidecar_tracklist).
CUE_TRACK_LINE_RE = re.compile(r"^\s*TRACK\s+(\d{1,3})\s+\S+", re.IGNORECASE)
CUE_TITLE_LINE_RE = re.compile(r'^\s*TITLE\s+"?([^"\r\n]*?)"?\s*$', re.IGNORECASE)
TOC_TRACK_ROW_RE = re.compile(
    rf"^\s*(\d{{1,3}})\s*\|\s*{_TOC_TIME}\s*\|\s*(\d+):(\d{{2}})[.:](\d{{2}})\s*\|",
    re.MULTILINE,
)
LOG_FILENAME_RE = re.compile(r"^\s*Filename\s+(.+?)\s*$", re.IGNORECASE)

# Containers whose decoded PCM can be the WAV an EAC/XLD log's CRC was taken
# from. Anything else (mp3/m4a/ogg/opus/aac) is lossy: it can never decode to
# those samples, so the .log CRC must not be compared for those files.
LOSSLESS_CRC_EXTS = (".flac", ".wav", ".alac", ".aiff", ".aif")


# Duration-match window in seconds and the uniqueness margin required
# before a TOC match is trusted.
TOC_TOLERANCE_S = 4.0
TOC_UNIQUE_MARGIN_S = 4.0


# --------------------------------------------------------------------------- #
# ONE disc vocabulary
# --------------------------------------------------------------------------- #
# Every reading of "which disc does this name state" lives here: the search's
# candidate trees, the importer's mover and the layout scan all call these, so
# a folder one of them takes for a disc cannot be an album to another (which is
# how one multi-disc release used to import as two albums named after its
# discs). Three near-identical local regex sets used to spell this, each
# slightly different — never again; add a spelling HERE.

# The decoration a share wraps its folder names in: "CD1 [FLAC]" -> "CD1",
# "Album (1994)" -> "Album".
DECOR_RE = re.compile(r"[\s._-]*[\(\[]([^\)\]]*)[\)\]][\s._-]*$")

# A disc WORD plus its index: "CD1", "CD 1", "CD-1", "CD_1", "Disc 2",
# "Disk1", "Volume 1", "DVD 2", "BD1" — the spelling a rip's own folder uses.
DISC_DIR_WORD_RE = re.compile(r"(?:cd|disc|disk|dvd|bd|volume|vol)[\s._-]?\d{1,2}",
                              re.IGNORECASE)
# ...and the bare index a plain rip numbers its folders with: "1", "02".
DISC_DIR_INDEX_RE = re.compile(r"\d{1,2}")

# The loose token a FILE (or folder) name can state a disc with, anywhere in
# the name: "CD1", "Disc 2 [FLAC]", "Album CD2".
DISC_TOKEN_RE = re.compile(r"(?:^|[^A-Za-z0-9])(?:cd|disc|disk|dvd|bd|volume|vol)"
                           r"\s*[-_.]?\s*(\d{1,2})(?![0-9])", re.IGNORECASE)
# "1-03 Title": the library's own D-TT convention — the first number names the
# disc, and only when a track number follows it ("12 - Title" is a track, not
# disc 12).
DISC_LEAD_RE = re.compile(r"^(\d{1,2})\s*-\s*\d{1,3}(?:\D|$)")
# "(2) 01 - Title": the disc in brackets in front of the track number.
DISC_BRACKET_RE = re.compile(r"^[\(\[]\s*(\d{1,2})\s*[\)\]]\s*")


def strip_decoration(name):
    """"CD1 [FLAC]" -> "CD1"; "Album (1994)" -> "Album"."""
    name = str(name or "")
    while True:
        m = DECOR_RE.search(name)
        if not m:
            return name.strip()
        name = name[:m.start()]


def disc_number_of_name(name):
    """Disc number a folder or FILE name claims ("CD1", "Disc 2 [FLAC]",
    "1-03 rip", "(2) 01 x"), or None when the name says nothing.

    The loose reading, for names that carry the disc as a token: a file name
    ("…/CD2/01 - x.flac", "1-03 x.flac") and a folder that states a disc in
    words. A BARE index is deliberately not a disc here — "1.flac" is a track
    name, not a statement that the track is disc 1, and a folder called "25"
    may be the album — that spelling counts only with its siblings' evidence
    (see :func:`is_disc_parent` and :func:`disc_number_of_path`)."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    stem = strip_decoration(os.path.splitext(base)[0] or base)
    m = DISC_TOKEN_RE.search(stem)
    if m:
        return _positive(int(m.group(1)))
    m = DISC_LEAD_RE.match(stem)
    if m:
        return _positive(int(m.group(1)))
    m = DISC_BRACKET_RE.match(stem)
    return _positive(int(m.group(1))) if m else None


def bare_disc_index(name):
    """The disc a name states when the WHOLE name is its index — "1", "02" —
    else None.

    A bare index is weak evidence on its own: a rip that numbers its folders
    "1"/"2" states its discs that way, and so does a folder holding the album
    "25". It is read as a disc only where the sibling folders settle it
    (:func:`is_disc_parent`, :func:`disc_number_of_path`)."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    if not DISC_DIR_INDEX_RE.fullmatch(base):
        return None
    return _positive(int(base))


def disc_dir_number(name):
    """The disc a DIRECTORY name states, or None when the folder is not a disc
    folder of its parent.

    The ONE rule the search's candidate tree, the importer's mover and the
    readiness check ask, for a folder that names its disc: "CD1", "CD 1",
    "CD-1", "Disk1", "Disc 2", "Volume 1", "DVD 2", "CD1 [FLAC]" (the share's
    decoration is stripped), plus the loose spellings disc_number_of_name
    reads. Anything else — "Album [FLAC]", "Extras", "Log+Cue", "Scans" — is
    not a disc folder whatever else it may be. The bare index ("1", "02") is
    answered by :func:`bare_disc_index`, which needs the siblings to count."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    bare = strip_decoration(base)
    if not bare:
        return None
    if DISC_DIR_WORD_RE.fullmatch(bare):
        m = re.search(r"\d{1,2}$", bare)
        return _positive(int(m.group(0))) if m else None
    if DISC_DIR_INDEX_RE.fullmatch(bare):
        return None                    # bare index: see bare_disc_index
    return disc_number_of_name(bare)


def is_disc_dir(name):
    """True when a directory name IS a disc folder of its parent — the
    strict/loose split lives in :func:`disc_dir_number`, the one rule."""
    return disc_dir_number(name) is not None


def is_disc_parent(child_names):
    """Whether a folder's album-bearing CHILD folders make it the parent of a
    multi-disc release — ONE album, its discs below it — or not.

    This is the rule the staging walk, the mover and the readiness check share
    (server.soulseek.disc_parent), stated once here:

      * a child that NAMES a disc — "CD1", "Disc 2", "Volume 1", "1-03 rip",
        "CD1 [FLAC]" — makes the parent the album, and ANY other album-bearing
        child rides along instead of splitting off as an album of its own (a
        stray log/cue folder, an artwork or bonus folder). The album's own
        files at the root win over this rule anyway: a folder holding an album
        file directly is an album;
      * a child that is only a BARE index ("1", "02") counts when the children
        are a numbered set — "1"+"2" is a rip's disc split, while a lone "25"
        is the album "25" and nothing else;
      * no child names a disc -> not a disc parent: the children are albums of
        their own (or the peer's own folder levels, which the walk descends).

    `child_names` are the names of the children that carry album material
    (.log/.cue/audio), as the caller has already established."""
    names = [str(n) for n in child_names or []]
    if any(disc_dir_number(n) for n in names):
        return True
    numbers = sorted(n for n in (bare_disc_index(c) for c in names) if n)
    # a numbered SET, not two unrelated folders that happen to be numbers: the
    # run must start at 1 and have no holes ("1","2","3" is a rip, "25","30"
    # is two albums).
    return len(numbers) >= 2 and numbers == list(range(1, len(numbers) + 1))


def disc_number_of_path(path):
    """The disc a FILE on disk belongs to, or None when nothing states one.

    The file's own name first, then the folder it sits in — including a folder
    that is only a bare index, which counts when its parent is a numbered set
    of discs (:func:`is_disc_parent`). The parent is only READ when the folder
    is a bare index, so this costs no directory listing for the ordinary
    "…/CD2/01 - x.flac" and "…/Album/01 - x.flac" shapes."""
    p = str(path).replace("\\", "/")
    own = disc_number_of_name(os.path.basename(p))
    if own:
        return own
    parent = os.path.dirname(p)
    stated = disc_dir_number(os.path.basename(parent))
    if stated:
        return stated
    if bare_disc_index(os.path.basename(parent)):
        grand = os.path.dirname(parent)
        try:
            siblings = os.listdir(grand) if os.path.isdir(grand) else []
        except OSError:
            siblings = []
        if is_disc_parent(siblings):
            return bare_disc_index(os.path.basename(parent))
    return None


def _positive(value):
    """A disc number is 1 or more ("CD0" states nothing)."""
    return int(value) if int(value) >= 1 else None


def disc_of_filename(name):
    """Disc number from the 'D-TT Title' filename convention, else None."""
    m = DISC_PREFIX_RE.match(os.path.basename(name))
    return int(m.group(1)) if m else None


def album_discs(album_dir):
    """Map {disc number: [audio file paths]} for an album folder.

    Only folders where every audio file carries the D-TT convention are
    returned; anything else has no reliable disc structure ({}).

    Only the CD-audio extensions count (paths.AUDIO_EXTS, the set the audit
    and the AccurateRip generator walk): mlo.stats.is_audio_file also accepts
    music-video containers, and one stray .mkv without the D-TT prefix used to
    void the disc mapping of the whole album — every per-disc gate then fell
    back to guessing.
    """
    discs = {}
    for f in sorted(os.listdir(album_dir)):
        if not f.lower().endswith(AUDIO_EXTS):
            continue
        d = disc_of_filename(f)
        if d is None or d < 1:
            return {}
        discs.setdefault(d, []).append(os.path.join(album_dir, f))
    return discs


def read_log_text(path):
    """Decode an EAC/XLD log (UTF-16LE with BOM, or UTF-8)."""
    try:
        raw = open(path, "rb").read()
    except OSError:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if b"\x00" in raw[:512]:
        # NUL-byte Heuristic: UTF-16 without BOM.
        try:
            return raw.decode("utf-16-le", errors="replace")
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


def parse_log_toc_seconds(text):
    """Total playtime of the log's 'TOC of the extracted CD' table, in
    seconds (CD sectors are 1/75 s)."""
    total = 0.0
    for m in TOC_ROW_RE.finditer(text):
        mins, secs, frames = int(m.group(1)), int(m.group(2)), int(m.group(3))
        total += mins * 60 + secs + frames / 75.0
    return total


def parse_log_checksums(text):
    """Map track number -> CRC-32 hex (8 chars, uppercase) from a rip log.

    Walks the "Track  N" sections — EAC writes "Track  1", XLD "Track 01".

    The EXTRACTION's CRC outranks a test run's, because that is the one that
    describes the bytes on disk: EAC's "Copy CRC" over its "Test CRC", and
    XLD's plain `CRC32 hash` over `CRC32 hash (test run)`. XLD writes the test
    run FIRST, so at equal priority a track whose first read had to be
    re-read (the CRCs then differ) was compared by the failed pass's value
    instead of the audio's. The priorities are Copy CRC 4, EAC Test CRC 3,
    XLD's plain CRC 2, XLD test run 1, an AccurateRip bracket 0. `CRC32 hash
    (skip zero)` matches NONE of them: it skips zero samples, so it is a
    different number and describes no file.
    """
    per_track = {}
    priority = {}  # track -> priority level
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"^Track\s+(\d{1,3})\b", line, re.IGNORECASE)
        if m:
            current = int(m.group(1))
            continue
        if current is None:
            continue
        m = COPY_CRC_RE.match(line)
        if m:
            # Copy is the extraction's own CRC: highest priority 4
            if priority.get(current, -1) < 4:
                per_track[current] = m.group(1).upper()
                priority[current] = 4
            continue
        m = TEST_CRC_RE.match(line)
        if m:
            if priority.get(current, -1) < 3:
                per_track[current] = m.group(1).upper()
                priority[current] = 3
            continue
        m = XLD_CRC_RE.match(line)
        if m:
            # XLD's extraction CRC: outranks its own test run (below)
            if priority.get(current, -1) < 2:
                per_track[current] = m.group(1).upper()
                priority[current] = 2
            continue
        m = XLD_TEST_CRC_RE.match(line)
        if m:
            if priority.get(current, -1) < 1:
                per_track[current] = m.group(1).upper()
                priority[current] = 1
            continue
        if "accurately" in line.lower():
            m = ACCURATE_CRC_RE.search(line)
            if m:
                if priority.get(current, -1) < 0:
                    per_track[current] = m.group(1).upper()
                    priority[current] = 0
    return per_track


# --------------------------------------------------------------------------- #
# What a rip's own sheets say about its tracklist
#
# A single song imported out of a CD rip brings its sheets with it (.cue, .log,
# .accurip). Those know what the file alone cannot: which album the track is
# part of, which position it occupies, and whether its samples are the ones
# that were ripped. The readers below turn them into the running order the
# import records as .mlo_expected.json — the same rows the wizard writes from
# a MusicBrainz release, so a partial album reads as partial either way.
# --------------------------------------------------------------------------- #

def read_cue_text(path):
    """Decode a cue sheet (utf-8-sig, then latin-1 — an EAC ANSI sheet must
    not come back as replacement characters)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _sheet_disc(name):
    """The disc a sheet's own file name claims (CD-2.cue / "2 - Album.log"),
    else 1 — a sheet that names no disc describes disc 1."""
    return _log_name_disc(os.path.basename(name)) or 1


def cue_track_rows(path, disc=None):
    """The tracklist a cue sheet states: [{disc, position, title, file}].

    `file` is the sheet's own FILE reference for that track — the evidence
    that places an imported file on the tracklist (a per-track rip writes one
    FILE per track; an image rip writes one FILE for the whole disc, and then
    only the track number or the playtime can place a file). A track the sheet
    gives no TITLE of its own keeps an empty one rather than the sheet's file
    name: the album's title is not the track's.
    """
    text = read_cue_text(path)
    rows = []
    current_file = ""
    for line in text.splitlines():
        m = CUE_FILE_RE.match(line)
        if m:
            current_file = m.group(1)
            continue
        m = CUE_TRACK_LINE_RE.match(line)
        if m:
            rows.append({"disc": disc or 1, "position": int(m.group(1)),
                         "title": "", "file": current_file})
            continue
        m = CUE_TITLE_LINE_RE.match(line)
        if m and rows and not rows[-1]["title"]:
            # A TITLE after a TRACK line is that track's title; one before any
            # TRACK line is the album's, which belongs to no row.
            rows[-1]["title"] = m.group(1).strip()
    # A FILE a single row claims names that row's file, so its stem is the
    # track's name when the sheet states no title.
    seen = {}
    for r in rows:
        if r["file"]:
            seen[r["file"]] = seen.get(r["file"], 0) + 1
    for r in rows:
        if not r["title"] and r["file"] and seen[r["file"]] == 1:
            r["title"] = os.path.splitext(os.path.basename(r["file"]))[0]
    return rows


def parse_log_track_seconds(text):
    """Track number -> playtime in seconds, from the log's TOC table."""
    out = {}
    for m in TOC_TRACK_ROW_RE.finditer(text):
        try:
            out[int(m.group(1))] = (int(m.group(2)) * 60 + int(m.group(3))
                                    + int(m.group(4)) / 75.0)
        except ValueError:
            continue
    return out


def parse_log_track_files(text):
    """Track number -> audio file name, from a log's per-track Filename
    lines.

    Both rippers write them; the path is the RIPPER's own (XLD names the Mac
    folder it ripped to, EAC its Windows one), so the name only places a local
    file when the library kept it. `mlo.paths._same_name` (via `_norm_name`)
    decides that, and the per-track CRC pairing never uses this at all — it
    matches by number, then by order (`log_crc_map`)."""
    out = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"^Track\s+(\d{1,3})\b", line, re.IGNORECASE)
        if m:
            current = int(m.group(1))
            continue
        if current is None:
            continue
        m = LOG_FILENAME_RE.match(line)
        if m:
            out[current] = os.path.basename(m.group(1).replace("\\", "/"))
    return out


def log_track_rows(text, disc=1):
    """The tracklist a rip log states: [{disc, position, title, file}].

    A log has no titles, so `title` is the file's stem when the log names the
    file and empty otherwise. The running order and the playtimes come from
    its TOC ("the log's own evidence", the same table
    `parse_log_toc_seconds` totals), never from the order the folder happens
    to list.
    """
    seconds = parse_log_track_seconds(text)
    files = parse_log_track_files(text)
    numbers = sorted(set(seconds) | set(files) | set(parse_log_checksums(text)))
    rows = []
    for n in numbers:
        ref = files.get(n, "")
        rows.append({"disc": disc, "position": n, "title": "",
                     "file": ref, "seconds": seconds.get(n)})
    for r in rows:
        if not r["title"] and r["file"]:
            r["title"] = os.path.splitext(r["file"])[0]
    return rows


def sidecar_tracklist(album_dir):
    """The album's own tracklist, read from its .cue and .log sheets.

    Returns {"source": "cue"|"log"|None, "cue": path|None, "log": path|None,
    "rows": [{disc, position, title, file, seconds}]} — the cue's tracklist
    when one is there (it is the sheet that names titles AND files), the log's
    TOC otherwise. Both are needed: an imported rip may ship only one of them,
    and a partial album must still be able to state what it is missing.
    """
    try:
        names = sorted(os.listdir(album_dir))
    except OSError:
        return {"source": None, "cue": None, "log": None, "rows": []}
    cues = [f for f in names if f.lower().endswith(".cue")]
    logs = [f for f in names if f.lower().endswith(".log")]
    rows = []
    cue = None
    used_log = None
    for f in cues:
        path = os.path.join(album_dir, f)
        got = cue_track_rows(path, disc=_sheet_disc(f))
        if not got:
            continue
        rows.extend(got)
        cue = cue or path
    source = "cue" if rows else None
    if not rows:
        # No cue: the log's TOC is the only statement of the running order
        # left, and it names the disc it was taken from.
        for f in logs:
            path = os.path.join(album_dir, f)
            got = log_track_rows(read_log_text(path), disc=_sheet_disc(f))
            if not got:
                continue
            rows.extend(got)
            used_log = used_log or path
        source = "log" if rows else None
    rows.sort(key=lambda r: (r["disc"], r["position"]))
    return {"source": source, "cue": cue,
            "log": used_log or (os.path.join(album_dir, logs[0]) if logs else None),
            "rows": rows}


def match_disc_row(rows, audio_path, duration=None):
    """The tracklist row an audio file occupies, or None.

    Evidence in order of strength:
      1. the row names this file (a .cue FILE entry or a log's Filename —
         exact, and it is what a rip writes),
      2. the file's own track number (tag, then the 'NN'/'D-TT' prefix),
      3. a unique playtime match against the log's TOC within the same
         tolerance the disc mapping trusts.
    A row is only ever returned when the evidence is unambiguous: placing an
    imported track on the wrong row is worse than leaving it unplaced, which
    reads as "this track is not on the sheet" and is reported as such.
    """
    if not rows:
        return None
    for r in rows:
        if r.get("file") and _norm_name(r["file"]) == _norm_name(audio_path):
            return r
    tn = _file_track_number(audio_path)
    if tn is not None:
        same = [r for r in rows if int(r["position"]) == int(tn)]
        if len(same) == 1:
            return same[0]
    if duration is None:
        # The album module's own reader (it totals a disc's playtime for the
        # log mapping); a file it cannot measure is no evidence at all.
        duration = _audio_seconds([audio_path])
    if duration:
        close = [r for r in rows if r.get("seconds")
                 and abs(float(r["seconds"]) - float(duration)) <= TOC_TOLERANCE_S]
        if len(close) == 1:
            return close[0]
    return None


def disk_rows(album_dir, audio_paths=None):
    """The album's audio, one row per file: {"disc", "position", "title"}.

    The same walk the old keys helper made, keeping what the manifest's rows
    carry (a disc, a track number and a title) so the two can be compared — see
    `mlo.paths.expected_tracks_state`, which aligns a disc's rows against its
    files BY ORDER when the two are numbered in different conventions (a
    release numbered continuously 14-26 against a manifest numbered per disc
    1-13). A file whose TITLE is unreadable keeps "" rather than a guess.
    """
    if audio_paths is None:
        try:
            audio_paths = [os.path.join(album_dir, f) for f in os.listdir(album_dir)
                           if f.lower().endswith(AUDIO_EXTS)]
        except OSError:
            return []
    discs = album_discs(album_dir) or {}
    disc_of = {}
    for d, paths in discs.items():
        for p in paths:
            disc_of[os.path.normcase(os.path.abspath(p))] = d
    out = []
    for p in audio_paths:
        d = disc_of.get(os.path.normcase(os.path.abspath(p))) or disc_of_filename(p) or 1
        title = ""
        try:
            title = str(AudioFile(p).get_tag("TITLE") or "").strip()
        except Exception:
            pass
        out.append({"disc": int(d), "position": _file_track_number(p),
                    "title": title, "file": os.path.basename(p)})
    return out


def album_expected_state(album_dir, audio_paths=None):
    """How much of the album's recorded tracklist is on disk.

    Returns None when the folder carries no manifest (nothing was recorded, so
    nothing can be missing), else {"total", "present", "missing", "rows"} with
    each row flagged — the same rule the library page and the grader read
    (mlo.paths.expected_tracks_state).
    """
    from .paths import expected_tracks_state, load_expected_tracks
    tracks = load_expected_tracks(album_dir)["tracks"]
    if not tracks:
        return None
    rows_on_disk = disk_rows(album_dir, audio_paths)
    keys = {(r["disc"], int(r["position"])) for r in rows_on_disk
            if r["position"] is not None}
    names = [r["file"] for r in rows_on_disk]
    rows = expected_tracks_state(tracks, keys, names, rows_on_disk)
    present = sum(1 for r in rows if not r["missing"])
    return {"total": len(rows), "present": present,
            "missing": len(rows) - present, "rows": rows}


def _file_track_number(path):
    """Track number from the TRACKNUMBER tag, else the 'NN' filename prefix."""
    try:
        af = AudioFile(path)
        raw = str(af.get_tag("TRACKNUMBER") or "").strip()
        if raw.isdigit():
            return int(raw)
    except Exception:
        pass
    # Use _track_num_of for D-TT (1-01 -> 1) correctly returns TT
    tn = _track_num_of(path)
    if tn is not None:
        return tn
    m = re.match(r"^(\d{1,3})(?:\s*[-._\s])", os.path.basename(path))
    if m:
        return int(m.group(1))
    return None


def _file_seconds(path):
    """The file's own playtime in seconds, or None when unreadable.

    The guard the log-to-file ORDER alignment uses (`log_crc_map`): a log
    states a playtime per track in its TOC, and two rows that agree within
    `TOC_TOLERANCE_S` are the same track. Reads through `AudioFile`, which the
    tag cache already answers for a file the run has touched.
    """
    try:
        tech = getattr(AudioFile(path), "tech", None) or {}
        value = tech.get("length")
        return float(value) if value else None
    except Exception:
        return None


# PCM is fed to zlib.crc32 in chunks this size, so verifying a track never
# buffers more than one chunk of decoded audio. 64 KiB is also the read size
# a pipe serves fastest on Windows (a byte-mode read only completes once the
# full request is buffered, so asking for megabytes costs throughput).
_CRC_CHUNK = 1 << 16

# Decoded CRCs per (path, size, mtime_ns). Decoding a track is the most
# expensive thing this module does, and the audit pass and the grader ask the
# same question about the same unchanged file (grading a CD library would
# otherwise decode every track a second time). Keyed on size+mtime, so a
# re-ripped or edited file is never served a stale verdict. Bounded: a long
# session over a churning library drops the map rather than growing forever.
_CRC_MEMO = {}
# The same verdict keyed by the AUDIO's own identity instead of the file's
# stat, which is what a tag write changes and the audio is not. FLAC states one
# (STREAMINFO's MD5, the same identity `mlo.accurip` keys its .accurip evidence
# on: a tag rewrite leaves it, a re-rip or re-encode changes it), and a
# container that cannot state one simply never hits this map. Without it every
# script that wrote a tag — an import writes dozens — moved the mtime and made
# the next reader decode the whole track again with ffmpeg, which is the most
# expensive thing this module does.
_CRC_AUDIO_MEMO = {}
_CRC_MEMO_LOCK = threading.Lock()
_CRC_MEMO_MAX = 20000


def _audio_crc32(ffmpeg_exe, path):
    """CRC-32 of the file's decoded 16-bit PCM (the value EAC/XLD print in
    their logs), as 8 uppercase hex digits, or None on failure.

    The decoder's PCM is fed to zlib.crc32 in _CRC_CHUNK-sized pieces as it
    arrives, so a track's samples are never held in memory — a 60-minute disc
    used to cost hundreds of MB of RSS for its single largest track."""
    import zlib

    key = None
    identity = ""
    try:
        st = os.stat(path)
        norm = os.path.normcase(os.path.abspath(path))
        key = (norm, st.st_size, st.st_mtime_ns)
        with _CRC_MEMO_LOCK:
            if key in _CRC_MEMO:
                return _CRC_MEMO[key]
        # The stat key missed: the file changed in SOME way. Ask what it says
        # about its own audio before paying for a decode — a tag write is the
        # common case by far, and re-decoding a 40 MB track to learn what a
        # rewrite could not have touched is what made a graded import slow.
        from mlo.accurip import _audio_identity
        identity = _audio_identity(path)
        if identity:
            with _CRC_MEMO_LOCK:
                hit = _CRC_AUDIO_MEMO.get((norm, identity))
            if hit is not None:
                with _CRC_MEMO_LOCK:
                    _CRC_MEMO[key] = hit
                return hit
    except OSError:
        key = None

    crc = 0
    size = 0
    rd, wd = os.pipe()

    def pump():
        nonlocal crc, size
        with os.fdopen(rd, "rb", 0) as fh:
            while True:
                buf = fh.read(_CRC_CHUNK)
                if not buf:
                    break
                crc = zlib.crc32(buf, crc)
                size += len(buf)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        proc = run_tool(
            [ffmpeg_exe, "-v", "error", "-i", path,
             "-f", "s16le", "-acodec", "pcm_s16le", "-"],
            stdout=wd, stderr=subprocess.PIPE, timeout=600,
        )
    except Exception:
        proc = None
    finally:
        # Drop our own write end so the reader sees EOF once the child's
        # handle goes away (a timeout has already killed the child).
        os.close(wd)
    reader.join()
    if proc is None or proc.returncode != 0 or not size:
        return None
    got = format(crc & 0xFFFFFFFF, "08X")
    if key is not None:
        with _CRC_MEMO_LOCK:
            if len(_CRC_MEMO) >= _CRC_MEMO_MAX:
                _CRC_MEMO.clear()
            _CRC_MEMO[key] = got
            if identity:
                if len(_CRC_AUDIO_MEMO) >= _CRC_MEMO_MAX:
                    _CRC_AUDIO_MEMO.clear()
                _CRC_AUDIO_MEMO[(key[0], identity)] = got
    return got


def log_crc_map(crcs, paths, seconds=None, durations=None, number_of=None):
    """One disc's log checksums, keyed by the PATH each belongs to.

    A rip log numbers a DISC's tracks 1..N (EAC's own running order), and the
    files on that disc may be numbered RELEASE-wide — `2-14 … 2-26` for the
    second disc of a 2-CD set, which is what the CD's TOC states and what this
    app writes. The two then share no track number at all, and every track of
    that disc graded "not covered by .log CRC (unverifiable CD rip)" beside a
    log that covered every one of them (measured on the owner's The Wall, US
    CD C2K 36183: 13 files, 13 CRC rows, 13 "unverifiable").

    Matching is by track number first, and BY ORDER when the two share no
    number — the i-th log row against the i-th file — guarded so a renumbering
    is the only thing it can ever explain: the counts must be equal, every file
    must state a number, and wherever both sides state a playtime (`seconds`
    from the log's TOC, `durations` per path) the two must agree within
    `TOC_TOLERANCE_S`. Otherwise {} — a file the log does not really
    cover is not given somebody else's checksum.

    *number_of* is the caller's own rule for "which track is this file", so a
    caller that reads the position off the FILE NAME (`_track_num_of`, the
    grader's rule) and one that reads it off the TAG (`_file_track_number`,
    the verifier's) each keep what they had.
    """
    number_of = number_of or _file_track_number
    by_track = {}
    for k, v in (crcs or {}).items():
        try:
            track = int(k)
        except (TypeError, ValueError):
            continue
        if v:
            by_track[track] = v
    if not by_track or not paths:
        return {}
    numbers = {}
    for p in paths:
        tn = number_of(p)
        if tn is not None:
            numbers[p] = int(tn)
    if numbers and set(numbers.values()) & set(by_track):
        return {p: by_track[tn] for p, tn in numbers.items() if tn in by_track}
    if len(numbers) != len(paths):
        return {}                      # a file that states no position: no order
    log_rows = sorted(by_track)
    file_rows = sorted(numbers.items(), key=lambda kv: kv[1])
    if not file_rows or len(log_rows) != len(file_rows):
        return {}
    secs = {}
    for k, v in (seconds or {}).items():
        try:
            secs[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    if secs and durations:
        for track, (p, _tn) in zip(log_rows, file_rows):
            want = secs.get(track)
            got = durations.get(p)
            if want is None or got is None:
                continue
            try:
                if abs(want - float(got)) > TOC_TOLERANCE_S:
                    return {}
            except (TypeError, ValueError):
                continue
    return {p: by_track[track] for track, (p, _tn) in zip(log_rows, file_rows)}


def verify_album_checksums(ffmpeg_exe, album_dir, paths, config=None, workers=None):
    """Verify MEDIA=CD tracks against the CRC-32 checksums in the rip logs.

    This is the ONLY integrity source for CD rips — AudioAuditor is never
    consulted for MEDIA=CD (see audit.py). A file whose log checksum
    matches its actual decoded-PCM CRC is REAL, a mismatch is FAKE, and
    files whose log carries no usable checksum are reported as unverified
    (they get no AUDIT value, so grading fails the album instead of
    guessing). Lossy containers are always unverified: the log CRC covers
    the uncompressed WAV, which a lossy encode can never reproduce.

    Returns ({path: 'REAL'|'FAKE'}, {path: reason}) — verified verdicts
    first, then unverified files with the reason (no log / no checksum /
    lossy format / undecodable).
    """
    unverified = {}
    if not config or not config.get("audit_verify_cd_checksums", True):
        return {}, {}
    if not paths:
        return {}, {}
    # Check MEDIA across all paths, not just first file order
    is_cd = False
    for pp in paths:
        try:
            af2 = AudioFile(pp)
            # mlo.tagtext.is_cd_media is the one rule for "is this medium a
            # CD": "cd" is the same medium as "CD", and an HDCD is a CD too, so
            # neither is a case variant (or a variant disc) away from being
            # audited by its rip log's CRCs here.
            if af2.audio is not None and is_cd_media(af2.get_tag("MEDIA")):
                is_cd = True
                break
        except Exception:
            continue
    if not is_cd:
        return {}, {}
    # Keep first-file check for unreadable early return
    af = AudioFile(paths[0])
    if af.audio is None:
        # If first is unreadable but others are CD, still verify those
        pass

    logs = [os.path.join(album_dir, f) for f in sorted(os.listdir(album_dir))
            if f.lower().endswith(".log")]
    if not logs:
        return {}, {p: "no .log file" for p in paths}
    discs = album_discs(album_dir)
    multi = bool(discs)

    verdicts = {}
    pattern = _disc_pattern_for(config)
    # Per path: the log CRC to compare against, or the unverified reason
    # resolved right here. Decoding happens afterwards, in parallel.
    entries = []
    for p in paths:
        if os.path.splitext(p)[1].lower() not in LOSSLESS_CRC_EXTS:
            entries.append((p, "lossy format: .log CRC not comparable", None))
            continue
        d = disc_of_filename(os.path.basename(p))
        if d is None or d < 1:
            d = 1
        if multi:
            log_path = os.path.join(album_dir, _disc_expected_name(pattern, d, ".log"))
            if not os.path.isfile(log_path):
                entries.append((p, f"missing {_disc_expected_name(pattern, d, '.log')}", None))
                continue
            text = read_log_text(log_path)
            per_track = parse_log_checksums(text)
            if not per_track:
                entries.append((p, f"{_disc_expected_name(pattern, d, '.log')} has no per-track CRCs", None))
                continue
            # The log's own numbering may be per disc while the files are
            # numbered release-wide (`2-14 …`): match by path, by number first
            # and by order when the two share none (`mlo.discs.log_crc_map`).
            mapped = log_crc_map(per_track, discs.get(d) or [p],
                                 parse_log_track_seconds(text),
                                 durations={q: _file_seconds(q) for q in (discs.get(d) or [p])})
            crc = mapped.get(p)
            if not crc:
                entries.append((p, f"{os.path.basename(log_path)} has no CRC for this track", None))
                continue
        else:
            per_track = {}
            log_text = ""
            for log_path in logs:
                log_text += read_log_text(log_path)
                per_track.update(parse_log_checksums(log_text))
            if not per_track:
                entries.append((p, "log has no per-track CRCs", None))
                continue
            mapped = log_crc_map(per_track, paths,
                                 parse_log_track_seconds(log_text),
                                 durations={q: _file_seconds(q) for q in paths})
            crc = mapped.get(p)
            if not crc:
                tn = _file_track_number(p)
                entries.append((p, f"log has no CRC for track {tn if tn else '?'}", None))
                continue
        entries.append((p, None, crc))

    to_decode = [(p, crc) for p, reason, crc in entries if reason is None]
    actuals = {}
    if to_decode:
        # A CRC is a property of the AUDIO, and computing it costs a full
        # decode of the track. The album page rebuilds this verdict whenever
        # anything in the folder moved — a script that rewrote every file's
        # tags moves every stamp while the samples stay put — so a file some
        # earlier pass already decoded is answered from mlo.audit's evidence
        # record (the same record the stream-MD5 verdict lives in) instead of
        # paying for the same decode again. Anything it cannot answer for is
        # decoded here and filed back for the next reader.
        try:
            from .audit import note_crc, recorded_crc
        except Exception:
            note_crc = recorded_crc = None
        todo = []
        for p, crc in to_decode:
            got = recorded_crc(p, config) if recorded_crc else ""
            if got:
                actuals[p] = got
            else:
                todo.append((p, crc))
        to_decode = todo
    if to_decode:
        from concurrent.futures import ThreadPoolExecutor
        # Decoders allowed at once, from the run's own worker budget. This
        # function is called once PER ALBUM from audit's album pool, so a
        # constant here multiplied the run's parallelism: worker_limit=2 with
        # two discs started eight ffmpeg decoders and pegged a container's CPU
        # quota — the setting exists to prevent exactly that. *workers* (the
        # caller's per-album share) wins when given.
        if workers is None:
            from .stats import worker_count
            workers = worker_count(config, maximum=4,
                                   items=len(to_decode))
        workers = max(1, min(int(workers), len(to_decode)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for (p, _), actual in zip(to_decode, ex.map(lambda pc: _audio_crc32(ffmpeg_exe, pc[0]), to_decode)):
                actuals[p] = actual
                if note_crc:
                    note_crc(p, actual, config)

    for p, reason, crc in entries:
        if reason is not None:
            unverified[p] = reason
            continue
        actual = actuals.get(p)
        if actual is None:
            unverified[p] = "could not decode audio for CRC"
            continue
        verdicts[p] = "REAL" if actual == crc else "FAKE"
    return verdicts, unverified


def _audio_seconds(paths):
    total = 0.0
    for p in paths:
        try:
            af = AudioFile(p)
            if af.audio is not None and af.audio.info is not None:
                total += float(af.audio.info.length)
        except Exception:
            return None
    return total


# ----------------------------------------------------------------------
# Renaming
# ----------------------------------------------------------------------
def _rename(src, dst, notes):
    """Rename src to dst. Case-only renames (no-ops for os.rename on
    Windows) go through a temp name so cd-1.cue can become CD-1.cue."""
    sbase, dbase = os.path.basename(src), os.path.basename(dst)
    try:
        if os.path.normcase(src) == os.path.normcase(dst):
            if src == dst:
                return False
            tmp = src + ".mlo_case_tmp"
            replace_locked(src, tmp)
            try:
                replace_locked(tmp, dst)
            except OSError:
                os.replace(tmp, src)  # roll back, surface the error
                raise
            notes.append((sbase, dbase))
            return True
        os.rename(src, dst)
        notes.append((sbase, dbase))
        return True
    except OSError as e:
        notes.append((sbase, f"rename failed: {e}"))
        return False


def _disc_pattern_for(config):
    """Return the discs rename pattern (e.g. 'CD-{n}') with {n} placeholder."""
    if config is None:
        return "CD-{n}"
    pat = str(config.get("discs_rename_pattern", "CD-{n}")).strip()
    if "{n}" not in pat:
        return "CD-{n}"
    # Validate after truncation still contains {n}
    truncated = pat[:32]
    if "{n}" not in truncated:
        return "CD-{n}"
    # Strip path separators to avoid traversal
    truncated = truncated.replace("/", "").replace("\\", "")
    return truncated


def _disc_expected_name(pattern, disc, ext):
    """Render pattern for a disc number, with extension."""
    base = pattern.replace("{n}", str(disc))
    # Ensure extension matches expected (lowercase comparison, keep ext as passed)
    if not base.lower().endswith(ext.lower()):
        base += ext
    return base


def _is_expected_disc_file(name, pattern, ext):
    """True if filename matches the pattern for any disc 1..99."""
    for n in range(1, 100):
        if name.lower() == _disc_expected_name(pattern, n, ext).lower():
            return True
    return False


def _cue_track_count(text):
    return len(CUE_TRACK_RE.findall(text))


def _cue_index_starts(text):
    """Start time (seconds) of each track's INDEX 01 entry."""
    return [int(m) * 60 + int(s) + int(f) / 75.0
            for m, s, f in CUE_INDEX_RE.findall(text)]


def _audio_durations(paths):
    """Durations (seconds) of the given audio files in track-number order.

    Returns None when any file is unreadable so the caller can treat the
    disc as having no duration evidence.
    """
    def order_key(p):
        n = _file_track_number(p)
        return ((0, n, os.path.basename(p).lower()) if n is not None
                else (1, 0, os.path.basename(p).lower()))
    out = []
    for p in sorted(paths, key=order_key):
        try:
            af = AudioFile(p)
            if af.audio is None or af.audio.info is None:
                return None
            out.append(float(af.audio.info.length))
        except Exception:
            return None
    return out


def _cue_matches_disc(starts, durations, tol):
    """True when the gaps between the cue's INDEX 01 starts match the audio
    durations. Offset-invariant, so a pregap on track 1 is tolerated."""
    if len(starts) < 2 or len(durations) != len(starts):
        return False
    return all(abs((starts[k + 1] - starts[k]) - durations[k]) <= tol
               for k in range(len(starts) - 1))


def rename_cues_for_discs(album_dir, discs=None, log_fn=None, config=None):
    """Rename cues to <pattern>.cue using content-derived evidence, in
    order of preference:

      1. FILE entries referencing the audio of exactly one disc
      2. an explicit disc number in the current filename
      3. the trivial single-disc case (one disc, one cue)
      4. a unique track-count match
      5. a unique INDEX start-time match against disc audio durations
         (covers image-style cue sheets whose FILE names no longer exist)

    Pattern is configurable via discs_rename_pattern (default 'CD-{n}').
    Uses only content-derived evidence, never order. Single-disc albums
    without D-TT naming are treated as disc 1 so their single cue still
    becomes CD-1.cue (trivial, no guessing).
    """
    if config is not None and not config.get("discs_rename_enabled", True):
        return []
    pattern = _disc_pattern_for(config)
    synthetic = False
    discs = discs if discs is not None else album_discs(album_dir)
    # Single-disc fallback: no D-TT but audio files exist → treat as disc 1
    # Config discs_rename_single_fallback (default True) allows lone .cue/.log
    # to become CD-1 even when no disc evidence exists.
    if not discs:
        if config is not None and not config.get("discs_rename_single_fallback", True):
            return []
        # Use is_audio_file to count actual music (skip sidecars)
        aud = [f for f in os.listdir(album_dir) if is_audio_file(f)]
        if len(aud) > 0:
            # Only trivial single-cue case qualifies; multi-cue without
            # disc evidence remains untouched to avoid guessing.
            cues_tmp = [f for f in os.listdir(album_dir) if f.lower().endswith(".cue")]
            if len(cues_tmp) == 1 and len(aud) >= 1:
                discs = {1: [os.path.join(album_dir, f) for f in aud]}
                synthetic = True
            else:
                return []
    # Build lookup maps for FILE-entry matching: exact, stem-insensitive,
    # and normalized (handles Unicode dashes, disc prefix, and .wav vs .flac)
    known_exact = {}
    known_stem = {}
    known_norm = {}
    for d, paths in discs.items():
        for p in paths:
            base = os.path.basename(p)
            known_exact[base.lower()] = d
            known_stem[os.path.splitext(_ascii_dashes(base))[0].lower()] = d
            known_norm.setdefault(_norm_name(base), d)

    notes = []
    claimed = {}  # disc number -> cue filename
    remaining = []
    info = {}     # cue filename -> (file_discs, track_count, index_starts)
    for f in sorted(os.listdir(album_dir)):
        if not f.lower().endswith(".cue"):
            continue
        try:
            text = open(os.path.join(album_dir, f), "r",
                        encoding="utf-8", errors="replace").read()
        except OSError:
            text = ""
        file_discs = set()
        for m in CUE_FILE_RE.finditer(text):
            # A "/" in a reference is a name character the app may have written
            # as "_" (see naming.cue_ref_names): every spelling is tried.
            for raw in naming.cue_ref_names(m.group(1)):
                # Try exact, then stem, then normalized (most lenient)
                d = known_exact.get(raw.lower())
                if d is None:
                    stem = os.path.splitext(_ascii_dashes(raw))[0].lower()
                    d = known_stem.get(stem)
                if d is None:
                    d = known_norm.get(_norm_name(raw))
                if d is not None:
                    file_discs.add(d)
                    break
        info[f] = (file_discs, _cue_track_count(text), _cue_index_starts(text))

        if _is_expected_disc_file(f, pattern, ".cue"):
            # Keep its claim so no other cue takes the disc; case-only
            # fixes (cd-1.cue -> CD-1.cue) still rename below.
            d = _log_name_disc(f)
            if d:
                claimed.setdefault(d, f)
            continue
        remaining.append(f)

    # 1) FILE entries referencing audio of exactly one disc
    still = []
    for f in remaining:
        file_discs = info[f][0]
        if len(file_discs) == 1 and next(iter(file_discs)) not in claimed:
            claimed[next(iter(file_discs))] = f
        else:
            still.append(f)
    remaining = still

    # 2) explicit disc number already present in the filename
    still = []
    for f in remaining:
        d = _log_name_disc(f)
        if d and d in discs and d not in claimed:
            claimed[d] = f
        else:
            still.append(f)
    remaining = still

    # 3) trivial single-disc case
    if len(discs) == 1 and len(remaining) == 1 and 1 not in claimed:
        claimed[1] = remaining.pop(0)

    if not synthetic:
        # 4) unique track-count match among unclaimed discs
        if remaining and len(claimed) < len(discs):
            counts = {d: len(paths) for d, paths in discs.items()
                      if d not in claimed}
            still = []
            for f in remaining:
                tc = info[f][1]
                cands = [d for d, n in counts.items() if n == tc] if tc else []
                if len(cands) == 1 and cands[0] not in claimed:
                    claimed[cands[0]] = f
                    counts.pop(cands[0])
                else:
                    still.append(f)
            remaining = still

        # 5) unique INDEX start-time match (image-style cue sheets whose
        #    FILE references no longer exist on disk)
        if remaining and len(claimed) < len(discs):
            tol = float(config.get("discs_toc_tolerance_s", TOC_TOLERANCE_S)) if config else TOC_TOLERANCE_S
            margin = float(config.get("discs_toc_unique_margin_s", TOC_UNIQUE_MARGIN_S)) if config else TOC_UNIQUE_MARGIN_S
            durs = {}
            for d, paths in discs.items():
                if d in claimed:
                    continue
                seq = _audio_durations(paths)
                if seq:
                    durs[d] = seq
            for f in remaining:
                starts = info[f][2]
                if len(starts) < 2:
                    continue
                scored = sorted(
                    (max(abs((starts[k + 1] - starts[k]) - seq[k])
                         for k in range(len(starts) - 1)), d)
                    for d, seq in durs.items()
                    if len(seq) == len(starts)
                    and _cue_matches_disc(starts, seq, tol)
                )
                if not scored:
                    continue
                unique = (len(scored) == 1
                          or scored[1][0] - scored[0][0] >= margin)
                if unique and scored[0][1] not in claimed:
                    claimed[scored[0][1]] = f
                    durs.pop(scored[0][1], None)

    for d, f in sorted(claimed.items()):
        src = os.path.join(album_dir, f)
        dst = os.path.join(album_dir, _disc_expected_name(pattern, d, ".cue"))
        if dst == src:
            continue
        # exists() is case-insensitive on Windows: only skip when dst is a
        # DIFFERENT file; a case-variant of src must still rename.
        if os.path.exists(dst) and os.path.normcase(dst) != os.path.normcase(src):
            continue
        _rename(src, dst, notes)
    if log_fn and notes:
        for old, new in notes:
            log_fn(f"cue: {old} -> {new}")
    return notes


def _log_name_disc(name):
    base = os.path.splitext(name)[0]
    m = LOG_NAME_DISC_RE.search(base)
    if not m:
        return None
    raw = next((g for g in m.groups() if g), None)
    if raw is None:
        return None
    d = int(raw)
    return d if 1 <= d <= 99 else None


def rename_logs_for_discs(album_dir, discs=None, log_fn=None, config=None):
    """Rename logs to <pattern>.log using content-derived evidence only."""
    if config is not None and not config.get("discs_rename_enabled", True):
        return []
    pattern = _disc_pattern_for(config)
    discs = discs if discs is not None else album_discs(album_dir)
    # Single-disc fallback without D-TT: one log → CD-1.log
    if not discs:
        if config is not None and not config.get("discs_rename_single_fallback", True):
            return []
        aud = [f for f in os.listdir(album_dir) if is_audio_file(f)]
        if len(aud) > 0:
            logs_tmp = [f for f in os.listdir(album_dir) if f.lower().endswith(".log")]
            if len(logs_tmp) == 1 and len(aud) >= 1:
                discs = {1: [os.path.join(album_dir, f) for f in aud]}
            else:
                return []
    logs = [f for f in sorted(os.listdir(album_dir))
            if f.lower().endswith(".log")]
    notes = []

    # 1) explicit disc numbers already present in filenames
    remaining = []
    claimed = {}
    for f in logs:
        if _is_expected_disc_file(f, pattern, ".log"):
            d = _log_name_disc(f)
            if d:
                claimed.setdefault(d, f)
            continue
        d = _log_name_disc(f)
        if d and d in discs and d not in claimed:
            claimed[d] = f
        else:
            remaining.append(f)

    # 2) trivial single-disc case
    if len(discs) == 1 and len(remaining) == 1 and 1 not in claimed:
        claimed[1] = remaining.pop(0)

    # 3) unique TOC total-duration match against real audio durations
    if remaining and len(claimed) < len(discs):
        toc_tol = float(config.get("discs_toc_tolerance_s", TOC_TOLERANCE_S)) if config else TOC_TOLERANCE_S
        toc_margin = float(config.get("discs_toc_unique_margin_s", TOC_UNIQUE_MARGIN_S)) if config else TOC_UNIQUE_MARGIN_S
        durations = {}
        for d, paths in discs.items():
            if d in claimed:
                continue
            secs = _audio_seconds(paths)
            if secs:
                durations[d] = secs
        for f in remaining:
            toc = parse_log_toc_seconds(read_log_text(os.path.join(album_dir, f)))
            if toc <= 0:
                continue
            candidates = [d for d, s in durations.items()
                          if abs(s - toc) <= toc_tol]
            if len(candidates) == 1:
                d = candidates[0]
                margins = sorted(abs(s - toc) for s in durations.values())
                unique = (len(margins) < 2 or
                          margins[1] - margins[0] >= toc_margin)
                if unique and d not in claimed:
                    claimed[d] = f
                    durations.pop(d, None)

    for d, f in sorted(claimed.items()):
        src = os.path.join(album_dir, f)
        dst = os.path.join(album_dir, _disc_expected_name(pattern, d, ".log"))
        if dst == src:
            continue
        if os.path.exists(dst) and os.path.normcase(dst) != os.path.normcase(src):
            continue
        _rename(src, dst, notes)
    if log_fn and notes:
        for old, new in notes:
            log_fn(f"log: {old} -> {new}")
    return notes


def _accurip_disc_by_track_crc(album_dir, filename, discs, claimed):
    """The one disc whose tracks all match this .accurip's CRC table, or None.

    CUETools prints each track's CRC-32 of the decoded audio in the EAC-style
    table at the end of the log, and `_audio_crc32` reproduces that value from
    the file on disk — real evidence of which disc an .accurip whose name
    carries no disc number ("App.accurip") belongs to. That is the .accurip's
    counterpart of the rip log's TOC-duration match above.

    None when the file has no such table, no ffmpeg can decode the audio, or
    the match is not UNIQUE: renaming onto the wrong disc's name makes grading
    read the other disc's verdict, which is worse than leaving the file alone.
    """
    try:
        from .accurip import parse_accurip_track_crcs
    except Exception:
        return None
    try:
        with open(os.path.join(album_dir, filename), "r",
                  encoding="utf-8", errors="replace") as fh:
            table = parse_accurip_track_crcs(fh.read())
    except OSError:
        return None
    if not table:
        return None
    try:
        from .tools import detect_all_tools
        ffmpeg_exe = (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")
    except Exception:
        ffmpeg_exe = None
    if not ffmpeg_exe or not os.path.isfile(ffmpeg_exe):
        return None
    matches = []
    for d, paths in sorted(discs.items()):
        if d in claimed or len(paths) < max(table):
            continue
        ordered = sorted(paths, key=lambda p: (_track_num_of(p) or 0,
                                               _file_track_number(p) or 0))
        for tn in sorted(table):
            if _audio_crc32(ffmpeg_exe, ordered[tn - 1]) != table[tn]:
                break
        else:
            matches.append(d)
    return matches[0] if len(matches) == 1 else None


def rename_accurip_for_discs(album_dir, discs=None, log_fn=None, config=None):
    """Rename .accurip files to <pattern>.accurip.

    Same evidence order as the logs: an explicit disc number in the filename,
    the single-disc fallback, then content — the log's per-track CRC table,
    which is the .accurip's equivalent of the rip log's TOC durations.
    """
    if config is not None and not config.get("discs_rename_enabled", True):
        return []
    pattern = _disc_pattern_for(config)
    discs = discs if discs is not None else album_discs(album_dir)
    if not discs:
        if config is not None and not config.get("discs_rename_single_fallback", True):
            return []
        aud = [f for f in os.listdir(album_dir) if is_audio_file(f)]
        if len(aud) > 0:
            tmp = [f for f in os.listdir(album_dir) if f.lower().endswith(".accurip")]
            if len(tmp) == 1 and len(aud) >= 1:
                discs = {1: [os.path.join(album_dir, f) for f in aud]}
            else:
                return []
    files = [f for f in sorted(os.listdir(album_dir)) if f.lower().endswith(".accurip")]
    notes = []
    # Reuse same logic as logs: explicit disc numbers, single-disc fallback, TOC match
    # 1) explicit
    remaining = []
    claimed = {}
    for f in files:
        if _is_expected_disc_file(f, pattern, ".accurip"):
            d = _log_name_disc(f)
            if d:
                claimed.setdefault(d, f)
            continue
        d = _log_name_disc(f)
        if d and d in discs and d not in claimed:
            claimed[d] = f
        else:
            remaining.append(f)
    if len(discs) == 1 and 1 not in claimed and remaining:
        # Single-disc with orphan .accurip files: pick the best candidate (prefer valid CUETools log)
        # This fixes automatic rename for .accurip per user request; previous logic required exactly 1 remaining.
        best = None
        best_score = -1
        for f in remaining:
            pth = os.path.join(album_dir, f)
            try:
                head = open(pth, "r", encoding="utf-8", errors="replace").read(4096)
                has_header = "[CUETools log;" in head
                try:
                    sz = os.path.getsize(pth)
                except OSError:
                    sz = 0
                score = (1000000 if has_header else 0) + sz
                # Prefer larger/valid
            except Exception:
                score = 0
            if score > best_score:
                best_score = score
                best = f
        if best is not None:
            claimed[1] = best
            try:
                remaining.remove(best)
            except ValueError:
                pass
    # 3) unique track-CRC match against the disc's own decoded audio. This is
    #    the step a legacy name with no disc number needs: without it an
    #    "App.accurip" on a multi-disc album stayed orphaned while the album
    #    read REAL/FAKE off whatever that leftover file happened to say.
    if remaining and len(claimed) < len(discs):
        for f in list(remaining):
            try:
                d = _accurip_disc_by_track_crc(album_dir, f, discs, claimed)
            except Exception:
                d = None
            if d is not None and d not in claimed:
                claimed[d] = f
                remaining.remove(f)
    for d, f in sorted(claimed.items()):
        src = os.path.join(album_dir, f)
        dst = os.path.join(album_dir, _disc_expected_name(pattern, d, ".accurip"))
        if dst == src:
            continue
        if os.path.exists(dst) and os.path.normcase(dst) != os.path.normcase(src):
            continue
        _rename(src, dst, notes)
    if log_fn and notes:
        for old, new in notes:
            log_fn(f"accurip: {old} -> {new}")
    return notes


# ----------------------------------------------------------------------
# CUE FILE-name correction (conservative, evidence-based)
# ----------------------------------------------------------------------
# Unicode dashes that appear in filenames (especially "Suite‐Pee" U+2010)
_UNICODE_DASHES = ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212", "\uFE58", "\uFE63", "\uFF0D")

def _ascii_dashes(s):
    for ch in _UNICODE_DASHES:
        s = s.replace(ch, "-")
    return s

def _strip_disc_prefix(name):
    """Strip leading disc prefix 'D-' from D-TT filenames, leaving 'TT Title'."""
    base = os.path.basename(name)
    # D-TT like "1-01 Title" or "1 - 01 Title" -> strip the "1-"
    m = re.match(r"^\d{1,2}\s*-\s*", _ascii_dashes(base))
    if m:
        # Only strip if what remains starts with a track number (TT)
        rest = base[m.end():]
        if re.match(r"^\d{1,3}(?:\s*[-._\s]|\b)", rest):
            return rest
    return base

def _norm_name(s):
    """Normalize a filename for comparison: lowercase, strip extension
    separators/underscores/double spaces. Never removes digits.
    Handles Unicode dashes and strips disc prefix so '1-01 Title' matches '01 Title'.

    The name is keyed through the shared rule first (naming.name_key), so a
    sheet naming the PRE-change spelling of a file still matches the file this
    app wrote: "A*B.flac" and "A_B.flac" key alike."""
    # Normalize dashes first, then strip disc prefix for comparison
    s = naming.name_key(s)
    s = _ascii_dashes(s)
    s = _strip_disc_prefix(s)
    s = os.path.splitext(s)[0].lower()
    s = re.sub(r"[\s_\-\.]+", " ", s).strip()
    return re.sub(r"\s+", " ", s)


def _track_num_of(name):
    """Track number from filename.
    For D-TT like '1-01 Title.flac' returns 1 (the TT, not the disc).
    For '01 - Title.flac' returns 1. Handles Unicode dashes."""
    base = _ascii_dashes(os.path.basename(name))
    # D-TT: disc-track
    m = re.match(r"^\d{1,2}\s*-\s*(\d{1,3})(?:\s*[-._\s]|\b)", base)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    m = re.match(r"^(\d{1,3})(?:\s*[-._\s])", base)
    return int(m.group(1)) if m else None


def cue_file_refs(path):
    """Every FILE reference a cue sheet names, verbatim (no disk access).

    Read the same way fix_cue_filenames reads it (utf-8-sig, then latin-1 so
    an EAC ANSI sheet does not come back as replacement characters), so the
    grader and the rewriter can never disagree about what a sheet points at.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    refs = []
    for line in text.splitlines():
        m = CUE_FILE_RE.match(line.rstrip("\r\n"))
        if m:
            refs.append(m.group(1))
    return refs


def _converted_twin(ref_base, album_dir, audio):
    """The one audio file sharing `ref_base`'s stem under a different extension.

    A converted album keeps its cue pointing at the file it was ripped from —
    "…CDImage.wav" — while the library holds "…CDImage.flac". With
    ``lossless_remove_original`` off the referenced name still EXISTS, which is
    exactly the case an existence test skips, so the sheet would name a file
    the player never touches.

    "Alone" is measured over EVERY file sharing the stem, not just the ones
    this library calls audio: a folder holding "img.wav" + "img.flac" +
    "img.mp3" is ambiguous and stays as the sheet wrote it, while the plain
    conversion case (the source plus the file that replaced it) resolves.
    """
    stem = os.path.splitext(naming.name_key(ref_base))[0].lower()
    if not stem:
        return None
    ext = os.path.splitext(ref_base)[1].lower()
    try:
        siblings = [f for f in os.listdir(album_dir)
                    if os.path.splitext(naming.name_key(f))[0].lower() == stem
                    and os.path.splitext(f)[1].lower() != ext]
    except OSError:
        return None
    if len(siblings) != 1 or siblings[0] not in audio:
        return None
    return siblings[0]


def _cue_disc_audio(cue_name, discs):
    """The disc a cue sheet belongs to, and that disc's audio files.

    A FILE reference numbered like a track only means something against the
    audio of the sheet's OWN disc, so the disc mapping is the app's own
    (`album_discs`) and the disc comes from the sheet's name ("CD-1.cue") —
    the same reading `rename_cues_for_discs` and `grade_album_logs` use.

    The caller owns the lone-cue fallback (a folder with no D-TT structure
    and one sheet is one disc, so it hands in a synthetic mapping); an empty
    mapping here means there is no disc evidence, and a number is then read
    against nothing rather than against every file in the folder. A sheet
    naming a disc this album does not have reads against nothing too:
    (None, []) — never another disc's audio.
    """
    d = _log_name_disc(cue_name)
    if d is not None:
        return (d, list(discs[d])) if d in discs else (None, [])
    if len(discs) == 1:
        only = next(iter(discs))
        return only, list(discs[only])
    return None, []


def _ref_track_numbers(ref_base, disc):
    """Track numbers a cue FILE reference can name on *disc*, best first.

    A reference is written either "TT - Title.ext" (track first — what EAC
    writes) or "D-TT Title.ext" (the library's own name), and the two are
    identical text when the title itself starts with a number: "09 - 36.wav"
    on disc 1 is its ninth track, titled "36", while `_track_num_of` reads
    that very text as disc 09 / track 36 — one library's every reference left
    unrepaired because of it.

    The leading number is the track. Only when it IS this disc's number can
    the text also be this disc's own D-TT name, and then that reading is
    tried first ("2-05 x" on disc 2 is track 5) before the track-first one
    ("02 - 36" on disc 2 is track 2, titled "36"). The number after the dash
    is never offered otherwise: it belongs to the title far more often than
    to a second track, and offering it would leave a disc carrying both
    tracks 09 and 36 unrepaired.
    """
    base = _ascii_dashes(os.path.basename(ref_base))
    m = re.match(r"^(\d{1,3})\s*[-._\s]\s*(\d{1,3})?", base)
    if not m:
        return []
    lead = int(m.group(1))
    second = int(m.group(2)) if m.group(2) else None
    if second is not None and lead == disc:
        return [second, lead]
    return [lead]


# One ALBUM at a time: a conversion (script 3) fixes the sheet from several
# worker threads as it walks an album's files, and two writers computing
# different texts for the same sheet would let the last one win with a
# half-converted view of the folder.
#
# Per folder, not global: nothing here crosses an album boundary — a repair
# reads and rewrites only the sheets it finds in the folder it was handed — so
# one process-wide lock also made scripts that repair many albums queue behind
# one another. Script 2's rename/repair pass runs its albums in lanes now, and
# a folder with a sheet to rewrite would otherwise hold every other lane up
# for the length of its own I/O. Same exclusion where it is needed, none where
# it is not. Locks are created on first use, as in mlo.fetchdeps.install_lock.
_cue_fix_locks = {}
_cue_fix_locks_guard = threading.Lock()


def _cue_fix_lock(album_dir):
    """The lock guarding cue repair of ONE album folder."""
    key = os.path.normcase(os.path.abspath(album_dir))
    with _cue_fix_locks_guard:
        lock = _cue_fix_locks.get(key)
        if lock is None:
            lock = _cue_fix_locks[key] = threading.Lock()
        return lock


def fix_cue_filenames(album_dir, log_fn=None, config=None):
    """Correct FILE entries inside .cue sheets to match the actual audio
    filenames in *album_dir* — with minimal assumptions.

    A FILE entry is rewritten when:
      1. the referenced file does not exist on disk (any letter-case), and
         exactly ONE candidate audio file matches by normalized name
         (punctuation/space-insensitive), OR exactly one candidate shares
         the same leading track number AND the cue references exactly the
         tracks of one album folder (single-cue sanity); or
      2. the referenced file exists but a conversion left exactly one
         same-stem twin under another extension — the file the library
         actually holds (see _converted_twin); or
      3. the referenced file does not exist and its number identifies
         exactly one track OF THE DISC THIS CUE BELONGS TO — the app's own
         disc mapping, so "09 - 36.wav" in CD-1.cue finds disc 1's ninth
         track without reading the title as a track 36 (see
         _ref_track_numbers); or
      4. the sheet is a single-image sheet (one FILE line for the whole
         disc) and that disc holds exactly one audio file.
    Ambiguous or missing matches are left untouched and reported.

    Returns a list of note strings describing every change.
    """
    if config is not None and not config.get("cue_fix_filenames", True):
        return []
    with _cue_fix_lock(album_dir):
        return _fix_cue_filenames_locked(album_dir, log_fn, config)


def _fix_cue_filenames_locked(album_dir, log_fn, config):
    notes = []
    cues = [f for f in sorted(os.listdir(album_dir))
            if f.lower().endswith(".cue")]
    if not cues:
        return notes

    audio = [f for f in sorted(os.listdir(album_dir)) if is_audio_file(f)]
    if not audio:
        return notes

    # The app's own disc mapping, for reading a FILE reference's number as a
    # track. A folder with no D-TT structure and a single cue is still one
    # disc — the same fallback rename_cues_for_discs uses — so that folder
    # gets a synthetic mapping and its references can be read as that disc's
    # tracks; with more than one cue and no mapping there is no disc to read
    # them against, and the rules below simply do not apply.
    discs = album_discs(album_dir)
    if not discs and len(cues) == 1:
        discs = {1: [os.path.join(album_dir, f) for f in audio]}

    # Lookup tables over real files.
    exact = {f.lower(): f for f in audio}
    norm = {}
    for f in audio:
        norm.setdefault(_norm_name(f), []).append(f)
    nums = {}
    for f in audio:
        n = _track_num_of(f)
        if n is not None:
            nums.setdefault(n, []).append(f)

    for cue in cues:
        path = os.path.join(album_dir, cue)
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            # EAC writes cue sheets in the ANSI codepage (cp1252/latin-1);
            # errors="replace" would turn every such byte into U+FFFD and
            # corrupt the sheet on the write-back below.
            text = raw.decode("latin-1")
        lines = text.splitlines(keepends=True)

        # Which disc this sheet describes, and which files of the folder are
        # that disc's. A rewritten reference is a name in this folder, so the
        # per-disc table is keyed by basename.
        cue_disc, disc_audio = _cue_disc_audio(cue, discs)
        disc_audio = [os.path.basename(p) for p in disc_audio]
        disc_nums = {}
        for f in disc_audio:
            n = _track_num_of(f)
            if n is not None:
                disc_nums.setdefault(n, []).append(f)
        # One FILE line for the whole disc: the sheet names the disc's image,
        # not a track, so a number can never resolve it.
        single_image = (len(disc_audio) == 1
                        and sum(1 for ln in lines
                                if CUE_FILE_RE.match(ln.rstrip("\n"))) == 1)

        # ONE pass decides every FILE entry's fate, because the trim below is
        # a decision about the SHEET and not about one line: an entry can only
        # be called EXTRA when the rest of the sheet is provably the album.
        starts = [i for i, line in enumerate(lines)
                  if CUE_FILE_RE.match(line.rstrip("\n"))]
        entries = []
        for k, i in enumerate(starts):
            ref = CUE_FILE_RE.match(lines[i].rstrip("\n")).group(1)
            entries.append({
                "start": i,
                "end": starts[k + 1] if k + 1 < len(starts) else len(lines),
                "line": lines[i],
                "ref": ref,
                "names": naming.cue_ref_names(ref),
            })

        def decide(entry):
            """(the line to write, the file it names, a note) for ONE entry.

            The file it names is the audio file this entry CLAIMS — the proof
            the trim below rests on — and None when nothing resolved. Every
            rule is the one this function has always had; only the bookkeeping
            around them is new."""
            line, ref, names = entry["line"], entry["ref"], entry["names"]
            ref_base = names[-1] if names else ""
            new_line = line
            exists = bool(ref_base) and (
                os.path.isfile(os.path.join(album_dir, ref_base))
                or ref_base.lower() in exact
                or any(f.lower() == ref_base.lower() for f in audio)
            )
            if exists:
                twin = _converted_twin(ref_base, album_dir, audio)
                if twin is not None and twin.lower() != ref_base.lower():
                    head = ref[: len(ref) - len(ref_base)] if ref_base else ""
                    new_ref = head + twin
                    new_line = line.replace(f'"{ref}"', f'"{new_ref}"', 1)
                    if new_line != line:
                        return new_line, twin, (
                            f'{cue}: FILE "{ref}" -> "{new_ref}" (converted)')
                return new_line, ref_base, None
            candidates, matched = None, None
            for nm in names:
                # 1) unique normalized-name match
                c = norm.get(_norm_name(nm), [])
                if len(c) == 1:
                    candidates, matched = c, nm
                    break
                # 2) unique leading-track-number match
                tn = _track_num_of(nm)
                if tn is not None:
                    c2 = nums.get(tn, [])
                    if len(c2) == 1:
                        candidates, matched = c2, nm
                        break
                if cue_disc is not None:
                    # 3) the reference's number as a track of THIS cue's disc
                    #    (the number alone is ambiguous — see _ref_track_numbers)
                    for n in _ref_track_numbers(nm, cue_disc):
                        c3 = disc_nums.get(n, [])
                        if len(c3) == 1:
                            candidates, matched = c3, nm
                            break
                    if candidates:
                        break
                    if single_image:
                        # 4) one FILE line for the whole disc, and the disc
                        #    holds one file: that file IS what the sheet names.
                        candidates, matched = list(disc_audio), nm
                        break
            if candidates:
                actual = candidates[0]
                # Keep any directory part of the original reference — but only
                # when the LEAF was what matched. When the whole reference is
                # the name (its "/" was a character this app writes as "_"),
                # its "directory" was never one.
                head = (ref[: len(ref) - len(matched)]
                        if matched and ref.endswith(matched) else "")
                new_ref = head + actual
                if new_ref != ref:
                    new_line = line.replace(f'"{ref}"', f'"{new_ref}"', 1)
                    if new_line != line:
                        return new_line, actual, f'{cue}: FILE "{ref}" -> "{new_ref}"'
                return new_line, actual, None
            return new_line, None, None

        resolved = [(e, *decide(e)) for e in entries]
        claims = [claimed for _e, _line, claimed, _note in resolved if claimed]

        # THE ALBUM'S OWN TRACKS ARE THE PROOF that an unresolvable entry is
        # EXTRA. Every entry that DID resolve must claim a DIFFERENT file of
        # this disc, and between them they must be exactly the disc's files:
        # then the sheet lists the album PLUS something the album does not have
        # (the owner's "CD-1.cue: 13 - Creep [clean].wav" beside twelve tracks),
        # and removing the extras is not a guess — what is left maps one-to-one
        # onto the files on disk. Anything else (an ambiguous reference, a sheet
        # whose files are not all accounted for, a disc the mapping does not
        # name) is left exactly as it was and reported, which is what this
        # function has always done.
        drop = set()
        missing = [e for e, _line, claimed, _note in resolved if not claimed]
        own = [os.path.basename(p) for p in (disc_audio if cue_disc is not None else [])]
        if missing and own:
            wanted = {f.lower() for f in own}
            claimed_keys = [c.lower() for c in claims]
            if len(claimed_keys) == len(set(claimed_keys)) and set(claimed_keys) == wanted:
                drop = {id(e) for e in missing}

        changed = False
        out_lines = list(lines[:entries[0]["start"]]) if entries else []
        for entry, new_line, claimed, note in resolved:
            if id(entry) in drop:
                # The entry AND the TRACK/INDEX block it owns go together: a
                # TRACK line without its FILE (or one claiming a file the album
                # does not have) is the "CUE references a file the album does
                # not have" failure itself, and dropping only the FILE line
                # would leave the sheet listing a track nothing can play.
                block = lines[entry["start"]:entry["end"]]
                track = next((CUE_TRACK_LINE_RE.match(ln).group(1)
                              for ln in block if CUE_TRACK_LINE_RE.match(ln)), "")
                notes.append(
                    f'{cue}: removed {"track " + track + " " if track else ""}'
                    f'"{entry["ref"]}" — this {"disc" if len(discs) > 1 else "album"} '
                    f'has {len(own)} file(s) and this entry names one it does not have')
                changed = True
                continue
            if note:
                notes.append(note)
            elif not claimed:
                # Reported where its own entry sits, so the note stream stays in
                # sheet order (what a caller reads) — an entry that could not be
                # resolved and could not be proven extra is left exactly as it is.
                notes.append(f'{cue}: unresolved FILE "{entry["ref"]}" left as-is')
            if new_line != entry["line"]:
                changed = True
            out_lines.append(new_line)
            out_lines.extend(lines[entry["start"] + 1:entry["end"]])

        if changed:
            try:
                fd, tmp = tempfile.mkstemp(prefix=".cue_fix_", suffix=".cue", dir=os.path.dirname(path) or ".")
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                    fh.writelines(out_lines)
                    try:
                        fh.flush()
                        os.fsync(fh.fileno())
                    except Exception:
                        pass
                replace_locked(tmp, path)
                fsync_dir(os.path.dirname(path))
            except OSError as e:
                notes.append(f"{cue}: write failed ({e})")
    if log_fn and notes:
        for n in notes:
            log_fn(n)
    return notes


# ----------------------------------------------------------------------
# Per-disc rip-log scoring — now via OPSnet Logchecker (PHP) instead of AudioAuditor
# ----------------------------------------------------------------------
def logchecker_available():
    """Whether a rip-log scorer (Logchecker phar + PHP) is installed.

    A missing scorer and an unscorable log are different things: with no
    scorer nothing judged the logs, so callers must not read "no score" as
    "every log of this library is broken" (the audit used to write
    AUDIT=FAKE for every CD track of a library when PHP was absent).
    """
    try:
        from .tools import detect_all_tools
        tools = detect_all_tools()
        lc = tools.get("logchecker") or {}
        php = tools.get("php") or {}
        phar = lc.get("phar_path")
        php_exe = lc.get("php_exe") or php.get("php_exe") or shutil.which("php")
        return bool(phar) and os.path.isfile(phar) and bool(php_exe) and os.path.isfile(php_exe)
    except Exception:
        return False


def _logchecker_env():
    """The environment the phar runs with: this process's, plus every place an
    `eac-logchecker` console script may live.

    Logchecker does not compute an EAC log's SHA256 itself — it shells out to
    the pypi package's script, and prints `Checksum: checksum_ok` either way.
    The app vendors pip packages and a second Python on the box often holds the
    shim, so both are put on PATH before asking."""
    env = dict(os.environ)
    try:
        import sys as _sys
        import glob as _glob
        candidates = [os.path.join(os.path.dirname(_sys.executable), "Scripts")]
        for pattern in (
            os.path.expandvars(r"%LocalAppData%\Python\*\Scripts"),
            os.path.expandvars(r"%LocalAppData%\Programs\Python\Python*\Scripts"),
            os.path.expandvars(r"%LocalAppData%\Programs\Python\*\Scripts"),
        ):
            candidates.extend(_glob.glob(pattern))
        for p in candidates:
            if os.path.isdir(p) and p not in env.get("PATH", ""):
                env["PATH"] = p + os.pathsep + env.get("PATH", "")
        py_dir = os.path.dirname(_sys.executable)
        if py_dir not in env.get("PATH", ""):
            env["PATH"] = py_dir + os.pathsep + env.get("PATH", "")
    except Exception:
        pass
    return env


def _logchecker_paths():
    """(php_exe, phar) for the installed Logchecker, or (None, None)."""
    from .tools import detect_all_tools
    tools = detect_all_tools()
    lc_info = tools.get("logchecker") or {}
    php_info = tools.get("php") or {}
    phar = lc_info.get("phar_path")
    php_exe = lc_info.get("php_exe") or (php_info.get("php_exe") if php_info else None)
    if not phar or not php_exe or not os.path.isfile(phar) or not os.path.isfile(php_exe):
        php_exe = shutil.which("php") or php_exe
    if not phar or not php_exe or not os.path.isfile(phar) or not os.path.isfile(php_exe):
        return None, None
    return php_exe, phar


def run_logchecker(log_path, timeout=30):
    """`php logchecker.phar analyze --no_text <log>` -> its output, or None.

    The ONE runner: the score, the report the UI shows and the checksum
    fallback all read this output, so they cannot disagree about what
    Logchecker said — or about whether it ran at all."""
    if not log_path or not os.path.isfile(log_path):
        return None
    try:
        php_exe, phar = _logchecker_paths()
    except Exception:
        return None
    if not php_exe or not phar:
        return None
    try:
        proc = run_tool([php_exe, phar, "analyze", "--no_text", log_path],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding="utf-8", errors="replace",
                        timeout=timeout, env=_logchecker_env())
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def parse_logchecker(out):
    """Logchecker's `--no_text` output as data.

    The header keys it always writes (Ripper/Version/Language/Score/Checksum),
    the `Details :` lines under them — its own per-check notices, which is where
    a deduction explains itself — and `raw`, so a version that adds a field
    later is still visible to whoever opens the log."""
    rep = {"ripper": "", "version": "", "language": "", "score": None,
           "checksum": "", "details": [], "raw": (out or "")[:20000]}
    if not out:
        return rep
    for key, field in (("Ripper", "ripper"), ("Version", "version"),
                       ("Language", "language"), ("Checksum", "checksum")):
        m = re.search(rf"^{key}\s*:\s*(.*)$", out, re.MULTILINE | re.IGNORECASE)
        if m:
            rep[field] = m.group(1).strip()
    m = re.search(r"^Score\s*:\s*(-?\d+)\s*$", out, re.MULTILINE | re.IGNORECASE)
    if m:
        rep["score"] = int(m.group(1))
    m = re.search(r"^Details\s*:\s*$(.*)", out,
                  re.MULTILINE | re.IGNORECASE | re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip()
            if line:
                rep["details"].append(line)
    return rep


def log_report(log_path, timeout=30, max_text=200_000):
    """Everything this app knows about ONE rip log: Logchecker's own report,
    this app's checksum verdict, and the log's text.

    Read-only, and the answer the owner asked for ("I don't even know a way to
    view logs"): the score alone never said WHY a log scored 60, and the text is
    what a person reads to see it. `available` is false when no scorer is
    installed — an empty report is never dressed up as a scored one."""
    log_path = str(log_path or "")
    report = {"path": log_path, "name": os.path.basename(log_path),
              "exists": bool(log_path) and os.path.isfile(log_path),
              "available": False, "report": parse_logchecker(None),
              "checksum": {"state": None, "detail": None},
              "text": "", "truncated": False, "bytes": 0}
    if not report["exists"]:
        return report
    try:
        report["bytes"] = os.path.getsize(log_path)
    except OSError:
        report["bytes"] = 0
    out = run_logchecker(log_path, timeout=timeout)
    report["available"] = out is not None
    if out:
        report["report"] = parse_logchecker(out)
    state, detail = check_log_checksum(log_path)
    report["checksum"] = {"state": state, "detail": detail}
    text = read_log_text(log_path)
    report["truncated"] = len(text) > max_text
    report["text"] = text[:max_text]
    return report


def score_disc_log(cli_exe, log_path=None, disc_files=None, timeout=30):
    """Score one disc's log with OPSnet Logchecker via PHP. Returns 0-100 or None.

    Supports both old calling convention score_disc_log(cli, log_path, disc_files)
    and new score_disc_log(log_path). disc_files is ignored (Logchecker only needs log).
    """
    # Handle overloaded signatures
    actual_log = log_path if log_path is not None else cli_exe
    # If cli_exe is actually a log path (new call) and log_path is None
    if actual_log is None and isinstance(cli_exe, str) and cli_exe.lower().endswith(".log"):
        actual_log = cli_exe
    if isinstance(disc_files, int) and timeout == 30:
        # timeout passed as disc_files when called with 3 args where third is timeout
        timeout = disc_files
        disc_files = None
    log_path = actual_log
    if not log_path or not isinstance(log_path, str) or not os.path.isfile(log_path):
        return None
    try:
        from .tools import detect_all_tools
        tools = detect_all_tools()
        lc_info = tools.get("logchecker")
        php_info = tools.get("php")
        if not lc_info:
            return None
        phar = lc_info.get("phar_path")
        php_exe = lc_info.get("php_exe") or (php_info.get("php_exe") if php_info else None)
        if not phar or not php_exe or not os.path.isfile(phar) or not os.path.isfile(php_exe):
            # Fallback: try to locate php via PATH if not in deps
            php_exe = shutil.which("php") or php_exe
            if not php_exe or not os.path.isfile(php_exe):
                return None
        # Ensure Python Scripts for checksum validation are findable (Logchecker tries python/eac-logchecker)
        env = dict(os.environ)
        try:
            import sys as _sys
            import glob as _glob
            candidates = [os.path.join(os.path.dirname(_sys.executable), "Scripts")]
            # Also scan common user-level Python installs so Logchecker's
            # eac-logchecker helper resolves on any machine (the Scripts
            # dir of a second, non-runtime Python often holds the shim).
            for pattern in (
                os.path.expandvars(r"%LocalAppData%\Python\*\Scripts"),
                os.path.expandvars(r"%LocalAppData%\Programs\Python\Python*\Scripts"),
                os.path.expandvars(r"%LocalAppData%\Programs\Python\*\Scripts"),
            ):
                candidates.extend(_glob.glob(pattern))
            for p in candidates:
                if os.path.isdir(p) and p not in env.get("PATH", ""):
                    env["PATH"] = p + os.pathsep + env.get("PATH", "")
            # Also add python exe dir
            py_dir = os.path.dirname(_sys.executable)
            if py_dir not in env.get("PATH", ""):
                env["PATH"] = py_dir + os.pathsep + env.get("PATH", "")
        except Exception:
            pass
        proc = run_tool([php_exe, phar, "analyze", "--no_text", log_path],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding="utf-8", errors="replace",
                        timeout=timeout, env=env)
        # The score is the rip grade only when Logchecker actually finished
        # AND its own checksum validation did not FAIL: the number it prints
        # for a log whose SHA256 does not match grades the log's text (and a
        # crashed run can print a partial one). A log Logchecker never
        # validated (XLD logs, no checksum concept) is still scored — the
        # audit's own checksum gate owns that verdict.
        if proc.returncode != 0:
            return None
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        m_chk = re.search(r"Checksum\s*:\s*(\w+)", out, re.IGNORECASE)
        if m_chk and m_chk.group(1).lower() in ("checksum_invalid", "checksum_error"):
            return None
        m = re.search(r"Score\s*:\s*(\d+)", out)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        return None
    except Exception:
        return None


# ----------------------------------------------------------------------
# Log checksum + AccurateRip verification (for audit)
# ----------------------------------------------------------------------
def _eac_helper_on_path(path=None):
    """Whether the phar's own checksum helper is runnable with this PATH.

    Logchecker does not validate a log itself: it shells out to the pypi
    package's `eac-logchecker` script and prints `Checksum: checksum_ok` either
    way — including for a log with one byte changed. Asking PATH is asking the
    same question the phar asks itself, and it is the only signal that holds for
    every log: a log the phar cannot even parse prints no "not validated" notice
    at all, while a helper it cannot find is missing for all of them."""
    try:
        return bool(shutil.which("eac-logchecker",
                                 path=path or os.environ.get("PATH")))
    except Exception:
        return False


def check_log_checksum(log_path):
    """Verify the EAC SHA256 log checksum (==== Log checksum ... ====).

    Returns (state, detail):
      state: 'ok' | 'invalid' | 'missing' | 'unsupported' | None (error)
      detail: human string or None

    EAC 1.0b1+ rip logs are UTF-16LE with BOM and carry a Rijndael checksum
    (pypi `eac-logchecker`). XLD / older EAC logs that never had a checksum
    return 'unsupported' so callers can treat them as PASS when the toggle is
    on (avoids false-failing XLD collections). A log that claims a checksum
    but fails verification returns 'invalid'.

    "Older EAC" is read off the log's OWN header: a version below 1.0 (the
    release the checksum arrived in, same boundary as the line above) is
    'unsupported' — nothing claimed, nothing refuted, exactly like XLD. The
    version is never guessed from the log's date, and a 1.x log whose
    checksum line is gone stays 'missing': that is a log edited after EAC
    signed it, and the state exists so the run log can say so.

    NEITHER 'missing' nor 'unsupported' is REQUIRED to be there: the rule the
    callers follow (spec R30) is "a checksum that is PRESENT must verify,
    an ABSENT one costs nothing" — so only 'invalid' fails a disc, and both
    absent states are reported rather than charged. 'missing' remains
    distinguishable from 'unsupported' so the warning names the right case.
    """
    try:
        if not log_path or not os.path.isfile(log_path):
            return (None, "not found")
        # Read raw to preserve BOM/encoding for eac-logchecker
        try:
            raw = open(log_path, "rb").read()
        except OSError as e:
            return (None, str(e)[:120])
        # Detect log flavour via decoded snippet
        txt = read_log_text(log_path)
        # XLD or non-EAC -> unsupported (no checksum to verify)
        if "X Lossless Decoder" in txt or txt.lstrip().startswith("XLD"):
            return ("unsupported", "XLD log has no EAC checksum")
        if "Exact Audio Copy" not in txt:
            # No EAC header -> no checksum concept
            if "==== Log checksum" not in txt:
                return ("unsupported", "no EAC header / no checksum")
        # Log claims a checksum?
        has_line = bool(re.search(r"====\s*Log checksum\s+[0-9A-Fa-f]+\s*====", txt))
        if not has_line:
            # A version that never wrote the line cannot be missing it: EAC
            # added the checksum in 1.0, so a 0.99-era log is 'unsupported'
            # (the spec's "older EAC logs pass") instead of FAKE — that
            # verdict failed every track of an honest 2008 rip. Read from the
            # header only: a 1.x log with the line deleted keeps failing.
            m_ver = re.search(r"Exact Audio Copy\s+v?(\d+)\.(\d+)", txt,
                              re.IGNORECASE)
            if m_ver and (int(m_ver.group(1)), int(m_ver.group(2))) < (1, 0):
                return ("unsupported",
                        f"EAC {m_ver.group(1)}.{m_ver.group(2)} predates "
                        f"log checksums")
            return ("missing", "no 'Log checksum' line")
        if not HAS_EAC_CHECKER or eac_logchecker is None:
            # Fallback to Logchecker PHP if eac-logchecker not installed
            try:
                from .tools import detect_all_tools
                tools = detect_all_tools()
                lc = tools.get("logchecker")
                php = tools.get("php")
                if lc and lc.get("phar_path") and (lc.get("php_exe") or (php and php.get("php_exe"))):
                    phar = lc.get("phar_path")
                    php_exe = lc.get("php_exe") or php.get("php_exe")
                    if os.path.isfile(phar) and php_exe and os.path.isfile(php_exe):
                        # The same environment score_disc_log runs with, so the
                        # phar finds its helper wherever it was installed and
                        # the PATH probe below asks about THAT PATH.
                        env = _logchecker_env()
                        proc = run_tool([php_exe, phar, "analyze", "--no_text", log_path],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace",
                                        timeout=15, env=env)
                        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
                        m = re.search(r"Checksum\s*:\s*(\w+)", out, re.IGNORECASE)
                        if m:
                            state = m.group(1).lower()
                            # `checksum_ok` is evidence only when the phar says
                            # it actually checked. Without its own EAC helper it
                            # prints the same word and a Notice — "Could not find
                            # EAC logchecker, checksum not validated" — so the
                            # answer there is 'unverified': the log CLAIMS a
                            # checksum and nothing checked it. Measured, on a
                            # real EAC 1.3 log with one digit of "Peak level"
                            # changed: Score 100, Checksum: checksum_ok — the
                            # edit goes through untouched, which is what made
                            # this branch claim 'ok' for a doctored log.
                            if state == "checksum_ok":
                                m_rip = re.search(r"Ripper\s*:\s*(\S+)", out,
                                                  re.IGNORECASE)
                                if (m_rip and m_rip.group(1).strip().lower()
                                        == "unknown"):
                                    # It carries a checksum line nothing can be
                                    # checked against (the phar could not tell
                                    # which ripper wrote it): the same
                                    # "claimed, never checked" reading as a
                                    # missing helper, not 'unsupported' — that
                                    # one belongs to a log with no checksum
                                    # concept at all.
                                    return ("unverified",
                                            "Logchecker could not identify the "
                                            "ripper, so the log's checksum was "
                                            "left unchecked")
                                if (re.search(r"checksum not validated", out,
                                              re.IGNORECASE)
                                        or not _eac_helper_on_path(env.get("PATH"))):
                                    return ("unverified",
                                            "Logchecker found no EAC log checker "
                                            "to validate the log's checksum, so "
                                            "its 'checksum_ok' is untested")
                                return ("ok", None)
                            if state in ("checksum_invalid", "checksum_error"):
                                return ("invalid", state)
                            if state in ("checksum_missing", "checksum_not_found"):
                                return ("missing", state)
                            return (state, out[:120])
            except Exception:
                pass
            return (None, "eac-logchecker not installed")
        # Use eac-logchecker (accurate, UTF-16LE aware)
        try:
            logs = eac_logchecker.get_logs(raw)  # handles BOM + \r\n -> \n
        except Exception as e:
            return (None, f"parse: {e}")
        if not logs:
            return ("missing", "eac_logchecker: no log found")
        # Usually single log per file
        for lg in logs:
            try:
                eac_logchecker.eac_verify(lg)
            except Exception as e:
                return (None, f"verify: {e}")
            if getattr(lg, "old_checksum", None) is None:
                return ("missing", "no stored checksum in log")
            if getattr(lg, "checksum", None) != lg.old_checksum:
                return ("invalid", f"expected {lg.old_checksum} computed {lg.checksum}")
            return ("ok", None)
        return ("missing", "no log")
    except Exception as e:
        return (None, str(e)[:160])


def grade_album_logs(cli_exe, album_dir, force=False, log_fn=None,
                     write_tags=True, config=None):
    """Rename logs/cues to CD-N (config-gated), fix CUE FILE names and
    write LOG_GRADE (0-100) to every track of MEDIA=CD albums, one score
    per disc.

    Returns ({disc: score}, notes list, unscorable list). *unscorable* holds
    the disc numbers whose .log could not be scored (missing or unreadable) as
    DATA: the audit fails exactly those discs, and used to recover the numbers
    by regexing this function's free-text notes. Callers that act on it should
    check discs.logchecker_available() first — with no scorer installed
    nothing was judged, so an empty-score run says nothing about the logs.
    """
    notes = []
    unscorable = []
    discs = album_discs(album_dir)
    if not discs:
        # Single-disc fallback for scoring: if enabled and single album with one log/cue,
        # treat as disc 1 so CD-1.log can be scored even without D-TT naming.
        if config is None or config.get("discs_rename_single_fallback", True):
            try:
                aud = [f for f in os.listdir(album_dir) if is_audio_file(f)]
                logs_tmp = [f for f in os.listdir(album_dir) if f.lower().endswith(".log")]
                cues_tmp = [f for f in os.listdir(album_dir) if f.lower().endswith(".cue")]
                if aud and (len(logs_tmp) == 1 or len(cues_tmp) == 1):
                    discs = {1: [os.path.join(album_dir, f) for f in aud]}
                else:
                    return {}, notes, unscorable
            except OSError:
                return {}, notes, unscorable
        else:
            return {}, notes, unscorable

    # CD-DA only (mlo.tagtext.is_cd_media: CD and its HDCD variant) — check
    # all discs first file, not arbitrary order
    first = None
    for d in sorted(discs.keys()):
        if discs[d]:
            first = discs[d][0]
            break
    if first is None:
        return {}, notes, unscorable
    af = AudioFile(first)
    if af.audio is None:
        return {}, notes, unscorable
    # The same one rule as every other MEDIA comparison here.
    if not is_cd_media(af.get_tag("MEDIA")):
        return {}, notes, unscorable

    # Fix FILE entries first (conservative) so the subsequent
    # FILE->disc mapping for renaming has correct references; .log
    # files are never modified — only renamed.
    fix_cue_filenames(album_dir, log_fn=log_fn, config=config)
    rename_logs_for_discs(album_dir, discs, log_fn=log_fn, config=config)
    rename_cues_for_discs(album_dir, discs, log_fn=log_fn, config=config)
    rename_accurip_for_discs(album_dir, discs, log_fn=log_fn, config=config)

    scores = {}
    pattern = _disc_pattern_for(config)
    handled_logs = set()
    for d, paths in sorted(discs.items()):
        log_name = _disc_expected_name(pattern, d, ".log")
        handled_logs.add(log_name.lower())
        log_path = os.path.join(album_dir, log_name)
        if not os.path.isfile(log_path):
            notes.append(f"disc {d}: no {_disc_expected_name(pattern, d, '.log')}")
            unscorable.append(d)
            continue
        if not force:
            have = []
            for p in paths:
                t = AudioFile(p)
                v = str(t.get_tag("LOG_GRADE") or "").strip()
                have.append(v)
            if have and all(v.isdigit() and 0 <= int(v) <= 100 for v in have):
                continue  # already graded
        score = score_disc_log(log_path)
        if score is None:
            notes.append(f"disc {d}: could not score {_disc_expected_name(pattern, d, '.log')} (Logchecker failed)")
            unscorable.append(d)
            continue
        scores[d] = score
        for p in paths:
            if not write_tags:
                continue
            if config is not None and not should_write_audio_tag(config, "LOG_GRADE", filepath=p):
                continue
            t = AudioFile(p)
            if str(t.get_tag("LOG_GRADE") or "").strip() != str(score):
                if t.set_tag("LOG_GRADE", str(score)):
                    if log_fn:
                        log_fn(f"disc {d}: LOG_GRADE={score} -> "
                               f"{os.path.basename(p)}")
                else:
                    notes.append(f"disc {d}: failed writing LOG_GRADE to "
                                 f"{os.path.basename(p)}")
    # Logs that do not map to a real disc are reported, never graded: their
    # score would be written to every audio file in the album (clobbering the
    # real per-disc grade) under an invented disc number that does not exist.
    try:
        for logf in sorted(os.listdir(album_dir)):
            if not logf.lower().endswith(".log"):
                continue
            if logf.lower() in handled_logs:
                continue
            dname = _log_name_disc(logf)
            if dname is not None and dname in discs:
                # Belongs to a real disc; the per-disc loop already noted it.
                continue
            notes.append(f"{logf}: not mapped to a disc - not graded")
    except OSError:
        pass
    return scores, notes, sorted(unscorable)
