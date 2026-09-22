#!/usr/bin/env python3
"""Dependency updates: an Update row must be installable, and must settle.

The Dependencies page and the setup wizard both reported success while changing
nothing, because of two separate comparisons that were "different" instead of
"behind", plus a folder whose name never followed its contents:

* `state` was `update` whenever upstream merely DIFFERED from what is installed,
  so a tool installed at 10.2.1 with the pin at 10.2.0 was marked "Update" for
  good - pointing at an older release. Installing it fetched that older release,
  reported ok, and left the chip exactly where it was: "the Install button does
  nothing".
* The installer reused the folder the previous version lived in and never
  renamed it, while the detector reads a tool's version off the FOLDER name
  (mlo.tools._detect_tool). A successful update therefore kept being reported as
  the old version, so the row could never leave `update`.
* The auto-update worker skipped any tool whose installed version equalled the
  pin, because that used to be an endless re-download of the same archive - a
  guard that, with updates now landing, would only hide the update it exists to
  perform.

Nothing here touches the network or the real .dependencies: the ordering and
naming rules are pure, and the rename is exercised against a temp folder.

Run: python tools/test_dep_updates.py
Exit 0 = pass, 1 = failure.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import fetchdeps  # noqa: E402

FAILURES = []


def check(label, cond):
    if cond:
        return
    FAILURES.append(label)
    print(f"FAIL {label}")


# --------------------------------------------------------------------------- #
# 1. "Behind" is an order, not a difference
# --------------------------------------------------------------------------- #
check("10.2.1 is newer than 10.2.0",
      fetchdeps.newer_version("10.2.1", "10.2.0") is True)
check("the pinned 10.2.0 is NOT newer than an installed 10.2.1",
      fetchdeps.newer_version("10.2.0", "10.2.1") is False)
check("a version is not newer than itself",
      fetchdeps.newer_version("10.2.0", "10.2.0") is False)
check("a GitHub tag compares with a plain version",
      fetchdeps.newer_version("v10.2.1", "10.2.0") is True)
check("10.2 and 10.2.0 are the same release",
      fetchdeps.newer_version("10.2", "10.2.0") is False)
check("a longer version is still ordered",
      fetchdeps.newer_version("10.2.0.1", "10.2.0") is True)
check("major beats minor",
      fetchdeps.newer_version("11.0", "10.9.9") is True)
# An unreadable label must not become an update the installer cannot perform:
# that is the phantom chip this file exists to prevent.
for candidate, current in (("rolling", "10.2.0"), ("10.2.0", None), (None, "10.2.0"),
                           ("latest", "latest")):
    check(f"unreadable {candidate!r} vs {current!r} is never an update",
          fetchdeps.newer_version(candidate, current) is False)

check("same_version accepts tag spellings",
      fetchdeps.same_version("v10.2.1", "10.2.1") is True)
check("same_version rejects a different release",
      fetchdeps.same_version("10.2.0", "10.2.1") is False)
check("same_version is not fooled by an unreadable value",
      fetchdeps.same_version(None, "10.2.0") is False)


# --------------------------------------------------------------------------- #
# 2. The folder carries the version that is in it
# --------------------------------------------------------------------------- #
# _rename_install works on the path it is handed, so a temp stand-in is enough.
with tempfile.TemporaryDirectory() as tmp:
    old = os.path.join(tmp, "oxipng v10.2.0")
    os.makedirs(old)
    with open(os.path.join(old, "oxipng.exe"), "w") as fh:
        fh.write("binary")

    new = fetchdeps._rename_install(old, "oxipng", "10.2.1", log=lambda m: None)
    check("an updated install folder is renamed to the new version",
          os.path.basename(new) == "oxipng v10.2.1")
    check("the renamed folder is the one on disk",
          os.path.isdir(new) and not os.path.exists(old))
    check("the contents travelled with it",
          os.path.isfile(os.path.join(new, "oxipng.exe")))
    check("only one folder is left behind", sorted(os.listdir(tmp)) == ["oxipng v10.2.1"])

    # Already correct: nothing to do, and the same path comes back.
    again = fetchdeps._rename_install(new, "oxipng", "10.2.1", log=lambda m: None)
    check("a folder already named for its version is left alone", again == new)

    # Rolling layouts are the shipped shape and carry no version to rename to.
    rolling = os.path.join(tmp, "ffmpeg vlatest")
    os.makedirs(rolling)
    kept = fetchdeps._rename_install(rolling, "ffmpeg", "2026.8.19", log=lambda m: None)
    check("a vlatest rolling folder keeps its name", kept == rolling)

    # A target that already exists must not be clobbered.
    os.makedirs(os.path.join(tmp, "rsgain v3.8"), exist_ok=True)
    src = os.path.join(tmp, "rsgain v3.7")
    os.makedirs(src)
    settled = fetchdeps._rename_install(src, "rsgain", "3.8", log=lambda m: None)
    check("an existing same-version folder is never overwritten", settled == src)


# --------------------------------------------------------------------------- #
# 3. The row the user sees: "Update" only when it is genuinely behind
# --------------------------------------------------------------------------- #
# The row is what the chip and the Install button read, so pin the promise
# there, with the four lookups it makes stubbed out (no network, no tools).
def row_for(installed, pin, upstream):
    real = (fetchdeps.detect_all_tools, fetchdeps.installed_versions,
            fetchdeps.latest_versions, fetchdeps.upstream_versions)
    fetchdeps.detect_all_tools = lambda: {"oxipng": {"version": installed, "oxipng_exe": "x"}}
    fetchdeps.installed_versions = lambda: {"oxipng": installed} if installed else {}
    fetchdeps.latest_versions = lambda: {"oxipng": pin}
    fetchdeps.upstream_versions = lambda refresh=False, block=False: (
        {"oxipng": {"version": upstream, "checked_at": 0.0, "error": None}} if upstream else {})
    try:
        for row in fetchdeps.dependency_rows():
            if row["key"] == "oxipng":
                return row
        return {}
    finally:
        (fetchdeps.detect_all_tools, fetchdeps.installed_versions,
         fetchdeps.latest_versions, fetchdeps.upstream_versions) = real


r = row_for(installed="10.2.1", pin="10.2.0", upstream="10.2.1")
check("installed at the newest release with an older pin reads ok, not update",
      r.get("state") == "ok")
check("...and offers no update", r.get("update_available") is False)

r = row_for(installed="10.2.0", pin="10.2.0", upstream="10.2.1")
check("installed behind upstream reads update", r.get("state") == "update")
check("...and is the update the page counts", r.get("update_available") is True)

# Installed AHEAD of upstream (a newer release pulled in by hand, or upstream
# yanking one): still not an update, and still not a chip that installs an
# older release over a newer one.
r = row_for(installed="10.2.2", pin="10.2.0", upstream="10.2.1")
check("installed ahead of upstream is not an update", r.get("state") == "ok")
check("...and does not offer one", r.get("update_available") is False)

# Upstream unknown (the background check has not answered yet): the pin is the
# only reference, and it is behind, so the row is an update.
r = row_for(installed="3.7", pin="3.8", upstream=None)
check("with no upstream answer the pin still decides update", r.get("state") == "update")

# ...but a pin that is OLDER than what is installed is not an update.
r = row_for(installed="3.8", pin="3.7", upstream=None)
check("an older pin is not an update against what is installed", r.get("state") == "ok")


# --------------------------------------------------------------------------- #
# 4. The auto-update worker no longer skips the updates it exists to perform
# --------------------------------------------------------------------------- #
import inspect  # noqa: E402

src = inspect.getsource(fetchdeps.auto_update_pass)
check("the pin-equality skip is gone from the auto-update pass",
      "latest_version\"])):" not in src and "continue" in src)
check("the pass still only touches missing/update rows",
      'row["state"] not in ("missing", "update")' in src)

# --------------------------------------------------------------------------- #
# 5. An install that would change nothing does not download
# --------------------------------------------------------------------------- #
# "Install / update all" re-fetched slskd's 118 MB on every press even when the
# folder already carried the newest release's version. An install exists to
# replace a copy that is BEHIND, so an already-current one is a no-op — and a
# no-op must not touch the network at all.
real = (fetchdeps._existing_install, fetchdeps.installed_versions,
        fetchdeps.get_latest_release, fetchdeps._download)
fetchdeps._existing_install = lambda prefix, markers: f"{prefix} v10.2.0"
fetchdeps.installed_versions = lambda: {"oxipng": "10.2.0"}
fetchdeps.get_latest_release = (
    lambda key, upstream=False: {"version": "10.2.0", "assets": [], "urls": {}})


def _refuse(*a, **k):
    raise AssertionError("an already-current install fetched the release again")


fetchdeps._download = _refuse
try:
    got = fetchdeps.install_dependency("oxipng", log=lambda m: None)
    check(f"an already-current install is a no-op (got {got!r})", got == "10.2.0")
except AssertionError as e:
    check(f"an already-current install is a no-op ({e})", False)
finally:
    (fetchdeps._existing_install, fetchdeps.installed_versions,
     fetchdeps.get_latest_release, fetchdeps._download) = real

# --------------------------------------------------------------------------- #
# 6. A pip tool behind its upstream release actually updates
# --------------------------------------------------------------------------- #
# librosa/beets/yt-dlp could never move: _install_pip_package returned "already
# installed" whenever a folder existed at all, installed the hardcoded pin when
# one did not, and installed_versions() reported the pin for any folder — so the
# row said Update, the press answered "nothing to do", and a successful install
# could not even be seen. This walks the whole loop with pip itself stubbed (no
# package is fetched from PyPI) inside a temp .dependencies.
import contextlib  # noqa: E402
import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import mlo.tools as tools_mod  # noqa: E402


def sandbox_deps(deps_dir):
    """Point every install/detection path at a temp tools folder.

    The tools live under the music folder now (mlo.paths.tools_dir), so what
    has to be redirected is the RESOLVER and not a constant: fetchdeps writes
    through tools_dir(), and detection reads tools_dirs() — both are imported
    into the modules below, so they are what gets stubbed.
    """
    real = (fetchdeps.tools_dir, fetchdeps.tools_dirs, tools_mod.tools_dirs)
    fetchdeps.tools_dir = lambda music_folder=None: deps_dir
    fetchdeps.tools_dirs = tools_mod.tools_dirs = (
        lambda music_folder=None: [deps_dir])
    tools_mod._TOOLS_CACHE = None
    return real


def restore_deps(real):
    (fetchdeps.tools_dir, fetchdeps.tools_dirs, tools_mod.tools_dirs) = real
    tools_mod._TOOLS_CACHE = None


@contextlib.contextmanager
def upstream_cache(entries=None):
    """A private upstream cache for one block: the process-wide one holds
    whatever a live background pass has already fetched."""
    real = dict(fetchdeps._upstream_cache)
    fetchdeps._upstream_cache.clear()
    fetchdeps._upstream_cache.update(entries or {})
    try:
        yield
    finally:
        fetchdeps._upstream_cache.clear()
        fetchdeps._upstream_cache.update(real)


def rows_with(installed, upstream):
    """The Dependencies rows for a stubbed world: {key: version} installed,
    {key: version} upstream — never the tools this host happens to have."""
    real = (fetchdeps.detect_all_tools, fetchdeps.installed_versions,
            fetchdeps.upstream_versions)
    fetchdeps.detect_all_tools = lambda: {
        key: {"version": version, "oxipng_exe": "x"} for key, version in installed.items()}
    fetchdeps.installed_versions = lambda: dict(installed)
    fetchdeps.upstream_versions = lambda refresh=False, block=False: {
        key: {"version": version, "checked_at": 0.0, "error": None}
        for key, version in upstream.items()}
    try:
        return {row["key"]: row for row in fetchdeps.dependency_rows()}
    finally:
        (fetchdeps.detect_all_tools, fetchdeps.installed_versions,
         fetchdeps.upstream_versions) = real


def vendor_pip_pkg(deps_dir, key, version):
    """A folder shaped the way `pip install --target` leaves one, dist-info
    included: that metadata is what detection reads the version from."""
    top = tools_mod.PIP_IMPORT_NAMES.get(key, key)
    root = os.path.join(deps_dir, f"{key} v{version}")
    os.makedirs(os.path.join(root, top), exist_ok=True)
    open(os.path.join(root, top, "__init__.py"), "w").close()
    dist = os.path.join(root, f"{key.replace('-', '_')}-{version}.dist-info")
    os.makedirs(dist, exist_ok=True)
    open(os.path.join(dist, "METADATA"), "w").close()
    return root


with tempfile.TemporaryDirectory() as tmp:
    vendor_pip_pkg(tmp, "librosa", "0.11.0")
    real = (sandbox_deps(tmp), fetchdeps._api_json, fetchdeps.run_tool)
    seen = {}

    def _api(url, headers=None):
        if url == "https://pypi.org/pypi/librosa/json":
            return {"info": {"version": "0.12.0"}}
        raise AssertionError(f"unexpected URL {url}")

    def _pip(cmd, **kw):
        seen["cmd"] = list(cmd)
        # pip itself is replaced, but it writes the package for real: the
        # assertions below read a folder rather than a stub's answer.
        vendor_pip_pkg(tmp, "librosa", seen["cmd"][-1].split("==")[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    fetchdeps._api_json = _api
    fetchdeps.run_tool = _pip
    try:
        with upstream_cache({}):
            def librosa_row():
                """The librosa row as the page paints it, with the upstream
                answer the probe would have given. Everything else is real:
                detection, the installed version, the state logic."""
                real_up = fetchdeps.upstream_versions
                fetchdeps.upstream_versions = lambda refresh=False, block=False: (
                    {"librosa": {"version": "0.12.0", "checked_at": 0.0, "error": None}})
                try:
                    return next(r for r in fetchdeps.dependency_rows()
                                if r["key"] == "librosa")
                finally:
                    fetchdeps.upstream_versions = real_up

            before = fetchdeps.installed_versions().get("librosa")
            check(f"a vendored pip package reports its real version (got {before!r})",
                  before == "0.11.0")

            row_before = librosa_row()
            got = fetchdeps.install_dependency("librosa", log=lambda m: None)
            row_after = librosa_row()
            after = fetchdeps.installed_versions().get("librosa")
            print(f"  librosa row before: state={row_before['state']} "
                  f"installed={row_before['installed_version']} "
                  f"available={row_before['upstream_version']}")
            print(f"  librosa row after:  state={row_after['state']} "
                  f"installed={row_after['installed_version']} "
                  f"available={row_after['upstream_version']}")

        check(f"a pip tool behind upstream installs the upstream version (got {got!r})",
              got == "0.12.0")
        check(f"...asking pip for that exact version, not the pin ({seen.get('cmd')})",
              seen.get("cmd", [""])[-1] == "librosa==0.12.0")
        check("...into a folder stamped with it",
              os.path.isfile(os.path.join(tmp, "librosa v0.12.0", "librosa", "__init__.py")))
        check("...pruning the version it replaced",
              not os.path.isdir(os.path.join(tmp, "librosa v0.11.0")))
        check(f"...and reporting what is now installed (got {after!r})", after == "0.12.0")
        check("the row read Update before the press",
              row_before["state"] == "update" and row_before["update_available"] is True)
        check("...and Ready after it, at the new version",
              row_after["state"] == "ok" and row_after["installed_version"] == "0.12.0")
    finally:
        fetchdeps._api_json, fetchdeps.run_tool = real[1], real[2]
        restore_deps(real[0])


# --------------------------------------------------------------------------- #
# 7. A PATH copy at the pin updates; a copy newer than upstream is untouched
# --------------------------------------------------------------------------- #
# "Already installed" is what the DETECTOR says, PATH included. Deciding it
# from a .dependencies folder alone meant a copy on PATH at the pin made the
# press "reinstall" the pin (changed: false, chip survives), and a copy NEWER
# than the pin was walked DOWN into .dependencies. oxipng is the key here
# because this host can install it natively on every platform the suite runs on.
if fetchdeps.installable("oxipng"):
    patterns = fetchdeps._asset_patterns("oxipng")
    asset = ("oxipng-10.2.1-x86_64-pc-windows-msvc.zip"
             if any(p.endswith(r"\.zip$") for p in patterns)
             else "oxipng-10.2.1-x86_64-unknown-linux-musl.tar.gz")

    with tempfile.TemporaryDirectory() as tmp:
        real = (sandbox_deps(tmp), fetchdeps._existing_install,
                fetchdeps.installed_versions, fetchdeps.get_latest_release,
                fetchdeps._download, fetchdeps._extract_archive,
                fetchdeps._locate_binaries)
        seen = {}

        def _download(url, dest, progress=None):
            seen.setdefault("urls", []).append(url)
            with open(dest, "wb") as fh:
                fh.write(b"x" * 8192)

        def _extract(archive, dest_dir, log):
            payload = os.path.join(dest_dir, "payload")
            os.makedirs(payload, exist_ok=True)
            # The installer checks the installed folder against THIS host's own
            # binary names (`oxipng.exe` on Windows, `oxipng` on Linux — see
            # fetchdeps.LINUX_BINARIES), so the stub lands both: the suite runs
            # on either platform, and a Windows-only name made it fail on the
            # Linux runners with "Installed folder is missing: oxipng".
            for name in ("oxipng.exe", "oxipng"):
                open(os.path.join(payload, name), "wb").close()

        def _refuse(*a, **k):
            raise AssertionError("a copy that is not behind was re-downloaded")

        fetchdeps._existing_install = lambda prefix, markers: None
        fetchdeps.installed_versions = lambda: {"oxipng": "10.2.0"}
        fetchdeps.get_latest_release = lambda key, upstream=False: {
            "version": "10.2.1" if upstream else "10.2.0",
            "assets": [asset], "urls": {asset: "https://example.invalid/" + asset}}
        fetchdeps._download, fetchdeps._extract_archive = _download, _extract
        fetchdeps._locate_binaries = lambda root, key: os.path.join(root, "payload")
        try:
            got = fetchdeps.install_dependency("oxipng", log=lambda m: None)
            check(f"a PATH copy at the pin installs the newest release (got {got!r})",
                  got == "10.2.1")
            check(f"...from the newest release, not the pin ({seen.get('urls')})",
                  seen.get("urls") == ["https://example.invalid/" + asset])
            check("...into a folder stamped with the new version",
                  any(os.path.isfile(os.path.join(tmp, "oxipng v10.2.1", n))
                      for n in ("oxipng.exe", "oxipng")))

            # A copy NEWER than the newest release (a hand-pulled build, or a
            # release upstream yanked): an update must leave it alone.
            seen.clear()
            fetchdeps.installed_versions = lambda: {"oxipng": "10.2.2"}
            fetchdeps._download = _refuse
            try:
                got = fetchdeps.install_dependency("oxipng", log=lambda m: None)
                check(f"a copy newer than the newest release is left alone (got {got!r})",
                      got == "10.2.2")
                check("...and nothing was downloaded for it", not seen.get("urls"))
            except AssertionError as e:
                check(f"a copy newer than the newest release is left alone ({e})", False)
        finally:
            (fetchdeps._existing_install, fetchdeps.installed_versions,
             fetchdeps.get_latest_release, fetchdeps._download,
             fetchdeps._extract_archive, fetchdeps._locate_binaries) = real[1:]
            restore_deps(real[0])
else:
    print("  (no oxipng build for this host — skipping the PATH-copy checks)")


# --------------------------------------------------------------------------- #
# 8. beets, librosa and php report an AVAILABLE version at all
# --------------------------------------------------------------------------- #
# Three of the fifteen rows had no probe: their AVAILABLE column was empty and
# no update could ever be offered for them. None of the three publishes GitHub
# releases, so the probe reads PyPI's JSON (beets, librosa) and windows.php.net's
# release index (php). The HTTP client is stubbed with the shape each API really
# returns (checked against the live endpoints).
PHP_INDEX = {
    "8.1": {"version": "8.1.34",
            "nts-vs16-x64": {"zip": {"path": "php-8.1.34-nts-Win32-vs16-x64.zip"}}},
    "8.3": {"version": "8.3.33",
            "nts-vs16-x64": {"zip": {"path": "php-8.3.33-nts-Win32-vs16-x64.zip"}}},
    # 8.4 and 8.5 publish vs17 builds only — a php that needs the VC++ 2022
    # redistributable — so they are not an update this app can install.
    "8.4": {"version": "8.4.25",
            "nts-vs17-x64": {"zip": {"path": "php-8.4.25-nts-Win32-vs17-x64.zip"}}},
}
PYPI_NEWEST = {"librosa": "1.0.0", "beets": "2.14.1"}

real = (fetchdeps._api_json, dict(fetchdeps._php_build_cache))
fetchdeps._php_build_cache.update(at=0.0, version=None, url=None)


def _api(url, headers=None):
    if url.startswith("https://pypi.org/pypi/"):
        return {"info": {"version": PYPI_NEWEST[url.split("/")[4]]}}
    if url == fetchdeps.PHP_RELEASES_URL:
        return PHP_INDEX
    raise AssertionError(f"unexpected URL {url}")


fetchdeps._api_json = _api
try:
    for key in ("librosa", "beets"):
        got = fetchdeps._probe_version(key)
        check(f"{key}'s probe reads its newest release from PyPI (got {got!r})",
              got == PYPI_NEWEST[key])
    got = fetchdeps._probe_version("php")
    check(f"php's probe reads windows.php.net and skips the vs17-only series "
          f"(got {got!r})", got == "8.3.33")
    check(f"php's install URL is upstream's own file name "
          f"({fetchdeps.php_zip_url('8.3.33')})",
          fetchdeps.php_zip_url("8.3.33")
          == "https://windows.php.net/downloads/releases/php-8.3.33-nts-Win32-vs16-x64.zip")
    check("...while the pinned build keeps its archived URL",
          fetchdeps.php_zip_url(fetchdeps.PINNED["php"]["version"]) == fetchdeps.PHP_ZIP_URL)

    rows = rows_with({"librosa": "0.11.0", "beets": "2.4.0", "php": "8.1.28"},
                     {"librosa": "1.0.0", "beets": "2.14.1", "php": "8.3.33"})
    for key in ("librosa", "beets", "php"):
        row = rows[key]
        check(f"{key} has a real AVAILABLE column ({row['upstream_version']!r})",
              bool(row["upstream_version"]))
        check(f"{key} behind upstream counts as an update",
              row["update_available"] is True)
        # Behind is behind, however this host installs it: php is a download on
        # Windows, a distro package on Linux and neither on a platform with no
        # build at all — the row says `update` in all three cases, and the
        # ACTION column is what differs (see section 10).
        check(f"{key} reads Update ({row['state']} / {row['install_kind']})",
              row["state"] == "update"
              and row["action"] == {"deps": "update", "system": "upgrade"}.get(
                  row["install_kind"], "none"))
finally:
    fetchdeps._api_json = real[0]
    fetchdeps._php_build_cache.update(real[1])


# --------------------------------------------------------------------------- #
# 9. Two installs of one tool cannot collide
# --------------------------------------------------------------------------- #
# Every row installs on its own now, so a second press of one row — or the
# auto-update pass landing on a tool a user is installing — must not unpack a
# second download into the folder the first is writing. It is refused, in words
# the caller can show.
with tempfile.TemporaryDirectory() as tmp:
    real = (sandbox_deps(tmp), fetchdeps._api_json, fetchdeps.run_tool)
    busy = threading.Event()      # set from INSIDE pip: the lock is held
    done = threading.Event()
    outcome = {}

    def _api(url, headers=None):
        return {"info": {"version": "0.12.0"}}

    def _slow_pip(cmd, **kw):
        busy.set()
        done.wait(30)
        vendor_pip_pkg(tmp, "librosa", list(cmd)[-1].split("==")[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    fetchdeps._api_json = _api
    fetchdeps.run_tool = _slow_pip

    def _first_install():
        try:
            outcome["version"] = fetchdeps.install_dependency("librosa", log=lambda m: None)
        except Exception as e:      # noqa: BLE001 - reported, never raised here
            outcome["error"] = e

    try:
        with upstream_cache({}):
            first = threading.Thread(target=_first_install, daemon=True)
            first.start()
            busy.wait(30)
            check("the first install holds its tool's lock", fetchdeps.installing("librosa") is True)
            try:
                fetchdeps.install_dependency("librosa", log=lambda m: None)
                check("a second install of the same tool is refused", False)
            except RuntimeError as e:
                check(f"a second install of the same tool is refused ({e})",
                      "already being installed" in str(e))
            done.set()
            first.join(60)
            check(f"the first install finishes normally (got {outcome.get('version')!r}, "
                  f"{outcome.get('error')})", outcome.get("version") == "0.12.0")
            check("...and the tool is free again afterwards",
                  fetchdeps.installing("librosa") is False)
    finally:
        done.set()
        fetchdeps._api_json, fetchdeps.run_tool = real[1], real[2]
        restore_deps(real[0])


# --------------------------------------------------------------------------- #
# 10. "Behind upstream" and "what this host can do about it" are two facts
# --------------------------------------------------------------------------- #
# The regression this pins: state was forced back to `ok` for every row the
# installer could not fetch, so a distro tool behind upstream read a green
# Ready beside an amber Available and had no button to close either — and the
# header counted none of them, because its filter was the same `installable`
# one the button uses. The row now says it is behind (state) and separately
# says what is possible here (install_kind) and what to press (action).

# Upstream's Linux assets, resolved from the tables: rsgain for x86-64 only,
# fpcalc for both architectures the app supports — and, since the move that
# closed this gap, libjxl's static `.tar.lz` and libjpeg-turbo's `.deb` (one per
# architecture). Every asset name is one upstream really publishes (checked
# against the releases API), so a pattern that stops matching a real release
# fails here.
LINUX_ASSETS = (
    ("rsgain", "x86_64", "rsgain-3.8-Linux.tar.xz"),
    ("chromaprint", "x86_64", "chromaprint-fpcalc-1.6.1-linux-x86_64.tar.gz"),
    ("chromaprint", "aarch64", "chromaprint-fpcalc-1.6.1-linux-arm64.tar.gz"),
    ("libjxl", "x86_64", "jxl-linux-x86_64-static.tar.lz"),
    ("libjpeg_turbo", "x86_64", "libjpeg-turbo-official_3.2.0_amd64.deb"),
    ("libjpeg_turbo", "aarch64", "libjpeg-turbo-official_3.2.0_arm64.deb"),
)
import re  # noqa: E402

for key, machine, asset in LINUX_ASSETS:
    pattern = fetchdeps._linux_pattern(key, machine)
    check(f"Linux/{machine} installs {key} from its own release",
          fetchdeps.install_kind(key, platform="linux", machine=machine) == "deps"
          and bool(pattern))
    check(f"...and its asset pattern matches upstream's own file name ({asset})",
          bool(pattern) and re.fullmatch(pattern, asset, re.IGNORECASE) is not None)
check("rsgain's x86-64-only build falls back to the distro package on ARM",
      fetchdeps.install_kind("rsgain", platform="linux", machine="aarch64") == "system")
# libjxl is the same shape as rsgain: one static x86-64 build, and NO arm64
# asset of any kind upstream (checked against the release), so the ARM row must
# keep naming the distro package rather than a pattern that cannot match.
check("libjxl has no ARM build and falls back to the distro package there",
      fetchdeps.install_kind("libjxl", platform="linux", machine="aarch64") == "system"
      and fetchdeps._linux_pattern("libjxl", "aarch64") is None)
check("a tool with no Linux build and no package has neither",
      fetchdeps.install_kind("audioauditor", platform="linux",
                             machine="armv7l") == "unsupported")

# The command a distro row's Copy button hands over: apt's, from the package
# name in LINUX_PACKAGES, and none at all where the package manager is not the
# answer (another platform, or a tool with no package).
check("the upgrade command names the distro package",
      fetchdeps.system_upgrade_command("libjpeg_turbo", platform="linux")
      == "apt-get install --only-upgrade libjpeg-progs")
check("no package means no command",
      fetchdeps.system_upgrade_command("oxipng", platform="linux") is None)
check("off Linux there is no distro command to copy",
      fetchdeps.system_upgrade_command("rsgain", platform="windows") is None)


@contextlib.contextmanager
def linux_host():
    """dependency_rows() reads the platform off the HOST (install_kind is called
    with no arguments), so the distro-row case has to be simulated here: os.name
    and sys.platform are what host_platform() reads."""
    real = (os.name, sys.platform)
    os.name, sys.platform = "posix", "linux"
    try:
        yield
    finally:
        os.name, sys.platform = real


# A distro row on Linux is the case from the screenshot: flac has no fetchable
# Linux build at all (LINUX_PACKAGES only), so 1.5.0 is published while 1.4.3
# is installed. libjpeg-turbo USED to be this file's example and is not any
# more: upstream's .deb is unpacked by the installer now, so a row behind is an
# app-managed download like oxipng's — asserted below.
with linux_host():
    rows = rows_with({"flac": "1.4.3"}, {"flac": "1.5.0"})
    row = rows["flac"]
    check("a distro row behind upstream reads update (not ok)",
          row["state"] == "update")
    check("...counts as an available update",
          row["update_available"] is True and row["upstream_version"] == "1.5.0")
    check("...reports that the distro owns it",
          row["install_kind"] == "system" and row["installable"] is False)
    check("...and offers the command that upgrades it",
          row["action"] == "upgrade"
          and row["upgrade_command"] == "apt-get install --only-upgrade flac")
    check("...and carries the versions and the command in its note",
          row["note"] and "1.4.3" in row["note"] and "1.5.0" in row["note"]
          and row["upgrade_command"] in row["note"])

    rows = rows_with({"flac": "1.5.0"}, {"flac": "1.5.0"})
    row = rows["flac"]
    check("a distro row at the upstream version reads ok",
          row["state"] == "ok" and row["update_available"] is False)
    check("...and has nothing to offer in the action column",
          row["action"] == "none" and row["upgrade_command"] is None)

    # The two tools this change is about. On x86-64 Linux both are a download of
    # their own now, so a row behind upstream offers THIS row's install — never
    # the package manager's command, which is what it used to offer instead.
    rows = rows_with({"libjpeg_turbo": "2.1.5", "libjxl": "0.11.1"},
                     {"libjpeg_turbo": "3.2.0", "libjxl": "0.12.0"})
    jt, jx = rows["libjpeg_turbo"], rows["libjxl"]
    check("a libjpeg-turbo behind upstream is installable here",
          jt["state"] == "update" and jt["installable"] is True
          and jt["install_kind"] == "deps" and jt["action"] == "update"
          and jt["upgrade_command"] is None)
    check("...and so is a libjxl behind upstream",
          jx["state"] == "update" and jx["installable"] is True
          and jx["install_kind"] == "deps" and jx["action"] == "update"
          and jx["upgrade_command"] is None)

    # A row a DOWNLOAD can move must not borrow the "nothing this host can
    # install" note: that sentence sits next to a button that DOES install it,
    # and the two would contradict each other.
    rows = rows_with({"oxipng": "10.2.0"}, {"oxipng": "10.2.1"})
    row = rows["oxipng"]
    check("a downloadable update carries no 'nothing can be installed' note",
          row["state"] == "update" and row["action"] == "update"
          and not row["note"])


# --------------------------------------------------------------------------- #
# 11. A native Linux install is SEEN, not just downloaded
# --------------------------------------------------------------------------- #
# Adding upstream's Linux assets made these tools installable on Linux — but
# tools.py reports what sits in a tools folder through its own field map, and a
# key missing from that map is installed and then invisible: detection would keep
# describing the distro copy on PATH, so the amber Update would never clear and
# the press would read as "nothing happened".
with tempfile.TemporaryDirectory() as tmp:
    for folder, exe in (("rsgain v3.8", "rsgain"), ("chromaprint v1.6.1", "fpcalc"),
                        ("libjxl v0.12.0", "cjxl"), ("libjxl v0.12.0", "djxl"),
                        ("libjpeg-turbo v3.2.0", "jpegtran")):
        root = os.path.join(tmp, folder)
        os.makedirs(root, exist_ok=True)
        open(os.path.join(root, exe), "w").close()
    real = (sandbox_deps(tmp), fetchdeps.installed_versions)
    try:
        with linux_host():
            found = tools_mod.detect_all_tools()
            check(f"a native rsgain install is detected ({found.get('rsgain')})",
                  (found.get("rsgain") or {}).get("version") == "3.8"
                  and (found["rsgain"].get("rsgain_exe") or "").endswith("rsgain"))
            check(f"a native fpcalc install is detected ({found.get('chromaprint')})",
                  (found.get("chromaprint") or {}).get("version") == "1.6.1"
                  and (found["chromaprint"].get("fpcalc_exe") or "").endswith("fpcalc"))
            # images.py opens BOTH of libjxl's executables, so a native install
            # that only reported cjxl would raise a KeyError on the decoder.
            check(f"a native libjxl install reports both its tools "
                  f"({found.get('libjxl')})",
                  (found.get("libjxl") or {}).get("version") == "0.12.0"
                  and (found["libjxl"].get("cjxl_exe") or "").endswith("cjxl")
                  and (found["libjxl"].get("djxl_exe") or "").endswith("djxl"))
            check(f"a native jpegtran install is detected ({found.get('libjpeg_turbo')})",
                  (found.get("libjpeg_turbo") or {}).get("version") == "3.2.0"
                  and (found["libjpeg_turbo"].get("jpegtran_exe") or "").endswith(
                      "jpegtran"))
            check("...and the rows report them as installed, not behind",
                  fetchdeps.installed_versions().get("rsgain") == "3.8"
                  and fetchdeps.installed_versions().get("chromaprint") == "1.6.1"
                  and fetchdeps.installed_versions().get("libjxl") == "0.12.0"
                  and fetchdeps.installed_versions().get("libjpeg_turbo") == "3.2.0")
    finally:
        fetchdeps.installed_versions = real[1]
        restore_deps(real[0])


# --------------------------------------------------------------------------- #
# 12. libjxl's .tar.lz and libjpeg-turbo's .deb install from the REAL layouts
# --------------------------------------------------------------------------- #
# Both assets upstream publishes for Linux have an archive shape nothing here
# could open when this file was written (lzip, and an `ar` package a dpkg-less
# host cannot unpack), so this section builds both shapes as fixtures — the same
# internal layout the real ones have, measured by unpacking the v0.12.0
# jxl-linux-x86_64-static.tar.lz and the 3.2.0 libjpeg-turbo-official_amd64.deb
# — and runs the REAL installer over them. No network: the download step is
# stubbed to copy the fixture. (Against upstream: both real files were unpacked
# with these functions during development, and their layouts are what the
# fixtures copy.)
import binascii  # noqa: E402
import gzip  # noqa: E402
import io  # noqa: E402
import lzma  # noqa: E402
import shutil  # noqa: E402
import struct  # noqa: E402
import tarfile  # noqa: E402


def lzip_member(data):
    """One lzip member: 6-byte header, raw LZMA1 stream, 20-byte trailer.

    lzip is exactly what makes libjxl's asset unreadable to `lzma.open` (it
    knows .xz and the LZMA-alone header, not this container), so the fixture has
    to be genuine: the magic, version 1 and the dictionary-size code (0x19 is
    32 MiB, the one lzip writes), the raw LZMA1 stream with lzip's fixed
    lc/lp/pb, then crc32/uncompressed size/member size.
    """
    stream = lzma.compress(
        data, format=lzma.FORMAT_RAW,
        filters=[{"id": lzma.FILTER_LZMA1, "dict_size": 1 << 25,
                  "lc": 3, "lp": 0, "pb": 2}])
    member = b"LZIP\x01\x19" + stream
    return member + struct.pack("<IQQ", binascii.crc32(data) & 0xFFFFFFFF,
                                len(data), len(member) + 20)


def lzip_bytes(payload, *, trailer_member=False):
    """*payload* as a real lzip file, optionally with the second member the
    upstream asset carries (tar's two empty end blocks, 1024 bytes) — which is
    what proves the multi-member walk in _lzip_plain_bytes."""
    out = lzip_member(payload)
    if trailer_member:
        out += lzip_member(b"\0" * 1024)
    return out


def ar_archive(members):
    """A Unix `ar` archive: the wrapper a .deb is, built the way dpkg builds it.

    `members` is [(name, bytes)]; names stay under the 16-byte field (dpkg's
    own do), and an odd-sized member is padded with the newline the format
    requires — the byte an offset arithmetic mistake eats.
    """
    out = b"!<arch>\n"
    for name, data in members:
        out += (f"{name:<16}{0:<12}{0:<6}{0:<6}{0o100644:<8o}{len(data):<10}`\n"
                .encode("ascii"))
        out += data + (b"\n" if len(data) % 2 else b"")
    return out


def deb_bytes(data_tar):
    """A .deb holding *data_tar*: debian-binary + control.tar.gz + data.tar.xz.

    The same three members dpkg writes, in the same order, so the reader has to
    skip the two it does not need and take the third.
    """
    return ar_archive([("debian-binary", b"2.0\n"),
                       ("control.tar.gz", gzip.compress(b"")),
                       ("data.tar.xz", lzma.compress(data_tar))])


def tar_bytes(paths):
    """A `.tar` of {relative path: bytes} entries, made in memory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as fh:
        for rel, data in paths.items():
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o755
            fh.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# The shapes, as measured upstream: libjxl's tarball holds tools/cjxl +
# tools/djxl in an UNPREFIXED tree (plus the licence texts at its root), and
# libjpeg-turbo's data.tar holds its /opt prefix with bin/ beside the lib64/
# that the libjpeg.so.62 the binaries link against lives in.
JXL_TAR = tar_bytes({"tools/cjxl": b"\x7fELF cjxl", "tools/djxl": b"\x7fELF djxl",
                     "LICENSE.libjpeg": b"licence\n"})
LJT_TAR = tar_bytes({
    "opt/libjpeg-turbo/bin/jpegtran": b"\x7fELF jpegtran",
    "opt/libjpeg-turbo/lib64/libjpeg.so.62.4.0": b"\x7fELF libjpeg",
    "usr/share/doc/libjpeg-turbo-official/LICENSE.md": b"licence\n",
})
LINUX_ARCHIVE_TOOLS = (
    ("libjxl", "jxl-linux-x86_64-static.tar.lz", "0.12.0"),
    ("libjpeg_turbo", "libjpeg-turbo-official_3.2.0_amd64.deb", "3.2.0"),
)

with tempfile.TemporaryDirectory() as tmp:
    jxl_lz = os.path.join(tmp, "jxl-linux-x86_64-static.tar.lz")
    with open(jxl_lz, "wb") as fh:
        fh.write(lzip_bytes(JXL_TAR, trailer_member=True))
    ljt_deb = os.path.join(tmp, "libjpeg-turbo-official_3.2.0_amd64.deb")
    with open(ljt_deb, "wb") as fh:
        fh.write(deb_bytes(LJT_TAR))

    check("an lzip asset keeps its whole extension on disk",
          fetchdeps._archive_suffix(os.path.basename(jxl_lz)) == ".tar.lz")
    check("a .deb keeps its own",
          fetchdeps._archive_suffix(os.path.basename(ljt_deb)) == ".deb")

    # The extraction step on its own: what lands, and where the marker lookup
    # finds it. This is the part that used to fall through to "run it as an
    # installer" and die with rc=126.
    for archive, key, wanted in ((jxl_lz, "libjxl", "cjxl"),
                                 (ljt_deb, "libjpeg_turbo", "jpegtran")):
        out = os.path.join(tmp, "extract_" + key)
        os.makedirs(out)
        found = None
        with linux_host():
            try:
                fetchdeps._extract_archive(archive, out, log=lambda m: None)
                found = fetchdeps._locate_binaries(out, key)
            except Exception as e:      # noqa: BLE001 - reported, not raised
                check(f"{os.path.basename(archive)} extracts ({e})", False)
        check(f"{os.path.basename(archive)} extracts, and the marker lookup "
              f"finds {wanted}",
              bool(found) and os.path.isfile(os.path.join(found, wanted)))

    tools_root = os.path.join(tmp, "tools")
    os.makedirs(tools_root)
    real = (sandbox_deps(tools_root), fetchdeps._download,
            fetchdeps.get_latest_release, fetchdeps.installed_versions)
    downloads = []

    def _download(url, dest, progress=None):
        downloads.append(url)
        asset = os.path.basename(url)
        shutil.copy2(jxl_lz if asset.endswith(".tar.lz") else ljt_deb, dest)

    fetchdeps._download = _download
    fetchdeps.installed_versions = lambda: {}
    try:
        with linux_host():
            for key, asset, version in LINUX_ARCHIVE_TOOLS:
                fetchdeps.get_latest_release = lambda k, upstream=False: {
                    "version": version, "assets": [asset],
                    "urls": {asset: "https://example.invalid/" + asset}}
                got = fetchdeps.install_dependency(key, log=lambda m: None)
                check(f"{key} installs from upstream's own asset (got {got!r})",
                      got == version)

            check("...downloading exactly those two release assets",
                  sorted(downloads) == sorted(
                      "https://example.invalid/" + a for _, a, _ in LINUX_ARCHIVE_TOOLS))

            jxl_dir = os.path.join(tools_root, "libjxl v0.12.0")
            check("libjxl installs into a folder named for its version",
                  os.path.isdir(jxl_dir)
                  and all(os.path.isfile(os.path.join(jxl_dir, n))
                          for n in ("cjxl", "djxl")))
            check("...with the licence from the archive beside them",
                  os.path.isfile(os.path.join(jxl_dir, "LICENSE.libjpeg")))

            ljt_dir = os.path.join(tools_root, "libjpeg-turbo v3.2.0")
            launcher = os.path.join(ljt_dir, "jpegtran")
            check("jpegtran installs as a runnable stack",
                  os.path.isfile(launcher)
                  and os.path.isfile(os.path.join(ljt_dir, "jpegtran.bin")))
            check("...with the libjpeg.so.62 the binary links against beside it",
                  os.path.isfile(os.path.join(
                      ljt_dir, "lib64", "libjpeg.so.62.4.0")))
            script = (open(launcher, encoding="utf-8").read()
                      if os.path.isfile(launcher) else "")
            check("...and a launcher that points it at that folder",
                  "LD_LIBRARY_PATH" in script and "lib64" in script
                  and "jpegtran.bin" in script)

            # What the installer wrote is what the DETECTOR has to report: a
            # tool that lands invisibly keeps its amber chip for ever.
            found = tools_mod.detect_all_tools()
            check(f"the installed libjpeg-turbo is detected ({found.get('libjpeg_turbo')})",
                  (found.get("libjpeg_turbo") or {}).get("version") == "3.2.0"
                  and (found["libjpeg_turbo"].get("jpegtran_exe") or "").endswith("jpegtran"))
            check(f"the installed libjxl is detected ({found.get('libjxl')})",
                  (found.get("libjxl") or {}).get("version") == "0.12.0"
                  and (found["libjxl"].get("cjxl_exe") or "").endswith("cjxl")
                  and (found["libjxl"].get("djxl_exe") or "").endswith("djxl"))
    finally:
        restore_deps(real[0])
        (fetchdeps._download, fetchdeps.get_latest_release,
         fetchdeps.installed_versions) = real[1:]

# A file that is not lzip must be refused with the reason, never half-extracted
# into the folder an install would report as working.
with tempfile.TemporaryDirectory() as tmp:
    bogus = os.path.join(tmp, "jxl-linux-x86_64-static.tar.lz")
    with open(bogus, "wb") as fh:
        fh.write(b"not an lzip file at all")
    out = os.path.join(tmp, "out")
    os.makedirs(out)
    try:
        fetchdeps._extract_archive(bogus, out, log=lambda m: None)
        check("a file that is not lzip is refused", False)
    except RuntimeError as e:
        check(f"a file that is not lzip is refused ({e})", "not an lzip" in str(e))
    check("...and nothing was extracted", not os.listdir(out))


# --------------------------------------------------------------------------- #
# 13. A tool installed BEFORE the move is still found, and never overwritten
# --------------------------------------------------------------------------- #
# The tools moved from <app folder>/.dependencies to <music folder>/.mlo/tools.
# An install that predates the move keeps working from the old folder: detection
# reads every folder the resolver names, the row says which one it came from,
# and a reinstall installs into the NEW folder instead of writing to the old
# one. Getting this wrong is how a working install is told its tools are gone.
with tempfile.TemporaryDirectory() as tmp:
    new_root = os.path.join(tmp, "music", ".mlo", "tools")
    old_root = os.path.join(tmp, "app", ".dependencies")
    old_install = os.path.join(old_root, "oxipng v10.2.0")
    os.makedirs(old_install)
    # Both names, because which one is the marker is a property of the HOST
    # this suite runs on (oxipng.exe on Windows, the bare oxipng elsewhere).
    for name in ("oxipng.exe", "oxipng"):
        open(os.path.join(old_install, name), "w", encoding="utf-8").close()
    real = (fetchdeps.tools_dir, fetchdeps.tools_dirs, tools_mod.tools_dirs,
            fetchdeps.detect_all_tools)
    fetchdeps.tools_dir = lambda music_folder=None: new_root
    fetchdeps.tools_dirs = tools_mod.tools_dirs = (
        lambda music_folder=None: [new_root, old_root])
    tools_mod._TOOLS_CACHE = None
    try:
        found = fetchdeps.detect_all_tools()
        check("a tool in the pre-move folder is detected, not called missing",
              (found.get("oxipng") or {}).get("version") == "10.2.0")
        row = next(r for r in fetchdeps.dependency_rows() if r["key"] == "oxipng")
        check(f"...and is not reported missing (state {row['state']})",
              row["state"] != "missing")
        check("...and the row says which folder it came from",
              row["legacy_root"] is True and old_root in (row["note"] or ""))
        check("...and nothing has been written to the new folder for it",
              not os.path.isdir(new_root))
    finally:
        (fetchdeps.tools_dir, fetchdeps.tools_dirs, tools_mod.tools_dirs,
         fetchdeps.detect_all_tools) = real
        tools_mod._TOOLS_CACHE = None


if FAILURES:
    print(f"{len(FAILURES)} failure(s)")
    sys.exit(1)
print("OK test_dep_updates")
sys.exit(0)
