#!/usr/bin/env python3
"""Write the updater manifest (`latest.json`) for a release, from its own bundles.

The shell's updater asks ONE static URL what the newest build is, where it is
and what its signature is — `releases/latest/download/latest.json` (see
`plugins.updater.endpoints` in `desktop/src-tauri/tauri.conf.json`). Nothing in
this repository generated that file: `tauri-action` does it for projects that use
it, and this one builds the bundles with the CLI directly, so the manifest is
assembled here — from the artifacts the bundler left behind, which is the point:
the URL and the signature come from the SAME files the release publishes, so the
two cannot describe different builds.

A signature (`<artifact>.sig`, written by `bundle.createUpdaterArtifacts`) is
what marks a file as an updater artifact, so this walks them rather than
guessing names. The platform keys are Tauri's own: the updater asks for
`{os}-{arch}-{installer}` first and `{os}-{arch}` second (tauri-plugin-updater's
`get_urls`), where `{installer}` names the bundle type the installed app came
from — nsis, msi, app, appimage, deb, rpm — and `{os}` is `windows`, `darwin` or
`linux`.

A platform missing from the manifest does not fail loudly anywhere: the apps on
it simply stop being offered updates. That is why a missing OS family, or an
artifact with no signature beside it, is an error here, not a warning.

Run:
    python tools/make_updater_manifest.py CLIENTS_DIR --tag v5.3.0 \
        --repo owner/name [--notes-file docs/release-notes/release-notes-5.3.0.md] \
        [--date 2026-10-06T21:00:00Z] --out latest.json
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

# The bundle types, by the filename the bundler writes. Matched case-sensitively
# and in order (`.AppImage.tar.gz` must not be read as `.app.tar.gz`, which a
# case-blind match would do, and each is an updater artifact in its own right).
#
# Read off real bundles (tauri 2.11), not off the docs, whose example manifest
# shows a tarball for Linux: Windows updates from the NSIS installer itself
# (`…_x64-setup.exe` + `.sig`) and Linux from the AppImage itself
# (`…_amd64.AppImage` + `.sig`), while macOS really is a tarball
# (`la musica.app.tar.gz` + `.sig`). The forms the bundler does NOT write today
# are still recognised, after the measured one, because tauri-plugin-updater
# accepts them too — so a release that ever produces both is described by
# whichever Tauri names.
_KINDS = (
    ("-setup.exe.zip", "windows", "nsis"),
    (".msi.zip", "windows", "msi"),
    ("-setup.exe", "windows", "nsis"),
    (".msi", "windows", "msi"),
    (".app.tar.gz", "darwin", "app"),
    (".AppImage", "linux", "appimage"),
    (".AppImage.tar.gz", "linux", "appimage"),
    (".deb", "linux", "deb"),
    (".rpm", "linux", "rpm"),
)

# Every OS the release builds for. A release missing one of these has nothing to
# offer the apps on it, which is exactly the silent failure this checks for.
_OSES = ("windows", "darwin", "linux")

# The arch token the bundler writes, and the name Tauri's updater asks for.
_ARCHES = {
    "x86_64": "x86_64", "x64": "x86_64", "amd64": "x86_64",
    "aarch64": "aarch64", "arm64": "aarch64",
    "i686": "i686", "armv7": "armv7",
}

# Which installer a BARE `{os}-{arch}` key should point at when a release ships
# more than one for the same arch: the one an app can replace from inside
# itself. An AppImage swaps in place; a .deb needs root, so it is the last
# resort rather than the first.
_BARE_ORDER = ("appimage", "app", "nsis", "msi", "deb", "rpm")


def classify(name):
    """`(os, installer)` for an updater artifact's filename, or None."""
    for suffix, os_name, installer in _KINDS:
        if name.endswith(suffix):
            return os_name, installer
    return None


def arch_of(name):
    """The canonical arch of an artifact: the first token naming one, or None.

    The bundler writes it between underscores (`_x64-setup.exe.zip`,
    `_amd64.deb`, `_aarch64.app.tar.gz`), so the token is split off before the
    first `-` or `.` and looked up.
    """
    for token in re.split(r"_", name):
        head = re.split(r"[-.]", token)[0].lower()
        if head in _ARCHES:
            return _ARCHES[head]
    return None


