"""Atomic file replacement, and the ledger of the app's own temp files.

The updater restarts this container whenever a new image appears, and Docker
SIGKILLs whatever is still running when the stop grace runs out — so ANY write
can be cut in half at any instant. The one defence that holds at every
instant is to never write to the file a reader cares about: the bytes land in
a temp file in the SAME directory, are flushed and fsynced, take over the
destination's permissions, and are moved onto it with a single
``os.replace``. A kill therefore leaves either the old file or the new one —
never a torn one.

Two consequences this module owns:

* the temp name is recognisable (``.mlo_tmp_*``, and the older per-module
  prefixes in :data:`TEMP_PREFIXES`), so a leftover temp from a killed run is
  never mistaken for library content and can be swept at startup by
  :func:`sweep_temps`;
* the directory entry is fsynced after the replace on POSIX (see
  :func:`mlo.paths.fsync_dir`), so the rename itself survives a power cut.
"""
import errno
import os
import re
import shutil
import tempfile
import time

from .paths import fsync_dir

# New temp files. A single prefix means one rule for the scanner (it ignores
# dot-names already) and one rule for the startup sweep.
TEMP_PREFIX = ".mlo_tmp_"
TEMP_SUFFIX = ".tmp"

# Every temp prefix the app writes anywhere. The sweep deletes a file only
# when its name matches one of these — a user's file can share a folder with
# a leftover temp, but it cannot be named like one.
TEMP_PREFIXES = (
    TEMP_PREFIX,          # this module (new writers)
    ".mlo_covers_",       # mlo.paths.save_track_covers
    ".mlo_expected_",     # mlo.paths.save_expected_tracks
    ".mlo_pending_",      # mlo.paths.save_pending
    ".mlo_importing_",    # mlo.paths.save_importing
    ".mlo_config_",       # mlo.config.save_config
    ".accurip_tmp_",      # mlo.accurip
    ".accurip_fmt_",      # mlo.format_all
    ".artwork_",          # mlo.artistdata (artwork.json)
    ".artist_",           # mlo.artistdata (artist image)
    ".description_",      # mlo.artistdata (descriptions)
    ".audit_evidence_",   # mlo.audit
    ".cue_fix_",          # mlo.discs
    ".cue_fmt_",          # mlo.format_all
    ".cue_tmp_",          # mlo.discs (cue rewrite)
    ".conv_",             # mlo.flac (format conversion)
    ".decode_",           # mlo.flac (decode to a temp for analysis)
    ".lrc_fmt_",          # mlo.format_all
    ".lrc_tmp_",          # mlo.lyrics + server.main (lrc sidecar)
    ".replaygain_",       # mlo.loudness
    ".cover_tmp_",        # server.main (embedded cover)
    ".videotag_",         # mlo.audio (video tag rewrite)
    ".remux_",            # mlo.remux
    "mlo_php_",           # server.integrations (logchecker PHP scratch)
)

# Names that signal a temp ANYWHERE in the name. Two kinds live here: the
# working-file names the engine still writes (mlo.flac's `.opttmp.flac`,
# mlo.discs' case-rename staging) and the names an older version left beside a
# cover (mlo.images now writes everything as `.mlo_tmp_*`, but a run killed on
# an older build left one of these behind). None of them is a name a library
# file would plausibly carry.
#
# Names an older version used that a user's own file COULD share — `.no_meta.`,
# `.no_alpha.`, `.decoded.`, `.optimized.` — are deliberately NOT claimed:
# nothing writes them any more, and deleting a user's file is worse than
# leaving our own junk for them to see.
TEMP_MARKERS = (
    ".opttmp.",            # mlo.flac / mlo.images optimise-in-place temp
    ".cover_resized.tmp",  # mlo.images resize pass (older builds)
    ".streamlined.tmp",    # mlo.images jpegli/xl streamline pass (older)
    ".ffmpeg.png",         # mlo.images ffmpeg fallback encode (older)
    ".upright.png",        # mlo.images EXIF-transposed fallback (older)
    ".mlo_case_tmp",       # mlo.discs case-only rename staging name
    ".reencode.tmp",       # mlo.images JXL re-encode (older)
)

# ``<dst>.mlo-tmp-<pid>-<hex>``: mlo.paths._copy_across_volumes' copy staging
# name (a suffix pattern, so it is matched separately from the prefixes).
_CROSS_VOLUME_RE = re.compile(r"\.mlo-tmp-\d+-[0-9a-f]{8}$")

# Names that are only a temp when they sit in the app's OWN state folders
# (``.mlo/data``, ``.mlo/downloads``…), never in the library: a user may keep
# any file they like in there, and ``x.tmp`` / ``x.part`` are plausible names
# for one.
STATE_TEMP_SUFFIXES = (".tmp", ".part")


