#!/usr/bin/env python3
"""Verification for the MusicBrainz auto-import path (release/release-group/
artist pages → Soulseek).

A MusicBrainz 503 used to travel from mb_get through release_lookup into
POST /api/mb/auto-import, which sat on it for minutes while the page's
Auto-import button stayed disabled — the user saw "nothing happens". These
asserts pin the four halves of the fix:

  1. mb_get retries 429/5xx (and connection errors) with backoff and still
     raises the typed MusicBrainzError when the whole retry budget is spent —
     with the 1 req/s MusicBrainz etiquette untouched,
  2. the route answers in well under a second even while the resolver hangs,
     and the item is QUEUED anyway ("queued (resolving)") instead of dropped,
  3. an item whose resolution really fails lands in `skipped` with a reason
     while the other items of the same call still queue,
  4. a MusicBrainz failure inside the job marks that job failed with a
     readable reason and frees the single-job slot — the next queued release
     runs, the queue never wedges.

Offline: httpx.get, the MusicBrainz resolvers and slskd are stubbed; no socket
is opened. Everything app-path related is redirected into a temp folder.

Run:  python tools/test_mb_auto_import.py
"""
import json
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: seed the music folder BEFORE server.main is imported, and point
# mlo.config at a stub config file so nothing touches the developer's library.
# --------------------------------------------------------------------------- #
REDIRECT = tempfile.mkdtemp(prefix="mlo-mbauto-redirect-")
os.environ["MLO_MUSIC_FOLDER"] = REDIRECT

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB_CFG = os.path.join(REDIRECT, "config.json")
with open(_STUB_CFG, "w", encoding="utf-8") as f:
    json.dump({"music_folder": REDIRECT}, f)

cfgmod.CONFIG_FILE = _STUB_CFG
pathmod.CONFIG_FILE = _STUB_CFG

import httpx  # noqa: E402

import server.integrations as intg  # noqa: E402
import server.soulseek as slsk  # noqa: E402
import server.soulseek_auto as auto  # noqa: E402
import server.wishes as wishes  # noqa: E402
from server import main as mlo_main  # noqa: E402  (heavy import)
from fastapi.testclient import TestClient  # noqa: E402

MBID_RELEASE = "0b1a2c3d-4e5f-6789-abcd-ef0123456789"
MBID_GROUP = "1c2b3a4d-5e6f-789a-bcde-f01234567890"
MBID_ARTIST = "2d3c4b5a-6f7e-8901-bcde-f12345678901"

_client = TestClient(mlo_main.app)


class _Resp:
    """The two httpx.Response attributes mb_get touches."""

    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"ok": True}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _patch_mb_transport(script):
    """Serve mb_get from `script`, a list of _Resp, an Exception to raise, or a
    callable(attempt) → either. Records every call."""
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        i = len(calls)
        calls.append({"url": url, "timeout": timeout})
        step = script(i) if callable(script) else script[min(i, len(script) - 1)]
        if isinstance(step, Exception):
            raise step
        return step

    intg.httpx.get = fake_get
    return calls


# --------------------------------------------------------------------------- #
# 1. the MusicBrainz client: retry, backoff, etiquette, typed failure
# --------------------------------------------------------------------------- #
# the 1 req/s etiquette and the retry policy are still the shipped values
assert intg._MB_MIN_INTERVAL == 1.0, intg._MB_MIN_INTERVAL
assert 429 in intg._MB_RETRY_STATUS and 503 in intg._MB_RETRY_STATUS, intg._MB_RETRY_STATUS
assert 0 < intg.MB_RETRY_DEADLINE <= 120, intg.MB_RETRY_DEADLINE

# the test may not spend seconds obeying them
intg._last_request = 0.0
intg._MB_MIN_INTERVAL = 0.02
intg._MB_BACKOFF_BASE = 0.05

calls = _patch_mb_transport([_Resp(503), _Resp(200, {"id": MBID_RELEASE})])
t0 = time.time()
data = intg.mb_get("release/x", {"fmt": "json"})
elapsed = time.time() - t0
assert data == {"id": MBID_RELEASE}, data            # the retried answer wins
assert len(calls) == 2, calls                        # one retry, not none
assert elapsed >= intg._MB_BACKOFF_BASE, elapsed      # backoff really waited

