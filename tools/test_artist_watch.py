#!/usr/bin/env python3
"""Verify artist watching: an artist's NEW releases only, one capped cycle.

No network at all. The three remote seams are stubbed — MusicBrainz (the
release-group browse, the edition picker, the identity lookup), the Cover Art
Archive (the art cache's own fetch seam) and the wish worker's search trigger —
while everything that decides what may be downloaded is the real code: the
policy, the store, the cycle, the API routes, and `pending_albums.create`, the
same "Add to library" helper the route uses. The music folder and the watch
database are temp files; the developer's library is never opened.

Pinned here, one case each:
  * creating a watch queues NOTHING (whatever its policy);
  * a release group dated before the watch is ignored, one dated after is
    queued, and an UNDATED group is never "new";
  * the type filter (primary OR secondary, incl. "live") is honoured;
  * one cycle never queues more than `max_per_cycle`, even with ten candidates;
  * the same release is never queued twice across cycles;
  * `auto_add: false` notifies without queueing;
  * disabling a watch stops its checks;
  * `backfill` queues only what the library LACKS;
  * `include` is an allow-list and `exclude` a veto that outranks both it and
    backfill; an unknown type name is rejected at write time.

Run: python tools/test_artist_watch.py  (exit 0 pass, 1 fail)
"""
import atexit
import datetime
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: the app's paths resolve through the music folder the moment they
# are first touched, so the scope is redirected BEFORE server.main is imported.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-watch-redirect-")
MF = tempfile.mkdtemp(prefix="mlo-watch-test-")
os.environ["MLO_MUSIC_FOLDER"] = MF

import mlo  # noqa: E402
import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as f:
    json.dump({"music_folder": MF}, f)
for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")

atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True)
                         for d in (REDIRECT, MF)])
atexit.register(lambda: os.environ.pop("MLO_MUSIC_FOLDER", None))

_REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/").lower()
assert not MF.replace("\\", "/").lower().startswith(_REAL or "\0"), \
    f"temp fixture {MF} sits inside the real music folder {REAL_MUSIC_FOLDER}"

# The redirect is deliberate here, so mlo.paths' "you are pointing at a temp
# folder" warning is pure noise on every config read this test makes.
pathmod._warn_if_temp_folder = lambda mf: None

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from mlo.config import load_config  # noqa: E402
from server import artcache, artist_watch, artist_watch_worker, events  # noqa: E402
from server import api_watch, integrations, pending_albums, wishes  # noqa: E402

# The router line the coordinator registers in server/main.py:
#     app.include_router(api_watch.router)
# The test mounts it on its own app so it does not depend on that line (or on
# whatever else is landing in main.py) — the routes are the same either way.
APP = FastAPI()
APP.include_router(api_watch.router)
CLIENT = TestClient(APP)

FAILED = []


