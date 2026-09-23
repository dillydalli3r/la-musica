"""Library layout scan and fix — is the music folder shaped the way the app
expects, and can a script put it back?

The canonical library is ``<music>/Artists/<Artist>/<Album>/<files>``.
Everything else that holds audio, or that sits where no audio belongs, is
reported here.

The SCAN moves nothing: it says what is wrong and where. :func:`apply_fixes`
then fixes the three things that are unambiguous — a name spelled in the wrong
letter case, audio that is not in an album folder at all, and an artist folder
with no album under it — and reports every other row as left alone. Nothing is
ever deleted: a folder it removes goes to the app's own Trash, and a file whose
album cannot be read from its own tags is named, never guessed at.

One scan answers every surface, so their numbers cannot disagree:

  * script 20 (``run_scan_layout``) — the Run All step: scans, applies (the
    ``layout_apply`` config key / the runner's force flag) and persists what is
    left;
  * ``POST /api/library/layout/apply`` (server/main.py) — the same scan and
    fix on demand, run by the Optimization page's Apply fixes button;
  * ``GET /api/library/layout`` — the panel's read-only scan, which reports
    and changes nothing;
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
from .paths import app_data_dir, library_root, move_path, trash_path
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

# What the apply phase may act on, and why each remaining kind is left alone.
# The split is the whole safety argument of this module: a fix exists only
# where the answer is already proven (letter case, the file's own tags, a
# folder with no music under it). Everything else — a foreign folder, junk in
# an album, a leftover state file — would need a judgement about somebody
# else's files, so it is REPORTED and nothing more. Never deleted: the one
# removal goes through the app's Trash.
_UNFIXABLE = {
    "unexpected_folder": "a foreign folder in the music folder root — where its "
                         "contents belong is the user's call",
    "unexpected_subfolder": "only disc folders belong inside an album — nothing "
                            "here can name the album that folder belongs to",
    "empty_album": "an album folder holding no audio — removing it is the "
                   "user's call",
    "stray_file": "not audio, artwork or a known sidecar — deleting it is the "
                  "user's call",
    "stray_in_artists": "not audio — nothing can say which album it belongs to",
    "hidden_folder": "hidden folders are not library content — moving or "
                     "deleting it is the user's call",
    "legacy_state_file": "leftover app state from the old layout — deleting it "
                         "is the user's call",
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _is_audio(name):
    from .paths import LIB_AUDIO_EXTS, LIB_VIDEO_EXTS
    ext = os.path.splitext(name)[1].lower()
    return ext in LIB_AUDIO_EXTS or ext in LIB_VIDEO_EXTS


def _is_disc_structure(path):
    """Whether *path* is a disc structure folder the app understands: a
    ``VIDEO_TS`` (DVD-Video) or ``BDMV`` (Blu-ray) holding the files of one.

    The name alone is not enough — an empty folder called ``VIDEO_TS`` is junk,
    and reporting it is the layout's job — so the shape is confirmed by
    mlo.videodisc, the same recognition the remux picks a feature with. What
    the scan does with a confirmed structure is NOT report it: a rip's own
    folder is the shape a disc comes in, not a stray folder.
    """
    from .paths import is_video_disc_dir

    if not is_video_disc_dir(path):
        return False
    from . import videodisc

    # honey: the NAME is not the answer — an empty folder called VIDEO_TS is
    # junk and the layout's job is to say so, while a real rip's folder is the
    # shape a disc comes in. Confirming the shape costs one listdir (and the
    # playlist read only for a BDMV), and it is the same recognition the remux
    # picks a feature with, so the two can never disagree about what a disc is.
    return videodisc.recognize(path) is not None


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


def _issue(kind, path, folder, detail, hint, fix=None):
    """One report row. `path` is always music-folder-relative for display, so
    the UI never has to know the machine's absolute layout.

    *fix*, when given, is what :func:`apply_fixes` will do about this row —
    ``{"action": "rename"|"move"|"trash", "to": <absolute path>}`` — worked
    out HERE, where the scan already knows the canonical name or read the
    tags that name the album. A row without one is a report and nothing else:
    the scan found nothing that may act on it, so nothing will.
    """
    try:
        rel = os.path.relpath(path, folder)
    except ValueError:
        rel = path
    row = {"kind": kind, "path": rel.replace("\\", "/"),
           "abs": path.replace("\\", "/"), "detail": detail, "hint": hint}
    if fix:
        row["fix"] = fix
    return row


def _within(path, root):
    """Whether *path* is *root* itself or sits inside it.

    The apply's guard, asked about every source AND every destination before
    it moves anything: a row names paths from a walk of the music folder, but
    a fix that trusted them would be the one bug with consequences, so the
    check is made at the move and not at the row. Case-insensitive and
    boundary-aware ("C:/Music" and "c:/music" are one place; "C:/Music2" is
    not inside "C:/Music").
    """
    if not path or not root:
        return False
    p = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(os.path.abspath(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def _counts(issues):
    counts = {}
    for i in issues:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    return counts


def _new_sink():
    """The counters ONE walker keeps while it works.

    A sink carries the four keys the run's stats are built from, so a lane's
    answer folds back with plain addition, plus the two the report itself
    carries (``albums``, ``audio_files``). The runner thread's sink IS the
    run's own ``stats`` dict — same key names, which is why the walk's
    accounting helpers can take either — and a lane gets a fresh one of these,
    so several artist folders can be visited at once without one lane touching
    another's numbers (see :func:`scan_library`'s merge, the only writer).
    """
    return {"total_scanned": 0, "skipped_count": 0, "unchanged_count": 0,
            "error_count": 0, "errors": [], "albums": 0, "audio_files": 0}


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


def artist_album_folders(artist_dir):
    """The album folders directly inside an artist folder, by name.

    An album folder is a DIRECTORY the artist folder holds. Files are not
    albums: an artist folder with no albums holds the artist's own artist.jpg
    and description.txt and nothing else, which is exactly the `empty_artist`
    shape. This is the ONE definition of the question — the scan's finding,
    the artist grade (mlo.grader.grade_artist) and the remove-empty-artist
    route all ask it, so a folder one of them refuses to act on is never one
    another reports as removable.
    """
    try:
        return sorted(n for n in os.listdir(artist_dir)
                      if os.path.isdir(os.path.join(artist_dir, n)))
    except OSError:
        return []


def empty_artist(artist_dir) -> bool:
    """Whether *artist_dir* is the scan's `empty_artist` finding.

    True when the artist folder holds no album folder AND no audio anywhere
    beneath it. A folder holding ANY audio is never this finding: that audio is
    a different problem (audio_in_artist, audio_in_artists, empty_album), and
    the removal this finding offers must never be offered for a folder that
    holds music.
    """
    if not os.path.isdir(artist_dir) or artist_album_folders(artist_dir):
        return False
    return not _has_audio(artist_dir)


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


def _loose_fix(path, folder, script):
    """Where a loose audio file belongs, from its OWN tags — or None.

    The naming script the organizer renames with, run over the file's own
    tags, names the artist and album folders it belongs in, and that is the
    only answer that is not a guess: a file sitting in an artist folder (or in
    the music folder root) carries in itself the album it is from. No readable
    tags, or a script that names no album folder at all, means None — the file
    is reported and left exactly where it is, because moving music to a folder
    that is not its own is worse than leaving it one level up.

    The file keeps its own name: titling files is the organizer's job. Only a
    case-only difference is corrected on the way in, because the scan has
    already PROVED that spelling is the script's, and a fix that dropped a
    file into the canonical folder under a name the next scan reports as wrong
    would be half a fix.
    """
    from .naming import eval_script, track_variables

    base = library_root(folder)
    if not base:
        return None
    tags = _track_tags(path)
    if not tags:
        return None
    release_type = str(tags.get("RELEASETYPE") or "").strip() or None
    try:
        expected = eval_script(script, track_variables(tags, release_type=release_type))
    except Exception:
        return None
    segs = [s for s in str(expected or "").replace("\\", "/").split("/") if s]
    if len(segs) < 2:
        return None
    dst_dir = os.path.normpath(os.path.join(base, *segs[:-1]))
    # The script is user-editable: a "/.." in it must not aim a move outside
    # the library, and the check is the same one the move itself gets.
    if not _within(dst_dir, base):
        return None
    name = os.path.basename(path)
    if _case_only(segs[-1] + os.path.splitext(name)[1], name):
        name = segs[-1] + os.path.splitext(name)[1]
    return {"action": "move", "to": os.path.join(dst_dir, name)}


def _scope(cfg, folder):
    """What a SCOPED run visits: ``{artist folder: album folder names}``.

    ``None`` means the whole library — the empty target list, and a target
    that can only mean the whole library (the music folder itself, or
    ``Artists/``). ``{}`` means the caller named targets and none of them are
    in the library: nothing is looked at, which is the safe reading of "only
    these".

    The import chain runs script 20 with the album folder it just wrote, one
    call per album, and confinement is what makes the apply safe to run
    unattended: the scan reports that album and the apply fixes that album —
    never a second artist's folder that happened to be mis-cased. An album's
    canonical path is its artist folder above it and itself, so the artist
    folder is visited too, with this one album in it. A target deeper than an
    album (a disc folder, a file) picks the album it sits in.

    ``config["targets"]`` holds whatever the caller selected — album folders
    from an import, tracks from the library view — so a file target means the
    album it is in, and anything outside the music folder is simply not ours
    to look at.
    """
    raw = (cfg or {}).get("targets") or []
    if not raw or not folder:
        return None
    lib = library_root(folder)
    if not lib:
        return None
    scope = {}
    for target in raw:
        p = os.path.normpath(str(target))
        if not _within(p, folder):
            continue                       # outside the music folder: not ours
        if os.path.isfile(p):
            p = os.path.dirname(p)
        if _within(lib, p):
            # The music folder root, or Artists/ itself: there is no smaller
            # subject than the library, which is what an empty list means too.
            return None
        if not _within(p, lib) or not os.path.isdir(p):
            continue                       # else in the music folder, not the library
        # The artist folder is the direct child of Artists/ that holds the
        # target; the album is what the target sits in below it.
        parts = os.path.relpath(p, lib).split(os.sep)
        artist_dir = os.path.join(lib, parts[0])
        albums = scope.get(artist_dir, set())
        if len(parts) == 1:
            scope[artist_dir] = None          # the artist folder itself
        elif albums is not None:
            # Named albums accumulate; None means the artist is already the
            # subject whole, and one album does not narrow that.
            albums.add(parts[1])
            scope[artist_dir] = albums
    return scope


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
            "casing; Apply fixes renames the name itself",
            # Case-only is the one name mismatch that can be fixed without
            # knowing anything the scan has not already read: the expected
            # name is the same name, so the target is the row's own folder
            # plus the script's spelling of it.
            fix={"action": "rename",
                 "to": os.path.join(os.path.dirname(path), expected_name)}))

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
    """Scan the music folder for misplaced files, unexpected folders and names
    spelled in the wrong letter case.

    Read-only: it reports, and moves nothing (``apply_fixes`` is what acts on
    a report). Checks the music-folder root, <music>/Artists, each artist
    folder, each album folder and the tree's depth — reporting every place the
    canonical `<music>/Artists/<Artist>/<Album>/<files>` shape is not met —
    and works out, for each row it can, WHERE that row's file belongs, so the
    fix and the report are one answer about one file.

    ``config["targets"]`` confines the walk to what the caller named (see
    :func:`_scope`): the import chain runs this per album folder, so the rows
    it gets are the rows about that album and the artist folder it sits in.
    No targets means the whole library.

    What the app itself stores counts as expected, never as a stray: the
    album's description.txt (mlo.paths.ALBUM_SIDECAR_NAMES) next to the
    cover art, and inside an artist folder its artist.jpg / artist.png and
    description.txt (only audio with no album folder is reported there).

    The artist folders are visited on a pool (`worker_count`, so the Worker
    threads setting sizes it): they share nothing but the music folder they
    sit in, and the expensive part of a whole-library scan is the one
    container read per album the `wrong_case` check needs to know what the
    naming script would have spelled. Lanes return their rows and their
    counters and the runner merges them in the walk's own order — see the
    plan/merge pair below.

    *stats*, when given, is the runner's stats dict, because the payload below
    is the API's shape and must not grow a bookkeeping key. It accounts the
    entries the scan examined (`total_scanned`), then closes each one as
    unchanged (judged, clean), skipped (the layout has nothing to say about
    it: expected sidecars, the app's own state and manifests, disc folders) or
    an error (a folder that could not be listed). No entry is counted as
    modified here — the scan writes no library file, only its own report under
    <music>/.mlo/data, which is app state rather than library content; the
    modifying half is :func:`apply_fixes`, which counts what it changed.
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

    def entries(d, sink):
        """One folder's entries, with a failed listing accounted as the error
        it is: an unreadable artist or album folder would otherwise look
        exactly like an empty one, and the report would say the library holds
        nothing there.

        *sink* is the counter dict of the walker asking — the run's ``stats``
        for the runner thread, a lane's own :func:`_new_sink` for an artist
        folder the pool is visiting. Each walker counts only what IT looked
        at, so lanes never share a counter; the merge in :func:`scan_library`
        is what folds them back into the run.
        """
        names, err = _list(d)
        if err and sink is not None:
            sink["error_count"] += 1
            sink["errors"].append((d, f"cannot list: {err}"))
        return names

    def opened(sink):
        """One entry the scan looked at."""
        if sink is not None:
            sink["total_scanned"] += 1

    def closed(sink, reported=False, skipped=False):
        """Close out one entry opened above: skipped when the layout does not
        judge it at all, otherwise unchanged unless it produced a row."""
        if sink is None:
            return
        if skipped:
            sink["skipped_count"] += 1
        elif not reported:
            sink["unchanged_count"] += 1

    lib = library_root(folder)
    out["exists"] = True
    out["artists_dir"] = (lib or "").replace("\\", "/")
    issues = []
    # What this run looks at: None = the whole library, else the artist
    # folders a target named (see _scope). A scoped run skips the two
    # library-wide steps below — a foreign folder in the root and a stray
    # file in Artists/ are nobody's album, and an import fixing one album
    # must not be the thing that reports them.
    scope = _scope(cfg, folder)

    # ---- 1. the music-folder root -----------------------------------------
    # Only Artists/ and the app's own .mlo state dirs belong here. A loose
    # audio file at the root is the classic "dropped it in the wrong place",
    # and a foreign folder is either a manual rip dump or a stray copy.
    for name in (entries(folder, stats) if scope is None else []):
        p = os.path.join(folder, name)
        if os.path.isdir(p):
            opened(stats)
            if name in _ROOT_ALLOWED or name.startswith(".mlo"):
                closed(stats, skipped=True)
                continue
            if lib and os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(lib)):
                closed(stats, skipped=True)
                continue
            holds = _has_audio(p)
            issues.append(_issue(
                "unexpected_folder", p, folder,
                "folder in the music folder root%s" % (" holding audio" if holds else ""),
                "the library lives in Artists/ — move anything real into "
                "Artists/<Artist>/<Album>/"))
            closed(stats, reported=True)
        elif _is_audio(name):
            opened(stats)
            issues.append(_issue(
                "audio_at_root", p, folder, "audio file in the music folder root",
                "move it into Artists/<Artist>/<Album>/ (or import it) so "
                "grading and the organizer can see it",
                fix=_loose_fix(p, folder, naming_script)))
            closed(stats, reported=True)
        elif name.startswith(".mlo_"):
            opened(stats)
            issues.append(_issue(
                "legacy_state_file", p, folder,
                "leftover from the old .mlo_data layout",
                "safe to delete once the migration has been confirmed"))
            closed(stats, reported=True)
        else:
            # Any other loose file at the root is not the layout's business:
            # the root also carries the user's own notes and scripts.
            opened(stats)
            closed(stats, skipped=True)

    if not lib or not os.path.isdir(lib):
        out.update(issues=issues, counts=_counts(issues), total=len(issues))
        return out

    def artist(name, p, albums=None):
        """One artist folder: its loose files, its album folders and the
        folder itself.

        *albums* restricts the visit to those album folder NAMES — what a
        scoped run passes, so an import scans the album it wrote and no other
        album of the same artist. None means every album folder there is.

        ``(rows, sink)`` comes back instead of the rows going straight into
        the report: this is the unit :func:`scan_library` runs one lane per
        artist folder, so the rows it found and the entries it counted are its
        own. Nothing under an artist folder can be reported from another
        artist's walk, which is what makes that safe — and the only writer of
        the report is the merge that consumes these.
        """
        rows = []
        sink = _new_sink()
        # Paths already reported as wrong_case, for THIS artist: one artist
        # folder serves all of its albums and the user does not need that row
        # ten times. Only paths inside this folder can ever reach the set, so
        # one set per artist is the same dedupe a whole-library set did.
        case_seen = set()
        # ---- 3. <music>/Artists/<Artist> ----------------------------------
        for an in entries(p, sink):
            ap = os.path.join(p, an)
            if not os.path.isdir(ap):
                opened(sink)
                if _is_audio(an):
                    rows.append(_issue(
                        "audio_in_artist", ap, folder,
                        "audio file directly in the artist folder \u201c%s\u201d "
                        "(no album folder)" % name,
                        "give it an album folder: Artists/<Artist>/<Album>/",
                        fix=_loose_fix(ap, folder, naming_script)))
                    closed(sink, reported=True)
                # Any other file in an artist folder is the artist's own
                # content (artist.jpg / artist.png / description.txt written
                # by mlo.artistdata) — expected, so nothing to report.
                else:
                    closed(sink, skipped=True)
                continue
            if albums is not None and an not in albums:
                # A neighbouring album of a scoped run: listed, not looked at.
                opened(sink)
                closed(sink, skipped=True)
                continue
            # An album folder is one entry, opened when it is seen and closed
            # with whatever its contents produced: unchanged only when nothing
            # below answered for it. Counted by the rows themselves rather than
            # by a flag each branch has to remember to set — an album whose
            # stray file was reported is not an unchanged folder.
            opened(sink)
            sink["albums"] += 1
            rows_before = len(rows)
            if not _has_audio(ap):
                rows.append(_issue(
                    "empty_album", ap, folder,
                    "album folder \u201c%s / %s\u201d holds no audio" % (name, an),
                    "remove it, or fill it \u2014 an empty album grades as an error"))
            # Letter-case drift: the folder or file is in the right PLACE but
            # spells its name the way the filesystem let somebody type it,
            # not the way the naming script spells it. Reported next to the
            # shape problems because from here it is the same kind of answer:
            # "this is not the canonical library yet".
            rows.extend(_case_issues(name, an, ap, folder, naming_script, case_seen))

            # ---- 4. inside an album: strays and unexpected subfolders ------
            for f in entries(ap, sink):
                fp = os.path.join(ap, f)
                opened(sink)
                if os.path.isdir(fp):
                    if _DISC_RE.match(f) or _is_disc_structure(fp):
                        # A disc folder (CD1, Disc 2 …) and a disc STRUCTURE
                        # (VIDEO_TS/BDMV, the shape a DVD/Blu-ray rip comes in)
                        # are both the layout working as intended: the remux
                        # turns the structure into one MKV (mlo.videodisc).
                        closed(sink, skipped=True)
                    else:
                        rows.append(_issue(
                            "unexpected_subfolder", fp, folder,
                            "folder \u201c%s\u201d inside album \u201c%s / %s\u201d"
                            % (f, name, an),
                            "only disc folders (CD1, Disc 2, \u2026) belong "
                            "inside an album"))
                        closed(sink, reported=True)
                    continue
                ext = os.path.splitext(f)[1].lower()
                if _is_audio(f):
                    sink["audio_files"] += 1
                    closed(sink)
                elif ext and ext in _ALBUM_SIDECARS:
                    closed(sink, skipped=True)      # .lrc/.cue/.log/.accurip
                elif ext in IMAGE_EXTS:
                    closed(sink, skipped=True)      # cover art
                elif f.lower() in ALBUM_SIDECAR_NAMES:
                    closed(sink, skipped=True)      # album description.txt
                elif f.startswith("."):
                    closed(sink, skipped=True)      # the app's own manifests
                else:
                    rows.append(_issue(
                        "stray_file", fp, folder,
                        "file \u201c%s\u201d is not audio, artwork or a known "
                        "sidecar" % f,
                        "delete it if it is junk (nfo/db/txt) — it is dead "
                        "weight in the library"))
                    closed(sink, reported=True)
            closed(sink, reported=len(rows) > rows_before)

        # ---- 5. the artist folder itself -----------------------------------
        # An artist folder holding NO album folder at all is a dead artist:
        # nothing under it can be graded, the library still lists it, and the
        # artist's own image and description are the only things in it. A
        # folder with ANY audio anywhere beneath is never this finding — that
        # audio is a real problem the rows above already name
        # (audio_in_artist / audio_in_artists / empty_album) — which is what
        # empty_artist() decides, the same question the grade and the removal
        # route ask.
        if empty_artist(p):
            opened(sink)
            rows.append(_issue(
                "empty_artist", p, folder,
                "artist folder \u201c%s\u201d holds no album folder" % name,
                "remove it to the Trash (Optimize → Library layout → remove), "
                "or put one of the artist's albums inside it",
                fix={"action": "trash"}))
            closed(sink, reported=True)
        return rows, sink

    # ---- 2. <music>/Artists, and every folder under it --------------------
    # The walk is PLANNED first and merged after: an artist folder is its own
    # subject (nothing under one can be reported from another's walk) and its
    # rows are the rows a serial walk produced for it, so the pool below can
    # visit several at once without the report being able to tell. What it
    # overlaps is what this scan actually spends its time on: one container
    # read per album, to compare the stored spelling against the naming
    # script, plus the directory listings. `plan` holds the walk's own order
    # and `merge` is the ONE writer of `issues`/`stats`, so the rows, the
    # counters and the order between them are what they always were — the
    # apply rebases later fixes through the report's order (parent before
    # child, see apply_fixes), which a pool must not reshuffle.
    plan = []
    if scope is None:
        # ---- 2. <music>/Artists -------------------------------------------
        artist_names = entries(lib, stats)
        for name in artist_names:
            p = os.path.join(lib, name)
            if not os.path.isdir(p):
                opened(stats)
                if _is_audio(name):
                    rows = [_issue(
                        "audio_in_artists", p, folder,
                        "audio file directly in Artists/ (no artist or album folder)",
                        "move it into Artists/<Artist>/<Album>/",
                        fix=_loose_fix(p, folder, naming_script))]
                else:
                    rows = [_issue(
                        "stray_in_artists", p, folder,
                        "non-audio file directly in Artists/",
                        "delete it, or move it into the album it belongs to")]
                closed(stats, reported=True)
                plan.append(("rows", rows))
                continue
            if name.startswith("."):
                opened(stats)
                plan.append(("rows", [_issue(
                    "hidden_folder", p, folder, "hidden folder inside Artists/",
                    "hidden folders are not library content — move or delete it")]))
                closed(stats, reported=True)
                continue
            plan.append(("artist", (name, p, None)))
        out["artists"] = sum(1 for n in artist_names
                             if os.path.isdir(os.path.join(lib, n))
                             and not n.startswith("."))
    else:
        # A scoped run: exactly the artist folders a target named. Nothing
        # above them is judged — an album that is being fixed cannot be the
        # reason to report the root it happens to sit under.
        plan = [("artist", (os.path.basename(p), p, scope[p]))
                for p in sorted(scope)]
        out["artists"] = len(scope)

    def merge(rows, sink):
        """Fold one lane's answer into the report — the only writer."""
        issues.extend(rows)
        if stats is not None:
            for key in ("total_scanned", "skipped_count", "unchanged_count",
                        "error_count"):
                stats[key] += sink[key]
            stats["errors"].extend(sink["errors"])
        out["albums"] += sink["albums"]
        out["audio_files"] += sink["audio_files"]

    # Sized by the shared worker setting; one lane (a scoped run's single
    # album, or worker_limit=1) visits in-line, which is also the order every
    # report was written in before there was a pool.
    lanes = mlo_stats.worker_count(cfg, default=min(8, os.cpu_count() or 1),
                                   maximum=16, items=len(plan))
    if lanes > 1 and len(plan) > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=lanes) as ex:
            futures = [None if kind == "rows" else ex.submit(artist, *args)
                       for kind, args in plan]
            for (kind, args), fut in zip(plan, futures):
                if fut is None:
                    issues.extend(args)
                else:
                    merge(*fut.result())
    else:
        for kind, args in plan:
            if kind == "rows":
                issues.extend(args)
            else:
                merge(*artist(*args))

    out.update(issues=issues, counts=_counts(issues), total=len(issues))
    return out


