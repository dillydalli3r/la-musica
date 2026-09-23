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
    lock_names = re.findall(r'^name = "([^"]+)"\nversion = "([^"]+)"',
                            read("desktop/src-tauri/Cargo.lock"), re.M)
    strays = [name for name, ver in lock_names if ver == source]
    if strays != ["mlo-desktop"]:
        print(f"\ncheck_versions: the lock carries {source} in "
              f"{strays or 'no package'} — expected exactly ['mlo-desktop']; "
              f"a dependency's own version is not ours to bump")
        return 1

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
