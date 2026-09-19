#!/usr/bin/env python3
"""Wire the bundled Python runtime and the keep-alive service into Tauri's
generated Android project.

`desktop/src-tauri/gen/` is generated, never committed (see desktop/README.md),
so everything an Android build needs beyond the template has to be applied to it
after `tauri android init` and before `tauri android build`. This is that step —
one script, idempotent, and it fails loudly on every anchor it does not find, so
a Tauri CLI update shows up as a clear error instead of a silently different app.

What it does, and why each part cannot live in the repository:

1. Copies the staged native libraries (`libpython3.14.so`, its `*_python.so`
   externals, the payload `libmlopy.so` and the bundled tools) into
   `app/src/main/jniLibs/arm64-v8a/` — the only place on Android that is both
   packaged and executable at runtime.
2. Pins the build to arm64-v8a (`abiList`/`targetList` in gradle.properties):
   a universal APK would claim four ABIs and carry a Python for one, and the
   other three would fail to link against it.
3. Turns on `jniLibs.useLegacyPackaging`, i.e. `extractNativeLibs`: without it
   AGP loads the libraries straight out of the APK, where they have no
   filesystem path — and both the Python standard library extraction and the
   exec of a bundled ffmpeg need a real path.
4. Adds the foreground-service permissions and the service element for
   `tools/mobile/android/MloKeepAlive.kt`, which keeps the process (and so the
   embedded backend) alive while the app is in the background.
5. Copies that Kotlin file into the app's package and calls it from
   `MainActivity.onCreate`, so the service follows the shell's own setting.

Usage:
    python tools/mobile/android/inject.py --project desktop/src-tauri/gen/android
        [--jni-libs desktop/src-tauri/resources/mobile/android/jniLibs/arm64-v8a]
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

PERMISSIONS = [
    "android.permission.FOREGROUND_SERVICE",
    "android.permission.FOREGROUND_SERVICE_SPECIAL_USE",
    "android.permission.POST_NOTIFICATIONS",
    "android.permission.WAKE_LOCK",
]

SERVICE = """
        <!-- Keep-alive for the embedded backend: a foreground service is the
             only thing that stops Android from killing this process (and the
             Python serving inside it) once the user switches away. The type is
             specialUse because hosting a library for other devices, P2P
             transfers and long imports is none of the specific ones; Android
             14+ requires a type, and the substring below is what review reads.
             Injected by tools/mobile/android/inject.py. -->
        <service
            android:name=".MloKeepAliveService"
            android:exported="false"
            android:foregroundServiceType="specialUse">
            <property
                android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"
                android:value="local_backend_host" />
        </service>
"""

GRADLE_PACKAGING = """
    packaging {
        // Native libraries must be extracted to disk: the bundled Python is
        // unpacked from one of them and the bundled ffmpeg/flac are executed
        // from here, and neither works against a library inside the APK zip.
        jniLibs {
            useLegacyPackaging = true
        }
    }