# --------------------------------------------------------------------------- #
# The apply — what a script may put back
# --------------------------------------------------------------------------- #
def _rel(path, folder):
    """*path* for display: music-folder-relative, forward-slashed."""
    try:
        return os.path.relpath(path, folder).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _rebase(path, moved):
    """*path* as it is NOW, after the fixes already made.

    Renaming a folder rewrites the path of everything under it, and the rows
    behind it in the report still spell it the old way — on a case-sensitive
    filesystem that spelling no longer exists. So every later fix is rebased
    through what has moved since the scan. Boundary-aware and
    case-insensitive, like every other path comparison here; a case-only
    rename keeps the rest of the path exactly as the report spelled it.
    """
    if not path:
        return path
    p = os.path.normpath(path)
    key = os.path.normcase(p)
    for old, new in moved:
        old_norm = os.path.normpath(old)
        old_key = os.path.normcase(old_norm)
        if key == old_key:
            return new
        if key.startswith(old_key + os.sep):
            return new + p[len(old_norm):]
    return p


def _carry_entries(src_dir, dst_dir, names):
    """``(moved, left)`` for *names* traveling from *src_dir* into *dst_dir*.

    The one place the album's own files are moved with it, so every caller gets
    the same two promises: a name *dst_dir* already holds is NEVER overwritten
    (that file stays where it is and is reported instead — a cover search's pick
    must not eat the album's own artwork), and the move is ``move_path``, i.e. a
    rename whose bytes are never copied out of (``shutil.move`` degraded to a
    silent copytree when a file was still open: the album ended up in two
    places, see mlo.paths).
    """
    moved, left = [], []
    for name in names:
        src = os.path.join(src_dir, name)
        dst = os.path.join(dst_dir, name)
        if os.path.exists(dst):
            left.append(name)
            continue
        try:
            ok = move_path(src, dst)
        except Exception:
            ok = False
        (moved if ok else left).append(name)
    return moved, left