# timeout shrinks to what is left of the deadline: never a 30 s socket sit
assert calls[-1]["timeout"] <= intg.MB_RETRY_DEADLINE, calls[-1]

# a connection error is retried the same way
calls = _patch_mb_transport([httpx.ConnectError("no route"), _Resp(200, {"id": "ok"})])
assert intg.mb_get("release/x") == {"id": "ok"}

# a permanent 503 raises the TYPED error, carrying the status for the caller
calls = _patch_mb_transport([_Resp(503)])
try:
    intg.mb_get("release/x", retries=3)
except intg.MusicBrainzError as e:
    assert "503" in str(e) and "busy" in str(e), str(e)
    assert e.status == 503, e.status
else:
    raise AssertionError("a permanent 503 did not raise MusicBrainzError")
assert len(calls) == 3, calls                         # stopped at the retry budget

# Retry-After is respected (never shorter than what MusicBrainz asked for)
seen = {}
_real_sleep = intg.time.sleep


def _fake_sleep(s):
    seen["wait"] = max(seen.get("wait", 0.0), s)
    _real_sleep(min(s, 0.01))


intg.time.sleep = _fake_sleep
_patch_mb_transport([_Resp(429, headers={"Retry-After": "2"}), _Resp(200, {})])
intg.mb_get("release/x")
intg.time.sleep = _real_sleep
assert seen.get("wait", 0) >= 2.0, seen


# --------------------------------------------------------------------------- #
# 2. the route answers while the resolver hangs; the item is queued anyway
# --------------------------------------------------------------------------- #
assert 0 < mlo_main._MB_QUICK_RESOLVE_S <= 5.0, mlo_main._MB_QUICK_RESOLVE_S

queued_calls = []
_real_enqueue = auto.enqueue
_real_targets = intg.auto_import_targets


def _recording_enqueue(**kw):
    queued_calls.append(kw)
    return 0


def _hanging_targets(*a, **k):
    time.sleep(30)          # a MusicBrainz lookup that never answers in time
    return [], []           # ...if this is ever reached, the route waited


auto.enqueue = _recording_enqueue
intg.auto_import_targets = _hanging_targets
mlo_main._MB_QUICK_RESOLVE_S = 0.2
try:
    t0 = time.time()
    r = _client.post("/api/mb/auto-import",
                     json={"mbid": MBID_RELEASE, "kind": "release", "mode": "best"})
    elapsed = time.time() - t0
finally:
    mlo_main._MB_QUICK_RESOLVE_S = 3.0
    auto.enqueue = _real_enqueue
    intg.auto_import_targets = _real_targets

assert r.status_code == 200, (r.status_code, r.text)
body = r.json()
assert elapsed < 1.0, f"the route waited {elapsed:.2f}s on a hung resolver"
assert body["queued"] == 1, body
assert body["items"][0]["status"] == "queued (resolving)", body["items"]
assert body["items"][0]["mbid"] == MBID_RELEASE, body["items"]
assert body["skipped"] == [], body["skipped"]
# queued, not dropped — and carrying what the job needs to resolve it
assert len(queued_calls) == 1, queued_calls
assert queued_calls[0]["release_mbid"] == MBID_RELEASE, queued_calls[0]
assert queued_calls[0]["kind"] == "release", queued_calls[0]
assert queued_calls[0]["mode"] == "best", queued_calls[0]


# --------------------------------------------------------------------------- #
# 3. one unusable item is skipped with a reason; the rest still queue
# --------------------------------------------------------------------------- #
def _one_bad_group(gid, mode, *, types=None):
    if gid == "aaaaaaaa-0000-0000-0000-000000000001":
        return [], "MusicBrainz release-group lookup failed: HTTP 503"
    return [{"mbid": MBID_RELEASE, "title": "Some Album"}], None


