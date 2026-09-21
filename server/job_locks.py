"""Which in-flight job holds which library path right now.

Scripts, imports and the organizer mutate the library in place: they re-encode
a file, rewrite its tags, rename and move it, and remove what they replaced. A
second caller acting on the same folder meanwhile — a batch tag edit, Organize,
"remove from library" — races that work: it can write tags into a file the job
has already renamed away, or move a folder out from under a script still
writing into it. This registry is where a job claims the paths it is about to
touch, so every entry point can refuse with 409 (``PathLocked``) instead.

Two paths conflict when they are the same path, or one holds the other (an
album folder vs. a track inside it), so album-scoped work and a single-track
edit block each other in both directions. Paths are compared under the same
``normcase(normpath(abspath(...)))`` normalization the rest of the app uses for
path identity — on Windows that also folds case and the separator.

A job never conflicts with itself. The paths it already holds are its own, and
a nested call in the same CONTEXT (one thread, or one async task) joins the
job running there through :func:`current` instead of claiming a second one.
That is what makes "the album is locked" and "this import may organize that
album" both true at once — and why the context, not the thread, is what a job
is scoped to: two requests sharing one event-loop thread are two jobs.

A claim lives exactly as long as the ``with`` block that made it: a job that
raises, is cancelled or returns early releases everything in ``finally``.
Nothing else may release it — a claim dropped while the work it protects is
still in flight would only mean the files are unprotected — so the only
endings are the block itself and :func:`release` at the end of a job whose
outermost block owns several steps (the import queue). A block may also hand
one reference of its claim to a worker thread (:func:`in_background`), which
keeps the paths locked until that thread is finished.
"""
import contextlib
import contextvars
import functools
import inspect
import os
import threading
import time

# One lock for the registry and the condition that waits on it; every public
# entry point takes it, so a reader never sees half a job's claims.
_lock = threading.RLock()
_cond = threading.Condition(_lock)

# job id -> record. The record is both what MAINTAIN → In progress lists and
# what a refusal names.
_jobs: dict[str, dict] = {}
# normalized path -> the job id holding it.
_owners: dict[str, str] = {}
# The job the calling THREAD (and, on one thread, the calling TASK) is inside,
# innermost last: a nested acquire joins it.
#
# A ContextVar, not a threading.local: an async route holds its claim across
# its awaits, and two requests that share one event-loop thread must NOT share
# a job. Each asyncio task runs in its own context, so the second request sees
# no current job, claims its own paths, and is refused like any other
# collision — where a thread-local made it join the first request's job and
# write to the same album unchallenged. A plain thread starts with a fresh
# context, so a worker thread still has to be handed a job explicitly
# (:func:`in_background`).
_active: contextvars.ContextVar[tuple] = contextvars.ContextVar("mlo_active_jobs", default=())

_seq = 0

# An import queues behind a run instead of failing (the album is already on
# disk and the user asked for it to be finished); the bound is the one
# server.script_runners.run_chain uses for the same wait, so a wedged job
# cannot hold an import forever.
DEFAULT_WAIT = 3600.0


class PathLocked(RuntimeError):
    """Another job holds one of the paths this one asked for.

    Carries the path that was asked for, the holder's job id and its record,
    so a caller can say who to wait for rather than "it failed".
    """

    def __init__(self, path, holder):
        super().__init__(refusal(path, holder))
        self.path = path
        self.job = (holder or {}).get("job")
        self.holder = holder


def refusal(path, holder):
    """The sentence a refusal shows: which path, and which job is using it."""
    holder = holder or {}
    who = holder.get("label") or holder.get("kind") or "another job"
    job = str(holder.get("job") or "").strip()
    name = os.path.basename(str(path).rstrip("\\/")) or str(path)
    return (f"{name} is in use by {who}" + (f" ({job})" if job else "")
            + " — wait for it to finish, then retry")


def normalize(path):
    """The identity a path is locked under: absolute, normalized, case-folded.

    The same comparison the rest of the app makes for "is this the same file"
    (see server.playlists._norm, mlo.loudness._cache_key).
    """
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _within(parent, child):
    """Whether *child* is *parent* itself or something inside it.

    Compared at directory boundaries: "…/Album" does not hold "…/Album 2",
    which a plain prefix test would call a conflict.
    """
    if parent == child:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


def _slots(paths):
    """``[(key, as-given path)]`` for *paths*, blanks and repeats dropped."""
    out = []
    seen = set()
    for raw in paths or []:
        text = str(raw or "").strip()
        if not text:
            continue
        key = normalize(text)
        if key in seen:
            continue
        seen.add(key)
        out.append((key, text))
    return out


def _push(job):
    """Put *job* on this context's stack (a fresh tuple: a ContextVar's value
    is shared by every context it was copied into, so it must never be mutated
    in place)."""
    _active.set(_active.get() + (job,))


def _pop(job):
    """Take one entry for *job* off this context's stack."""
    stack = _active.get()
    if job in stack:
        rest = list(stack)
        rest.remove(job)
        _active.set(tuple(rest))


