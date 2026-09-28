"""Shared statistics, byte accounting, progress shims and file walking."""
import os
import threading

from .deps import tqdm
from .paths import LIB_AUDIO_EXTS, SKIP_DIRS, AUDIO_EXTS

# Pre-lowered once: _walk_files visits thousands of directories and used to
# rebuild this set at every level.
_SKIP_DIRS_LOWER = {d.lower() for d in SKIP_DIRS}

# What _walk_files records for a directory in its optional *dirs_out* scan:
# whether the walk found no file, files of another kind, or a matching one. The
# grader's empty-folder sweep needs both halves — a folder with no file at all
# under it and a folder whose audio is gone while its sidecars are not.
WALK_EMPTY = 0
WALK_FILES = 1
WALK_MATCHED = 2

BAR_OPTS = dict(
    dynamic_ncols=True,
    ascii=False,
    leave=True,
    bar_format=(
        "{l_bar}{bar}| {n_fmt}/{total_fmt} "
        "[{elapsed}<{remaining}, {rate_fmt}{postfix}]"
    ),
)


_write_lock = threading.Lock()


def new_stats():
    return {
        "total_scanned": 0,
        "modified_count": 0,
        "unchanged_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "total_bytes_added": 0,
        "total_bytes_removed": 0,
        "errors": [],
    }


def _diff_bytes(before_size, final_size, existing_dest_size=0):
    """
    Accurate byte accounting.

    before_size          = size of original source file
    existing_dest_size   = size of an existing destination file that will be
                           replaced/overwritten/deleted, if different from source
    final_size           = size of final output file
    """
    try:
        before = int(before_size) + int(existing_dest_size)
        after = int(final_size)
    except Exception:
        return 0, 0

    diff = before - after

    if diff >= 0:
        return diff, 0
    return 0, -diff


def _existing_size(path, avoid_path=None):
    try:
        if not path:
            return 0
        if avoid_path and os.path.normcase(os.path.normpath(path)) == os.path.normcase(os.path.normpath(avoid_path)):
            return 0
        # One syscall, not two: getsize already answers "missing" with the
        # OSError the old os.path.exists probe paid a whole extra stat for.
        return os.path.getsize(path)
    except OSError:
        return 0


def _safe_remove(path):
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# Progress reporting hook. Front-ends (e.g. the GUI) assign a callable
# receiving (done, total, description) while a runner is in flight.
progress_hook = None


class _HookPbar:
    """Minimal tqdm stand-in that forwards progress to progress_hook."""

    def __init__(self, total=None, desc="", unit="file"):
        self.total = total
        self.desc = desc
        self.done = 0

    def update(self, n=1):
        self.done += n
        self._fire()

    def refresh(self):
        self._fire()

    def set_postfix(self, *args, **kwargs):
        pass

    def close(self):
        pass

    def _fire(self):
        if progress_hook is not None:
            try:
                progress_hook(self.done, self.total, self.desc)
            except Exception:
                pass


def _make_pbar(total, desc, unit="file"):
    if tqdm:
        return tqdm(total=total, desc=desc, unit=unit, **BAR_OPTS)
    return _HookPbar(total, desc, unit)


def _pbar_skip(pbar, counts):
    # A skipped file is still one scanned file: it advances the bar so the
    # denominator cannot shrink and x/y keeps matching the run.
    with _write_lock:
        counts["skip"] += 1
    # The bar is ticked OUTSIDE the lock: every worker thread of a run comes
    # through here once per file, and rendering (tqdm formatting its line,
    # writing to the terminal) is orders of magnitude longer than the two int
    # increments the lock exists to protect.
    _pbar_tick(pbar, counts)


def _pbar_update(pbar, counts, kind="ok"):
    with _write_lock:
        if kind == "ok":
            counts["ok"] += 1
        elif kind == "fail":
            counts["fail"] += 1
    _pbar_tick(pbar, counts)


def _pbar_tick(pbar, counts):
    """Advance *pbar* by one file and refresh its postfix counters."""
    if pbar is None:
        return
    try:
        pbar.update(1)
        pbar.set_postfix(ok=counts["ok"], skip=counts["skip"], fail=counts["fail"])
    except Exception:
        pass


