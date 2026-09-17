#!/usr/bin/env python3
"""Pin the Soulseek candidate ranking, search polling and download waiting.

Auto-import used to report "No candidate folder contained every track" for
searches that had hundreds of hits, it offered a perfect-scoring MP3 folder
ahead of a lossless one, it ran its query templates one after another (up to
three minutes before the first byte), it accepted a file merely because
something with that name lay on disk, and it let a queue that never moved hold
a candidate for the full timeout. Pinned here:

  * find_candidates() ranks lossless folders first (score only breaks ties),
  * _search_queries() POSTs every template up front and polls them in ONE
    loop, stopping the moment the merged responses hold a complete lossless
    folder; the window reaches slskd as MILLISECONDS (slskd passes
    searchTimeout straight to Soulseek.NET, so a seconds-style number ends
    the search at once and yields no responses), and Errored /
    never-terminating searches are REPORTED instead of looking like an empty
    result set,
  * _wait_for_files() accepts a file only when it exists at the expected size
    AND slskd reports a successful terminal state for it, publishes the
    download progress payload each tick, and drops a transfer that has moved
    no bytes for the stall window,
  * _verify_album() fails a CD rip it could not check a single CRC of,
  * confirm_lossy()/cancel() only answer a prompt that is actually pending.

Run:  python tools/test_soulseek_candidates.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time as real_time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import discs as mlo_discs, tools as mlo_tools

from server import soulseek, soulseek_auto


# --------------------------------------------------------------------------- #
# Synthetic slskd search results / MusicBrainz release
# --------------------------------------------------------------------------- #
def row(user, path, slot=True, queue=0, duration=200.0):
    return {"username": user, "file": path, "size": 30_000_000,
            "duration": duration, "slot": slot, "speed": 1_000_000,
            "queue": queue, "ext": os.path.splitext(path)[1].lstrip(".").lower()}


def folder(user, root, ext, logs=0, slot=True, queue=0):
    """A complete 2-track folder (01 - A, 02 - B) plus optional rip logs."""
    out = [row(user, f"Music/{root}/01 - A.{ext}", slot=slot, queue=queue),
           row(user, f"Music/{root}/02 - B.{ext}", slot=slot, queue=queue)]
    out += [row(user, f"Music/{root}/rip {i}.log", slot=slot, queue=queue)
            for i in range(1, logs + 1)]
    return out


RELEASE = {
    "id": "11111111-2222-3333-4444-555555555555",
    "title": "Test Album",
    "media": [{"disc": 1, "position": 1, "title": "A", "length": 200000},
              {"disc": 1, "position": 2, "title": "B", "length": 210000}],
    "medium_formats": ["Digital Media"],
    "artists": [{"name": "Test Artist", "mbid": "a1b2c3d4-0000-0000-0000-000000000000"}],
}
CFG = {}


# 1. A lossy folder that scores higher still ranks behind a lossless one:
#    the download is the irreversible part, so codec beats score. (The lossy
#    folder's score comes from its free slot + cue: extra junk logs no longer
#    score at all — one log per disc is what counts.)
lossy = folder("mp3user", "Album MP3", "mp3", logs=4, queue=0)
lossy.append(row("mp3user", "Music/Album MP3/Album.cue"))
lossless = folder("flacuser", "Album FLAC", "flac", logs=0, slot=False, queue=500)
cands = soulseek_auto.find_candidates(lossy + lossless, RELEASE, CFG)
assert len(cands) == 2, [c["dir"] for c in cands]
assert cands[0]["lossless"] is True, cands[0]["lossless"]
assert cands[0]["dir"] == "Music/Album FLAC/", cands[0]["dir"]
assert cands[1]["lossless"] is False, cands[1]["lossless"]
assert cands[1]["dir"] == "Music/Album MP3/", cands[1]["dir"]
# ...and the ranking really did override the score, not agree with it.
assert cands[1]["score"] > cands[0]["score"], [c["score"] for c in cands]
assert cands[0]["complete"] and cands[1]["complete"], [c["complete"] for c in cands]
# the four raw logs are NOT four scored logs: one per disc is selected.
assert len(cands[1]["logs"]) == 1, cands[1]["logs"]


def only_lossless(results):
    c = soulseek_auto.find_candidates(results, RELEASE, CFG)
    assert len(c) == 1, [x["dir"] for x in c]
    return c[0]["lossless"]


# 2. Codec buckets: mp3 and m4a/AAC are lossy, wav is lossless.
assert only_lossless(folder("u1", "A", "mp3")) is False, "mp3 must be lossy"
assert only_lossless(folder("u2", "A", "m4a")) is False, "m4a/AAC must be lossy"
assert only_lossless(folder("u3", "A", "wav")) is True, "wav must be lossless"
assert only_lossless(folder("u4", "A", "flac")) is True, "flac must be lossless"


# --------------------------------------------------------------------------- #
# _local_download_candidates: the two layouts slskd leaves on disk
# --------------------------------------------------------------------------- #
REMOTE = "share\\Music\\Album\\02 - bad guy.flac"
_case_root = tempfile.mkdtemp(prefix="mlo-download-cases-")


def put_file(*parts, body=b"x"):
    p = os.path.join(*parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(body)
    return p


try:
    def norm(paths):
        """Same file, one spelling per path.

        _local_download_candidates builds its exact-shape paths with a
        forward-slash relative part, so on Windows the same file shows up a
        second time from the os.walk basename scan spelled with backslashes."""
        return sorted({os.path.normpath(p) for p in paths})

    # 7. FLAT LAYOUT (slskd 0.26): <ddir>/<remote folder>/<file>, no username.
    flat_dir = os.path.join(_case_root, "flat")
    flat = put_file(flat_dir, "share", "Music", "Album", "02 - bad guy.flac")
    assert not os.path.exists(os.path.join(flat_dir, "peeruser")), \
        f"flat case must have no <ddir>/<username> dir: {os.listdir(flat_dir)}"
    got = soulseek_auto._local_download_candidates(flat_dir, "peeruser", REMOTE, 0)
    assert norm(got) == norm([flat]), \
        f"flat layout (no username segment) missed {REMOTE}: got {got}"

    # 8. NESTED LAYOUT (older builds / per-user downloads):
    #    <ddir>/<username>/share/Music/Album/<file>.
    nested_dir = os.path.join(_case_root, "nested")
    nested = put_file(nested_dir, "peeruser", "share", "Music", "Album",
                      "02 - bad guy.flac")
    got = soulseek_auto._local_download_candidates(nested_dir, "peeruser", REMOTE, 0)
    assert norm(got) == norm([nested]), \
        f"nested layout (<ddir>/<username>/<rel>) missed {REMOTE}: got {got}"

    # 9. Partials under .incomplete are never a completed download.
    partial_dir = os.path.join(_case_root, "partial")
    put_file(partial_dir, ".incomplete", "share", "Music", "Album",
             "02 - bad guy.flac")
    got = soulseek_auto._local_download_candidates(partial_dir, "peeruser", REMOTE, 0)
    assert got == [], f".incomplete partial offered as a completed file: got {got}"

    # 10. Size filter: a mismatched size means a different/truncated file, but
    #     size=0 means the peer never reported one, so the file still counts.
    #     The basename fallback only walks the candidate's OWN album tree
    #     (<ddir>/<leaf> and <ddir>/<user>/<leaf>), so a file a level deeper
    #     under it is still found...
    size_dir = os.path.join(_case_root, "size")
    sized = put_file(size_dir, "Album", "sub dir", "02 - bad guy.flac", body=b"x" * 5)
    got = soulseek_auto._local_download_candidates(size_dir, "peeruser", REMOTE, 999)
    assert got == [], f"size mismatch still returned {got} for on-disk size {os.path.getsize(sized)}"
    got = soulseek_auto._local_download_candidates(size_dir, "peeruser", REMOTE, 0)
    assert norm(got) == norm([sized]), f"unknown size (size=0) dropped a real file: got {got}"

    # ...while a same-named file from ANOTHER album (outside the candidate's
    # leaf tree) must never satisfy the pending download.
    foreign_dir = os.path.join(_case_root, "foreign")
    put_file(foreign_dir, "Other Album", "02 - bad guy.flac")
    got = soulseek_auto._local_download_candidates(foreign_dir, "peeruser", REMOTE, 0)
    assert got == [], f"a same-named file of another album satisfied the download: {got}"
    own = put_file(foreign_dir, "Album", "CD1", "02 - bad guy.flac")
    got = soulseek_auto._local_download_candidates(foreign_dir, "peeruser", REMOTE, 0)
    assert norm(got) == norm([own]), got

    # 10c. A file slskd renamed/removed between the walk and the size read
    #      counts as not-yet-present: os.path.getsize used to raise straight
    #      out of the job.
    gone = os.path.join(size_dir, "Album", "sub dir", "vanished.flac")
    got = soulseek_auto._local_download_candidates(
        size_dir, "peeruser", "share/Music/Album/vanished.flac", 5,
        index={"vanished.flac": [gone]})
    assert got == [], got
    #      ...and the prebuilt index really is what answers the walk, once
    #      for the whole tick instead of once per pending file.
    again = soulseek_auto._local_download_candidates(
        size_dir, "peeruser", REMOTE, 0,
        index=soulseek_auto._index_download_tree(size_dir, "peeruser", ["Album"]))
    assert norm(again) == norm([sized]), again

    # 10b. _local_album_root: the highest directory BELOW the download dir
    #      holding every file of the candidate — both disc folders of a
    #      multi-disc tree resolve to one album root...
    split = put_file(_case_root, "multi", "Album", "CD1", "01 a.flac")
    split2 = put_file(_case_root, "multi", "Album", "CD2", "01 b.flac")
    assert soulseek_auto._local_album_root(os.path.join(_case_root, "multi"),
                                           [split, split2]) == \
        os.path.join(_case_root, "multi", "Album"), "multi-disc tree lost its album root"
    #      ...and files split over two TOP-LEVEL folders have no such root,
    #      so the caller rejects instead of half-verifying one of them.
    top1 = put_file(_case_root, "split", "Album CD1", "01 a.flac")
    top2 = put_file(_case_root, "split", "Album CD2", "01 b.flac")
    assert soulseek_auto._local_album_root(os.path.join(_case_root, "split"),
                                           [top1, top2]) is None, \
        "files spread over two top-level folders must not resolve to one album root"
    assert soulseek_auto._local_album_root(os.path.join(_case_root, "split"),
                                           [top1]) == os.path.dirname(top1)
finally:
    shutil.rmtree(_case_root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# _search_queries: one loop, every template POSTed up front
# --------------------------------------------------------------------------- #
class FakeClock:
    """Every sleep advances the clock, so multi-minute poll windows run instantly."""

    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def strftime(self, *a, **k):
        return real_time.strftime(*a, **k)


class FakeSlsk:
    """slskd stub: scripts a sequence of poll DTOs; is_search_done/search_error
    delegate to the real implementation so the state parsing stays under test."""

    is_search_done = staticmethod(soulseek.is_search_done)
    search_error = staticmethod(soulseek.search_error)

    def __init__(self, polls):
        self.polls = list(polls)
        self.timeouts = []
        self.polls_made = 0

    def search(self, query, timeout_ms=None):
        self.timeouts.append(timeout_ms)
        return "sid-1"

    def search_results(self, sid):
        assert sid == "sid-1", sid
        self.polls_made += 1
        # keep handing back the last scripted poll once it is exhausted
        return self.polls[min(self.polls_made, len(self.polls)) - 1]


class MultiSearchSlsk:
    """slskd stub for N simultaneous searches: one poll script per query.

    Every query gets its own search id, so "did template B keep running after
    template A already found the album?" is answerable."""

    is_search_done = staticmethod(soulseek.is_search_done)
    search_error = staticmethod(soulseek.search_error)

    def __init__(self, scripts):
        self.scripts = dict(scripts)
        self.posted, self.polls = [], {}

    def search(self, query, timeout_ms=None):
        self.posted.append(query)
        return f"sid-{len(self.posted)}"

    def search_results(self, sid):
        query = self.posted[int(sid.split("-")[1]) - 1]
        polls = self.scripts[query]
        n = self.polls.get(sid, 0)
        self.polls[sid] = n + 1
        return polls[min(n, len(polls) - 1)]