def current():
    """The id of the job this thread (or this async task) is inside, or None.

    The inner call of a job must never queue behind its own outer call: the
    import pipeline holds the album it is importing and then organizes it, and
    both happen in one context.
    """
    stack = _active.get()
    return stack[-1] if stack else None


def new_job():
    """Mint a job id (``job-7``): unique per process, ordered by creation.

    A job that spans several steps — an import queue working through albums one
    at a time — mints one id and passes it back into :func:`acquire`, so the UI
    keeps showing one row for it. Nothing is registered until a path is claimed
    under the id, so an id that never gets used leaves nothing behind.
    """
    global _seq
    with _lock:
        _seq += 1
        return f"job-{_seq}"


def _record(job, kind="", label=""):
    """The job's record, created on first use. Caller holds ``_lock``."""
    rec = _jobs.get(job)
    if rec is None:
        rec = {"job": job, "kind": str(kind or ""), "label": str(label or ""),
               "started_at": time.time(), "paths": [], "keys": set(),
               "refs": {}, "progress": None}
        _jobs[job] = rec
    else:
        # A kind/label that arrives with a later step refines the row the UI is
        # already showing rather than replacing it (the import queue names its
        # run once and then works through album after album).
        if kind:
            rec["kind"] = str(kind)
        if label:
            rec["label"] = str(label)
    return rec


def _conflict(slots, job):
    """``(asked-for path, holder record)`` of the first foreign claim, else None.

    Caller holds ``_lock``. Exact matches first (a dict hit), then the
    containment scan: the number of held paths is small — one per job, or one
    per target of a batch — so a plain scan beats an interval index here.
    """
    for key, shown in slots:
        owner = _owners.get(key)
        if owner is not None and owner != job:
            return shown, (_jobs.get(owner) or {"job": owner})
    for held, owner in _owners.items():
        if owner == job:
            continue
        for key, shown in slots:
            if _within(held, key) or _within(key, held):
                return shown, (_jobs.get(owner) or {"job": owner})
    return None


def _await_free(slots, job, wait, timeout):
    """Wait for the paths to clear, or refuse. Caller holds ``_lock``."""
    deadline = None
    if wait:
        deadline = time.monotonic() + (DEFAULT_WAIT if timeout is None else float(timeout))
    while True:
        hit = _conflict(slots, job)
        if hit is None:
            return
        if not wait:
            raise PathLocked(hit[0], hit[1])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PathLocked(hit[0], hit[1])
        # Released claims notify, so this wakes when a job ends instead of
        # polling for it.
        _cond.wait(remaining)


def acquire(paths, job=None, *, kind="", label="", wait=False, timeout=None):
    """Claim *paths* for *job*; returns the job id used.

    *job* defaults to the job this context is already in (a nested call), else
    to a fresh one. Raises :class:`PathLocked` when a DIFFERENT job holds any
    of the paths — at once, or after *timeout* seconds (default
    ``DEFAULT_WAIT``) when *wait* is set. Claiming a path this job already
    holds is a reference: it stays held until the last block releases it.
    """
    slots = _slots(paths)
    with _cond:
        job = job or current() or new_job()
        if slots:
            _await_free(slots, job, wait, timeout)
        rec = _record(job, kind, label)
        for key, shown in slots:
            _owners[key] = job
            rec["refs"][key] = rec["refs"].get(key, 0) + 1
            if key not in rec["keys"]:
                rec["keys"].add(key)
                rec["paths"].append(shown)
        _push(job)
        _cond.notify_all()
        return job


def release(job):
    """End *job* here: give up this block's place in it, and forget the job
    once nothing holds it any more. Never raises.

    Called at the end of the job's own outermost block — and by a worker that
    was handed the job (:func:`in_background`), whose reference is what keeps
    the job, and the paths it is still rewriting, registered until it is done.
    """
    if not job:
        return
    with _cond:
        rec = _jobs.get(job)
        _pop(job)
        if rec is not None and not rec["keys"]:
            del _jobs[job]
        _cond.notify_all()


def _release_slots(job, keys):
    """Undo ONE block's claims on *keys*, keeping the job's other ones."""
    with _cond:
        rec = _jobs.get(job)
        if rec is not None:
            for key in keys:
                refs = rec["refs"].get(key, 0)
                if refs > 1:
                    rec["refs"][key] = refs - 1
                    continue
                rec["refs"].pop(key, None)
                rec["keys"].discard(key)
                rec["paths"] = [p for p in rec["paths"] if normalize(p) != key]
                if _owners.get(key) == job:
                    del _owners[key]
        _pop(job)
        _cond.notify_all()


