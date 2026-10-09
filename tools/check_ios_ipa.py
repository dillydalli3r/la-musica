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
    to play in the background), the ATS exemptions the page AND the media loader
    need (`NSAllowsArbitraryLoadsInWebContent`, `NSAllowsArbitraryLoadsForMedia`
    and the blanket `NSAllowsArbitraryLoads`), and the local-network prompt's
    reason (`NSLocalNetworkUsageDescription`, without which a server on the LAN
    can never be granted access on iOS 14+). All of them are read back out of the
    built `.app`, because the merge in `tauri-build` is what decides them, not
    the source file.

    The MEDIA key is the one to be careful with: on iOS 10+ Apple IGNORES
    `NSAllowsArbitraryLoads` as soon as any scoped key is present, so a build
    carrying the web-content key alone ships a media exemption of NO while
    looking, in the source, exactly like one of YES — the page loads and every
    track is refused ("pressing play just pauses it immediately", issue #58).
  * the app binary — the Objective-C names the modules use at runtime
    (`AVAudioSession`, the playback category and mode constants from AVFAudio,
    `AVAudioPlayer` for the background keep-alive, `NSNotificationCenter` for
    the session's lifecycle observers, and `MPRemoteCommandCenter` / `likeCommand`
    / `dislikeCommand` / `addTargetWithHandler:` / `sharedCommandCenter` for the
    Now Playing star, `mlo-ios-like`, the event a star press is handed to the
    webview with, and the readout row names the two modules publish
    (`now_playing_like_enabled`, `now_playing_like_active`,
    `now_playing_like_web_state`, `now_playing_like_press_pending`,
    `now_playing_skip_forward_enabled`, `session_activate_last`,
    `keep_alive_platform_stops`, `app_heartbeat`)). These are string literals in
    the binary, so a `strip`ped release build still carries them; a module that
    silently stopped being compiled (a lost `#[cfg(target_os = "ios")]`, a
    dependency that resolved differently) does not.

Each needle is a piece of the module that produces it, and every one of them is
also stated as an app-level truth in `desktop/README.md` ("The audio session on
iOS" / "The Now Playing star on iOS"): the tool asserts the ARTIFACT, the README
is what the assertion is answerable to. `tools/test_sidestore_source.py` builds
a synthetic IPA and proves both directions — this tool passes a build that
carries them and fails a build that has had any one of them deleted.

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
    (b"AVAudioSessionMediaServicesWereResetNotification",
     "the media-server-reset observer (ios_audio.rs)"),
    (b"AVAudioPlayer", "the background keep-alive player (ios_audio.rs)"),
    (b"NSTimer", "the app-process heartbeat behind the readout (ios_audio.rs)"),
    (b"NSNotificationCenter", "the session's lifecycle observers (ios_audio.rs)"),
    (b"session_activate_last",
     "the session's own activation answer in the readout (ios_audio.rs)"),
    (b"keep_alive_platform_stops",
     "the platform-teardown count in the readout (ios_audio.rs)"),
    (b"app_heartbeat",
     "whether the app-process heartbeat is ticking (ios_audio.rs)"),
    (b"MPRemoteCommandCenter", "the Now Playing command centre (ios_like.rs)"),
    (b"sharedCommandCenter", "the command centre singleton (ios_like.rs)"),
    (b"likeCommand", "the Now Playing star's command (ios_like.rs)"),
    (b"dislikeCommand", "the pinned-inactive sibling command (ios_like.rs)"),
    (b"addTargetWithHandler:", "the star's press handler (ios_like.rs)"),
    (b"mlo-ios-like", "the star-press event (ios_like.rs / iosFavs.ts)"),
    (b"skipForwardCommand", "the skip pair the readout measures (ios_like.rs)"),
    (b"now_playing_like_enabled", "the Now Playing readout rows (ios_like.rs)"),
    (b"now_playing_like_active",
     "the star's fill in the readout (ios_like.rs)"),
    (b"now_playing_like_web_state",
     "the state the web last pushed (ios_like.rs)"),
    (b"now_playing_like_press_pending",
     "a press still owed to the webview (ios_like.rs)"),
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
    # The MEDIA key, which is the one a track needs and the one a build is most
    # likely to be missing without any other symptom: on iOS 10+ the blanket
    # NSAllowsArbitraryLoads is IGNORED the moment a scoped key is present, so
    # the web-content key alone leaves AVFoundation's media loads blocked — the
    # page loads, every track is refused, and "pressing play just pauses it
    # immediately" is what that looks like from the app.
    if not (plist.get("NSAppTransportSecurity") or {}).get("NSAllowsArbitraryLoadsForMedia"):
        fail("NSAppTransportSecurity.NSAllowsArbitraryLoadsForMedia is missing — on iOS 10+ "
             "the blanket key is ignored while a scoped one is present, so the media "
             "loader would be held to full ATS and every plain-http track refused")
    print("ok   the media loader's ATS exemption is declared (audio/video loads)")
    # …and the blanket key, which is the coarse exemption the two scoped keys
    # above replace on every OS this app runs on. It is asserted because the
    # file states it deliberately (and merges into the macOS .app too), not
    # because a track reads it: on iOS 10+ it is ignored while either scoped key
    # is present, which is exactly the trap the media key above closes.
    if not (plist.get("NSAppTransportSecurity") or {}).get("NSAllowsArbitraryLoads"):
        fail("NSAppTransportSecurity.NSAllowsArbitraryLoads is missing — the coarse "
             "exemption the plist states for systems that read no scoped key")
    print("ok   the blanket ATS exemption is declared (pre-iOS-10 shape)")
    # iOS 14+ needs a declared reason before the local-network permission can be
    # requested at all; without it a server on the LAN is unreachable.
    if not str(plist.get("NSLocalNetworkUsageDescription") or "").strip():
        fail("NSLocalNetworkUsageDescription is empty — a LAN server could never be "
             "granted local-network access")
    print("ok   the local-network prompt has a reason to show")

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

    print(f"\nall good: this IPA carries the iOS audio session (and its background "
          f"keep-alive), the plain-http exemptions the page AND the media loader "
          f"need, and the Now Playing star's like command")


if __name__ == "__main__":
    main()
