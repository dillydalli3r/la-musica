#!/usr/bin/env python3
"""Nothing found is not a dead end: the album stays a wish, the queue says so.

The two halves of the Soulseek queue this proves, against the REAL registries
(the wish db, the job registry, the queue view and the retry policy) with
slskd, MusicBrainz and the filesystem stubbed where the network would be:

  1. WHICH RELEASE each row is about. Every queue row carries the release
     identity block (``server.wishes.RELEASE_KEYS``: catalogue number, medium,
     country, date, track count, disambiguation, status, label) — built from
     what the row already knows, so no polled route ever spends a MusicBrainz
     request on it — and a row that cannot name a release carries the SAME keys
     empty instead of missing. A wish whose release could not be resolved (an
     outage, an id MusicBrainz has nothing for) still renders.
  2. An album the network does not have. "Add to library" with `download` true
     records the framework album + wish and runs the quick attempt; the attempt
     finds nothing, and the wish must still be there — with its status,
     attempts, empty-search count, reason and scheduled retry — immediately
     after and after a later worker cycle, in the WISHES list and in the queue's
     "Needs you" section (not filed as a failure). An unrelated wish in the
     same session keeps going to the library, and one press on the row's retry
     re-arms the wish and searches it again.

Standalone: `python tools/test_wishes_pipeline.py`, temp roots, no network.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{('  — ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILED.append(name)


# --------------------------------------------------------------------------- #
# hermeticity: throwaway music/config/state roots, so nothing here can touch
# the developer's library, wishes or download folder.
# --------------------------------------------------------------------------- #
REDIRECT = tempfile.mkdtemp(prefix="mlo-nf-")
MUSIC = os.path.join(REDIRECT, "music")
DL = os.path.join(REDIRECT, "downloads")
DATA = os.path.join(REDIRECT, "data")
for _d in (MUSIC, DL, DATA):
    os.makedirs(_d, exist_ok=True)
os.environ["MLO_MUSIC_FOLDER"] = MUSIC

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB_CFG = os.path.join(REDIRECT, "config.json")
with open(_STUB_CFG, "w", encoding="utf-8") as f:
    json.dump({"music_folder": MUSIC}, f)
cfgmod.CONFIG_FILE = _STUB_CFG
pathmod.CONFIG_FILE = _STUB_CFG
pathmod.app_data_dir = lambda *a, **k: DATA

import server.api_add as api_add  # noqa: E402
import server.api_queue as api_queue  # noqa: E402
import server.integrations as intg  # noqa: E402
import server.pending_albums as pending  # noqa: E402
import server.soulseek as slsk  # noqa: E402
import server.soulseek_auto as auto  # noqa: E402
import server.wishes as wishes  # noqa: E402
import server.wishes_worker as worker  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

wishes.db_path = lambda: os.path.join(REDIRECT, "wishes.db")
wishes._initialized = False

MISSING_ID = "0b1a2c3d-4e5f-6789-abcd-ef0123456789"
OTHER_ID = "11111111-2222-3333-4444-555555555555"
RARE_ID = "33333333-4444-5555-6666-777777777777"
REL = {
    "id": MISSING_ID, "title": "An Album", "date": "1994-05-06", "country": "GB",
    "catalog_number": "CAT-1", "catalog_numbers": ["CAT-1", "CAT-2"],
    "label": "A Label", "status": "Official", "disambiguation": "Deluxe Edition",
    "release_group_id": MISSING_ID, "medium_formats": ["CD"], "medium": "CD",
    "artists": [{"name": "An Artist"}],
    "media": [{"disc": 1, "position": i, "title": f"Track {i}", "length": 1000}
              for i in (1, 2)],
}
OTHER_REL = dict(REL, id=OTHER_ID, title="Another Album", catalog_number="CAT-9",
                 catalog_numbers=["CAT-9"], disambiguation="")
RARE_REL = dict(REL, id=RARE_ID, title="Rare Album", catalog_number="RARE-7",
                catalog_numbers=["RARE-7"], disambiguation="")
CFG = {
    "music_folder": MUSIC,
    "soulseek_search_concurrency": 3,
    "soulseek_auto_log_min_score": 100,
    "soulseek_auto_search_wait": 1,
    "soulseek_auto_response_limit": 5,
    "soulseek_auto_wish_prompt": True,
    "wishes_enabled": True,
    "wishes_interval_hours": 6,
    "wishes_auto_import": True,
    "wishes_max_attempts": 0,
    "wishes_not_found_attempts": 1,
    "wishes_retry_backoff_minutes": 30,
}


class Patch:
    """Set attributes for the block, restore them however it ends."""

    def __init__(self, obj, **kw):
        self.obj, self.kw, self.saved = obj, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = getattr(self.obj, k, None)
            setattr(self.obj, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.obj, k, v)
        return False


def wait_until(predicate, timeout=40.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# --------------------------------------------------------------------------- #
# The stubbed pipeline. `found` is the whole scenario: the network has the
# album, or it does not have a single usable folder.
# --------------------------------------------------------------------------- #
class Pipeline:
    def __init__(self):
        self.found = False
        self.searches = 0

    def search(self, slsk_, queries, wait_s, usable=None, response_limit=0):
        self.searches += 1
        responses = [("peer", {"fileCount": 4})] if self.found else []
        return ([("q", {"responses": responses})], [], 0)

    def candidates(self, results, release, cfg):
        if not self.found:
            return []                       # the network answered: nothing usable
        name = f"{release['artists'][0]['name']} - {release['title']}"
        files = [{"file": f"/remote/{name}/0{i} - Track.flac", "size": 1000}
                 for i in (1, 2)]
        return [{
            "username": "peer", "dir": f"/remote/{name}/", "files": files,
            "audio": files, "logs": [], "cues": [], "matched": 2, "expected": 2,
            "complete": True, "lossless": True, "slot": False, "queue": 0,
            "speed": 0, "total_size": 2000, "score": 90,
        }]

    def wait_for_files(self, slsk_, ddir, username, wanted, timeout_s,
                       cancel_check=None, phase="download", queue_budget_s=None,
                       on_start=None):
        root = os.path.join(ddir, username, "album")
        got = {}
        for w in wanted:
            path = os.path.join(root, os.path.basename(w["filename"]))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"x" * 64)
            got[w["filename"]] = path
        return got

    def import_album(self, root, release, cfg, media):
        artist = (release.get("artists") or [{}])[0].get("name", "")
        return {"album_path": os.path.join(MUSIC, "Artists", f"{artist} - {release['title']}"),
                "imported": True, "staging_path": root, "organized": True,
                "organize_error": None}


PIPE = Pipeline()


def _resolve(mbid):
    """integrations.resolve_release, canned — the release the wish is for."""
    rid = str(mbid or "").strip().lower()
    for rel in (REL, OTHER_REL, RARE_REL):
        if rid in (rel["id"].lower(), rel["release_group_id"].lower()):
            return dict(rel, id=rel["id"]), rel["id"]
    return None, ""                         # MusicBrainz has nothing for it


def pipeline_patches(cfg=None, **over):
    kw = dict(
        load_config=lambda: dict(cfg or CFG),
        _search_queries=PIPE.search,
        find_candidates=PIPE.candidates,
        _wait_for_files=PIPE.wait_for_files,
        _verify_album=lambda root, cfg, is_cd: (True, []),
        _stamp_media=lambda root, media, c: (2, []),
        _import=PIPE.import_album,
        _drop_candidate=lambda *a, **k: None,
        _prune_downloads=lambda: None,
        traceback=SimpleNamespace(print_exc=lambda *a, **k: None),
    )
    kw.update(over)
    return Patch(auto, **kw)


SLSK = Patch(slsk, is_running=lambda *a, **k: True, web_up=lambda *a, **k: True,
             server_state=lambda *a, **k: {"isLoggedIn": True},
             download_dir=lambda *a, **k: DL,
             prune_download_dirs=lambda *a, **k: None,
             enqueue_download=lambda *a, **k: {"ok": True},
             cancel_downloads=lambda *a, **k: [],
             clear_transfer_files=lambda *a, **k: {"files_deleted": 0})

app = FastAPI()
app.include_router(api_add.router)
app.include_router(api_queue.router)
client = TestClient(app)


def queue():
    return client.get("/api/queue").json()


def row_for(kind, ref):
    """The row for one item, whichever section it is in."""
    for name, rows in queue()["sections"].items():
        for r in rows:
            if r["id"] == f"{kind}:{ref}":
                return name, r
    return None, None


def wait_settled(wid, attempts=1):
    """Wait for the wish's quick attempt to have been COUNTED (attempts is the
    store's own record that an attempt happened and settled)."""
    return wait_until(lambda: int(wishes.get_wish(wid).get("attempts") or 0) >= attempts,
                      what=f"wish {wid} to settle its attempt")


