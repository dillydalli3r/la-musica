"""Command-line front-end for the Music Library Optimizer.

Non-interactive argparse CLI plus PATH management (install --user /
--system, with automatic UAC elevation for the system scope). The
interactive console menu stays available via `mlo menu` / `python -m mlo`.
"""
import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile

from . import __version__
from .audit import run_audit_library
from .autotag import run_auto_tagging
from .cue import run_format_cues
from .deps import HAS_MUTAGEN
from .flac import run_optimize_flacs
from .grader import run_grade_library
from .images import run_process_images
from .loudness import run_calc_dr_replaygain
from .lyrics import run_format_lyrics
from .lyrics_fetch import run_fetch_lyrics
from .accurip import run_generate_accurip
from .audiometa import run_analyze_audiometa
from .format_all import run_format_all
from .remux import run_remux_videos
from .report import print_results, print_grade_results, print_combined_results
from .config import load_config, save_config, DEFAULT_CONFIG
from .paths import SCRIPT_DIR, DEPS_DIR, HOME_MARKER
from .ui import c, Color

APP_NAME = "Music Library Optimizer"
USER_INSTALL_DIR = os.path.expandvars(
    r"%LocalAppData%\Programs\Music Library Optimizer")
SYSTEM_INSTALL_DIR = r"C:\Program Files\Music Library Optimizer"

def _run_beets_tagging(config):
    """Beets tagging lives with the server integration (vendored beets +
    managed config). Imported lazily so the plain CLI still works when the
    server package isn't on sys.path."""
    try:
        from server.beetscfg import run_beets_tagging
    except Exception:
        from .stats import new_stats
        from .ui import Color, c, log, print_header
        print_header("Beets Tagging (MusicBrainz)")
        log(c("beets tagging needs the server package — run from the repo "
              "checkout, or use Run All in the web app.", Color.YELLOW))
        return new_stats()
    return run_beets_tagging(config)


def _run_lyrics_xlit(config):
    """Script 15 wrapper: AI transforms need the server AI client (httpx),
    so import lazily exactly like the beets runner above."""
    try:
        from .lyrics_xlit import run_lyrics_xlit
    except Exception:
        from .stats import new_stats
        from .ui import Color, c, log, print_header
        print_header("Lyrics Translate/Transliterate")
        log(c("script 15 needs the 'httpx' package (server install).",
              Color.YELLOW))
        return new_stats()
    return run_lyrics_xlit(config)


SCRIPTS = {
    "lyrics": (1, "Format Lyrics", run_format_lyrics),
    "cues": (2, "Format CUEs", run_format_cues),
    "flac": (3, "Optimize FLACs", run_optimize_flacs),
    "grade": (4, "Grade Library", run_grade_library),
    "images": (5, "Process Images", run_process_images),
    "audit": (6, "Audit Library", run_audit_library),
    "dr": (7, "DR & ReplayGain", run_calc_dr_replaygain),
    "autotag": (8, "Auto Tagging", run_auto_tagging),
    "accurip": (9, "AccurateRip", run_generate_accurip),
    "formatall": (10, "Format All", run_format_all),
    "remux": (11, "Video Remux", run_remux_videos),
    "audiometa": (12, "Key & BPM", run_analyze_audiometa),
    "fetchlyrics": (13, "Fetch Lyrics", run_fetch_lyrics),
    "beets": (14, "Beets Tagging", _run_beets_tagging),
    "xlit": (15, "Lyrics Translate/Transliterate", _run_lyrics_xlit),
}


