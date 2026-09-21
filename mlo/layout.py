"""Library layout scan — is the music folder shaped the way the app expects?

The canonical library is ``<music>/Artists/<Artist>/<Album>/<files>``.
Everything else that holds audio, or that sits where no audio belongs, is
reported here. This is a READ-ONLY report: it says what is wrong and where,
and never moves anything on its own.

One scan answers all three surfaces, so their numbers cannot disagree:

  * script 20 (``run_scan_layout``) — the Run All step, which also persists
    what it found;
  * ``GET /api/library/layout`` (server/main.py) — the Optimization page's
    layout panel, scanned on demand;
  * ``GET /api/library/layout/report`` — the persisted report the Library
    page warns from, so its warning costs no second walk of the library.

A scan that found nothing is not the same as a scan that never ran. The
report file records when it ran (``scanned_at``) and which music folder it
describes, and :func:`load_report` marks a report of a DIFFERENT folder
stale, so the Library warning can never claim a scan that did not happen.
"""
import json
import os
import re
import tempfile
import time

from . import stats as mlo_stats
from .paths import app_data_dir, library_root
from .ui import Color, c, log, print_header

# --------------------------------------------------------------------------- #
# What the layout tolerates
# --------------------------------------------------------------------------- #
_ALBUM_SIDECARS = {".lrc", ".cue", ".log", ".accurip"}
# Folders allowed directly in the music folder. `.mlo*` covers the app's own
# state dirs; everything else is reported.
_ROOT_ALLOWED = {".mlo"}
# Disc folders are the one nesting the app creates and understands.
_DISC_RE = re.compile(r"^(cd|disc|disk)\s*\d+$", re.I)

# Where the last scan is kept: app state, next to config.json, so it travels
# with the music folder it describes and the library itself never gains a file.
REPORT_NAME = "layout_report.json"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _is_audio(name):
    from .paths import LIB_AUDIO_EXTS, LIB_VIDEO_EXTS
    ext = os.path.splitext(name)[1].lower()
    return ext in LIB_AUDIO_EXTS or ext in LIB_VIDEO_EXTS


def _list(d):
    """``(entries, error)`` for *d* — an unreadable folder is not an empty one.

    The scan reports what it can see, so a folder it may not list still has to
    be counted somewhere: silence would make it read exactly like a folder
    that holds nothing, and no issue row can say "I could not look" without
    guessing at the music that may be inside. The error is therefore carried
    back to the caller and accounted into the run's error count.
    """
    try:
        return os.listdir(d), ""
    except OSError as e:
        return [], str(e)


def _issue(kind, path, folder, detail, hint):
    """One report row. `path` is always music-folder-relative for display, so
    the UI never has to know the machine's absolute layout."""
    try:
        rel = os.path.relpath(path, folder)
    except ValueError:
        rel = path
    return {"kind": kind, "path": rel.replace("\\", "/"),
            "abs": path.replace("\\", "/"), "detail": detail, "hint": hint}


def _counts(issues):
    counts = {}
    for i in issues:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    return counts


def _has_audio(d):
    """Whether any audio sits directly in *d* or one/two levels down (the only
    nesting the layout uses: disc folders inside an album)."""
    from .paths import SKIP_DIRS
    for root, dirs, names in os.walk(d):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        if root.count(os.sep) - d.count(os.sep) > 2:
            dirs[:] = []
            continue
        if any(_is_audio(f) for f in names):
            return True
    return False


def _case_only(expected, actual):
    """Whether two names are the SAME name in different letter case.

    Case-only is the only kind of name mismatch the layout scanner reports.
    Anything else — a different album, a different title — is a naming
    problem and belongs to the grader's PATH check; repeating it here would
    only give the user two rows for one thing, and a guess about paths is
    what the scanner must never make.
    """
    return (bool(expected) and expected != actual
            and expected.casefold() == actual.casefold())


def _track_tags(path):
    """The album probe file's tags, or {} when they cannot be read.

    Through the server's tag cache — the same reader the library payload uses,
    so a scan right after a page load re-parses nothing. Imported lazily (and
    tolerated as missing): the CLI runs this scan too, and one unimportable
    optional module must cost the `wrong_case` comparison, not the scan.
    """
    try:
        from server import tagcache
        return tagcache.read_track(path)[0] or {}
    except Exception:
        return {}


