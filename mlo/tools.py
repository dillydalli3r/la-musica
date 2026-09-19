"""Auto-detection of external encoder tools in the .dependencies folder."""
import os
import re

from .paths import DEPS_DIR

# Vendored pip packages whose import name differs from the pip name.
PIP_IMPORT_NAMES = {"yt-dlp": "yt_dlp"}

def _parse_version(s):
    if not s:
        return None
    clean = str(s).strip().lstrip("vV")
    parts = []
    for p in clean.split("."):
        m = re.match(r"(\d+)", p)
        parts.append(int(m.group(1)) if m else 0)
    return tuple(parts) if parts else None


def _version_is_older(a, b):
    va, vb = _parse_version(a), _parse_version(b)
    if va is None or vb is None:
        return False
    return va < vb


def _detect_tool(prefix, deps_dir):
    if not os.path.isdir(deps_dir):
        return None, None

    cands = []
    unversioned = None
    for entry in os.listdir(deps_dir):
        full = os.path.join(deps_dir, entry)
        if not os.path.isdir(full):
            continue
        if not entry.lower().startswith(prefix.lower()):
            continue
        m = re.search(r"v?\s*(\d+(?:\.\d+)*)", entry)
        if not m:
            # Rolling installs (e.g. "ffmpeg vlatest") have no parseable
            # version; keep them as lowest-priority candidates.
            unversioned = full
            continue
        try:
            v = tuple(int(x) for x in m.group(1).split("."))
        except ValueError:
            continue
        cands.append((v, m.group(1), entry))

    if not cands:
        if unversioned and os.path.isdir(unversioned):
            return None, os.path.basename(unversioned)
        return None, None

    cands.sort(reverse=True)
    return cands[0][1], cands[0][2]


_TOOLS_CACHE = None
# The .dependencies folder as it looked when _TOOLS_CACHE was built. Installing
# a tool adds a folder there, so the cache below re-detects instead of serving
# a stale "not installed" for the rest of the process — a session that installs
# a tool through any path (Dependencies UI, CLI, auto-update) sees it at the
# next detect_all_tools() call, not at the next restart.
_TOOLS_CACHE_SIG = None
_CACHE_LOCK = __import__("threading").Lock()


def deps_dir_signature():
    """Signature of the dependencies folder: changes when a tool is installed.

    detect_all_tools() keys its cache on this (and the per-module ffmpeg /
    ffprobe latches can do the same) so a mid-session install becomes visible
    without a manual refresh_tool_cache() call.
    """
    try:
        return os.stat(DEPS_DIR).st_mtime_ns
    except OSError:
        return None


def _store_tools_cache(tools, sig):
    """Publish *tools* as the process-wide detection result."""
    global _TOOLS_CACHE, _TOOLS_CACHE_SIG
    with _CACHE_LOCK:
        _TOOLS_CACHE = tools
        _TOOLS_CACHE_SIG = sig
    return tools


def _which_pair(exe_name):
    """Find exe+probe on PATH, skipping Microsoft Store alias stubs.

    WindowsApps execution aliases shadow real installs (the alias is a
    0-byte reparse point that fails when spawned), so walk PATH in order
    and prefer a directory whose exe is a real binary (>1 MB). Returns
    (ffmpeg_path, ffprobe_path) or (None, None).
    """
    probe_name = "ffprobe.exe" if exe_name.lower() == "ffmpeg.exe" else None
    real = None
    any_hit = None
    try:
        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    except Exception:
        path_dirs = []
    for d in path_dirs:
        if not d or not os.path.isdir(d):
            continue
        exe = os.path.join(d, exe_name)
        if not os.path.isfile(exe):
            continue
        try:
            size = os.path.getsize(exe)
        except OSError:
            continue
        if size <= 1:
            continue  # 0-byte app-execution alias
        if probe_name:
            probe = os.path.join(d, probe_name)
            if not os.path.isfile(probe):
                continue
            pair = (exe, probe)
        else:
            pair = (exe, None)
        if any_hit is None:
            any_hit = pair
        if size > 1024 * 1024:
            real = pair
            break
    return real or any_hit or (None, None)


