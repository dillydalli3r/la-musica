"""Subprocess wrapper that never flashes console windows.

The GUI runs windowed (pythonw / PyInstaller --windowed), so it owns no
console of its own. On Windows every console executable launched by such a
process would otherwise create its own console window - and with dozens of
encoder threads running, that means a storm of flashing windows during
Run All. Every external tool invocation therefore goes through run_tool(),
which passes CREATE_NO_WINDOW. Output is already captured via pipes, so
nothing is lost.
"""

import os
import signal
import subprocess
import threading

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_ACTIVE = {}
_ACTIVE_LOCK = threading.Lock()


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