def _prune_emptied(src_dir, music_folder):
    """Remove *src_dir*, and the empty folders above it up to the library root.

    A mover takes everything an album folder held, so the folder itself is
    left as an audio-less shell — which the library scan reports as a broken
    album and the user has to remove by hand. The walk up stops AT the library
    root (never removing it) and only runs inside it: a folder outside the
    library is not ours to remove, and an unbounded climb up an empty chain
    could take a folder the user made.
    """
    lib = library_root(str(music_folder or "")) if music_folder else ""
    stop = os.path.normcase(os.path.abspath(lib)) if lib else ""
    d = os.path.abspath(src_dir)
    if stop and not os.path.normcase(d).startswith(stop + os.sep):
        return                          # outside the library: leave it alone
    try:
        os.rmdir(d)
    except OSError:
        return
    if not stop:
        return
    d = os.path.dirname(d)
    while d and os.path.normcase(d) != stop:
        try:
            os.rmdir(d)
        except OSError:
            return
        d = os.path.dirname(d)


def carry_album_files(src_dir, dst_dir, *, music_folder="", log=None):
    """Move the album's own files from the folder it LEFT to the folder it is IN.

    A mover takes the AUDIO and leaves everything else behind: script 14
    imports the tracks into the library and renames them, while the cover the
    import's cover step fetched BEFORE the chain ran, the description beside it
    and the expected-tracklist manifest stayed in the staging folder — so the
    album landed without artwork, the grader reported COVER, and the import was
    parked for a person over an album it had just found artwork for. This is the
    same rule the organizer applies when it renames an album ("move leftover
    album files (cover art etc.) to the new album root",
    ``server.main.organize``): the album's own files travel with it, at the
    album root, whatever moved the audio.

    Everything that is not audio goes, directories included — except a folder
    that still holds audio beneath it, which is not an artifact of the move but
    music the mover did not take (beets refuses a file it cannot read), and
    dragging it in would put un-imported tracks inside the album. Collisions and
    unreadable entries come back in ``left`` for the caller to say out loud;
    the emptied source folder is pruned (see :func:`_prune_emptied`).

    Returns ``(moved, left)`` — the names carried and the names left behind.
    """
    src_dir = os.path.normpath(str(src_dir or ""))
    dst_dir = os.path.normpath(str(dst_dir or ""))
    if not src_dir or not dst_dir or not os.path.isdir(dst_dir):
        return [], []
    if os.path.normcase(os.path.abspath(src_dir)) == \
            os.path.normcase(os.path.abspath(dst_dir)):
        return [], []
    try:
        names = sorted(os.listdir(src_dir))
    except OSError:
        return [], []
    carrying = []
    for name in names:
        if _is_audio(name):
            continue
        path = os.path.join(src_dir, name)
        if os.path.isdir(path) and mlo_stats._find_albums(path):
            continue
        carrying.append(name)
    moved, left = _carry_entries(src_dir, dst_dir, carrying)
    _prune_emptied(src_dir, music_folder)
    if log is not None and moved:
        try:
            log("carried the album's own files with it: " + ", ".join(moved[:6]))
        except Exception:
            pass
    return moved, left