def _detect_system_tools():
    """Non-Windows fallback: detect distro tools on PATH (Docker/Linux/macOS)."""
    import shutil

    tools = {}

    flac = shutil.which("flac")
    if flac:
        tools["flac"] = {
            "version": None,
            "flac_exe": flac,
            "metaflac_exe": shutil.which("metaflac"),
        }

    cjxl = shutil.which("cjxl")
    djxl = shutil.which("djxl")
    if cjxl and djxl:
        tools["libjxl"] = {"version": None, "cjxl_exe": cjxl, "djxl_exe": djxl}

    jpegtran = shutil.which("jpegtran")
    if jpegtran:
        tools["libjpeg_turbo"] = {"version": None, "jpegtran_exe": jpegtran}

    oxipng = shutil.which("oxipng")
    if oxipng:
        tools["oxipng"] = {"version": None, "oxipng_exe": oxipng}

    ffmpeg, ffprobe = _which_pair("ffmpeg.exe")
    if not ffmpeg:
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe") if ffmpeg else None
    if ffmpeg and ffprobe:
        tools["ffmpeg"] = {"version": None, "ffmpeg_exe": ffmpeg, "ffprobe_exe": ffprobe}

    rsgain = shutil.which("rsgain")
    if rsgain:
        tools["rsgain"] = {"version": None, "rsgain_exe": rsgain}

    # chromaprint's fpcalc: the Linux counterpart of the Windows download
    # (Debian/Ubuntu: libchromaprint-tools) - mlo/acoustid.py resolves it from
    # PATH on its own, this is only so Dependencies shows it.
    fpcalc = shutil.which("fpcalc")
    if fpcalc:
        tools["chromaprint"] = {"version": None, "fpcalc_exe": fpcalc}

    # yt-dlp: on Linux the vendored pip package (fetchdeps installs it with
    # `pip --target`, see PIP_ON_LINUX) or a distro/pip install on PATH. There
    # is no Linux binary to point at, so ytdlp_exe stays None for the vendored
    # package - server/youtube.py imports the module instead.
    vendored_ytdlp = python_pkg_path("yt-dlp")
    if vendored_ytdlp:
        tools["yt-dlp"] = {"version": None, "ytdlp_exe": None,
                           "python_path": vendored_ytdlp}
    else:
        ytdlp = shutil.which("yt-dlp")
        if ytdlp:
            tools["yt-dlp"] = {"version": None, "ytdlp_exe": ytdlp}

    return tools