def ok(cond, label, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{f' — {extra}' if extra else ''}")
        FAILED.append(label)


def eq(got, want, label, extra=""):
    ok(got == want, label, extra or f"got {got!r}, want {want!r}")


# --------------------------------------------------------------------------- #
# stubs: the network's seams, and the shape of a MusicBrainz answer
# --------------------------------------------------------------------------- #
def _uuid(n, part):
    return f"{n:08d}-{part * 4}-{part * 4}-{part * 4}-{part * 12}"


def rg_id(n):
    return _uuid(n, "2")


def rel_id(n):
    return _uuid(n, "1")


def artist_id(n=1):
    return _uuid(n, "3")


ARTIST = artist_id(1)
ARTIST_NAME = "Test Artist"

print("\nstubs")
print("  ...ok" if ARTIST else "")

# What the artist's discography "is" this run: browse rows plus the release
# each group resolves to.
GROUPS = {}       # release-group id -> {title, primary_type, secondary_types, date}
RELEASES = {}     # release id -> release dict (what pending_albums.create gets)
BY_GROUP = {}     # release-group id -> its release dict
browse_calls = []
identity_error = None
owned = {}        # the library's own holdings, as wishes.owned_mbids reports them
triggers = []     # wish ids handed to the wish worker
events_seen = []  # every event emitted while the test runs


def add_group(n, title, date, primary="album", secondary=None):
    GROUPS[rg_id(n)] = {"id": rg_id(n), "title": title,
                        "primary_type": primary,
                        "secondary_types": secondary or [],
                        "first_release_date": date}
    RELEASES[rel_id(n)] = {
        "id": rel_id(n), "title": title, "date": date, "originaldate": date[:4],
        "country": "GB", "status": "Official", "medium": "CD",
        "label": "Test Label", "catalog_number": f"CAT-{n}",
        "release_group_id": rg_id(n), "release_type": primary,
        "primary_type": primary.capitalize(), "secondary_types": secondary or [],
        "artists": [{"name": ARTIST_NAME, "mbid": ARTIST}],
        "medium_count": 1,
        "media": [{"disc": 1, "position": 1, "title": "One",
                   "recording_mbid": "44444444-4444-4444-4444-444444444444",
                   "artist_credit": ARTIST_NAME},
                  {"disc": 1, "position": 2, "title": "Two",
                   "recording_mbid": "55555555-5555-5555-5555-555555555555",
                   "artist_credit": ARTIST_NAME}]}
    BY_GROUP[rg_id(n)] = RELEASES[rel_id(n)]


def fake_browse(mbid, limit=100, offset=0, primary_type="", secondary_type=""):
    browse_calls.append(str(mbid))
    rows = list(GROUPS.values())
    return {"total": len(rows), "offset": 0, "next": None, "release_groups": rows}


def fake_identity(mbid):
    if identity_error:
        raise identity_error
    if str(mbid).lower() != ARTIST:
        return {}
    return {"id": ARTIST, "name": ARTIST_NAME, "type": "Group", "country": "GB"}


def fake_group_targets(rg_mbid, mode="best", **kw):
    # `types` is the watch's own release-group type filter, now handed INTO the
    # choice (integrations.group_targets); this fixture's groups are the ones
    # the watch already accepted, so the filter has nothing left to refuse.
    rel = BY_GROUP.get(str(rg_mbid).lower())
    if not rel:
        return [], "no eligible edition"
    return [{"mbid": rel["id"], "title": rel["title"],
             "score": 1.0, "reasons": ["official release"]}], None


def fake_resolve_release(mbid):
    rid = str(mbid or "").lower()
    return RELEASES.get(rid), rid


def fake_fetch_art(url, **kwargs):
    return b"\xff\xd8\xff" + b"placeholder-cover" * 8, "image/jpeg", "coverartarchive"


integrations.artist_release_groups = fake_browse
integrations.artist_identity = fake_identity
integrations.group_targets = fake_group_targets
integrations.resolve_release = fake_resolve_release
integrations.MusicBrainzError = integrations.MusicBrainzError  # keep the type
artcache.fetch_art = fake_fetch_art
wishes.owned_mbids = lambda cfg=None: dict(owned)


def fake_trigger(wid=None):
    triggers.append(wid)
    return {"ok": True}


def fake_emit(kind, title, body="", data=None, config=None):
    events_seen.append({"event": kind, "title": title, "body": body,
                        "data": data or {}})
    return {"event": kind}


import server.wishes_worker as wishes_worker  # noqa: E402

wishes_worker.trigger = fake_trigger

prefetched = []   # folders the watch asked to fill in AFTER queuing them


def fake_prefetch(folder, cfg=None, **kw):
    """The add-time page content (artist artwork, descriptions, ranked cover
    candidates) reaches the providers, and this suite's contract is "no network
    at all" — so it is recorded here and skipped. The exercise that matters is
    that the watch ASKS for it in the background instead of blocking its own
    cycle on it (see artist_watch._release_for)."""
    prefetched.append(folder)


pending_albums.prefetch_content = fake_prefetch
events.emit = fake_emit


def watch_events():
    return [e for e in events_seen if e["event"] == "watch.new_release"]


def reset():
    """A clean slate: no watches, no wishes, no library, no stub history."""
    for table in ("artist_watch_release", "artist_watch"):
        with artist_watch._conn() as c:
            c.execute(f"DELETE FROM {table}")
    for w in wishes.list_wishes():
        wishes.delete_wish(w["id"])
    shutil.rmtree(pathmod.library_root(MF), ignore_errors=True)
    GROUPS.clear()
    RELEASES.clear()
    BY_GROUP.clear()
    browse_calls.clear()
    triggers.clear()
    events_seen.clear()
    owned.clear()


def folders():
    """Every album folder the library holds right now."""
    root = pathmod.library_root(MF)
    out = []
    for dirpath, _dirs, _files in os.walk(root):
        if pathmod.load_pending(dirpath):
            out.append(dirpath.replace("\\", "/"))
    return sorted(out)


CFG = load_config()
EPOCH = time.mktime(time.strptime("2026-01-01 12:00", "%Y-%m-%d %H:%M"))
TODAY = datetime.date.today()


def days_after_watch(days=2):
    return (TODAY + datetime.timedelta(days=days)).isoformat()


# --------------------------------------------------------------------------- #
# 1. creating a watch queues nothing
# --------------------------------------------------------------------------- #
print("\ncreate queues nothing")
reset()
add_group(1, "Brand New", days_after_watch())
w = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH)
eq(artist_watch.list_watches()[0]["id"], w["id"], "watch stored")
eq(wishes.list_wishes(), [], "no wish at creation")
eq(folders(), [], "no framework folder at creation")
eq(watch_events(), [], "no event at creation")
# the same through the API, with an allow-list and backfill both set
reset()
add_group(1, "Brand New", days_after_watch())
r = CLIENT.post("/api/watches", json={"artist_mbid": ARTIST, "artist": ARTIST_NAME,
                                      "policy": "backfill",
                                      "include": [rg_id(1)]})