# --------------------------------------------------------------------------- #
# 1. the release identity block: what each key is, and where it comes from
# --------------------------------------------------------------------------- #
print("== the release's own identity ==")

block = wishes.release_identity(REL, MISSING_ID)
check("the identity block carries the documented keys",
      set(block) == set(wishes.RELEASE_KEYS), sorted(block))
check("...naming the pressing first (catalogue number, medium)",
      block["catalog_number"] == "CAT-1" and block["media"] == ["CD"], json.dumps(block))
check("...then where/when and how much it carries",
      (block["country"], block["date"], block["track_count"]) == ("GB", "1994-05-06", 2),
      json.dumps(block))
check("...then the edition's disambiguation, its status and its label",
      (block["disambiguation"], block["status"], block["label"])
      == ("Deluxe Edition", "Official", "A Label"), json.dumps(block))

# The compact summary a running job carries spells two of those differently
# (server/soulseek_auto._run) — the same block comes out of it.
summary = {"id": "aaaaaaaa", "title": "Isles", "artist": "Bicep", "date": "2018-04-20",
           "country": "GB", "catalog_number": "ZEN-124", "media": "CD",
           "media_formats": ["CD"], "tracks": 12, "status": "Official",
           "disambiguation": "Deluxe Edition", "label": "Ninja Tune"}
