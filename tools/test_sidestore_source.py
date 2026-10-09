#!/usr/bin/env python3
"""The SideStore/AltStore source this repo publishes.

The release workflow writes `source.json` with tools/make_sidestore_source.py
and attaches it to the release; a sideloading tool reads it from the stable url
`releases/latest/download/source.json`. When a key the tool's decoder requires
is missing, the import fails with a Swift decoding error and nothing useful:

    Decoding failed: Key 'date' not found. No value associated with key CodingKeys

That is the exact failure the owner hit on their iPhone (2026-09-24), and it
came from this file: the version entry had no `date` at all (and the app's
`category` was "music", which AltStore's closed enum does not contain — the
next decode failure waiting behind the first).

Everything AltStore's own documentation names required is asserted here, so the
published file cannot regress to that state. The same IPA is then handed to
tools/check_ios_ipa.py — a synthetic one this time, built from that tool's own
needle list — and every invariant the tool asserts (each Info.plist key, each
Objective-C name in the binary) is deleted on its own, because an assertion that
cannot fail is decoration rather than a check. Offline: the "IPA" is a file of a
known size or a zip this file writes, and nothing is fetched.

Run:  python tools/test_sidestore_source.py
"""
import datetime
import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAIL = 0


def check(label, ok, detail=""):
    global FAIL
    if not ok:
        FAIL += 1
    print(f"{'ok  ' if ok else 'FAIL'} {label}{'' if ok else ' — ' + str(detail)}")