queued_calls.clear()
_real_group_targets = intg.group_targets
auto.enqueue = _recording_enqueue
intg.auto_import_targets = _real_targets
intg.artist_browse = lambda *a, **k: {"release_groups": [
    {"id": "aaaaaaaa-0000-0000-0000-000000000001"},
    {"id": "bbbbbbbb-0000-0000-0000-000000000002"},
]}
intg.group_targets = _one_bad_group
wishes.owned_mbids = lambda cfg=None: set()
try:
    r = _client.post("/api/mb/auto-import",
                     json={"mbid": MBID_ARTIST, "kind": "artist", "mode": "best"})
finally:
    auto.enqueue = _real_enqueue
    intg.group_targets = _real_group_targets

assert r.status_code == 200, (r.status_code, r.text)
body = r.json()
assert body["queued"] == 1, body
assert body["items"][0]["mbid"] == MBID_RELEASE, body["items"]
assert len(body["skipped"]) == 1, body["skipped"]
assert body["skipped"][0]["mbid"] == "aaaaaaaa-0000-0000-0000-000000000001", body["skipped"]
assert "release-group lookup failed" in body["skipped"][0]["reason"], body["skipped"]


# --------------------------------------------------------------------------- #
# 4. a MusicBrainz failure inside the job fails THAT job and frees the slot
# --------------------------------------------------------------------------- #
# slskd is up and logged in; the release identity lookup is what fails.
auto.load_config = lambda: {"music_folder": REDIRECT, "soulseek_auto_log_min_score": 100}
slsk.is_running = lambda *a, **k: True
slsk.web_up = lambda *a, **k: True
slsk.server_state = lambda *a, **k: {"isLoggedIn": True}
slsk.download_dir = lambda *a, **k: REDIRECT

resolves = []


def _mb_down(*a, **k):
    resolves.append(1)
    raise intg.MusicBrainzError(
        "MusicBrainz is busy (HTTP 503) for release-group/x after 3 attempt(s) "
        "in 6s — try again in a moment", status=503)


intg.auto_import_targets = _mb_down
with auto._lock:
    auto._job.update({"state": "idle", "cancel": False, "log": [], "attempts": [],
                      "release": None, "result": None, "confirm": None,
                      "search": None, "progress": None})
with auto._queue_lock:
    del auto._queue[:]


def _enqueue(**item):
    """enqueue() must return promptly: it starts the job while holding the
    queue lock, and start_job reports through job_state() → queued() → that
    same lock. A plain Lock self-deadlocked the calling (request) thread here —
    run it off-thread so a regression FAILS instead of hanging the suite."""
    done = []
    t = threading.Thread(target=lambda: done.append(auto.enqueue(**item)), daemon=True)
    t.start()
    t.join(10)
    assert done, f"enqueue({item}) never returned — the queue lock deadlocked"
    return done[0]


_enqueue(release_mbid=MBID_GROUP, kind="release_group", mode="best")
_enqueue(release_mbid=MBID_ARTIST, kind="artist", mode="best")

deadline = time.time() + 10
while time.time() < deadline and not (len(resolves) >= 2
                                      and auto.job_state()["state"] == "error"):
    time.sleep(0.05)
state = auto.job_state()
assert len(resolves) >= 2, f"the queue wedged after the first failure: {state}"
assert state["state"] == "error", state
assert auto.job_active() is False, state          # the single-job slot is free
err = (state.get("result") or {}).get("error") or ""
assert "MusicBrainz" in err and "503" in err, state
# ...and it is readable in the job's own log/stage, which is what the Soulseek
# page shows
assert "MusicBrainz" in state["stage"], state["stage"]
assert auto.queued() == [], auto.queued()          # nothing left stuck behind it

print("ok  mb_get retries 503/connection errors with backoff, honours "
      "Retry-After + 1 req/s, raises MusicBrainzError when out of budget")
print("ok  POST /api/mb/auto-import answers without waiting on a hung resolver "
      "and queues the item (queued (resolving))")
print("ok  an unusable item is skipped with a reason while the rest queue")
print("ok  a MusicBrainz failure fails its job with a reason, frees the slot "
      "and lets the next queued release run")

