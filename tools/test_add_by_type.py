#!/usr/bin/env python3
""""Add to library" / "Download all" by RELEASE-GROUP TYPE — offline.

The artist page offers one add/download pair for the whole discography and one
per release-group type MusicBrainz actually holds ("Album", "Album +
Compilation", "Single", …). This test drives that contract end to end with
MusicBrainz itself stubbed at its ONE seam (`integrations.mb_get_cached`), so
every assertion below is about this app's own behaviour:

  * `types=["album"]` queues the Album groups and reports every other group as
    skipped WITH the type it actually is — never a silent drop;
  * `types=["album", "album+compilation"]` handles the combined spelling, and
    a live album is still not an album (a secondary type decides the match);
  * an empty `types` queues everything, which is the behaviour every caller
    had before the filter existed;
  * `download=True` asks the existing wishes worker to search exactly the
    wishes the call created, `download=False` leaves them to the queue's own
    loop;
  * with `auto_acquisition_enabled` off, Add records the album and says so in
    that switch's own words, and Download all cannot start — it says the same
    thing instead of silently doing nothing.

Run: python tools/test_add_by_type.py
"""
import atexit
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: the app's paths resolve through the music folder the moment they
# are first touched, so the scope is redirected BEFORE anything imports it.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-types-redirect-")
MF = tempfile.mkdtemp(prefix="mlo-types-test-")
os.environ["MLO_MUSIC_FOLDER"] = MF

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")

def set_cfg(**over):
    """Write the stubbed config where the app ACTUALLY reads it and load it.

    The live file is <music folder>/.mlo/data/config.json once the folder is
    known (`mlo.config.active_config_file`), not the legacy stub — and the
    switch tests flip `auto_acquisition_enabled` through here."""
    with open(cfgmod.active_config_file(), "w", encoding="utf-8") as f:
        json.dump({"music_folder": MF, **over}, f)
    return cfgmod.load_config()


for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")

atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True)
                         for d in (REDIRECT, MF)])
atexit.register(lambda: os.environ.pop("MLO_MUSIC_FOLDER", None))

REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/")
for _t in (MF,):
    if REAL:
        assert not _t.replace("\\", "/").lower().startswith(REAL.lower()), \
            f"temp fixture {_t} sits inside the real music folder {REAL}"

from mlo import import_policy, release_choice  # noqa: E402
from server import api_add, artcache, integrations as intg, pending_albums  # noqa: E402
from server import events, wishes, wishes_worker  # noqa: E402

FAILED = []


def ok(cond, label, extra=""):
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}{f' — {extra}' if extra else ''}")
        FAILED.append(label)


def eq(got, want, label):
    ok(got == want, label, f"got {got!r}, want {want!r}")


# --------------------------------------------------------------------------- #
# stubs: MusicBrainz at its one seam, the cover fetch, the worker's trigger and
# the event channel. Every MusicBrainz request this app makes goes through
# `mb_get_cached`, so an endpoint the fixtures do not know is an ERROR — the
# test proves nothing when it quietly reaches the network.
# --------------------------------------------------------------------------- #
COVER = b"\xff\xd8\xff" + b"placeholder-cover" * 8
artcache.fetch_art = lambda url, **kw: (COVER, "image/jpeg", "coverartarchive")

ARTIST = "aaaaaaaa-0000-0000-0000-000000000001"
ARTIST_NAME = "Type Artist"

# (key, title, primary type, secondary types) — the five shapes the artist page
# has to tell apart: a plain album, the combined "Album + Compilation", a live
# album (which is NOT an album), a single and an EP.
GROUPS = (
    ("album", "First Album", "Album", []),
    ("albumcomp", "Hits", "Album", ["Compilation"]),
    ("live", "Live Somewhere", "Album", ["Live"]),
    ("single", "A Single", "Single", []),
    ("ep", "An EP", "EP", []),
)


def rg_id(index):
    return f"{index:08d}-2222-2222-2222-222222222222"


def rel_id(index):
    return f"{index:08d}-1111-1111-1111-111111111111"


def _group_node(index):
    key, title, primary, secondary = GROUPS[index]
    return {"id": rg_id(index), "title": title, "primary-type": primary,
            "secondary-types": list(secondary), "first-release-date": "2001-01-01",
            "artist-credit": [{"name": ARTIST_NAME, "artist": {"id": ARTIST}}]}