job_block = wishes.release_identity(summary)
check("a job's own compact release summary reads as the same block",
      job_block["media"] == ["CD"] and job_block["track_count"] == 12
      and job_block["catalog_number"] == "ZEN-124", json.dumps(job_block))

# Nothing known is STILL the documented block, with the id it was keyed by.
empty = wishes.release_identity({}, MISSING_ID)
check("a release nobody resolved is the same keys, empty",
      set(empty) == set(wishes.RELEASE_KEYS) and empty["id"] == MISSING_ID
      and not any(v for k, v in empty.items() if k != "id"), json.dumps(empty))

# A wish whose release MusicBrainz cannot resolve: identity_of answers None (no
# exception) and its row keeps the empty block.
with Patch(intg, resolve_release=lambda mbid: (None, "")):
    check("an unresolvable release is None, never an exception",
          wishes.identity_of(MISSING_ID) is None)

# --------------------------------------------------------------------------- #
# 2. an album the network does not have: add-to-library, quick attempt, cycle
# --------------------------------------------------------------------------- #
print("\n== an album the network does not have ==")

PIPE.found = False
with pipeline_patches(), SLSK, \
     Patch(intg, resolve_release=_resolve, auto_import_targets=lambda mbid, kind=None,
           mode="best", **kw: ([{"mbid": MISSING_ID, "title": "An Album"}], [])), \
     Patch(api_add, load_config=lambda: dict(CFG)), \
     Patch(worker, load_config=lambda: dict(CFG)), \
     Patch(pending, write_placeholder_cover=lambda *a, **k: None,
           prefetch_content=lambda *a, **k: None):
    r = client.post("/api/library/add",
                    json={"mbid": MISSING_ID, "kind": "release", "download": True})
    check("the add answers 200 with the framework album + wish",
          r.status_code == 200 and (r.json().get("albums") or [{}])[0].get("wish_id"),
          r.text[:300])
    album = (r.json().get("albums") or [{}])[0]
    wid = album["wish_id"]

    # IMMEDIATELY: the album is recorded — in the wishes list and in the queue.
    listed = [w for w in wishes.list_wishes() if w["id"] == wid]
    check("the wish exists the moment the album is asked for",
          len(listed) == 1 and listed[0]["release_mbid"] == MISSING_ID,
          json.dumps(listed))
    check("...and its recorded folder is the framework album on disk",
          os.path.isdir(album["album_path"]) and listed
          and os.path.normcase(listed[0]["album_path"]) == os.path.normcase(album["album_path"]),
          json.dumps({"album_path": album["album_path"]}))

    wait_settled(wid)
    stored = wishes.get_wish(wid)

    # AFTER the quick attempt found nothing: still a wish, with its state.
    check("the wish is still there after the quick attempt found nothing",
          stored is not None and stored["status"] == "not_found", json.dumps(stored))
    check("...recording the attempt, the empty searches, the reason and the retry slot",
          (stored["attempts"], stored["not_found"]) == (1, 1)
          and "No candidate folder" in stored["last_error"]
          and float(stored["retry_at"]) >= 0.0,
          json.dumps({k: stored[k] for k in ("attempts", "not_found", "retry_at",
                                             "last_error")}))
    check("...and it is terminal, exactly as wishes_not_found_attempts (1) says",
          wishes.is_terminal(stored, CFG) and wishes.due_at(stored, CFG) == float("inf"))

    # The framework album the add created goes with that outcome. Nothing
    # searches this wish again by itself, so the folder the ADD made — its
    # marker, the placeholder cover, the release's own tracklist — would sit in
    # the library for good, looking like an album nobody has.
    wait_until(lambda: not os.path.isdir(album["album_path"]),
               what="the framework album to be taken down")
    wait_until(lambda: not (wishes.get_wish(wid) or {}).get("album_path"),
               what="the row to stop linking to the folder that went")
    ended = wishes.get_wish(wid)
    check("the framework album the terminal outcome leaves behind is taken back",
          not os.path.isdir(album["album_path"])
          and pathmod.load_pending(album["album_path"]) is None,
          json.dumps({"album_path": album["album_path"],
                      "marker": pathmod.load_pending(album["album_path"])}))
    check("...and the WISH row stays: it is the queue's own row, with the retry",
          ended is not None and not ended["album_path"]
          and row_for("wish", wid)[0] == "needs_attention",
          json.dumps({"row": ended, "section": row_for("wish", wid)[0]}))

    # The same pressing saved under the OTHER id. A wish from an album link
    # carries the release GROUP id (that is what the link holds) while an add
    # resolves the edition and keys its own row by the RELEASE id: two rows
    # would be two jobs, each downloading the same album.
    cross = dict(REL, id="0b1a2c3d-0000-0000-0000-00000000c0de",
                 release_group_id="0b1a2c3d-0000-0000-0000-00000000c0df",
                 title="Cross Wished", catalog_number="CROSS-1",
                 catalog_numbers=["CROSS-1"])
    saved = wishes.add_wish(cross["release_group_id"], title="Cross Wished",
                            artist="An Artist", source="soulseek")
    # The search count starts BEFORE the add: the add itself starts the
    # worker's pass, and that pass is what searches this row.
    before = PIPE.searches
    # The resolver stays patched over the wait below: the search the add starts
    # is the WORKER's, and it resolves the wish's own id.
    with Patch(intg, resolve_release=lambda mbid: (dict(cross), cross["id"]),
               auto_import_targets=lambda mbid, kind=None, mode="best", **kw: (
                   [{"mbid": cross["id"], "title": cross["title"]}], [])), \
         Patch(api_add, load_config=lambda: dict(CFG)):
        r2 = client.post("/api/library/add",
                         json={"mbid": cross["id"], "kind": "release"})
        added = (r2.json().get("albums") or [{}])[0]
        check("an add of the same pressing reuses the wish its release group saved",
              added.get("wish_id") == saved["id"],
              json.dumps({"added": added, "saved": saved["id"]}))
        check("...so the store holds ONE row for that release, not two",
              len([w for w in wishes.list_wishes()
                   if cross["id"] in (w["release_mbid"], (w.get("release") or {}).get("id"))
                   or w["release_mbid"] == cross["release_group_id"]]) == 1,
              json.dumps([w["id"] for w in wishes.list_wishes()]))
        wait_until(lambda: wishes.get_wish(saved["id"])["status"] == "not_found",
                   what="the cross-pressing search to settle")
        check("...and that one row costs exactly ONE search (not one per id)",
              PIPE.searches == before + 1, f"{before} -> {PIPE.searches}")
        # Hand the queue back quiet: the pass the add started is what searched
        # it, and the checks below run passes of their own.
        wait_until(lambda: not worker.status()["running"],
                   what="the pass the add started to end")

    # The queue, which is what the user reads.
    section, row = row_for("wish", wid)
    check("the queue lists it in the needs-you section, not as a failure",
          section == "needs_attention", json.dumps({"section": section, "row": row}))
    check("...with the store's own reason on it",
          bool(row) and "No candidate folder" in row["reason"], json.dumps(row or {}))
    check("...its state in words (nothing is searched again unless the user retries)",
          bool(row) and "not searched again unless you retry it" in row["note"],
          json.dumps(row or {}))
    check("...its store state (attempts / empty searches) and its one action",
          bool(row) and row["attempts"] == 1 and row["not_found"] == 1
          and row["retryable"] is True and row["cancelable"] is True, json.dumps(row or {}))
    check("...and the settled job did not draw a second row for itself",
          bool(row) and row["job_id"] is not None
          and len([1 for rows in queue()["sections"].values() for x in rows
                   if x.get("job_id") == row["job_id"]]) == 1,
          json.dumps(row or {}))
    check("...while still being listed at all (never dropped from the list)",
          any(w["id"] == wid for w in wishes.list_wishes()))

    # The identity block the row carries (offline: no MusicBrainz request).
    check("the wish row names the exact release, from the pipeline's own lookup",
          row["release"]["catalog_number"] == "CAT-1"
          and row["release"]["media"] == ["CD"]
          and row["release"]["track_count"] == 2
          and row["release"]["status"] == "Official"
          and row["release"]["label"] == "A Label", json.dumps(row["release"]))

    # A LATER worker cycle: the timer does not re-search a terminal wish, the
    # row stays.
    before = PIPE.searches
    worker.run_cycle()
    check("a later cycle does not search the terminal wish again",
          PIPE.searches == before, f"{before} -> {PIPE.searches}")
    check("...and the row is still there, unchanged",
          any(w["id"] == wid and w["status"] == "not_found" for w in wishes.list_wishes())
          and row_for("wish", wid)[0] == "needs_attention", json.dumps(row_for("wish", wid)[1] or {}))

    # AN UNRELATED WISH in the same session keeps going.
    other = wishes.add_wish(OTHER_ID, title="Another Album", artist="An Artist",
                            source="soulseek")
    PIPE.found = True
    worker.run_cycle()
    # The queue may already be running a pass (an add starts one), in which case
    # this call hands its request to that pass instead of starting a second one:
    # what is asserted is that the wish IS processed, not which call did it.
    wait_until(lambda: wishes.get_wish(other["id"])["status"] == "imported",
               what="the unrelated wish to be imported")
    other_now = wishes.get_wish(other["id"])
    check("an unrelated wish still processes in the same session",
          other_now["status"] == "imported" and other_now["album_path"],
          json.dumps(other_now))
    check("...and the not-found wish was not disturbed by it",
          wishes.get_wish(wid)["status"] == "not_found"
          and wishes.get_wish(wid)["attempts"] == 1, json.dumps(wishes.get_wish(wid)))
    osection, orow = row_for("wish", other["id"])
    check("...its row moved to Completed with the import outcome",
          osection == "completed" and orow and orow["release"]["catalog_number"] == "CAT-9",
          json.dumps(orow or {}))

    # ONE PRESS: the row's retry re-arms the wish and searches it again.
    PIPE.searches = 0
    rr = client.post("/api/queue/retry", json={"id": f"wish:{wid}"})
    check("the row's retry re-arms the wish", rr.status_code == 200, rr.text[:200])
    rearmed = wishes.get_wish(wid)
    check("...clearing the spent budget (attempts + empty searches)",
          rearmed["attempts"] == 0 and rearmed["not_found"] == 0
          and rearmed["retry_at"] == 0 and not rearmed["last_error"],
          json.dumps(rearmed))
    wait_until(lambda: wishes.get_wish(wid)["status"] == "imported",
               what="the re-armed wish to be found and imported")
    check("...and the search it starts really runs (the album lands)",
          wishes.get_wish(wid)["status"] == "imported" and PIPE.searches >= 1,
          json.dumps({"status": wishes.get_wish(wid)["status"], "searches": PIPE.searches}))

