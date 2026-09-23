#!/usr/bin/env python3
"""Every version string in the tree must agree, before anything is built.

The app reports `mlo.__version__` on `/api/health`, but five other files name
their own version and nothing compared them: `tauri.conf.json` (which the iOS
IPA's plist and every installer take their version from), the Tauri crate,
`desktop/package.json` (which names the IPA file), `web/package.json`, the
Dockerfile's `MLO_VERSION` (which is what a running container knows about
itself) and the README header. A release where one of them was missed ships a
build that disagrees with itself — an IPA whose Settings pane says 3.0.0 while
the file name says 3.1.0 — and nothing failed.

Run:  python tools/check_versions.py           (exit 0 = all agree, 1 = drift)
      python tools/check_versions.py v3.1.0    (also require the tag to match)
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = "mlo/__init__.py"


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        return fh.read()


def find(pattern, text, path, label):
    m = re.search(pattern, text, re.M)
    if not m:
        sys.exit(f"check_versions: no {label} found in {path} (pattern {pattern!r})")
    return m.group(1)


def lock_pins(text):
    """Every package in a Cargo.lock as `name -> (version, checksum)`."""
    pins = {}
    for block in text.split("[[package]]")[1:]:
        name = re.search(r'^name = "([^"]+)"', block, re.M)
        ver = re.search(r'^version = "([^"]+)"', block, re.M)
        if not name or not ver:
            continue
        sum_ = re.search(r'^checksum = "([^"]+)"', block, re.M)
        pins[name.group(1)] = (ver.group(1), sum_.group(1) if sum_ else "")
    return pins


def main():
    source = find(r'^__version__ = "([^"]+)"', read(SOURCE), SOURCE, "`__version__`")
    found = {SOURCE: source}

    conf = json.loads(read("desktop/src-tauri/tauri.conf.json"))
    found["desktop/src-tauri/tauri.conf.json"] = str(conf.get("version") or "")

    found["desktop/src-tauri/Cargo.toml"] = find(
        r'^version = "([^"]+)"', read("desktop/src-tauri/Cargo.toml"),
        "desktop/src-tauri/Cargo.toml", "package version")

    # Cargo.lock pins the crate's own version and `cargo build` rewrites it on
    # the next build; a lock left behind is a diff nobody asked for.
    found["desktop/src-tauri/Cargo.lock"] = find(
        r'^name = "mlo-desktop"\nversion = "([^"]+)"',
        read("desktop/src-tauri/Cargo.lock"), "desktop/src-tauri/Cargo.lock",
        "mlo-desktop lock version")

    # …and nothing ELSE in that lock may carry our version. A dependency whose
    # release number happens to equal ours is not ours to rename: `bumpalo` is
    # released as 3.20.3 today, and a blanket text replace of the version in
    # the lock turned its entry into "3.20.4" with 3.20.3's checksum under it —
    # a lockfile cargo cannot resolve, discovered only by the desktop build.
    lock = read("desktop/src-tauri/Cargo.lock")
    strays = [name for name, (ver, _) in lock_pins(lock).items() if ver == source]
    if strays != ["mlo-desktop"]:
        # A dependency whose release number HAPPENS to equal ours is not ours
        # and not a bug: `bumpalo` shipped as 3.20.3, `serde_with` as 3.22.0,
        # and a rule that looks for our number alone flags them every time we
        # land on one. The bug this check exists for is a BLANKET text replace,
        # which rewrites a dependency's version and leaves its OLD checksum
        # under it — a lockfile cargo refuses. So compare with HEAD: whatever
        # MOVED must be mlo-desktop, and nothing moving (a committed tree, as
        # in CI) means there is nothing here to police.
        head = subprocess.run(["git", "show", "HEAD:desktop/src-tauri/Cargo.lock"],
                              cwd=ROOT, capture_output=True, text=True)
        head_pins = lock_pins(head.stdout) if head.returncode == 0 else None
        moved = ([name for name, pin in lock_pins(lock).items()
                  if head_pins is not None and head_pins.get(name) != pin]
                 if head_pins is not None else None)
        if head_pins is None or not moved:
            print(f"  note  the lock carries {source} in {strays} — other "
                  f"packages' own release numbers, and nothing moved since HEAD")
        else:
            wrong = [name for name in moved if name != "mlo-desktop"]
            if wrong:
                print(f"\ncheck_versions: {wrong} changed in the lock — a "
                      f"dependency's own version is not ours to bump")
                return 1
            print(f"  ok    only mlo-desktop moved in the lock ({source})")

    # The iOS build number ships in the same plist as the marketing version;
    # two numbers that can disagree is the exact drift this script exists for.
    found["desktop/src-tauri/tauri.conf.json (iOS bundleVersion)"] = str(
        (conf.get("bundle") or {}).get("iOS", {}).get("bundleVersion") or "")

    for path in ("desktop/package.json", "web/package.json"):
        found[path] = str(json.loads(read(path)).get("version") or "")

    found["Dockerfile"] = find(r'^ARG MLO_VERSION=([^\s\\]+)', read("Dockerfile"),
                               "Dockerfile", "`ARG MLO_VERSION`")

    found["README.md"] = find(r'^\*\*v([0-9][^*]*)\*\*', read("README.md"),
                              "README.md", "version header")

    # desktop/README quotes the iOS bundle version in prose; a stale number
    # there is actively misleading during a release.
    found["desktop/README.md"] = find(
        r'`bundle\.iOS\.bundleVersion` ([0-9][^,\s]*)', read("desktop/README.md"),
        "desktop/README.md", "quoted `bundle.iOS.bundleVersion`")

    drift = {p: v for p, v in found.items() if v != source}
    for path, value in sorted(found.items()):
        print(f"  {'ok  ' if value == source else 'DRIFT'} {value:<12} {path}")
    if drift:
        print(f"\ncheck_versions: {len(drift)} file(s) disagree with {SOURCE} "
              f"({source})")
        return 1

    if len(sys.argv) > 1:
        tag = sys.argv[1].lstrip("v")
        if tag != source:
            print(f"\ncheck_versions: tag {sys.argv[1]} does not match {SOURCE} ({source})")
            return 1
        print(f"\ncheck_versions: tag {tag} matches every copy")

    print(f"\ncheck_versions: all {len(found)} copies say {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