RESPONSES = [{"username": "u", "file": f"Music/A/0{i} - T.flac"} for i in (1, 2, 3)]
PENDING = {"state": "InProgress", "isComplete": False, "responses": []}
DONE = {"state": "Completed", "isComplete": True, "responses": RESPONSES}

clock = FakeClock()
real_time_module = soulseek_auto.time
soulseek_auto.time = clock
try:
    # 3. Two InProgress polls, then complete: the 3 responses must come back,
    #    and the loop stops the moment the search is terminal instead of
    #    waiting out the window (or a 2s tail) it no longer needs.
    slsk = FakeSlsk([PENDING, PENDING, DONE])
    results, errors, skipped = soulseek_auto._search_queries(slsk, ["q"], wait_s=6)
    assert [q for q, _r in results] == ["q"], results
    assert results[0][1] == DONE, results
    assert errors == [] and skipped == 0, (errors, skipped)
    assert slsk.polls_made >= 3, slsk.polls_made
    assert clock.now < 6, f"a finished search still waited out its window (t={clock.now}s)"
    # slskd hands searchTimeout to Soulseek.NET as MILLISECONDS: 6 s -> 6000.
    # A seconds-style 6 ends the search almost immediately and returns nothing.
    assert slsk.timeouts == [6000], slsk.timeouts

    # 4. A search that never terminates is REPORTED, never read as "no hits".
    clock.now = 0.0
    results, errors, skipped = soulseek_auto._search_queries(
        FakeSlsk([PENDING]), ["never-done"], wait_s=2)
    assert results == [] and skipped == 0, (results, skipped)
    assert any("never-done" in e and "did not finish" in e for e in errors), errors

    # 5. An Errored search is reported as an error, not an empty result set.
    clock.now = 0.0
    errored = {"state": "Errored", "isComplete": True, "responses": []}
    results, errors, _skipped = soulseek_auto._search_queries(
        FakeSlsk([errored]), ["errored-query"], wait_s=2)
    assert results == [], results
    assert any("Errored" in e and "errored-query" in e for e in errors), errors

    # 5b. PARALLEL: every template is POSTed at once and the first complete
    #     lossless folder ends the wait for all of them — the slow template
    #     never costs a second full window.
    complete = {"state": "Completed", "isComplete": True,
                "responses": folder("peer", "Album FLAC", "flac")}
    clock.now = 0.0
    multi = MultiSearchSlsk({"catalognumber": [complete], "artist": [PENDING]})
    usable = lambda merged: [c for c in soulseek_auto.find_candidates(merged, RELEASE, CFG)
                             if c["complete"] and c["lossless"]]
    results, errors, skipped = soulseek_auto._search_queries(
        multi, ["catalognumber", "artist"], wait_s=5, usable=usable)
    assert multi.posted == ["catalognumber", "artist"], multi.posted
    assert [q for q, _r in results] == ["catalognumber"], results
    assert skipped == 1, skipped          # the slow template was never waited out
    assert errors == [], errors
    assert clock.now < 5, f"the second template's window was waited out (t={clock.now}s)"