# --------------------------------------------------------------------------- #
# 3. the budget `wishes_not_found_attempts` buys: it waits, then ends terminal
# --------------------------------------------------------------------------- #
print("\n== the not-found budget ==")

PIPE.found = False
CFG2 = dict(CFG, wishes_not_found_attempts=2)
with pipeline_patches(cfg=CFG2), SLSK, \
     Patch(intg, resolve_release=_resolve), Patch(worker, load_config=lambda: dict(CFG2)):
    w2 = wishes.add_wish(RARE_ID, title="Rare Album",
                         artist="An Artist", source="soulseek")
    worker.run_cycle(w2["id"])
    after1 = wishes.get_wish(w2["id"])
    check("one empty search of a two-search budget leaves it wanted, not terminal",
          after1["status"] == "wanted" and after1["not_found"] == 1
          and after1["attempts"] == 1 and not wishes.is_terminal(after1, CFG2),
          json.dumps(after1))
    check("...with the reason recorded and its next automatic search scheduled",
          "No candidate folder" in after1["last_error"]
          and wishes.due_at(after1, CFG2) > time.time(), json.dumps(after1))
    section, row = row_for("wish", w2["id"])
    check("...and the row is still queued, saying why it waits",
          section == "queued" and row and row["reason"] == after1["last_error"],
          json.dumps(row or {}))
    worker.run_cycle(w2["id"])
    after2 = wishes.get_wish(w2["id"])
    check("the last empty search of the budget ends it terminal, still listed",
          after2["status"] == "not_found" and after2["not_found"] == 2
          and wishes.is_terminal(after2, CFG2)
          and any(w["id"] == w2["id"] for w in wishes.list_wishes()),
          json.dumps(after2))
    check("...and it was never deleted at any point",
          wishes.get_wish(w2["id"]) is not None)