eq(r.status_code, 200, "POST /api/watches", r.text[:200])
eq(wishes.list_wishes(), [], "backfill watch queues nothing at creation")
eq(folders(), [], "backfill watch creates no folder at creation")

# --------------------------------------------------------------------------- #
# 2. new only: after the watch, never before
# --------------------------------------------------------------------------- #
print("\nnew only: after the watch was created, not before")
reset()
add_group(1, "Old Album", "2025-11-20")
add_group(2, "New Album", "2026-02-14")
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH)
out = artist_watch.run_watch(watch, CFG, force=True)
eq([q["title"] for q in out["queued"]], ["New Album"], "only the newer group queued")
ok(prefetched and prefetched[-1].endswith(("New Album", str(out["queued"][0].get("album_path") or "")[-12:])),
   f"and its page content is fetched in the background, not inline ({prefetched[-1:]})")
eq(len(browse_calls), 1, "ONE MusicBrainz browse for the artist, not one per group")
eq(len(wishes.list_wishes()), 1, "one wish on the existing queue")
eq(len(folders()), 1, "one framework album")
eq(triggers, [wishes.list_wishes()[0]["id"]], "the existing wish worker was told")
ev = watch_events()
eq(len(ev), 1, "ONE event for the queued release")
eq(ev[0]["title"], "New release — Test Artist", "event title names the artist")
eq(ev[0]["body"], "New Album (2026)", "event body is album (year)")
ok(str(ev[0]["data"].get("link") or "").startswith("/album/"),
   "event links to the album", ev[0]["data"].get("link"))
# a track-by-track guard against the dump: the watch's own date rule, directly
ok(artist_watch.is_new("2026-06-01", EPOCH), "a later date is new")
ok(not artist_watch.is_new("2025-06-01", EPOCH), "an earlier date is not new")
ok(not artist_watch.is_new("", EPOCH), "no date is never new")

# --------------------------------------------------------------------------- #
# 3. an undated release group is never new
# --------------------------------------------------------------------------- #
print("\nundated is never new")
reset()
add_group(1, "No Date Anywhere", "")
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH)
out = artist_watch.run_watch(watch, CFG, force=True)
eq(out["queued"], [], "undated group not queued")
eq(wishes.list_wishes(), [], "no wish")
eq(watch_events(), [], "no event")
cand = artist_watch.candidates(ARTIST, watch, CFG)["items"]
eq([(c["title"], c["is_new"], c["allowed"]) for c in cand],
   [("No Date Anywhere", False, False)], "candidate row says why")
