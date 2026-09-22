"""Auto-detection of external encoder tools in the app's tools folder."""
import os
import re

from .paths import tools_dirs

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


def _detect_tool_dirs(prefix):
    """(`version`, `folder`, `root`) of the newest install of *prefix*, or
    three Nones when no tools folder holds one.

    Reads EVERY tools folder (mlo.paths.tools_dirs), the current one first:
    the tools moved from <app folder>/.dependencies to <music folder>/.mlo/tools,
    and a lookup that only knew the new one would report a tool the user
    installed before the move as missing — the row would then offer to install
    something that is already there.
    """
    for root in tools_dirs():
        version, folder = _detect_tool(prefix, root)
        if folder:
            return version, folder, root
    return None, None, None


_TOOLS_CACHE = None
# The tools folders as they looked when _TOOLS_CACHE was built. Installing a
# tool adds a folder there, so the cache below re-detects instead of serving
# a stale "not installed" for the rest of the process — a session that installs
# a tool through any path (Dependencies UI, CLI, auto-update) sees it at the
# next detect_all_tools() call, not at the next restart.
_TOOLS_CACHE_SIG = None
_CACHE_LOCK = __import__("threading").Lock()


def deps_dir_signature():
    """Signature of the tools folders: changes when a tool is installed.

    detect_all_tools() keys its cache on this (and the per-module ffmpeg /
    ffprobe latches can do the same) so a mid-session install becomes visible
    without a manual refresh_tool_cache() call. Both folders are in it: an
    install into the music folder has to invalidate the cache exactly like one
    into the pre-move folder did.
    """
    sig = []
    for root in tools_dirs():
        try:
            sig.append(os.stat(root).st_mtime_ns)
        except OSError:
            sig.append(None)
    return tuple(sig)


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
# (that is how an installed tool reports one, see _detect_tool), so the
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
    "php": ("-v",),                  # "PHP 8.4.11 (cli) …"
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
    """Non-Windows detection: installs in a tools folder, then distro tools on PATH.

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
    # install (server/soulseek.py resolves it from the tools folder on its own).
    slskd = shutil.which("slskd")
    if slskd:
        tools["slskd"] = _system_entry("slskd", slskd_exe=slskd)

    # PHP: the Logchecker phar's runtime, and nothing else's. The distro
    # package (Debian/Ubuntu: php-cli) is the Linux counterpart of the
    # Windows php zip the installer fetches - without it the phar below is
    # listed but cannot run, which is exactly what logchecker_available()
    # decides on.
    php = shutil.which("php")
    if php:
        tools["php"] = _system_entry("php", php_exe=php)

    # The rip-log scorer is a phar - the SAME file on every platform - so only
    # its runtime is platform-specific (see php above).
    lc_version, lc_folder, lc_root = _detect_tool_dirs("logchecker")
    if lc_folder:
        phar = os.path.join(lc_root, lc_folder, "logchecker.phar")
        if os.path.isfile(phar):
            tools["logchecker"] = {
                "version": lc_version,
                "phar_path": phar,
                "php_exe": tools.get("php", {}).get("php_exe"),
            }

    # yt-dlp: on Linux the vendored pip package (fetchdeps installs it with
    # `pip --target`, see PIP_ON_LINUX) or a distro/pip install on PATH. There
    # is no Linux binary to point at, so ytdlp_exe stays None for the vendored
    # package - server/youtube.py imports the module instead.
    vendored_ytdlp = python_pkg_path("yt-dlp")
    if vendored_ytdlp:
        tools["yt-dlp"] = {"version": python_pkg_version("yt-dlp"),
                           "ytdlp_exe": None,
                           "python_path": vendored_ytdlp}
    else:
        ytdlp = shutil.which("yt-dlp")
        if ytdlp:
            tools["yt-dlp"] = _system_entry("yt-dlp", ytdlp_exe=ytdlp)

    # A native install in a tools folder LAST, so it wins over a copy on
    # PATH: it is the versioned one the Dependencies page reports and updates,
    # and without this the app could install oxipng or slskd and still call
    # them missing.
    tools.update(_detect_deps_native())

    return tools


# How a native install is reported: (field for the executable run_name() names,
# extra (field, file) pairs for the rest). The field names are the SAME names
# the Windows detection uses, because they are what the consumers read: audit.py
# takes `cli_exe`, accurip.py takes `arcue_exe`, images.py reads `cjxl_exe` AND
# `djxl_exe`, and the capability report just looks for a `*_exe`. The file the
# first field points at is run_name()'s — the launcher where the install wrote
# one, so what callers execute is what is detected.
_DEPS_NATIVE_FIELDS = {
    "oxipng": ("oxipng_exe", ()),
    "slskd": ("slskd_exe", ()),
    "audioauditor": ("cli_exe", ()),
    "cuetools": ("arcue_exe", ()),
    # rsgain, chromaprint, libjxl and libjpeg-turbo became installable here when
    # upstream's Linux assets were added (fetchdeps.LINUX_BINARIES). Without an
    # entry they would be installed into the tools folder and still not COUNT:
    # detection would keep reporting the distro copy on PATH, so the row's amber
    # Update would never clear and the new download would be invisible to
    # mlo.loudness and mlo.acoustid (which read rsgain_exe / resolve fpcalc
    # itself) and to images.py, which opens BOTH of libjxl's executables.
    "rsgain": ("rsgain_exe", ()),
    "chromaprint": ("fpcalc_exe", ()),
    "libjxl": ("cjxl_exe", (("djxl_exe", "djxl"),)),
    "libjpeg_turbo": ("jpegtran_exe", ()),
}


def _detect_deps_native():
    """Tools installed as native binaries under a tools folder (POSIX only).

    fetchdeps installs native Linux builds there (see fetchdeps.LINUX_BINARIES:
    oxipng, slskd, AudioAuditor, CUETools through its mono launcher, upstream's
    rsgain, fpcalc, libjxl and libjpeg-turbo) and the .exe scan above cannot
    see them. A Windows host sharing this folder must never pick one up: an
    .exe-less folder is a file it cannot execute.
    """
    from .fetchdeps import INSTALL_PREFIX, LINUX_BINARIES, run_name

    tools = {}
    if os.name == "nt":
        return tools
    for key, spec in LINUX_BINARIES.items():
        fields = _DEPS_NATIVE_FIELDS.get(key)
        if not fields:
            continue
        version, folder, root = _detect_tool_dirs(INSTALL_PREFIX.get(key, key))
        if not folder:
            continue
        d = os.path.join(root, folder)
        if not all(os.path.isfile(os.path.join(d, m)) for m in spec["markers"]):
            continue
        exe = os.path.join(d, run_name(key))
        if not os.path.isfile(exe):
            continue
        entry = {"version": version, fields[0]: exe}
        for extra_field, name in fields[1]:
            extra = os.path.join(d, name)
            if not os.path.isfile(extra):
                entry = None
                break
            entry[extra_field] = extra
        if entry:
            tools[key] = entry
    return tools


def detect_all_tools():
    sig = deps_dir_signature()
    with _CACHE_LOCK:
        if _TOOLS_CACHE is not None and _TOOLS_CACHE_SIG == sig:
            return _TOOLS_CACHE

    # Off-Windows the tools are the app's own installs under its tools folders
    # (native binaries and pip packages - fetchdeps installs those there, see
    # _detect_deps_native) plus the distro/Homebrew ones on PATH. The .exe scan
    # below is Windows-only: a folder of Windows binaries must never be selected
    # as an install on a host that cannot run them.
    if os.name != "nt":
        return _store_tools_cache(_detect_system_tools(), sig)

    tools = {}

    if not any(os.path.isdir(root) for root in tools_dirs()):
        return _store_tools_cache(_detect_system_tools(), sig)

    fv, ff, deps_root = _detect_tool_dirs("flac")
    if ff:
        d = os.path.join(deps_root, ff)
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

    jv, jf, deps_root = _detect_tool_dirs("libjxl")
    if jf:
        d = os.path.join(deps_root, jf)
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

    lv, lf, deps_root = _detect_tool_dirs("libjpeg-turbo")
    if lf:
        d = os.path.join(deps_root, lf)
        if os.path.isfile(os.path.join(d, "jpegtran.exe")):
            tools["libjpeg_turbo"] = {
                "version": lv,
                "jpegtran_exe": os.path.join(d, "jpegtran.exe"),
            }

    ov, of, deps_root = _detect_tool_dirs("oxipng")
    if of:
        d = os.path.join(deps_root, of)
        if os.path.isfile(os.path.join(d, "oxipng.exe")):
            tools["oxipng"] = {
                "version": ov,
                "oxipng_exe": os.path.join(d, "oxipng.exe"),
            }

    av, af, deps_root = _detect_tool_dirs("audioauditor")
    if af:
        d = os.path.join(deps_root, af)
        if os.path.isfile(os.path.join(d, "AudioAuditorCLI.exe")):
            tools["audioauditor"] = {
                "version": av,
                "cli_exe": os.path.join(d, "AudioAuditorCLI.exe"),
            }

    rv, rf, deps_root = _detect_tool_dirs("rsgain")
    if rf:
        d = os.path.join(deps_root, rf)
        if os.path.isfile(os.path.join(d, "rsgain.exe")):
            tools["rsgain"] = {
                "version": rv,
                "rsgain_exe": os.path.join(d, "rsgain.exe"),
            }

    fv2, ff2, deps_root = _detect_tool_dirs("ffmpeg")
    if ff2:
        d = os.path.join(deps_root, ff2)
        if (os.path.isfile(os.path.join(d, "ffmpeg.exe"))
                and os.path.isfile(os.path.join(d, "ffprobe.exe"))):
            tools["ffmpeg"] = {
                "version": fv2,
                "ffmpeg_exe": os.path.join(d, "ffmpeg.exe"),
                "ffprobe_exe": os.path.join(d, "ffprobe.exe"),
            }

    pv, pf, deps_root = _detect_tool_dirs("php")
    if pf:
        d = os.path.join(deps_root, pf)
        if os.path.isfile(os.path.join(d, "php.exe")):
            tools["php"] = {
                "version": pv,
                "php_exe": os.path.join(d, "php.exe"),
            }

    yv, yf, deps_root = _detect_tool_dirs("yt-dlp")
    if yf:
        d = os.path.join(deps_root, yf)
        if os.path.isfile(os.path.join(d, "yt-dlp.exe")):
            tools["yt-dlp"] = {
                "version": yv,
                "ytdlp_exe": os.path.join(d, "yt-dlp.exe"),
            }

    # fpcalc: the AcoustID fingerprinter. The Windows installer has always
    # put it in the tools folder, but only the PATH scan could see it — so the
    # Dependencies table said "ready" while every AcoustID lookup reported the
    # tool missing. Detected here like the rest, so both agree.
    cv, cf, deps_root = _detect_tool_dirs("chromaprint")
    if cf:
        d = os.path.join(deps_root, cf)
        if os.path.isfile(os.path.join(d, "fpcalc.exe")):
            tools["chromaprint"] = {
                "version": cv,
                "fpcalc_exe": os.path.join(d, "fpcalc.exe"),
            }

    # slskd is a folder of its own in the tools folder (server/soulseek.py
    # looks for the same slskd.exe through fetchdeps.installed_path), and the
    # capability report reads the detected tools, so it is detected here too.
    sv, sf, deps_root = _detect_tool_dirs("slskd")
    if sf:
        d = os.path.join(deps_root, sf)
        if os.path.isfile(os.path.join(d, "slskd.exe")):
            tools["slskd"] = {
                "version": sv,
                "slskd_exe": os.path.join(d, "slskd.exe"),
            }

    lc_v, lc_f, deps_root = _detect_tool_dirs("logchecker")
    if lc_f:
        d = os.path.join(deps_root, lc_f)
        phar = os.path.join(d, "logchecker.phar")
        if os.path.isfile(phar):
            tools["logchecker"] = {
                "version": lc_v,
                "phar_path": phar,
                "php_exe": tools.get("php", {}).get("php_exe"),
            }

    ct_v, ct_f, deps_root = _detect_tool_dirs("cuetools")
    if ct_f:
        d = os.path.join(deps_root, ct_f)
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
    # category resolves even when no tool has been downloaded yet.
    system = _detect_system_tools()
    for key in ("flac", "libjxl", "libjpeg_turbo", "oxipng", "ffmpeg",
                "rsgain", "chromaprint", "slskd", "yt-dlp", "php", "logchecker"):
        if key not in tools and key in system:
            tools[key] = system[key]

    return _store_tools_cache(tools, sig)


def _pkg_dist_version(folder, pkg):
    """The version pip recorded in *folder*, or None.

    `pip install --target` writes `<name>-<version>.dist-info/` beside every
    package it installs, and a vendored folder holds MANY of them — librosa's
    folder also carries numpy's and scipy's — so the name decides which one is
    this tool's. pip's own metadata outranks the folder name because it is what
    actually runs when the folder is put on sys.path.
    """
    if not os.path.isdir(folder):
        return None
    # Distribution names are normalised ("yt-dlp" -> "yt_dlp") and lowercased.
    norm = re.sub(r"[-_.]+", "_", pkg).lower()
    rx = re.compile(rf"^{re.escape(norm)}-(.+)\.dist-info$")
    try:
        entries = os.listdir(folder)
    except OSError:
        return None
    for entry in entries:
        m = rx.match(entry.lower())
        if m and os.path.isdir(os.path.join(folder, entry)):
            return m.group(1)
    return None


def _pkg_folder_version(entry, pkg):
    """The version in a vendored folder's own name (`librosa v0.12.0`)."""
    m = re.match(rf"^{re.escape(pkg)}\s+v(.+)$", entry, re.IGNORECASE)
    return m.group(1) if m else None