def is_temp_name(name, *, in_state_dir=False) -> bool:
    """Whether *name* is a temp file the app itself wrote.

    *in_state_dir* widens the rule to the suffix-only temps the app writes in
    its own folders (``<file>.tmp``, ``<file>.part``) — those are only ever
    deleted there.
    """
    text = str(name or "")
    if not text:
        return False
    if text.startswith(TEMP_PREFIXES):
        return True
    if any(marker in text for marker in TEMP_MARKERS):
        return True
    if _CROSS_VOLUME_RE.search(text):
        return True
    return bool(in_state_dir and text.lower().endswith(STATE_TEMP_SUFFIXES))


def _read_umask() -> int:
    """umask, read once so a NEW file keeps the mode open() would have given.

    ``mkstemp`` creates 0600, but a new cover or lyrics sidecar must stay
    readable the way it was before (0644 under the usual 022 umask) — the
    library is bind-mounted, and the host user often is not the container's.
    POSIX only; Windows has no file modes.
    """
    if os.name != "posix":
        return 0o022
    try:
        current = os.umask(0)
        os.umask(current)
        return current
    except OSError:
        return 0o022


_NEW_FILE_MODE = 0o666 & ~_read_umask()


def new_temp(dest, *, prefix=TEMP_PREFIX, suffix=None) -> str:
    """Create (and close) an empty temp file beside *dest*; returns its path.

    The temp lives in the destination's OWN directory so the final
    ``os.replace`` stays within one filesystem and is therefore a rename.
    """
    folder = os.path.dirname(os.path.abspath(str(dest))) or "."
    fd, tmp = tempfile.mkstemp(prefix=prefix,
                               suffix=TEMP_SUFFIX if suffix is None else suffix,
                               dir=folder)
    os.close(fd)
    return tmp


def fsync_file(path) -> bool:
    """Flush *path*'s bytes to the device; False when the platform refuses."""
    try:
        with open(path, "rb") as fh:
            os.fsync(fh.fileno())
        return True
    except OSError:
        return False


def _apply_mode(tmp, dest) -> None:
    """Give the temp the destination's permissions (or the umask default)."""
    try:
        if os.path.exists(dest):
            shutil.copymode(dest, tmp)
        elif os.name == "posix":
            os.chmod(tmp, _NEW_FILE_MODE)
    except OSError:
        pass


# A rename onto a file Windows still has open fails with ERROR_ACCESS_DENIED
# (5) — a plain ``open(path, "rb")`` does not share delete, so this app's own
# player/range reader, slskd's share scan, a virus scan or a sibling job's
# ffprobe all turn an ``os.replace`` into this error even though neither file
# is read-only. A source that is itself still open (or a memory mapping over
# it) surfaces as ERROR_SHARING_VIOLATION (32) / ERROR_LOCK_VIOLATION (33).
# All three clear on their own in milliseconds, which is what the bounded
# retry below waits out; a real permissions problem (a read-only destination,
# no rights on the folder) still ends in the same error after the attempts.
_TRANSIENT_WINERRORS = (5, 32, 33)
# A few tries over ~1.5 s. Long enough to outlast an AV pass or a short
# ffprobe/share-scan read of the same file; short enough that an interactive
# tag write (a star click) never hangs the UI on a holder that will not let go.
REPLACE_ATTEMPTS = 6
REPLACE_DELAY = 0.25


def _is_transient_replace(exc) -> bool:
    """Whether *exc* from a rename is a sharing/lock denial worth a retry."""
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS:
        return True
    return getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM,
                                           errno.EBUSY, errno.ETXTBSY)


def _held_open(dest, exc) -> PermissionError:
    """The honest error for a rename Windows refused: what likely holds it, and
    that nothing was written."""
    out = PermissionError(
        f"{os.path.basename(str(dest))} could not be replaced: Windows denied "
        f"the rename — another program has it open (a player, a scanner, "
        f"antivirus, or another job) or the file is read-only; nothing was "
        f"written and the original is unchanged. ({exc})")
    out.winerror = getattr(exc, "winerror", None)
    out.errno = getattr(exc, "errno", None)
    return out