ok("never treated as new" in cand[0]["reason"], "reason explains it")

# --------------------------------------------------------------------------- #
# 4. the type filter
# --------------------------------------------------------------------------- #
print("\ntype filter (a secondary type is a qualifier)")
reset()
add_group(1, "Live At Somewhere", "2026-03-10", primary="album", secondary=["Live"])
add_group(2, "Studio Record", "2026-03-11")
add_group(3, "Best Of", "2026-03-12", primary="album", secondary=["Compilation"])
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH)
rows = {c["title"]: c for c in artist_watch.candidates(ARTIST, watch, CFG)["items"]}
eq((rows["Live At Somewhere"]["allowed"], rows["Live At Somewhere"]["is_new"]),
   (False, True), "a live album is excluded with the album+EP default")
eq(rows["Best Of"]["allowed"], False, "so is a compilation")
eq(rows["Studio Record"]["allowed"], True, "a plain album is allowed")
out = artist_watch.run_watch(watch, CFG, force=True)
eq([q["title"] for q in out["queued"]], ["Studio Record"], "only the studio record queued")
# ticking "live" finds the live album even though its primary type is Album
artist_watch.update_watch(watch["id"], {"release_types": ["live"]})
watch = artist_watch.get_watch(watch["id"])
out = artist_watch.run_watch(watch, CFG, force=True)
eq([q["title"] for q in out["queued"]], ["Live At Somewhere"], "live ticked, queued")
eq(len(watch_events()), 2, "one event per queued release, over the whole run")
# a secondary type matches even when the primary is not selected at all
reset()
add_group(1, "Score", "2026-03-10", primary="single", secondary=["Soundtrack"])
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH,
                               release_types=["soundtrack"])
out = artist_watch.run_watch(watch, CFG, force=True)
eq([q["title"] for q in out["queued"]], ["Score"], "secondary type alone matches")
# the vocabulary is closed: an unknown name is rejected, not kept
try:
    artist_watch.update_watch(watch["id"], {"release_types": ["album", "bogus"]})
    ok(False, "unknown type name rejected")
except ValueError as e:
    ok("bogus" in str(e), "unknown type name rejected", str(e))

# --------------------------------------------------------------------------- #
# 5. max_per_cycle is never exceeded
# --------------------------------------------------------------------------- #
print("\nmax_per_cycle")
reset()
for n in range(1, 11):
    add_group(n, f"Release {n}", f"2026-04-{n:02d}")
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH)
out = artist_watch.run_watch(watch, CFG, force=True)
eq(len(out["queued"]), 1, "ten candidates, cap 1 → one queued")
eq(len(wishes.list_wishes()), 1, "one wish after the first cycle")
eq(len(watch_events()), 1, "one event")
out = artist_watch.run_watch(watch, CFG, force=True)
eq(len(out["queued"]), 1, "the next cycle queues one more, not nine")
eq(len(wishes.list_wishes()), 2, "two wishes after two cycles")
# the cap is what bounds a cycle whatever the policy is
reset()
for n in range(1, 11):
    add_group(n, f"Release {n}", f"2026-04-{n:02d}")
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH,
                               policy="backfill", max_per_cycle=3)
out = artist_watch.run_watch(watch, CFG, force=True)
eq(len(out["queued"]), 3, "backfill with cap 3 queues exactly three")
eq(len(wishes.list_wishes()), 3, "three wishes, not ten")