def detect_all_tools():
    sig = deps_dir_signature()
    with _CACHE_LOCK:
        if _TOOLS_CACHE is not None and _TOOLS_CACHE_SIG == sig:
            return _TOOLS_CACHE

    # .dependencies only ever holds Windows binaries (fetchdeps refuses to
    # install anything else off-Windows), so on Linux/macOS the distro
    # packages on PATH ARE the tools - never select an unrunnable .exe folder.
    if os.name != "nt":
        return _store_tools_cache(_detect_system_tools(), sig)

    tools = {}

    if not os.path.isdir(DEPS_DIR):
        return _store_tools_cache(_detect_system_tools(), sig)

    fv, ff = _detect_tool("flac", DEPS_DIR)
    if ff:
        d = os.path.join(DEPS_DIR, ff)
        if os.path.isfile(os.path.join(d, "flac.exe")):
            tools["flac"] = {
                "version": fv,
                "flac_exe": os.path.join(d, "flac.exe"),
                "metaflac_exe": (
                    os.path.join(d, "metaflac.exe")
                    if os.path.isfile(os.path.join(d, "metaflac.exe"))
                    else None
                ),
            }

    jv, jf = _detect_tool("libjxl", DEPS_DIR)
    if jf:
        d = os.path.join(DEPS_DIR, jf)
        if os.path.isfile(os.path.join(d, "cjxl.exe")):
            tools["libjxl"] = {
                "version": jv,
                "cjxl_exe": os.path.join(d, "cjxl.exe"),
                "djxl_exe": (
                    os.path.join(d, "djxl.exe")
                    if os.path.isfile(os.path.join(d, "djxl.exe"))
                    else None
                ),
            }

    lv, lf = _detect_tool("libjpeg-turbo", DEPS_DIR)
    if lf:
        d = os.path.join(DEPS_DIR, lf)
        if os.path.isfile(os.path.join(d, "jpegtran.exe")):
            tools["libjpeg_turbo"] = {
                "version": lv,
                "jpegtran_exe": os.path.join(d, "jpegtran.exe"),
            }

    ov, of = _detect_tool("oxipng", DEPS_DIR)
    if of:
        d = os.path.join(DEPS_DIR, of)
        if os.path.isfile(os.path.join(d, "oxipng.exe")):
            tools["oxipng"] = {
                "version": ov,
                "oxipng_exe": os.path.join(d, "oxipng.exe"),
            }

    av, af = _detect_tool("audioauditor", DEPS_DIR)
    if af:
        d = os.path.join(DEPS_DIR, af)
        if os.path.isfile(os.path.join(d, "AudioAuditorCLI.exe")):
            tools["audioauditor"] = {
                "version": av,
                "cli_exe": os.path.join(d, "AudioAuditorCLI.exe"),
            }

    rv, rf = _detect_tool("rsgain", DEPS_DIR)
    if rf:
        d = os.path.join(DEPS_DIR, rf)
        if os.path.isfile(os.path.join(d, "rsgain.exe")):
            tools["rsgain"] = {
                "version": rv,
                "rsgain_exe": os.path.join(d, "rsgain.exe"),
            }

    fv2, ff2 = _detect_tool("ffmpeg", DEPS_DIR)
    if ff2:
        d = os.path.join(DEPS_DIR, ff2)
        if (os.path.isfile(os.path.join(d, "ffmpeg.exe"))
                and os.path.isfile(os.path.join(d, "ffprobe.exe"))):
            tools["ffmpeg"] = {
                "version": fv2,
                "ffmpeg_exe": os.path.join(d, "ffmpeg.exe"),
                "ffprobe_exe": os.path.join(d, "ffprobe.exe"),
            }

    pv, pf = _detect_tool("php", DEPS_DIR)
    if pf:
        d = os.path.join(DEPS_DIR, pf)
        if os.path.isfile(os.path.join(d, "php.exe")):
            tools["php"] = {
                "version": pv,
                "php_exe": os.path.join(d, "php.exe"),
            }

    yv, yf = _detect_tool("yt-dlp", DEPS_DIR)
    if yf:
        d = os.path.join(DEPS_DIR, yf)
        if os.path.isfile(os.path.join(d, "yt-dlp.exe")):
            tools["yt-dlp"] = {
                "version": yv,
                "ytdlp_exe": os.path.join(d, "yt-dlp.exe"),
            }

    lc_v, lc_f = _detect_tool("logchecker", DEPS_DIR)
    if lc_f:
        d = os.path.join(DEPS_DIR, lc_f)
        phar = os.path.join(d, "logchecker.phar")
        if os.path.isfile(phar):
            tools["logchecker"] = {
                "version": lc_v,
                "phar_path": phar,
                "php_exe": tools.get("php", {}).get("php_exe"),
            }

    ct_v, ct_f = _detect_tool("cuetools", DEPS_DIR)
    if ct_f:
        d = os.path.join(DEPS_DIR, ct_f)
        # CUETools.exe is the main exe, may be in CUETools or nested
        exe = None
        arcue = None
        for cand in [os.path.join(d, "CUETools.exe"), os.path.join(d, "cuetools", "CUETools.exe")]:
            if os.path.isfile(cand):
                exe = cand
                break
        for cand in [
            os.path.join(d, "CUETools.ARCUE.exe"),
            os.path.join(d, "cuetools", "CUETools.ARCUE.exe"),
            os.path.join(d, "ArCueDotNet.exe"),
            os.path.join(d, "cuetools", "ArCueDotNet.exe"),
            os.path.join(d, "CUETools.ARCUE.exe".lower()),
        ]:
            if os.path.isfile(cand):
                arcue = cand
                break
        # Fallback scan for any ARCUE-named exe
        if not arcue:
            try:
                for entry in os.listdir(d):
                    low = entry.lower()
                    if "arcue" in low and low.endswith(".exe"):
                        cand = os.path.join(d, entry)
                        if os.path.isfile(cand):
                            arcue = cand
                            break
            except OSError:
                pass
        if exe or arcue or os.path.isdir(d):
            tools["cuetools"] = {
                "version": ct_v,
                "exe": exe or os.path.join(d, "CUETools.exe"),
                "arcue_exe": arcue,
                "dir": d,
            }

    # Fill gaps from system packages (Linux/macOS/Docker) so each tool
    # category resolves even without the .dependencies downloader.
    system = _detect_system_tools()
    for key in ("flac", "libjxl", "libjpeg_turbo", "oxipng", "ffmpeg",
                "rsgain", "chromaprint", "yt-dlp"):
        if key not in tools and key in system:
            tools[key] = system[key]

    return _store_tools_cache(tools, sig)


SIMPLE_DR_METER_DIRNAME = "simple-dr-meter"


def simple_dr_meter_path():
    """Path to simple-dr-meter's main.py, or None when not downloaded."""
    candidate = os.path.join(DEPS_DIR, SIMPLE_DR_METER_DIRNAME, "main.py")
    return candidate if os.path.isfile(candidate) else None


def python_pkg_path(pkg):
    """Vendored pip-package dir for *pkg* ('librosa', 'beets', 'yt-dlp').

    Layout is '.dependencies/<pkg> vX.Y' (see fetchdeps.PIP_PACKAGES); the
    import name can differ from the pip name (yt-dlp -> yt_dlp), hence
    PIP_IMPORT_NAMES.
    """
    if not os.path.isdir(DEPS_DIR):
        return None
    top = PIP_IMPORT_NAMES.get(pkg, pkg)
    try:
        for entry in sorted(os.listdir(DEPS_DIR)):
            full = os.path.join(DEPS_DIR, entry)
            if (os.path.isdir(full) and entry.lower().startswith(pkg.lower())
                    and os.path.isfile(os.path.join(full, top, "__init__.py"))):
                return full
    except OSError:
        pass
    return None