def _version_sort_key(version):
    """Comparable tuple for a version label; a folder with no version sorts
    lowest (empty tuple), which is what the plain name it carries deserves."""
    if not version:
        return ()
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[._\-+]", version))


def _pip_pkg_dirs(pkg):
    """[(version, dir)] for every vendored install of *pkg* in a tools folder."""
    top = PIP_IMPORT_NAMES.get(pkg, pkg)
    out = []
    for root in tools_dirs():
        if not os.path.isdir(root):
            continue
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            full = os.path.join(root, entry)
            if not (os.path.isdir(full) and entry.lower().startswith(pkg.lower())):
                continue
            if not os.path.isfile(os.path.join(full, top, "__init__.py")):
                continue
            out.append((_pkg_dist_version(full, pkg) or _pkg_folder_version(entry, pkg),
                        full))
    return out


def python_pkg_path(pkg):
    """Vendored pip-package dir for *pkg* ('librosa', 'beets', 'yt-dlp').

    Layout is '<tools folder>/<pkg> vX.Y' (see fetchdeps.PIP_PACKAGES); the
    import name can differ from the pip name (yt-dlp -> yt_dlp), hence
    PIP_IMPORT_NAMES.

    The NEWEST version wins, across every tools folder an install may hold one
    in (mlo.paths.tools_dirs — the pre-move folder is still read). An update
    installs a new `vX.Y` folder beside the old one, and before this the first
    `sorted()` entry won — i.e. the oldest, so the app kept importing the
    version an update had just replaced and the Dependencies row kept reporting
    it.
    """
    dirs = _pip_pkg_dirs(pkg)
    if not dirs:
        return None
    return max(dirs, key=lambda item: _version_sort_key(item[0]))[1]


def python_pkg_version(pkg):
    """Installed version of the vendored pip package *pkg*, or None.

    Answered from pip's `.dist-info` metadata (falling back to the install
    folder's name) so nothing has to import the package — the Dependencies
    table asks this on every refresh, and beets/librosa are heavy imports.
    """
    dirs = _pip_pkg_dirs(pkg)
    if not dirs:
        return None
    version, _folder = max(dirs, key=lambda item: _version_sort_key(item[0]))
    return version or None