# ==========================================================================
# PATH management
# ==========================================================================
def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _elevate_and_run(args):
    """Relaunch the current executable elevated with `args`.

    The elevated child runs hidden and writes its status lines to the
    file named by $MLO_INSTALL_LOG; the parent relays them once the
    child exits. Returns the child's exit code."""
    exe = sys.executable
    params = subprocess.list2cmdline(args)
    if not getattr(sys, "frozen", False):
        # Locate cli entry: prefer mlo_cli.py, fallback to cliapp.py / __main__
        cand = os.path.join(SCRIPT_DIR, "mlo_cli.py")
        if not os.path.isfile(cand):
            cand = os.path.join(SCRIPT_DIR, "mlo", "cliapp.py")
        if not os.path.isfile(cand):
            cand = os.path.join(SCRIPT_DIR, "mlo", "__main__.py")
        script = cand
        params = subprocess.list2cmdline([script] + args)

    fd, log_path = tempfile.mkstemp(prefix="mlo_install_", suffix=".log")
    os.close(fd)
    os.environ["MLO_INSTALL_LOG"] = log_path

    import ctypes.wintypes

    class SEE_MASK_FLAGS:
        SEE_MASK_NOCLOSEPROCESS = 0x00000040

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", ctypes.wintypes.HANDLE),
            ("lpVerb", ctypes.c_wchar_p),
            ("lpFile", ctypes.c_wchar_p),
            ("lpParameters", ctypes.c_wchar_p),
            ("lpDirectory", ctypes.c_wchar_p),
            ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", ctypes.c_wchar_p),
            ("hkeyClass", ctypes.wintypes.HKEY),
            ("dwHotKey", ctypes.wintypes.DWORD),
            ("hIconOrMonitor", ctypes.wintypes.HANDLE),
            ("hProcess", ctypes.wintypes.HANDLE),
        ]

    sei = ShellExecuteInfo()
    sei.cbSize = ctypes.sizeof(ShellExecuteInfo)
    sei.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.lpDirectory = SCRIPT_DIR
    sei.nShow = 0  # SW_HIDE

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        print(c("Elevation was declined or failed.", Color.RED))
        os.environ.pop("MLO_INSTALL_LOG", None)
        try:
            os.remove(log_path)
        except OSError:
            pass
        return 1

    KERNEL32 = ctypes.windll.kernel32
    WAIT_TIMEOUT = 0x00000102
    if KERNEL32.WaitForSingleObject(sei.hProcess, 120000) == WAIT_TIMEOUT:
        KERNEL32.TerminateProcess(sei.hProcess, 1)
        KERNEL32.CloseHandle(sei.hProcess)
        print(c("Elevated install timed out after 120 s.", Color.RED))
        os.environ.pop("MLO_INSTALL_LOG", None)
        try:
            os.remove(log_path)
        except OSError:
            pass
        return 1
    code = ctypes.wintypes.DWORD()
    KERNEL32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
    KERNEL32.CloseHandle(sei.hProcess)

    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f.read().splitlines():
                print("  " + line)
    except OSError:
        pass
    finally:
        os.environ.pop("MLO_INSTALL_LOG", None)
        try:
            os.remove(log_path)
        except OSError:
            pass
    return code.value


