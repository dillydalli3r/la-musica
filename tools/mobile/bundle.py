#!/usr/bin/env python3
"""Assemble what a mobile build ships so the backend can run on the device.

The desktop shell spawns `python -m uvicorn`. A phone has no Python at all, so a
mobile build has to carry one: a CPython interpreter, the standard library, the
backend's own Python sources and a site-packages tree, all laid out where the
shell (`desktop/src-tauri/src/mobile_backend.rs`) looks for them. This script
produces that tree, and `verify` reads a *built* .apk/.ipa back to prove the
tree really made it into the artifact.

    python tools/mobile/bundle.py ios      [--wheels DIR] [--out DIR]
    python tools/mobile/bundle.py android  [--wheels DIR] [--tools DIR] [--out DIR]
    python tools/mobile/bundle.py verify  --apk PATH [--require-tools]
    python tools/mobile/bundle.py verify  --ipa PATH

Platform choices, and why they are these and not others:

iOS takes BeeWare's **Python-Apple-support** build (a `Python.xcframework` with
a device slice, headers and the standard library) — the supported way to embed
CPython in an iOS app, and what the CPython docs' "Using Python on iOS" recipe
describes. The framework is linked and embedded by `bundle.iOS.frameworks` in
tauri.conf.json; the standard library, site-packages and the app's own modules
go in through `bundle.resources`, which on iOS lands as *real files* under
`<app>/assets` (unlike Android, see below). The tree is laid out exactly as the
docs prescribe — `python/lib/python3.13` for the standard library,
`python/lib/python3.13/lib-dynload` for its extension modules, `site-packages`
beside them — so PYTHONHOME/PYTHONPATH are the documented ones.

  What is deliberately NOT done here: the docs also convert every third-party
  binary extension module (.so) into an individual signed framework and leave a
  `.fwork` marker behind. That conversion is an App Store packaging rule, not a
  functional one — CPython's own iOS recipe ships the standard library's
  `lib-dynload/*.so` as plain dylibs inside the bundle — so this pipeline ships
  `site-packages/**/*.so` the same way lib-dynload does. An App-Store-*submitted*
  build needs the conversion (Python.xcframework/build/utils.sh does it in an
  Xcode "Process Python libraries" phase); a sideloaded/re-signed build loads
  the dylibs as-is. See desktop/README.md.

Android takes the **official python.org Android embeddable package** and embeds
it in-process, which is not a choice: docs.python.org "Using Python on Android"
states that embedding is *the only way* to run Python on Android — there is no
`python` executable and no console, so "spawn the interpreter as a child
process" is not available to any app, ours included. The release package is
pinned and verified like every other input.

  Two packaging consequences, both from how Android packages an app:

  * The interpreter and its shared libraries MUST be in the APK's native-lib
    directory (`jniLibs/<abi>/`), and only files named `lib*.so` survive that
    trip. They are therefore staged as `libpython3.14.so`, `libpython3.so`,
    `lib*_python.so` — the exact file set CPython's own Android testbed copies.
  * Assets are NOT readable as files: an app reads them through the Java
    AssetManager, and `resource_dir()` on Android is the pseudo-URI
    `asset://localhost/`. CPython needs a real filesystem path for `import`, so
    the standard library + site-packages + app code ship as ONE gzipped tar
    (`libmlopy.so`, which is that archive under a name AGP will package) in the
    same native-lib directory, and the shell extracts it on first launch. One
    file, real path, no JNI.

  Wheels for Android are the cibuildwheel ones: PyPI has no `android_*` wheel
  for pydantic-core (FastAPI cannot import without it) or for Pillow, so those
  come from `--wheels` where CI puts the cibuildwheel output, cached by version.

The CLI tools (ffmpeg/flac) are a separate concern — they are not Python — and
Android is the only mobile platform that can run them: binaries shipped in the
native-lib directory may be executed there, which is how the shell points
mlo/tools.py at them (`MLO_BUNDLED_TOOLS`). iOS cannot execute them at all, so
the iOS stage bundles none and the app reports the affected features as
unavailable. Build the Android ones with tools/mobile/build_android_tools.sh.

Nothing here writes into `src-tauri/gen/` (generated, never committed): the
staged tree is a build input that CI copies into the generated project.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "desktop" / "src-tauri" / "target" / "mobile-cache"
WHEELS = ROOT / "desktop" / "src-tauri" / "target" / "mobile-wheels"

# Inputs, pinned. The hashes are the verification: a build that fetched
# something else is not our build. The Android package's md5 is what python.org
# publishes for it (python.org publishes no sha256); its sha256 is recorded here
# as well so a mismatch in either direction is caught.
PINS = {
    "ios": {
        "python": "3.13",
        "platform_tag": "ios_13_0_arm64_iphoneos",
        "runtime": {
            "name": "Python-3.13-iOS-support.b15.tar.gz",
            "url": "https://github.com/beeware/Python-Apple-support/releases/"
                   "download/3.13-b15/Python-3.13-iOS-support.b15.tar.gz",
            "sha256": "80175765a31babe43b0910395cf86ba4e8412adf1902b069d55b74d523ecc5d1",
            "label": "Python-Apple-support 3.13-b15",
        },
        "requirements": "requirements-ios.txt",
        "out": ROOT / "desktop" / "src-tauri" / "resources" / "mobile" / "ios",
    },
    "android": {
        "python": "3.14",
        "platform_tag": "android_24_arm64_v8a",
        "abi": "arm64-v8a",
        "runtime": {
            "name": "python-3.14.7-aarch64-linux-android.tar.gz",
            "url": "https://www.python.org/ftp/python/3.14.7/"
                   "python-3.14.7-aarch64-linux-android.tar.gz",
            "sha256": "6d50cc3aa66e414a439594089bcdfb5f1264358155c70c1f00471c24cfb477fb",
            "md5": "7bc230d345204798769f41c29f91e30c",
            "label": "CPython 3.14.7 (python.org Android embeddable package)",
        },
        "requirements": "requirements-android.txt",
        "out": ROOT / "desktop" / "src-tauri" / "resources" / "mobile" / "android",
    },
}

# Import names that MUST resolve for `server.main` to be importable at all.
# fastapi/starlette/pydantic/uvicorn are the framework; the rest are the
# transitive imports those make on the way in. pydantic_core is the one that
# has no wheel anywhere and is why the pipeline refuses to stage without it.
# The import closure the backend needs, taken from what the pinned
# requirements actually resolve to (verified by installing them for this
# interpreter and listing the tree) — NOT from a package's dependency list as
# remembered. `sniffio` sat here for a run and failed the iOS build on a tree
# that was perfectly good: modern anyio/httpcore no longer install it, so the
# check demanded an import nothing provides. Re-derive this list from the
# closure whenever the requirements move.
REQUIRED_IMPORTS = [
    "fastapi", "starlette", "pydantic", "pydantic_core", "uvicorn",
    "click", "h11", "anyio", "httpx", "httpcore", "idna",
    "certifi", "aiofiles", "mutagen", "multipart", "websockets",
    "typing_extensions", "typing_inspection", "annotated_types",
]

# Present when the platform allows it; the backend runs without them and
# /api/capabilities reports the gap (`mlo/deps.py: HAS_PIL`).
OPTIONAL_IMPORTS = ["PIL"]

# The packages that exist only as wheels pydantic-core is built into. When the
# pipeline is asked for a pure-client tree they are the ones left out, because
# dropping any of them is what makes the rest of the set installable at all.
PYDANTIC_CHAIN = {"fastapi", "pydantic", "pydantic-core"}

# The CLI tools Android can execute, as (built name, staged name). AGP only
# packages `lib*.so` from jniLibs, so the staged name is the tool's real path in
# the APK: mlo/tools.py resolves both spellings under MLO_BUNDLED_TOOLS.
ANDROID_TOOLS = {
    "ffmpeg": "libffmpeg.so",
    "ffprobe": "libffprobe.so",
    "flac": "libflac.so",
    "metaflac": "libmetaflac.so",
}


def log(msg: str) -> None:
    print(f"[bundle] {msg}", flush=True)


def die(msg: str) -> "NoReturn":  # noqa: F821 - simple CLI exit
    print(f"[bundle] ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_file(path: Path, spec: dict) -> None:
    """Refuse a runtime that is not the one this file pins."""
    if spec.get("sha256"):
        got = sha256_file(path)
        if got != spec["sha256"]:
            die(f"{path.name} is not the pinned build:\n"
                f"  expected sha256 {spec['sha256']}\n  got      sha256 {got}\n"
                f"Delete {path} and re-run if the pin is meant to move.")
    if spec.get("md5"):
        got = md5_file(path)
        if got != spec["md5"]:
            die(f"{path.name} is not the pinned build: expected md5 "
                f"{spec['md5']}, got {got}")


def fetch(spec: dict, cache: Path) -> Path:
    """Download *spec* once, verify it, and reuse it after that."""
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / spec["name"]
    if dest.exists():
        verify_file(dest, spec)
        log(f"cached   {dest.name} ({dest.stat().st_size / 1e6:.1f} MB, hash ok)")
        return dest
    part = dest.with_suffix(dest.suffix + ".part")
    log(f"fetching {spec['url']}")
    with urllib.request.urlopen(spec["url"]) as src, open(part, "wb") as out:
        shutil.copyfileobj(src, out)
    verify_file(part, spec)
    part.replace(dest)
    log(f"fetched  {dest.name} ({dest.stat().st_size / 1e6:.1f} MB, hash ok)")
    return dest


def extract_tar(archive: Path, dest: Path) -> Path:
    """Unpack once; the result is reused while it is there."""
    if dest.exists():
        return dest
    dest.mkdir(parents=True)
    with tarfile.open(archive) as t:
        # `filter="data"` is Python 3.12+; on 3.11 the default (unfiltered) is
        # what the interpreter does anyway.
        try:
            t.extractall(dest, filter="data")
        except TypeError:
            t.extractall(dest)
    return dest


# Standard-library directories that must not ship inside an app: CPython's own
# test suite (the single largest part of the tree, and nothing imports it), the
# Tk/IDLE toolchain that no mobile app can open a window with, the old
# lib2to3, and the build leftovers. Everything else is kept: a phone bundle is
# the wrong place to guess which stdlib module the backend will import.
STDLIB_EXCLUDE = (
    "test", "tests", "idlelib", "tkinter", "turtledemo", "lib2to3",
    "ensurepip", "__pycache__", "*.pyc", "*.pyo",
)


def copy_tree(src: Path, dest: Path, exclude: tuple[str, ...] = ()) -> int:
    """Copy *src* into *dest* (Python's None-aware copytree, no py3.8+ kwargs)."""
    import fnmatch

    def ignore(_dir, names):
        return [n for n in names for pat in exclude if fnmatch.fnmatch(n, pat)]

    shutil.copytree(src, dest, dirs_exist_ok=True, symlinks=True, ignore=ignore or None)
    return sum(1 for _ in dest.rglob("*"))


def _run_pip(spec: dict, req_file: Path, target: Path, wheels: Path | None) -> str:
    """Install *req_file* for the target platform; "" on success, else the tail."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--only-binary=:all:",           # nothing here can be built on a phone
        "--platform", spec["platform_tag"],
        "--python-version", spec["python"],
        "--implementation", "cp",
        "--no-compile",                  # bytecode is written on the device or not at all
        "--quiet", "--target", str(target),
        "-r", str(req_file),
    ]
    if wheels and wheels.is_dir():
        cmd += ["--find-links", str(wheels)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return ""
    return "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-12:])


def pip_install(spec: dict, req_file: Path, target: Path, wheels: Path | None,
                allow_missing: bool) -> list[str]:
    """Install the pinned requirements for the *target* platform, not this host.

    Returns the requirements it had to leave out (only ever non-empty when
    *allow_missing* asked for the pure-client tree), and refuses to produce an
    app that would install and then fail on first use otherwise.
    """
    log(f"pip install ({spec['platform_tag']}, python {spec['python']})")
    failure = _run_pip(spec, req_file, target, wheels)
    if not failure:
        return []

    hint = (
        "\npydantic-core has no wheel for " + spec["platform_tag"] + ", and FastAPI\n"
        "cannot import without it, so this bundle would install and then fail on\n"
        "first use. Build it once and pass the directory it lands in:\n"
        "  pip install cibuildwheel\n"
        "  python tools/mobile/bundle.py " + spec["platform_tag"].split("_")[0]
        + " --wheels <dir> --build-missing-wheels\n"
        "cibuildwheel is the ecosystem's tool for this (BeeWare's mobile-wheels index\n"
        "recommends it and Chaquopy's docs point at it for Android on 3.13+), and it\n"
        "needs the platform toolchain:\n"
        "  ios     -> macOS with Xcode\n"
        "  android -> Linux/macOS with the Android SDK+NDK and java\n"
        "Neither runs on Windows; CI has both runners, and it caches the wheel.\n"
        "Pass --allow-missing-required only to stage the pure-client build the setup\n"
        "wizard must be able to handle."
    )
    if not allow_missing:
        die(f"pip install failed for {spec['platform_tag']}:\n{failure}{hint}")

    # The pure-client tree, on purpose: install everything that does have a
    # wheel and record what could not be installed, so the app can say why
    # "Host on this device" is not on offer instead of failing at import.
    keep, dropped = [], []
    for line in req_file.read_text(encoding="utf-8").splitlines():
        name = line.split("#")[0].split("==")[0].strip().lower()
        (dropped if name in PYDANTIC_CHAIN else keep).append(line)
    partial = CACHE / (req_file.stem + "-partial.txt")
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_text("\n".join(keep) + "\n", encoding="utf-8")
    log("WARNING  no wheel for: " + ", ".join(dropped) + " — staging the "
        "pure-client tree (--allow-missing-required)")
    failure = _run_pip(spec, partial, target, wheels)
    if failure:
        die(f"pip install failed for {spec['platform_tag']}:\n{failure}{hint}")
    return dropped


def missing_imports(site_packages: Path, names: list[str]) -> list[str]:
    """Which *names* site-packages cannot import: no dir, no module, no dist-info."""
    missing = []
    for name in names:
        if (site_packages / name).is_dir() or (site_packages / f"{name}.py").is_file():
            continue
        missing.append(name)
    return missing


def installed_top_level(site_packages: Path) -> list[str]:
    out = []
    for entry in sorted(site_packages.iterdir()):
        if entry.name.endswith(".dist-info") or entry.name == "__pycache__":
            continue
        if entry.is_dir() or entry.suffix == ".py":
            out.append(entry.stem if entry.suffix == ".py" else entry.name)
    return out


def tree_sha256(path: Path, exclude: tuple[str, ...] = ("runtime.json",)) -> str:
    """One hash over a tree's names and contents — its identity as a build input."""
    h = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = file.relative_to(path).as_posix()
        if rel in exclude:
            continue
        h.update(rel.encode())
        h.update(b"\0")
        h.update(sha256_file(file).encode())
    return h.hexdigest()


def stage_app_code(dest: Path) -> None:
    """The backend's own Python sources, which the interpreter imports."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for pkg in ("server", "mlo"):
        copy_tree(ROOT / pkg, dest / pkg,
                  exclude=("__pycache__", "*.pyc", "test_*", "*.log"))
    for extra in ("start_app.py",):
        if (ROOT / extra).is_file():
            shutil.copy2(ROOT / extra, dest / extra)


def write_manifest(python_root: Path, spec: dict, site_packages: Path,
                   app_dir: Path, tools: list[str]) -> dict:
    missing_req = missing_imports(site_packages, REQUIRED_IMPORTS)
    missing_opt = missing_imports(site_packages, OPTIONAL_IMPORTS)
    reason = ""
    if missing_req:
        reason = ("this build's Python cannot import the backend: missing "
                  + ", ".join(missing_req))
    manifest = {
        "schema": 1,
        "platform": "ios" if "ios" in spec["platform_tag"] else "android",
        "python": spec["python"],
        "runtime": spec["runtime"]["label"],
        "platform_tag": spec["platform_tag"],
        "site_packages": installed_top_level(site_packages),
        "missing_required": missing_req,
        "missing_optional": missing_opt,
        "backend_importable": not missing_req,
        "app_sha256": tree_sha256(app_dir, exclude=()),
        "content_sha256": tree_sha256(python_root),
        "tools": tools,
        "reason": reason,
        "staged": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (python_root / "runtime.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                              encoding="utf-8")
    return manifest


def stamp_path(platform: str) -> Path:
    return CACHE / f"staged-{platform}.json"


def inputs_key(platform: str, spec: dict, wheels: Path | None, tools: Path | None,
               allow_missing: bool) -> str:
    h = hashlib.sha256()
    h.update(json.dumps(spec["runtime"], sort_keys=True).encode())
    h.update(spec["platform_tag"].encode())
    h.update((ROOT / "tools" / "mobile" / spec["requirements"]).read_bytes())
    h.update(tree_sha256(ROOT / "server", exclude=()).encode())
    h.update(tree_sha256(ROOT / "mlo", exclude=()).encode())
    for d in (wheels, tools):
        if d and d.is_dir():
            h.update(json.dumps(sorted(p.name for p in d.iterdir())).encode())
    h.update(b"allow-missing" if allow_missing else b"strict")
    return h.hexdigest()


def up_to_date(platform: str, key: str, manifest: Path) -> bool:
    stamp = stamp_path(platform)
    if not manifest.is_file() or not stamp.is_file():
        return False
    try:
        return json.loads(stamp.read_text(encoding="utf-8")) == {"key": key}
    except (OSError, ValueError):
        return False


def mark_staged(platform: str, key: str) -> None:
    stamp_path(platform).parent.mkdir(parents=True, exist_ok=True)
    stamp_path(platform).write_text(json.dumps({"key": key}), encoding="utf-8")


def build_missing_wheels(platform: str, wheel_dir: Path, spec: dict) -> None:
    """Build the wheels PyPI does not have, with the ecosystem's own tool.

    cibuildwheel is what BeeWare's mobile-wheels index recommends and what
    Chaquopy's own docs point at for Android wheels on 3.13+. It is invoked
    here rather than hand-rolled so the wheel a CI cache holds and the wheel a
    Mac builds locally come from the same code path.
    """
    # The TARGET interpreter, taken from the platform's own spec — never the
    # host's. This ran as `sys.version_info` once, and on a cp312 runner that
    # silently pinned CIBW_BUILD to `cp312-*` before the platform branch could
    # set anything: cibuildwheel then reported "0 builds selected" and exited 3,
    # so the mobile jobs were skipped and the release could not proceed. The
    # wheel must match the runtime the app ships (3.13 for iOS, 3.14 for
    # Android), which has nothing to do with the machine building it.
    target = f"cp{spec['python'].replace('.', '')}-*"
    os.environ.setdefault("CIBW_BUILD", target)
    if platform == "ios":
        if sys.platform != "darwin":
            die("cibuildwheel iOS builds need macOS with Xcode — this host is "
                f"{sys.platform}. Pass --wheels with wheels built on macOS.")
        env_arch = ["--platform", "ios", "--archs", "arm64_iphoneos"]
        os.environ.setdefault("CIBW_XBUILD_TOOLS", "cargo rustc")
    else:
        if os.name == "nt":
            die("cibuildwheel Android builds need a POSIX host (Linux/macOS) "
                "with the Android SDK, NDK and java — this host is Windows. "
                "Pass --wheels with wheels built in CI.")
        if not os.environ.get("ANDROID_HOME"):
            die("cibuildwheel Android builds need ANDROID_HOME pointing at an "
                "Android SDK (the script installs what it needs from there).")
        env_arch = ["--platform", "android", "--archs", "arm64_v8a"]
        os.environ.setdefault("ANDROID_API_LEVEL", "24")
    src = CACHE / "wheel-src"
    src.mkdir(parents=True, exist_ok=True)
    target_dir = wheel_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    pin = parse_requirements_versions(
        ROOT / "tools" / "mobile" / spec["requirements"])["pydantic-core"]
    requirement = f"pydantic-core=={pin}"
    pkg = "pydantic-core"
    out = target_dir / pkg
    if out.is_dir() and any(out.glob("*.whl")):
        log(f"wheels    {pkg}: already built, skipping")
        return
    subprocess.run([sys.executable, "-m", "pip", "download", "--no-binary=:all:",
                    "--no-deps", "-d", str(src), requirement], check=True)
    sdist = sorted(src.glob(f"{pkg.replace('-', '_')}-*.tar.gz")) or \
        sorted(src.glob(f"{pkg}-*.tar.gz"))
    if not sdist:
        die(f"could not download the {pkg} source (pip download found nothing)")
    unpack = src / pkg
    if unpack.exists():
        shutil.rmtree(unpack)
    unpack.mkdir()
    with tarfile.open(sdist[-1]) as t:
        t.extractall(unpack)
    project = next(p for p in unpack.iterdir() if (p / "pyproject.toml").is_file())
    log(f"wheels    building {pkg} with cibuildwheel ({spec['platform_tag']})")
    subprocess.run([sys.executable, "-m", "cibuildwheel", *env_arch,
                    "--output-dir", str(target_dir), str(project)], check=True)


def parse_requirements_versions(req_file: Path) -> dict[str, str]:
    out = {}
    for line in req_file.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if "==" in line:
            name, ver = line.split("==", 1)
            out[name.strip().lower()] = ver.strip()
    return out


def stage_ios(args) -> int:
    spec = PINS["ios"]
    out: Path = args.out or spec["out"]
    req_file = ROOT / "tools" / "mobile" / spec["requirements"]
    wheels = args.wheels or (WHEELS / "ios")
    key = inputs_key("ios", spec, wheels if wheels.is_dir() else None, None,
                     args.allow_missing_required)
    python_root = out / "python"
    manifest_path = python_root / "runtime.json"
    if not args.force and up_to_date("ios", key, manifest_path):
        log(f"up to date: {out} (delete {stamp_path('ios')} or pass --force to restage)")
        return 0

    if args.build_missing_wheels:
        build_missing_wheels("ios", wheels, spec)

    runtime = extract_tar(fetch(spec["runtime"], CACHE / "runtime"), CACHE / "src-ios")
    xc = runtime / "Python.xcframework"
    if not (xc / "ios-arm64" / "Python.framework" / "Python").is_file():
        die(f"{runtime} is not the expected Python-Apple-support layout")
    ver = spec["python"]
    target = python_root / "lib" / f"python{ver}"

    log(f"staging  {python_root}")
    if python_root.exists():
        shutil.rmtree(python_root)
    target.mkdir(parents=True)
    # Standard library: the platform-independent tree, then the device slice's
    # arch-specific half (lib-dynload + _sysconfigdata) over it — the same two
    # rsyncs the support package's own install_stdlib does.
    copy_tree(xc / "lib" / f"python{ver}", target,
              exclude=STDLIB_EXCLUDE + ("libpython*.dylib",))
    copy_tree(xc / "ios-arm64" / f"lib-arm64" / f"python{ver}", target,
              exclude=STDLIB_EXCLUDE + ("libpython*.dylib",))
    pip_install(spec, req_file, target / "site-packages",
                wheels if wheels.is_dir() else None, args.allow_missing_required)
    stage_app_code(python_root / "app")

    # The framework itself, staged next to the tree so `bundle.iOS.frameworks`
    # can point at it relative to src-tauri (nothing else copies it: an
    # xcframework is code, not a resource).
    xc_dest = out / "Python.xcframework"
    if xc_dest.exists():
        shutil.rmtree(xc_dest)
    shutil.copytree(xc, xc_dest, symlinks=True)

    manifest = write_manifest(python_root, spec, target / "site-packages",
                              python_root / "app", tools=[])
    log("staged   ios: python " + manifest["python"]
        + ", " + str(len(manifest["site_packages"])) + " packages, site-packages "
        + str(sum(1 for _ in (target / 'site-packages').rglob('*') if _.is_file()))
        + " files")
    if manifest["missing_required"]:
        if not args.allow_missing_required:
            die("staged an iOS tree the backend cannot import (missing: "
                + ", ".join(manifest["missing_required"]) + "). Build the missing "
                "wheel (see the pip failure text) or pass --allow-missing-required "
                "on purpose, for the pure-client build the wizard must handle.")
        log("WARNING  missing required imports: "
            + ", ".join(manifest["missing_required"]))
    if manifest["missing_optional"]:
        log("note     not bundled on ios: " + ", ".join(manifest["missing_optional"]))
    mark_staged("ios", key)
    return 0


def elf_check(path: Path, want_machine: int = 0xB7) -> None:
    """A staged Android tool must be an AArch64 ELF, or it cannot run there."""
    head = path.read_bytes()[:20]
    if head[:4] != b"\x7fELF":
        die(f"{path.name} is not an ELF binary — did the NDK build actually run?")
    machine = struct.unpack_from("<H", head, 18)[0]
    if machine != want_machine:
        die(f"{path.name} is ELF machine 0x{machine:x}, expected 0x{want_machine:x} "
            "(AArch64) — a host build slipped into the bundle")


def stage_android(args) -> int:
    spec = PINS["android"]
    out: Path = args.out or spec["out"]
    abi = spec["abi"]
    req_file = ROOT / "tools" / "mobile" / spec["requirements"]
    wheels = args.wheels or (WHEELS / "android")
    tools_dir = args.tools
    key = inputs_key("android", spec, wheels if wheels.is_dir() else None, tools_dir,
                     args.allow_missing_required)
    python_root = out / "python"
    manifest_path = python_root / "runtime.json"
    if not args.force and up_to_date("android", key, manifest_path):
        log(f"up to date: {out} (delete {stamp_path('android')} or pass --force to restage)")
        return 0

    if args.build_missing_wheels:
        build_missing_wheels("android", wheels, spec)

    runtime = extract_tar(fetch(spec["runtime"], CACHE / "runtime"), CACHE / "src-android")
    prefix = runtime / "prefix"
    ver = spec["python"]
    if not (prefix / "lib" / f"libpython{ver}.so").is_file():
        die(f"{runtime} is not the expected Android embeddable package layout")

    log(f"staging  {out}")
    if python_root.exists():
        shutil.rmtree(python_root)
    jni = out / "jniLibs" / abi
    if jni.exists():
        shutil.rmtree(jni)
    jni.mkdir(parents=True)

    # The interpreter and every shared library it needs: the file set CPython's
    # own Android testbed copies into jniLibs (libpython*.so + the externals,
    # which are the *_python.so names).
    libdir = prefix / "lib"
    copied = []
    for entry in sorted(libdir.glob("libpython*.*.so")) + sorted(libdir.glob("lib*_python.so")):
        shutil.copy2(entry, jni / entry.name)
        copied.append(entry.name)
    log("jniLibs  " + ", ".join(copied))

    target = python_root / "lib" / f"python{ver}"
    target.mkdir(parents=True)
    copy_tree(libdir / f"python{ver}", target, exclude=STDLIB_EXCLUDE)
    pip_install(spec, req_file, target / "site-packages",
                wheels if wheels.is_dir() else None, args.allow_missing_required)
    stage_app_code(python_root / "app")

    tools: list[str] = []
    if tools_dir:
        tools_dir = Path(tools_dir)
        for name, staged in ANDROID_TOOLS.items():
            src = tools_dir / name
            if not src.is_file():
                die(f"{src} is missing — build the Android tools first:\n"
                    "  bash tools/mobile/build_android_tools.sh <out-dir>")
            elf_check(src)
            shutil.copy2(src, jni / staged)
            os.chmod(jni / staged, 0o755)
            tools.append(name)
        log("tools    " + ", ".join(tools))
    else:
        log("tools    none staged (pass --tools DIR to bundle ffmpeg/flac)")

    manifest = write_manifest(python_root, spec, target / "site-packages",
                              python_root / "app", tools)
    if manifest["missing_required"] and not args.allow_missing_required:
        die("staged an Android tree the backend cannot import (missing: "
            + ", ".join(manifest["missing_required"]) + "). Build the missing "
            "wheel with cibuildwheel (--wheels DIR --build-missing-wheels).")

    # One archive under a name AGP packages as a native library. CPython needs a
    # real filesystem path and Android assets have none, so this is the only
    # shape of the tree an app can read: the shell extracts it on first launch.
    payload = jni / "libmlopy.so"
    with open(payload, "wb") as raw:
        # mtime=0 and a sorted member order: the same inputs hash to the same
        # payload, so the shell's stamp and CI's cache both stay stable.
        import gzip
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as t:
                for file in sorted(p for p in python_root.rglob("*") if p.is_file()):
                    t.add(file, arcname=str(file.relative_to(out)))
    log(f"payload  {payload.name} ({payload.stat().st_size / 1e6:.1f} MB, "
        f"sha256 {sha256_file(payload)[:16]}…)")
    manifest["payload_sha256"] = sha256_file(payload)
    (python_root / "runtime.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                              encoding="utf-8")
    log("staged   android: python " + manifest["python"] + ", "
        + str(len(manifest["site_packages"])) + " packages, tools: "
        + (", ".join(tools) or "none"))
    if manifest["missing_optional"]:
        log("note     not bundled on android: " + ", ".join(manifest["missing_optional"]))
    mark_staged("android", key)
    return 0


def _read_payload(blob: bytes) -> dict:
    """The runtime manifest out of a staged Android payload (tar.gz in memory)."""
    import gzip
    import io
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as gz:
        with tarfile.open(fileobj=gz) as t:
            member = t.getmember("python/runtime.json")
            return json.loads(t.extractfile(member).read().decode("utf-8"))


def verify_apk(path: Path, require_tools: bool) -> int:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        libs = [n for n in names if n.startswith("lib/")]
        abis = sorted({n.split("/")[1] for n in libs if n.count("/") >= 1})
        if "arm64-v8a" not in abis:
            die(f"{path} carries no arm64-v8a native libraries (found: {abis or 'none'})")
        abi = "arm64-v8a"
        python_libs = [n for n in libs if n.startswith(f"lib/{abi}/libpython")]
        payload = f"lib/{abi}/libmlopy.so"
        if not python_libs:
            die(f"{path} carries no libpython*.so in lib/{abi}/ — the interpreter "
                "is not in the APK's native-lib directory")
        if payload not in names:
            die(f"{path} carries no {payload} — the standard library and "
                "site-packages are not in the APK")
        manifest = _read_payload(z.read(payload))
        log(f"{path.name}: libs {python_libs}")
        log(f"{path.name}: python {manifest.get('python')} "
            f"({manifest.get('runtime')}), {len(manifest.get('site_packages', []))} packages")
        if not manifest.get("backend_importable"):
            die(f"{path} carries a Python that cannot import the backend "
                f"(missing: {manifest.get('missing_required')})")
        tools = manifest.get("tools") or []
        log(f"{path.name}: bundled tools {tools or 'none'}")
        if require_tools:
            for name, staged in ANDROID_TOOLS.items():
                if name not in tools or f"lib/{abi}/{staged}" not in names:
                    die(f"{path} carries no {staged} — an Android build that "
                        "cannot run ffmpeg/flac is not the bundle we ship")
        if manifest.get("missing_optional"):
            log(f"{path.name}: not bundled {manifest['missing_optional']}")
    log("verified " + path.name)
    return 0


def verify_ipa(path: Path, require_runtime: bool) -> int:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        apps = sorted({n.split("/")[1] for n in names
                       if n.startswith("Payload/") and n.count("/") > 2})
        if not apps:
            die(f"{path} has no Payload/<app>.app — it is not an .ipa")
        app = apps[0]
        root = f"Payload/{app}"
        framework = [n for n in names if n.startswith(f"{root}/Frameworks/Python.framework/")]
        manifest_name = f"{root}/assets/mobile/python/runtime.json"
        site = [n for n in names
                if n.startswith(f"{root}/assets/mobile/python/lib/python") and "/site-packages/" in n]
        if require_runtime:
            if not framework:
                die(f"{path} does not link/embed Python.framework — check "
                    "bundle.iOS.frameworks in tauri.conf.json")
            if manifest_name not in names:
                die(f"{path} carries no {manifest_name} — bundle.resources did "
                    "not stage the Python tree into the app")
        if manifest_name in names:
            manifest = json.loads(z.read(manifest_name))
            log(f"{path.name}: python {manifest.get('python')} "
                f"({manifest.get('runtime')}), {len(manifest.get('site_packages', []))} packages")
            if not manifest.get("backend_importable"):
                log(f"{path.name}: pure client — {manifest.get('reason')}")
        log(f"{path.name}: {len(framework)} framework files, {len(site)} site-packages files")
        if site:
            tops = sorted({n.split("site-packages/")[1].split("/")[0] for n in site})
            log(f"{path.name}: site-packages top level {tops[:12]}"
                + (" …" if len(tops) > 12 else ""))
    log("verified " + path.name)
    return 0


def build_wheels(args) -> int:
    """Build the wheels PyPI does not have, without staging anything.

    Its own subcommand because a CI wheel job has no business needing the
    runtime tarballs: it downloads the pinned source, hands it to cibuildwheel,
    and uploads what comes out for the bundle jobs to consume.
    """
    spec = PINS[args.platform]
    build_missing_wheels(args.platform, args.wheels or (WHEELS / args.platform), spec)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for platform in ("ios", "android"):
        p = sub.add_parser(platform, help=f"stage the {platform} runtime")
        p.add_argument("--out", type=Path, default=None)
        p.add_argument("--wheels", type=Path, default=None,
                       help="extra wheel directory (cibuildwheel output); default: "
                            f"{WHEELS / platform}")
        p.add_argument("--build-missing-wheels", action="store_true",
                       help="build the wheels PyPI does not have, with cibuildwheel "
                            "(needs the platform toolchain: Xcode / the Android NDK)")
        p.add_argument("--force", action="store_true", help="restage even if up to date")
        p.add_argument("--allow-missing-required", action="store_true",
                       help="stage the pure-client build on purpose: the app opens "
                            "and the wizard offers a server address instead")
        if platform == "android":
            p.add_argument("--tools", type=Path, default=None,
                           help="directory with the aarch64 ffmpeg/flac built by "
                                "tools/mobile/build_android_tools.sh")
    w = sub.add_parser("wheels", help="build the wheels PyPI does not have")
    w.add_argument("platform", choices=["ios", "android"])
    w.add_argument("--wheels", type=Path, default=None,
                   help=f"where the wheel lands; default: {WHEELS}/<platform>")
    v = sub.add_parser("verify", help="assert a built artifact carries the runtime")
    v.add_argument("artifact", type=Path)
    v.add_argument("--require-tools", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "ios":
        return stage_ios(args)
    if args.command == "android":
        return stage_android(args)
    if args.command == "wheels":
        return build_wheels(args)
    name = args.artifact.name.lower()
    if name.endswith(".apk"):
        return verify_apk(args.artifact, args.require_tools)
    if name.endswith(".ipa"):
        return verify_ipa(args.artifact, require_runtime=True)
    die(f"{args.artifact} is neither an .apk nor an .ipa")


if __name__ == "__main__":
    raise SystemExit(main())
