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
  * confirm()/cancel() only answer a prompt that is actually pending, and
  * a job whose search found NO usable folder parks and offers the wishes list
    (interactive path only) instead of failing, handing the wish the very
    queries the job searched with.

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

# 3. The rank's precedence, table-driven: lossless, then the score, then the
#    FASTEST peer (the user's "download from the fastest good source first"),
#    then its queue — and a total order, so a job's candidate order is
#    reproducible run to run. A fast WRONG folder never overtakes a slower
#    complete one: the codec bucket is the first key and the score the second,
#    and the score's own speed bonus is capped below one matched track.
def rankable(user, *, score=90.0, speed=1_000_000, queue=0, lossless=True,
             folder=None):
    return {"username": user, "dir": folder or f"Music/{user.title()}/",
            "score": score, "speed": speed, "queue": queue, "lossless": lossless}


def rank_order(*rows):
    return [c["username"] for c in sorted(rows, key=soulseek_auto._rank)]


# faster wins the tie between equally scored lossless folders, and a queue only
# breaks a speed tie (all four share the score, so the table is exactly the
# user's question).
_ranked = [rankable("slow", speed=300_000), rankable("fast", speed=9_000_000),
           rankable("busy", speed=9_000_000, queue=40), rankable("unknown", speed=0)]
assert [c["username"] for c in sorted(_ranked, key=soulseek_auto._rank)] == \
    ["fast", "busy", "slow", "unknown"], _ranked
# ...and the keys above speed still outrank it: a lossy folder with the best
# score and the fastest peer in the network loses to the slowest lossless one,
# and a fast folder that matches fewer tracks loses to a slow complete one.
assert rank_order(rankable("mp3", score=120, speed=20_000_000, lossless=False),
                  rankable("flac", score=40, speed=64_000)) == ["flac", "mp3"]
assert rank_order(rankable("fast_partial", score=60, speed=20_000_000),
                  rankable("slow_complete", score=80, speed=64_000)) == \
    ["slow_complete", "fast_partial"]
# the order is TOTAL: two candidates the search reports identically still come
# back in one fixed order, and a same-peer tie is settled by the folder.
_twins = [rankable("same", folder="Music/B/"), rankable("same", folder="Music/A/")]
assert [c["dir"] for c in sorted(_twins, key=soulseek_auto._rank)] == \
    ["Music/A/", "Music/B/"], _twins
assert rank_order(rankable("zeta", folder="Music/A/"),
                  rankable("alpha", folder="Music/Z/")) == ["alpha", "zeta"]


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
        self.limits = []
        self.cancelled = []
        self.polls_made = 0

    def search(self, query, timeout_ms=None, response_limit=0):
        self.timeouts.append(timeout_ms)
        self.limits.append(response_limit)
        return "sid-1"

    def cancel_search(self, sid):
        self.cancelled.append(sid)
        return True

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
        self.posted, self.polls, self.limits = [], {}, []

    def search(self, query, timeout_ms=None, response_limit=0):
        self.posted.append(query)
        self.limits.append(response_limit)
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
    # ...and the seconds it really spent are on record for the job (`waited` in
    # the wish prompt): two InProgress polls x the poll cadence, NEVER the 6s
    # window the search was allowed — the number the prompt used to publish.
    assert soulseek_auto._search_seconds() == 2 * soulseek_auto._SEARCH_POLL_S, \
        soulseek_auto._search_seconds()
    assert soulseek_auto._search_seconds() < 6, soulseek_auto._search_seconds()
    # slskd hands searchTimeout to Soulseek.NET as MILLISECONDS: 6 s -> 6000.
    # A seconds-style 6 ends the search almost immediately and returns nothing.
    assert slsk.timeouts == [6000], slsk.timeouts

    # 4. A search that never terminates is REPORTED, never read as "no hits".
    clock.now = 0.0
    results, errors, skipped = soulseek_auto._search_queries(
        FakeSlsk([PENDING]), ["never-done"], wait_s=2)
    assert results == [] and skipped == 0, (results, skipped)
    assert any("never-done" in e and "did not finish" in e for e in errors), errors
    # A search that really DID run out its window reports that whole elapsed
    # time (`waited`): the measured number is only "short" when the search
    # really ended early, which is what makes it trustworthy.
    assert soulseek_auto._search_seconds() == clock.now, soulseek_auto._search_seconds()
    assert soulseek_auto._search_seconds() > 2 + soulseek_auto._SEARCH_GRACE_S - 1, \
        soulseek_auto._search_seconds()

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


# slskd publishes remote paths with FORWARD slashes on every platform it runs
# on, and that is what the job splits names out of — a fixture spelled with
# backslashes only "worked" on Windows, where os.sep made the split happen.
BROWSE_DIRS = [
    {"directory": "downloads/Album", "files": [
        {"filename": "downloads/Album/01 a.flac", "size": 10, "length": 123},
        {"filename": "downloads/Album/02 b.FLAC", "size": 11},
        {"filename": "downloads/Album/cover.jpg", "size": 5},
        {"filename": "downloads/Album/rip.log", "size": 2},
    ]},
    {"directory": "downloads/Other", "files": [{"filename": "downloads/Other/z.mp3"}]},
]

