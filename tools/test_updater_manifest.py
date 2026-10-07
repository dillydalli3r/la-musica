#!/usr/bin/env python3
"""The updater manifest: what every installed app reads to learn about a release.

`tools/make_updater_manifest.py` turns a release's own artifacts into
`latest.json`, and this drives it over a fabricated clients tree — the shape the
desktop workflow uploads — so the mappings are pinned without a build and
without the network:

  * every platform key Tauri's updater asks for (`{os}-{arch}-{installer}`, and
    the bare `{os}-{arch}` fallback), including WHICH installer the bare key
    means when a release ships more than one for the same arch;
  * the signature, which must come from the artifact's OWN `.sig` — a manifest
    whose signature and URL describe different files installs nothing, and the
    failure looks like a checksum error at the user's end, not here;
  * the failure that matters: a release with no updater artifact for an OS is an
    error, because a platform missing from the manifest does not fail anywhere
    else — its installed apps simply stop being offered updates.

No build is involved: the artifacts are empty files, which is all the mapping
reads. Exit 0 pass, 1 failed.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "make_updater_manifest.py")

FAILURES = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok or not detail else f" — {detail}"))
    if not ok:
        FAILURES.append(name)


def artifact(tree, name, signature="sig-"):
    """A signed updater artifact: the file, and the `.sig` beside it."""
    path = os.path.join(tree, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("payload")
    with open(path + ".sig", "w", encoding="utf-8") as fh:
        fh.write(f"{signature}{name}\n")
    return path


def manifest_for(files, notes=None):
    """Run the tool over a tree of `files`, and return (exit code, output, JSON)."""
    tree = tempfile.mkdtemp(prefix="mlo-manifest-")
    for name in files:
        artifact(tree, name)
    out = os.path.join(tree, "latest.json")
    argv = [sys.executable, SCRIPT, tree, "--version", "5.3.0", "--tag", "v5.3.0",
            "--repo", "dillydalli3r/la-musica", "--date", "2026-10-06T21:00:00Z", "--out", out]
    if notes:
        notes_path = os.path.join(tree, "notes.md")
        with open(notes_path, "w", encoding="utf-8") as fh:
            fh.write(notes)
        argv += ["--notes-file", notes_path]
    proc = subprocess.run(argv, capture_output=True, text=True)
    document = None
    if os.path.exists(out):
        with open(out, encoding="utf-8") as fh:
            document = json.load(fh)
    return proc.returncode, proc.stdout + proc.stderr, document


def main():
    # The bundler's own names, one per platform, plus both Linux installers so
    # the bare key has a choice to make. The Windows entry is the raw NSIS
    # installer, which is what tauri 2.11 writes a `.sig` beside (read off a
    # real build); the zipped form the plugin also accepts is covered below.
    files = [
        "la musica_5.3.0_x64-setup.exe",
        "la musica_5.3.0_aarch64.app.tar.gz",
        "la musica_5.3.0_amd64.AppImage.tar.gz",
        "la musica_5.3.0_amd64.deb",
    ]
    code, output, doc = manifest_for(files, notes="# la musica 5.3.0\n\nSome notes.\n")

    check("the tool exits 0 on a complete release", code == 0, output.strip())
    if doc is None:
        print("  FAIL no manifest was written")
        return 1

    platforms = doc.get("platforms", {})
    # The FILES carry the product name with its space; the URLs carry what
    # GitHub serves for them, which has that space rewritten to a dot. Both
    # halves matter: a manifest built from the file's own name 404s on every
    # platform (measured on the 5.3.0 release), and one built by hand-escaping
    # it as %20 does exactly the same.
    expected = {
        "windows-x86_64-nsis": "la.musica_5.3.0_x64-setup.exe",
        "windows-x86_64": "la.musica_5.3.0_x64-setup.exe",
        "darwin-aarch64-app": "la.musica_5.3.0_aarch64.app.tar.gz",
        "darwin-aarch64": "la.musica_5.3.0_aarch64.app.tar.gz",
        "linux-x86_64-appimage": "la.musica_5.3.0_amd64.AppImage.tar.gz",
        "linux-x86_64-deb": "la.musica_5.3.0_amd64.deb",
        # The bare fallback: an AppImage can replace itself, a .deb needs root —
        # the preference, not the alphabet, decides.
        "linux-x86_64": "la.musica_5.3.0_amd64.AppImage.tar.gz",
    }
    check(
        "every key the updater asks for is present",
        set(platforms) == set(expected),
        f"got {sorted(platforms)}",
    )
    for key, want in expected.items():
        entry = platforms.get(key, {})
        check(
            f"{key} points at {want}",
            entry.get("url", "").endswith("/" + want),
            entry.get("url", "(missing)"),
        )

    check(
        "the url is the release's own asset path",
        all(
            "/releases/download/v5.3.0/" in entry["url"]
            and entry["url"].startswith("https://github.com/dillydalli3r/la-musica/")
            for entry in platforms.values()
        ),
        json.dumps(platforms, indent=1)[:300],
    )
    check(
        "no url escapes the product name's space",
        all("%20" not in entry["url"] and " " not in entry["url"]
            for entry in platforms.values()),
        json.dumps(sorted(e["url"].rsplit("/", 1)[-1] for e in platforms.values())),
    )
    check(
        "each signature is the one beside its own artifact",
        platforms.get("linux-x86_64-deb", {}).get("signature") == "sig-la musica_5.3.0_amd64.deb"
        and platforms.get("windows-x86_64-nsis", {}).get("signature")
        == "sig-la musica_5.3.0_x64-setup.exe",
        json.dumps({k: v.get("signature") for k, v in platforms.items()}, indent=1)[:300],
    )

    # The other form tauri-plugin-updater accepts on Windows (a ZIP holding the
    # installer): still recognised, still under the same key.
    zipped = ["la musica_5.3.0_x64-setup.exe.zip"] + files[1:]
    zipped_code, zipped_out, zipped_doc = manifest_for(zipped)
    check(
        "a zipped installer maps to the same platform key",
        zipped_code == 0
        and zipped_doc is not None
        and zipped_doc["platforms"]["windows-x86_64-nsis"]["url"].endswith(
            "la.musica_5.3.0_x64-setup.exe.zip"
        ),
        zipped_out.strip()[:200],
    )
    check(
        "the version, the date and the notes travel with it",
        doc.get("version") == "5.3.0"
        and doc.get("pub_date") == "2026-10-06T21:00:00Z"
        and "Some notes." in doc.get("notes", ""),
        json.dumps({k: doc.get(k) for k in ("version", "pub_date")}),
    )

    # A platform with NO updater artifact: the failure the tool exists for.
    partial = [name for name in files if not name.startswith("la musica_5.3.0_aarch64")]
    code, output, doc = manifest_for(partial)
    check("a release with no artifact for an OS fails", code != 0, f"exit {code}")
    check("…and says which OS is missing", "darwin" in output, output.strip()[:200])
    check("…and writes no manifest to be published", doc is None or doc.get("platforms"))

    # A signature nobody can classify: visible, and not fatal on its own.
    code, output, _ = manifest_for(files + ["something-else.bin"])
    check("an unclassifiable signature is reported, not fatal", code == 0 and "something-else.bin" in output,
          output.strip()[:200])

    print()
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("PASS — the updater manifest names every platform, from the artifacts' own signatures")
    return 0


if __name__ == "__main__":
    sys.exit(main())