def _case_issues(artist, album, album_dir, folder, script, seen):
    """`wrong_case` rows for one album folder, or [] when there is nothing
    trustworthy to compare against.

    The expected names come from the naming script the ORGANIZER applies —
    the same `naming_script` the grader evaluates — run over the album's own
    tags. That is the whole reason this is reportable at all: Windows is
    case-insensitive, so "abbey road" and "Abbey Road" open the same folder
    and nothing in the normal file API will ever admit the difference. What
    it does NOT do is lie about the spelling: `os.listdir` returns the name
    exactly as it is STORED on disk, and that stored spelling is what every
    comparison below reads. So a case-only mismatch shows up here precisely
    because the scanner looks at the raw directory entries instead of asking
    the OS whether two paths are "the same file" — it would always say yes.

    Tags are read from ONE audio file per album (the first one, through the
    tag cache), because the artist and album segments are album properties
    and the script's last segment is only compared against the file those
    tags came from. Any other file in the album is left alone: its expected
    name needs its own tags, and without them a comparison would be a guess.
    Unreadable tags, an empty tag dict or a script that evaluates to nothing
    all end in silence — a false "wrong case" here would push the user into
    renaming music to a name that is not actually correct.

    Cost ceiling: this reads tags for one file per album on every full scan.
    If scanning a large library ever gets slow, the cheap win is a prefilter
    — only read tags for albums whose folder or file names do not already
    contain the tag spelling — or a cached layout snapshot; both are more
    machinery than a read-only report currently earns.

    `seen` holds the absolute paths this scan already reported, so an artist
    folder shared by ten albums produces one row, not ten.
    """
    from .naming import eval_script, track_variables

    src = None
    for f in _list(album_dir)[0]:
        if _is_audio(f):
            src = f
            break
    if src is None:
        return []
    tags = _track_tags(os.path.join(album_dir, src))
    if not tags:
        return []
    release_type = str(tags.get("RELEASETYPE") or "").strip() or None
    expected = eval_script(script, track_variables(tags, release_type=release_type))
    if not expected:
        return []
    segs = [s for s in expected.replace("\\", "/").split("/") if s]
    if not segs:
        return []

    rows = []

    def add(path, expected_name, actual_name, what):
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return
        seen.add(key)
        rows.append(_issue(
            "wrong_case", path, folder,
            "%s \u201c%s\u201d differs from the naming script\u2019s \u201c%s\u201d "
            "in letter case only" % (what, actual_name, expected_name),
            "run Organize \u2014 it rewrites this to the script\u2019s exact "
            "casing (nothing is renamed by this scan)"))

    # The script's leading segments are directories — segment 0 is the artist
    # folder, segment 1 the album folder — and the last one is the file name
    # WITHOUT its extension (beets-style: the extension belongs to the file,
    # so it is appended from the file on disk, exactly as the grader does).
    # A shorter script simply names fewer things, and zip then compares only
    # what it does name.
    for seg, actual_name, path, what in zip(
            segs[:-1],
            (artist, album),
            (os.path.dirname(album_dir), album_dir),
            ("artist folder", "album folder")):
        if _case_only(seg, actual_name):
            add(path, seg, actual_name, what)
    expected_file = segs[-1] + os.path.splitext(src)[1]
    if _case_only(expected_file, src):
        add(os.path.join(album_dir, src), expected_file, src, "file")
    return rows