def _release_row(index):
    """One edition as the *browse* endpoint serves it (inc=media): the policy
    reads status/country/media/track-count from exactly these fields."""
    key, title, primary, secondary = GROUPS[index]
    return {"id": rel_id(index), "title": f"{title} (CD)", "date": "2001-01-01",
            "country": "US", "status": "Official", "barcode": "",
            "disambiguation": "",
            "release-group": {"id": rg_id(index), "primary-type": primary,
                              "secondary-types": list(secondary)},
            "media": [{"format": "CD", "position": 1, "track-count": 2}]}


def _release_lookup(index):
    """The release as `release_lookup` normalizes it — what a framework album
    is built from."""
    key, title, primary, secondary = GROUPS[index]
    return {
        "id": rel_id(index), "title": f"{title} (CD)", "date": "2001-01-01",
        "originaldate": "2001", "country": "US", "status": "Official",
        "medium": "CD", "label": "Type Label", "catalog_number": f"TYPE-{index}",
        "release_group_id": rg_id(index),
        "release_type": "+".join([primary.lower()] + [s.lower() for s in secondary]),
        "primary_type": primary, "secondary_types": list(secondary),
        "artists": [{"name": ARTIST_NAME, "mbid": ARTIST}],
        "medium_count": 1,
        "media": [{"disc": 1, "position": 1, "title": f"{title} One",
                   "recording_mbid": f"{index:08d}-4444-4444-4444-444444444444",
                   "artist_credit": ARTIST_NAME},
                  {"disc": 1, "position": 2, "title": f"{title} Two",
                   "recording_mbid": f"{index:08d}-5555-5555-5555-555555555555",
                   "artist_credit": ARTIST_NAME}],
    }


INDEX_BY_RG = {rg_id(i): i for i in range(len(GROUPS))}
INDEX_BY_REL = {rel_id(i): i for i in range(len(GROUPS))}
mb_calls = []


def fake_mb_get_cached(endpoint, params=None, timeout=30.0, retries=5):
    """MusicBrainz's own answers for this artist, and nothing else."""
    params = dict(params or {})
    mb_calls.append((endpoint, params))
    if endpoint == f"artist/{ARTIST}":
        return {"id": ARTIST, "name": ARTIST_NAME, "type": "Group",
                "country": "", "area": {"name": "Testland"},
                "life-span": {"begin": "1994", "end": ""},
                "genres": [], "tags": []}
    if endpoint == "release-group" and params.get("artist") == ARTIST:
        return {"release-groups": [_group_node(i) for i in range(len(GROUPS))],
                "release-group-count": len(GROUPS)}
    if endpoint.startswith("release-group/"):
        index = INDEX_BY_RG.get(endpoint.split("/", 1)[1])
        assert index is not None, f"unknown release group {endpoint}"
        return _group_node(index)
    if endpoint == "release" and params.get("release-group"):
        index = INDEX_BY_RG.get(params["release-group"])
        assert index is not None, f"unknown release group {params['release-group']}"
        return {"releases": [_release_row(index)], "release-count": 1}
    if endpoint.startswith("release/"):
        index = INDEX_BY_REL.get(endpoint.split("/", 1)[1])
        assert index is not None, f"unknown release {endpoint}"
        return _release_lookup(index)
    raise AssertionError(f"unexpected MusicBrainz request: {endpoint} {params}")


intg.mb_get_cached = fake_mb_get_cached
intg.mb_get = lambda endpoint, params=None, **kw: fake_mb_get_cached(endpoint, params)

triggered = []
wishes_worker.trigger = lambda wid=None: (triggered.append(wid), {"ok": True})[1]

emitted = []
events.emit = lambda kind, title, body="", data=None, **kw: emitted.append(
    {"kind": kind, "title": title, "body": body, "data": data or {}})


def queued_ids(rows):
    return [r["mbid"] for r in rows]


def skipped_reasons(rows, want_ids=None):
    return {r["mbid"]: r["reason"] for r in rows
            if want_ids is None or r["mbid"] in want_ids}


# --------------------------------------------------------------------------- #
# 1. the type filter, straight through the one resolution path
# --------------------------------------------------------------------------- #
print("\nrelease-group types select what is queued")
cfg = set_cfg()

