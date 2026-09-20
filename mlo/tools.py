"""Auto-detection of external encoder tools in the .dependencies folder."""
import os
import re

from .paths import DEPS_DIR

# Vendored pip packages whose import name differs from the pip name.
PIP_IMPORT_NAMES = {"yt-dlp": "yt_dlp"}

# The folder the SHELL that started this backend ships tools in, when the build
# has any. A mobile build cannot install into .dependencies the way a desktop
# one does: an app bundle is read-only, and the only place Android lets an app
# keep an executable is the APK's own native-lib folder (see tools/mobile).
# The launcher knows that path at runtime and passes it here; nothing is
# assumed about the layout beyond "one flat directory of binaries".
BUNDLED_TOOLS_ENV = "MLO_BUNDLED_TOOLS"


def bundled_tools_dir():
    """The directory this build ships external tools in, or None."""
    root = os.environ.get(BUNDLED_TOOLS_ENV)
    return root if root and os.path.isdir(root) else None


def _bundled_file(root, name, exe=True):
    """A bundled file called *name*, in whichever spelling a mobile build uses.

    Android's native-lib folder only loads names of the shape lib<name>.so —
    that is what a bundled ffmpeg is called there — while an extracted assets
    folder keeps the plain name. Both are tried so the shell is free to pick
    either, and a `.exe` is accepted for a desktop build that bundles its own.
    """
    names = [name, name + ".exe", f"lib{name}.so", name + ".so"] if exe else [name]
    for cand in names:
        path = os.path.join(root, cand)
        if os.path.isfile(path):
            return path
    return None


def _detect_bundled_tools(root):
    """The tools *root* ships, in the same shapes the tables below use.

    Assembled by NAME, not by scanning versioned folders: a bundled tool has no
    version directory to scan, and a mobile build has no way to install a
    second copy beside it.
    """
    tools = {}

    def add(key, exe=True, **names):
        found = {k: _bundled_file(root, v, exe) for k, v in names.items()}
        if any(found.values()):
            tools[key] = {"version": None, **found}

    add("flac", flac_exe="flac", metaflac_exe="metaflac")
    add("libjxl", cjxl_exe="cjxl", djxl_exe="djxl")
    add("libjpeg_turbo", jpegtran_exe="jpegtran")
    add("oxipng", oxipng_exe="oxipng")
    add("audioauditor", cli_exe="AudioAuditorCLI")
    add("rsgain", rsgain_exe="rsgain")
    add("ffmpeg", ffmpeg_exe="ffmpeg", ffprobe_exe="ffprobe")
    add("php", php_exe="php")
    add("yt-dlp", ytdlp_exe="yt-dlp")
    add("chromaprint", fpcalc_exe="fpcalc")
    add("slskd", slskd_exe="slskd")
    add("cuetools", exe="CUETools", arcue_exe="CUETools.ARCUE")
    # The rip-log checker is a PHP phar, not a program of its own.
    add("logchecker", exe=False, phar_path="logchecker.phar")
    return tools


def _with_bundled(tools):
    """Overlay the tools this BUILD ships over *tools* (see bundled_tools_dir).

    A bundled copy WINS over PATH and .dependencies: it is the one built for
    this device, and preferring a desktop install over it would run the wrong
    architecture.
    """
    root = bundled_tools_dir()
    if not root:
        return tools
    merged = dict(tools)
    merged.update(_detect_bundled_tools(root))
    return merged

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


# How to ask each tool for its own version, and the fact that a PATH-installed
# tool has to be ASKED: a distro package has no versioned folder name to read
# (that is how a .dependencies install reports one, see _detect_tool), so the
# Dependencies table showed "—" for flac, ffmpeg, rsgain, fpcalc, cjxl and
# jpegtran — and a row with no installed version can never be compared with
# what upstream ships, which is exactly what the table is for. The flags are the
# tools' own (jpegtran and fpcalc print to stderr; both are read).
_VERSION_ARGS = {
    "flac": ("--version",),
    "libjxl": ("--version",),        # cjxl
    "libjpeg_turbo": ("-version",),  # jpegtran
    "oxipng": ("--version",),
    "rsgain": ("--version",),
    "ffmpeg": ("-version",),
    "chromaprint": ("-version",),    # fpcalc
    "slskd": ("--version",),
    "yt-dlp": ("--version",),
}

# The first dotted number in a tool's version banner: "flac 1.5.0",
# "ffmpeg version 7.1.5-0+deb13u1 Copyright …" -> 7.1.5, "libjpeg-turbo version
# 2.1.5 (build 20250503)" -> 2.1.5 (the build date is not the version).
_VERSION_RX = re.compile(r"\d+(?:\.\d+)+")


def _first_version(text):
    """The first dotted number in *text*, or None when there is none."""
    found = _VERSION_RX.search(text or "")
    return found.group(0) if found else None