# --------------------------------------------------------------------------- #
# 5. bulk auto-import never queues an album the library already holds
# --------------------------------------------------------------------------- #
# `wishes.owned_mbids` is the library's index of MusicBrainz release AND
# release-group ids (from the album tags). A bulk import that ignores it
# downloads the same album a second time, and the duplicate lands beside the
# first in the library.
_OWNED_GROUP = "3e4d5c6b-7a89-0123-cdef-23456789abcd"
_REL_DATA = {
    "id": MBID_RELEASE,
    "title": "Some Album",
    "status": "Official",
    "country": "US",
    "date": "2001-01-01",
    "release-group": {"id": _OWNED_GROUP, "primary-type": "Album",
                      "first-release-date": "2001-01-01"},
    "media": [{"position": 1, "format": "CD",
               "tracks": [{"position": 1, "title": "One",
                           "recording": {"id": MBID_RELEASE}}]}],
}

# section 4 leaves its MusicBrainz-failure stub installed on
# intg.auto_import_targets; the guard under test lives in the genuine function
# (saved in section 2), so call that one.
_auto_targets = _real_targets
_real_owned_mbids = wishes.owned_mbids
_real_rg_browse = intg.release_group_browse
_real_httpx_get = intg.httpx.get
_rg_calls = []


def _rgb_one_album(rg_mbid, limit=100, offset=0):
    """The group's single, importable edition (the MusicBrainz browse seam)."""
    _rg_calls.append(rg_mbid)
    return {"id": rg_mbid, "title": "Some Album", "releases": [
        {"id": MBID_RELEASE, "title": "Some Album", "status": "Official",
         "country": "US", "date": "2001-01-01", "format": "CD",
         "media": [{"position": 1, "format": "CD"}]}]}


def _owns(group_id):
    return lambda cfg=None: {group_id: os.path.join(REDIRECT, "Artists", "Some Album")}


try:
    intg.release_group_browse = _rgb_one_album

    # a RELEASE id whose release group the library already holds: resolved, then
    # refused — never queued
    wishes.owned_mbids = _owns(_OWNED_GROUP)
    _patch_mb_transport([_Resp(200, dict(_REL_DATA))])
    _rows, _skipped = _auto_targets(MBID_RELEASE, "release", "best")
    assert _rows == [], _rows
    assert _skipped == [{"mbid": MBID_RELEASE,
                         "reason": "already in the library"}], _skipped

    # a RELEASE GROUP id the library holds: refused BEFORE any MusicBrainz
    # browse — nothing is fetched for an album that is already on disk
    _rg_calls.clear()
    _patch_mb_transport([_Resp(200, {"id": _OWNED_GROUP})])
    wishes.owned_mbids = _owns(_OWNED_GROUP)
    _rows, _skipped = _auto_targets(_OWNED_GROUP, "release_group", "best")
    assert _rows == [], _rows
    assert _skipped == [{"mbid": _OWNED_GROUP,
                         "reason": "already in the library"}], _skipped
    assert _rg_calls == [], _rg_calls

    # nothing owned: BOTH paths queue exactly as before (the guard must never
    # eat a legitimate import). The row also carries `candidates` now — the
    # ranked fallback list (spec R150) — so the assertion names the fields it
    # has always been about (what to queue) and checks the list starts on the
    # row's own edition, instead of pinning the whole dict: a named RELEASE is
    # one candidate by definition, since the caller asked for that pressing.
    wishes.owned_mbids = lambda cfg=None: {}
    _patch_mb_transport([_Resp(200, dict(_REL_DATA))])
    _rows, _skipped = _auto_targets(MBID_RELEASE, "release", "best")
    assert [(r["mbid"], r["title"]) for r in _rows] == \
        [(MBID_RELEASE, "Some Album")], _rows
    assert _rows[0]["candidates"] == [{"mbid": MBID_RELEASE,
                                       "title": "Some Album"}], _rows[0]
    assert _skipped == [], _skipped

    _rg_calls.clear()
    _rows, _skipped = _auto_targets(_OWNED_GROUP, "release_group", "best")
    # The policy's rows carry the chosen edition's score and its reasons next
    # to the identity (the watch reports them), so the identity is what is
    # compared here.
    assert [(r["mbid"], r["title"]) for r in _rows] == \
        [(MBID_RELEASE, "Some Album")], _rows
    assert _skipped == [], _skipped
    assert _rg_calls == [_OWNED_GROUP], _rg_calls