rows, skipped = intg.auto_import_targets(ARTIST, "artist", "best", types=["album"])
eq(queued_ids(rows), [rel_id(0)], '"album" queues exactly the plain Album group')
eq(skipped_reasons(skipped), {
    rg_id(1): "release-group type not requested (Album + Compilation)",
    rg_id(2): "release-group type not requested (Album + Live)",
    rg_id(3): "release-group type not requested (Single)",
    rg_id(4): "release-group type not requested (EP)",
}, "every other group is skipped WITH the type it actually is")

rows, skipped = intg.auto_import_targets(ARTIST, "artist", "best",
                                        types=["album", "album+compilation"])
eq(queued_ids(rows), [rel_id(0), rel_id(1)],
   'the combined spelling "album+compilation" selects the combined group')
eq(sorted(r["reason"] for r in skipped),
   ["release-group type not requested (Album + Live)",
    "release-group type not requested (EP)",
    "release-group type not requested (Single)"],
   "and the live album is still not an album")

rows, skipped = intg.auto_import_targets(ARTIST, "artist", "best",
                                        types=["album + live"])
eq(queued_ids(rows), [rel_id(2)],
   '"Album + Live" as the page spells it selects the live album')

rows, skipped = intg.auto_import_targets(ARTIST, "artist", "best")
eq(sorted(queued_ids(rows)), sorted(rel_id(i) for i in range(len(GROUPS))),
   "an empty types filter queues every group (no regression)")
eq(skipped, [], "and reports nothing as skipped")

rows, skipped = intg.auto_import_targets(rg_id(3), "release_group", "best",
                                        types=["single"])
eq(queued_ids(rows), [rel_id(3)], "a release GROUP add is filtered by type too")
rows, skipped = intg.auto_import_targets(rg_id(3), "release_group", "best",
                                        types=["album"])
eq(rows, [], "a Single group asked for as an album queues nothing")
eq([r["reason"] for r in skipped],
   ["release-group type not requested (Single)"],
   "and says which type it actually is")

ok(release_choice.type_matches("Album", ["Compilation"], ["album + compilation"]),
   "the ONE matching rule accepts the page's own combined spelling")
ok(not release_choice.type_matches("Album", ["Compilation"], ["album"]),
   "and a secondary type still decides the match")
ok(not release_choice.type_matches("Album", [], ["album + compilation"]),
   "while a combined spelling is the group's WHOLE type, not its parts")
# The one normalizer keeps a combined selection whole — an add's row and a
# watch's stored selection alike.
eq(release_choice.type_names(["Album + Compilation", "album", "album+compilation"]),
   ["album + compilation", "album"],
   "type_names keeps a combined SELECTION whole (lowercased, de-duplicated) in ask order")
eq(release_choice.type_names(["Album; Live"]), ["album + live"],
   "and reads MusicBrainz's own '; '-joined spelling as the same one selection")

# --------------------------------------------------------------------------- #
# 1b. a row's own list is expanded WHOLE by the background prepare
# --------------------------------------------------------------------------- #
print("\nthe background prepare expands the row it named")
# The per-call bound is for a call that has to ANSWER (the auto-import route's
# quick attempt, whose remainder a job redoes). The artist page's add works
# OFF the request — the albums appear one at a time — so a bound there
# truncated the row the button named ("Album + Compilation · 106" prepared 50
# and called the other 56 "per-call limit reached — call again"), which is the
# button failing while the endpoint answered 200.
many = [{"id": rg_id(i), "title": f"Group {i}", "primary_type": "Album",
         "secondary_types": [], "first_release_date": "2001-01-01"}
        for i in range(60)]
_real_browse, _real_group = intg.artist_browse, intg.group_targets
intg.artist_browse = lambda *a, **kw: {"release_groups": many, "total": len(many)}
intg.group_targets = lambda gid, mode, **kw: (
    [{"mbid": rel_id(0), "title": "A Group"}], None)
try:
    capped, skipped_rows = intg.auto_import_targets(ARTIST, "artist", "best")
    eq(len(capped), intg.BULK_MAX_GROUPS, "a bounded call still stops at the limit")
    eq(sum("per-call limit" in (r["reason"] or "") for r in skipped_rows), 10,
       "and reports the rest, never dropping them")
    whole, nothing_skipped = intg.auto_import_targets(ARTIST, "artist", "best",
                                                      limit=None)
    eq(len(whole), len(many), "the unbounded call expands every group of the row")
    eq(nothing_skipped, [], "with nothing left over to call again for")
finally:
    intg.artist_browse, intg.group_targets = _real_browse, _real_group