def _generator():
    """tools/make_sidestore_source.py, loaded as a module (it is a script)."""
    path = os.path.join(ROOT, "tools", "make_sidestore_source.py")
    spec = importlib.util.spec_from_file_location("make_sidestore_source", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The categories AltStore accepts, from its own documentation. A value outside
# this set (our old "music" among them) is decoded into an enum and throws.
CATEGORIES = {"developer", "entertainment", "games", "lifestyle", "other",
              "photo-video", "social", "utilities"}

gen = _generator()
TAG = "v9.9.9"
VERSION = "9.9.9"
SIZE = 4321

work = tempfile.mkdtemp(prefix="sidestore-")
ipa = os.path.join(work, f"la-musica_{VERSION}_ios-unsigned.ipa")
with open(ipa, "wb") as fh:
    fh.write(b"\0" * SIZE)

print("== the source the release workflow would publish ==")
source = gen.build(ipa, VERSION, TAG)

check("it is JSON-serialisable", json.dumps(source) != "", type(source))
check("the source names itself", bool(source.get("name")), source.get("name"))
check("the source carries an identifier", bool(source.get("identifier")),
      source.get("identifier"))
check("it lists at least one app", bool(source.get("apps")), source.get("apps"))
check("news is a list (AltStore reads it even when empty)",
      isinstance(source.get("news"), list), type(source.get("news")).__name__)

app = (source.get("apps") or [{}])[0]
for key in ("name", "bundleIdentifier", "developerName", "localizedDescription"):
    check(f"the app states {key}", bool(app.get(key)), app.get(key))
check("the app's icon is an https url",
      str(app.get("iconURL", "")).startswith("https://"), app.get("iconURL"))
check("the app declares a valid AltStore category",
      app.get("category") in CATEGORIES,
      f"{app.get('category')!r} not in {sorted(CATEGORIES)}")

print("\n== the version entry: every key the decoder requires ==")
versions = app.get("versions") or []
check("there is a version entry", len(versions) == 1, len(versions))
entry = versions[0] if versions else {}

check("version", entry.get("version") == VERSION, entry.get("version"))
check("buildVersion", entry.get("buildVersion") == VERSION, entry.get("buildVersion"))

# The key that actually broke the owner's import.
check("date is PRESENT (SideStore: \"Key 'date' not found\")",
      "date" in entry and bool(entry.get("date")), entry.get("date"))
try:
    datetime.datetime.fromisoformat(str(entry.get("date", "")).replace("Z", "+00:00"))
    parsed = True
except ValueError:
    parsed = False
check("date parses as ISO 8601", parsed, entry.get("date"))

check("downloadURL points at this tag's asset",
      entry.get("downloadURL") ==
      f"https://github.com/{gen.REPO}/releases/download/{TAG}/"
      f"la-musica_{VERSION}_ios-unsigned.ipa",
      entry.get("downloadURL"))
check("size is the IPA's own size", entry.get("size") == SIZE, entry.get("size"))
check("minOSVersion is stated (the app's own floor)",
      entry.get("minOSVersion") == gen.MIN_OS,
      f"{entry.get('minOSVersion')!r} vs {gen.MIN_OS!r}")
check("localizedDescription", bool(entry.get("localizedDescription")),
      entry.get("localizedDescription"))

print("\n== the release date ==")
today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
check("without --date it is today, UTC (the run's own day)",
      str(entry.get("date", "")).startswith(today), f"{entry.get('date')} vs {today}")
stamped = gen.build(ipa, VERSION, TAG, date="2026-09-24T12:46:30Z")
check("--date is honoured verbatim",
      stamped["apps"][0]["versions"][0]["date"] == "2026-09-24T12:46:30Z",
      stamped["apps"][0]["versions"][0].get("date"))

# --------------------------------------------------------------------------- #
# tools/check_ios_ipa.py — the SHIPPED IPA carries the iOS pieces.
#
# That tool needs a built .ipa, which only the macOS CI job can produce, so it
# is exercised here against a synthetic one with the same shape: `Payload/*.app`
# holding an Info.plist and an executable. The binary is assembled from the
# tool's OWN needle list, so a needle added there is covered here without a
# second edit — and then every invariant is deleted from the fixture, one at a
# time, and the tool has to fail on each. That is the whole point of the check:
# an assertion that cannot fail is decoration (issue #58 is the background-audio
# mode, the plain-http exemption and the like/command-centre symbols).
# --------------------------------------------------------------------------- #
IPA_TOOL = os.path.join(ROOT, "tools", "check_ios_ipa.py")


def _ipa_tool():
    """tools/check_ios_ipa.py, loaded as a module (it is a script)."""
    spec = importlib.util.spec_from_file_location("check_ios_ipa", IPA_TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ipa_check = _ipa_tool()
# The executable: comfortably over the tool's "this is the app" floor, and
# carrying every name it looks for — string literals, exactly as a `strip`ped
# release build carries them.
PAD = b"\0" * (ipa_check.BUILD_MIN_BYTES + 1000)
APP_PLIST = {
    "CFBundleShortVersionString": VERSION,
    "UIBackgroundModes": ["audio"],
    "NSAppTransportSecurity": {
        "NSAllowsArbitraryLoadsInWebContent": True,
        "NSAllowsArbitraryLoadsForMedia": True,
        "NSAllowsArbitraryLoads": True,
    },
    "NSLocalNetworkUsageDescription": "la musica connects to the la musica server you run.",
}


def _binary(omit=None):
    """The fixture's executable: every needle but `omit`.

    A needle that CONTAINS the omitted one is blanked rather than kept, because
    there is no such artifact as one without the other: `likeCommand` lives
    inside `dislikeCommand`'s spelling, and `AVAudioSession` inside the
    category/mode constant names. The tool must fail either way, which is what
    is asserted — the mutation only has to be honest about the artifact.
    """
    parts = []
    for needle, _ in ipa_check.NEEDLES:
        if needle == omit:
            continue
        parts.append(b"X" * len(needle) if omit and omit in needle else needle)
    return PAD + b"".join(parts)


def _write_ipa(path, plist=None, binary=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Payload/la-musica.app/Info.plist",
                   plistlib.dumps(APP_PLIST if plist is None else plist))
        z.writestr("Payload/la-musica.app/la-musica",
                   _binary() if binary is None else binary)
    return path


def _run_ipa_check(path):
    return subprocess.run([sys.executable, IPA_TOOL, path],
                          capture_output=True, text=True)


print("\n== the shipped IPA carries the iOS pieces ==")
good_ipa = _write_ipa(os.path.join(work, "synthetic-good.ipa"))
good = _run_ipa_check(good_ipa)
check("a build carrying every iOS piece passes",
      good.returncode == 0, (good.stdout or good.stderr)[-400:])

# The file the SideStore part above wrote is not a zip: the tool's own "this
# argument is unusable" code, distinct from "an invariant failed".
check("an unusable argument exits 2 (not 1)",
      _run_ipa_check(ipa).returncode == 2, _run_ipa_check(ipa).stdout[-200:])
check("no argument at all exits 2",
      subprocess.run([sys.executable, IPA_TOOL], capture_output=True, text=True).returncode == 2)

# Every plist invariant, deleted on its own. The exact key the issue is about:
# NSAllowsArbitraryLoadsForMedia is what covers an <audio> element's bytes, and
# on iOS 10+ the blanket key is ignored while a scoped one is present, so a
# build without it looks complete and refuses every track.
NO_MEDIA = {"CFBundleShortVersionString": VERSION,
            "UIBackgroundModes": ["audio"],
            "NSAppTransportSecurity": {
                "NSAllowsArbitraryLoadsInWebContent": True,
                "NSAllowsArbitraryLoads": True,
            },
            "NSLocalNetworkUsageDescription": "la musica connects to the la musica server you run."}
NO_BLANKET = {"CFBundleShortVersionString": VERSION,
              "UIBackgroundModes": ["audio"],
              "NSAppTransportSecurity": {
                  "NSAllowsArbitraryLoadsInWebContent": True,
                  "NSAllowsArbitraryLoadsForMedia": True,
              },
              "NSLocalNetworkUsageDescription": "la musica connects to the la musica server you run."}
NO_WEB = {"CFBundleShortVersionString": VERSION,
          "UIBackgroundModes": ["audio"],
          "NSAppTransportSecurity": {
              "NSAllowsArbitraryLoadsForMedia": True,
              "NSAllowsArbitraryLoads": True,
          },
          "NSLocalNetworkUsageDescription": "la musica connects to the la musica server you run."}
NO_LAN = dict(APP_PLIST, NSLocalNetworkUsageDescription="   ")
NO_AUDIO = dict(APP_PLIST, UIBackgroundModes=["fetch"])
NO_MODES = dict(APP_PLIST)
del NO_MODES["UIBackgroundModes"]

PLIST_MUTATIONS = [
    ("UIBackgroundModes missing", NO_MODES),
    ("UIBackgroundModes without audio", NO_AUDIO),
    ("NSAllowsArbitraryLoadsInWebContent missing", NO_WEB),
    ("NSAllowsArbitraryLoadsForMedia missing (the one that covers a track)", NO_MEDIA),
    ("NSAllowsArbitraryLoads missing", NO_BLANKET),
    ("NSLocalNetworkUsageDescription blank", NO_LAN),
]
for label, plist in PLIST_MUTATIONS:
    path = _write_ipa(os.path.join(work, "plist-" + str(len(label)) + ".ipa"), plist=plist)
    run = _run_ipa_check(path)
    check(f"deleting {label} fails the check", run.returncode == 1,
          f"exit {run.returncode}: {(run.stdout or '').strip()[-200:]}")

# …and every name in the binary, the like/command-centre ones included.
for needle, what in ipa_check.NEEDLES:
    name = needle.decode().strip(":").replace("/", "_").replace(" ", "_")
    path = _write_ipa(os.path.join(work, f"bin-{name}.ipa"), binary=_binary(omit=needle))
    run = _run_ipa_check(path)
    check(f"deleting {needle.decode()} ({what}) fails the check", run.returncode == 1,
          f"exit {run.returncode}: {(run.stdout or '').strip()[-160:]}")

print()
if FAIL:
    print(f"sidestore source: {FAIL} check(s) failed")
    raise SystemExit(1)
print("sidestore source: all assertions passed")