# --------------------------------------------------------------------------- #
# 6. the same release is never queued twice
# --------------------------------------------------------------------------- #
print("\nnever twice")
reset()
add_group(1, "Once Only", "2026-05-01")
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH)
first = artist_watch.run_watch(watch, CFG, force=True)
eq(len(first["queued"]), 1, "queued once")
wishes_before, folders_before = len(wishes.list_wishes()), len(folders())
second = artist_watch.run_watch(watch, CFG, force=True)
eq(second["queued"], [], "the second cycle queues nothing")
eq(len(wishes.list_wishes()), wishes_before, "no second wish")
eq(len(folders()), folders_before, "no second framework album")
eq(len(watch_events()), 1, "no second event")
# a release the user queued by hand (a wish outside the watch) is respected too
reset()
add_group(1, "Hand Queued", "2026-05-01")
wishes.add_wish(rg_id(1), title="Hand Queued", artist=ARTIST_NAME)
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH)
out = artist_watch.run_watch(watch, CFG, force=True)
eq(out["queued"], [], "a group already on the wish queue is not queued again")
eq(len(wishes.list_wishes()), 1, "still the one wish")

# --------------------------------------------------------------------------- #
# 7. auto_add off: notify without queueing
# --------------------------------------------------------------------------- #
print("\nauto_add false notifies, never queues")
reset()
add_group(1, "Tell Me Only", "2026-06-01")
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH, auto_add=False)
out = artist_watch.run_watch(watch, CFG, force=True)
eq([n["title"] for n in out["notified"]], ["Tell Me Only"], "notified")
eq(out["queued"], [], "nothing queued")
eq(wishes.list_wishes(), [], "no wish")
eq(folders(), [], "no framework album")
eq(len(watch_events()), 1, "the notification IS the event")
eq(artist_watch.get_watch(watch["id"])["notified_count"], 1, "notified counted")
# and it is not announced twice
out = artist_watch.run_watch(artist_watch.get_watch(watch["id"]), CFG, force=True)
eq(out["notified"], [], "the same release is not notified again")
eq(len(watch_events()), 1, "still one event")

# --------------------------------------------------------------------------- #
# 8. disabling stops the checks
# --------------------------------------------------------------------------- #
print("\ndisable stops the checks")
reset()
add_group(1, "After Disable", "2026-07-01")
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH)
artist_watch.update_watch(watch["id"], {"enabled": False})
out = artist_watch.run_watch(artist_watch.get_watch(watch["id"]), CFG, force=True)
eq(out["queued"], [], "a disabled watch queues nothing")
eq(wishes.list_wishes(), [], "no wish")
cycle = artist_watch_worker.run_cycle(watch["id"], force=True)
eq(cycle.get("queued") or [], [], "the worker's cycle queues nothing either")
eq(browse_calls, [], "a disabled watch is not even browsed")
# re-enabling brings it back
artist_watch.update_watch(watch["id"], {"enabled": True})
out = artist_watch.run_watch(artist_watch.get_watch(watch["id"]), CFG, force=True)
eq([q["title"] for q in out["queued"]], ["After Disable"], "re-enabled, queued")

# --------------------------------------------------------------------------- #
# 9. backfill queues only what the library lacks
# --------------------------------------------------------------------------- #
print("\nbackfill queues only what the library lacks")
reset()
add_group(1, "Held Already", "1999-01-01")
add_group(2, "Missing From Library", "1998-01-01")
owned[rg_id(1)] = "/somewhere/Held Already"
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH, policy="backfill")
out = artist_watch.run_watch(watch, CFG, force=True)
eq([q["title"] for q in out["queued"]], ["Missing From Library"],
   "the held group is skipped, the missing one queued")
eq(len(wishes.list_wishes()), 1, "one wish")
# new_only would have queued nothing at all here (both are years old)
reset()
add_group(1, "Held Already", "1999-01-01")
add_group(2, "Missing From Library", "1998-01-01")
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH)
out = artist_watch.run_watch(watch, CFG, force=True)
eq(out["queued"], [], "new_only queues nothing for an old discography")

# --------------------------------------------------------------------------- #
# 10. include is an allow-list, exclude a veto
# --------------------------------------------------------------------------- #
print("\ninclude / exclude")
reset()
add_group(1, "In The List", "2026-08-01")
add_group(2, "Not In The List", "2026-08-02")
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH,
                               include=[rg_id(1)])
out = artist_watch.run_watch(watch, CFG, force=True)
eq([q["title"] for q in out["queued"]], ["In The List"],
   "with include set, only the listed group may be queued")