def _walk_files(root_dir, extensions, dirs_out=None):
    """Fast recursive file walker using os.scandir().

    *dirs_out*, when given, is filled with every directory the walk enters
    (SKIP_DIRS pruned) mapped to what it holds directly — WALK_EMPTY,
    WALK_FILES or WALK_MATCHED (see the constants above). One walk then answers
    both "which albums are there" and "which folders hold no audio at all"
    (mlo.grader's empty-folder sweep) instead of walking the library twice.
    """
    if not os.path.isdir(root_dir):
        return
    yield from _walk_dir(root_dir, extensions, dirs_out)


def _walk_dir(root_dir, extensions, dirs_out=None, _links=None):
    """The recursion half of :func:`_walk_files`.

    *root_dir* is known to be a directory here — its parent's scandir said so —
    so this must NOT stat it again: ``os.path.isdir`` per level was one extra
    syscall per directory (a warm-cache 400-album walk spent 17 of its 65 ms
    there, `nt._path_isdir` on the clock). A directory that vanished or turned
    unreadable between the scan and the descent still reports nothing: scandir
    raises OSError, which the guard below already swallows exactly as the
    old isdir check did.

    Linked directories are FOLLOWED: an artist or album folder that is a
    symlink (a mapped drive, a second library kept elsewhere) is a real folder
    in this library, and the walker yielded nothing at all for it while it did
    not — those albums were never graded. Only a linked entry pays for the
    resolve (see :func:`_link_loops`); a link that points back up its own
    chain, or at a target another link already brought in, is skipped so the
    recursion cannot run away.
    """
    if dirs_out is not None:
        dirs_out[root_dir] = WALK_EMPTY
    if _links is None:
        _links = set()
    try:
        for entry in os.scandir(root_dir):
            if entry.is_dir(follow_symlinks=True):
                if entry.name.lower() in _SKIP_DIRS_LOWER:
                    continue
                if _linked_dir(entry) and _link_loops(entry.path, root_dir, _links):
                    continue
                yield from _walk_dir(entry.path, extensions, dirs_out, _links)
            elif entry.is_file(follow_symlinks=True):
                matched = os.path.splitext(entry.name)[1].lower() in extensions
                if dirs_out is not None:
                    # Only ever climbs: a directory that already matched stays
                    # matched whatever else it holds.
                    if dirs_out.get(root_dir, WALK_EMPTY) < WALK_FILES:
                        dirs_out[root_dir] = WALK_FILES
                    if matched:
                        dirs_out[root_dir] = WALK_MATCHED
                if matched:
                    yield entry.path
    except OSError:
        pass


def _linked_dir(entry):
    """Whether a scanned directory entry is a link of any kind.

    A Windows junction (``mklink /J``, a folder mounted into the library) is a
    directory that ``DirEntry.is_symlink`` reports as False, so a walk that
    only tested for symlinks would follow one — and one pointing back up its
    own chain would recurse until the path outgrew the filesystem. Python < 3.12
    has no ``is_junction``, where junctions simply stay the plain directories
    they always were here.
    """
    if entry.is_symlink():
        return True
    try:
        return entry.is_junction()
    except AttributeError:
        return False


def _link_loops(path, parent, links):
    """Whether following the linked directory *path* would revisit a folder.

    Remembers each link's resolved target in *links*, so two links to the same
    folder are walked once, and refuses a target that *parent* already sits
    inside — that link points back up its own chain and would otherwise
    recurse until the path outgrew the filesystem.
    """
    try:
        target = os.path.realpath(path)
        here = os.path.realpath(parent)
    except OSError:
        return False
    if target in links or here == target or here.startswith(target + os.sep):
        return True
    links.add(target)
    return False


def is_audio_file(path):
    # LIB_AUDIO_EXTS: music-video MP4/M4A are first-class tracks (taggable,
    # playable, graded); engine walkers that need lossless-only inputs keep
    # using AUDIO_EXTS directly.
    return os.path.splitext(path)[1].lower() in LIB_AUDIO_EXTS