# --------------------------------------------------------------------------- #
# 3b. the SHIPPED policy: an empty search does not end anything
# --------------------------------------------------------------------------- #
print("\n== the shipped policy keeps looking ==")

SHIPPED = dict(cfgmod.DEFAULT_CONFIG)
SHIPPED["music_folder"] = MUSIC
check("the shipped not-found budget is 0 — a request is never given up on",
      wishes.not_found_attempts(SHIPPED) == 0,
      json.dumps({"wishes_not_found_attempts": SHIPPED.get("wishes_not_found_attempts")}))

STILL_ID = "66666666-1111-1111-1111-111111111111"
still = dict(REL, id=STILL_ID, release_group_id=STILL_ID, title="Still Looking")
w3 = wishes.add_wish(STILL_ID, title="Still Looking", artist="An Artist",
                     source="soulseek")
PIPE.found = False
with pipeline_patches(cfg=SHIPPED), SLSK, \
     Patch(intg, resolve_release=lambda mbid: (dict(still), STILL_ID)), \
     Patch(worker, load_config=lambda: dict(SHIPPED)):
    worker.run_cycle(w3["id"])
    after3 = wishes.get_wish(w3["id"])
    check("an empty search leaves the wish WANTED, not terminal",
          after3["status"] == "wanted" and not wishes.is_terminal(after3, SHIPPED),
          json.dumps(after3))
    check("...with the empty search recorded as a reason and a count, not an outcome",
          after3["not_found"] == 1 and "No candidate folder" in after3["last_error"],
          json.dumps({"not_found": after3["not_found"], "err": after3["last_error"]}))
    due3 = wishes.due_at(after3, SHIPPED)
    check("...and it is due again on its own interval — a real time, not never",
          due3 > time.time() and due3 != float("inf"),
          json.dumps({"due_at": due3, "now": time.time()}))
    section3, row3 = row_for("wish", w3["id"])
    check("...and its queue row is still QUEUED, saying it keeps looking",
          section3 == "queued" and "still looking" in (row3 or {}).get("note", "")
          and row3.get("retryable") is False,
          json.dumps({"section": section3, "note": (row3 or {}).get("note")}))
    check("...and a candidate folder that failed the CD check is the same, never gave up",
          wishes.outcome_of("No candidate folder contained every track (and cue/log "
                            "per disc for CD)") == "not_found"
          and not wishes.is_terminal(after3, SHIPPED))
    # The escape hatches stay: the user's own cancel ends it, their retry
    # restarts it, and a release that turns up in the library reconciles.
    check("...while the user can still end it by hand",
          row3.get("cancelable") is True, json.dumps(row3 or {}))