browsed = soulseek_auto._release_from_folder("peer", "downloads/Album",
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
        assert snap["files_arrived"] == 1, snap
        assert (snap["bytes"], snap["size"]) == (250, 400), snap
        # the byte share is reported fractionally now (250/400), so the bar
        # can move between two whole percents on a big file
        assert snap["percent"] == 62.5, snap["percent"]
        # ETA is the remaining BYTES at the current rate (150 B left at the
        # peer's 750 B/s), never slskd's per-file remainingTime guess — that
        # guess is what printed absurd ETAs while other files were queued.
        assert snap["speed"] == 750.0 and snap["eta_s"] == 0, snap
        assert [f["name"] for f in snap["files"]] == ["01 - X.flac", "02 - Y.flac"], snap["files"]
        assert snap["files"][0] == {"name": "01 - X.flac", "bytes": 100, "size": 100,
                                    "percent": 100, "speed": 0.0, "state": "Done",
                                    "done": True, "complete": True}, snap["files"][0]
        assert snap["files"][1]["done"] is False, snap["files"][1]
        assert snap["files"][1]["complete"] is False, snap["files"][1]
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
# confirm / cancel / job_active state machine
# --------------------------------------------------------------------------- #
def _snapshot_job():
    return {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
            for k, v in soulseek_auto._job.items()}


def call_locked(fn, *args):
    """Drive a _job accessor on a worker thread.

    cancel() once called _log() while holding the module's non-reentrant
    _lock, so it never returned and left every later job_state()/confirm()
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
    assert call_locked(soulseek_auto.confirm, True) is True, soulseek_auto._job["state"]
    assert soulseek_auto._job["state"] == "running", soulseek_auto._job["state"]
    assert soulseek_auto._job["confirm"] is None, soulseek_auto._job["confirm"]
    assert soulseek_auto._confirm_event.is_set() is True, "event not set for the waiter"
    assert soulseek_auto._confirm_answer["accept"] is True, soulseek_auto._confirm_answer

    # 6b. Nothing pending (job moved on) -> False, never a silent accept.
    soulseek_auto._confirm_event.clear()
    soulseek_auto._job["state"] = "running"
    assert call_locked(soulseek_auto.confirm, False) is False, "answered a prompt that was not pending"
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
        # Per PEER, not one flat list: a batch queues several peers before it
        # waits on any of them, and slskd reports every user's transfers at once
        # (_user_transfers filters by username) — a stub that remembered only the
        # last peer asked made a cancelled candidate look like it had no
        # transfers to cancel at all.
        self.queues = {}        # username -> {remote filename: size}
        self.uploader = ""      # the last peer asked
        self.cancelled = []
        self.cancelled_searches = []
        self.progress_snapshots = []
        self.searches = []      # (query, timeout_ms, response_limit)
        self._sid = 0

    def is_running(self):
        return True

    def web_up(self, cfg):
        return True

    def server_state(self, cfg):
        return {"isLoggedIn": True}

    def download_dir(self, cfg):
        return self.ddir

    def search(self, query, timeout_ms=None, response_limit=0):
        self._sid += 1
        self.searches.append((query, timeout_ms, response_limit))
        return f"sid-{self._sid}"

    def cancel_search(self, sid):
        self.cancelled_searches.append(sid)
        return True

    def search_results(self, sid):
        return {"state": "Completed", "isComplete": True, "responses": self.responses}

    def enqueue_download(self, username, wanted):
        self.enqueued.append([w["filename"] for w in wanted])
        self.uploader = username
        queue = self.queues.setdefault(username, {})
        for w in wanted:
            queue[w["filename"]] = int(w.get("size") or 0)
        return True

    def downloads_state(self):
        # record what the job published on every poll, so the test can prove
        # the search / logging / download phase wiring end to end
        self.progress_snapshots.append(soulseek_auto.job_state()["progress"])
        return [{"username": user, "directories": [{"files": [
            {"id": name, "filename": name, "state": "Completed, Succeeded",
             "bytesTransferred": size, "size": size, "percentComplete": 100,
             "averageSpeed": 0, "remainingTime": 0}
            for name, size in sorted(queue.items())]}]}
            for user, queue in self.queues.items()]

    def cancel_downloads(self, username, transfer_ids, cfg=None, failed=None):
        # the real signature (server.soulseek.cancel_downloads): `failed` is
        # where slskd's ids it did NOT confirm are reported back — this stub
        # confirms every one, so it is left empty.
        self.cancelled.extend(transfer_ids)
        return True


JOB_CFG = {"soulseek_auto_log_min_score": 100,
           "soulseek_auto_search_wait": 5, "soulseek_auto_complete_ratio": 1.0,
           "soulseek_auto_response_limit": 25}

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
        self.prompt = None        # the confirm payload a parked job published

    def submitted(self):
        return [f for batch in self.enqueued for f in batch]


def _tree_files(root):
    """Every file under a directory, as slash-separated paths relative to it."""
    out = []
    for dp, _dn, files in os.walk(root):
        for f in files:
            out.append(os.path.relpath(os.path.join(dp, f), root).replace("\\", "/"))
    return out


def run_job(release, rows, cfg=None, scores=None, queries=None, stub_cls=AutoSlsk,
            confirm_lossy=False, answer=None, keep_dir=False, **over):
    """Drive one whole _run() against a scripted slskd and planted downloads.

    Files are planted as finished downloads only where the job is supposed to
    find them mid-run, so the real _wait_for_files/_local_download_candidates
    path is what produces the verdicts.

    `answer` (None = never parks) answers a job that parks on a prompt: the
    interactive job BLOCKS waiting for the user, so it is driven on a worker
    thread and answered the way the HTTP route does — poll job_state(), then
    confirm(). True/False accept or decline; the string "cancel" calls
    cancel() on the parked job instead. The payload the job published is left
    on JobRun.prompt; a LIST of answers answers each prompt in turn (a job can
    park twice), with every payload on JobRun.prompts.

    Two extras for the tests that look past the job's own result: `keep_dir`
    leaves the download dir in place (returned on JobRun.ddir) so what survived
    a rejected candidate is inspectable, and JobRun.calls lists every
    ("stamp"|"verify"|"import", root, media) the job reached, so "was the
    medium switched BEFORE verification?" is answerable. The slskd double
    itself is on JobRun.stub (its query journal, cancellation log, ...), and any
    further `over` keyword replaces one of the stubbed stages for a test that
    needs the real one (the download-dir clear) or a scripted verdict."""
    saved = _snapshot_job()
    saved_time = soulseek_auto.time
    # Every worker this call starts, joined before the global job state is
    # restored below: a thread still running after the restore writes its own
    # result into the module-wide `_job`, and the NEXT block's assertion then
    # reads that instead of its own job. On a loaded CI runner that is exactly
    # what happened (the wish block saw a "slskd is not running" error from a
    # previous call's thread, whose patch had already been restored), which is
    # why this suite could pass locally and fail in CI.
    started = []
    ddir = tempfile.mkdtemp(prefix="mlo-run-")
    try:
        for f in rows:
            put_file(ddir, *f["file"].split("/"))
        stub = stub_cls(ddir, rows)
        verified, imported = [], []
        calls = []
        prompts = []
        # The clock the job runs on: every sleep advances it, so the seconds the
        # job MEASURES (`_search_seconds`, the wish prompt's `waited`) are
        # readable from the test instead of being wall time.
        clock = FakeClock()
        soulseek_auto.time = clock
        soulseek_auto._job.clear()
        soulseek_auto._job.update({"state": "running", "stage": "", "release": None,
                                   "log": [], "attempts": [], "result": None,
                                   "confirm": None, "search": None, "cancel": False})
        soulseek_auto._confirm_event.clear()
        soulseek_auto._confirm_answer["accept"] = False
        # The stages this suite stubs, with any `over` from the caller applied on
        # top: a test that needs the REAL clear (or a scripted verify verdict)
        # overrides exactly that one stage.
        _patches = dict(
            load_config=lambda: dict(cfg or JOB_CFG),
            _score_logs=scores or score_logs(),
            # The download-dir clear runs on a SUCCESSFUL import and this suite's
            # `_import` never moves anything: stubbed here so the trees the
            # assertions elsewhere read stay put (the clear itself is driven for
            # real, with keep_dir, in its own block).
            _clear_downloads=lambda *a, **k: {},
            _verify_album=lambda root, c, is_cd: (
                verified.append(root)
                or calls.append(("verify", root, is_cd))
                or (True, [])),
            _stamp_media=lambda root, media, c: (
                calls.append(("stamp", root, media)) or (0, [])),
            _import=lambda root, r, c, media: (
                imported.append(root)
                or calls.append(("import", root,
                                 list(r.get("medium_formats") or []), media))
                or {"album_path": root, "imported": True}),
            traceback=SimpleNamespace(print_exc=lambda *a, **k: None),
        )
        _patches.update(over)
        with Patch(soulseek, is_running=stub.is_running, web_up=stub.web_up,
                   server_state=stub.server_state, download_dir=stub.download_dir,
                   search=stub.search, search_results=stub.search_results,
                   enqueue_download=stub.enqueue_download,
                   downloads_state=stub.downloads_state,
                   cancel_downloads=stub.cancel_downloads), \
             Patch(soulseek_auto, **_patches):
            def drv():
                # `queries=[]` is the wish case: nothing stored, so the job
                # derives its queries from the release (release_queries). The
                # default stays the single "job album" stub every other test
                # scripts its searches against.
                soulseek_auto._run(release=release,
                                   queries=(["job album"] if queries is None else queries),
                                   confirm_lossy=confirm_lossy)
            if answer is None:
                drv()
            else:
                worker = threading.Thread(target=drv, daemon=True)
                started.append(worker)
                worker.start()
                # One answer per prompt the job parks on (a job can park twice:
                # the CD-vs-Digital decision, then the wishes offer a decline
                # falls through to). A scalar answer is the common one-prompt
                # case; run.prompt stays the FIRST payload the job published.
                for ans in (answer if isinstance(answer, (list, tuple)) else [answer]):
                    parked = None
                    # Poll until the job parks, the worker dies, or the CAP
                    # expires. The cap is generous on purpose: the loop exits
                    # the moment a prompt appears (a passing case costs
                    # milliseconds), and a loaded CI runner — three workflows
                    # share the machine — made a tight window the reason this
                    # suite failed, which is not something the assertion can
                    # tell apart from a real regression.
                    _wait = (cfg or JOB_CFG).get("soulseek_auto_search_wait", 5)
                    deadline = real_time.time() + max(120, 8 * float(_wait))
                    while real_time.time() < deadline:
                        st = soulseek_auto.job_state()
                        if st["state"] == "confirm":
                            parked = dict(st["confirm"] or {})
                            break
                        if not worker.is_alive():
                            break
                        real_time.sleep(0.01)
                    if parked is None:
                        # The whole state, not just its name: an unexpected
                        # `error` here is a job-side failure and its message
                        # ("stage") is the only thing that says which step
                        # died and why.
                        _st = soulseek_auto.job_state()
                        _detail = {k: _st.get(k) for k in ("state", "stage", "error")
                                   if _st.get(k) not in (None, "")}
                        _detail["wishes_db"] = wishes_store.db_path()
                        if worker.is_alive():
                            raise AssertionError(
                                "the job never parked on its prompt within "
                                f"{max(120, 8 * float(_wait)):.0f}s — {_detail!r}")
                        raise AssertionError(
                            "the job finished without parking on its prompt — "
                            f"{_detail!r}")
                    prompts.append(parked)
                    if ans == "cancel":
                        assert soulseek_auto.cancel() is True, "the parked job took no cancel"
                    else:
                        assert soulseek_auto.confirm(ans) is True, "the prompt was not answerable"
                worker.join(10)
                assert not worker.is_alive(), "the job stayed parked after its answer"
        tree = {}       # the download dir is gone once this returns
        for root in set(verified + imported):
            tree[root] = {f for _dp, _dn, files in os.walk(root) for f in files}
        run = JobRun(stub.enqueued, verified, imported, dict(soulseek_auto._job),
                     tree, list(stub.cancelled), list(stub.progress_snapshots))
        run.prompt = prompts[0] if prompts else None
        run.prompts = prompts
        run.calls = calls
        run.ddir = ddir
        run.stub = stub
        run.clock = clock
        return run
    finally:
        # Quiesce before restoring: cancel whatever is still parked, then wait
        # for every worker this call started. Nothing of this call may write
        # into the global job state after the restore.
        try:
            soulseek_auto.cancel()
        except Exception:
            pass
        for _w in started:
            _w.join(30)
        soulseek_auto.time = saved_time
        soulseek_auto._confirm_event.clear()
        soulseek_auto._job.clear()
        soulseek_auto._job.update(saved)
        if not keep_dir:
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

# 14. On a CD the .log goes to slskd ALONE first and the album is requested in
#     a SECOND call, only once that log has passed the gate — queueing the
#     album together with the log sent a rejected peer the album's bytes for
#     nothing. The second log for the same disc is never submitted, and nothing
#     is submitted twice.
job = run_job(JOB_RELEASE, ONE_DISC_ROWS)
assert job.imported, job.job["result"]
assert job.job["state"] == "done" and job.job["attempts"] == [], job.job
assert job.enqueued == [["Music/Album/rip 1.log"],
                        ["Music/Album/01 - Alpha.flac", "Music/Album/02 - Beta.flac",
                         "Music/Album/Album.cue"]], job.enqueued
assert "Music/Album/rip 2.log" not in job.submitted(), job.submitted()
assert len(job.submitted()) == len(set(job.submitted())), job.submitted()
# ...and the album really was held back for the gate's verdict, not queued on
# the way past: the grade is logged between the two enqueues.
_enqueue_log = [e["msg"] for e in job.job["log"]]
assert any(m.startswith("Downloading .log") for m in _enqueue_log), _enqueue_log[:4]
assert any("pass — downloading the full album" in m for m in _enqueue_log), \
    [m for m in _enqueue_log if "log" in m][:6]
# ...and the transfer block appears the INSTANT the peer accepts the files: a
# 'queued' snapshot is published right after enqueue_download, before the first
# poll, so the UI leaves the search stage immediately instead of sitting on a
# finished search until a byte counter shows up. The snapshot is cleared on the
# way out, so nothing stale survives into verify/import or the finished job.
_published = [s for s in job.progress_snapshots if s]
assert _published, "the job published no download progress at all"
# The FIRST thing the UI sees once the peer has the files is the queued block,
# published right after enqueue_download, before any poll — and on a CD that
# first enqueue is the log alone, so it shows one file, not the album the job
# has not asked for yet.
assert _published[0]["phase"] == "queued", _published
assert _published[0]["files_total"] == 1, _published[0]
# The block shows exactly what is in flight: the log while the gate runs, the
# album once it passed — never the two together (that would be the old
# single-pass enqueue) and never a subset of either. How many samples the run
# records depends on poll timing, so only the sets are pinned.
assert all(s["files_total"] in (1, 3) for s in _published), _published
assert all(len(s["files"]) == s["files_total"] for s in _published), _published
assert job.progress_snapshots[-1] is None, job.progress_snapshots
assert job.job["progress"] is None, job.job["progress"]
# ...and the stage text names the REAL caps (the configured response limit, and
# the requested window plus the grace tail slskd needs to hand its responses
# back), never a promise it overruns. The window is used EXACTLY as configured
# — no floor is applied, so the 5s this config asks for is the 5s it gets, and
# a live source ends the wait the moment a good folder appears.
_stage = [e["msg"] for e in job.job["log"] if e["msg"].startswith("Searching Soulseek")]
assert len(_stage) == 1, _stage
assert "“Job Album”" in _stage[0], _stage
assert "25 responses" in _stage[0] and "50s" in _stage[0], _stage

# 15. A second, junk log must not cost the album its good log: when one IS
#     graded low it is reported as ignored, not as a rejection.
job = run_job(JOB_RELEASE_2D, TWO_DISC_ROWS, scores=score_logs(low=("CD2.log",)))
assert job.imported, job.job["result"]
assert job.job["attempts"] == [], job.job["attempts"]
assert any("ignoring CD2.log" in e["msg"] for e in job.job["log"]), \
    [e["msg"] for e in job.job["log"]][-6:]
assert job.cancelled == [], job.cancelled     # a good log cancels nothing
# both discs' logs go out together first (one log per disc), and the album
# follows in the second call once CD1's log carried it.
assert len(job.enqueued) == 2, job.enqueued
assert job.enqueued[0] == ["Music/Album/CD1/CD1.log", "Music/Album/CD2/CD2.log"], \
    job.enqueued[0]
assert sorted(job.enqueued[1]) == sorted(r["file"] for r in TWO_DISC_ROWS
                                         if not r["file"].endswith(".log")), \
    job.enqueued[1]

# ...but a log whose checksum does not verify is evidence: with no good log
# left, the candidate is rejected and the WHOLE set it just queued is dropped —
# the graded log included — instead of downloading a rejected album.
job = run_job(JOB_RELEASE, ONE_DISC_ROWS, scores=score_logs(invalid=("rip 1.log",)))
assert not job.imported, job.job["result"]
assert len(job.job["attempts"]) == 1, job.job["attempts"]
assert "log rejected" in job.job["attempts"][0]["reason"], job.job["attempts"][0]
# the album was never requested: only the failed log ever reached slskd, so it
# is the only transfer there is to cancel.
assert job.enqueued == [["Music/Album/rip 1.log"]], job.enqueued
assert sorted(job.cancelled) == ["Music/Album/rip 1.log"], job.cancelled

# 16. Album/CD1 + Album/CD2 resolve to ONE album root holding every file, and
#     that root is what gets verified and imported.
job = run_job(JOB_RELEASE_2D, TWO_DISC_ROWS)
assert job.imported, job.job["result"]
assert job.verified[0] == job.imported[0], (job.verified, job.imported)
assert os.path.basename(job.imported[0]) == "Album", job.imported[0]
assert job.tree[job.imported[0]] == {
    "01 - Alpha.flac", "02 - Beta.flac", "CD1.log", "CD1.cue",
    "01 - Gamma.flac", "02 - Delta.flac", "CD2.log", "CD2.cue"}, job.tree
assert len(job.enqueued) == 2, job.enqueued           # the logs, then both discs
assert sorted(job.enqueued[0]) == ["Music/Album/CD1/CD1.log",
                                   "Music/Album/CD2/CD2.log"], job.enqueued[0]
assert sorted(job.enqueued[1]) == sorted([r["file"] for r in TWO_DISC_ROWS
                                          if not r["file"].endswith(".log")]), \
    job.enqueued[1]
assert len(job.submitted()) == len(set(job.submitted())), job.submitted()

# 17. Every candidate the search scored is TRIED — there is no rejection cap
#     any more (capping at N threw away copies that would have verified; each
#     rejected peer now costs only its own attempt and its cleaned-up partials).
_cap_rows = []
for _i in range(3):
    _cap_rows += [lrow(f"peer{_i}", f"Music/Album {_i}/01 - Alpha.flac"),
                  lrow(f"peer{_i}", f"Music/Album {_i}/02 - Beta.flac", 210.0),
                  lrow(f"peer{_i}", f"Music/Album {_i}/rip.log"),
                  lrow(f"peer{_i}", f"Music/Album {_i}/Album.cue")]
job = run_job(JOB_RELEASE, _cap_rows, scores=score_logs(low=("rip.log",)))
# every candidate got its .log alone: the album was never requested from a peer
# whose log had not passed yet.
assert [len(b) for b in job.enqueued] == [1, 1, 1], job.enqueued
assert sorted(job.submitted()) == [f"Music/Album {i}/rip.log" for i in range(3)], \
    job.submitted()
assert len(job.job["attempts"]) == 3, job.job["attempts"]
# the rejected candidates' logs are the only transfers slskd knows about, and
# each one is cancelled with its candidate.
assert sorted(job.cancelled) == [f"Music/Album {i}/rip.log" for i in range(3)], \
    job.cancelled
assert job.job["state"] == "error", job.job["state"]
assert "Every candidate was rejected" in job.job["result"]["error"], job.job["result"]
assert not job.imported


# 17b. A CD candidate whose LOG passed but whose ALBUM would not queue is a
#      rejected candidate like any other: the reason names that second enqueue,
#      its whole set is swept and the next peer is tried. Nothing of the
#      refused album ever reached slskd.
class AlbumQueueFails(AutoSlsk):
    """AutoSlsk that refuses the ALBUM enqueue for one peer — the second call,
    the .log gate having passed — so the failure lands exactly where the
    log-first split put it."""

    def __init__(self, ddir, rows, user="badpeer"):
        super().__init__(ddir, rows)
        self.user = user

    def enqueue_download(self, username, wanted):
        if username == self.user and not any(str(w["filename"]).lower().endswith(".log")
                                             for w in wanted):
            raise RuntimeError("slskd refused the album queue")
        return super().enqueue_download(username, wanted)


QUEUE_FAIL_ROWS = [
    lrow("badpeer", "Music/Bad/01 - Alpha.flac"),
    lrow("badpeer", "Music/Bad/02 - Beta.flac", 210.0),
    lrow("badpeer", "Music/Bad/rip.log"),
    lrow("badpeer", "Music/Bad/Album.cue"),
    lrow("goodpeer", "Music/Good/01 - Alpha.flac"),
    lrow("goodpeer", "Music/Good/02 - Beta.flac", 210.0),
    lrow("goodpeer", "Music/Good/rip.log"),
    lrow("goodpeer", "Music/Good/Album.cue"),
]
# the refusing peer must be ranked FIRST, or the good one imports before the
# failure is ever reached: a busier peer loses the tie on its queue.
for _r in QUEUE_FAIL_ROWS:
    if _r["username"] == "goodpeer":
        _r["queue"] = 500

job = run_job(JOB_RELEASE, QUEUE_FAIL_ROWS, stub_cls=AlbumQueueFails)
assert job.job["state"] == "done" and job.imported, job.job
assert [a["reason"] for a in job.job["attempts"]] == \
    ["album failed to queue: slskd refused the album queue"], job.job["attempts"]
assert os.path.basename(job.imported[0]) == "Good", job.imported
# the refused peer was asked for its log and nothing else; the next peer's log
# then cleared the gate and its album followed in a second call.
assert job.enqueued == [["Music/Bad/rip.log"],
                        ["Music/Good/rip.log"],
                        ["Music/Good/01 - Alpha.flac", "Music/Good/02 - Beta.flac",
                         "Music/Good/Album.cue"]], job.enqueued
assert sorted(job.cancelled) == ["Music/Bad/rip.log"], job.cancelled


# 17c. A non-CD release has no log to gate on, so everything a candidate holds
#      goes out in ONE call — the split is the CD's alone.
DIGITAL_RELEASE = dict(JOB_RELEASE, medium_formats=["Digital Media"])
DIGITAL_ROWS = [
    lrow("webpeer", "Music/Webrip/01 - Alpha.flac"),
    lrow("webpeer", "Music/Webrip/02 - Beta.flac", 210.0),
]
job = run_job(DIGITAL_RELEASE, DIGITAL_ROWS)
assert job.imported and job.job["state"] == "done", job.job
assert job.enqueued == [["Music/Webrip/01 - Alpha.flac",
                         "Music/Webrip/02 - Beta.flac"]], job.enqueued
assert not job.job["attempts"], job.job["attempts"]

# --------------------------------------------------------------------------- #
# 17d. BATCHING: up to three candidates of ONE release download at once, the
#      first that VERIFIES good is the import, and every other candidate is
#      cancelled AND swept — its partial bytes must not keep arriving behind the
#      album being imported. Verification (the CD .log/CRC gate included) is the
#      verdict, so a candidate that fails it is rejected and the NEXT one
#      imports instead of the album failing. The batch width is the user's 3,
#      bounded by the app's own release width so the two budgets never multiply
#      behind the user's back.
# --------------------------------------------------------------------------- #
def batch_rows(*users):
    """One complete CD folder per peer (two tracks, a log and a cue)."""
    out = []
    for u in users:
        root = f"Music/Batch {u[-1].upper()}"
        out += [lrow(u, f"{root}/01 - Alpha.flac"),
                lrow(u, f"{root}/02 - Beta.flac", 210.0),
                lrow(u, f"{root}/rip.log"),
                lrow(u, f"{root}/Album.cue")]
    return out


BATCH_FILES = ["01 - Alpha.flac", "02 - Beta.flac", "Album.cue", "rip.log"]


def batch_files(*users):
    return sorted(f"Music/Batch {x.upper()}/{n}" for x in users for n in BATCH_FILES)


BATCH_ROWS = batch_rows("peerA", "peerB", "peerC")


def verify_bad(needle):
    """A _verify_album that fails for the folder whose path contains `needle` —
    the CD rip that cannot produce its evidence."""
    def verify(root, cfg_, is_cd):
        return (False, ["00 - Track.flac: CRC mismatch"]) if needle in root else (True, [])
    return verify


# (a) peerA ranks first (the three tie on score and speed, so the peer name
#     decides), downloads, and FAILS verification; peerB verifies and imports;
#     peerC — transferring alongside both — is cancelled and swept.
run = run_job(JOB_RELEASE, BATCH_ROWS, keep_dir=True,
              _verify_album=verify_bad("Batch A"))
try:
    assert run.job["state"] == "done" and run.imported, run.job
    # ALL THREE were started, and all three at once: each peer's .log went out in
    # the same pass (the batch), and each passing log's album followed.
    assert [len(b) for b in run.stub.enqueued[:3]] == [1, 1, 1], run.stub.enqueued
    assert sorted(run.submitted()[:3]) == [f"Music/Batch {x}/rip.log" for x in "ABC"], \
        run.submitted()[:3]
    assert len(run.stub.enqueued) == 6, run.stub.enqueued
    # The first candidate's rejection names it AND the cause (a CD judged on its
    # log/CRC evidence is the fragile case); the third's says it lost the race.
    _rejected, _lost = run.job["attempts"]
    assert _rejected["username"] == "peerA" and _rejected["dir"] == "Music/Batch A/"
    assert "verification failed (1 problem(s))" in _rejected["reason"] \
        and "CRC mismatch" in _rejected["reason"], _rejected
    assert _lost["username"] == "peerC" and _lost["dir"] == "Music/Batch C/"
    assert "peerB delivered the album first" in _lost["reason"], _lost
    # ...and the album that landed is peerB's.
    assert os.path.basename(run.imported[0]) == "Batch B", run.imported
    # A's and C's transfers are cancelled, B's are not, and the losers' folders
    # are GONE — nothing of theirs can keep arriving behind the import.
    assert sorted(run.cancelled) == batch_files("A", "C"), run.cancelled
    assert sorted(_tree_files(run.ddir)) == batch_files("B"), _tree_files(run.ddir)
finally:
    shutil.rmtree(run.ddir, ignore_errors=True)


class QueuedPeer(AutoSlsk):
    """AutoSlsk where one peer's ALBUM transfers sit in slskd's queue forever:
    the first candidate in rank order is the slow one, and the whole album is
    already on another peer's shelf. The case a batch exists for."""

    def __init__(self, ddir, rows, user="peerA"):
        super().__init__(ddir, rows)
        self.user = user

    def downloads_state(self):
        state = super().downloads_state()
        for entry in state:
            if entry["username"] != self.user:
                continue
            for d in entry["directories"]:
                for f in d["files"]:
                    if not str(f["filename"]).lower().endswith(".log"):
                        f["state"] = "Queued, Remotely"
        return state


# (b) The first-ranked peer never delivers while a later one has the album: the
#     batch hands the job over instead of waiting the slow peer out — not even
#     its own queue budget, let alone the hours a stalled peer could cost.
run = run_job(JOB_RELEASE, BATCH_ROWS, stub_cls=QueuedPeer, keep_dir=True)
try:
    assert run.job["state"] == "done" and run.imported, run.job
    _slow, _lost = run.job["attempts"]
    assert _slow["username"] == "peerA" and "peerB delivered the album first" in _slow["reason"], _slow
    assert _lost["username"] == "peerC", run.job["attempts"]
    assert os.path.basename(run.imported[0]) == "Batch B", run.imported
    assert sorted(_tree_files(run.ddir)) == batch_files("B"), _tree_files(run.ddir)
    # It did not sit out peerA at all: the batch's own clock says so.
    assert run.clock.now < 60, run.clock.now
finally:
    shutil.rmtree(run.ddir, ignore_errors=True)

# (c) The batch is the user's 3 and never wider than the app's own release
#     width, so a machine configured for one release at a time is not quietly
#     handed three times its network load.
assert soulseek_auto._batch_width({}) == 3, soulseek_auto._batch_width({})
assert soulseek_auto._batch_width({"soulseek_search_concurrency": 1}) == 1
assert soulseek_auto._batch_width({"soulseek_search_concurrency": 2}) == 2
assert soulseek_auto._batch_width({"soulseek_search_concurrency": 8}) == 3

# (d) A folder that answers BOTH searches (the configured template and the
#     broader second pass) is scored twice — and downloaded ONCE. Attempting it
#     again after it failed is the "downloads files twice that it already failed
#     on" the user asked about: the second attempt would spend a whole peer's
#     bandwidth on the very bytes that just failed.
_TWOPASS_CFG = dict(JOB_CFG, soulseek_auto_complete_ratio=0.5)
_TWOPASS_ROWS = [lrow("peer", "Music/Partial/01 - Alpha.flac")]   # one of two tracks
run = run_job(DIGITAL_RELEASE, _TWOPASS_ROWS, cfg=_TWOPASS_CFG,
              _verify_album=lambda root, c, is_cd: (False, ["CRC mismatch"]))
assert run.job["state"] == "error", run.job
assert len(run.stub.enqueued) == 1, run.stub.enqueued      # asked for ONCE
assert len(run.job["attempts"]) == 1, run.job["attempts"]
assert "(1 attempt(s)" in run.job["result"]["error"], run.job["result"]

# --------------------------------------------------------------------------- #
# 17e. `soulseek_clear_downloads` (Settings → Soulseek, ON by default): the
#      import MOVES the album into the library, so what this job left in the
#      download dir is staging and goes with the import — only that job's own
#      folder, only inside the download dir (`soulseek_download_dir` can be a
#      path the user picked), only after an import that really landed, and
#      always reported: what went, and what was kept with the reason.
# --------------------------------------------------------------------------- #
CLEAR_ROWS = [lrow("peer", "Music/Clear/01 - Alpha.flac"),
              lrow("peer", "Music/Clear/02 - Beta.flac", 210.0)]
CLEAR_FILES = ["Music/Clear/01 - Alpha.flac", "Music/Clear/02 - Beta.flac"]

# (a) ON: the folder the job downloaded is deleted, and the job says so.
run = run_job(DIGITAL_RELEASE, CLEAR_ROWS, keep_dir=True,
              _clear_downloads=soulseek_auto._clear_downloads)
try:
    assert run.job["state"] == "done" and run.imported, run.job
    assert _tree_files(run.ddir) == [], _tree_files(run.ddir)
    _cleared = run.job["result"]
    assert _cleared["download_cleared"] is True, _cleared
    assert (_cleared["download_removed"]["files"],
            _cleared["download_removed"]["dirs"]) == (2, 1), _cleared["download_removed"]
    assert _cleared["download_kept"] == [], _cleared
    assert any(m.startswith("Cleared the download:") for m in
               [e["msg"] for e in run.job["log"]]), run.job["log"][-4:]
finally:
    shutil.rmtree(run.ddir, ignore_errors=True)

# (b) OFF: everything stays, and the result says why nothing was touched.
run = run_job(DIGITAL_RELEASE, CLEAR_ROWS, keep_dir=True,
              cfg=dict(JOB_CFG, soulseek_clear_downloads=False),
              _clear_downloads=soulseek_auto._clear_downloads)
try:
    assert run.job["state"] == "done", run.job
    assert sorted(_tree_files(run.ddir)) == CLEAR_FILES, _tree_files(run.ddir)
    assert run.job["result"]["download_cleared"] is False, run.job["result"]
    assert run.job["result"]["download_removed"]["files"] == 0, run.job["result"]
    assert run.job["result"]["download_kept"] == \
        ["the download is kept: the setting is off"], run.job["result"]
finally:
    shutil.rmtree(run.ddir, ignore_errors=True)


def import_fails(root, release, cfg, media):
    """A real _import failure: the album could not be moved (a file still open),
    so nothing landed in the library and the files must stay for the retry."""
    raise RuntimeError("a file inside the album is still open")


# (c) a FAILED import keeps its download: deleting it here would make the retry
#     download the whole album again.
run = run_job(DIGITAL_RELEASE, CLEAR_ROWS, keep_dir=True, _import=import_fails,
              _clear_downloads=soulseek_auto._clear_downloads)
try:
    assert run.job["state"] == "error", run.job
    assert sorted(_tree_files(run.ddir)) == CLEAR_FILES, _tree_files(run.ddir)
    assert "still open" in run.job["result"]["error"], run.job["result"]
    assert "download_cleared" not in run.job["result"], run.job["result"]
finally:
    shutil.rmtree(run.ddir, ignore_errors=True)

# (d) the delete is scoped to THIS download's own bytes: a folder that also
#     holds another download's file keeps the folder (this candidate's own file
#     still goes), and nothing outside the download dir is ever touched.
_settle_clear = tempfile.mkdtemp(prefix="mlo-clear-")
_outside = tempfile.mkdtemp(prefix="mlo-clear-outside-")
try:
    put_file(_settle_clear, "Music", "Own", "01 - Alpha.flac")
    _foreign = put_file(_settle_clear, "Music", "Own", "notes.txt")
    _outside_file = put_file(_outside, "Music", "Own", "02 - Beta.flac")
    _found = {"username": "peer", "wanted": [
        {"filename": "Music/Own/01 - Alpha.flac", "size": 0},
        {"filename": "Music/Own/02 - Beta.flac", "size": 0}]}
    _res = soulseek_auto._clear_downloads(
        SimpleNamespace(clear_transfer_files=lambda *a, **k: {"files_deleted": 0}),
        _settle_clear, _found, {})
    assert _res["download_cleared"] is True, _res
    # the candidate's own file is gone, the other download's file keeps the
    # folder standing, and the file planted OUTSIDE the download dir is untouched
    assert not os.path.exists(os.path.join(_settle_clear, "Music", "Own",
                                           "01 - Alpha.flac")), _res
    assert os.path.exists(_foreign), "another download's file was deleted"
    assert os.path.exists(_outside_file), "a file outside the download dir was deleted"
    assert any("file(s) of another download" in k for k in _res["download_kept"]), _res
finally:
    shutil.rmtree(_settle_clear, ignore_errors=True)
    shutil.rmtree(_outside, ignore_errors=True)

# --------------------------------------------------------------------------- #
# search progress payload: live counts only, never a countdown — the search
# window is a ceiling that a usable candidate ends early, so a timer readout
# promised a duration the search does not serve.
# --------------------------------------------------------------------------- #
saved_search = soulseek_auto._job["search"]
try:
    soulseek_auto._job_search_progress("q", {"state": "InProgress",
                                             "responseCount": 3, "fileCount": 9})
    _s = soulseek_auto._job["search"]
    assert _s == {"query": "q", "state": "InProgress", "responses": 3, "files": 9}, _s
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

# 19. A search that came back empty-ish (no folder held the whole album) used
#     to end the job on "No candidate folder contained every track" — the user
#     waited out a search window and lost the release. On the INTERACTIVE path
#     the job now parks on the same prompt the lossy branch uses and offers the
#     wishes list; accepting creates a wish carrying the very queries the job
#     searched with, so the background worker can fill it later with no second
#     decision, and the job ends "done" with REAL keys plus wished/wish_id.
from server import wishes as wishes_store   # noqa: E402

_wish_dir = tempfile.mkdtemp(prefix="mlo-wish-")
_saved_wish_init = wishes_store._initialized
NO_RESULT_MSG = ("No candidate folder contained every track (and cue/log per "
                 "disc for CD). Try the manual entry or different search terms.")
try:
    with Patch(wishes_store, db_path=lambda: os.path.join(_wish_dir, "wishes.db")):
        # The store creates its schema on FIRST USE, and "first use" is a
        # process-wide flag: whatever initialized it earlier in this process
        # (another block of this suite, a route) leaves it set, so the fresh DB
        # patched in here would be opened WITHOUT its tables and every wish
        # write would fail ("no such table: wishes"). The job then ended in
        # `error` instead of parking on its offer — the reason this suite flaked
        # on CI while passing on a developer machine.
        wishes_store._initialized = False
        run = run_job(JOB_RELEASE, [], confirm_lossy=True, answer=True)
        assert run.prompt, "the interactive job never offered the wishes list"
        assert run.prompt["reason"] == "no_results", run.prompt
        # the job's OWN query strings ride along ("job album" is resolved to
        # the release's title, exactly what the search was issued with)
        assert run.prompt["queries"] == ["Job Album"], run.prompt
        assert run.prompt["formats"] == [] and run.prompt["candidates"] == [], run.prompt
        # MEASURED, not advertised: this slskd double answers on the first poll,
        # so the search really took no time at all and the prompt says so. The
        # prompt used to publish the CEILING (the 5s window this config asks for
        # + the 45s grace tail = 50) as if the search had spent it.
        assert run.prompt["waited"] == 0, run.prompt
        assert run.prompt["waited"] == int(run.clock.now), run.prompt
        assert run.job["state"] == "done", run.job["state"]
        result = run.job["result"]
        assert result["wished"] is True and isinstance(result["wish_id"], int), result
        assert result["album_path"] is None and result["imported"] == 0, result
        assert result["organized"] == 0, result
        assert not run.imported and not run.submitted(), (run.imported, run.submitted())
        assert "Added to wishes" in [e["msg"] for e in run.job["log"]][-1], run.job["log"][-1]
        wish = wishes_store.get_wish(result["wish_id"])
        assert wish and wish["release_mbid"] == JOB_RELEASE["id"], wish
        assert wish["queries"] == run.prompt["queries"], wish
        assert wish["status"] == "wanted", wish
        assert (wish["title"], wish["artist"], wish["year"]) == \
            ("Job Album", "Job Artist", "1996"), wish
        # The wish RECORDS the attempt that just failed. Born due (no
        # `last_search`, `retry_at` 0), the worker's next pass — at most two
        # minutes later — re-ran this very search against the very peers that
        # had just failed on every candidate. The retry policy's own numbers
        # (server/wishes: attempts, empty-search budget, backoff) now apply to a
        # job the USER ran exactly as they do to one the worker ran itself, and
        # `due_at` is what the next pass asks.
        assert (wish["attempts"], wish["not_found"]) == (1, 1), wish
        assert wish["last_error"] == NO_RESULT_MSG, wish
        _backoff = wishes_store.retry_delay(dict(JOB_CFG), 1)
        assert _backoff >= 60 and wish["retry_at"] - run.clock.now >= _backoff - 1, \
            (wish["retry_at"], run.clock.now, _backoff)

        # Declining keeps the old behaviour: the job fails with the same
        # message and nothing is wished.
        run = run_job(JOB_RELEASE, [], confirm_lossy=True, answer=False)
        assert run.prompt["reason"] == "no_results", run.prompt
        assert run.job["state"] == "error", run.job["state"]
        assert run.job["result"]["error"] == NO_RESULT_MSG, run.job["result"]

        # The background wishes worker runs with confirm_lossy=False: a wish
        # must NEVER park to ask whether it should become another wish.
        run = run_job(JOB_RELEASE, [])
        assert run.prompt is None, run.prompt
        assert run.job["state"] == "error", run.job["state"]
        assert run.job["result"]["error"] == NO_RESULT_MSG, run.job["result"]

        # ...and the knob (and the missing MusicBrainz id) turn the offer off
        # on the interactive path too. Both cases end on the old error.
        run = run_job(JOB_RELEASE, [], confirm_lossy=True,
                      cfg=dict(JOB_CFG, soulseek_auto_wish_prompt=False))
        assert run.prompt is None and run.job["state"] == "error", run.job
        run = run_job(dict(JOB_RELEASE, id=None), [], confirm_lossy=True)
        assert run.prompt is None and run.job["state"] == "error", run.job

        # Cancelling the parked prompt (the user walked away) must not leave a
        # wish behind either: the job ends "cancelled" with nothing added.
        _before = len(wishes_store.list_wishes())
        run = run_job(JOB_RELEASE, [], confirm_lossy=True, answer="cancel")
        assert run.prompt["reason"] == "no_results", run.prompt
        assert run.job["state"] == "cancelled", run.job["state"]
        assert len(wishes_store.list_wishes()) == _before, "cancel added a wish"
finally:
    wishes_store._initialized = _saved_wish_init
    shutil.rmtree(_wish_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# The duration the wish offer reports is MEASURED, not the cap it was allowed.
# A search ends the moment it is terminal (or a usable folder appears), so the
# configured window plus the grace tail — the number the prompt used to publish
# — is a ceiling the wait rarely reaches. The fake clock moves only when the
# poll loop sleeps, so the seconds asserted here are the loop's own.
# --------------------------------------------------------------------------- #
class LazySearch(AutoSlsk):
    """AutoSlsk whose searches stay InProgress for the first `pending` polls;
    the next one is terminal. One poll per tick, one `_SEARCH_POLL_S` sleep per
    tick — so `pending` polls are `pending * 0.75` seconds of waiting."""

    def __init__(self, ddir, rows, pending=4):
        super().__init__(ddir, rows)
        self.pending = pending

    def search_results(self, sid):
        if self.pending > 0:
            self.pending -= 1
            return {"state": "InProgress", "isComplete": False, "responses": []}
        return super().search_results(sid)


run = run_job(JOB_RELEASE, [], stub_cls=LazySearch, confirm_lossy=True, answer=False)
assert run.prompt and run.prompt["reason"] == "no_results", run.prompt
assert run.prompt["waited"] == 3, run.prompt       # 4 polls x 0.75s, floored
assert run.prompt["waited"] == int(run.clock.now), (run.prompt, run.clock.now)
# …and the 5s + 45s ceiling the SAME prompt used to report is nowhere in it:
# the search was terminal on the fifth poll, long before any of it elapsed.
assert JOB_CFG["soulseek_auto_search_wait"] + 45 != run.prompt["waited"], run.prompt
# The job's own log carries the same measured seconds, so the line that
# advertises the ceiling is not the only duration a reader sees.
assert any("search(es) in 3s" in e["msg"] for e in run.job["log"]), \
    [e["msg"] for e in run.job["log"]]


# --------------------------------------------------------------------------- #
# Search shape: a PHYSICAL release goes out with the traits of its pressing
# ALONE (the catalog number and the barcode — broader templates drag in every
# other pressing), DIGITAL media keeps the broad wording it has no alternative
# to, the response limit reaches slskd, and a search still running when the
# window closes is cancelled instead of left occupying slskd's search slots.
# --------------------------------------------------------------------------- #
assert soulseek_auto.release_queries(JOB_RELEASE, {}) == ["CAT-1"], \
    soulseek_auto.release_queries(JOB_RELEASE, {})
# A pressing states its BARCODE too, and that is the other trait unique to it:
# the two go out as two queries, in template order.
assert soulseek_auto.release_queries(
    dict(JOB_RELEASE, barcode="4571694676537"), {}) == \
    ["CAT-1", "4571694676537"]
# A physical release MusicBrainz states NO catalog number and no barcode for
# falls back to its label and country — never to the artist/title wording,
# which is the query that asks the network for every other pressing of the same
# album (and made a CD job take a WEB rip).
assert soulseek_auto.release_queries(
    dict(JOB_RELEASE, catalog_number="", label="Nippon Columbia", country="JP"), {}) == \
    ["Nippon Columbia JP 1996"]
# …and a pressing stating neither label nor country has nothing left to
# identify it by: no query at all (the job reports "nothing to search by")
# rather than one that searches for a different release.
assert soulseek_auto.release_queries(
    dict(JOB_RELEASE, catalog_number="", country=""), {}) == []
assert soulseek_auto.release_queries(
    dict(JOB_RELEASE, catalog_number="", barcode="", label="EMI", country="GB"), {}) == \
    ["EMI GB 1996"]
# DIGITAL media is the one kind that keeps the broad wording: it has no
# pressing trait to be identified by.
assert soulseek_auto.release_queries(
    dict(JOB_RELEASE, medium_formats=["Digital Media"], catalog_number=""), {}) == \
    ["Job Artist Job Album 1996"]
# The physical/digital split reads the medium through mlo.release_choice
# .media_formats: every physical carrier is a PRESSING, an empty or unknown
# medium list counts as one, and only "all Digital Media" is digital.
assert soulseek_auto._is_digital({"medium_formats": ["Digital Media"]}) is True
assert soulseek_auto._is_digital({"medium_formats": ["CD", "DVD"]}) is False
assert soulseek_auto._is_digital({"medium_formats": ["12\" Vinyl"]}) is False
assert soulseek_auto._is_digital({"medium_formats": []}) is False
assert soulseek_auto._is_digital({}) is False
assert soulseek_auto._is_digital({"medium": "Digital Media"}) is True
for _medium in ("Vinyl", "Cassette", "SACD", "CD-R", "SHM-CD", "Blu-spec CD",
                "12\" Vinyl", "DVD-Audio"):
    assert soulseek_auto.release_queries(
        dict(JOB_RELEASE, medium_formats=[_medium], catalog_number="", barcode=""),
        {}) == ["GB 1996"], _medium      # a pressing: label + country, no title
    assert soulseek_auto.release_queries(
        dict(JOB_RELEASE, medium_formats=[_medium]), {}) == ["CAT-1"], _medium
# A configured template list still wins over the default.
assert soulseek_auto.release_queries(
    JOB_RELEASE, {"soulseek_auto_cd_queries": ["artist album"]}) == ["Job Artist Job Album"]
# …and the physical key does the same for EVERY pressing, so a widened vinyl
# search is one setting, not a code change.
assert soulseek_auto.release_queries(
    dict(JOB_RELEASE, medium_formats=["Vinyl"]),
    {"soulseek_auto_physical_queries": ["artist album catalognumber"]}) == \
    ["Job Artist Job Album CAT-1"]
assert soulseek_auto.release_queries(
    dict(JOB_RELEASE, medium_formats=["Vinyl"]),
    {"soulseek_auto_physical_queries": "catalognumber; barcode"}) == ["CAT-1"]
# The legacy CD key only wins when a config really SET it: its shipped default
# (the catalog number alone) is not a decision, so an untouched install follows
# the physical default — which adds the barcode — instead.
assert soulseek_auto.release_queries(
    dict(JOB_RELEASE, barcode="4571694676537"),
    {"soulseek_auto_cd_queries": ["catalognumber"]}) == ["CAT-1", "4571694676537"]
assert soulseek_auto.release_queries(
    dict(JOB_RELEASE, barcode="4571694676537"),
    {"soulseek_auto_cd_queries": ["catalognumber", "barcode"]}) == \
    ["CAT-1", "4571694676537"]
# …and it is the CD's key alone: a vinyl ignores it.
assert soulseek_auto.release_queries(
    dict(JOB_RELEASE, medium_formats=["Vinyl"], catalog_number="", barcode=""),
    {"soulseek_auto_cd_queries": ["artist album"]}) == ["GB 1996"]

# The physical key is a SHIPPED default like the other two query keys: the
# settings UI edits a template list as one ";"-separated line, an empty/blank
# list falls back to the shipped one, and a saved config holding a list a
# physical release was already searched with before this key existed (the CD
# key's default, the digital key's old defaults — the fall-through this key
# ends) was never a decision about it, so it follows the shipped default too.
from mlo import config as mlo_config     # noqa: E402

assert mlo_config.DEFAULT_CONFIG["soulseek_auto_physical_queries"] == \
    ["catalognumber", "barcode"], mlo_config.DEFAULT_CONFIG["soulseek_auto_physical_queries"]
for _saved, _want in (
        ("catalognumber; barcode", ["catalognumber", "barcode"]),
        ([], ["catalognumber", "barcode"]),
        (["  "], ["catalognumber", "barcode"]),
        (None, ["catalognumber", "barcode"]),
        (["catalognumber"], ["catalognumber", "barcode"]),
        (["artist album year", "artist album"], ["catalognumber", "barcode"]),
        (["catalognumber", "artist album catalognumber", "artist album"],
         ["catalognumber", "barcode"]),
        (["label country year"], ["label country year"]),
        (["catalognumber", "barcode", "label", "country", "year", "date", "artist",
          "album"], ["catalognumber", "barcode", "label", "country", "year", "date"])):
    assert mlo_config.normalize_config(
        {"soulseek_auto_physical_queries": _saved})["soulseek_auto_physical_queries"] == \
        _want, _saved
# the CD and digital keys keep their own values — the new key did not become a
# second home for them
assert mlo_config.normalize_config({})["soulseek_auto_cd_queries"] == ["catalognumber"]
assert mlo_config.normalize_config({})["soulseek_auto_digital_queries"] == \
    ["artist album year"]

# A CD carrying SEVERAL catalog numbers (one per label/pressing) is searched
# for EACH number as its own query, in MusicBrainz order — searching only the
# first lost every other pressing.
TWO_CAT = dict(JOB_RELEASE, catalog_numbers=["CAT-1", "CAT-2"])
assert soulseek_auto.release_queries(TWO_CAT, {}) == ["CAT-1", "CAT-2"], \
    soulseek_auto.release_queries(TWO_CAT, {})
# a blank and a repeated number are dropped, never searched twice
assert soulseek_auto.release_queries(
    dict(JOB_RELEASE, catalog_numbers=["CAT-1", "  ", "CAT-2", "CAT-1"]), {}) == \
    ["CAT-1", "CAT-2"]
# a template naming the field inside longer wording expands the same way
assert soulseek_auto.release_queries(
    TWO_CAT, {"soulseek_auto_cd_queries": ["artist album catalognumber"]}) == \
    ["Job Artist Job Album CAT-1", "Job Artist Job Album CAT-2"]
# ...and a template that does NOT name it still yields exactly one query
assert soulseek_auto.release_queries(
    TWO_CAT, {"soulseek_auto_cd_queries": ["artist album"]}) == ["Job Artist Job Album"]
assert soulseek_auto.release_queries(
    TWO_CAT, {"soulseek_auto_cd_queries": ["artist album", "catalognumber"]}) == \
    ["Job Artist Job Album", "CAT-1", "CAT-2"]
# a release with a dozen numbers must not flood the network: four, at most
assert soulseek_auto.release_queries(
    dict(JOB_RELEASE, catalog_numbers=[f"C{i}" for i in range(1, 8)]), {}) == \
    ["C1", "C2", "C3", "C4"]
# an EMPTY plural key still means "no catalog number or barcode" -> the
# pressing fallback (its label and country), never the artist/title wording
assert soulseek_auto.release_queries(
    dict(JOB_RELEASE, catalog_numbers=[], catalog_number=""), {}) == ["GB 1996"]
# the plural key wins over the singular; a singular-only payload still works
assert soulseek_auto.release_queries(
    dict(JOB_RELEASE, catalog_numbers=["NEW"], catalog_number="OLD"), {}) == ["NEW"]
assert soulseek_auto.release_queries(
    dict(JOB_RELEASE, catalog_numbers=None), {}) == ["CAT-1"]
# the job's own template seam agrees with release_queries on the same inputs
for _tpl in (["catalognumber"], ["artist album catalognumber"], ["artist album"]):
    assert soulseek_auto._build_from_templates(_tpl, TWO_CAT) == \
        soulseek_auto.release_queries(TWO_CAT, {"soulseek_auto_cd_queries": _tpl}), _tpl

# --------------------------------------------------------------------------- #
# Search text: what reaches slskd is what a SHARE FOLDER NAME can carry. slskd
# POSTs `searchText` verbatim, so a MusicBrainz title's punctuation used to go
# to the network as typed — and the full-width forms a Japanese title uses are
# exactly what NFKD folds onto ASCII punctuation, which is then dropped.
# --------------------------------------------------------------------------- #
_s = soulseek_auto._search_text
# The full-width "！" of a real MusicBrainz title folds to "!" and goes; the
# letters of every script stay, voicing marks and all ("ぴ" must NOT be folded
# to "ひ" the way an accent is).
assert _s("ぴーなた") == "ぴーなた", _s("ぴーなた")
assert _s("アンタに言ってんの！") == "アンタに言ってんの", _s("アンタに言ってんの！")
assert _s("Мумий Тролль — Точно Ртуть!") == "Мумий Тролль - Точно Ртуть"
assert _s("ヴィヴァルディ・四季") == "ヴィヴァルディ 四季", _s("ヴィヴァルディ・四季")
# quotes, brackets, slashes, "? *", emoji and typographic dashes: dropped, but
# as WORD breaks — "Album(Remastered)" and "Live/2" are two words, and gluing
# them into "AlbumRemastered"/"Live2" would search for a term no share carries.
assert _s('AC/DC “Live” – Album') == "AC DC Live - Album", _s('AC/DC “Live” – Album')
assert _s("Album(Remastered)") == "Album Remastered", _s("Album(Remastered)")
assert _s("Live/2 🎵") == "Live 2", _s("Live/2 🎵")
assert _s("a\nb\tc") == "a b c", _s("a\nb\tc")
# Accents fold onto the plain letter in Latin text (either spelling of a title
# must produce one query), and the punctuation a share DOES carry is kept: the
# hyphen inside a catalog number above all.
assert _s("Björk – Motörhead") == "Bjork - Motorhead", _s("Björk – Motörhead")
assert _s("Don’t Stop (Remastered)") == "Don't Stop Remastered"
assert _s("WPCL-1234") == "WPCL-1234" and _s("k.d. lang") == "k.d. lang"
# A term that was ALL punctuation keeps its plain normalized text: an empty
# query would silently drop that field from the search entirely.
assert _s("!!!") == "!!!" and _s("") == "" and _s(None) == ""

# …and the rendered query is clean: the release the issue was filed about
# (88bf1693-2cde-48e3-9a40-cc928ad283f6) is DIGITAL, so it searches its title
# and year — with the full-width "!" gone and the kana intact.
CJK_RELEASE = {
    "id": "88bf1693-2cde-48e3-9a40-cc928ad283f6",
    "title": "アンタに言ってんの！", "date": "2026-07-29",
    "medium_formats": ["Digital Media"], "country": "JP",
    "catalog_number": "", "barcode": "4571694676537",
    "release_group_id": "b4c0b0f2-0000-4000-8000-000000000000",
    "artists": [{"name": "ぴーなた", "mbid": "aaaaaaaa-0000-4000-8000-000000000001"}],
}
assert soulseek_auto.release_queries(CJK_RELEASE, {}) == \
    ["ぴーなた アンタに言ってんの 2026"], \
    soulseek_auto.release_queries(CJK_RELEASE, {})

# --------------------------------------------------------------------------- #
# MusicBrainz aliases: a release MusicBrainz files under a translated name is
# unfindable on Soulseek under the name it carries — the folders there are
# named with the ALIAS. The extra queries are the SAME templates with the alias
# in place of the artist (and of the album, when the release group has one),
# built from the ids the release already carries, and they must exist BEFORE
# the wish offer because a wish replays the queries it is stored with.
# --------------------------------------------------------------------------- #
from server import integrations as _intg   # noqa: E402

_real_mb_cached = _intg.mb_get_cached
_mb_calls = []


def _fake_mb_cached(endpoint, params=None, timeout=30.0, retries=5):
    _mb_calls.append(endpoint)
    if endpoint.startswith("artist/"):
        return {"aliases": [
            {"name": "ピナタ", "locale": "ja", "primary": True},
            {"name": "pinata", "locale": "en", "primary": True},
            {"name": "Pinata", "locale": "en", "primary": False}]}
    if endpoint.startswith("release-group/"):
        return {"aliases": [{"name": "What I'm Telling You", "locale": "en",
                             "primary": True},
                            {"name": "アンタに言ってんの", "locale": "ja"}]}
    return {}


try:
    _intg.mb_get_cached = _fake_mb_cached
    _mb_calls.clear()
    _q = soulseek_auto.release_queries(CJK_RELEASE, {"beets_locale": "en"})
    # the alias-artist query is the template with `pinata` in the artist slot,
    # and the alias TITLE replaces the album: MusicBrainz' own English name for
    # the release group is what an English share folder carries.
    assert _q == ["ぴーなた アンタに言ってんの 2026",
                  "pinata What I'm Telling You 2026"], _q
    # the ids the release ALREADY carries are what was asked about — the cached
    # client, one request per entity
    assert _mb_calls == [
        f"artist/{CJK_RELEASE['artists'][0]['mbid']}",
        f"release-group/{CJK_RELEASE['release_group_id']}"], _mb_calls
    # the reader's locale decides WHICH alias: a ja reader is served the ja
    # spelling of the artist (a different one — katakana against hiragana), and
    # the ja alias TITLE is the title itself, so no second query is added for it
    _mb_calls.clear()
    _ja = soulseek_auto.release_queries(CJK_RELEASE, {"beets_locale": "ja"})
    assert _ja == ["ぴーなた アンタに言ってんの 2026",
                   "ピナタ アンタに言ってんの 2026"], _ja
    # A NAME ALREADY IN THE READER'S SCRIPT COSTS NO REQUEST AT ALL: "Job
    # Artist" has nothing to translate, so a Latin-script library asks
    # MusicBrainz nothing (this is the beets plugin's own rule).
    _mb_calls.clear()
    assert soulseek_auto.release_queries(JOB_RELEASE, {}) == ["CAT-1"], _mb_calls
    assert _mb_calls == [], _mb_calls
    # A release with no MusicBrainz ids has nothing to ask about either.
    _mb_calls.clear()
    assert soulseek_auto.release_queries(
        dict(CJK_RELEASE, release_group_id=None,
             artists=[{"name": "ぴーなた", "mbid": ""}]), {}) == \
        ["ぴーなた アンタに言ってんの 2026"], _mb_calls
    assert _mb_calls == [], _mb_calls
    # An alias lookup that fails NEVER costs the release its search: the
    # configured templates are already in hand.
    def _boom(endpoint, params=None, timeout=30.0, retries=5):
        raise RuntimeError("MusicBrainz is busy")
    _intg.mb_get_cached = _boom
    assert soulseek_auto.release_queries(CJK_RELEASE, {}) == \
        ["ぴーなた アンタに言ってんの 2026"], "a failed alias lookup lost the search"
    # …and the alias queries are CAPPED like the catalog expansion, so a
    # release with many aliases (or many templates) cannot flood the network.
    def _many(endpoint, params=None, timeout=30.0, retries=5):
        if endpoint.startswith("artist/"):
            return {"aliases": [{"name": f"alias{i}", "locale": "en", "primary": i == 0}
                                for i in range(9)]}
        return {"aliases": [{"name": f"title{i}", "locale": "en"} for i in range(9)]}
    _intg.mb_get_cached = _many
    _capped = soulseek_auto.release_queries(
        CJK_RELEASE,
        {"beets_locale": "en",
         "soulseek_auto_digital_queries": ["artist album year", "album", "artist",
                                           "album year", "artist year"]})
    _alias_extra = [q for q in _capped if "alias0" in q or "title0" in q]
    assert len(_alias_extra) == soulseek_auto._MAX_CATALOG_QUERIES, _capped
    assert "alias0 2026" not in _capped, _capped   # the 5th template never rendered

    # …and a JOB really searches with them: the alias query is one of the
    # queries POSTed to slskd, which is what makes a wish created from this job
    # replay it (a wish stores the queries the job searched with).
    _intg.mb_get_cached = _fake_mb_cached
    _cjk_digital = dict(DIGITAL_RELEASE, id=CJK_RELEASE["id"],
                        title=CJK_RELEASE["title"], date=CJK_RELEASE["date"],
                        country=CJK_RELEASE["country"],
                        barcode=CJK_RELEASE["barcode"],
                        catalog_number=CJK_RELEASE["catalog_number"],
                        release_group_id=CJK_RELEASE["release_group_id"],
                        artists=CJK_RELEASE["artists"])
    run = run_job(_cjk_digital, DIGITAL_ROWS, queries=[])
    assert run.imported, run.job["result"]
    assert [q for q, _t, _l in run.stub.searches] == [
        "ぴーなた アンタに言ってんの 2026",
        "pinata What I'm Telling You 2026"], run.stub.searches
finally:
    _intg.mb_get_cached = _real_mb_cached

# --------------------------------------------------------------------------- #
# RELEASECOUNTRY carries the release's WHOLE set of countries, not the singular
# `country` MusicBrainz puts first (issue #18's write side, folded in here): a
# worldwide reissue of a pressing is filed under both, and a tag written from
# the singular loses one. The singular still leads the value — it is what every
# other writer stamps and the FIRST entry is what the naming script reads back.
# --------------------------------------------------------------------------- #
assert soulseek_auto._release_country_tag({"country": "GB"}) == "GB"
assert soulseek_auto._release_country_tag(
    {"country": "GB", "countries": []}) == "GB"
# an event that states no code (a historic area) contributes nothing, and the
# singular is moved to the front so mlo.naming._first_multi keeps reading it
assert soulseek_auto._release_country_tag(
    {"country": "US", "countries": [
        {"code": "JP", "country": "Japan"},
        {"code": "", "country": "Europe"},
        {"code": "US", "country": "United States"}]}) == "US; JP"
assert soulseek_auto._release_country_tag(
    {"countries": [{"code": "US", "country": "United States"}]}) == "US"
# a code repeated by two events is written once
assert soulseek_auto._release_country_tag(
    {"country": "DE", "countries": [{"code": "DE"}, {"code": "DE"}]}) == "DE"

from mlo import audio as mlo_audio     # noqa: E402


class RecorderAudio:
    """`mlo.audio.AudioFile` stand-in: records every tag the stamping writes,
    so "what lands in RELEASECOUNTRY" is answerable without a real encoder.
    Written once (on disk) through the app's own tag layer in production."""

    written = {}

    def __init__(self, path):
        self.path = path
        self.audio = object()      # not None: the file "can be read"
        self.tags = {}

    def get_tag(self, name):
        return self.tags.get(name, "")

    def set_tag(self, name, value):
        self.tags[name] = str(value).strip()
        RecorderAudio.written.setdefault(os.path.basename(self.path), {})[name] = \
            str(value).strip()
        return True


_stamp_dir = tempfile.mkdtemp(prefix="mlo-stamp-")
try:
    put_file(_stamp_dir, "01 - Alpha.flac")
    put_file(_stamp_dir, "02 - Beta.flac")
    _wb_release = dict(JOB_RELEASE, country="US", countries=[
        {"code": "JP", "country": "Japan", "date": "2026-07-29"},
        {"code": "US", "country": "United States", "date": "2026-07-29"}])
    with Patch(mlo_audio, AudioFile=RecorderAudio):
        _n = soulseek_auto._stamp_mb_tags(_stamp_dir, _wb_release)
    assert _n == 2, _n
    _tags = RecorderAudio.written["01 - Alpha.flac"]
    assert _tags["RELEASECOUNTRY"] == "US; JP", _tags["RELEASECOUNTRY"]
    # the rest of the identity still lands, so the whole set replaced the
    # singular inside the stamp rather than the stamp itself
    assert _tags["MUSICBRAINZ_ALBUMID"] == JOB_RELEASE["id"], _tags
    assert _tags["DATE"] == "1996" and _tags["CATALOGNUMBER"] == "CAT-1", _tags
    assert _tags["TITLE"] == "Alpha", _tags      # the per-track tags still run
finally:
    shutil.rmtree(_stamp_dir, ignore_errors=True)

# --------------------------------------------------------------------------- #
# A wish stores the queries it was created with, but an EMPTY list must not pin
# the search to nothing: the worker hands the job None, and the job derives the
# EXPANDED set from the release itself. Two seams — wishes_worker._run_one
# (what reaches start_job) and the job harness with no queries (what the job
# then searches with). The wish schema is unchanged.
# --------------------------------------------------------------------------- #
from server import wishes_worker          # noqa: E402
from server import integrations as intg   # noqa: E402

_wish_none_dir = tempfile.mkdtemp(prefix="mlo-wish-none-")
_saved_wish_init_none = wishes_store._initialized
_sj_calls = []
try:
    with Patch(wishes_store, db_path=lambda: os.path.join(_wish_none_dir, "wishes.db")):
        # The store creates its schema on FIRST USE, and "first use" is a
        # process-wide flag: whatever initialized it earlier in this process
        # (another block of this suite, a route) leaves it set, so the fresh DB
        # patched in here would be opened WITHOUT its tables and every wish
        # write would fail ("no such table: wishes"). The job then ended in
        # `error` instead of parking on its offer — the reason this suite flaked
        # on CI while passing on a developer machine.
        wishes_store._initialized = False
        _w_empty = wishes_store.add_wish(JOB_RELEASE["id"], title="Job Album",
                                         artist="Job Artist", year="1996")
        assert _w_empty["queries"] == [], _w_empty          # nothing stored
        _w_pinned = wishes_store.add_wish("44444444-5555-6666-7777-888888888888",
                                          title="Other", artist="Other",
                                          queries=["pinned query"])
        with Patch(wishes_store, mark_searching=lambda wid: None,
                   mark_wanted=lambda wid, **kw: None,
                   log=lambda level, msg: None), \
             Patch(soulseek, is_running=lambda *a, **k: True,
                   server_state=lambda *a, **k: {"isLoggedIn": True}), \
             Patch(soulseek_auto, job_active=lambda: False,
                   start_job=lambda **kw: (_sj_calls.append(kw)
                                           or {"ok": False, "error": "stop here"})), \
             Patch(intg, resolve_release=lambda mbid: (dict(TWO_CAT, id=mbid), mbid)):
            assert wishes_worker._run_one(_w_empty, {"wishes_auto_import": True}) == "pending"
            assert wishes_worker._run_one(_w_pinned, {"wishes_auto_import": True}) == "pending"
finally:
    wishes_store._initialized = _saved_wish_init_none
    shutil.rmtree(_wish_none_dir, ignore_errors=True)

# empty stored queries -> None (the job, not the wish, decides the queries)
assert _sj_calls[0]["queries"] is None, _sj_calls[0]
assert _sj_calls[0]["release_mbid"] == JOB_RELEASE["id"], _sj_calls[0]
# ...a wish that DOES store queries keeps them, pinned
assert _sj_calls[1]["queries"] == ["pinned query"], _sj_calls[1]

# ...and the derived set really is the EXPANDED one: a job with no queries
# searches every catalog number, in order.
run = run_job(TWO_CAT, ONE_DISC_ROWS, queries=[])
assert run.imported, run.job["result"]
assert [q for q, _t, _l in run.stub.searches] == ["CAT-1", "CAT-2"], run.stub.searches

clock = FakeClock()
_real_time = soulseek_auto.time
soulseek_auto.time = clock
try:
    slsk = FakeSlsk([PENDING])           # never reaches a terminal state
    results, errors, skipped = soulseek_auto._search_queries(
        slsk, ["CAT-1"], wait_s=2, response_limit=7)
    assert slsk.limits == [7], slsk.limits          # the limit reaches slskd
    assert slsk.timeouts == [2000], slsk.timeouts   # ms, quiet window
    assert slsk.cancelled == ["sid-1"], slsk.cancelled
    assert results == [] and skipped == 0 and errors, (results, errors, skipped)
finally:
    soulseek_auto.time = _real_time

# --------------------------------------------------------------------------- #
# Peer speed: it decides between equally complete folders (a fast peer is the
# one that actually serves the album), and its bonus is capped below a single
# matched track so it can never buy an incomplete folder.
# --------------------------------------------------------------------------- #
def _speedy(user, mib_per_s):
    out = []
    for r in folder(user, "Album FLAC", "flac"):
        out.append({**r, "speed": int(mib_per_s * 1024 * 1024)})
    return out

ranked = soulseek_auto.find_candidates(_speedy("slowpeer", 0.2) + _speedy("fastpeer", 5.0),
                                       RELEASE, CFG)
assert [c["username"] for c in ranked] == ["fastpeer", "slowpeer"], [c["score"] for c in ranked]
assert ranked[0]["score"] > ranked[1]["score"], [c["score"] for c in ranked]

# An incomplete folder is not a candidate at all at the default completeness
# requirement (1.0) — but even where a looser ratio admits it (0.5), its speed
# cannot lift it above the complete folder it is competing with.
partial_fast = _speedy("fastpartial", 20.0)[:1] + [row("fastpartial", "Music/Album FLAC/Album.cue")]
complete_slow = _speedy("slowcomplete", 0.3)
assert [c["username"] for c in soulseek_auto.find_candidates(partial_fast + complete_slow, RELEASE, CFG)] == \
    ["slowcomplete"], "an incomplete folder was scored with completeness required"
loose = {"soulseek_auto_complete_ratio": 0.5}
mixed = soulseek_auto.find_candidates(partial_fast + complete_slow, RELEASE, loose)
by_user = {c["username"]: c for c in mixed}
assert set(by_user) == {"fastpartial", "slowcomplete"}, list(by_user)
assert by_user["slowcomplete"]["complete"] and not by_user["fastpartial"]["complete"], \
    {u: c["complete"] for u, c in by_user.items()}
assert by_user["slowcomplete"]["score"] > by_user["fastpartial"]["score"], \
    (by_user["slowcomplete"]["score"], by_user["fastpartial"]["score"])

# --------------------------------------------------------------------------- #
# 1. A finished file whose slskd transfer record is GONE is still a download —
#    and only under the three fences: no recorded ACTIVE state, the exact
#    expected size, and a settled mtime. Without the disk rule a finished album
#    whose slskd record was pruned/cleared was rejected, its files deleted and
#    the album downloaded again from the next peer.
# --------------------------------------------------------------------------- #
class RecordlessSlsk:
    """slskd transfer stub with one scripted record for the one wanted file:
    `state=None` scripts NO record at all (a cleared transfer list, or a slskd
    that restarted mid-job). `growth` bytes are added per poll."""

    finished_transfer = staticmethod(soulseek.finished_transfer)

    def __init__(self, state=None, growth=0, size=10, touch=None, clock=None):
        self.state, self.growth, self.size = state, growth, size
        self.touch, self.clock = touch, clock
        self.polls, self.cancelled = 0, []

    def downloads_state(self):
        self.polls += 1
        if self.touch:      # the peer is still writing it, between every poll
            os.utime(self.touch, (self.clock.now, self.clock.now))
        if self.state is None:
            return []
        return [{"username": "peer", "directories": [{"files": [
            {"id": "t1", "filename": WANTED[0]["filename"], "state": self.state,
             "bytesTransferred": self.growth * self.polls, "size": self.size,
             "percentComplete": 0, "averageSpeed": 10, "remainingTime": 5}]}]}]

    def cancel_downloads(self, username, transfer_ids):
        self.cancelled.extend(transfer_ids)
        return True


_r1 = tempfile.mkdtemp(prefix="mlo-record-")     # holds the finished file
_r2 = tempfile.mkdtemp(prefix="mlo-nofile-")     # slskd's word, empty disk
_r3 = tempfile.mkdtemp(prefix="mlo-wrongsize-")  # the exact rel path, wrong size
_fork = FakeClock()
_EPOCH = real_time.time()          # a real epoch, so mtimes compare like a run
_fork.now = _EPOCH
_record_saved_time = soulseek_auto.time
_record_saved_log = soulseek_auto._job["log"]
soulseek_auto.time = _fork
try:
    # The file sits in the candidate's OWN album tree (<ddir>/A/…, slskd's
    # `<ddir>/<leaf>` layout), not at the exact remote path, so the acceptance
    # goes through the tree index the real job walks.
    _finished = put_file(_r1, "A", "01 - X.flac", body=b"x" * 10)
    os.utime(_finished, (_EPOCH - 10, _EPOCH - 10))

    # (a) no record at all: the finished file on disk IS the download.
    soulseek_auto._job["log"] = []
    got = soulseek_auto._wait_for_files(RecordlessSlsk(), _r1, "peer", WANTED, 400.0)
    assert list(got) == [WANTED[0]["filename"]], got
    assert got[WANTED[0]["filename"]] == os.path.normpath(_finished), got
    assert _fork.now == _EPOCH, f"an already-finished file was waited on (t={_fork.now - _EPOCH}s)"
    # ...and the user can see WHY it counted instead of a silent disk guess
    _msgs = [e["msg"] for e in soulseek_auto._job["log"]]
    assert any("no live transfer record" in m for m in _msgs), _msgs
    soulseek_auto._job["log"] = _record_saved_log

    # (b) slskd reports a terminal SUCCESS with nothing on disk: a verdict is
    #     not a file, so the wait runs out and drops the candidate.
    _fork.now = _EPOCH
    got = soulseek_auto._wait_for_files(RecordlessSlsk("Completed, Succeeded"),
                                        _r2, "peer", WANTED, 400.0)
    assert got == {}, got
    assert 180 < _fork.now - _EPOCH < 200, _fork.now - _EPOCH

    # (c) a recorded ACTIVE state is not a hole in the record: slskd is still
    #     fetching the file, so a perfect local copy proves nothing. Queued
    #     waits out the queue window (nothing is InProgress), InProgress keeps
    #     its bytes moving and runs to the caller's timeout — neither accepts.
    _fork.now = _EPOCH
    _queued = RecordlessSlsk("Queued")
    got = soulseek_auto._wait_for_files(_queued, _r1, "peer", WANTED, 400.0)
    assert got == {}, got
    assert 180 < _fork.now - _EPOCH < 200, _fork.now - _EPOCH
    assert _queued.cancelled == ["t1"], _queued.cancelled
    _fork.now = _EPOCH
    _moving = RecordlessSlsk("InProgress", growth=3)
    got = soulseek_auto._wait_for_files(_moving, _r1, "peer", WANTED, 400.0)
    assert got == {}, got
    assert _fork.now - _EPOCH >= 400, _fork.now - _EPOCH
    assert _moving.cancelled == ["t1"], _moving.cancelled

    # (d) a size mismatch is a different (or truncated) file, never this one —
    #     in the album tree AND at the exact path slskd would have written.
    _fork.now = _EPOCH
    got = soulseek_auto._wait_for_files(
        RecordlessSlsk(), _r1, "peer",
        [{"filename": WANTED[0]["filename"], "size": 11}], 400.0)
    assert got == {}, got
    assert 180 < _fork.now - _EPOCH < 200, _fork.now - _EPOCH
    _wrong = put_file(_r3, "Music", "A", "01 - X.flac", body=b"x" * 10)
    os.utime(_wrong, (_EPOCH - 10, _EPOCH - 10))
    _fork.now = _EPOCH
    got = soulseek_auto._wait_for_files(
        RecordlessSlsk(), _r3, "peer",
        [{"filename": WANTED[0]["filename"], "size": 11}], 400.0)
    assert got == {}, got
    assert 180 < _fork.now - _EPOCH < 200, _fork.now - _EPOCH

    # (e) a file still being written is not a finished download: a stable mtime
    #     older than two poll intervals is the whole proof, so a peer that keeps
    #     touching it never gets it counted — however perfect its size is.
    _fork.now = _EPOCH
    got = soulseek_auto._wait_for_files(
        RecordlessSlsk(touch=_finished, clock=_fork), _r1, "peer", WANTED, 400.0)
    assert got == {}, got
    assert 180 < _fork.now - _EPOCH < 200, _fork.now - _EPOCH
finally:
    soulseek_auto.time = _record_saved_time
    soulseek_auto._job["log"] = _record_saved_log
    shutil.rmtree(_r1, ignore_errors=True)
    shutil.rmtree(_r2, ignore_errors=True)
    shutil.rmtree(_r3, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 2. A peer whose transfers are ALL Queued is busy, not dead: it is held for
#    its OWN queue budget (_queue_budget) instead of being abandoned at the
#    180 s stall threshold — while a transfer that moved bytes and then went
#    quiet still is.
# --------------------------------------------------------------------------- #
class PeerQueue:
    """One transfer of "peer" for WANTED[0]: `state` is reported on every poll
    and `bytesTransferred` rises for the first `moving_polls` polls only
    (moving_polls=0 = a queue that never moved a byte)."""

    finished_transfer = staticmethod(soulseek.finished_transfer)

    def __init__(self, state="Queued", moving_polls=0, size=10):
        self.state, self.moving_polls, self.size = state, moving_polls, size
        self.polls, self.cancelled = 0, []

    def downloads_state(self):
        self.polls += 1
        return [{"username": "peer", "directories": [{"files": [
            {"id": "t1", "filename": WANTED[0]["filename"], "state": self.state,
             "bytesTransferred": min(self.polls, self.moving_polls) * 3,
             "size": self.size, "percentComplete": 0, "averageSpeed": 30,
             "remainingTime": 5}]}]}]

    def cancel_downloads(self, username, transfer_ids):
        self.cancelled.extend(transfer_ids)
        return True


# 4 files the size of this release's average file queued ahead, at the rate the
# search advertised: 204.8 s of waiting this peer's own queue is worth — the
# same figures _est_timeout budgets the queued part of its deadline from.
_QB_CAND = {"total_size": 10 * _MB, "speed": 100 * _KB, "queue": 4, "files": [{}, {}]}
_budget = soulseek_auto._queue_budget(_QB_CAND)
assert abs(_budget - 204.8) < 1e-6, _budget
assert _budget > soulseek_auto._STALL_AFTER_S, _budget
# a peer that reported no queue at all keeps the old grace, not an instant drop
assert soulseek_auto._queue_budget({"total_size": 10 * _MB, "speed": 100 * _KB,
                                    "queue": 0, "files": [{}, {}]}) == \
    soulseek_auto._STALL_AFTER_S

_qb_dir = tempfile.mkdtemp(prefix="mlo-queue-")
_qb_clock = FakeClock()
_qb_saved = soulseek_auto.time
soulseek_auto.time = _qb_clock
try:
    # (a) every transfer Queued, none InProgress: the stall window is the wrong
    #     rule here — the peer is intact and we are simply behind its queue, so
    #     the candidate gets its own budget (204.8 s), not 180 s.
    q = PeerQueue("Queued")
    got = soulseek_auto._wait_for_files(q, _qb_dir, "peer", WANTED, 400.0,
                                        queue_budget_s=_budget)
    assert got == {}, got
    assert 200 < _qb_clock.now < 210, f"a queued peer was dropped at t={_qb_clock.now}s"
    assert q.cancelled == ["t1"], q.cancelled

    # ...and with no caller budget the old grace still applies, so the .log gate
    # (deliberately cheap and bounded) is not left waiting out a queue.
    _qb_clock.now = 0.0
    soulseek_auto._wait_for_files(PeerQueue("Queued"), _qb_dir, "peer", WANTED, 400.0)
    assert 180 < _qb_clock.now < 190, _qb_clock.now

    # (b) a transfer that DID move bytes and then went quiet is a dead peer: the
    #     stall rule wins even with the generous queue budget in hand.
    _qb_clock.now = 0.0
    stalled = PeerQueue("InProgress", moving_polls=10)
    got = soulseek_auto._wait_for_files(stalled, _qb_dir, "peer", WANTED, 400.0,
                                        queue_budget_s=_budget)
    assert got == {}, got
    assert 180 < _qb_clock.now < 200, \
        f"a stalled transfer was waited out (t={_qb_clock.now}s)"
    assert _qb_clock.now < _budget, _qb_clock.now
finally:
    soulseek_auto.time = _qb_saved
    shutil.rmtree(_qb_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 3. A .log gate that fails leaves NO orphans: the album was never enqueued
#    (the log went out alone), and any bytes sitting in the candidate's tree
#    are deleted — the next candidate must not inherit a rejected peer's
#    partials. The log transfer itself is cancelled in both cases.
# --------------------------------------------------------------------------- #
class GateSlsk(AutoSlsk):
    """AutoSlsk whose .log transfer is scripted (log_state="Queued" = a log
    that never arrives). Records the download tree as it stands when the job
    cancels a rejected candidate's transfers — _run cancels BEFORE it cleans
    up, so that snapshot is evidence the bytes really had arrived."""

    def __init__(self, ddir, responses, log_state="Completed, Succeeded"):
        super().__init__(ddir, responses)
        self.log_state = log_state
        self.trees = []

    def downloads_state(self):
        self.progress_snapshots.append(soulseek_auto.job_state()["progress"])
        return [{"username": user, "directories": [{"files": [
            {"id": name, "filename": name,
             "state": (self.log_state if name.lower().endswith(".log")
                       else "Completed, Succeeded"),
             "bytesTransferred": size, "size": size,
             "percentComplete": 100, "averageSpeed": 0, "remainingTime": 0}
            for name, size in sorted(queue.items())]}]}
            for user, queue in self.queues.items()]

    def cancel_downloads(self, username, transfer_ids, **kw):
        self.trees.append(sorted(_tree_files(self.ddir)))
        return super().cancel_downloads(username, transfer_ids, **kw)


GATE_ROWS = [
    lrow("peer", "Music/Album/01 - Alpha.flac"),
    lrow("peer", "Music/Album/02 - Beta.flac", 210.0),
    lrow("peer", "Music/Album/rip.log"),
    lrow("peer", "Music/Album/Album.cue"),
]
GATE_FILES = sorted(f"Music/Album/{n}" for n in
                    ("01 - Alpha.flac", "02 - Beta.flac", "rip.log", "Album.cue"))

# (a) the log arrives and grades BELOW the bar (JOB_CFG's minimum is 100).
run = run_job(JOB_RELEASE, GATE_ROWS, scores=score_logs(low=("rip.log",)),
              stub_cls=GateSlsk, keep_dir=True)
try:
    assert run.job["state"] == "error" and not run.imported, run.job["state"]
    assert len(run.job["attempts"]) == 1, run.job["attempts"]
    assert "log rejected" in run.job["attempts"][0]["reason"], run.job["attempts"][0]
    # slskd was asked for the log ALONE: the album bytes sit in the candidate's
    # tree (run_job plants every row), but they were never enqueued.
    assert run.stub.enqueued == [["Music/Album/rip.log"]], run.stub.enqueued
    # the whole tree WAS on disk when the candidate was rejected...
    assert run.stub.trees and run.stub.trees[0] == GATE_FILES, run.stub.trees
    # ...and none of it survives, so the next candidate re-downloads the album
    # instead of inheriting a rejected peer's bytes.
    assert _tree_files(run.ddir) == [], _tree_files(run.ddir)
    # the only transfer slskd has is the log the gate graded, so it is the one
    # cancelled — a rejection leaves no transfer behind to re-fetch what was
    # just deleted.
    assert sorted(run.cancelled) == ["Music/Album/rip.log"], run.cancelled
finally:
    shutil.rmtree(run.ddir, ignore_errors=True)

# (b) the log never arrives: the gate gives up at its own 180 s bound and the
#     SAME cleanup runs — including the log transfer the wait itself dropped.
run = run_job(JOB_RELEASE, GATE_ROWS, stub_cls=lambda d, r: GateSlsk(d, r, "Queued"),
              keep_dir=True)
try:
    assert run.job["state"] == "error" and not run.imported, run.job["state"]
    _reason = run.job["attempts"][0]["reason"]
    assert "log(s) arrived" in _reason and "180s" in _reason, _reason
    # a log that never arrived cost this peer its transfer only — the album was
    # never requested, so there is nothing of it to log about.
    assert run.stub.enqueued == [["Music/Album/rip.log"]], run.stub.enqueued
    assert run.stub.trees and run.stub.trees[0] == GATE_FILES, run.stub.trees
    assert _tree_files(run.ddir) == [], _tree_files(run.ddir)
    # the timed-out log is cancelled by the wait and again by the rejection:
    # the set makes the duplicate harmless, and nothing else was ever queued.
    assert sorted(set(run.cancelled)) == ["Music/Album/rip.log"], run.cancelled
finally:
    shutil.rmtree(run.ddir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 4. The live download block tells the truth: `speed` is the INSTANTANEOUS rate
#    the wait measured (never the sum of lifetimes slskd reports), `eta_s` is
#    the remaining bytes over that rate (never slskd's per-file remainingTime
#    guess), and the two file counts are different units.
# --------------------------------------------------------------------------- #
PROGRESS3_WANTED = [{"filename": "Music/P/01 - X.flac", "size": 1000},
                    {"filename": "Music/P/02 - Y.flac", "size": 500},
                    {"filename": "Music/P/03 - Z.flac", "size": 300},
                    {"filename": "Music/P/04 - W.flac", "size": 200}]


class HonestSlsk:
    """One file slskd calls complete that never lands, one still moving 100
    bytes per tick (with a 12 kB/s LIFETIME average slskd keeps reporting and a
    77 s remainingTime guess), and one this job accepts from disk with no
    slskd record at all."""

    finished_transfer = staticmethod(soulseek.finished_transfer)

    def __init__(self):
        self.polls, self.cancelled = 0, []

    def downloads_state(self):
        self.polls += 1
        return [{"username": "peer", "directories": [{"files": [
            {"id": "t1", "filename": PROGRESS3_WANTED[0]["filename"],
             "state": "Completed, Succeeded", "bytesTransferred": 1000,
             "size": 1000, "percentComplete": 100, "averageSpeed": 999999,
             "remainingTime": 0},
            {"id": "t2", "filename": PROGRESS3_WANTED[1]["filename"],
             "state": "InProgress", "bytesTransferred": 100 * (self.polls - 1),
             "size": 500, "percentComplete": 20, "averageSpeed": 12345,
             "remainingTime": 77},
        ]}]}]

    def cancel_downloads(self, username, transfer_ids):
        self.cancelled.extend(transfer_ids)
        return True


_p3_dir = tempfile.mkdtemp(prefix="mlo-honest-")
_p3_pub = []
_p3_clock = FakeClock()
_p3_clock.now = real_time.time()
_p3_saved = soulseek_auto.time
soulseek_auto.time = _p3_clock
try:
    # Z and W have no slskd record and sit on disk settled, so the wait ACCEPTS
    # them while slskd never vouched for them; X is the mirror image (slskd
    # says complete, nothing landed). That is what arrived/done vs slskd's
    # verdict mean, and the job runs its 4 ticks to the timeout with X and Y
    # unresolved either way.
    _z = put_file(_p3_dir, "P", "03 - Z.flac", body=b"x" * 300)
    _w = put_file(_p3_dir, "P", "04 - W.flac", body=b"x" * 200)
    os.utime(_z, (_p3_clock.now - 10, _p3_clock.now - 10))
    os.utime(_w, (_p3_clock.now - 10, _p3_clock.now - 10))
    with Patch(soulseek_auto, _job_progress=lambda payload: _p3_pub.append(payload)):
        got = soulseek_auto._wait_for_files(HonestSlsk(), _p3_dir, "peer",
                                            PROGRESS3_WANTED, 4.0)
    assert list(got) == [PROGRESS3_WANTED[2]["filename"],
                         PROGRESS3_WANTED[3]["filename"]], got
    snaps = [s for s in _p3_pub if s]
    assert len(snaps) == 4, snaps
    # Tick 1 has no measurement yet, so it falls back to slskd's own averages;
    # from tick 2 on the rate is the DELTA between ticks (100 B per 1 s poll)
    # — never the lifetime averages (999999 + 12345 sum them), and a finished
    # file's frozen average stops counting the moment it is not active.
    assert snaps[0]["speed"] == 12345.0, snaps[0]["speed"]
    assert [s["speed"] for s in snaps[1:]] == [100.0, 100.0, 100.0], \
        [s["speed"] for s in snaps]
    snap = snaps[1]
    # 1000 (X, slskd's own byte counter) + 100 (Y) + 300 + 200 (Z, W arrived)
    assert (snap["bytes"], snap["size"]) == (1600, 2000), snap
    assert snap["eta_s"] == 4, snap["eta_s"]          # 400 B left at 100 B/s
    assert snap["eta_s"] != 77, "slskd's per-file remainingTime won the ETA"
    # 1 slskd verdict, 2 real arrivals — different units, not the same number.
    assert (snap["files_done"], snap["files_arrived"], snap["files_total"]) == (1, 2, 4), snap
    _p3_by = {f["name"]: f for f in snap["files"]}
    assert (_p3_by["01 - X.flac"]["done"], _p3_by["01 - X.flac"]["complete"]) == \
        (False, True), _p3_by["01 - X.flac"]          # slskd's verdict, no file
    assert (_p3_by["03 - Z.flac"]["done"], _p3_by["03 - Z.flac"]["complete"]) == \
        (True, False), _p3_by["03 - Z.flac"]          # arrived, nobody vouched
    assert (_p3_by["04 - W.flac"]["done"], _p3_by["04 - W.flac"]["complete"]) == \
        (True, False), _p3_by["04 - W.flac"]
    assert (_p3_by["02 - Y.flac"]["done"], _p3_by["02 - Y.flac"]["complete"]) == \
        (False, False), _p3_by["02 - Y.flac"]
    assert _p3_pub[-1] is None, "the progress block was not cleared on the way out"
finally:
    soulseek_auto.time = _p3_saved
    shutil.rmtree(_p3_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 5. The sequential fallback query, and the `no_logs` prompt a CD release whose
#    copies are WEB rips now gets instead of a dead end.
# --------------------------------------------------------------------------- #
class PerQuerySlsk(AutoSlsk):
    """AutoSlsk with one response script per search ISSUED, in order — the
    job's configured template(s) first, then the single broader fallback."""

    def __init__(self, ddir, scripts):
        super().__init__(ddir, (scripts or [[]])[0])
        self.scripts = list(scripts)

    def search_results(self, sid):
        n = int(sid.split("-")[1]) - 1
        return {"state": "Completed", "isComplete": True,
                "responses": self.scripts[n] if n < len(self.scripts) else []}


WEBRIP_ROWS = [
    lrow("webpeer", "Music/Webrip/01 - Alpha.flac"),
    lrow("webpeer", "Music/Webrip/02 - Beta.flac", 210.0),
]
# The decline cases add a complete MP3 RIP of the same CD: it IS a candidate
# (every track, plus its log and cue — merely lossy), so "declined" really has
# a download it could fall back into. Without it a decline and a fall-through
# would look identical, because the CD view of the WEB rip is not a candidate
# at all.
MP3_ROWS = [
    lrow("mp3peer", "Music/Mp3rip/01 - Alpha.mp3"),
    lrow("mp3peer", "Music/Mp3rip/02 - Beta.mp3", 210.0),
    lrow("mp3peer", "Music/Mp3rip/rip.log"),
    lrow("mp3peer", "Music/Mp3rip/Album.cue"),
]
DECLINE_ROWS = MP3_ROWS + WEBRIP_ROWS
BROAD_QUERY = "Job Artist Job Album 1996"


def per_query(scripts):
    return lambda ddir, rows: PerQuerySlsk(ddir, scripts)


# (a) NOTHING usable from the configured template on a DIGITAL release: exactly
#     ONE broader `artist album year` query runs AFTER it (the job's own search
#     journal shows one query, then the other — never two windows at once), and
#     the copy it finds imports with no prompt at all.
run = run_job(DIGITAL_RELEASE, WEBRIP_ROWS,
              stub_cls=per_query([[], WEBRIP_ROWS]))
assert run.imported, run.job["result"]
assert [q for q, _t, _l in run.stub.searches] == ["Job Album", BROAD_QUERY], run.stub.searches
assert [t for _q, t, _l in run.stub.searches] == [5000, 5000], run.stub.searches
assert [l for _q, _t, l in run.stub.searches] == [25, 25], run.stub.searches
assert run.prompt is None, run.prompt
# ...and both windows report how long they REALLY took (this double answers on
# its first poll), not the ceiling the announcement line carries.
assert any("search(es) in 0s" in e["msg"] for e in run.job["log"]), \
    [e["msg"] for e in run.job["log"]]
assert any("broader query: 2 result file(s) in 0s" in e["msg"] for e in run.job["log"]), \
    [e["msg"] for e in run.job["log"]]

# (a2) ...and a PHYSICAL release never runs it ALL: the very same scripting that
#      just rescued the digital release finds nothing here, because the broad
#      query is never issued. "artist album year" asks the network for every
#      other pressing of the same album, so a pressing is searched by what
#      identifies THE PRESSING and by nothing else — a perfect copy the broad
#      wording would have found is not the release this job was asked for.
run = run_job(JOB_RELEASE, ONE_DISC_ROWS + WEBRIP_ROWS,
              stub_cls=per_query([[], ONE_DISC_ROWS]))
assert [q for q, _t, _l in run.stub.searches] == ["Job Album"], run.stub.searches
assert run.job["state"] == "error", run.job
assert run.job["result"]["error"] == NO_RESULT_MSG, run.job["result"]
assert (run.enqueued, run.imported) == ([], []), (run.enqueued, run.imported)
assert not any("one broader" in e["msg"] for e in run.job["log"]), \
    [e["msg"] for e in run.job["log"]]

# (b) a candidate from the FIRST query means the fallback never runs: one
#     search, no broader window, and no prompt — the release had a good copy,
#     so the template it came from is the one that stands.
run = run_job(DIGITAL_RELEASE, WEBRIP_ROWS, stub_cls=per_query([WEBRIP_ROWS, []]))
assert run.imported, run.job["result"]
assert [q for q, _t, _l in run.stub.searches] == ["Job Album"], run.stub.searches
assert not any("one broader" in e["msg"] for e in run.job["log"]), \
    [e["msg"] for e in run.job["log"]]

# (c) the configured query finds no CD rip, but a complete lossless WEB rip of
#     the same tracklist exists: the job parks on the CD-vs-Digital decision
#     and shows the folders it would take.
run = run_job(JOB_RELEASE, WEBRIP_ROWS, stub_cls=per_query([WEBRIP_ROWS]),
              confirm_lossy=True, answer=True)
assert run.prompt and run.prompt["reason"] == "no_logs", run.prompt
assert run.prompt["media"] == "Digital Media", run.prompt
assert len(run.prompt["candidates"]) == 1, run.prompt["candidates"]
_prow = run.prompt["candidates"][0]
assert (_prow["username"], _prow["dir"], _prow["format"]) == \
    ("webpeer", "Music/Webrip/", "FLAC"), _prow
assert (_prow["matched"], _prow["expected"]) == (2, 2), _prow
# accepting switches the release it believes it is importing to Digital Media
# BEFORE verification, then downloads and imports the copy it just showed.
assert run.job["state"] == "done" and run.imported, run.job
assert [c[0] for c in run.calls] == ["stamp", "verify", "import"], run.calls
assert run.calls[0][2] == "Digital Media", run.calls     # stamped as a WEB rip
assert run.calls[1][2] is False, run.calls               # no CD log/CRC audit
assert run.calls[2][2] == ["Digital Media"], run.calls   # the release that imported
assert run.calls[2][3] is False, run.calls
assert sorted(run.submitted()) == sorted(r["file"] for r in WEBRIP_ROWS), run.submitted()
# ...and the release the CALLER handed in is untouched — the switch is the job's
# own in-memory view, never a mutation of MusicBrainz's release dict.
assert JOB_RELEASE["medium_formats"] == ["CD"], JOB_RELEASE["medium_formats"]
assert JOB_RELEASE["catalog_number"] == "CAT-1", JOB_RELEASE

# (d) declining lands on the OTHER existing path first: the wishes offer the
#     no-result branch already uses. The job asks twice (no_logs, then the
#     wishes) and asks NOTHING else — a decline that fell back into the
#     candidate list would ask the lossy question instead (the MP3 rip is
#     downloadable), which is exactly the difference this pins.
_nologs_wish_dir = tempfile.mkdtemp(prefix="mlo-nologs-wish-")
_saved_wish_init = wishes_store._initialized
try:
    with Patch(wishes_store, db_path=lambda: os.path.join(_nologs_wish_dir, "wishes.db")):
        # The store creates its schema on FIRST USE, and "first use" is a
        # process-wide flag: whatever initialized it earlier in this process
        # (another block of this suite, a route) leaves it set, so the fresh DB
        # patched in here would be opened WITHOUT its tables and every wish
        # write would fail ("no such table: wishes"). The job then ended in
        # `error` instead of parking on its offer — the reason this suite flaked
        # on CI while passing on a developer machine.
        wishes_store._initialized = False
        run = run_job(JOB_RELEASE, DECLINE_ROWS, stub_cls=per_query([DECLINE_ROWS]),
                      confirm_lossy=True, answer=[False, True])
        assert [p["reason"] for p in run.prompts] == ["no_logs", "no_results"], run.prompts
        assert run.job["state"] == "done", run.job
        assert run.job["result"]["wished"] is True, run.job["result"]
        # nothing was downloaded or verified on the way to the wishes list
        assert (run.enqueued, run.imported, run.calls) == ([], [], []), \
            (run.enqueued, run.imported, run.calls)
        _wish = wishes_store.get_wish(run.job["result"]["wish_id"])
        assert _wish and _wish["status"] == "wanted", _wish
        assert _wish["release_mbid"] == JOB_RELEASE["id"], _wish
finally:
    wishes_store._initialized = _saved_wish_init
    shutil.rmtree(_nologs_wish_dir, ignore_errors=True)

# (e) ...and with the wish prompt off, the same decline falls through to the
#     error the CD path always raised: nothing is downloaded (not even the
#     complete MP3 rip that IS downloadable), and no second question is asked.
run = run_job(JOB_RELEASE, DECLINE_ROWS, stub_cls=per_query([DECLINE_ROWS]),
              confirm_lossy=True, answer=False,
              cfg=dict(JOB_CFG, soulseek_auto_wish_prompt=False))
assert [p["reason"] for p in run.prompts] == ["no_logs"], run.prompts
assert run.job["state"] == "error", run.job["state"]
assert run.job["result"]["error"] == NO_RESULT_MSG, run.job["result"]
assert (run.enqueued, run.imported, run.calls) == ([], [], []), \
    (run.enqueued, run.imported, run.calls)

# (f) accepting does NOT shrink the offer to a single peer: when the first WEB
#     folder fails (its transfers error out), the NEXT offered candidate is
#     tried. The live bug this pins ended a real run with "Every candidate was
#     rejected (1 attempt(s))" while three usable peers sat untouched, because
#     the accept branch replaced the candidate list with `[loose[0]]`.
SECOND_WEBRIP_ROWS = [
    lrow("webpeer2", "Music/Webrip2/01 - Alpha.flac"),
    lrow("webpeer2", "Music/Webrip2/02 - Beta.flac", 210.0),
]
BOTH_WEBRIPS = WEBRIP_ROWS + SECOND_WEBRIP_ROWS


class FirstPeerFails(AutoSlsk):
    """slskd double whose FIRST enqueued peer errors out, so the second peer's
    transfers are the only ones that can succeed."""

    def __init__(self, ddir, rows):
        super().__init__(ddir, rows)
        self.first = None

    def enqueue_download(self, username, wanted):
        if self.first is None:
            self.first = username
        return super().enqueue_download(username, wanted)

    def downloads_state(self):
        tree = super().downloads_state()
        for entry in tree:
            if entry.get("username") != self.first:
                continue
            for d in entry.get("directories") or []:
                for f in d.get("files") or []:
                    f["state"] = "Errored"
        return tree


run = run_job(JOB_RELEASE, BOTH_WEBRIPS, stub_cls=FirstPeerFails,
              confirm_lossy=True, answer=True)
assert run.prompt and run.prompt["reason"] == "no_logs", run.prompt
assert [c["username"] for c in run.prompt["candidates"]] == ["webpeer", "webpeer2"], \
    run.prompt["candidates"]
# the failing peer is rejected, the second one is enqueued and imports
assert [len(e) for e in run.enqueued] == [2, 2], run.enqueued
assert all("Webrip/" in f for f in run.enqueued[0]), run.enqueued[0]
assert all("Webrip2/" in f for f in run.enqueued[1]), run.enqueued[1]
assert [(a["username"], a["reason"][:18]) for a in run.job["attempts"]] == \
    [("webpeer", "download incomplet")], run.job["attempts"]
assert run.job["state"] == "done" and run.imported, run.job
assert os.path.basename(run.imported[-1]) == "Webrip2", run.imported
assert sorted(run.submitted()) == sorted(r["file"] for r in BOTH_WEBRIPS), \
    run.submitted()

# (g) a release the library ALREADY HOLDS cannot be started again — the guard
#     the bulk paths always had now covers the interactive job too. A live run
#     started a second download of an album that had just been imported, which
#     is exactly how "downloading the same album repeatedly" happens.
_saved_job = _snapshot_job()
try:
    with Patch(wishes_store,
               owned_mbids=lambda cfg=None: {JOB_RELEASE["id"].lower(): "C:/M/Owned"}):
        _res = soulseek_auto.start_job(release_mbid=JOB_RELEASE["id"])
    assert _res["ok"] is False, _res
    assert "already in your library" in _res["error"], _res
    assert soulseek_auto.job_state()["state"] == "idle", soulseek_auto.job_state()
    # …and an unowned release still starts (the guard must not become a wall)
    with Patch(wishes_store, owned_mbids=lambda cfg=None: {}):
        _res2 = soulseek_auto.start_job(release_mbid=JOB_RELEASE["id"])
    assert _res2["ok"] is True, _res2
    soulseek_auto.cancel()
finally:
    soulseek_auto._job.clear()
    soulseek_auto._job.update(_saved_job)


# --------------------------------------------------------------------------- #
# 6. A rejected candidate leaves NOTHING behind — in slskd's CURRENT layout,
#    and only for its OWN files. The rejection deletes the candidate's whole set
#    (album audio, cue and the graded log) from
#    `<ddir>/<user>/<batch id>/<remote path>` plus its staged partials under the
#    sibling `incomplete/`, while another user's copy of the same file and the
#    SAME user's copy of it in another album stay. The two late writes are
#    covered too: the flush slskd makes when it answers the cancel (so the
#    delete must run AFTER the cancel, which is what the settle is for) and one
#    that lands after the first sweep (why the sweep repeats until a pass
#    removes nothing).
# --------------------------------------------------------------------------- #
BATCH = "20101"                    # slskd's own batch id: user / batch / remote
OTHER_BATCH = "20202"
LATE_CANCEL = "02 - Beta.flac"     # re-created by the cancel itself
LATE_FLUSH = "01 - Alpha.flac"     # flushed in after the first sweep


class BatchGateSlsk(GateSlsk):
    """GateSlsk living in slskd's current layout, with both late writes.

    run_job plants the rows flat (`<ddir>/<remote path>`), which is no longer
    where slskd puts a download, so they are MOVED into
    `<ddir>/<user>/<batch id>/<remote path>` here — only then is the file the
    rejection deletes the one slskd really wrote. The neighbours the sweep must
    never reach are planted beside them: another user's copy of the same file,
    and this user's copy of it in ANOTHER album (both in the download tree and
    staged under `incomplete/`). `cancel_downloads` re-creates a file as well,
    because slskd answers the DELETE while a request is still in flight and
    flushes the bytes it already received."""

    def __init__(self, ddir, rows, log_state="Queued"):
        super().__init__(ddir, rows, log_state)
        for r in rows:
            rel = r["file"].split("/")
            dst = os.path.join(ddir, r["username"], BATCH, *rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(os.path.join(ddir, *rel), dst)
        put_file(ddir, "otheruser", OTHER_BATCH, "Music", "Album", "01 - Alpha.flac")
        put_file(ddir, "peer", OTHER_BATCH, "Music", "Album2", "01 - Alpha.flac")
        self.incomplete = soulseek._incomplete_dir_for(ddir)
        for parts in (("peer", "Music", "Album", "01 - Alpha.flac"),
                      ("peer", "Music", "Album", "Album.cue"),
                      ("peer", "Music", "Album2", "01 - Alpha.flac"),
                      ("otheruser", "Music", "Album", "01 - Alpha.flac")):
            put_file(self.incomplete, *parts, body=b"part")

    def cancel_downloads(self, username, transfer_ids, **kw):
        # `failed` is passed by the REJECTION's own cancel (_cancel_candidate)
        # and by nothing else — the wait cancels its timed-out log without it.
        if "failed" in kw:
            put_file(self.ddir, username, BATCH, "Music", "Album", LATE_CANCEL,
                     body=b"late")
        return super().cancel_downloads(username, transfer_ids, **kw)


_real_clear = soulseek.clear_transfer_files
_flushed = []


def _flush_late(ddir, username, filename, size=0):
    """The real clear_transfer_files, plus the bytes slskd flushes into the
    download dir AFTER the first sweep has already run — the write the repeated
    sweep exists for. (The sweep calls this once per wanted file, after its own
    delete, so the first call lands between the first pass and the next one.)"""
    if not _flushed:
        _flushed.append(put_file(ddir, username, BATCH, "Music", "Album",
                                 LATE_FLUSH, body=b"late"))
    return _real_clear(ddir, username, filename, size)


with Patch(soulseek, clear_transfer_files=_flush_late):
    run = run_job(JOB_RELEASE, GATE_ROWS,
                  stub_cls=lambda d, r: BatchGateSlsk(d, r, "Queued"), keep_dir=True)
try:
    assert run.job["state"] == "error" and not run.imported, run.job["state"]
    _reason = run.job["attempts"][0]["reason"]
    assert "log(s) arrived" in _reason and "180s" in _reason, _reason
    # the cancel re-created a file, and the snapshot IT took has it: the delete
    # runs after the cancel, which is the order the settle window exists for.
    assert f"peer/{BATCH}/Music/Album/{LATE_CANCEL}" in run.stub.trees[-1], \
        run.stub.trees[-1]
    assert _flushed, "the post-sweep flush never ran"
    # ...and the whole rejected set is gone, the two late writes included.
    assert _tree_files(os.path.join(run.ddir, "peer", BATCH)) == [], \
        _tree_files(os.path.join(run.ddir, "peer", BATCH))
    assert not os.path.exists(_flushed[0]), _flushed
    # SCOPE: another user's identically-named file (the index is scoped to the
    # candidate's own user subtree) and this user's copy of it in another album
    # (the delete is scoped to the candidate's own remote folder) are untouched.
    assert sorted(_tree_files(run.ddir)) == [
        f"otheruser/{OTHER_BATCH}/Music/Album/01 - Alpha.flac",
        f"peer/{OTHER_BATCH}/Music/Album2/01 - Alpha.flac"], _tree_files(run.ddir)
    # ...and nothing of the candidate stays staged under the sibling
    # incomplete/ dir either — nor is a neighbour's staged partial taken.
    _inc = run.stub.incomplete
    assert _tree_files(os.path.join(_inc, "peer", "Music", "Album")) == [], \
        _tree_files(os.path.join(_inc, "peer", "Music", "Album"))
    assert os.path.isfile(os.path.join(_inc, "peer", "Music", "Album2", "01 - Alpha.flac"))
    assert os.path.isfile(os.path.join(_inc, "otheruser", "Music", "Album", "01 - Alpha.flac"))
finally:
    for _u in ("peer", "otheruser"):
        shutil.rmtree(os.path.join(run.stub.incomplete, _u), ignore_errors=True)
    shutil.rmtree(run.ddir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 7. Exhausted candidates offer the wishes retry — the SAME parking spot the
#    no-usable-folder dead end uses — so a release whose every peer refused ends
#    on a wish instead of a bare error, with the queries this job searched with.
#    Declining, or switching the offer off, still reproduces that error verbatim.
# --------------------------------------------------------------------------- #
class QueueRefused(AutoSlsk):
    """slskd refuses every enqueue, so the candidate is rejected before a byte
    is queued — which is what makes "nothing enqueued" assertable."""

    def enqueue_download(self, username, wanted):
        raise RuntimeError("slskd refused the queue")


REJECTED_MSG = "Every candidate was rejected (1 attempt(s) — see the log)."
_orph_wish_dir = tempfile.mkdtemp(prefix="mlo-rejected-wish-")
_saved_wish_init = wishes_store._initialized
try:
    with Patch(wishes_store, db_path=lambda: os.path.join(_orph_wish_dir, "wishes.db")):
        # The store creates its schema on FIRST USE, and "first use" is a
        # process-wide flag: whatever initialized it earlier in this process
        # (another block of this suite, a route) leaves it set, so the fresh DB
        # patched in here would be opened WITHOUT its tables and every wish
        # write would fail ("no such table: wishes"). The job then ended in
        # `error` instead of parking on its offer — the reason this suite flaked
        # on CI while passing on a developer machine.
        wishes_store._initialized = False
        # (a) accepted: one wish carrying the release id and the job's own
        #     queries, and a finished "done" job that downloaded nothing.
        run = run_job(JOB_RELEASE, GATE_ROWS, stub_cls=QueueRefused,
                      confirm_lossy=True, answer=True)
        assert run.prompt and run.prompt["reason"] == "no_results", run.prompt
        assert run.prompt["queries"] == ["Job Album"], run.prompt
        assert len(run.job["attempts"]) == 1, run.job["attempts"]
        _result = run.job["result"]
        assert run.job["state"] == "done", run.job
        assert _result["wished"] is True and isinstance(_result["wish_id"], int), _result
        assert _result["album_path"] is None and _result["imported"] == 0, _result
        assert (run.enqueued, run.imported, run.calls) == ([], [], []), \
            (run.enqueued, run.imported, run.calls)
        _wishes = wishes_store.list_wishes()
        assert [w["id"] for w in _wishes] == [_result["wish_id"]], _wishes
        assert _wishes[0]["status"] == "wanted", _wishes[0]
        assert _wishes[0]["release_mbid"] == JOB_RELEASE["id"], _wishes[0]
        assert _wishes[0]["queries"] == run.prompt["queries"], _wishes[0]
        # ...and the RECORDED attempt is the classified one: a job that never
        # got a byte spends an ATTEMPT (not an empty search), and carries the
        # backoff as `retry_at` so the next pass does not re-run it at once.
        assert (_wishes[0]["attempts"], _wishes[0]["not_found"]) == (1, 0), _wishes[0]
        assert _wishes[0]["last_error"] == REJECTED_MSG, _wishes[0]
        assert _wishes[0]["retry_at"] - run.clock.now >= \
            wishes_store.retry_delay(dict(JOB_CFG), 1) - 1, (_wishes[0], run.clock.now)

        # (b) declined: the error the rejected-candidate dead end always raised,
        #     verbatim, and nothing wished.
        _count = len(wishes_store.list_wishes())     # the wish (a) just added
        run = run_job(JOB_RELEASE, GATE_ROWS, stub_cls=QueueRefused,
                      confirm_lossy=True, answer=False)
        assert [p["reason"] for p in run.prompts] == ["no_results"], run.prompts
        assert run.job["state"] == "error", run.job["state"]
        assert run.job["result"]["error"] == REJECTED_MSG, run.job["result"]
        assert len(wishes_store.list_wishes()) == _count, "a declined offer still wished"

        # (c) the offer switched off: no prompt at all, and the same error. The
        #     empty answer list drives the job on the worker thread and asserts
        #     it never parks — with the knob ignored this fails in seconds
        #     instead of blocking the inline call forever.
        run = run_job(JOB_RELEASE, GATE_ROWS, stub_cls=QueueRefused,
                      confirm_lossy=True, answer=[],
                      cfg=dict(JOB_CFG, soulseek_auto_wish_prompt=False))
        assert run.prompts == [], run.prompts
        assert run.job["state"] == "error", run.job["state"]
        assert run.job["result"]["error"] == REJECTED_MSG, run.job["result"]
finally:
    wishes_store._initialized = _saved_wish_init
    shutil.rmtree(_orph_wish_dir, ignore_errors=True)

print("ok")