def _clean_set(values):
    out = set()
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out.add(s)
    return out


def _summarize_values(values):
    clean = _clean_set(values)
    if not clean:
        return None
    if len(clean) == 1:
        return next(iter(clean))
    return "INCONSISTENT"


def _decode_mp4_value(v):
    try:
        if isinstance(v, bytes):
            return v.decode("utf-8", "replace")
        if hasattr(v, "data"):
            d = v.data
            return d.decode("utf-8", "replace") if isinstance(d, bytes) else str(d)
        return str(v)
    except Exception:
        return str(v)


# --------------------------------------------------------------------------- #
# Chain-scoped library scan
# --------------------------------------------------------------------------- #
# A script CHAIN (Run All, an import's chain) runs up to 22 scripts over ONE
# library, and every album-walking script used to discover that library for
# itself: `_find_albums` re-walked the whole music folder per script — the same
# answer, re-derived a dozen times, on a library where one walk is thousands of
# scandir calls. So a chain resolves the album list ONCE (chain_scan_start) and
# the walkers inside it are served that list.
#
# OPT-IN and chain-scoped on purpose: outside a scope (a page request, a CLI
# script, a test, a lone runner) every walker still sees the library as it is
# NOW, and nothing is memoized. A script that MOVES an album — or one that
# failed halfway, so what it did is unknown — drops the list
# (chain_scan_drop), the next walker resolves it again, and the chain ends the
# scope however it ends (chain_scan_end, in a finally at the chain's own entry
# point).
_SCAN = {"root": "", "albums": None}
_SCAN_LOCK = threading.Lock()


def _scan_root_key(root_dir):
    """The comparison key for *root_dir* ("" when it is not a usable path)."""
    try:
        text = str(root_dir or "")
    except Exception:
        return ""
    return os.path.normcase(os.path.abspath(text)) if text else ""


def _scan_hit(root_dir):
    """The chain's own album list for *root_dir*, or None when there is none.

    None means "walk it": either no scope is active, or this walker is asking
    about ANOTHER library (a caller must never be handed the albums of a music
    folder it did not ask about).
    """
    with _SCAN_LOCK:
        if _SCAN["albums"] is None or not _SCAN["root"]:
            return None
        return _SCAN["albums"] if _scan_root_key(root_dir) == _SCAN["root"] else None


def _scan_store(root_dir, albums):
    """Keep *albums* for the chain in flight, when that is the same library."""
    with _SCAN_LOCK:
        if _SCAN["root"] and _scan_root_key(root_dir) == _SCAN["root"]:
            _SCAN["albums"] = list(albums)


def chain_scan_start(root_dir):
    """Resolve *root_dir*'s album list once, for the chain that is starting.

    Returns the list (empty for a root that does not exist — the same answer a
    walk would give). Every `_find_albums` inside the scope is then answered
    from it until the scope is dropped or ended.
    """
    with _SCAN_LOCK:
        _SCAN["root"], _SCAN["albums"] = _scan_root_key(root_dir), None
    if not os.path.isdir(str(root_dir or "")):
        with _SCAN_LOCK:
            _SCAN["root"] = ""
        return []
    albums = _find_albums(root_dir)      # stores itself (the scope is armed)
    return albums


def chain_scan_drop():
    """Forget the chain's album list: a script moved an album (or failed).

    The scope STAYS active — the next walker resolves the library again and the
    scripts after it share that fresh answer.
    """
    with _SCAN_LOCK:
        if _SCAN["root"]:
            _SCAN["albums"] = None


def chain_scan_end():
    """End the scope: walkers discover the library for themselves again."""
    with _SCAN_LOCK:
        _SCAN["root"], _SCAN["albums"] = "", None