def replace_locked(tmp, dest, *, attempts=REPLACE_ATTEMPTS, delay=REPLACE_DELAY,
                   what=None) -> None:
    """``os.replace(tmp, dest)``, retried through a transient sharing denial.

    Every atomic writer in this app funnels its final swap through here (and
    through :func:`replace_temp`) so a rename that a reader, a scanner or a
    sibling job has momentarily blocked is waited out instead of surfacing as
    the owner's "Access is denied" on a library file that is not read-only.

    On give-up it raises :class:`PermissionError` whose message names what
    likely holds the file and says nothing was written. The caller still owns
    *tmp*: the swap did not happen, the destination keeps its original bytes,
    and the caller discards the temp (as :func:`write_bytes` /
    :func:`rewrite_via` do).
    """
    last = None
    for i in range(1, max(1, int(attempts)) + 1):
        try:
            os.replace(tmp, dest)
            return
        except OSError as exc:
            if i >= attempts or not _is_transient_replace(exc):
                if _is_transient_replace(exc):
                    raise _held_open(dest, exc) from exc
                raise
            last = exc
            if delay:
                time.sleep(delay)
    if last is not None:  # defensive: the loop returns or raises
        raise _held_open(dest, last) from last


def read_bytes_locked(path, *, attempts=REPLACE_ATTEMPTS, delay=REPLACE_DELAY) -> bytes:
    """The bytes of *path*, retried through a transient sharing denial.

    :func:`replace_locked`'s twin on the READ side. The denial it waits out is
    the same one and comes from the same holders, but the read direction is
    where it is silent: a writer holds ``cover.jpg`` open for the few
    milliseconds between its temp landing and its ``os.replace``, a scanner has
    the file mapped, or the anti-malware pass has it locked — and this app's
    readers used to turn that instant into a FAILED READ (an empty answer the
    caller had no way to tell from "there is nothing here"). A read has no
    atomicity to protect: the retry is free and the answer is the file the
    caller is entitled to.

    On give-up it raises the last :class:`OSError` (never a made-up empty
    result), so the caller decides what a real, persistent denial means for it.
    """
    last = None
    for i in range(1, max(1, int(attempts)) + 1):
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError as exc:
            if i >= attempts or not _is_transient_replace(exc):
                raise
            last = exc
            if delay:
                time.sleep(delay)
    raise last  # defensive: the loop returns or raises


def replace_temp(tmp, dest) -> str:
    """Durably move a finished temp onto *dest*; returns *dest*."""
    fsync_file(tmp)
    _apply_mode(tmp, dest)
    replace_locked(tmp, dest)
    fsync_dir(os.path.dirname(os.path.abspath(str(dest))) or ".")
    return dest


def write_bytes(dest, data: bytes) -> str:
    """Write *data* to *dest* atomically (temp -> fsync -> os.replace)."""
    tmp = new_temp(dest)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except OSError:
                pass
        return replace_temp(tmp, dest)
    except BaseException:
        _discard(tmp)
        raise


def rewrite_via(dest, write_fn) -> str:
    """Let *write_fn(tmp)* rewrite a COPY of *dest* in place, then swap it in.

    The form an in-place rewriter needs (mutagen, TagLib, and any library
    whose ``save()`` opens the original ``r+b``): the file is copied, the
    copy is rewritten, and one ``os.replace`` puts it in place. A kill
    mid-rewrite leaves the original exactly as it was.

    Costs one copy of the file per write — unavoidable, since the library
    only ever writes the path it was opened from. Only the tag block is
    usually rewritten by the library itself, but mutagen shifts the whole
    stream when the new tag does not fit the old padding, so the copy is
    what keeps that streaming rewrite off the original.
    """
    tmp = new_temp(dest)
    try:
        shutil.copy2(dest, tmp)
        write_fn(tmp)
        return replace_temp(tmp, dest)
    except BaseException:
        _discard(tmp)
        raise


def _discard(path) -> None:
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.remove(path)
    except OSError:
        pass


def sweep_temps(root, *, in_state_dir=False, log=None, limit=100000) -> list:
    """Delete leftover temp files under *root*; returns the paths removed.

    Only names :func:`is_temp_name` recognises are touched, so a user's file
    is never a candidate — an interrupted run's temp is deleted, a real file
    with an unrelated name is left exactly where it is. Pass *in_state_dir*
    when *root* is one of the app's OWN folders, which widens the rule to the
    suffix-only temps (``x.tmp``, ``x.part``) the app writes there; never pass
    it for the library or for the download folders. *log(line)* is called once
    per file so the caller can say what it cleaned up.
    """
    removed = []
    if not root or not os.path.isdir(root):
        return removed
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if d not in (".git", "$RECYCLE.BIN")]
        for name in filenames:
            if not is_temp_name(name, in_state_dir=in_state_dir):
                continue
            target = os.path.join(dirpath, name)
            try:
                os.remove(target)
            except OSError:
                continue  # in use, or already gone: next start gets it
            removed.append(target)
            if log is not None:
                try:
                    log(target)
                except Exception:
                    pass
            if len(removed) >= limit:
                return removed
    return removed
