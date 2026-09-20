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

if FAILURES:
    print(f"{len(FAILURES)} failure(s)")
    sys.exit(1)
print("OK test_dep_updates")
sys.exit(0)
