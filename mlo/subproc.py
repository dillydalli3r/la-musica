"""Subprocess wrapper that never flashes console windows.

The GUI runs windowed (pythonw / PyInstaller --windowed), so it owns no
console of its own. On Windows every console executable launched by such a
process would otherwise create its own console window - and with dozens of
encoder threads running, that means a storm of flashing windows during
Run All. Every external tool invocation therefore goes through run_tool(),
which passes CREATE_NO_WINDOW. Output is already captured via pipes, so
nothing is lost.
"""

import atexit
import ctypes
import os
import signal
import subprocess
import tempfile
import threading

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_ACTIVE = {}
_ACTIVE_LOCK = threading.Lock()

# --------------------------------------------------------------------------- #
# Windows long paths (MAX_PATH) — the bundled tools are not long-path aware
# --------------------------------------------------------------------------- #
# Every console tool the engine drives (flac, ffmpeg, rsgain, simple-dr-meter,
# AudioAuditor, oxipng, cjxl, CUETools) opens its file through the MSVC CRT,
# which stops at 260 characters. A library whose album folders carry the ids,
# the dates and the media type reaches that on its own: measured on a real
# library, 3 of 8 files were 257-279 characters long and `flac -t` answered
# "No such file or directory" — so the FLAC pass did nothing, the audit
# could not read the audio and stamped AUDIT=FAKE, and the DR/ReplayGain pass
# skipped the album. Python itself is unaffected (it uses the wide APIs), which
# is why only the tool-driven steps failed.
#
# Two escapes, tried in order, both transparent to the caller:
#   1. the volume's own 8.3 alias (GetShortPathNameW) — free, but only where
#      short-name generation is still enabled (often off on data drives);
#   2. a DIRECTORY JUNCTION under %TEMP%\mlo-longpath pointing at the real
#      folder, which gives the tool a short path to the same files (junctions
#      need no admin rights) — created once per folder, removed on exit.
# The path handed to the tool is always absolute, so a junction can never
# change what a tool interprets as a relative path.
LONG_PATH_MIN = 240          # below MAX_PATH with room for a tool's own suffix
_BRIDGE_ROOT = os.path.join(tempfile.gettempdir(), "mlo-longpath")
_bridges = {}                # normalized directory -> junction path
_bridge_lock = threading.Lock()
_can_short = os.name == "nt"


def _short_8_3(path):
    """The path's 8.3 alias when the volume has one and it is shorter."""
    try:
        get = ctypes.windll.kernel32.GetShortPathNameW
        size = get(str(path), None, 0)
        if not size:
            return ""
        buf = ctypes.create_unicode_buffer(size)
        if not get(str(path), buf, size):
            return ""
        alias = buf.value
        return alias if alias and len(alias) < len(str(path)) else ""
    except Exception:
        return ""


def _junction_for(directory):
    """A short path that reaches *directory*, or "" when it cannot be made."""
    key = os.path.normcase(os.path.abspath(directory))
    with _bridge_lock:
        hit = _bridges.get(key)
        if hit:
            return hit
        try:
            os.makedirs(_BRIDGE_ROOT, exist_ok=True)
            link = os.path.join(_BRIDGE_ROOT, f"d{len(_bridges) + 1}")
            if os.path.exists(link):
                try:
                    os.rmdir(link)
                except OSError:
                    return ""
            made = subprocess.run(
                ["cmd", "/c", "mklink", "/J", link, os.path.abspath(directory)],
                capture_output=True, text=True, errors="replace",
                creationflags=CREATE_NO_WINDOW)
            if made.returncode or not os.path.isdir(link):
                return ""
            _bridges[key] = link
            return link
        except Exception:
            return ""


def _drop_bridges():
    for link in list(_bridges.values()):
        try:
            os.rmdir(link)      # removes the junction, never its target
        except OSError:
            try:
                subprocess.run(["cmd", "/c", "rmdir", link], capture_output=True,
                               creationflags=CREATE_NO_WINDOW)
            except Exception:
                pass
    _bridges.clear()