# --------------------------------------------------------------------------- #
# 4. a wish whose release cannot be resolved still renders
# --------------------------------------------------------------------------- #
print("\n== a wish nobody could look up ==")

w4 = wishes.add_wish("44444444-5555-6666-7777-888888888888", title="Unknown Pressing",
                     artist="An Artist", source="soulseek")
with Patch(intg, resolve_release=lambda mbid: (None, "")):
    filled = wishes.prime_identities(CFG)
check("an unresolvable release fills in nothing (and raises nothing)", filled == 0)
stored4 = wishes.get_wish(w4["id"])
check("...the wish keeps the documented block, with only the id it is keyed by",
      set(stored4["release"]) == set(wishes.RELEASE_KEYS)
      and stored4["release"]["id"] == "44444444-5555-6666-7777-888888888888"
      and not stored4["release"]["title"], json.dumps(stored4["release"]))
section4, row4 = row_for("wish", w4["id"])
check("...and the row renders with that empty block (no placeholder invented)",
      bool(row4) and set(row4["release"]) == set(wishes.RELEASE_KEYS)
      and not row4["release"]["catalog_number"], json.dumps(row4 or {}))

# A wish read out of a store that predates the block (no `release` column
# value at all) still gets the documented shape from the row builder.
with Patch(wishes, list_wishes=lambda: [{"id": 91, "release_mbid": "55555555-6666-7777-8888-999999999999",
                                         "title": "Old Row", "artist": "", "year": "",
                                         "status": "wanted", "attempts": 0, "not_found": 0,
                                         "retry_at": 0.0, "last_error": "", "added_at": 1.0,
                                         "updated_at": 1.0, "album_path": "", "source": ""}]), \
     Patch(auto, jobs=lambda: []), \
     Patch(slsk, ready_albums=lambda *a, **k: []):
    sections = api_queue.build_queue(dict(CFG))["sections"]
    old = sections["queued"][0] if sections["queued"] else {}
    check("a wish row from an older store still carries the documented block",
          set(old.get("release") or {}) == set(wishes.RELEASE_KEYS), json.dumps(old))