# --------------------------------------------------------------------------- #
# 2. "Add to library" records; "Download all" also starts the search now
# --------------------------------------------------------------------------- #
print("\nAdd vs Download all")
try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except Exception as e:                                            # pragma: no cover
    print(f"  SKIP  TestClient unavailable: {e}")
    raise SystemExit(0)

app = FastAPI()
app.include_router(api_add.router)
client = TestClient(app)


def add(**body):
    r = client.post("/api/library/add", json=body)
    return r.status_code, r.json()


triggered.clear()
status, body = add(mbid=rg_id(3), kind="release_group", types=["single"])
eq(status, 200, "a type-filtered release-group add answers 200")
eq([a["release_id"] for a in body["albums"]], [rel_id(3)], "its album was created")
# Every add starts the search it recorded, on the worker's OWN pass (no wish
# id: the pass reads the store and searches what is due). Waiting for the
# loop's next tick was up to two minutes of nothing happening.
eq(triggered, [None], "Add starts the search it recorded, on the worker's own pass")
eq(body["note"], "Soulseek is searching for them now.",
   "and says so, exactly as Download all does")
wish_id = body["albums"][0]["wish_id"]
ok(bool(wish_id) and wishes.get_wish(wish_id) is not None,
   "the album's wish is on the existing queue")

triggered.clear()
status, body = add(mbid=rg_id(4), kind="release_group", types=["ep"], download=True)
eq(status, 200, "a download ask answers 200")
eq(body["note"], "Soulseek is searching for them now.",
   "Download all says the search started")
eq(triggered, [None], "Download all starts the same one pass")

status, body = add(mbid=rg_id(0), kind="release_group", types=["nope"])
eq(status, 400, "a type outside MusicBrainz's vocabulary is refused")
ok("nope" in json.dumps(body), "and the refusal names it", body)

# --------------------------------------------------------------------------- #
# 3. the artist handover: what it is queueing, and what the filter left out
# --------------------------------------------------------------------------- #
print("\nthe artist handover")
# The discography prepare is one MusicBrainz browse per release group, so the
# route hands it to a thread. The thread's own work is section 4, called
# directly; here the handover is what is under test.
real_prepare = api_add._prepare_artist
started = []
api_add._prepare_artist = lambda mbid, mode, cfg, req, types=None: started.append(
    {"mbid": mbid, "mode": mode, "types": types, "download": req.download})

before = len(mb_calls)
status, body = add(mbid=ARTIST, kind="artist", types=["album", "album+compilation"])
eq(status, 200, "a type-filtered artist add answers 200")
eq(body["background"], True, "which is handed over to the background prepare")
eq(body["queued"], 2, "the answer says how many release groups it is queueing")
eq(sorted(r["mbid"] for r in body["skipped"]),
   sorted([rg_id(2), rg_id(3), rg_id(4)]),
   "and reports the groups the type filter left out")
ok(all("type not requested" in r["reason"] for r in body["skipped"]),
   "each with the type it actually is", body["skipped"][:2])
eq(started[-1]["types"], ["album", "album + compilation"],
   "the background job got the rows' selections, the compound one kept WHOLE")
eq(started[-1]["download"], False, "and the add's own download flag")
ok("Album + Compilation" in body["note"],
   "the note names the type it is preparing", body["note"])
ok(len(mb_calls) > before,
   "which took ONE MusicBrainz browse (the one the page has already made)")

started.clear()
status, body = add(mbid=ARTIST, kind="artist", types=["Album + Compilation"])
eq(status, 200, "the page's own combined spelling answers 200")
eq(body["queued"], 1, "and counts exactly the groups of that WHOLE type")
eq(sorted(r["mbid"] for r in body["skipped"]),
   sorted([rg_id(0), rg_id(2), rg_id(3), rg_id(4)]),
   "the plain album is skipped as the Album it is — the flattened reading "
   "queued it beside the compilation")
eq([s["reason"] for s in body["skipped"] if s["mbid"] == rg_id(0)],
   ["release-group type not requested (Album)"], "and names the type it is")
eq(started[-1]["types"], ["album + compilation"],
   "while the background job prepares that one selection, not its two names")

started.clear()
status, body = add(mbid=ARTIST, kind="artist", types=["broadcast"])
eq(status, 200, "a type the artist has none of answers 200")
eq(body["background"], False, "and hands nothing over")
eq(body["queued"], 0, "the count is zero")
eq(started, [], "so nothing was started")
eq(len(body["skipped"]), len(GROUPS), "while every group is reported, by type")