atexit.register(_drop_bridges)


def tool_path(path):
    """*path* in a form the external tools can open, Windows only.

    Anything short enough (and everything on POSIX) is returned unchanged, so
    the common case costs one integer compare. A long path is expressed
    through the volume's 8.3 alias, or through a temporary junction on the
    file's own folder — the SAME file either way, which is what lets a tool's
    output, in-place rewrite or integrity check land where it belongs.
    """
    if not _can_short or not isinstance(path, str) or len(path) < LONG_PATH_MIN:
        return path
    if os.path.exists(path):
        alias = _short_8_3(path)
        if alias:
            return alias
    folder, name = os.path.split(path)
    if not folder or not os.path.isdir(folder):
        return path
    link = _junction_for(folder)
    return os.path.join(link, name) if link else path


def _argv_with_tool_paths(args):
    """Rewrite the paths in a tool's argv (the executable itself is left)."""
    if not args or not isinstance(args[0], (list, tuple)):
        return args
    argv = args[0]
    if not any(isinstance(a, str) and len(a) >= LONG_PATH_MIN for a in argv[1:]):
        return args
    return ([argv[0]] + [tool_path(a) if isinstance(a, str) else a for a in argv[1:]],) + args[1:]


def _kill_tree(proc):
    """Kill a tool and every process it spawned.

    On timeout the direct child is often a shell (``shell=True`` -> cmd.exe)
    or an encoder wrapper whose real work keeps running and keeps its output
    file and temp dir locked. Windows has no job object here, so taskkill /T
    walks the tree; POSIX children get their own session (see run_tool) and
    are killed as a group.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def active_process_count():
    """Return the number of external tools currently owned by this process."""
    with _ACTIVE_LOCK:
        for pid, proc in list(_ACTIVE.items()):
            if proc.poll() is not None:
                _ACTIVE.pop(pid, None)
        return len(_ACTIVE)


def run_tool(*args, **kwargs):
    """Run a tool without a console window and keep an active-process count.

    The wrapper mirrors the subset of ``subprocess.run`` used by the project,
    including ``capture_output``, ``input``, ``timeout`` and ``check``.
    """
    # OR creationflags instead of overwriting
    if "creationflags" in kwargs:
        kwargs["creationflags"] |= CREATE_NO_WINDOW
    else:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    input_data = kwargs.pop("input", None)
    capture_output = kwargs.pop("capture_output", False)
    check = kwargs.pop("check", False)
    # Tool output (ffmpeg/flac/metadata) is UTF-8; without an explicit
    # encoding, Windows decodes with the ANSI codepage and can raise
    # UnicodeDecodeError on any non-ASCII byte.
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    if capture_output:
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    timeout = kwargs.pop("timeout", None)
    # When input is provided, Popen needs stdin=PIPE to actually feed it
    if input_data is not None and "stdin" not in kwargs:
        kwargs["stdin"] = subprocess.PIPE
    # Own process group on POSIX so a timeout can kill the whole tree
    # (Windows uses taskkill /T instead; the flag is POSIX-only).
    if os.name != "nt":
        kwargs.setdefault("start_new_session", True)
    else:
        # Long paths: the bundled tools cannot open past MAX_PATH (see
        # tool_path), so every path argument — and the working directory —
        # is expressed in a form they can reach. No-op for a short path and
        # for every tool call on POSIX.
        args = _argv_with_tool_paths(args)
        if kwargs.get("cwd"):
            kwargs["cwd"] = tool_path(kwargs["cwd"])

    proc = subprocess.Popen(*args, **kwargs)
    with _ACTIVE_LOCK:
        _ACTIVE[proc.pid] = proc
    try:
        stdout, stderr = proc.communicate(input=input_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except Exception:
                stdout, stderr = b"", b""
        raise
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.pop(proc.pid, None)

    result = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
    if check and proc.returncode:
        raise subprocess.CalledProcessError(
            proc.returncode, proc.args, output=stdout, stderr=stderr
        )
    return result
