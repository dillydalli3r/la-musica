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
    """Point every install/detection path at a temp .dependencies folder."""
    real = (fetchdeps.DEPS_DIR, tools_mod.DEPS_DIR)
    fetchdeps.DEPS_DIR = tools_mod.DEPS_DIR = deps_dir
    tools_mod._TOOLS_CACHE = None
    return real


def restore_deps(real):
    fetchdeps.DEPS_DIR, tools_mod.DEPS_DIR = real
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
        check(f"{key} reads Update where this host can install it "
              f"({row['state']} / {row['install_kind']})",
              row["state"] == ("update" if row["install_kind"] == "deps" else "ok"))
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


if FAILURES:
    print(f"{len(FAILURES)} failure(s)")
    sys.exit(1)
print("OK test_dep_updates")
sys.exit(0)