finally:
    soulseek_auto.time = real_time_module


# --------------------------------------------------------------------------- #
# _normalize_browse: slskd 0.26's object shape -> full remote paths
# --------------------------------------------------------------------------- #
BROWSE_PAYLOAD = {"directories": [
    {"name": "downloads\\Album", "fileCount": 2, "files": [
        {"filename": "01 a.flac", "size": 10, "extension": "flac", "length": 123},
        {"filename": "cover.jpg", "size": 5},
    ]},
    "not-a-directory",
    {"name": "downloads\\Solo", "files": [
        {"filename": "downloads\\Solo\\02 b.mp3", "size": 7, "extension": "mp3"},
        "not-a-file",
    ]},
]}

rows = soulseek._normalize_browse(BROWSE_PAYLOAD)
assert len(rows) == 2, rows  # the non-dict entry is skipped, not fatal
album = rows[0]
assert album["directory"] == "downloads\\Album", album["directory"]
# each file's `filename` is a BARE name in this shape: the download needs the
# directory joined back on, or slskd is asked for a file that cannot exist.
assert [f["filename"] for f in album["files"]] == \
    ["downloads\\Album\\01 a.flac", "downloads\\Album\\cover.jpg"], album["files"]
assert [f["size"] for f in album["files"]] == [10, 5], album["files"]
assert album["files"][0]["ext"] == "flac", album["files"][0]
assert album["files"][0]["length"] == 123, album["files"][0]
assert album["files"][1]["ext"] == "" and album["files"][1]["length"] is None, album["files"][1]
# a `filename` that is already a remote path must not be joined twice
assert rows[1]["files"][0]["filename"] == "downloads\\Solo\\02 b.mp3", rows[1]["files"]
assert len(rows[1]["files"]) == 1, rows[1]["files"]

# older slskd builds answer with the list shape, already keyed by `directory`
legacy = soulseek._normalize_browse(
    [{"directory": "Music/x", "files": [{"filename": "Music/x/01.flac",
                                         "size": 3, "extension": "flac"}]}])
assert legacy == [{"directory": "Music/x",
                   "files": [{"filename": "Music/x/01.flac", "size": 3,
                              "ext": "flac", "length": None}]}], legacy


# --------------------------------------------------------------------------- #
# _release_from_folder: a browsed folder without a MusicBrainz release
# --------------------------------------------------------------------------- #
class FakeBrowse:
    """slskd stub for browse(): one scripted share tree, already normalized."""

    def __init__(self, dirs):
        self.dirs = dirs

    def browse(self, username):
        return self.dirs


BROWSE_DIRS = [
    {"directory": "downloads\\Album", "files": [
        {"filename": "downloads\\Album\\01 a.flac", "size": 10, "length": 123},
        {"filename": "downloads\\Album\\02 b.FLAC", "size": 11},
        {"filename": "downloads\\Album\\cover.jpg", "size": 5},
        {"filename": "downloads\\Album\\rip.log", "size": 2},
    ]},
    {"directory": "downloads\\Other", "files": [{"filename": "downloads\\Other\\z.mp3"}]},
]

browsed = soulseek_auto._release_from_folder("peer", "downloads\\Album",
                                             FakeBrowse(BROWSE_DIRS))
assert not browsed["id"], browsed["id"]
assert browsed["title"] == "Album", browsed["title"]
assert browsed["medium_formats"] == ["Digital Media"], browsed["medium_formats"]
# one expected track per AUDIO file only: the .jpg and .log are not tracks
assert [m["title"] for m in browsed["media"]] == ["01 a", "02 b"], browsed["media"]
assert [m["position"] for m in browsed["media"]] == [1, 2], browsed["media"]
assert [m["disc"] for m in browsed["media"]] == [1, 1], browsed["media"]
assert browsed["media"][0]["length"] == 123000, browsed["media"][0]
assert browsed["media"][1]["length"] is None, browsed["media"][1]

# the folder match ignores case (and path spelling) on both sides
ci = soulseek_auto._release_from_folder("peer", "DOWNLOADS\\album",
                                        FakeBrowse(BROWSE_DIRS))
assert [m["title"] for m in ci["media"]] == ["01 a", "02 b"], ci["media"]

# a folder the peer does not have is an empty release, never a crash
unmatched = soulseek_auto._release_from_folder("peer", "downloads\\Nope",
                                               FakeBrowse(BROWSE_DIRS))
assert unmatched["media"] == [], unmatched["media"]


# --------------------------------------------------------------------------- #
# _wait_for_files: a dead transfer is dropped, an unfinished one is not accepted
# --------------------------------------------------------------------------- #
class FakeTransfers:
    """slskd transfer tree stub: ONE transfer of "peer" for WANTED[0].

    `growth` bytes are added per poll, so growth=0 is a peer that went silent
    and growth>0 is a live one."""

    finished_transfer = staticmethod(soulseek.finished_transfer)

    def __init__(self, state="InProgress", growth=0, size=10):
        self.state = state
        self.growth = growth
        self.size = size
        self.polls = 0
        self.cancelled = []

    def downloads_state(self):
        self.polls += 1
        return [{"username": "peer", "directories": [{"files": [
            {"id": "t1", "filename": WANTED[0]["filename"], "state": self.state,
             "bytesTransferred": self.growth * self.polls, "size": self.size,
             "percentComplete": 0, "averageSpeed": 10, "remainingTime": 5}]}]}]

    def cancel_downloads(self, username, transfer_ids):
        self.cancelled.extend(transfer_ids)
        return True