finally:
    wishes.owned_mbids = _real_owned_mbids
    intg.release_group_browse = _real_rg_browse
    intg.httpx.get = _real_httpx_get

print("ok  bulk auto-import skips a release/release-group whose MBID the "
      "library already holds (both kinds, nothing queued) and still queues "
      "everything it does not own")

# --------------------------------------------------------------------------- #
# 6. release_lookup keeps EVERY catalog number of the release, in MusicBrainz
#    order, blanks and duplicates dropped — one per label/pressing — while
#    `catalog_number` stays the first one, unchanged for every existing caller.
#    A release with no label-info carries an EMPTY list, never None: callers
#    iterate it.
# --------------------------------------------------------------------------- #
_CATNO_PAYLOAD = {
    "id": MBID_RELEASE, "title": "Two Label Album", "date": "2001-03-04",
    "country": "GB", "barcode": "5012345678900", "status": "Official",
    "artist-credit": [{"name": "Some Artist", "artist": {"id": MBID_ARTIST}}],
    "release-group": {"id": MBID_GROUP, "primary-type": "Album",
                      "first-release-date": "2001"},
    "media": [{"position": 1, "format": "CD", "tracks": []}],
    "label-info": [
        {"catalog-number": "CAT-1", "label": {"name": "First Label"}},
        {"catalog-number": "   ", "label": {"name": "Blank Label"}},
        {"catalog-number": "CAT-2", "label": {"name": "Second Label"}},
        {"catalog-number": "CAT-1", "label": {"name": "Repress"}},
    ],
}

_saved_mb_get = intg.httpx.get


def _drop_mb_cache():
    """release_lookup reads through mb_get_cached (a release page must not
    re-hit MusicBrainz at 1 req/s), so three DIFFERENT payloads for the same
    release id are only distinguishable with the cache cleared between them —
    a test artifact of replaying one MBID, not a behaviour to pin."""
    with intg._BROWSE_LOCK:
        intg._BROWSE_CACHE.clear()
        intg._INFLIGHT.clear()


try:
    _drop_mb_cache()
    _patch_mb_transport([_Resp(200, dict(_CATNO_PAYLOAD))])
    _rel = intg.release_lookup(MBID_RELEASE)
    assert _rel["catalog_numbers"] == ["CAT-1", "CAT-2"], _rel["catalog_numbers"]
    assert _rel["catalog_number"] == "CAT-1", _rel["catalog_number"]   # unchanged
    assert _rel["label"] == "First Label", _rel["label"]

    # no label-info at all (and none of the labels named): both keys still there
    _drop_mb_cache()
    _patch_mb_transport([_Resp(200, {"id": MBID_RELEASE, "title": "No Labels",
                                     "media": []})])
    _rel = intg.release_lookup(MBID_RELEASE)
    assert _rel["catalog_numbers"] == [], _rel["catalog_numbers"]
    assert _rel["catalog_number"] == "", repr(_rel["catalog_number"])

    # every label-info entry blank: still an empty list, not [""]
    _drop_mb_cache()
    _patch_mb_transport([_Resp(200, {"id": MBID_RELEASE, "title": "Blank Labels",
                                     "media": [],
                                     "label-info": [{"catalog-number": " "},
                                                    {"label": {"name": "L"}}]})])
    _rel = intg.release_lookup(MBID_RELEASE)
    assert _rel["catalog_numbers"] == [], _rel["catalog_numbers"]
    assert _rel["catalog_number"] == "", repr(_rel["catalog_number"])
finally:
    intg.httpx.get = _saved_mb_get

print("ok  release_lookup keeps every catalog number (order, blanks and "
      "duplicates dropped; catalog_number unchanged) and an empty list when "
      "the release carries none")