# --------------------------------------------------------------------------- #
# The scan
# --------------------------------------------------------------------------- #
def scan_library(cfg=None, stats=None):
    """Scan the whole music folder for misplaced files, unexpected folders and
    names spelled in the wrong letter case.

    Read-only. Checks the music-folder root, <music>/Artists, each artist
    folder, each album folder and the tree's depth — reporting every place the
    canonical `<music>/Artists/<Artist>/<Album>/<files>` shape is not met.

    What the app itself stores counts as expected, never as a stray: the
    album's description.txt (mlo.paths.ALBUM_SIDECAR_NAMES) next to the
    cover art, and inside an artist folder its artist.jpg / artist.png and
    description.txt (only audio with no album folder is reported there).

    *stats*, when given, is the runner's stats dict, because the payload below
    is the API's shape and must not grow a bookkeeping key. It accounts the
    entries the scan examined (`total_scanned`), then closes each one as
    unchanged (judged, clean), skipped (the layout has nothing to say about
    it: expected sidecars, the app's own state and manifests, disc folders) or
    an error (a folder that could not be listed). No entry is ever counted as
    modified — the scan writes no library file, only its own report under
    <music>/.mlo/data, which is app state rather than library content.
    """
    from .naming import DEFAULT_NAMING_SCRIPT
    from .paths import ALBUM_SIDECAR_NAMES, IMAGE_EXTS

    cfg = cfg or {}
    folder = str(cfg.get("music_folder") or "")
    # The canonical spellings the `wrong_case` check compares against: the
    # same script (same fallback) the organizer renames with and the grader
    # grades against.
    naming_script = (str(cfg.get("naming_script") or "").strip()
                     or DEFAULT_NAMING_SCRIPT)
    out = {"folder": folder.replace("\\", "/"), "artists_dir": "",
           "exists": False, "issues": [], "counts": {}, "total": 0,
           "albums": 0, "artists": 0, "audio_files": 0}
    if not folder or not os.path.isdir(folder):
        return out

    def entries(d):
        """One folder's entries, with a failed listing accounted as the error
        it is: an unreadable artist or album folder would otherwise look
        exactly like an empty one, and the report would say the library holds
        nothing there."""
        names, err = _list(d)
        if err and stats is not None:
            stats["error_count"] += 1
            stats["errors"].append((d, f"cannot list: {err}"))
        return names

    def opened():
        """One entry the scan looked at."""
        if stats is not None:
            stats["total_scanned"] += 1

    def closed(reported=False, skipped=False):
        """Close out one entry opened above: skipped when the layout does not
        judge it at all, otherwise unchanged unless it produced a row."""
        if stats is None:
            return
        if skipped:
            stats["skipped_count"] += 1
        elif not reported:
            stats["unchanged_count"] += 1

    lib = library_root(folder)
    out["exists"] = True
    out["artists_dir"] = (lib or "").replace("\\", "/")
    issues = []
    # Paths already reported as wrong_case — one artist folder serves all of
    # its albums, and the user does not need that row ten times.
    case_seen = set()

    # ---- 1. the music-folder root -----------------------------------------
    # Only Artists/ and the app's own .mlo state dirs belong here. A loose
    # audio file at the root is the classic "dropped it in the wrong place",
    # and a foreign folder is either a manual rip dump or a stray copy.
    for name in entries(folder):
        p = os.path.join(folder, name)
        if os.path.isdir(p):
            opened()
            if name in _ROOT_ALLOWED or name.startswith(".mlo"):
                closed(skipped=True)
                continue
            if lib and os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(lib)):
                closed(skipped=True)
                continue
            holds = _has_audio(p)
            issues.append(_issue(
                "unexpected_folder", p, folder,
                "folder in the music folder root%s" % (" holding audio" if holds else ""),
                "the library lives in Artists/ — move anything real into "
                "Artists/<Artist>/<Album>/"))
            closed(reported=True)
        elif _is_audio(name):
            opened()
            issues.append(_issue(
                "audio_at_root", p, folder, "audio file in the music folder root",
                "move it into Artists/<Artist>/<Album>/ (or import it) so "
                "grading and the organizer can see it"))
            closed(reported=True)
        elif name.startswith(".mlo_"):
            opened()
            issues.append(_issue(
                "legacy_state_file", p, folder,
                "leftover from the old .mlo_data layout",
                "safe to delete once the migration has been confirmed"))
            closed(reported=True)
        else:
            # Any other loose file at the root is not the layout's business:
            # the root also carries the user's own notes and scripts.
            opened()
            closed(skipped=True)

    if not lib or not os.path.isdir(lib):
        out.update(issues=issues, counts=_counts(issues), total=len(issues))
        return out

    # ---- 2. <music>/Artists ------------------------------------------------
    artist_names = entries(lib)
    for name in artist_names:
        p = os.path.join(lib, name)
        if not os.path.isdir(p):
            opened()
            if _is_audio(name):
                issues.append(_issue(
                    "audio_in_artists", p, folder,
                    "audio file directly in Artists/ (no artist or album folder)",
                    "move it into Artists/<Artist>/<Album>/"))
            else:
                issues.append(_issue(
                    "stray_in_artists", p, folder,
                    "non-audio file directly in Artists/",
                    "delete it, or move it into the album it belongs to"))
            closed(reported=True)
            continue
        if name.startswith("."):
            opened()
            issues.append(_issue(
                "hidden_folder", p, folder, "hidden folder inside Artists/",
                "hidden folders are not library content — move or delete it"))
            closed(reported=True)
            continue

        # ---- 3. <music>/Artists/<Artist> ----------------------------------
        for an in entries(p):
            ap = os.path.join(p, an)
            if not os.path.isdir(ap):
                opened()
                if _is_audio(an):
                    issues.append(_issue(
                        "audio_in_artist", ap, folder,
                        "audio file directly in the artist folder \u201c%s\u201d "
                        "(no album folder)" % name,
                        "give it an album folder: Artists/<Artist>/<Album>/"))
                    closed(reported=True)
                # Any other file in an artist folder is the artist's own
                # content (artist.jpg / artist.png / description.txt written
                # by mlo.artistdata) — expected, so nothing to report.
                else:
                    closed(skipped=True)
                continue
            # An album folder is one entry, opened when it is seen and closed
            # with whatever its contents produced: unchanged only when nothing
            # below answered for it. Counted by the rows themselves rather than
            # by a flag each branch has to remember to set — an album whose
            # stray file was reported is not an unchanged folder.
            opened()
            out["albums"] += 1
            rows_before = len(issues)
            if not _has_audio(ap):
                issues.append(_issue(
                    "empty_album", ap, folder,
                    "album folder \u201c%s / %s\u201d holds no audio" % (name, an),
                    "remove it, or fill it \u2014 an empty album grades as an error"))
            # Letter-case drift: the folder or file is in the right PLACE but
            # spells its name the way the filesystem let somebody type it,
            # not the way the naming script spells it. Reported next to the
            # shape problems because from here it is the same kind of answer:
            # "this is not the canonical library yet".
            issues.extend(_case_issues(name, an, ap, folder, naming_script, case_seen))

            # ---- 4. inside an album: strays and unexpected subfolders ------
            for f in entries(ap):
                fp = os.path.join(ap, f)
                opened()
                if os.path.isdir(fp):
                    if _DISC_RE.match(f):
                        closed(skipped=True)
                    else:
                        issues.append(_issue(
                            "unexpected_subfolder", fp, folder,
                            "folder \u201c%s\u201d inside album \u201c%s / %s\u201d"
                            % (f, name, an),
                            "only disc folders (CD1, Disc 2, \u2026) belong "
                            "inside an album"))
                        closed(reported=True)
                    continue
                ext = os.path.splitext(f)[1].lower()
                if _is_audio(f):
                    out["audio_files"] += 1
                    closed()
                elif ext and ext in _ALBUM_SIDECARS:
                    closed(skipped=True)      # .lrc/.cue/.log/.accurip
                elif ext in IMAGE_EXTS:
                    closed(skipped=True)      # cover art
                elif f.lower() in ALBUM_SIDECAR_NAMES:
                    closed(skipped=True)      # album description.txt
                elif f.startswith("."):
                    closed(skipped=True)      # the app's own manifests
                else:
                    issues.append(_issue(
                        "stray_file", fp, folder,
                        "file \u201c%s\u201d is not audio, artwork or a known "
                        "sidecar" % f,
                        "delete it if it is junk (nfo/db/txt) — it is dead "
                        "weight in the library"))
                    closed(reported=True)
            closed(reported=len(issues) > rows_before)

    out["artists"] = sum(1 for n in artist_names
                         if os.path.isdir(os.path.join(lib, n)) and not n.startswith("."))
    out.update(issues=issues, counts=_counts(issues), total=len(issues))
    return out