def _track_companions(folder, name):
    """The files next to *name* that name IT — the organizer's sidecar rule.

    "01 - Song.jpg", "01 - Song.cover.jpg" and "01 - Song.lrc" belong to
    "01 - Song.flac": the exact stem, or the stem and a further qualifier. It is
    the same convention ``server.main.organize`` moves sidecars with and the one
    the grader's extra-artwork check accepts as tied to a track, so a file that
    survives one and fails the other cannot exist.
    """
    stem = os.path.splitext(name)[0].lower()
    out = []
    for entry in _list(folder)[0]:
        path = os.path.join(folder, entry)
        if os.path.isdir(path) or _is_audio(entry):
            continue
        estem = os.path.splitext(entry)[0].lower()
        if estem == stem or estem.startswith(stem + "."):
            out.append(entry)
    return sorted(out)


def carry_track_files(src_file, dst_file):
    """Move a loose track's own companions with it into the album it lands in.

    The apply files a track that sat loose in an artist folder into the album
    its tags name; its art and lyrics name the track and nothing else, so they
    go with it (``_track_companions``) instead of being left in the artist
    folder, where they are lost to the album and reported as that folder's
    sidecar on every later scan.

    Named by the track it carries for, and asked AFTER the track itself has
    moved (a companion of a file that never made it must not travel), so what
    has to exist here is the folder it is read from — the track is already in
    the album by now.

    Returns ``(moved, left)``, the same contract as :func:`carry_album_files`.
    """
    src_file, dst_file = os.path.normpath(str(src_file)), os.path.normpath(str(dst_file))
    src_dir, name = os.path.dirname(src_file), os.path.basename(src_file)
    dst_dir = os.path.dirname(dst_file)
    if not src_dir or not dst_dir or not os.path.isdir(src_dir):
        return [], []
    if os.path.normcase(os.path.abspath(src_dir)) == \
            os.path.normcase(os.path.abspath(dst_dir)):
        return [], []
    return _carry_entries(src_dir, dst_dir, _track_companions(src_dir, name))


