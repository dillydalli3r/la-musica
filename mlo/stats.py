"""Shared statistics, byte accounting, progress shims and file walking."""
import os
import threading

from .deps import tqdm
from .paths import LIB_AUDIO_EXTS, SKIP_DIRS, AUDIO_EXTS

# Pre-lowered once: _walk_files visits thousands of directories and used to
# rebuild this set at every level.
_SKIP_DIRS_LOWER = {d.lower() for d in SKIP_DIRS}

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
        if not path or not os.path.exists(path):
            return 0
        if avoid_path and os.path.normcase(os.path.normpath(path)) == os.path.normcase(os.path.normpath(avoid_path)):
            return 0
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
    counts["skip"] += 1
    if pbar is not None:
        try:
            with _write_lock:
                # A skipped file is still one scanned file: advance the bar so
                # the denominator cannot shrink and x/y keeps matching the run.
                pbar.update(1)
                pbar.set_postfix(ok=counts["ok"], skip=counts["skip"], fail=counts["fail"])
        except Exception:
            pass


def _pbar_update(pbar, counts, kind="ok"):
    with _write_lock:
        if kind == "ok":
            counts["ok"] += 1
        elif kind == "fail":
            counts["fail"] += 1

        if pbar is not None:
            try:
                pbar.update(1)
                pbar.set_postfix(ok=counts["ok"], skip=counts["skip"], fail=counts["fail"])
            except Exception:
                pass


def _walk_files(root_dir, extensions):
    """Fast recursive file walker using os.scandir()."""
    if not os.path.isdir(root_dir):
        return
    try:
        for entry in os.scandir(root_dir):
            if entry.is_dir(follow_symlinks=False):
                if entry.name.lower() not in _SKIP_DIRS_LOWER:
                    yield from _walk_files(entry.path, extensions)
            elif entry.is_file(follow_symlinks=False):
                if os.path.splitext(entry.name)[1].lower() in extensions:
                    yield entry.path
    except OSError:
        pass


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


def _find_albums(root_dir):
    albums = set()
    for file_path in _walk_files(root_dir, LIB_AUDIO_EXTS):
        # Normalize so F:/Music/Artists + \System\... mixed separators don't
        # create mismatched keys between the scanner and the UI's
        # os.path.dirname comparisons (Windows allows both / and \).
        albums.add(os.path.normpath(os.path.dirname(file_path)))
    return sorted(albums)


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
            for f in _walk_files(t, extensions):
                files[os.path.normcase(os.path.normpath(f))] = f
    return sorted(files.values())


def worker_count(config=None, default=None, maximum=None, items=None):
    """Choose a bounded worker count from the shared performance setting.

    ``worker_limit=0`` keeps the module's normal automatic behavior. An
    explicit positive limit prevents a Run All job from creating too many
    competing encoder processes on a busy or slower disk.
    """
    config = config or {}
    cpu = os.cpu_count() or 1
    try:
        requested = int(config.get("worker_limit", 0) or 0)
    except (TypeError, ValueError):
        requested = 0
    count = requested if requested > 0 else (default or cpu)
    if maximum is not None:
        count = min(count, maximum)
    if items is not None:
        count = min(count, max(1, int(items)))
    return max(1, count)