def _find_albums(root_dir, dirs_out=None):
    """Every folder that holds audio at or below *root_dir*, sorted.

    A chain that is in flight has already resolved this list for its own
    library (`chain_scan_start`): a walker inside it is served that answer
    instead of re-walking the whole music folder per script, which is what a
    Run All over a real library paid for every one of its album-walking
    scripts.

    *dirs_out* (the walker's own directory scan) is never answered from the
    chain's list: a caller that wants the per-directory detail wants THIS
    walk, and the grader is the one caller that asks for it.
    """
    if dirs_out is None:
        hit = _scan_hit(root_dir)
        if hit is not None:
            return list(hit)
    albums = set()
    # The walker yields a directory's files one after another, so the parent
    # is the same string for the whole album: normalizing it once per album
    # instead of once per track saves a normpath call per file (a 20-track
    # album cost 20, the same path 20 times).
    #
    # *dirs_out* receives the walker's own directory scan (see _walk_files):
    # the grader's empty-folder sweep reads it instead of walking the library
    # a second time.
    last_raw = last_album = None
    for file_path in _walk_files(root_dir, LIB_AUDIO_EXTS, dirs_out):
        raw = os.path.dirname(file_path)
        if raw != last_raw:
            # Normalize so F:/Music/Artists + \System\... mixed separators don't
            # create mismatched keys between the scanner and the UI's
            # os.path.dirname comparisons (Windows allows both / and \).
            last_raw, last_album = raw, os.path.normpath(raw)
            albums.add(last_album)
    result = sorted(albums)
    if dirs_out is None:
        # A chain that is in flight just paid for this walk: keep the answer
        # for its next script (a no-op when no scope is active, which is every
        # caller outside a chain).
        _scan_store(root_dir, result)
    return result


def _collect_targets(targets, extensions):
    """Expand user-selected targets (files or dirs) into matching files.

    Used by the GUI library view so scripts run only on the directories
    and tracks the user selected, instead of the whole library. Files are
    kept ONLY when their extension is in `extensions` - a wrong file type
    in targets (e.g. a .flac reaching the cue formatter) must never be
    processed and risk overwriting the file.
    """
    if not targets:
        return []
    files = {}
    for t in targets:
        if not t:
            continue
        if os.path.isfile(t):
            if os.path.splitext(t)[1].lower() in extensions:
                files[os.path.normcase(os.path.normpath(t))] = t
        elif os.path.isdir(t):
            # Same memo as _find_albums: the walker hands one directory's files
            # over together, and normcase(normpath(parent + sep + name)) is
            # normcase(normpath(parent)) + normcase(normpath(sep + name)) — the
            # parent's two string operations run once per directory, not once
            # per file. The REMAINDER goes through the same two calls, so a file
            # selected twice (the folder it sits in AND the file itself, which
            # is exactly a mixed selection) keys IDENTICALLY either way and is
            # processed once: with the parent's spelling alone, a target spelled
            # "F:/…/Album" and the walker's "/1-02 …" remainder produced a key no
            # file target could match, and the track was scanned twice.
            last_raw = last_key = None
            for f in _walk_files(t, extensions):
                raw = os.path.dirname(f)
                if raw != last_raw:
                    last_raw = raw
                    last_key = os.path.normcase(os.path.normpath(raw))
                files[last_key
                      + os.path.normcase(os.path.normpath(f[len(raw):]))] = f
    return sorted(files.values())


def usable_cores():
    """How many cores THIS process may actually use.

    `process_cpu_count` respects an affinity mask (a container's `--cpuset`,
    taskset) where `cpu_count` reports the host's cores; it is the honest
    answer to "how many can this process use" and Python 3.13 has it. Fall
    back to the older call on anything without it. Every sizing decision
    (:func:`worker_count`, :func:`thread_budget`) starts from this one number,
    so a pool and the budget it divides cannot disagree about the machine.
    """
    return (getattr(os, "process_cpu_count", None) or os.cpu_count)() or 1