# the newer, unlisted group is new and of an allowed type — and still refused
cand = {c["title"]: c for c in artist_watch.candidates(ARTIST, watch, CFG)["items"]}
ok(cand["Not In The List"]["is_new"] and not cand["Not In The List"]["allowed"],
   "unlisted group: new but not allowed", cand["Not In The List"]["reason"])
# exclude wins over include
reset()
add_group(1, "Vetoed", "2026-08-03")
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH,
                               include=[rg_id(1)], exclude=[rg_id(1)])
out = artist_watch.run_watch(watch, CFG, force=True)
eq(out["queued"], [], "exclude beats include")
# ... and over backfill
reset()
add_group(1, "Vetoed Too", "1997-01-01")
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH,
                               policy="backfill", exclude=[rg_id(1)])
out = artist_watch.run_watch(watch, CFG, force=True)
eq(out["queued"], [], "exclude beats backfill")
cand = artist_watch.candidates(ARTIST, watch, CFG)["items"][0]
eq(cand["reason"], "excluded by this watch", "the veto is the stated reason")
# an EMPTY include behaves exactly as before (any group that passes the rules)
reset()
add_group(1, "Anything New", "2026-08-04")
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH, include=[])
out = artist_watch.run_watch(watch, CFG, force=True)
eq([q["title"] for q in out["queued"]], ["Anything New"], "empty include = no filter")

# --------------------------------------------------------------------------- #
# 11. candidates — what the rules would do
# --------------------------------------------------------------------------- #
print("\ncandidates")
reset()
add_group(1, "New One", "2026-09-01")
add_group(2, "Old One", "2001-01-01")
add_group(3, "Queued One", "2026-09-02")
add_group(4, "Held One", "2026-09-03")
owned[rg_id(4)] = "/somewhere/Held One"
wishes.add_wish(rg_id(3), title="Queued One", artist=ARTIST_NAME)
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH)
out = artist_watch.candidates(ARTIST, watch, CFG)
eq(out["sources_asked"], ["musicbrainz"], "sources_asked")
eq(out["notes"]["policy"], "new_only", "notes carry the rules")
eq(out["notes"]["types"], ["album", "ep"], "notes carry the types")
eq(out["artist"], ARTIST_NAME, "the watch's artist name")
rows = {c["title"]: c for c in out["items"]}
eq(len(out["items"]), 4, "every release group is listed")
eq((rows["New One"]["allowed"], rows["New One"]["is_new"], rows["New One"]["queued"],
    rows["New One"]["in_library"]), (True, True, False, False), "allowed + new")
eq(rows["Old One"]["allowed"], False, "old group not allowed")
eq(rows["Queued One"]["queued"], True, "already on the wish queue")
eq(rows["Queued One"]["allowed"], True, "queued is not a policy veto")
eq(rows["Held One"]["in_library"], True, "the library holds it")
eq(len(browse_calls), 1, "one browse answered the whole picker")
# the pre-creation form, over HTTP, with the rules in the query
browse_calls.clear()
r = CLIENT.get("/api/watches/candidates",
               params={"artist_mbid": ARTIST, "policy": "backfill",
                       "release_types": "live,album"})
eq(r.status_code, 200, "GET /api/watches/candidates", r.text[:200])
body = r.json()
eq(body["watch_id"], None, "no watch yet")
eq(body["notes"]["policy"], "backfill", "the query's policy is applied")
eq(body["notes"]["types"], ["live", "album"], "repeated/comma types are accepted")
eq(sorted(c["title"] for c in body["items"] if c["allowed"]),
   ["Held One", "New One", "Old One", "Queued One"],
   "backfill allows everything the library lacks, old ones included")