started.clear()
status, body = add(mbid=ARTIST, kind="artist", download=True)
eq(body["queued"], None, "a whole-discography handover states no count")
eq(body["note"], "Preparing the discography — each album appears in the "
                 "library as it is added, and the search starts with it.",
   "and its note says the search starts with each album")
eq(started[-1]["download"], True, "which is what it was asked for")
api_add._prepare_artist = real_prepare

# --------------------------------------------------------------------------- #
# 4. the prepare itself: the type filter, the skipped reasons, the trigger
# --------------------------------------------------------------------------- #
print("\nthe discography prepare, called directly (no thread)")


class _Req:
    """The request the route would have handed the thread."""
    queries = None
    title = ""
    artist = ""
    year = ""
    download = False


def prepare(types=None, cfg=None):
    emitted.clear()
    triggered.clear()
    api_add._prepare_artist(ARTIST, "best", cfg or set_cfg(), _Req(), types)
    return emitted[-1]


event = prepare(types=["album"])
album_paths = event["data"]["albums"]
eq(len(album_paths), 1, "the one Album group's album was created")
eq(len(triggered), 1, "which started ONE pass for the whole batch")
# The kick names no wish (the pass reads the store and searches what is due),
# so what matters is that the wish for THIS release is on the queue, is due
# now, and owns the framework album the event reported.
found = [w for w in wishes.list_wishes() if w["release_mbid"] == rel_id(0)]
eq(len(found), 1, "and the album has exactly one wish on the existing queue")
started_wish = found[0] if found else None
ok(bool(started_wish) and wishes_worker._due(started_wish, set_cfg()),
   "which is due for its own next search, so that pass picks it up", started_wish)
ok(bool(started_wish) and os.path.normcase(started_wish["album_path"])
   == os.path.normcase(album_paths[0]),
   "and it owns the framework album the event reported")
eq(event["body"], "Soulseek is searching for them now. 4 release group(s) skipped.",
   "the event says the search started, and how many groups were left out")
eq(sorted(r["reason"] for r in event["data"]["errors"]),
   ["release-group type not requested (Album + Compilation)",
    "release-group type not requested (Album + Live)",
    "release-group type not requested (EP)",
    "release-group type not requested (Single)"],
   "and those four are reported, by the type they actually are")
eq(event["data"]["types"], ["album"], "the event names the type filter it ran with")

event = prepare(types=["album"])
eq(len(triggered), 1, "a second call starts its own pass too (one kick each)")
eq(event["body"], "Soulseek is searching for them now. 4 release group(s) skipped.",
   "and the event says the search started, and what the filter left out")

# --------------------------------------------------------------------------- #
# 5. auto_acquisition_enabled off: both buttons say the same thing
# --------------------------------------------------------------------------- #
print("\nautomation off")
# The switch is read from the config the app loads, so it is the FILE that has
# to say it — for the route (which loads its own config) exactly as for the
# background prepare (which is handed one).
off = set_cfg(auto_acquisition_enabled=False)

triggered.clear()
status, body = add(mbid=rg_id(1), kind="release_group",
                  types=["album + compilation"], download=True)
eq(status, 200, "Download all still answers 200")
ok(body["albums"] and body["albums"][0]["created"],
   "and the album IS recorded — the request was the user's")
eq(triggered, [], "nothing was started")
eq(body["note"], import_policy.AUTO_OFF_NOTE,
   "and the reply is the switch's own sentence")
ok("auto_acquisition_enabled" in body["note"],
   "which names the switch", body["note"])

triggered.clear()
status, body = add(mbid=rg_id(2), kind="release_group", types=["album + live"])
eq(triggered, [], "Add starts nothing either")
eq(body["note"], import_policy.AUTO_OFF_NOTE,
   "and reports the same switch")

event = prepare(types=["single"], cfg=off)
eq(triggered, [], "the background prepare starts nothing with the switch off")
eq(event["body"], import_policy.AUTO_OFF_NOTE + " 4 release group(s) skipped.",
   "and its event is that same wording")
ok(import_policy.auto_acquisition_enabled(set_cfg()), "the switch is back on")

print()
if FAILED:
    print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
    sys.exit(1)
print("All add-by-type checks passed.")
sys.exit(0)