def _log_status(message):
    log_path = os.environ.get("MLO_INSTALL_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(message + "\n")
    else:
        print(message)


def _path_registry_locations(scope):
    """(root, subkey) for the requested scope's PATH storage."""
    import winreg
    if scope == "user":
        return winreg.HKEY_CURRENT_USER, "Environment"
    return (winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")


def _read_path(root, subkey):
    """Return (entries, value_type) or (None, None) when missing."""
    import winreg
    try:
        with winreg.OpenKey(root, subkey, 0,
                            winreg.KEY_QUERY_VALUE) as k:
            value, vtype = winreg.QueryValueEx(k, "Path")
            return [p for p in str(value).split(";") if p.strip()], vtype
    except FileNotFoundError:
        return [], winreg.REG_EXPAND_SZ
    except OSError as e:
        _log_status(c(f"Could not read PATH: {e}", Color.RED))
        return None, None


def _write_path(root, subkey, entries, vtype):
    import winreg
    try:
        with winreg.OpenKey(root, subkey, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "Path", 0, vtype, ";".join(entries))
        return True
    except OSError as e:
        _log_status(c(f"Could not write PATH: {e}", Color.RED))
        return False


def _broadcast_env_change():
    import ctypes.wintypes
    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    result = ctypes.c_long()
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
        SMTO_ABORTIFHUNG, 5000, ctypes.byref(result))


def _norm(entry):
    return os.path.normcase(os.path.normpath(entry))


def _modify_path(scope, target, add):
    """Add/remove `target` on the PATH of the given scope."""
    import winreg
    root, subkey = _path_registry_locations(scope)
    entries, vtype = _read_path(root, subkey)
    if entries is None:
        return False

    exists = any(_norm(e) == _norm(target) for e in entries)
    if add:
        if exists:
            _log_status(f"PATH already contains {target}")
            return True
        entries.append(target)
    else:
        if not exists:
            _log_status(f"PATH does not contain {target}")
            return True
        entries = [e for e in entries
                   if _norm(e) != _norm(target)]

    if not _write_path(root, subkey, entries, vtype):
        return False
    _broadcast_env_change()
    return True


def install_cli(scope):
    """Install this CLI onto PATH (user or system scope)."""
    if sys.platform != "win32":
        print(c("install is Windows-only.", Color.RED))
        return 1

    if scope == "system" and not _is_admin():
        print("System scope needs administrator rights - requesting "
              "elevation (UAC)…")
        args = ["install", "--system"]
        code = _elevate_and_run(args)
        if code == 0:
            print(c(f"Installed to {SYSTEM_INSTALL_DIR} (system PATH).",
                    Color.GREEN))
        return code

    target_dir = USER_INSTALL_DIR if scope == "user" else SYSTEM_INSTALL_DIR
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as e:
        _log_status(c(f"Could not create {target_dir}: {e}", Color.RED))
        return 1

    if getattr(sys, "frozen", False):
        src = os.path.abspath(sys.executable)
        dst = os.path.join(target_dir, "mlo.exe")
        try:
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copy2(src, dst)
            # Shared config / toolchain stay with the original folder.
            with open(os.path.join(target_dir, HOME_MARKER), "w",
                      encoding="utf-8") as f:
                f.write(SCRIPT_DIR)
        except OSError as e:
            _log_status(c(f"Could not copy CLI: {e}", Color.RED))
            return 1
    else:
        shim = os.path.join(target_dir, "mlo.cmd")
        entry = os.path.join(SCRIPT_DIR, "mlo_cli.py")
        if not os.path.isfile(entry):
            entry = os.path.join(SCRIPT_DIR, "mlo", "cliapp.py")
        try:
            with open(shim, "w", encoding="utf-8", newline="\r\n") as f:
                f.write('@echo off\r\n'
                        f'"{sys.executable}" "{entry}" %*\r\n')
        except OSError as e:
            _log_status(c(f"Could not write shim: {e}", Color.RED))
            return 1

    if not _modify_path(scope, target_dir, add=True):
        return 1

    _log_status(c(f"Installed: {target_dir}", Color.GREEN))
    _log_status("Open a new terminal, then run:  mlo version")
    return 0


def uninstall_cli(scope):
    if sys.platform != "win32":
        print(c("uninstall is Windows-only.", Color.RED))
        return 1

    if scope == "system" and not _is_admin():
        print("System scope needs administrator rights - requesting "
              "elevation (UAC)…")
        code = _elevate_and_run(["uninstall", "--system"])
        if code == 0:
            print(c(f"Removed from {SYSTEM_INSTALL_DIR} (system PATH).",
                    Color.GREEN))
        return code

    target_dir = USER_INSTALL_DIR if scope == "user" else SYSTEM_INSTALL_DIR
    if not _modify_path(scope, target_dir, add=False):
        return 1

    removed_any = False
    in_use = []
    for name in ("mlo.exe", "mlo.cmd", HOME_MARKER):
        p = os.path.join(target_dir, name)
        if os.path.isfile(p):
            try:
                os.remove(p)
                removed_any = True
            except OSError:
                in_use.append(name)
    try:
        if os.path.isdir(target_dir) and not os.listdir(target_dir):
            os.rmdir(target_dir)
    except OSError:
        pass

    if removed_any and not in_use:
        print(c("Uninstalled.", Color.GREEN))
    elif in_use:
        print(c(f"Removed from PATH, but could not delete "
                f"{', '.join(in_use)} (in use). Re-run uninstall after "
                f"closing this terminal.", Color.YELLOW))
    else:
        print("Nothing to remove - the PATH entry was already absent.")
    print("Config and .dependencies are left in place.")
    return 0


# ==========================================================================
# Run commands
# ==========================================================================
def _resolve_script_ids(spec):
    if spec.lower() == "all":
        cfg = load_config()
        from .config import DEFAULT_RUN_ALL_ORDER
        return list(cfg.get("run_all_order", DEFAULT_RUN_ALL_ORDER)), "RUN ALL"
    ids = []
    for part in spec.replace(" ", "").split(","):
        if part.isdigit() and 1 <= int(part) <= 15:
            sid = int(part)
            if sid not in ids:
                ids.append(sid)
        else:
            key = part.lower()
            if key in SCRIPTS:
                sid = SCRIPTS[key][0]
                if sid not in ids:
                    ids.append(sid)
            else:
                print(c(f"Unknown script '{part}'. Use 1-{len(SCRIPTS)} or "
                        "lyrics/cues/flac/grade/images/audit/dr/autotag/accurip/formatall/remux/audiometa/fetchlyrics/beets.",
                        Color.RED))
                return None, None
    return ids, f"RUN {spec.upper()}"


def cmd_run(args):
    ids, title = _resolve_script_ids(args.scripts)
    if not ids:
        print(c("Nothing to run.", Color.RED))
        return 1

    config = load_config()
    if args.folder:
        config["music_folder"] = args.folder
    if args.targets:
        config["targets"] = list(args.targets)
    if args.force:
        config["force_reencode_flac"] = True
        config["force_reencode_images"] = True
        config["force_audit"] = True
        config["force_lyrics"] = True
        config["force_cue"] = True
        config["force_dr_replaygain"] = True
        config["force_auto_tag"] = True
    if args.thorough:
        config["audit_thorough"] = True

    folder = config.get("music_folder", "")
    if not folder or not os.path.isdir(folder):
        print(c(f"Music folder not set or missing: "
                f"{folder or '(empty)'}", Color.RED))
        print("Set it with:  mlo config music_folder \"C:\\path\\to\\music\"")
        return 1

    print(f">>> {title}: "
          + " -> ".join(str(i) for i in ids))

    per_script = []
    for sid in ids:
        _num, name, runner = next(
            (v for k, v in SCRIPTS.items() if v[0] == sid))
        print(c(f"--- {name} ---", Color.CYAN))
        s = runner(config)
        per_script.append((name, s))

    if len(per_script) > 1:
        print_combined_results(per_script, title="COMBINED RESULTS")
    return 0 if all(s.get("error_count", 0) == 0 for _n, s in per_script) \
        else 2


def cmd_config(args):
    config = load_config()
    if not args.key:
        width = max(len(k) for k in DEFAULT_CONFIG) + 2
        for key in sorted(config):
            print(f"  {key:{width}} {config[key]}")
        return 0
    if args.key not in config and args.key not in DEFAULT_CONFIG:
        print(c(f"Unknown key: {args.key}", Color.RED))
        return 1
    if args.value is None:
        print(f"  {args.key} = {config.get(args.key)!r}")
        return 0
    raw = " ".join(args.value)
    default = DEFAULT_CONFIG.get(args.key)
    try:
        if isinstance(default, bool):
            val = raw.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int) and not isinstance(default, bool):
            val = int(raw)
        else:
            val = raw
    except ValueError:
        val = raw
    config[args.key] = val
    if not save_config(config):
        return 1
    print(c(f"Saved {args.key} = {val!r}", Color.GREEN))
    return 0