def worker_count(config=None, maximum=None, items=None):
    """Choose THIS script's pool width from the shared performance setting.

    ``worker_limit`` (Settings → General, "Worker threads") is the one knob
    that decides how many lanes every script runs: ``0``/unset means "use the
    machine", so the pool is sized from :func:`usable_cores` — a 16-core box
    gets 16 lanes instead of whatever number was picked back when four cores
    was a lot of computer. A script may still declare a ``maximum``: that is
    its own honest ceiling, either because its worker's native tool already
    saturates the machine (one encoder per core is slower, not faster, on one
    disk) or because the work is NESTED inside another pool and must not
    multiply the run's budget (R79) — and it caps the AUTOMATIC count only.
    An EXPLICIT ``worker_limit`` is the user sizing the pools themselves, so
    it is never clamped by a script's ceiling. ``items`` bounds the pool by
    the work it actually has, so one album never starts eight lanes.
    """
    config = config or {}
    try:
        requested = int(config.get("worker_limit", 0) or 0)
    except (TypeError, ValueError):
        requested = 0
    if requested > 0:
        count = requested
    else:
        cores = usable_cores()
        count = cores if maximum is None else min(cores, max(1, int(maximum)))
    if items is not None:
        count = min(count, max(1, int(items)))
    return max(1, count)


def thread_budget(config=None):
    """How many CPU threads a run may use in total.

    ``worker_limit`` is the user's answer to "how much of this machine may the
    scripts use", and it used to bound only the POOL sizes: each worker's
    native tool (oxipng, cjxl, rsgain) and every in-process math library
    (numpy/OpenBLAS through librosa) went on claiming every core, so a
    2-worker run on a 16-thread host could still peg the CPU — the exact thing
    the setting exists to prevent, and what a container's CPU quota turns into
    throttling, i.e. a slower run. Anything spawning threads off a worker
    derives its count from here. 0 (auto) reports the machine's cores — the
    same number :func:`usable_cores` gives the pools, so a run's total is the
    machine, never more.
    """
    config = config or {}
    try:
        requested = int(config.get("worker_limit", 0) or 0)
    except (TypeError, ValueError):
        requested = 0
    return requested if requested > 0 else usable_cores()


def tool_threads(config=None, workers=1):
    """The share of :func:`thread_budget` ONE worker's native tool may use.

    A pool of *workers* tools each taking this many threads adds up to the
    budget, instead of workers × cores.
    """
    return max(1, thread_budget(config) // max(1, int(workers or 1)))


# The math libraries a worker's analysis rides on: numpy's BLAS backend
# (OpenBLAS/MKL/BLIS), OpenMP, NumExpr and Accelerate. Each has its own thread
# pool, invisible to worker_count, and each reads exactly one of these at load.
_NUMERIC_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
                "BLIS_NUM_THREADS")

# The cap currently applied to the loaded pools, so a re-apply at the same
# width is a no-op and the run does not re-limit every album.
_NUMERIC_LIMIT = None


def bound_numeric_threads(config=None, workers=1):
    """Cap this process's math-library pools to ONE worker's thread share.

    *workers* is the pool width the caller is about to fill, exactly as for
    :func:`tool_threads`: the libraries are what each worker computes on, so
    ``workers × cap`` is the run's total, not ``workers × cores`` — which is
    what an un-capped librosa/numpy did to a 4-lane script 12 or 16 (R79).

    Returns the cap. The environment is set first, because a library that has
    not been imported yet reads it at load; the same cap is then applied to the
    pools ALREADY loaded through threadpoolctl (a hard dependency of the
    analysis path — see server/requirements.txt), because the server imports
    numpy long before a script runs and the environment alone would change
    nothing there. A build without threadpoolctl keeps the environment answer.
    """
    global _NUMERIC_LIMIT
    cap = tool_threads(config, workers)
    for name in _NUMERIC_ENV:
        os.environ[name] = str(cap)
    if _NUMERIC_LIMIT != cap:
        try:
            import threadpoolctl
        except ImportError:
            _NUMERIC_LIMIT = cap
            return cap
        try:
            threadpoolctl.threadpool_limits(limits=cap)
        except Exception:
            # An ABI the limiter cannot read is still not a reason to abort a
            # run: the pool keeps whatever it had, as before this existed.
            pass
        _NUMERIC_LIMIT = cap
    return cap