"""


def log(msg: str) -> None:
    print(f"[inject] {msg}", flush=True)


def die(msg: str) -> "NoReturn":  # noqa: F821
    print(f"[inject] ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def find_main_activity(app: Path) -> Path:
    hits = sorted((app / "src/main").rglob("MainActivity.kt"))
    if not hits:
        die(f"no MainActivity.kt under {app}/src/main — did `tauri android init` run?")
    if len(hits) > 1:
        die(f"several MainActivity.kt files found: {hits}")
    return hits[0]


def patch_jni_libs(app: Path, jni_libs: Path | None) -> None:
    if not jni_libs:
        log("jniLibs  skipped (no --jni-libs given): the APK would carry no Python")
        return
    payload = jni_libs / "libmlopy.so"
    if not payload.is_file():
        die(f"{payload} is missing — stage the Android runtime first: "
            "python tools/mobile/bundle.py android")
    dest = app / "src/main/jniLibs/arm64-v8a"
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for entry in sorted(jni_libs.iterdir()):
        if entry.is_file() and entry.name.endswith(".so"):
            shutil.copy2(entry, dest / entry.name)
            copied.append(entry.name)
    log(f"jniLibs  {len(copied)} files into {dest}: {', '.join(copied)}")


def patch_gradle_properties(project: Path) -> None:
    path = project / "gradle.properties"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    wanted = {
        # One ABI, the one the bundled Python targets.
        "abiList": "arm64-v8a",
        "targetList": "aarch64-linux-android",
    }
    if all(re.search(rf"^{key}={re.escape(value)}$", text, re.M) for key, value in wanted.items()):
        log("gradle   abiList/targetList already pinned")
        return
    lines = [line for line in text.splitlines()
             if not re.match(r"^(abiList|targetList)=", line)]
    lines += ["", "# Pinned by tools/mobile/android/inject.py: the bundled Python runtime",
              "# exists for arm64-v8a only, so a universal APK would be a lie."]
    lines += [f"{key}={value}" for key, value in wanted.items()]
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    log(f"gradle   pinned abiList/targetList in {path.name}")


def patch_gradle_packaging(app: Path) -> None:
    path = app / "build.gradle.kts"
    if not path.is_file():
        die(f"{path} does not exist — did `tauri android init` run?")
    text = path.read_text(encoding="utf-8")
    if "useLegacyPackaging" in text:
        log("gradle   useLegacyPackaging already set")
        return
    anchor = "    buildFeatures {\n        buildConfig = true\n    }\n"
    if text.count(anchor) != 1:
        die(f"{path}: expected exactly one `buildFeatures {{ buildConfig = true }}` "
            f"block to anchor the packaging switch on, found {text.count(anchor)} — "
            "the Tauri template changed, update inject.py")
    path.write_text(text.replace(anchor, anchor + GRADLE_PACKAGING), encoding="utf-8")
    log("gradle   jniLibs.useLegacyPackaging = true (libs are extracted to disk)")


def patch_manifest(app: Path) -> None:
    path = app / "src/main/AndroidManifest.xml"
    if not path.is_file():
        die(f"{path} does not exist — did `tauri android init` run?")
    text = path.read_text(encoding="utf-8")
    changed = False
    for permission in PERMISSIONS:
        if f'android:name="{permission}"' in text:
            continue
        text = text.replace(
            '    <uses-permission android:name="android.permission.INTERNET" />',
            '    <uses-permission android:name="android.permission.INTERNET" />\n'
            f'    <uses-permission android:name="{permission}" />',
            1,
        )
        if f'android:name="{permission}"' not in text:
            die(f"{path}: could not add {permission} — is the INTERNET permission "
                "still the first line of the manifest?")
        changed = True
    if 'android:name=".MloKeepAliveService"' not in text:
        if "\n    </application>" not in text:
            die(f"{path}: no `</application>` to insert the keep-alive service before")
        text = text.replace("\n    </application>", SERVICE + "\n    </application>", 1)
        changed = True
    if changed:
        path.write_text(text, encoding="utf-8")
        log("manifest permissions + MloKeepAliveService")
    else:
        log("manifest already patched")


def patch_main_activity(app: Path) -> Path:
    activity = find_main_activity(app)
    text = activity.read_text(encoding="utf-8")
    package = re.search(r"^package\s+([\w.]+)", text, re.M)
    if not package:
        die(f"{activity}: no `package` line to put the service in")
    dest = activity.parent / "MloKeepAlive.kt"
    template = (HERE / "MloKeepAlive.kt").read_text(encoding="utf-8")
    rendered = template.replace("package __MLO_PACKAGE__", f"package {package.group(1)}")
    if not dest.is_file() or dest.read_text(encoding="utf-8") != rendered:
        dest.write_text(rendered, encoding="utf-8")
        log(f"kotlin   wrote {dest.relative_to(app)} (package {package.group(1)})")

    if "MloKeepAliveService.sync" in text:
        log("kotlin   MainActivity already syncs the keep-alive service")
        return activity
    anchor = "super.onCreate(savedInstanceState)"
    if text.count(anchor) != 1:
        die(f"{activity}: expected exactly one `{anchor}` to hook, found "
            f"{text.count(anchor)} — the Tauri template changed, update inject.py")
    text = text.replace(
        anchor,
        anchor + "\n    // Follows the shell's keep-alive setting (Rust owns the marker\n"
        "    // file): a foreground service is what keeps this process — and the\n"
        "    // Python backend inside it — alive while the app is in the background.\n"
        "    MloKeepAliveService.sync(this)",
        1,
    )
    activity.write_text(text, encoding="utf-8")
    log(f"kotlin   MainActivity.onCreate -> MloKeepAliveService.sync")
    return activity


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--project", type=Path, required=True,
                        help="the generated project, i.e. desktop/src-tauri/gen/android")
    parser.add_argument("--jni-libs", type=Path, default=None,
                        help="staged native libraries (resources/mobile/android/jniLibs/arm64-v8a)")
    args = parser.parse_args(argv)

    project = args.project
    app = project / "app"
    if not app.is_dir():
        die(f"{app} does not exist — run `tauri android init` first")
    patch_jni_libs(app, args.jni_libs)
    patch_gradle_properties(project)
    patch_gradle_packaging(app)
    patch_manifest(app)
    patch_main_activity(app)
    log("done: the generated Android project now carries the runtime and the keep-alive")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