# --------------------------------------------------------------------------- #
# Persistence — the last scan, for the Library page's warning
# --------------------------------------------------------------------------- #
def report_path(cfg=None):
    """``<music>/.mlo/data/layout_report.json``, or "" when no music folder is
    configured (there is no library whose last scan it could describe)."""
    folder = str((cfg or {}).get("music_folder") or "")
    if not folder:
        return ""
    return os.path.join(app_data_dir(folder), REPORT_NAME)


def _same_folder(a, b):
    """Whether two music-folder spellings name the same folder."""
    if not a or not b:
        return False
    try:
        return (os.path.normcase(os.path.realpath(a))
                == os.path.normcase(os.path.realpath(b)))
    except OSError:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def save_report(cfg, report):
    """Store *report* with the moment it ran. Returns the path, or "" when it
    could not be stored — a scan that cannot be remembered must still not fail.

    Atomic (temp file + `os.replace`), like the audit evidence map: the Library
    page reads this file while a run may be rewriting it, and half a JSON
    document would read as "no scan ever ran"."""
    path = report_path(cfg)
    if not path:
        return ""
    payload = {"scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "music_folder": str((cfg or {}).get("music_folder") or "").replace("\\", "/"),
               "report": report}
    tmp = None
    try:
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".layout_report_", suffix=".json", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f)
            try:
                f.flush()
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        return path
    except OSError:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return ""