WANTED = [{"filename": "Music/A/01 - X.flac", "size": 10}]
_wait_dir = tempfile.mkdtemp(prefix="mlo-wait-")
try:
    fork = FakeClock()
    saved_time = soulseek_auto.time
    soulseek_auto.time = fork
    try:
        # 11. bytes frozen: give up after the stall window, not at the 900s
        #     per-candidate timeout, and cancel the dead transfer.
        fork.now = 0.0
        stuck = FakeTransfers("InProgress", growth=0)
        got = soulseek_auto._wait_for_files(stuck, _wait_dir, "peer", WANTED, 900.0)
        assert got == {}, got
        assert 180 < fork.now < 200, \
            f"stalled transfer gave up at t={fork.now}s of 900s"
        assert stuck.cancelled == ["t1"], stuck.cancelled

        # 12. bytes still rising -> slow, but not dead: run out the timeout.
        fork.now = 0.0
        moving = FakeTransfers("InProgress", growth=3)
        got = soulseek_auto._wait_for_files(moving, _wait_dir, "peer", WANTED, 400.0)
        assert got == {}, got
        assert fork.now >= 400, \
            f"a transfer still moving was abandoned early at t={fork.now}s"

        # 13. QUEUED with no byte movement is a dead queue, not a normal wait:
        #     nothing is InProgress, so the stall window applies.
        fork.now = 0.0
        queued = FakeTransfers("Queued", growth=0)
        got = soulseek_auto._wait_for_files(queued, _wait_dir, "peer", WANTED, 400.0)
        assert got == {}, got
        assert 180 < fork.now < 200, \
            f"a queue that never moved was waited out (t={fork.now}s)"

        # 14. A file on disk with the EXACT expected size is not an arrived
        #     download while slskd has not finished its transfer...
        put_file(_wait_dir, "Music", "A", "01 - X.flac", body=b"x" * 10)
        fork.now = 0.0
        unfin = FakeTransfers("InProgress", growth=3, size=10)
        got = soulseek_auto._wait_for_files(unfin, _wait_dir, "peer", WANTED, 400.0)
        assert got == {}, got
        assert fork.now >= 400, f"the file was accepted without a finished transfer (t={fork.now}s)"

        #     ...and the moment slskd reports success, the same file counts.
        fork.now = 0.0
        finished = FakeTransfers("Completed, Succeeded", growth=3, size=10)
        got = soulseek_auto._wait_for_files(finished, _wait_dir, "peer", WANTED, 400.0)
        assert list(got) == [WANTED[0]["filename"]], got
        assert got[WANTED[0]["filename"]] == os.path.normpath(
            os.path.join(_wait_dir, "Music", "A", "01 - X.flac")), got
        assert fork.now == 0.0, "an already-finished transfer was waited on"

        # 15. ...and a transfer that slskd says failed is never accepted, even
        #     with the right bytes on disk.
        fork.now = 0.0
        failed = FakeTransfers("Completed, Errored", growth=3, size=10)
        got = soulseek_auto._wait_for_files(failed, _wait_dir, "peer", WANTED, 400.0)
        assert got == {}, got
        assert fork.now == 0.0, fork.now
    finally:
        soulseek_auto.time = saved_time