# --------------------------------------------------------------------------- #
# 12. the HTTP surface
# --------------------------------------------------------------------------- #
print("\nHTTP surface")
reset()
add_group(1, "Via API", days_after_watch())
r = CLIENT.post("/api/watches", json={"artist_mbid": ARTIST})
eq(r.status_code, 200, "POST /api/watches", r.text[:200])
wid = r.json()["watch"]["id"]
eq(r.json()["watch"]["release_types"], ["album", "ep"], "config defaults applied")
eq(r.json()["watch"]["max_per_cycle"], 1, "default cap")
eq(r.json()["watch"]["auto_add"], True, "default auto_add")
eq(wishes.list_wishes(), [], "the route queued nothing")
r = CLIENT.post("/api/watches", json={"artist_mbid": ARTIST})
eq(r.status_code, 409, "a second watch for the same artist is refused")
ok("already watching" in r.json()["detail"], "409 detail names the artist",
   r.json().get("detail"))
r = CLIENT.post("/api/watches", json={"artist_mbid": "not-an-id"})
eq(r.status_code, 400, "a malformed artist id is 400")
r = CLIENT.post("/api/watches", json={"artist_mbid": "11111111-1111-1111-1111-111111111111"})
eq(r.status_code, 400, "an id MusicBrainz does not know as an artist is 400")
identity_error = integrations.MusicBrainzError("busy")
r = CLIENT.post("/api/watches", json={"artist_mbid": ARTIST})
eq(r.status_code, 503, "a MusicBrainz outage is 503, not 400")
identity_error = None
r = CLIENT.patch(f"/api/watches/{wid}", json={"release_types": ["album", "live", "demo"]})
eq(r.status_code, 200, "PATCH release_types", r.text[:200])
eq(r.json()["watch"]["release_types"], ["album", "live", "demo"], "types patched")
r = CLIENT.patch(f"/api/watches/{wid}", json={"release_types": ["nonsense"]})
eq(r.status_code, 400, "PATCH with an unknown type name is 400")
r = CLIENT.patch(f"/api/watches/{wid}", json={"policy": "everything"})
eq(r.status_code, 400, "PATCH with an unknown policy is 400")
r = CLIENT.patch(f"/api/watches/{wid}", json={"exclude": [rg_id(1)]})
eq(r.json()["watch"]["exclude"], [rg_id(1)], "the veto is patchable")
r = CLIENT.patch(f"/api/watches/{wid}", json={"exclude": []})
eq(r.json()["watch"]["exclude"], [], "and clearable")
r = CLIENT.post(f"/api/watches/{wid}/check")
eq(r.status_code, 200, "POST /api/watches/{id}/check", r.text[:200])
eq([q["title"] for q in r.json()["queued"]], ["Via API"], "check now queues a cycle")
eq(len(wishes.list_wishes()), 1, "one wish")
r = CLIENT.get("/api/watches")
body = r.json()
eq(len(body["watches"]), 1, "GET /api/watches lists it")
watch_payload = body["watches"][0]
eq([q["title"] for q in watch_payload["queued"]], ["Via API"], "what it queued")
eq(watch_payload["imported"], [], "nothing imported yet")
eq(watch_payload["queued_count"], 1, "the queued counter")
ok(watch_payload["last_checked_at"] > 0, "last check recorded")
ok(watch_payload["next_check_at"] > watch_payload["last_checked_at"],
   "next check is an interval after the last", watch_payload["next_check_at"])
ok("worker" in body and "watches" in body["worker"], "the worker's own state")
ok("runs" not in body["worker"], "no invented fields")
# the same check again queues nothing, and the payload still shows the one item
r = CLIENT.post(f"/api/watches/{wid}/check")
eq(r.json()["queued"], [], "a forced check is still capped and deduped")
r = CLIENT.get(f"/api/watches/{wid}/candidates")
eq(r.status_code, 200, "GET /api/watches/{id}/candidates", r.text[:200])
eq(r.json()["watch_id"], wid, "the watch's own rules")
# an imported release leaves the queued list
wish_id = wishes.list_wishes()[0]["id"]
wishes.mark_imported(wish_id, "/somewhere/Via API")
body = CLIENT.get("/api/watches").json()["watches"][0]
eq([i["title"] for i in body["imported"]], ["Via API"], "imported is reported")
eq(body["queued"], [], "and no longer queued")
eq(body["imported_count"], 1, "imported counted")
r = CLIENT.delete(f"/api/watches/{wid}")
eq(r.status_code, 200, "DELETE /api/watches/{id}")
eq(CLIENT.get("/api/watches").json()["watches"], [], "gone from the list")
eq(CLIENT.delete(f"/api/watches/{wid}").status_code, 404, "deleting it again is 404")
eq(CLIENT.patch("/api/watches/999999", json={"enabled": False}).status_code, 404,
   "patching an unknown watch is 404")