def _probe_version(exe, args=("--version",)):
    """Ask *exe* for its version, None when it will not say.

    Never raises: a tool that is present but mute (or that hangs until the
    timeout) must not take the whole detection pass down with it — the version
    is a label, not the detection itself.
    """
    if not exe:
        return None
    try:
        from .subproc import run_tool
        result = run_tool([exe, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=15)
    except Exception:
        return None
    return _first_version(f"{result.stdout or ''}\n{result.stderr or ''}")


def _system_entry(key, **paths):
    """A detected system tool, with the version its own binary reports."""
    main = next((p for p in paths.values() if p), None)
    return {"version": _probe_version(main, _VERSION_ARGS[key]), **paths}


def _detect_system_tools():
    """Non-Windows detection: .dependencies installs, then distro tools on PATH.

    Docker/Linux/macOS: the app's own installs (native binaries, pip packages)
    come first, and anything else is whatever the system provides — including
    the version the system's copy reports, so the row can be compared with
    upstream instead of showing a blank.
    """
    import shutil

    tools = {}

    flac = shutil.which("flac")
    if flac:
        tools["flac"] = _system_entry(
            "flac", flac_exe=flac, metaflac_exe=shutil.which("metaflac"))

    cjxl = shutil.which("cjxl")
    djxl = shutil.which("djxl")
    if cjxl and djxl:
        tools["libjxl"] = _system_entry("libjxl", cjxl_exe=cjxl, djxl_exe=djxl)

    jpegtran = shutil.which("jpegtran")
    if jpegtran:
        tools["libjpeg_turbo"] = _system_entry(
            "libjpeg_turbo", jpegtran_exe=jpegtran)

    oxipng = shutil.which("oxipng")
    if oxipng:
        tools["oxipng"] = _system_entry("oxipng", oxipng_exe=oxipng)

    ffmpeg, ffprobe = _which_pair("ffmpeg.exe")
    if not ffmpeg:
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe") if ffmpeg else None
    if ffmpeg and ffprobe:
        tools["ffmpeg"] = _system_entry(
            "ffmpeg", ffmpeg_exe=ffmpeg, ffprobe_exe=ffprobe)

    rsgain = shutil.which("rsgain")
    if rsgain:
        tools["rsgain"] = _system_entry("rsgain", rsgain_exe=rsgain)

    # chromaprint's fpcalc: the Linux counterpart of the Windows download
    # (Debian/Ubuntu: libchromaprint-tools) - mlo/acoustid.py resolves it from
    # PATH on its own, this is only so Dependencies shows it.
    fpcalc = shutil.which("fpcalc")
    if fpcalc:
        tools["chromaprint"] = _system_entry("chromaprint", fpcalc_exe=fpcalc)

    # slskd is the one dependency this app RUNS rather than invokes; the
    # Soulseek page starts it, and the capability report has to see the same
    # install (server/soulseek.py resolves it from .dependencies on its own).
    slskd = shutil.which("slskd")
    if slskd:
        tools["slskd"] = _system_entry("slskd", slskd_exe=slskd)

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
            tools["yt-dlp"] = _system_entry("yt-dlp", ytdlp_exe=ytdlp)

    # A native install under .dependencies LAST, so it wins over a copy on
    # PATH: it is the versioned one the Dependencies page reports and updates,
    # and without this the app could install oxipng or slskd and still call
    # them missing.
    tools.update(_detect_deps_native())

    return tools


def _detect_deps_native():
    """Tools installed as native binaries under .dependencies (POSIX only).

    fetchdeps installs native Linux builds there (oxipng, slskd - see
    fetchdeps.LINUX_BINARIES) and the .exe scan above cannot see them. A
    Windows host sharing this folder must never pick one up: an .exe-less
    folder is a file it cannot execute.
    """
    from .fetchdeps import INSTALL_PREFIX, LINUX_BINARIES

    tools = {}
    if os.name == "nt":
        return tools
    for key, spec in LINUX_BINARIES.items():
        version, folder = _detect_tool(INSTALL_PREFIX.get(key, key), DEPS_DIR)
        if not folder:
            continue
        d = os.path.join(DEPS_DIR, folder)
        if all(os.path.isfile(os.path.join(d, m)) for m in spec["markers"]):
            tools[key] = {"version": version,
                          f"{key}_exe": os.path.join(d, spec["markers"][0])}
    return tools


def detect_all_tools():
    sig = deps_dir_signature()
    with _CACHE_LOCK:
        if _TOOLS_CACHE is not None and _TOOLS_CACHE_SIG == sig:
            return _TOOLS_CACHE

    # Off-Windows the tools are the app's own installs under .dependencies
    # (native binaries and pip packages - fetchdeps installs those there, see
    # _detect_deps_native) plus the distro/Homebrew ones on PATH. The .exe
    # scan below is Windows-only: a folder of Windows binaries must never be
    # selected as an install on a host that cannot run them.
    if os.name != "nt":
        return _store_tools_cache(_with_bundled(_detect_system_tools()), sig)

    tools = {}

    if not os.path.isdir(DEPS_DIR):
        return _store_tools_cache(_with_bundled(_detect_system_tools()), sig)

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

    # fpcalc: the AcoustID fingerprinter. The Windows installer has always
    # put it in .dependencies, but only the PATH scan could see it — so the
    # Dependencies table said "ready" while every AcoustID lookup reported the
    # tool missing. Detected here like the rest, so both agree.
    cv, cf = _detect_tool("chromaprint", DEPS_DIR)
    if cf:
        d = os.path.join(DEPS_DIR, cf)
        if os.path.isfile(os.path.join(d, "fpcalc.exe")):
            tools["chromaprint"] = {
                "version": cv,
                "fpcalc_exe": os.path.join(d, "fpcalc.exe"),
            }

    # slskd is a folder of its own under .dependencies (server/soulseek.py
    # looks for the same slskd.exe through fetchdeps.installed_path), and the
    # capability report reads the detected tools, so it is detected here too.
    sv, sf = _detect_tool("slskd", DEPS_DIR)
    if sf:
        d = os.path.join(DEPS_DIR, sf)
        if os.path.isfile(os.path.join(d, "slskd.exe")):
            tools["slskd"] = {
                "version": sv,
                "slskd_exe": os.path.join(d, "slskd.exe"),
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
                "rsgain", "chromaprint", "slskd", "yt-dlp"):
        if key not in tools and key in system:
            tools[key] = system[key]

    return _store_tools_cache(_with_bundled(tools), sig)


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