def _perform(row, intent, folder, lib, user, moved):
    """``(result, words)`` for one row's fix; the actual move is here and
    nowhere else, so the guards below are the only way a file can be touched.
    """
    src = _rebase(row["abs"], moved)
    action = intent.get("action")

    if action == "trash":
        if not _within(src, lib):
            return "failed", "%s is outside the library" % row["path"]
        # The removal is re-derived from the folder itself, exactly as the
        # route does: only a folder with no album and no audio may go, so a
        # folder that gained music since the scan is refused, not moved.
        if not empty_artist(src):
            return "failed", ("%s is no longer an artist folder without "
                              "albums — it holds an album or audio now"
                              % row["path"])
        dest = trash_path(src, folder, user)
        if not dest:
            return "failed", ("could not move %s to the Trash — a file inside "
                              "it is still in use (stop playback and retry)"
                              % row["path"])
        return "fixed", ("moved the album-less artist folder %s to the Trash "
                         "(%s)" % (row["path"], _rel(dest, folder)))

    dst = _rebase(intent.get("to") or "", moved)
    if not src or not dst:
        return "failed", "nothing in this row can be moved"
    # The guard that matters: both ends of every move are inside the library,
    # whatever the row or a hand-edited naming script said.
    if not _within(src, folder):
        return "failed", "%s is outside the music folder" % row["path"]
    if not _within(dst, lib):
        return "failed", ("%s would land outside Artists/ — left where it is"
                          % row["path"])
    if not os.path.exists(src):
        return "failed", "%s is no longer there" % row["path"]
    # A destination that already holds something is REPORTED, never
    # overwritten — the one way a fix could lose a file. The exception is a
    # rename onto its own name in another case, which is the fix itself.
    same = (os.path.normcase(os.path.abspath(src))
            == os.path.normcase(os.path.abspath(dst)))
    if os.path.exists(dst) and not same:
        return "failed", ("%s already exists — %s was left as it is"
                          % (_rel(dst, folder), row["path"]))
    if not move_path(src, dst):
        return "failed", ("%s could not be moved — a file inside it is still "
                          "in use (stop playback and retry)" % row["path"])
    moved.append((src, dst))
    if action == "rename":
        return "fixed", "renamed %s to \u201c%s\u201d" % (
            row["path"], os.path.basename(dst))
    # A loose track is filed into its album: its own art and lyrics go with it
    # (the organizer's own sidecar rule), so nothing that names this track is
    # left in the artist folder it came from.
    carried, _left = carry_track_files(src, dst)
    with_it = (" with %d file(s) that belong to it" % len(carried)) if carried else ""
    if os.path.basename(src) != os.path.basename(dst):
        return "fixed", "moved %s into %s as \u201c%s\u201d (letter case only)%s" % (
            row["path"], _rel(os.path.dirname(dst), folder), os.path.basename(dst),
            with_it)
    return "fixed", "moved %s into %s%s" % (
        row["path"], _rel(os.path.dirname(dst), folder), with_it)