def in_background(paths, target, *args, kind="", label="", **kwargs):
    """Run *target* on a daemon thread that takes over the claim on *paths*.

    A route that starts background work on the paths it is holding cannot just
    return: its own block ends there and would drop the claim before the worker
    — which is still rewriting those files — had registered its own. That gap
    is a real one (an import's chain writes tags, covers and lyrics after the
    HTTP call is over), so this takes one MORE reference on each path here,
    hands the caller's job to the thread (through a copy of the calling
    context, so the job's nested calls stay its own), and gives that reference
    back when *target* returns or raises.

    Returns the started thread. *kind*/*label* name the job only when the
    caller is not inside one already (a route normally is).
    """
    slots = _slots(paths)
    job = current()
    with _cond:
        if job is None:
            job = new_job()
            _record(job, kind or "background", label)
        rec = _record(job)
        for key, shown in slots:
            _owners[key] = job
            rec["refs"][key] = rec["refs"].get(key, 0) + 1
            if key not in rec["keys"]:
                rec["keys"].add(key)
                rec["paths"].append(shown)
        _cond.notify_all()

    keys = [key for key, _ in slots]
    ctx = contextvars.copy_context()

    def run():
        _push(job)
        try:
            return target(*args, **kwargs)
        finally:
            _release_slots(job, keys)
            release(job)

    thread = threading.Thread(target=ctx.run, args=(run,), daemon=True,
                              name=f"mlo-job-{job}")
    thread.start()
    return thread


@contextlib.contextmanager
def holding(paths, job=None, *, kind="", label="", wait=False, timeout=None):
    """Hold *paths* for the block, releasing them however it ends.

    Pass *job* to keep one identity across several steps (the import queue
    holds its run once, then the album it is on); omit it to join the job this
    context is already in, or to claim a fresh one. The job itself ends with
    its OUTERMOST block, so a nested hold (an import's organize step) leaves
    the album locked for the work still going on around it — and a reference
    handed to a worker (:func:`in_background`) keeps it locked past the block.
    """
    job = job or current()
    nested = job is not None and current() == job
    keys = [key for key, _ in _slots(paths)]
    job = acquire(paths, job, kind=kind, label=label, wait=wait, timeout=timeout)
    try:
        yield job
    finally:
        _release_slots(job, keys)
        if not nested:
            release(job)


def holds(paths_of, *, kind="", label="", wait=False, timeout=None):
    """Decorator: hold the paths ``paths_of(*args, **kwargs)`` names, for the call.

    For a route that mutates library files this is the whole guard: the paths
    are claimed before the body runs (a foreign claim raises PathLocked, which
    the app answers as 409) and released in ``finally``, so a body that raises
    or returns early cannot leave a path locked. An empty selection holds
    nothing — a dry run changes no files and must not lock the album it would
    have touched. *label* may be a callable of the same arguments, for a row
    that names what is being worked on.

    An async endpoint gets an async wrapper: holding across the coroutine
    would otherwise end the claim the moment the function returned its
    coroutine, i.e. before any of the work it guards had started.
    """
    def decorate(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                text = label(*args, **kwargs) if callable(label) else label
                with holding(paths_of(*args, **kwargs) or [], kind=kind,
                             label=text, wait=wait, timeout=timeout):
                    return await fn(*args, **kwargs)
            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            text = label(*args, **kwargs) if callable(label) else label
            with holding(paths_of(*args, **kwargs) or [], kind=kind, label=text,
                         wait=wait, timeout=timeout):
                return fn(*args, **kwargs)
        return wrapper
    return decorate


def holder(path, asker=None):
    """The record of the job holding *path* (or a folder above it), else None.

    The non-blocking query: *path* answers for itself and for everything under
    it, so a track is answered by the album-wide job holding its folder. *asker*
    is the job asking (default: the job this thread is inside) — its own claim
    is never a conflict, which is how a job asks about the paths it holds. Pass
    another job's id to ask absolutely.
    """
    with _lock:
        hit = _conflict(_slots([path]), asker or current())
        if hit is None:
            return None
        rec = _jobs.get(hit[1].get("job"))
        return dict(rec) if rec else dict(hit[1])


def set_progress(job, done=None, total=None, text=""):
    """How far *job* has got, for the in-progress list. "" total = unknown.

    Runners call this as they work (a script per chain step, an album per
    import); a job that never does is still listed, with no bar.
    """
    with _lock:
        rec = _jobs.get(job)
        if rec is None:
            return
        rec["progress"] = {"done": done, "total": total, "text": str(text or "")}


def jobs():
    """Snapshot for the UI: one row per in-flight job, oldest first.

    ``elapsed`` is measured here so every client reads the same figure off the
    server's clock — a phone whose clock is off would otherwise print a run
    that started "in 3 minutes".
    """
    with _lock:
        now = time.time()
        return [{
            "job": rec["job"],
            "kind": rec["kind"],
            "label": rec["label"] or rec["kind"] or "Job",
            "started_at": rec["started_at"],
            "elapsed": max(0.0, now - rec["started_at"]),
            "paths": list(rec["paths"]),
            "progress": dict(rec["progress"]) if rec["progress"] else None,
        } for rec in sorted(_jobs.values(), key=lambda r: r["started_at"])]
