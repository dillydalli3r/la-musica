#!/usr/bin/env python3
"""Regression: a download QUEUED FROM THE SOULSEEK PAGE imports itself — the
whole of it, once, and across a restart.

`server/main.py`'s page-download section records what the page's Download
button asked slskd for and imports the album those transfers produce. Three
things it must not do:

* import an album from a PARTIAL set. The intent carries the files and the
  sizes the press asked for, so a folder holding only some of them (or one of
  them half written) is a download still coming: importing it would chain and
  grade the album from part of itself, and the files that land afterwards would
  arrive into a folder nothing is watching any more — the intent would have
  been spent on the first import;
* forget the intent across a restart. The backend restarts on every config save
  and every update, and a transfer that arrives after one would import nothing,
  which is the second press this whole feature exists to remove;
* queue the same files twice. slskd's enqueue does not dedupe: a re-press would
  open a SECOND batch for files a live transfer already covers, download the
  album over again, and (with the page's own auto-import) import it twice.

The three download routes are exercised through the real app (fastapi's
TestClient) with slskd stubbed at `server.soulseek`'s own seams, and the import
runner replaced by a recorder — the pass's real work (which intents are whole,
which folders are ready, which files an import took over) runs unchanged. The
music folder, the download folder and the intent store all live in a temp
directory.

Run:  python tools/test_soulseek_page_downloads.py
"""
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import server.import_queue as import_queue
import server.main as mlo_main
from server import soulseek

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{('  — ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILED.append(name)


PEER = "pagepeer"
BATCH = "20202"
REMOTE = "Music/Page Album"
NAMES = ("01 - one.wav", "02 - two.wav", "03 - three.wav")
SIZES = (4000, 4200, 4400)          # the sizes the browse/slskd reported
TMP = tempfile.mkdtemp(prefix="mlo_page_dl_")
MF = os.path.join(TMP, "Music")     # also holds .mlo/data (the intent store)
DD = os.path.join(TMP, "downloads")
ALBUM = os.path.join(DD, PEER, BATCH, *REMOTE.split("/"))
CFG = {"music_folder": MF, "soulseek_download_dir": DD,
       "import_autonomy": "automatic", "manual_import_enabled": True}
FILES = [{"filename": f"{REMOTE}/{name}", "size": size}
         for name, size in zip(NAMES, SIZES)]
EXTRA = {"filename": f"{REMOTE}/04 - four.wav", "size": 4600}

client = TestClient(mlo_main.app)
enqueued = []          # what the routes really asked slskd to queue
started = []           # what the pass really handed the import runner
transfers = {"files": [], "state": "InProgress"}

_real = {"load": mlo_main.load_config, "running": soulseek.is_running,
         "web": soulseek.web_up, "enqueue": soulseek.enqueue_download,
         "state": soulseek.downloads_state, "ready": soulseek.ready_albums,
         "iq_running": import_queue.running, "iq_start": import_queue.start}


def write(name, size, folder=ALBUM):
    """A file of exactly `size` bytes, where slskd would leave it."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(b"RIFF" + b"\0" * max(0, size - 4))
    return path


def transfer_tree():
    """slskd's transfer tree for what the last press asked for."""
    done = transfers["state"] != "InProgress"
    return [{"username": PEER, "directories": [{
        "directory": REMOTE.replace("/", "\\"),
        "files": [{"id": f"t{i}", "filename": f["filename"], "size": f["size"],
                   "state": transfers["state"],
                   "bytesTransferred": f["size"] if done else 0}
                  for i, f in enumerate(transfers["files"])]}]}]


def watcher_running():
    """`import_queue.running()` for the background watcher's OWN ticks only.

    server/main.py starts a thread that runs the page-download pass every few
    seconds. That tick would race these assertions (it starts the same import
    this suite drives by hand), so a call from any thread but this one answers
    "an import is already running" and the pass returns before it looks at an
    intent. The suite's own calls — which all happen on the main thread — get
    the truth."""
    return threading.current_thread() is not threading.main_thread()


def fake_enqueue(username, wanted, cfg=None):
    """slskd's enqueue: the batch appears in the transfer tree it reports."""
    enqueued.append((username, [dict(f) for f in wanted]))
    queued = {str(f["filename"]) for f in wanted}
    transfers["files"] = ([dict(f) for f in wanted]
                          + [f for f in transfers["files"]
                             if f["filename"] not in queued])


def install(cfg, files=(), state="InProgress"):
    """The process image a restart leaves, with slskd stubbed at its seams."""
    mlo_main.load_config = lambda: dict(cfg)
    mlo_main._PAGE_DOWNLOADS[:] = []
    mlo_main._PAGE_STORE.update(path="", loaded=False)
    try:
        os.remove(mlo_main._page_store_path(cfg))   # a store from an older case
    except OSError:
        pass
    enqueued.clear()
    started.clear()
    transfers.update(files=[dict(f) for f in files], state=state)
    soulseek.is_running = lambda *a, **k: True
    soulseek.web_up = lambda *a, **k: True
    soulseek.enqueue_download = fake_enqueue
    soulseek.downloads_state = lambda *a, **k: transfer_tree()
    soulseek.ready_albums = lambda *a, **k: [ALBUM] if os.path.isdir(ALBUM) else []
    import_queue.running = watcher_running
    import_queue.start = lambda paths=None, **kw: (
        started.append(list(paths or [])) or {"ok": True})


def press(path="/api/soulseek/download", files=FILES):
    return client.post(path, json={"username": PEER, "files": files})


# --------------------------------------------------------------------------- #
print("\nwhole or nothing: an intent imports only once ALL of it has arrived")
# --------------------------------------------------------------------------- #
install(CFG)
res = press()
check("the press queues every file", res.json() == {"ok": True, "queued": 3,
                                                    "skipped": 0}, res.text)
check("...and is remembered as a download to import",
      len(mlo_main._page_intents()) == 1)

for name, size in zip(NAMES[:2], SIZES[:2]):
    write(name, size)           # two of the three files land; the rest is queued
check("a folder holding PART of what was queued does not import",
      mlo_main._page_download_pass() is False)
check("...nothing was handed to the import runner", started == [], str(started))
check("...and the intent is kept for the arrival",
      len(mlo_main._page_intents()) == 1)

write(NAMES[2], SIZES[2] // 2)  # the third arrives, still being written
check("a file at another SIZE is not the file that was asked for",
      not mlo_main._page_file_arrived({"filename": NAMES[2], "size": SIZES[2]},
                                      os.path.join(ALBUM, NAMES[2])))
check("...so a half-written file does not import the album either",
      mlo_main._page_download_pass() is False)
check("...with nothing handed to the import runner", started == [], str(started))

write(NAMES[2], SIZES[2])       # ...and now it really has arrived
check("the whole set imports itself", mlo_main._page_download_pass() is True)
check("...the album as a whole", started == [[ALBUM]], str(started))
check("...and the intent is spent", mlo_main._page_intents() == [])

# --------------------------------------------------------------------------- #
print("\na restart: the intent outlives the process that wrote it")
# --------------------------------------------------------------------------- #
install(CFG)
res = press()
store = mlo_main._page_store_path(CFG)
check("the intent is durably written beside the app's own state",
      os.path.isfile(store), store)
for name, size in zip(NAMES, SIZES):
    write(name, size)           # the bytes land while the backend restarts
# what a restart leaves: the in-memory list gone, the disk record still there
mlo_main._PAGE_DOWNLOADS[:] = []
mlo_main._PAGE_STORE.update(path="", loaded=False)
check("the in-memory list really is empty after the restart",
      mlo_main._PAGE_DOWNLOADS == [])
check("...and the download is still known",
      [i["username"] for i in mlo_main._page_intents()] == [PEER],
      str(mlo_main._page_intents()))
check("the download that arrived after the restart imports itself",
      mlo_main._page_download_pass() is True)
check("...the album as a whole", started == [[ALBUM]], str(started))
check("...and the reloaded intent is spent", mlo_main._page_intents() == [])

# --------------------------------------------------------------------------- #
print("\na re-press: a file already on its way is never queued twice")
# --------------------------------------------------------------------------- #
install(CFG)
first = press()
check("the first press queues the album",
      first.json() == {"ok": True, "queued": 3, "skipped": 0}, first.text)
again = press()
check("a second press queues NOTHING", again.json().get("queued") == 0, again.text)
check("...and says what it left alone", again.json().get("skipped") == 3, again.text)
check("...so slskd was asked exactly once",
      [len(f) for _u, f in enqueued] == [3], str(enqueued))
check("...and only the first press recorded an intent",
      len(mlo_main._page_intents()) == 1)

third = press(files=FILES + [EXTRA])
check("a press with one NEW file queues just that one",
      third.json() == {"ok": True, "queued": 1, "skipped": 3}, third.text)

transfers["files"] = FILES + [EXTRA]      # what slskd now holds
bulk = client.post("/api/soulseek/download-bulk",
                   json={"username": PEER, "files": FILES + [EXTRA]})
check("the bulk route merges a re-press by the same rule",
      bulk.json() == {"queued": 0, "skipped": 4}, bulk.text)
check("...so nothing extra was enqueued",
      [len(f) for _u, f in enqueued] == [3, 1], str(enqueued))

# a transfer that FINISHED is not "already queued": re-asking for the file is a
# new download the user asked for, not a duplicate of a live one
transfers["state"] = "Completed, Succeeded"
fresh = press()
check("a file whose transfer finished is queueable again",
      fresh.json().get("queued") == 3, fresh.text)

# --------------------------------------------------------------------------- #
# The manual search: free text, or an MBID (one TRACK, several parallel searches)
# --------------------------------------------------------------------------- #
# A user who is missing ONE song has its id (the library stores each track's
# recording MBID) and nothing else worth typing. `POST /api/soulseek/search`
# takes that id, resolves it with the cached MusicBrainz client, and starts ONE
# slskd search PER QUERY at once — the same "all at once, poll in one loop"
# shape the auto-importer uses, so the wall time is one window. The answer's id
# names them all, so the existing poll (one key, one merged payload) and the
# existing per-file download work unchanged. Nothing here touches the wishes or
# the queue: the user presses download on what the results show.
from server import integrations as intg          # noqa: E402
from server import soulseek_auto                 # noqa: E402
from server import wishes                        # noqa: E402

RECORDING = "1f2e3d4c-5b6a-4798-8a9b-0c1d2e3f4a5b"
RELEASE_ID = "9a8b7c6d-5e4f-4321-8abc-def012345678"
stated_queries = []          # what the route POSTed, in order
cancelled_ids = []           # what stop really cancelled
search_results_map = {}
_real_search = soulseek.search
_real_search_results = soulseek.search_results
_real_cancel_search = soulseek.cancel_search

# Each query answers with its own peers, and TWO of them answer with the same
# peer+file — one peer is one peer however many queries saw it.
PER_QUERY_ROWS = {
    "sid1": [("peerA", "Music/One.flac"), ("peerA", "Music/One.flac")],
    "sid2": [("peerB", "Music/Two.flac")],
    "sid3": [("peerA", "Music/One.flac"), ("peerC", "Music/Three.flac")],
}


def fake_search(query, cfg=None, timeout_ms=None, response_limit=None):
    stated_queries.append(query)
    sid = f"sid{len(stated_queries)}"
    search_results_map[sid] = {
        "state": "Completed", "isComplete": True,
        "responses": [{"username": u, "file": f, "size": 10, "bitrate": 900,
                       "duration": 100, "vbr": False, "slot": True,
                       "speed": 1000, "queue": 0, "ext": "flac"}
                      for u, f in PER_QUERY_ROWS.get(sid, [])]}
    return sid


def fake_search_results(sid, cfg=None):
    res = search_results_map.get(sid) or {
        "state": "Completed", "isComplete": True, "responseCount": 0,
        "fileCount": 0, "responses": []}
    return {**res,
            "responseCount": len({r["username"] for r in res["responses"]}),
            "fileCount": len(res["responses"])}


def fake_cancel_search(sid, cfg=None):
    cancelled_ids.append(sid)
    return True


RECORDING_MB = {
    "id": RECORDING, "title": "One Song",
    "artist-credit": [{"name": "The Artist", "joinphrase": ""}],
    "releases": [{"id": RELEASE_ID, "title": "Some Album"}],
}
RELEASE_MB = {
    "id": RELEASE_ID, "title": "Some Album", "date": "1999",
    "country": "GB", "catalog_number": "CAT-9", "medium_formats": ["CD"],
    "artists": [{"name": "The Artist", "mbid": "aaaa1111-0000-0000-0000-000000000000"}],
}
_real_mb_cached = intg.mb_get_cached
_real_resolve = intg.resolve_release


def fake_mb_cached(endpoint, params=None, timeout=30.0, retries=5):
    if endpoint == f"recording/{RECORDING}":
        return RECORDING_MB
    return {"aliases": []}


def fake_resolve(mbid):
    rid = str(mbid or "").strip().lower()
    if rid == RELEASE_ID:
        return dict(RELEASE_MB), RELEASE_ID
    return None, ""


install(CFG)
soulseek.search = fake_search
soulseek.search_results = fake_search_results
soulseek.cancel_search = fake_cancel_search
intg.mb_get_cached = fake_mb_cached
intg.resolve_release = fake_resolve
try:
    started_wishes = len(wishes.list_wishes())
    stated_queries.clear()
    by_mbid = client.post("/api/soulseek/search", json={"mbid": RECORDING})
    check("an MBID starts one search PER query, all at once",
          by_mbid.status_code == 200 and stated_queries == [
              "The Artist One Song", "Some Album", RECORDING], (by_mbid.text, stated_queries))
    check("...and the answer names every one of them as one poll key",
          by_mbid.json()["id"] == "sid1,sid2,sid3"
          and by_mbid.json()["ids"] == ["sid1", "sid2", "sid3"]
          and by_mbid.json()["queries"] == stated_queries, by_mbid.text)
    check("...saying which track and which id it resolved",
          by_mbid.json()["kind"] == "track"
          and by_mbid.json()["label"] == "The Artist — One Song", by_mbid.text)
    polled = client.get(f"/api/soulseek/search/{by_mbid.json()['id']}")
    check("the poll merges every query's results into the shape the page draws",
          polled.status_code == 200 and polled.json()["isComplete"] is True
          and polled.json()["fileCount"] == 3 and polled.json()["responseCount"] == 3,
          polled.text)
    _rows = polled.json()["responses"]
    check("...deduping a peer+file two queries both found",
          len(_rows) == 3 and sum(1 for r in _rows if r["file"] == "Music/One.flac") == 1,
          _rows)
    check("...and the per-file download the page already has takes them",
          all(set(("username", "file", "size")) <= set(r)
              for r in polled.json()["responses"]), polled.text)
    stopped = client.post("/api/soulseek/search/cancel",
                          json={"id": by_mbid.json()["id"]})
    check("stopping an MBID search cancels EVERY query it started",
          stopped.status_code == 200 and cancelled_ids == ["sid1", "sid2", "sid3"],
          (stopped.text, cancelled_ids))
    # A RELEASE id is the release case: its own query set, the release's id
    # included (the MBID-driven queries are on by default).
    stated_queries.clear()
    rel = client.post("/api/soulseek/search", json={"mbid": RELEASE_ID})
    check("a release MBID searches the release's own query set",
          rel.status_code == 200 and stated_queries == ["CAT-9", RELEASE_ID],
          (rel.text, stated_queries))
    check("...and says it was a release, not a track",
          rel.json()["kind"] == "release" and rel.json()["label"] == "The Artist — Some Album",
          rel.text)
    # Free text is untouched: one query, one search, the same id shape.
    stated_queries.clear()
    free = client.post("/api/soulseek/search", json={"query": "The Artist — One Song"})
    check("a typed query still runs exactly one search",
          free.status_code == 200 and stated_queries == ["The Artist — One Song"]
          and free.json()["id"] == "sid1", (free.text, stated_queries))
    # An id MusicBrainz does not know, and a string that is not an id at all,
    # both answer with the reason instead of searching for the text.
    stated_queries.clear()
    unknown = client.post("/api/soulseek/search",
                          json={"mbid": "00000000-0000-0000-0000-000000000000"})
    check("an unknown MBID says so and searches nothing",
          unknown.status_code == 404 and not stated_queries, (unknown.text, stated_queries))
    not_an_id = client.post("/api/soulseek/search", json={"mbid": "not a uuid"})
    check("a non-UUID MBID is refused, never searched as text",
          not_an_id.status_code == 404 and not stated_queries, not_an_id.text)
    check("…and the manual search added NOTHING to the wishes or the queue",
          len(wishes.list_wishes()) == started_wishes
          and not soulseek_auto.queued() and not soulseek_auto.jobs(),
          (len(wishes.list_wishes()), soulseek_auto.jobs()))
finally:
    intg.mb_get_cached = _real_mb_cached
    intg.resolve_release = _real_resolve
    soulseek.search = _real_search
    soulseek.search_results = _real_search_results
    soulseek.cancel_search = _real_cancel_search

# --------------------------------------------------------------------------- #
mlo_main.load_config = _real["load"]
soulseek.is_running = _real["running"]
soulseek.web_up = _real["web"]
soulseek.enqueue_download = _real["enqueue"]
soulseek.downloads_state = _real["state"]
soulseek.ready_albums = _real["ready"]
import_queue.running = _real["iq_running"]
import_queue.start = _real["iq_start"]
shutil.rmtree(TMP, ignore_errors=True)

print()
if FAILED:
    print(f"{len(FAILED)} check(s) FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all checks passed")