def cmd_deps(args):
    from . import fetchdeps
    from . import tools as tools_mod

    installed = fetchdeps.installed_versions()
    if args.install is None and not args.check:
        try:
            latest = fetchdeps.latest_versions()
        except Exception as e:
            print(c(f"Could not query GitHub: {e}", Color.RED))
            latest = {}
        for key, name in fetchdeps.DISPLAY_NAMES.items():
            iv = installed.get(key, "-")
            lv = latest.get(key, "?")
            state = (c("up to date", Color.GREEN) if iv == lv and iv != "-"
                     else c("update available", Color.YELLOW) if iv != "-"
                     else c("not installed", Color.RED))
            print(f"  {name:<15} installed {iv:<10} latest {lv:<10} {state}")
        return 0

    keys = list(fetchdeps.DISPLAY_NAMES) \
        if args.install == "all" or args.install == "" \
        else [k.strip() for k in (args.install or "").split(",")]
    for key in keys:
        name = fetchdeps.DISPLAY_NAMES.get(key)
        if not name:
            print(c(f"Unknown dependency: {key}", Color.RED))
            continue
        print(f"Installing {name}…")
        try:
            version = fetchdeps.install_dependency(
                key, log=lambda m: print(f"  {m}"))
            print(c(f"  {name} v{version} installed", Color.GREEN))
        except Exception as e:
            print(c(f"  FAILED {name}: {e}", Color.RED))
    fetchdeps.refresh_tool_cache()
    tools = tools_mod.detect_all_tools()
    print(c(f"Detected {len(tools)}/"
            f"{len(fetchdeps.DISPLAY_NAMES)} tools.", Color.GREEN))
    return 0