def apply_fixes(cfg=None, report=None, stats=None, user=None):
    """Fix what *report* found. Returns the report, with the outcome of every
    row it acted on — and of every row it did not.

    *report* defaults to a fresh scan. Rows carrying a ``fix`` (the scan puts
    one on each row that can be fixed) are carried out: a name spelled in the
    wrong letter case is renamed to the script's spelling, audio that is not
    in an album folder is moved into the one its own tags name, and an artist
    folder with no album goes to the app's Trash. Every other row is reported
    as left alone, with the reason — the report says what happened to the
    library, both ways.

    The report gains ``fixes`` (one row per outcome: ``kind``, ``path``,
    ``result`` = fixed|failed|skipped, and ``action`` in words), the counts
    ``fixed``, ``fix_failed`` and ``skipped``, and drops the rows whose fix
    succeeded: what is left in ``issues`` is what a scan would find NOW, which
    is what the Library page's warning has to be about. ``issues``,
    ``counts`` and ``total`` keep their names and their meaning.

    *user* is the Trash bin to use (``mlo.paths.trash_dir``); None means the
    install's own scope, which is what a script run carries (see mlo.flac's
    converted originals). Only files inside the music folder are ever touched,
    and nothing is deleted.
    """
    cfg = cfg or {}
    folder = str(cfg.get("music_folder") or "")
    if report is None:
        report = scan_library(cfg, stats)
    if user is None:
        user = str(cfg.get("auth_username") or "")
    lib = library_root(folder)
    fixes = []
    moved = []
    # Renames first, then moves, then the Trash: a rename rewrites the path
    # under everything below it, and the loose file a move is about may be
    # headed for the very folder that rename just settled. On a case-sensitive
    # filesystem that order is what keeps one album folder from becoming two
    # ("Wish you were here" + a fresh "Wish You Were Here"). Stable sort, so
    # rows of one kind keep the walk's order — parent before child.
    _order = {"rename": 0, "move": 1, "trash": 2}
    rows = sorted(report.get("issues") or [],
                  key=lambda r: _order.get((r.get("fix") or {}).get("action"), 3))
    for row in rows:
        intent = row.get("fix") or {}
        if not intent.get("action"):
            fixes.append({"kind": row["kind"], "path": row["path"],
                          "result": "skipped",
                          "action": _UNFIXABLE.get(row["kind"])
                                    or "nothing here may move this file"})
            continue
        result, words = _perform(row, intent, folder, lib, user, moved)
        fixes.append({"kind": row["kind"], "path": row["path"],
                      "result": result, "action": words})
        if stats is not None:
            if result == "fixed":
                stats["modified_count"] += 1
            else:
                stats["error_count"] += 1
                stats["errors"].append((row["abs"], words))
    fixed = {(f["kind"], f["path"]) for f in fixes if f["result"] == "fixed"}
    if fixed:
        kept = [r for r in report["issues"]
                if (r["kind"], r["path"]) not in fixed]
        report["issues"] = kept
        report["counts"] = _counts(kept)
        report["total"] = len(kept)
    report["fixes"] = fixes
    report["fixed"] = sum(1 for f in fixes if f["result"] == "fixed")
    report["fix_failed"] = sum(1 for f in fixes if f["result"] == "failed")
    report["skipped"] = sum(1 for f in fixes if f["result"] == "skipped")
    return report


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
    """Script 20 — scan the library, fix what can be fixed, remember the rest.

    Scans and APPLIES by default: the run is what puts a library back in shape,
    not just what notices it is not. ``layout_apply`` (off = report only, the
    key the runner's force flag and the Settings tab write) is the switch.

    A run with targets fixes exactly those subtrees (see :func:`_scope`) and
    stores nothing: the report file is ONE object describing the shape of the
    music folder, and a partial scan stored as "the last scan" would let the
    Library page warn — or stay silent — about a library whose other half was
    never looked at.

    Nothing is deleted: a folder it cannot keep goes to the app's Trash, and a
    file whose album it cannot name from the file's own tags is left exactly
    where it is and reported instead. The only file it writes is its own
    report under <music>/.mlo/data.

    No progress bar: the walk lists directories and reads one file's tags per
    album through the app's tag cache, so its total is not known before it is
    already over — a bar could only announce a number it does not have.
    """
    config = config or {}
    stats = mlo_stats.new_stats()
    print_header("Scan library layout")
    folder = str(config.get("music_folder") or "")
    # The same answer scan_library works from (`_scope` is pure): a run that
    # was given targets looks at those and nothing else, and one that was not
    # is the whole-library run the stored report describes.
    scope = _scope(config, folder)
    apply_on = bool(config.get("layout_apply", True))
    log(f"music folder: {folder or '(not set)'} \u00b7 "
        + ("read-only" if not apply_on else "scan and fix")
        + (f" \u00b7 {len(scope)} target artist folder(s)" if scope is not None else ""))

    report = scan_library(config, stats)
    if not report["exists"]:
        log(c("music folder is not set or does not exist \u2014 nothing to scan",
              Color.YELLOW))
        return stats

    if apply_on:
        apply_fixes(config, report, stats)
        for f in report["fixes"]:
            if f["result"] == "fixed":
                log(c(f"  fixed    {f['action']}", Color.GREEN))
            elif f["result"] == "failed":
                log(c(f"  failed   {f['path']} \u2014 {f['action']}", Color.YELLOW))
        log(f"  {report['fixed']} fixed \u00b7 {report['fix_failed']} failed \u00b7 "
            f"{report['skipped']} left as they are (reported below)")
    else:
        log("apply: off (layout_apply) \u2014 this run reports and changes nothing")

    counts = report["counts"]
    for kind in sorted(counts):
        log(f"  {counts[kind]} \u00d7 {kind}")
    for row in report["issues"]:
        log(f"    [{row['kind']}] {row['path']} \u2014 {row['detail']}")

    stats["layout_total"] = report["total"]
    stats["layout_counts"] = dict(counts)
    stats["layout_fixed"] = report.get("fixed", 0)
    stats["layout_fix_failed"] = report.get("fix_failed", 0)
    if scope is not None:
        # A scoped run's report describes one album, not the library, so it is
        # never stored as "the last scan" — the Library page warns from that
        # file, and a run that proved one folder clean must not be able to say
        # the whole library is.
        stats["report_path"] = ""
        log("scoped run \u2014 the stored library-wide report is left as it is")
    else:
        path = save_report(config, report)
        stats["report_path"] = path
        if path:
            log(f"report: {path}")
        else:
            # The scan itself succeeded; only remembering it failed. Said out
            # loud because the Library page will then keep warning from the
            # PREVIOUS report, which is not what this run just found.
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
