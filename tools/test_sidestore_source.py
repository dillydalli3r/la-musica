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
published file cannot regress to that state. Offline: the "IPA" is a file of a
known size and nothing is fetched.

Run:  python tools/test_sidestore_source.py
"""
import datetime
import importlib.util
import json
import os
import tempfile

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

print()
if FAIL:
    print(f"sidestore source: {FAIL} check(s) failed")
    raise SystemExit(1)
print("sidestore source: all assertions passed")