def cmd_menu(_args):
    from .cli import main as menu_main
    menu_main()
    return 0


def cmd_gui(_args):
    """Open the web app (backend + browser via the tray launcher)."""
    from pathlib import Path
    tray = Path(SCRIPT_DIR) / "tray.py"
    if tray.is_file():
        subprocess.Popen([sys.executable, str(tray)], cwd=SCRIPT_DIR)
        return 0
    start = Path(SCRIPT_DIR) / "start_app.py"
    if start.is_file():
        subprocess.Popen([sys.executable, str(start)], cwd=SCRIPT_DIR)
        return 0
    print(c("No launcher found (tray.py / start_app.py).", Color.RED))
    return 1


def build_parser():
    p = argparse.ArgumentParser(
        prog="mlo",
        description="Music Library Optimizer - lossless audio & image "
                    "processing suite (CLI).")
    p.add_argument("--version", action="version",
                   version=f"Music Library Optimizer {__version__}")

    sub = p.add_subparsers(dest="command")

    def add_common(sp):
        sp.add_argument("--folder", metavar="DIR",
                        help="override the music folder for this run")
        sp.add_argument("--targets", nargs="+", metavar="PATH",
                        help="restrict the run to these files/dirs")
        sp.add_argument("--force", action="store_true",
                        help="force re-encoding / re-auditing")
        sp.add_argument("--thorough", action="store_true",
                        help="thorough audio audit (slower)")

    run = sub.add_parser("run", help="run scripts, e.g. 'mlo run 1,2,3' "
                                     "or 'mlo run all'")
    run.add_argument("scripts", help="script numbers/names or 'all'")
    add_common(run)
    run.set_defaults(func=cmd_run)

    for cmd, (_num, name, _runner) in SCRIPTS.items():
        sp = sub.add_parser(cmd, help=f"{name} (script {_num})")
        add_common(sp)
        sp.set_defaults(func=cmd_run, scripts=str(_num))

    allsp = sub.add_parser(
        "all", help="run the scripts in your configured Run All order")
    allsp.add_argument("scripts", nargs="?", default="all",
                       help=argparse.SUPPRESS)
    add_common(allsp)
    allsp.set_defaults(func=cmd_run)

    cfg = sub.add_parser("config", help="show or set settings")
    cfg.add_argument("key", nargs="?", help="setting key")
    cfg.add_argument("value", nargs="*", help="new value")
    cfg.set_defaults(func=cmd_config)

    deps = sub.add_parser("deps", help="manage the external toolchain")
    deps.add_argument("--install", nargs="?", const="all", default=None,
                      metavar="all|name",
                      help="install/update dependencies (default: all)")
    deps.add_argument("--check", action="store_true",
                      help="only show installed versions")
    deps.set_defaults(func=cmd_deps)

    inst = sub.add_parser(
        "install", help="install this CLI onto PATH (User or System)")
    scope = inst.add_mutually_exclusive_group(required=True)
    scope.add_argument("--user", action="store_const", const="user",
                       dest="scope",
                       help="install for the current user (no admin)")
    scope.add_argument("--system", action="store_const", const="system",
                       dest="scope",
                       help="install for all users (needs admin; UAC "
                            "elevation is requested automatically)")
    inst.set_defaults(func=lambda a: install_cli(a.scope))

    uninst = sub.add_parser("uninstall", help="remove the CLI from PATH")
    scope = uninst.add_mutually_exclusive_group(required=True)
    scope.add_argument("--user", action="store_const", const="user",
                       dest="scope")
    scope.add_argument("--system", action="store_const", const="system",
                       dest="scope")
    uninst.set_defaults(func=lambda a: uninstall_cli(a.scope))

    menu = sub.add_parser("menu", help="interactive console menu")
    menu.set_defaults(func=cmd_menu)

    gui = sub.add_parser("gui", help="launch the desktop GUI")
    gui.set_defaults(func=cmd_gui)

    ver = sub.add_parser("version", help="print the version")
    ver.set_defaults(func=lambda _a: (
        print(f"Music Library Optimizer {__version__}"), 0)[1])

    return p


def main(argv=None):
    if os.name == "nt":
        os.system("")   # enable ANSI escapes

    if not HAS_MUTAGEN:
        print(c("ERROR: mutagen is required.  pip install mutagen",
                Color.RED))
        return 1

    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
