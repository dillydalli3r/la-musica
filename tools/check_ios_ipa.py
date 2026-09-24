#!/usr/bin/env python3
"""Does the BUILT iOS app actually carry the iOS pieces? One argument: a path
to an `.ipa`, or an https URL to one (a release asset).

Why this exists: the Rust modules that give iOS a playback audio session
(`desktop/src-tauri/src/ios_audio.rs`), the Now Playing star
(`desktop/src-tauri/src/ios_like.rs`) and the `Info.plist` background-audio
declaration are compiled only inside the macOS CI job, and nothing on a Windows
or Linux desk can even `cargo check` them (the target needs Apple's SDK). "The
mobile job went green" and "the artifact the owner installs carries the fix" are
therefore two different facts, and the second one is what this checks — from the
IPA alone, with no Xcode and no device:

  * `Info.plist` — `UIBackgroundModes` containing `audio` (the app's permission
    to play in the background) and the ATS exemption the in-app browser needs.
    Both are read back out of the built `.app`, because the merge in
    `tauri-build` is what decides them, not the source file.
  * the app binary — the Objective-C names the modules use at runtime
    (`AVAudioSession`, the playback category and mode constants from AVFAudio,
    `NSNotificationCenter` for the session's lifecycle observers,
    `MPRemoteCommandCenter` for the star, and `mlo-ios-like`, the event a
    star press is handed to the webview with). These are string literals in the
    binary, so a `strip`ped release build still carries them; a module that
    silently stopped being compiled (a lost `#[cfg(target_os = "ios")]`, a
    dependency that resolved differently) does not.

Exit codes: 0 pass, 1 a check failed, 2 the argument is missing or unusable.
"""
import io
import plistlib
import re
import sys
import urllib.request
import zipfile

# The names the iOS modules look up or emit at runtime, each with the piece of
# the app it belongs to. A `str` in the app binary (they are UTF-8 literals).
NEEDLES = [
    (b"AVAudioSession", "the audio session class (ios_audio.rs)"),
    (b"AVAudioSessionCategoryPlayback", "the playback category (ios_audio.rs)"),
    (b"AVAudioSessionModeDefault", "the default mode (ios_audio.rs)"),
    (b"NSNotificationCenter", "the session's lifecycle observers (ios_audio.rs)"),
    (b"MPRemoteCommandCenter", "the Now Playing command centre (ios_like.rs)"),
    (b"mlo-ios-like", "the star-press event (ios_like.rs / iosFavs.ts)"),
]

# Enough of the app to prove this is the app and not an empty bundle.
BUILD_MIN_BYTES = 1_000_000


def fail(message: str) -> None:
    print(f"  FAIL {message}")
    sys.exit(1)


def load(source: str) -> bytes:
    if re.match(r"^https?://", source):
        print(f"fetching {source}")
        with urllib.request.urlopen(source, timeout=120) as response:  # noqa: S310
            return response.read()
    with open(source, "rb") as handle:
        return handle.read()


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    try:
        blob = load(sys.argv[1])
    except OSError as error:
        print(f"cannot read {sys.argv[1]}: {error}")
        sys.exit(2)
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        print("not a zip/ipa")
        sys.exit(2)

    appdirs = sorted({n.split("/")[1] for n in archive.namelist()
                      if n.startswith("Payload/") and n.count("/") > 1})
    if len(appdirs) != 1:
        fail(f"expected exactly one Payload/*.app, found {appdirs}")
    app = f"Payload/{appdirs[0]}"
    print(f"app: {appdirs[0]}")

    info = [n for n in archive.namelist() if re.fullmatch(rf"{re.escape(app)}/Info\.plist", n)]
    if not info:
        fail("the app has no Info.plist")
    plist = plistlib.loads(archive.read(info[0]))
    print(f"CFBundleShortVersionString: {plist.get('CFBundleShortVersionString')}")

    modes = plist.get("UIBackgroundModes") or []
    if "audio" not in modes:
        fail(f"UIBackgroundModes does not declare audio (got {modes})")
    print("ok   UIBackgroundModes carries audio")
    if not (plist.get("NSAppTransportSecurity") or {}).get("NSAllowsArbitraryLoadsInWebContent"):
        fail("NSAppTransportSecurity.NSAllowsArbitraryLoadsInWebContent is missing")
    print("ok   the webview's ATS exemption is declared")

    binaries = [n for n in archive.namelist()
                if re.fullmatch(rf"{re.escape(app)}/[^/]+", n) and archive.getinfo(n).file_size > BUILD_MIN_BYTES]
    if not binaries:
        fail(f"no executable-looking file over {BUILD_MIN_BYTES} bytes in {app}")
    main_binary = max(binaries, key=lambda n: archive.getinfo(n).file_size)
    image = archive.read(main_binary)
    print(f"binary: {main_binary.rsplit('/', 1)[-1]} ({len(image)} bytes)")

    for needle, what in NEEDLES:
        if needle not in image:
            fail(f"{needle.decode()} is not in the app binary — {what} did not ship")
        print(f"ok   {needle.decode()} — {what}")

    print(f"\nall good: this IPA carries the iOS audio session, the Now Playing star")


if __name__ == "__main__":
    main()