# --------------------------------------------------------------------------- #
# 5. clearing finished rows: exactly the terminal ones, and nothing else
# --------------------------------------------------------------------------- #
print("\n== clearing finished rows ==")

PIPE.found = False
with pipeline_patches(), SLSK, Patch(intg, resolve_release=_resolve), \
     Patch(worker, load_config=lambda: dict(CFG)):
    # Fresh releases: `add_wish` is idempotent by release id, and the ones
    # above have already been through their own outcomes.
    wanted = wishes.add_wish("77777777-1111-1111-1111-111111111111",
                             title="Still Wanted", artist="An Artist",
                             source="soulseek")
    done = wishes.add_wish("77777777-2222-2222-2222-222222222222",
                           title="Already In", artist="An Artist", source="soulseek")
    wishes.mark_imported(done["id"], os.path.join(MUSIC, "Artists", "Already In"))
    terminal = wishes.add_wish("77777777-3333-3333-3333-333333333333",
                               title="Nothing Anywhere", artist="An Artist",
                               source="soulseek")
    wishes.mark_not_found(terminal["id"], "No candidate folder contained every track", 3)

    # A real FAILED job (its own row) and a real job parked on a question — the
    # second one is still live and must survive the clear.
    failed = auto.start_job(release=dict(REL), confirm_lossy=False,
                            source="soulseek")
    parked = auto.start_job(release=dict(RARE_REL), confirm_lossy=True,
                            source="musicbrainz")
    parked_id = parked["job"]["id"]
    wait_until(lambda: auto.job_state(parked_id).get("state") == "confirm",
               what="the job to park on its question")
    wait_until(lambda: auto.job_state(failed["job"]["id"]).get("state") in ("done", "error"),
               what="the nothing-found job to settle")

    before = {r["id"]: r for r in
              [x for rows in queue()["sections"].values() for x in rows]}
    check("the queue holds the live rows and the finished ones",
          all(x in before for x in (f"wish:{wanted['id']}", f"wish:{terminal['id']}",
                                    f"wish:{done['id']}", f"job:{parked_id}",
                                    f"job:{failed['job']['id']}")), sorted(before))
    check("...a live wish and a parked job are NOT clearable",
          before[f"wish:{wanted['id']}"]["clearable"] is False
          and before[f"job:{parked_id}"]["clearable"] is False,
          json.dumps({k: before[k]["clearable"] for k in
                      (f"wish:{wanted['id']}", f"job:{parked_id}")}))
    check("...while the finished ones are",
          all(before[x]["clearable"] for x in
              (f"wish:{terminal['id']}", f"wish:{done['id']}",
               f"job:{failed['job']['id']}")),
          json.dumps({k: before[k]["clearable"] for k in
                      (f"wish:{terminal['id']}", f"wish:{done['id']}")}))

    # The live rows are REFUSED, by name.
    r = client.post("/api/queue/clear", json={"id": f"wish:{wanted['id']}"})
    check("clearing a wish that is still wanted is refused, with the alternative named",
          r.status_code == 409 and "cancel it instead" in r.text, r.text[:200])
    r = client.post("/api/queue/clear", json={"id": f"job:{parked_id}"})
    check("clearing a job still in the pipeline is refused too",
          r.status_code == 409 and "cancel it instead" in r.text, r.text[:200])

    # "Clear finished" takes EXACTLY the terminal rows — whatever else the
    # queue happens to be holding from this session included.
    finished_ids = {rid for rid, row in before.items() if row["clearable"]}
    live_ids = set(before) - finished_ids
    check("...and the two kinds add up to the whole queue",
          {f"wish:{terminal['id']}", f"wish:{done['id']}",
           f"job:{failed['job']['id']}"} <= finished_ids
          and {f"wish:{wanted['id']}", f"job:{parked_id}"} <= live_ids,
          json.dumps({"finished": sorted(finished_ids), "live": sorted(live_ids)}))
    live_wish_ids = {w["id"] for w in wishes.list_wishes() if not w["terminal"]}
    r = client.post("/api/queue/clear", json={"scope": "finished"})
    body = r.json()
    check("clear finished reports how many rows it removed, and which",
          r.status_code == 200 and body["cleared"] == len(finished_ids)
          and set(body["ids"]) == finished_ids, r.text[:300])
    left = {x["id"] for rows in queue()["sections"].values() for x in rows}
    check("...leaving the live rows alone (wanted wish, parked job)",
          left == live_ids, json.dumps(sorted(left)))
    check("...and the wish store agrees: the terminal wishes are gone, the live ones are not",
          {w["id"] for w in wishes.list_wishes()} == live_wish_ids,
          json.dumps({"left": [w["id"] for w in wishes.list_wishes()],
                      "expected": sorted(live_wish_ids)}))

    # One row, by hand: a wish the user is done with goes, and only it.
    again = wishes.add_wish("77777777-4444-4444-4444-444444444444", title="Gone Too",
                            artist="An Artist", source="soulseek")
    wishes.mark_not_found(again["id"], "nothing out there", 3)
    r = client.post("/api/queue/clear", json={"id": f"wish:{again['id']}"})
    check("one terminal row clears on its own",
          r.status_code == 200 and r.json()["cleared"] == 1
          and wishes.get_wish(again["id"]) is None, r.text[:200])
    check("...and the other live wish is untouched",
          wishes.get_wish(wanted["id"]) is not None
          and wishes.get_wish(wanted["id"])["status"] == "wanted")
    r = client.post("/api/queue/clear", json={"id": "wish:999999"})
    check("clearing a row that is not there answers 404", r.status_code == 404, r.text[:120])
    auto.cancel(parked_id)                   # do not leave the parked job waiting

shutil.rmtree(REDIRECT, ignore_errors=True)
print(f"\n{len(FAILED)} failure(s)")
sys.exit(1 if FAILED else 0)