def collect(clients_dir):
    """Every signed updater artifact under `clients_dir`.

    Returns `{(os, installer, arch): Path}` and the list of filenames that carry
    a signature but are not an updater artifact this script knows (a changed
    naming convention, which must be visible rather than silently dropped).
    """
    found, unknown = {}, []
    for sig in sorted(Path(clients_dir).rglob("*.sig")):
        artifact = sig.with_suffix("")
        if not artifact.is_file():
            continue
        kind = classify(artifact.name)
        arch = arch_of(artifact.name)
        if kind is None or arch is None:
            unknown.append(artifact.name)
            continue
        found[(*kind, arch)] = artifact
    return found, unknown


def served_name(artifact):
    """The file name the release actually serves for an artifact.

    GitHub rewrites a SPACE to a dot when a file is uploaded, so
    `la musica_5.3.0_x64-setup.exe` is published as
    `la.musica_5.3.0_x64-setup.exe` — measured on 5.3.0, whose first manifest
    pointed every platform at a URL that 404'd (the verifier fetched 9 bytes of
    "Not Found"). The manifest must name the SERVED file, not the file on disk.
    """
    return artifact.name.replace(" ", ".")


def manifest(found, version, tag, repo, notes, date):
    """The `latest.json` document: one entry per platform, plus the bare keys."""
    base = f"https://github.com/{repo}/releases/download/{quote(tag)}"
    platforms = {}
    for (os_name, installer, arch), artifact in sorted(found.items()):
        entry = {
            "signature": Path(f"{artifact}.sig").read_text(encoding="utf-8").strip(),
            "url": f"{base}/{quote(served_name(artifact))}",
        }
        platforms[f"{os_name}-{arch}-{installer}"] = entry
    # …and the bare key each installed app falls back to, decided by
    # `_BARE_ORDER` so one release cannot mean a different installer than
    # another for the same platform.
    for os_name in _OSES:
        for arch in {a for (candidate, _, a) in found if candidate == os_name}:
            candidates = [i for (c, i, a) in found if c == os_name and a == arch]
            if not candidates:
                continue
            best = min(candidates, key=lambda i: _BARE_ORDER.index(i) if i in _BARE_ORDER else 99)
            platforms.setdefault(
                f"{os_name}-{arch}",
                {
                    "signature": Path(f"{found[(os_name, best, arch)]}.sig").read_text(encoding="utf-8").strip(),
                    "url": f"{base}/{quote(served_name(found[(os_name, best, arch)]))}",
                },
            )
    return {
        "version": version,
        "notes": notes,
        "pub_date": date,
        "platforms": dict(sorted(platforms.items())),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("clients", help="folder the platform artifacts were collected into")
    ap.add_argument("--version", required=True, help="the release's version, without the v")
    ap.add_argument("--tag", required=True, help="the release's tag, e.g. v5.3.0")
    ap.add_argument("--repo", required=True, help="owner/name the assets live under")
    ap.add_argument("--notes-file", default=None, help="release notes to carry as `notes`")
    ap.add_argument("--date", default=None, help="RFC 3339, default: now (UTC)")
    ap.add_argument("--out", required=True, help="where to write latest.json")
    args = ap.parse_args()

    found, unknown = collect(args.clients)
    for name in unknown:
        print(f"  note  {name} carries a signature but is not a known updater artifact")
    have = {os_name for (os_name, _, _) in found}
    missing = [os_name for os_name in _OSES if os_name not in have]
    if missing:
        seen = sorted(p.name for p in Path(args.clients).rglob("*.sig")) or ["(no .sig files at all)"]
        print(f"make_updater_manifest: no updater artifact for {', '.join(missing)} — "
              f"those installs would stop being offered updates")
        # The suffix table at the top of this file is what recognises an
        # artifact; a bundler that names one differently lands here, so the
        # names are printed rather than left to be discovered by re-reading CI
        # logs.
        print("  the signatures this release carries: " + ", ".join(seen))
        print("  add the missing suffix to _KINDS in tools/make_updater_manifest.py")
        return 1

    notes = ""
    if args.notes_file and Path(args.notes_file).is_file():
        notes = Path(args.notes_file).read_text(encoding="utf-8").strip()
    date = args.date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    document = manifest(found, args.version, args.tag, args.repo, notes, date)
    Path(args.out).write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    for key, entry in document["platforms"].items():
        print(f"  {key:<26} {entry['url'].rsplit('/', 1)[-1]}")
    print(f"make_updater_manifest: {len(document['platforms'])} platforms for {args.tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())