def load_report(cfg=None):
    """The stored report as
    ``{"exists", "scanned_at", "music_folder", "stale", "report"}``.

    *exists* is False for "no scan has run yet" and for an unreadable file — a
    warning built on this must never appear for a scan that did not happen.

    *stale* is about the LIBRARY, not the clock: a report describing a music
    folder other than the one configured now answers for a library that is no
    longer this one, so nothing may be claimed from it. Age alone is not
    staleness — the app cannot know whether the folder changed since without
    walking it again, which is the very cost this file exists to avoid — so
    the moment it ran (`scanned_at`) and the folder it looked at
    (`music_folder`) travel with it, and the reader shows both.
    """
    empty = {"exists": False, "scanned_at": None, "music_folder": None,
             "stale": False, "report": None}
    path = report_path(cfg)
    if not path or not os.path.isfile(path):
        return empty
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("report"), dict):
        return empty
    recorded = str(data.get("music_folder") or "")
    current = str((cfg or {}).get("music_folder") or "").replace("\\", "/")
    return {"exists": True,
            "scanned_at": str(data.get("scanned_at") or "") or None,
            "music_folder": recorded or None,
            "stale": bool(recorded) and not _same_folder(recorded, current),
            "report": data["report"]}


# --------------------------------------------------------------------------- #
# Script 20
# --------------------------------------------------------------------------- #
def run_scan_layout(config):
    """Script 20 — scan the library and remember what it found.

    Whole-library on purpose, even when ``config["targets"]`` names a
    selection: the report is ONE object describing the shape of the music
    folder, and a partial scan stored as "the last scan" would let the Library
    page warn — or stay silent — about a library whose other half was never
    looked at.

    Read-only: nothing is moved, renamed, deleted or re-tagged. The only file
    it writes is its own report under <music>/.mlo/data.

    No progress bar: the walk lists directories and reads one file's tags per
    album through the app's tag cache, so its total is not known before it is
    already over — a bar could only announce a number it does not have.
    """
    config = config or {}
    stats = mlo_stats.new_stats()
    print_header("Scan library layout")
    folder = str(config.get("music_folder") or "")
    log(f"music folder: {folder or '(not set)'} \u00b7 read-only")

    report = scan_library(config, stats)
    if not report["exists"]:
        log(c("music folder is not set or does not exist \u2014 nothing to scan",
              Color.YELLOW))
        return stats

    counts = report["counts"]
    for kind in sorted(counts):
        log(f"  {counts[kind]} \u00d7 {kind}")
    for row in report["issues"]:
        log(f"    [{row['kind']}] {row['path']} \u2014 {row['detail']}")

    stats["layout_total"] = report["total"]
    stats["layout_counts"] = dict(counts)
    path = save_report(config, report)
    stats["report_path"] = path
    if path:
        log(f"report: {path}")
    else:
        # The scan itself succeeded; only remembering it failed. Said out loud
        # because the Library page will then keep warning from the PREVIOUS
        # report, which is not what this run just found.
        log(c("could not write the layout report \u2014 the Library warning "
              "stays on the last one that was stored", Color.YELLOW))
        stats["error_count"] += 1
        stats["errors"].append((folder, "layout report write failed"))

    log(c(f"layout: {report['total']} problem(s) in {len(counts)} "
          f"categor{'y' if len(counts) == 1 else 'ies'} \u00b7 "
          f"{report['audio_files']} audio file(s) \u00b7 "
          f"{report['albums']} album(s) \u00b7 {report['artists']} artist(s)",
          Color.GREEN if not report["total"] else Color.YELLOW))
    return stats