eq(CLIENT.post("/api/watches/999999/check").status_code, 404,
   "checking an unknown watch is 404")

# --------------------------------------------------------------------------- #
# 13. the worker
# --------------------------------------------------------------------------- #
print("\nworker")
reset()
add_group(1, "Worker Cycle", "2026-10-01")
watch = artist_watch.add_watch(ARTIST, ARTIST_NAME, added_at=EPOCH)
# A watch that has never been checked is due at once — waiting a whole
# interval before the first look would make a new watch feel broken.
out = artist_watch_worker.run_cycle()
eq([q["title"] for q in out.get("queued") or []], ["Worker Cycle"],
   "a new watch is checked on the first cycle")
ok(artist_watch_worker.status()["cycles"] >= 1, "the cycle is counted")
# ... and then it waits out its own interval
out = artist_watch_worker.run_cycle()
eq(out.get("queued") or [], [], "the watch is not due again straight away")
eq(len(watch_events()), 1, "still one event")
artist_watch._bump(watch["id"], last_checked_at=time.time() - 3600)
ok(not artist_watch.due(artist_watch.get_watch(watch["id"]), CFG),
   "an hour into a 24-hour interval the watch is not due")
# two cycles at once would evaluate the same artist twice
artist_watch_worker._running_cycle = True
try:
    refused = artist_watch_worker.run_cycle()
finally:
    artist_watch_worker._running_cycle = False
eq((refused.get("ok"), refused.get("error")), (False, "a watch cycle is already running"),
   "a second cycle at once is refused")
ok(artist_watch_worker.start() is True or artist_watch_worker._worker is not None,
   "the worker starts")
artist_watch_worker.stop()
ok(artist_watch_worker._stop.is_set(), "the worker stops")

# --------------------------------------------------------------------------- #
# 14. config
# --------------------------------------------------------------------------- #
print("\nconfig")
base = cfgmod.normalize_config({})
eq(base["artist_watch_enabled"], True, "enabled by default")
eq(base["artist_watch_interval_hours"], 24, "interval default")
eq(base["artist_watch_max_per_cycle"], 1, "cap default")
eq(base["artist_watch_types"], ["album", "ep"], "types default")
eq(base["artist_watch_auto_add"], True, "auto_add default")
eq(cfgmod.normalize_config({"artist_watch_types": ["Album", "live", "bogus"]})["artist_watch_types"],
   ["album", "live"], "the config vocabulary is closed too")
# …and the app's DERIVED type is part of that vocabulary (mlo.naming
# .RELEASE_TYPES = MusicBrainz's own names + "podcast", the app's own reading
# of a Podcast series relation), so a configured podcast watch is kept rather
# than dropped as an unknown name — while a name nobody knows still is.
eq(cfgmod.normalize_config({"artist_watch_types": ["Podcast", "broadcast"]})["artist_watch_types"],
   ["podcast", "broadcast"], "the derived podcast type is a watch type too")
eq(cfgmod.normalize_config({"artist_watch_types": ["podcast", "bogus"]})["artist_watch_types"],
   ["podcast"], "an unknown name is still dropped beside it")
eq(cfgmod.normalize_config({"artist_watch_max_per_cycle": 0})["artist_watch_max_per_cycle"],
   1, "the cap can never be zero")
eq(cfgmod.normalize_config({"artist_watch_interval_hours": 99999})["artist_watch_interval_hours"],
   720, "the interval is clamped")

# --------------------------------------------------------------------------- #
print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    for name in FAILED:
        print(f"  - {name}")
    sys.exit(1)
print("all checks passed")