finally:
    shutil.rmtree(_wait_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# _wait_for_files publishes the download progress payload, then clears it
# --------------------------------------------------------------------------- #
PROGRESS_WANTED = [{"filename": "Music/A/01 - X.flac", "size": 100},
                   {"filename": "Music/A/02 - Y.flac", "size": 300}]


class ProgressTransfers:
    """Two transfers: one finished, one still moving. Records the job payload
    the wait published on every poll."""

    finished_transfer = staticmethod(soulseek.finished_transfer)

    def __init__(self):
        self.snapshots = []

    def downloads_state(self):
        self.snapshots.append(soulseek_auto.job_state().get("progress"))
        return [{"username": "peer", "directories": [{"files": [
            {"id": "t1", "filename": PROGRESS_WANTED[0]["filename"],
             "state": "Completed, Succeeded", "bytesTransferred": 100, "size": 100,
             "percentComplete": 100, "averageSpeed": 0, "remainingTime": 0},
            {"id": "t2", "filename": PROGRESS_WANTED[1]["filename"],
             "state": "InProgress", "bytesTransferred": 150, "size": 300,
             "percentComplete": 50, "averageSpeed": 750, "remainingTime": 12},
        ]}]}]

    def cancel_downloads(self, username, transfer_ids):
        return True


_pdir = tempfile.mkdtemp(prefix="mlo-progress-")
try:
    put_file(_pdir, "Music", "A", "01 - X.flac", body=b"x" * 100)
    _pclock = FakeClock()
    soulseek_auto.time = _pclock
    try:
        pt = ProgressTransfers()
        soulseek_auto._job["progress"] = None
        got = soulseek_auto._wait_for_files(pt, _pdir, "peer", PROGRESS_WANTED, 400.0)
        # only the transfer slskd finished is accepted; the moving one is not
        assert list(got) == [PROGRESS_WANTED[0]["filename"]], got
        published = [s for s in pt.snapshots if s]
        assert published, "no progress payload was published while downloading"
        snap = published[0]
        assert snap["phase"] == "download" and snap["username"] == "peer", snap
        assert snap["dir"] == "Music/A", snap["dir"]
        assert (snap["files_done"], snap["files_total"]) == (1, 2), snap
        assert (snap["bytes"], snap["size"]) == (250, 400), snap
        assert snap["percent"] == 62, snap["percent"]
        assert snap["speed"] == 750.0 and snap["eta_s"] == 12, snap
        assert [f["name"] for f in snap["files"]] == ["01 - X.flac", "02 - Y.flac"], snap["files"]
        assert snap["files"][0] == {"name": "01 - X.flac", "bytes": 100, "size": 100,
                                    "percent": 100, "speed": 0.0, "state": "Done",
                                    "done": True}, snap["files"][0]
        assert snap["files"][1]["done"] is False, snap["files"][1]
        assert snap["files"][1]["state"] == "InProgress", snap["files"][1]
        assert (snap["files"][1]["bytes"], snap["files"][1]["size"]) == (150, 300), snap["files"][1]
        assert snap["files"][1]["percent"] == 50, snap["files"][1]
        # ...and nothing is left behind once no download is in flight
        assert soulseek_auto._job["progress"] is None, soulseek_auto._job["progress"]
    finally:
        soulseek_auto.time = real_time_module
finally:
    shutil.rmtree(_pdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# finished_transfer: compound states still name a removable transfer
# --------------------------------------------------------------------------- #
for _state in ("Completed, Succeeded", "Completed, Errored", "Cancelled", "TimedOut"):
    assert soulseek.finished_transfer(_state) is True, _state
for _state in ("InProgress", "Queued", "Requested", "Initializing", "", None):
    assert soulseek.finished_transfer(_state) is False, _state


# --------------------------------------------------------------------------- #
# confirm_lossy / cancel / job_active state machine
# --------------------------------------------------------------------------- #
def _snapshot_job():
    return {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
            for k, v in soulseek_auto._job.items()}


def call_locked(fn, *args):
    """Drive a _job accessor on a worker thread.

    cancel() once called _log() while holding the module's non-reentrant
    _lock, so it never returned and left every later job_state()/confirm_lossy()
    call blocked. A deadlock must fail an assert with a message, not hang the
    suite (or the app) forever."""
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("r", fn(*args)), daemon=True)
    t.start()
    t.join(2.0)
    assert not t.is_alive(), f"{fn.__name__} never returned within 2s — _lock held?"
    return box["r"]


saved_job = _snapshot_job()
saved_answer = dict(soulseek_auto._confirm_answer)
try:
    soulseek_auto._confirm_event.clear()
    soulseek_auto._job.update({"state": "confirm", "cancel": False,
                               "confirm": {"formats": ["mp3"], "candidates": ["Music/A/"]}})
    assert call_locked(soulseek_auto.job_active) is True, soulseek_auto._job["state"]

    # 6a. Accepting a pending prompt: True, resumed, payload and event cleared.
    assert call_locked(soulseek_auto.confirm_lossy, True) is True, soulseek_auto._job["state"]
    assert soulseek_auto._job["state"] == "running", soulseek_auto._job["state"]
    assert soulseek_auto._job["confirm"] is None, soulseek_auto._job["confirm"]
    assert soulseek_auto._confirm_event.is_set() is True, "event not set for the waiter"
    assert soulseek_auto._confirm_answer["accept"] is True, soulseek_auto._confirm_answer

    # 6b. Nothing pending (job moved on) -> False, never a silent accept.
    soulseek_auto._confirm_event.clear()
    soulseek_auto._job["state"] = "running"
    assert call_locked(soulseek_auto.confirm_lossy, False) is False, "answered a prompt that was not pending"
    assert soulseek_auto._job["state"] == "running", soulseek_auto._job["state"]
    assert soulseek_auto._confirm_event.is_set() is False, "spurious wakeup"

    # 6c. cancel() from the parked prompt releases the waiter and reports True.
    soulseek_auto._job["state"] = "confirm"
    assert call_locked(soulseek_auto.cancel) is True, soulseek_auto._job["state"]
    assert soulseek_auto._job["cancel"] is True, soulseek_auto._job["cancel"]
    assert soulseek_auto._confirm_event.is_set() is True, "waiter left parked on cancel"

    # job_active() covers both running and confirm, and nothing else.
    for state, active in (("confirm", True), ("running", True), ("idle", False)):
        soulseek_auto._job["state"] = state
        assert call_locked(soulseek_auto.job_active) is active, (state, soulseek_auto._job["state"])

    # cancel() outside a live job is a no-op.
    assert call_locked(soulseek_auto.cancel) is False, "cancel of an idle job reported True"
finally:
    soulseek_auto._job.clear()
    soulseek_auto._job.update(saved_job)
    soulseek_auto._confirm_answer.update(saved_answer)
    soulseek_auto._confirm_event.clear()

assert soulseek_auto._job == saved_job, "module job state not restored"


# --------------------------------------------------------------------------- #
# Foreign folder layouts (contract D): each row must yield exactly ONE candidate
# --------------------------------------------------------------------------- #
def lrow(user, path, duration=200.0):
    """A search row with no size — the _run tests plant the files themselves."""
    r = row(user, path, duration=duration)
    r["size"] = 0
    return r


LAYOUT_RELEASE = {
    "id": "22222222-3333-4444-5555-666666666666",
    "title": "Layout Album",
    "date": "1996",
    "medium_formats": ["Digital Media"],
    "artists": [{"name": "Layout Artist", "mbid": "b1b2c3d4-0000-0000-0000-000000000000"}],
    "media": [{"disc": 1, "position": 1, "title": "Alpha", "length": 200000},
              {"disc": 1, "position": 2, "title": "Beta", "length": 210000}],
}
LAYOUT_CD = dict(LAYOUT_RELEASE, medium_formats=["CD"])
LAYOUT_CD2 = dict(LAYOUT_CD, media=[
    {"disc": 1, "position": 1, "title": "Alpha", "length": 200000},
    {"disc": 1, "position": 2, "title": "Beta", "length": 210000},
    {"disc": 2, "position": 1, "title": "Gamma", "length": 220000},
    {"disc": 2, "position": 2, "title": "Delta", "length": 230000}])


def layout_candidate(rows, release=None):
    c = soulseek_auto.find_candidates(rows, release or LAYOUT_RELEASE, CFG)
    assert len(c) == 1, [x["dir"] for x in c]
    return c[0]


def planned(c):
    return sorted(os.path.basename(f["file"]) for f in c["files"])


# (a) every disc-folder spelling folds into one album root on its own
for _disc_dir in ("CD 1", "CD1", "Disc1", "Disc 1", "Disk1", "Volume 1"):
    _c = layout_candidate([
        lrow("peer", f"Music/Album/{_disc_dir}/01 - Alpha.flac"),
        lrow("peer", f"Music/Album/{_disc_dir}/02 - Beta.flac", 210.0),
        lrow("peer", f"Music/Album/{_disc_dir}/x.log"),
        lrow("peer", f"Music/Album/{_disc_dir}/x.cue"),
    ], LAYOUT_CD)
    assert _c["complete"] and _c["dir"] == "Music/Album/", (_disc_dir, _c["dir"], _c["complete"])

# (b) audio in a subfolder with the log/cue beside it -> one merged candidate
merged = layout_candidate([
    lrow("peer", "Music/Album/Music/01 - Alpha.flac"),
    lrow("peer", "Music/Album/Music/02 - Beta.flac", 210.0),
    lrow("peer", "Music/Album/Log+Cue/x.log"),
    lrow("peer", "Music/Album/Log+Cue/x.cue"),
])
assert merged["dir"] == "Music/Album/", merged["dir"]
assert merged["complete"], merged
assert planned(merged) == ["01 - Alpha.flac", "02 - Beta.flac", "x.cue", "x.log"], planned(merged)

# (d) per-track folders are covered by their album root
tracked = layout_candidate([
    lrow("peer", "Music/Album/01 - Alpha/01 - Alpha.flac"),
    lrow("peer", "Music/Album/02 - Beta/02 - Beta.flac", 210.0),
    lrow("peer", "Music/Album/x.log"),
])
assert tracked["dir"] == "Music/Album/" and tracked["complete"], tracked

# (e) "01 Title", "01.Title", "01_Title" and "(1) 01 - Title" all parse
for _name in ("01 Alpha.flac", "01.Alpha.flac", "01_Alpha.flac", "(1) 01 - Alpha.flac"):
    _c = layout_candidate([lrow("peer", f"Music/Album/{_name}"),
                           lrow("peer", "Music/Album/02 - Beta.flac", 210.0)])
    assert (_c["matched"], _c["complete"]) == (2, True), (_name, _c["matched"])

# (h) "track01.flac" with no separator between word and number
_nc = layout_candidate([lrow("peer", "Music/Album/track01.flac"),
                        lrow("peer", "Music/Album/track02.flac", 210.0)])
assert (_nc["matched"], _nc["complete"]) == (2, True), _nc["matched"]

# (f) a decorated album folder and a decorated disc folder ("CD1 [FLAC]")
decorated = layout_candidate([
    lrow("peer", "Music/Album [FLAC]/CD1 [FLAC]/01 - Alpha.flac"),
    lrow("peer", "Music/Album [FLAC]/CD1 [FLAC]/02 - Beta.flac", 210.0),
    lrow("peer", "Music/Album [FLAC]/CD1 [FLAC]/x.log"),
    lrow("peer", "Music/Album [FLAC]/CD1 [FLAC]/x.cue"),
    lrow("peer", "Music/Album [FLAC]/Volume 2/01 - Gamma.flac", 220.0),
    lrow("peer", "Music/Album [FLAC]/Volume 2/02 - Delta.flac", 230.0),
    lrow("peer", "Music/Album [FLAC]/Volume 2/x.log"),
    lrow("peer", "Music/Album [FLAC]/Volume 2/x.cue"),
], LAYOUT_CD2)
assert decorated["complete"] and decorated["dir"] == "Music/Album [FLAC]/", decorated["dir"]

# (g) a single-file-per-disc image rip: one file per disc plus its cue
img = layout_candidate([lrow("peer", "Music/Artist/Album.flac", 410.0),
                        lrow("peer", "Music/Artist/Album.cue")])
assert img["complete"] and img["matched"] == 2, img
assert planned(img) == ["Album.cue", "Album.flac"], planned(img)

img2 = layout_candidate([
    lrow("peer", "Music/Artist/CD1.flac", 410.0), lrow("peer", "Music/Artist/CD1.cue"),
    lrow("peer", "Music/Artist/CD1.log"),
    lrow("peer", "Music/Artist/CD2.flac", 420.0), lrow("peer", "Music/Artist/CD2.cue"),
    lrow("peer", "Music/Artist/CD2.log"),
], LAYOUT_CD2)
assert img2["complete"] and img2["matched"] == 4, img2
assert planned(img2) == ["CD1.cue", "CD1.flac", "CD1.log",
                         "CD2.cue", "CD2.flac", "CD2.log"], planned(img2)

# a stray single track file next to a cue is NOT an image rip
assert soulseek_auto.find_candidates(
    [lrow("peer", "Music/Album/01 - Alpha.flac"), lrow("peer", "Music/Album/Album.cue")],
    LAYOUT_CD, CFG) == [], "a lone track file was mistaken for a disc image"

# _verify_album's image-rip probe: a cue listing more TRACKs than there are
# audio files means the .log's per-track CRCs cannot be matched to a file.
_img_dir = tempfile.mkdtemp(prefix="mlo-image-")
try:
    _img = os.path.join(_img_dir, "Album.flac")
    _cue = os.path.join(_img_dir, "Album.cue")
    open(_img, "wb").close()
    with open(_cue, "w", encoding="utf-8") as _fh:
        _fh.write("TRACK 01 AUDIO\nTRACK 02 AUDIO\n")
    assert soulseek_auto._has_image_rip(_img_dir, [_img]) is True
    with open(_cue, "w", encoding="utf-8") as _fh:
        _fh.write("TRACK 01 AUDIO\n")
    assert soulseek_auto._has_image_rip(_img_dir, [_img]) is False
    os.remove(_cue)
    assert soulseek_auto._has_image_rip(_img_dir, [_img]) is False
finally:
    shutil.rmtree(_img_dir, ignore_errors=True)

# (c) flat "Artist/Album - 01 - Title.flac": accepted, but stage 2 must stay
#     inside the release and never enqueue the artist's other album
flat_artist = layout_candidate([
    lrow("peer", "Music/Artist/Album - 01 - Alpha.flac"),
    lrow("peer", "Music/Artist/Album - 02 - Beta.flac", 210.0),
    lrow("peer", "Music/Artist/Album.log"),
    lrow("peer", "Music/Artist/Other Album - 01 - Zed.flac", 99.0),
    lrow("peer", "Music/Artist/Other Album/01 - Zed.flac", 99.0),
])
assert flat_artist["dir"] == "Music/Artist/", flat_artist["dir"]
assert flat_artist["complete"], flat_artist
assert planned(flat_artist) == ["Album - 01 - Alpha.flac", "Album - 02 - Beta.flac", "Album.log"], \
    planned(flat_artist)
assert not any("Zed" in f["file"] for f in flat_artist["files"]), flat_artist["files"]


# --------------------------------------------------------------------------- #
# _run: ONE log per disc, nothing submitted twice, attempt cap, junk logs
# --------------------------------------------------------------------------- #
class Patch:
    """Swap module attributes for a block and always restore them."""

    def __init__(self, module, **attrs):
        self.module, self.attrs = module, attrs

    def __enter__(self):
        self.saved = {k: getattr(self.module, k) for k in self.attrs}
        for k, v in self.attrs.items():
            setattr(self.module, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.module, k, v)
        return False


class AutoSlsk:
    """slskd stub for a whole _run() job: scripted search results plus an
    enqueue log, so "was this file submitted twice?" is answerable.

    Every enqueued file is reported as a FINISHED transfer, because the tests
    plant the downloads on disk before the job runs and _wait_for_files only
    accepts a file slskd itself says is done."""

    finished_transfer = staticmethod(soulseek.finished_transfer)

    def __init__(self, ddir, responses):
        self.ddir = ddir
        self.responses = responses
        self.enqueued = []
        self.queued = {}        # remote filename -> size, in enqueue order
        self.uploader = ""
        self.cancelled = []
        self.progress_snapshots = []
        self._sid = 0

    def is_running(self):
        return True

    def web_up(self, cfg):
        return True

    def server_state(self, cfg):
        return {"isLoggedIn": True}

    def download_dir(self, cfg):
        return self.ddir

    def search(self, query, timeout_ms=None):
        self._sid += 1
        return f"sid-{self._sid}"

    def search_results(self, sid):
        return {"state": "Completed", "isComplete": True, "responses": self.responses}

    def enqueue_download(self, username, wanted):
        self.enqueued.append([w["filename"] for w in wanted])
        self.uploader = username
        for w in wanted:
            self.queued[w["filename"]] = int(w.get("size") or 0)
        return True

    def downloads_state(self):
        # record what the job published on every poll, so the test can prove
        # the search / logging / download phase wiring end to end
        self.progress_snapshots.append(soulseek_auto.job_state()["progress"])
        return [{"username": self.uploader, "directories": [{"files": [
            {"id": name, "filename": name, "state": "Completed, Succeeded",
             "bytesTransferred": size, "size": size, "percentComplete": 100,
             "averageSpeed": 0, "remainingTime": 0}
            for name, size in self.queued.items()]}]}]

    def cancel_downloads(self, username, transfer_ids):
        self.cancelled.extend(transfer_ids)
        return True


JOB_CFG = {"soulseek_auto_log_min_score": 100, "soulseek_auto_max_attempts": 3,
           "soulseek_auto_search_wait": 5, "soulseek_auto_complete_ratio": 1.0}

JOB_RELEASE = {
    "id": "33333333-4444-5555-6666-777777777777",
    "title": "Job Album", "date": "1996", "medium_formats": ["CD"],
    "country": "GB", "catalog_number": "CAT-1",
    "artists": [{"name": "Job Artist", "mbid": "c1b2c3d4-0000-0000-0000-000000000000"}],
    "media": [{"disc": 1, "position": 1, "title": "Alpha", "length": 200000},
              {"disc": 1, "position": 2, "title": "Beta", "length": 210000}],
}
JOB_RELEASE_2D = dict(JOB_RELEASE, media=[
    {"disc": 1, "position": 1, "title": "Alpha", "length": 200000},
    {"disc": 1, "position": 2, "title": "Beta", "length": 210000},
    {"disc": 2, "position": 1, "title": "Gamma", "length": 220000},
    {"disc": 2, "position": 2, "title": "Delta", "length": 230000}])


def score_logs(*, low=(), invalid=()):
    """A _score_logs stand-in: 'low'/'invalid' name the logs to grade badly."""
    def fake(paths, cfg):
        return [(p, 50 if os.path.basename(p) in low else 100,
                 "invalid" if os.path.basename(p) in invalid else "ok", None)
                for p in sorted(paths)]
    return fake


class JobRun:
    def __init__(self, enqueued, verified, imported, job, tree, cancelled,
                 progress_snapshots):
        self.enqueued = enqueued
        self.verified = verified
        self.imported = imported
        self.job = job
        self.tree = tree          # album root -> the files that were in it
        self.cancelled = cancelled
        self.progress_snapshots = progress_snapshots

    def submitted(self):
        return [f for batch in self.enqueued for f in batch]


def run_job(release, rows, cfg=None, scores=None, queries=None, stub_cls=AutoSlsk):
    """Drive one whole _run() against a scripted slskd and planted downloads.

    Files are planted as finished downloads only where the job is supposed to
    find them mid-run, so the real _wait_for_files/_local_download_candidates
    path is what produces the verdicts."""
    saved = _snapshot_job()
    saved_time = soulseek_auto.time
    ddir = tempfile.mkdtemp(prefix="mlo-run-")
    try:
        for f in rows:
            put_file(ddir, *f["file"].split("/"))
        stub = stub_cls(ddir, rows)
        verified, imported = [], []
        soulseek_auto.time = FakeClock()
        soulseek_auto._job.clear()
        soulseek_auto._job.update({"state": "running", "stage": "", "release": None,
                                   "log": [], "attempts": [], "result": None,
                                   "confirm": None, "search": None, "cancel": False})
        with Patch(soulseek, is_running=stub.is_running, web_up=stub.web_up,
                   server_state=stub.server_state, download_dir=stub.download_dir,
                   search=stub.search, search_results=stub.search_results,
                   enqueue_download=stub.enqueue_download,
                   downloads_state=stub.downloads_state,
                   cancel_downloads=stub.cancel_downloads), \
             Patch(soulseek_auto,
                   load_config=lambda: dict(cfg or JOB_CFG),
                   _score_logs=scores or score_logs(),
                   _verify_album=lambda root, c, is_cd: (verified.append(root) or (True, [])),
                   _stamp_media=lambda *a, **k: (0, []),
                   _import=lambda root, r, c, media: (imported.append(root)
                                                      or {"album_path": root, "imported": True}),
                   traceback=SimpleNamespace(print_exc=lambda *a, **k: None)):
            soulseek_auto._run(release=release, queries=queries or ["job album"])
        tree = {}       # the download dir is gone once this returns
        for root in set(verified + imported):
            tree[root] = {f for _dp, _dn, files in os.walk(root) for f in files}
        return JobRun(stub.enqueued, verified, imported, dict(soulseek_auto._job),
                      tree, list(stub.cancelled), list(stub.progress_snapshots))
    finally:
        soulseek_auto.time = saved_time
        soulseek_auto._job.clear()
        soulseek_auto._job.update(saved)
        shutil.rmtree(ddir, ignore_errors=True)


ONE_DISC_ROWS = [
    lrow("peer", "Music/Album/01 - Alpha.flac"),
    lrow("peer", "Music/Album/02 - Beta.flac", 210.0),
    lrow("peer", "Music/Album/rip 1.log"),
    lrow("peer", "Music/Album/rip 2.log"),
    lrow("peer", "Music/Album/Album.cue"),
]
TWO_DISC_ROWS = [
    lrow("peer", "Music/Album/CD1/01 - Alpha.flac"),
    lrow("peer", "Music/Album/CD1/02 - Beta.flac", 210.0),
    lrow("peer", "Music/Album/CD1/CD1.log"),
    lrow("peer", "Music/Album/CD1/CD1.cue"),
    lrow("peer", "Music/Album/CD2/01 - Gamma.flac", 220.0),
    lrow("peer", "Music/Album/CD2/02 - Delta.flac", 230.0),
    lrow("peer", "Music/Album/CD2/CD2.log"),
    lrow("peer", "Music/Album/CD2/CD2.cue"),
]

# 14. ONE enqueue pass per candidate: the album, its cue and the ONE selected
#     log go to slskd together (a peer's queue is joined once), the second log
#     for the same disc is never submitted, and nothing is submitted twice.
job = run_job(JOB_RELEASE, ONE_DISC_ROWS)
assert job.imported, job.job["result"]
assert job.job["state"] == "done" and job.job["attempts"] == [], job.job
assert len(job.enqueued) == 1, job.enqueued
assert job.enqueued[0] == ["Music/Album/01 - Alpha.flac", "Music/Album/02 - Beta.flac",
                           "Music/Album/rip 1.log", "Music/Album/Album.cue"], job.enqueued[0]
assert "Music/Album/rip 2.log" not in job.submitted(), job.submitted()
assert len(job.submitted()) == len(set(job.submitted())), job.submitted()
# ...and the transfer block appears the INSTANT the peer accepts the files: a
# 'queued' snapshot is published right after enqueue_download, before the first
# poll, so the UI leaves the search stage immediately instead of sitting on a
# finished search until a byte counter shows up. The snapshot is cleared on the
# way out, so nothing stale survives into verify/import or the finished job.
_published = [s for s in job.progress_snapshots if s]
assert _published, "the job published no download progress at all"
# The FIRST thing the UI sees once the peer has the files is the queued block:
# it is published right after enqueue_download, before any poll, so the view
# leaves the search stage immediately instead of sitting on a finished search.
assert _published[0]["phase"] == "queued", _published
# Every snapshot carries the WHOLE enqueue (2 audio + the selected log + the
# cue). The .log gate used to publish files_total=1 — the log alone — which
# read as "stuck on one file" while the album was already downloading behind
# it. The exact number of samples depends on poll timing, so the phase
# sequence is not pinned; what must hold is that no poll ever shows a subset.
assert all(s["files_total"] == 4 for s in _published), _published
assert all(len(s["files"]) == 4 for s in _published), _published
assert job.progress_snapshots[-1] is None, job.progress_snapshots
assert job.job["progress"] is None, job.job["progress"]
# ...and the stage text names the REAL cap (the requested window plus the
# grace tail slskd needs to hand its responses back), never a promise it
# overruns. The window is used EXACTLY as configured — no floor is applied, so
# the 5s this config asks for is the 5s it gets, and a live source ends the
# wait early long before that.
assert [e["msg"] for e in job.job["log"] if e["msg"].startswith("Searching Soulseek")] == \
    ["Searching Soulseek with 1 query template(s) in parallel: “Job Album” … (up to 50s)"], \
    [e["msg"] for e in job.job["log"] if e["msg"].startswith("Searching")]

# 15. A second, junk log must not cost the album its good log: when one IS
#     graded low it is reported as ignored, not as a rejection.
job = run_job(JOB_RELEASE_2D, TWO_DISC_ROWS, scores=score_logs(low=("CD2.log",)))
assert job.imported, job.job["result"]
assert job.job["attempts"] == [], job.job["attempts"]
assert any("ignoring CD2.log" in e["msg"] for e in job.job["log"]), \
    [e["msg"] for e in job.job["log"]][-6:]
assert job.cancelled == [], job.cancelled     # a good log cancels nothing

# ...but a log whose checksum does not verify is evidence: with no good log
# left, the candidate is rejected and the album transfers it just queued are
# dropped again instead of downloading a rejected album.
job = run_job(JOB_RELEASE, ONE_DISC_ROWS, scores=score_logs(invalid=("rip 1.log",)))
assert not job.imported, job.job["result"]
assert len(job.job["attempts"]) == 1, job.job["attempts"]
assert "log rejected" in job.job["attempts"][0]["reason"], job.job["attempts"][0]
assert sorted(job.cancelled) == ["Music/Album/01 - Alpha.flac", "Music/Album/02 - Beta.flac",
                                 "Music/Album/Album.cue"], job.cancelled

# 16. Album/CD1 + Album/CD2 resolve to ONE album root holding every file, and
#     that root is what gets verified and imported.
job = run_job(JOB_RELEASE_2D, TWO_DISC_ROWS)
assert job.imported, job.job["result"]
assert job.verified[0] == job.imported[0], (job.verified, job.imported)
assert os.path.basename(job.imported[0]) == "Album", job.imported[0]
assert job.tree[job.imported[0]] == {
    "01 - Alpha.flac", "02 - Beta.flac", "CD1.log", "CD1.cue",
    "01 - Gamma.flac", "02 - Delta.flac", "CD2.log", "CD2.cue"}, job.tree
assert len(job.enqueued) == 1, job.enqueued           # one pass, both discs
assert sorted(job.enqueued[0]) == sorted([r["file"] for r in TWO_DISC_ROWS]), job.enqueued[0]
assert len(job.submitted()) == len(set(job.submitted())), job.submitted()

# 17. The attempt cap stops the hunt after N rejected candidates.
_cap_rows = []
for _i in range(3):
    _cap_rows += [lrow(f"peer{_i}", f"Music/Album {_i}/01 - Alpha.flac"),
                  lrow(f"peer{_i}", f"Music/Album {_i}/02 - Beta.flac", 210.0),
                  lrow(f"peer{_i}", f"Music/Album {_i}/rip.log"),
                  lrow(f"peer{_i}", f"Music/Album {_i}/Album.cue")]
job = run_job(JOB_RELEASE, _cap_rows,
              cfg=dict(JOB_CFG, soulseek_auto_max_attempts=2),
              scores=score_logs(low=("rip.log",)))
assert len(job.enqueued) == 2, job.enqueued          # one pass per attempt
assert len(job.job["attempts"]) == 2, job.job["attempts"]
assert sorted(job.cancelled) == ["Music/Album 0/01 - Alpha.flac",
                                 "Music/Album 0/02 - Beta.flac",
                                 "Music/Album 0/Album.cue",
                                 "Music/Album 1/01 - Alpha.flac",
                                 "Music/Album 1/02 - Beta.flac",
                                 "Music/Album 1/Album.cue"], job.cancelled
assert job.job["state"] == "error", job.job["state"]
assert "Stopped after 2" in job.job["result"]["error"], job.job["result"]
assert not job.imported

# --------------------------------------------------------------------------- #
# search progress payload: the UI must never print an impossible overrun
# --------------------------------------------------------------------------- #
saved_search = soulseek_auto._job["search"]
try:
    soulseek_auto._job_search_progress("q", 40.0, 15, {"state": "InProgress",
                                                      "responseCount": 3, "fileCount": 9})
    _s = soulseek_auto._job["search"]
    assert _s == {"query": "q", "elapsed": 15, "wait": 15, "deadline_s": 60,
                  "remaining": 0, "state": "InProgress", "responses": 3, "files": 9}, _s
    # mid-window: elapsed and remaining count down inside the requested window
    soulseek_auto._job_search_progress("q", 4.0, 15, {})
    _s = soulseek_auto._job["search"]
    assert (_s["elapsed"], _s["remaining"], _s["deadline_s"]) == (4, 11, 60), _s
finally:
    soulseek_auto._job["search"] = saved_search


# --------------------------------------------------------------------------- #
# _est_timeout derives the wait from the peer's OWN numbers
# --------------------------------------------------------------------------- #
_KB = 1024
_MB = 1024 * _KB
_GB = 1024 * _MB
# 20 MB at the advertised 1 MB/s: the floor still holds
assert soulseek_auto._est_timeout({"total_size": 20 * _MB, "speed": _MB,
                                   "queue": 0, "files": [{}]}) == 900
# the estimate tracks the PEER's rate, not the old fixed 200 kB/s guess:
# 40 MB is 5s at 8 MB/s (floor) but 4096s at 10 kB/s
fast = soulseek_auto._est_timeout({"total_size": 40 * _MB, "speed": 8 * _MB,
                                   "queue": 0, "files": [{}]})
slow = soulseek_auto._est_timeout({"total_size": 40 * _MB, "speed": 10 * _KB,
                                   "queue": 0, "files": [{}]})
assert fast == 900, fast
assert slow == 4096, slow
# the queue position pushes the estimate out, and a peer that reports no
# speed at all still gets the floor rather than an instant timeout
assert soulseek_auto._est_timeout({"total_size": 40 * _MB, "speed": 10 * _KB,
                                   "queue": 10, "files": [{}]}) > slow
assert soulseek_auto._est_timeout({"total_size": 40 * _MB, "queue": 0,
                                   "files": [{}]}) == 900
assert soulseek_auto._est_timeout({"total_size": 400 * _GB, "speed": _KB,
                                   "queue": 0, "files": [{}]}) == 7200   # cap kept
# ...and the speed really is carried on the candidate the ranking produced
assert soulseek_auto.find_candidates(
    folder("speedy", "Album FLAC", "flac"), RELEASE, CFG)[0]["speed"] > 0


# --------------------------------------------------------------------------- #
# _verify_album: a CD whose CRCs could not be checked is NOT verified
# --------------------------------------------------------------------------- #
_verify_dir = tempfile.mkdtemp(prefix="mlo-verify-")
try:
    _track = os.path.join(_verify_dir, "01 - A.flac")
    open(_track, "wb").close()
    _tools = {"ffmpeg": {"ffmpeg_exe": "ffmpeg"}}
    with Patch(mlo_tools, detect_all_tools=lambda: _tools), \
         Patch(mlo_discs, verify_album_checksums=lambda *a, **k: ({}, {})):
        ok, problems = soulseek_auto._verify_album(_verify_dir, CFG, True)
    # zero verdicts and zero unverified entries = not one CRC was checked (a
    # missing MEDIA tag); reading that as "verified" is how a FAKE rip got
    # imported
    assert ok is False and problems, (ok, problems)
    assert "CRC" in problems[0], problems
    # ...and a real verdict still passes, so the guard is not over-tight
    with Patch(mlo_tools, detect_all_tools=lambda: _tools), \
         Patch(mlo_discs, verify_album_checksums=lambda *a, **k: ({_track: "REAL"}, {})):
        ok, problems = soulseek_auto._verify_album(_verify_dir, CFG, True)
    assert ok is True and problems == [], (ok, problems)
finally:
    shutil.rmtree(_verify_dir, ignore_errors=True)

# 18. A template slskd errored on is reported even while ANOTHER template
#     found the album: "one query failed" must never be hidden behind the
#     candidate that did come back.
class ErroringSearch(AutoSlsk):
    def search_results(self, sid):
        if sid == "sid-1":
            return {"state": "Errored", "isComplete": True, "responses": []}
        return super().search_results(sid)


job = run_job(JOB_RELEASE, ONE_DISC_ROWS,
              queries=["artist album catalognumber", "album"],
              stub_cls=ErroringSearch)
assert job.imported, job.job["result"]
_line = next(e["msg"] for e in job.job["log"] if e["msg"].startswith("Searching Soulseek"))
assert "2 query template(s) in parallel" in _line and " · " in _line, _line
assert any("Errored" in e["msg"] and "CAT-1" in e["msg"] for e in job.job["log"]), \
    [e["msg"] for e in job.job["log"][:4]]

print("ok")
