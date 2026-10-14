#!/usr/bin/env python3
"""MusicBrainz OAuth2 login + rating submissions (`server.mb_submit`,
`server.api_mb_auth`) — the wire shape, OFFLINE, plus a standing scratch server.

What this pins, and what it refuses to guess:

  * the EXACT MMD-2.0 body of a submission: one recording, one release group,
    and the two TOGETHER in one request (batching is the point), byte for byte,
    including the namespace and the list order (recording before release group);
  * the SNAP onto MusicBrainz's 20-point grid — HALF-UP, the same rule
    ``server.ratings.from_tag`` states in the other direction, because Python's
    ``round`` is banker's: 83 -> 80 and 91 -> 100 (the documented examples),
    50 -> 60, 10 -> 20, and 9 -> 0;
  * that 0 (which DELETES the rating on MusicBrainz) is NEVER SENT — an
    unrated entity, and a value that would round to 0, are both skipped with
    their own sentence and produce NO request at all;
  * the OAuth2 form bodies: the authorization-code exchange and the
    refresh_token renewal, exactly as MusicBrainz's token endpoint expects;
  * that a refusal is RETURNED as MusicBrainz's own text, never raised —
    HTTP status+body, and the `<error>` element MusicBrainz puts in a 200;
  * that the routes answer sanely with NOTHING connected, on a real server
    standing on a spare port (8017+): status / connect / finish / disconnect
    each say what is missing, and submit REFUSES with a sentence.

No live submission is possible here: it needs the owner's registered app and
one authorization, so the network is stubbed at ``mb_submit.httpx`` for the
wire checks and never touched afterwards (see `left` in the run's report).

Run:  python tools/test_mb_submit.py   (exit 0 = pass, 1 = failure, 2 = skip)
"""
import atexit
import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: a scratch music folder + state dir under F:/tmp (the owner's
# rule for a scratch run), removed afterwards. MLO_MUSIC_FOLDER is what keeps
# the repo's own config.json out of this.
# --------------------------------------------------------------------------- #
_BASE = ("F:/tmp" if os.path.isdir("F:/") else os.path.dirname(ROOT))
SCRATCH = os.environ.get("MLO_MB_SCRATCH") or os.path.join(_BASE, "mlo-mb-submit")
MF = os.path.join(SCRATCH, "music")
STATE = os.path.join(SCRATCH, "state")
for _d in (MF, STATE):
    os.makedirs(_d, exist_ok=True)
os.environ["MLO_MUSIC_FOLDER"] = MF
os.environ["MLO_APP_DATA_DIR"] = STATE


def _cleanup_scratch():
    """Remove the scratch tree, past the app's own DB handles: the tag index's
    module-level SQLite connection is still open at exit and keeps the file
    locked on Windows, so it is closed here before the tree is removed."""
    import gc
    try:
        from server import tagindex
        with tagindex._LOCK:
            if tagindex._conn is not None:
                tagindex._conn.close()
                tagindex._conn = None
    except Exception:
        pass
    for _ in range(40):
        gc.collect()
        shutil.rmtree(SCRATCH, ignore_errors=True)
        if not os.path.exists(SCRATCH):
            return
        time.sleep(0.05)


atexit.register(_cleanup_scratch)
atexit.register(lambda: os.environ.pop("MLO_MUSIC_FOLDER", None))
atexit.register(lambda: os.environ.pop("MLO_APP_DATA_DIR", None))

import mlo.config as cfgmod                                          # noqa: E402
from server import mb_submit as mb                                   # noqa: E402

# Seed the scratch config where the app actually reads it (the folder the
# routes resolve through), so nothing here touches the developer's config.
with open(cfgmod.active_config_file(), "w", encoding="utf-8") as _f:
    json.dump({"music_folder": MF}, _f)

REAL_HTTPX = mb.httpx
FAILED = []
PASSED = 0

REC = "11111111-2222-3333-4444-555555555555"
RG = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CID = "client-id-123"
SECRET = "client-secret-456"


def ok(cond, label):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok: {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL: {label}")


def eq(got, want, label):
    ok(got == want, label if got == want else f"{label}\n        got  {got!r}\n        want {want!r}")


class Resp:
    def __init__(self, status=200, text="", body=None):
        self.status_code = status
        self.text = text
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class FakeHTTP:
    """`mb_submit.httpx` stand-in: records every call, answers from a queue."""

    def __init__(self):
        self.calls = []
        self.queue = []

    def _next(self, method, url, kw):
        self.calls.append((method, url, kw))
        if not self.queue:
            raise AssertionError(f"unexpected {method} {url}")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, **kw):
        return self._next("POST", url, kw)

    def get(self, url, **kw):
        return self._next("GET", url, kw)


def connected_cfg(**over):
    """A cfg that is connected and whose access token is still fresh, so no
    refresh is attempted unless the test wants one."""
    cfg = {mb.CLIENT_ID_KEY: CID, mb.CLIENT_SECRET_KEY: SECRET,
           mb.REFRESH_KEY: "refresh-abc", mb.ACCESS_KEY: "access-abc",
           mb.EXPIRES_KEY: int(time.time()) + 3600,
           mb.USERNAME_KEY: "listener"}
    cfg.update(over)
    return cfg


# --------------------------------------------------------------------------- #
print("\nthe snap: 0-100 onto MusicBrainz's 20-point grid, half-up")
# --------------------------------------------------------------------------- #
eq(mb.snap(83), 80, "83 snaps to 80")
eq(mb.snap(91), 100, "91 snaps to 100 (rounds up)")
eq(mb.snap(50), 60, "50 snaps to 60 — half-UP, not banker's rounding")
eq(mb.snap(10), 20, "10 (half a star) snaps to 20")
eq(mb.snap(100), 100, "100 stays 100")
eq(mb.snap(9), 0, "9 snaps to 0 — which is 'do not send'")
eq(mb.snap(0), 0, "0 snaps to 0")
eq(mb.snap(-5), 0, "a negative clamps to 0")
eq(mb.snap("junk"), 0, "junk snaps to 0 rather than raising")

# --------------------------------------------------------------------------- #
print("\nthe MMD-2.0 body — the exact wire shape")
# --------------------------------------------------------------------------- #
NS = 'xmlns="http://musicbrainz.org/ns/mmd-2.0#"'
eq(mb.build_body([("recording", REC, 80)]),
   f'<metadata {NS}><recording-list><recording id="{REC}">'
   f"<user-rating>80</user-rating></recording></recording-list></metadata>",
   "one recording body is byte-for-byte the documented shape")
eq(mb.build_body([("release_group", RG, 60)]),
   f'<metadata {NS}><release-group-list><release-group id="{RG}">'
   f"<user-rating>60</user-rating></release-group></release-group-list></metadata>",
   "one release group body uses release-group-list / release-group")
# Batching: both kinds in ONE body, recordings before release groups, and the
# input order inside a list preserved.
eq(mb.build_body([("release_group", RG, 40), ("recording", REC, 80),
                  ("recording", "99999999-2222-3333-4444-555555555555", 100)]),
   f'<metadata {NS}><recording-list>'
   f'<recording id="{REC}"><user-rating>80</user-rating></recording>'
   f'<recording id="99999999-2222-3333-4444-555555555555">'
   f"<user-rating>100</user-rating></recording></recording-list>"
   f'<release-group-list><release-group id="{RG}">'
   f"<user-rating>40</user-rating></release-group></release-group-list>"
   f"</metadata>",
   "both lists travel in ONE body, in a stable order")
eq(mb.build_body([]), f'<metadata {NS}></metadata>',
   "no entities is an empty metadata element")

# --------------------------------------------------------------------------- #
print("\n0 is NEVER sent")
# --------------------------------------------------------------------------- #
fake = FakeHTTP()
mb.httpx = fake
try:
    # unrated file: nothing to send, so not a single request is made
    res = mb.submit_ratings([("recording", REC, 0)], connected_cfg())
    eq(res["sent"], 0, "an unrated entity sends nothing")
    eq(fake.calls, [], "and no request is made for it")
    eq(res["rows"][0]["reason"], "no rating of yours to send",
       "the skip says the rating is absent, not zero")
    # a value that SNAPS to 0 must not be turned into a delete
    res = mb.submit_ratings([("recording", REC, 5)], connected_cfg())
    eq(fake.calls, [], "a value that rounds to 0 makes no request either")
    ok("deletes the rating" in res["rows"][0]["reason"],
       "and the skip explains that 0 would delete the rating")
    # a bad entity / missing id is a skip too, never a crash
    res = mb.submit_ratings([("album", REC, 80)], connected_cfg())
    eq(res["rows"][0]["sent"], None, "an unsupported entity is skipped")
    ok("no rating for" in res["rows"][0]["reason"], "naming what MusicBrainz takes")
    res = mb.submit_ratings([("recording", "", 80)], connected_cfg())
    eq(res["rows"][0]["sent"], None, "a missing MBID is skipped")
finally:
    mb.httpx = REAL_HTTPX

# --------------------------------------------------------------------------- #
print("\nthe submission request: headers, content type, one request")
# --------------------------------------------------------------------------- #
fake = FakeHTTP()
mb.httpx = fake
try:
    fake.queue.append(Resp(200, "<metadata/>"))
    res = mb.submit_ratings([("recording", REC, 80), ("release_group", RG, 60)],
                            connected_cfg())
    eq(res["ok"], True, "an accepted submission reports ok")
    eq(res["sent"], 2, "both entities count as sent")
    eq(len(fake.calls), 1, "the whole album goes in ONE request")
    method, url, kw = fake.calls[0]
    eq(url, mb.RATING_URL, "POSTed to the WS/2 rating endpoint")
    eq(kw["headers"]["Content-Type"], "application/xml; charset=utf-8",
       "with the XML content type MusicBrainz expects")
    eq(kw["headers"]["Authorization"], "Bearer access-abc",
       "and the Bearer token")
    ok(kw["headers"]["User-Agent"].startswith("la-musica/"),
       "plus the app's own User-Agent")
    eq(kw["content"], mb.build_body([("recording", REC, 80),
                                     ("release_group", RG, 60)]).encode("utf-8"),
       "carrying the pinned body")
finally:
    mb.httpx = REAL_HTTPX

# --------------------------------------------------------------------------- #
print("\na refusal is TEXT, not an exception")
# --------------------------------------------------------------------------- #
fake = FakeHTTP()
mb.httpx = fake
try:
    fake.queue.append(Resp(400, "invalid user-rating"))
    res = mb.submit_ratings([("recording", REC, 80)], connected_cfg())
    eq(res["ok"], False, "a refused submission reports not-ok")
    eq(res["detail"], "HTTP 400: invalid user-rating",
       "with MusicBrainz's own words")
    eq(res["rows"][0]["sent"], None, "and the row is marked not sent")
    ok(res["rows"][0]["reason"].startswith("not sent —"),
       "carrying the reason, not raising it")
    # a 200 that still states an error is a refusal too
    fake.queue.append(Resp(200, "<metadata><error>invalid entity id</error></metadata>"))
    res = mb.submit_ratings([("recording", REC, 80)], connected_cfg())
    eq(res["ok"], False, "an <error> inside a 200 is a refusal as well")
    ok("invalid entity id" in res["detail"], "reported in its own words")
    # a login that is gone: every row skipped, still no raise
    fake.queue.append(Resp(400, '{"error":"invalid_grant"}'))
    cfg = connected_cfg(**{mb.EXPIRES_KEY: int(time.time()) - 10})
    res = mb.submit_ratings([("recording", REC, 80)], cfg)
    eq(res["ok"], False, "a refused token renewal is not-ok")
    ok("invalid_grant" in res["detail"], "with the token endpoint's words")
    eq(res["rows"][0]["sent"], None, "and nothing is claimed sent")
finally:
    mb.httpx = REAL_HTTPX

# --------------------------------------------------------------------------- #
print("\nthe OAuth2 form bodies")
# --------------------------------------------------------------------------- #
fake = FakeHTTP()
mb.httpx = fake
try:
    cfg = {mb.CLIENT_ID_KEY: CID, mb.CLIENT_SECRET_KEY: SECRET}
    fake.queue.append(Resp(200, "", {"access_token": "new-access",
                                     "refresh_token": "new-refresh",
                                     "expires_in": 3600, "token_type": "Bearer"}))
    fake.queue.append(Resp(200, "", {"sub": "listener"}))
    got = mb.finish_connect("the-code", "the-verifier", cfg)
    eq(got["ok"], True, "a code + verifier connects")
    eq(got["username"], "listener", "and the account name comes from userinfo")
    method, url, kw = fake.calls[0]
    eq((method, url), ("POST", mb.TOKEN_URL), "the exchange POSTs the token endpoint")
    eq(kw["data"], {
        "grant_type": "authorization_code", "code": "the-code",
        "client_id": CID, "client_secret": SECRET,
        "redirect_uri": mb.REDIRECT_URI, "code_verifier": "the-verifier",
    }, "with exactly MusicBrainz's authorization_code form")
    eq(cfg[mb.REFRESH_KEY], "new-refresh", "the refresh token was stored")
    eq(cfg[mb.ACCESS_KEY], "new-access", "the access token was stored")
    ok(int(cfg[mb.EXPIRES_KEY]) > time.time(), "and its expiry is in the future")

    # the renewal, on an expired access token
    fake.calls.clear()
    cfg2 = connected_cfg(**{mb.EXPIRES_KEY: int(time.time()) - 10})
    fake.queue.append(Resp(200, "", {"access_token": "renewed", "expires_in": 3600}))
    fake.queue.append(Resp(200, "", {"sub": "listener"}))
    got = mb.whoami(cfg2)
    eq(got["ok"], True, "whoami renews an expired token and answers ok")
    eq(got["checked"], "GET /oauth2/userinfo", "naming the endpoint it proved it on")
    eq(fake.calls[0][1], mb.TOKEN_URL, "the renewal hits the token endpoint")
    eq(fake.calls[0][2]["data"], {
        "grant_type": "refresh_token", "refresh_token": "refresh-abc",
        "client_id": CID, "client_secret": SECRET,
    }, "with exactly MusicBrainz's refresh_token form")
    eq(cfg2[mb.ACCESS_KEY], "renewed", "and the renewed token is stored")

    # an unconfigured install asks nobody and says so
    fake.calls.clear()
    got = mb.whoami({})
    eq(got, {"ok": False, "checked": "",
             "detail": "no MusicBrainz login is saved — connect the app to the "
                       "account whose ratings it should submit (client "
                       "id/secret and one authorization)"},
       "an unconnected whoami is skipped, never a pass — and never raises")
    eq(fake.calls, [], "with no request at all")

    # connect_url: PKCE, S256, the OOB redirect
    fake.calls.clear()
    got = mb.connect_url({mb.CLIENT_ID_KEY: CID})
    ok(got["ok"], "connect_url builds a URL once a client id is saved")
    ok(got["url"].startswith(mb.AUTHORIZE_URL + "?"), "on the authorize endpoint")
    for token in ("response_type=code", "client_id=" + CID,
                  "redirect_uri=urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob",
                  "scope=rating", "code_challenge_method=S256",
                  "code_challenge=", "state="):
        ok(token in got["url"], f"the URL carries {token}")
    ok(len(got["verifier"]) >= 43, "with a PKCE verifier of legal length")
    ok(not mb.connect_url({})["ok"], "and without a client id it refuses")
    eq(mb.connect_url({})["url"], "", "handing back no URL at all")

    # disconnect keeps the app registration but forgets the login
    cfg3 = connected_cfg()
    st = mb.disconnect(cfg3)
    eq(st["connected"], False, "disconnect leaves nothing connected")
    eq(cfg3[mb.CLIENT_ID_KEY], CID, "the app's client id survives disconnect")
    eq(cfg3[mb.REFRESH_KEY], "", "the refresh token does not")
finally:
    mb.httpx = REAL_HTTPX

# --------------------------------------------------------------------------- #
print("\nthe submit route batches one album into ONE request")
# --------------------------------------------------------------------------- #
from server import api_mb_auth, ratings as ratings_mod        # noqa: E402
from server import mbresolve, tagcache as tagcache_mod        # noqa: E402

ALBUM = os.path.join(MF, "Some Album")
F1 = os.path.join(ALBUM, "01 track.flac")
F2 = os.path.join(ALBUM, "02 track.flac")
REC2 = "99999999-2222-3333-4444-555555555555"
os.makedirs(ALBUM, exist_ok=True)
for _f in (F1, F2):
    with open(_f, "wb") as _h:
        _h.write(b"\0")
# A file's own Picard RATING tag (0-100) is what a library with no stored row
# adopts, so these two stand in for real files: 83 -> 8 half-stars -> 80,
# 91 -> 9 half-stars -> 90 -> snapped 100.
TAGS = {
    os.path.normpath(F1): {"MUSICBRAINZ_TRACKID": REC, "RATING": "83",
                           "MUSICBRAINZ_RELEASEGROUPID": RG},
    os.path.normpath(F2): {"MUSICBRAINZ_TRACKID": REC2, "RATING": "91",
                           "MUSICBRAINZ_RELEASEGROUPID": RG},
}
_real_read = tagcache_mod.read_track
_real_heal = mbresolve.heal_row
_real_load = api_mb_auth.load_config
tagcache_mod.read_track = lambda path, tag_list=None: (
    {k: TAGS.get(os.path.normpath(path), {}).get(k) for k in (tag_list or [])}, {})
mbresolve.heal_row = lambda kind, key, mbid: key          # no library walk
cfg_route = connected_cfg()
api_mb_auth.load_config = lambda: cfg_route
fake = FakeHTTP()
mb.httpx = fake
try:
    # the album's own verdict, so its release group rides in the same request
    ratings_mod.set_rating(ALBUM, 8, user="", scope="album")
    fake.queue.append(Resp(200, "<metadata/>"))
    out = api_mb_auth.mb_ratings_submit(api_mb_auth.SubmitBody(paths=[F1, F2]))
    eq(len(fake.calls), 1, "two files of one album are ONE request")
    eq(fake.calls[0][1], mb.RATING_URL, "POSTed to the rating endpoint")
    eq(fake.calls[0][2]["content"].decode("utf-8"),
       mb.build_body([("recording", REC, 80), ("recording", REC2, 100),
                      ("release_group", RG, 80)]),
       "carrying both recordings AND the album's release group, snapped")
    eq([r["status"] for r in out["rows"]], ["sent", "sent", "sent"],
       "every row says what was sent")
    eq((out["rows"][0]["sent"], out["rows"][0]["value"]), (80, 80),
       "the first file's row reports the snapped value and its source")
    eq(out["rows"][1]["sent"], 100, "83->80 and 91->100 through the route")
    eq(out["rows"][2]["kind"], "album", "the release group's own row is present")
    eq(out["sent"], 3, "three entities went in the one request")
    # a file of ANOTHER folder with nothing known about it: still one request
    # total, and its row says why nothing was sent
    F3 = os.path.join(MF, "No Ids", "03 track.flac")
    os.makedirs(os.path.dirname(F3), exist_ok=True)
    with open(F3, "wb") as _h:
        _h.write(b"\0")
    out = api_mb_auth.mb_ratings_submit(api_mb_auth.SubmitBody(paths=[F3]))
    eq(len(fake.calls), 1, "a file with no id and no rating makes no request")
    eq(out["rows"][0]["status"], "skipped", "and its row is a skip")
    ok("no MusicBrainz id" in out["rows"][0]["reason"],
       "with the reason MusicBrainz was not asked")
finally:
    mb.httpx = REAL_HTTPX
    tagcache_mod.read_track = _real_read
    mbresolve.heal_row = _real_heal
    api_mb_auth.load_config = _real_load

# --------------------------------------------------------------------------- #
print("\nfinish accepts a bare code, a redirect URL, or the stored verifier")
# --------------------------------------------------------------------------- #
fake = FakeHTTP()
mb.httpx = fake
try:
    cfg = {mb.CLIENT_ID_KEY: CID, mb.CLIENT_SECRET_KEY: SECRET}
    # A full redirect URL: the ?code= is extracted from it.
    fake.calls.clear()
    fake.queue.append(Resp(200, "", {"access_token": "a1", "expires_in": 3600}))
    fake.queue.append(Resp(200, "", {"sub": "listener"}))
    got = mb.finish_connect(
        "https://musicbrainz.org/oauth2/callback?code=URLCODE&state=s", "v1", cfg)
    eq(got["ok"], True, "a full redirect URL connects")
    eq(fake.calls[0][2]["data"]["code"], "URLCODE",
       "the ?code= is extracted from the pasted URL")
    eq(fake.calls[0][2]["data"]["code_verifier"], "v1",
       "and the verifier handed in alongside is used")

    # A redirect carrying ?error= is MusicBrainz's refusal, in its own words.
    fake.calls.clear()
    got = mb.finish_connect(
        "https://musicbrainz.org/cb?error=access_denied"
        "&error_description=The+user+denied+access", "v1", cfg)
    eq(got["ok"], False, "an error redirect does not connect")
    ok("denied" in got["detail"], "and says so in MusicBrainz's own words")
    eq(fake.calls, [], "with no token request at all")

    # A blank secret is a PUBLIC PKCE client: the token body omits it ENTIRELY,
    # and a code-only finish uses the verifier connect_url saved.
    cfg2 = {mb.CLIENT_ID_KEY: CID, mb.CLIENT_SECRET_KEY: ""}
    mb.connect_url(cfg2)
    stored = cfg2[mb.PKCE_VERIFIER_KEY]
    ok(bool(stored) and cfg2[mb.PKCE_STATE_KEY],
       "connect_url persists the PKCE verifier + state")
    fake.calls.clear()
    fake.queue.append(Resp(200, "", {"access_token": "a2", "expires_in": 3600}))
    fake.queue.append(Resp(200, "", {"sub": "listener"}))
    got = mb.finish_connect("just-the-code", "", cfg2)
    eq(got["ok"], True, "a bare code with the STORED verifier connects")
    data = fake.calls[0][2]["data"]
    eq(data["code_verifier"], stored, "the stored verifier is what was used")
    eq("client_secret" in data, False, "and a blank secret is omitted entirely")
    eq(data["client_id"], CID, "the client id is still sent")
    eq(cfg2[mb.PKCE_VERIFIER_KEY], "", "the verifier is cleared once consumed")

    # The refresh half works the same way for a public client.
    fake.calls.clear()
    cfg3 = {mb.CLIENT_ID_KEY: CID, mb.CLIENT_SECRET_KEY: "",
            mb.REFRESH_KEY: "r", mb.EXPIRES_KEY: int(time.time()) - 10}
    fake.queue.append(Resp(200, "", {"access_token": "a3", "expires_in": 3600}))
    tok, err, _where = mb.access_token(cfg3)
    eq((tok, err), ("a3", ""), "a blank-secret install renews its token")
    eq("client_secret" in fake.calls[0][2]["data"], False,
       "the refresh omits client_secret too")

    # No verifier anywhere is a clear error, not a silent broken request.
    fake.calls.clear()
    got = mb.finish_connect("code", "", {mb.CLIENT_ID_KEY: CID})
    eq(got["ok"], False, "finish without any verifier is a clear error")
    ok("verifier" in got["detail"], "naming what is missing")
    eq(fake.calls, [], "and asking nobody")
finally:
    mb.httpx = REAL_HTTPX

# --------------------------------------------------------------------------- #
print("\nthe upload queue — dedup, coalescing, and a worker submit")
# --------------------------------------------------------------------------- #
from server import mb_ratings_sync as sync                          # noqa: E402

_real_sync_load = sync.load_config
_real_sync_enqueue = sync.enqueue
cfg_q = connected_cfg()
sync.load_config = lambda: cfg_q
# A long window for the checks that only inspect the pending set — the worker
# must not drain it mid-assertion.
sync.COALESCE_SECONDS = 3600.0

QA = os.path.join(MF, "Queue Album")
QA1 = os.path.join(QA, "01 queue.flac")
os.makedirs(QA, exist_ok=True)
with open(QA1, "wb") as _h:
    _h.write(b"\0")
QTAGS = {os.path.normpath(QA1): {"MUSICBRAINZ_TRACKID": REC,
                                 "MUSICBRAINZ_RELEASEGROUPID": RG}}
_real_read2 = tagcache_mod.read_track
tagcache_mod.read_track = lambda path, tag_list=None: (
    {k: QTAGS.get(os.path.normpath(path), {}).get(k) for k in (tag_list or [])}, {})
try:
    sync._PENDING.clear()
    sync._RECENT.clear()
    eq(sync.enqueue(QA), True, "a folder joins the upload queue")
    eq(sync.enqueue(QA), False, "enqueuing the same folder again is a no-op")
    eq(sync.enqueue(QA + os.sep + "."), False,
       "a differently-spelled same folder is deduped (normpath)")
    eq(sync.state()["pending"], 1, "the pending set holds it exactly once")
    ok(sync.state()["connected"] and sync.state()["enabled"],
       "the queue reports a connected, enabled install")

    # The worker: coalesce a burst, build the SAME items the route builds, and
    # record the outcome.
    ratings_mod.set_rating(QA1, 8, mbid=REC, user="")
    ratings_mod.set_rating(QA, 8, user="", scope="album")
    sync._PENDING.clear()
    sync._RECENT.clear()
    sync.COALESCE_SECONDS = 0.05
    fake = FakeHTTP()
    fake.queue.append(Resp(200, "<metadata/>"))
    mb.httpx = fake
    try:
        sync.enqueue(QA)
        recent = []
        for _ in range(100):
            recent = sync.state()["recent"]
            if recent:
                break
            time.sleep(0.05)
        eq(len(recent), 1, "the worker recorded one submission")
        eq(recent[0]["ok"], True, "…marked ok")
        eq(recent[0]["name"], "Queue Album", "…named for its folder")
        eq(recent[0]["path"], os.path.normpath(QA), "…carrying the folder path")
        eq(recent[0]["sent"], 2, "both the recording and the release group sent")
        eq(len(fake.calls), 1, "the whole album went in ONE request")
        eq(fake.calls[0][2]["content"].decode("utf-8"),
           mb.build_body([("recording", REC, 80), ("release_group", RG, 80)]),
           "the worker builds the SAME body the submit route does")
        eq(sync.state()["pending"], 0, "and the pending set is drained")
    finally:
        mb.httpx = REAL_HTTPX
        sync.COALESCE_SECONDS = 3600.0

    # submit-all: every RATED folder, and nothing else.
    sync._PENDING.clear()
    sync._RECENT.clear()
    UNRATED = os.path.join(MF, "Unrated Album")
    os.makedirs(UNRATED, exist_ok=True)
    ARTIST = os.path.join(MF, "Solo Artist")
    os.makedirs(ARTIST, exist_ok=True)
    ratings_mod.set_rating(ARTIST, 8, user="", scope="artist")
    queued = sync.enqueue_all(cfg_q)
    ok(queued >= 2, "submit-all queues the app's rated folders")
    eq(sync.state()["pending"], queued, "and returns how many joined the queue")
    ok(os.path.normpath(QA) in sync._PENDING, "the rated album is queued")
    ok(os.path.normpath(ALBUM) in sync._PENDING,
       "so is the album rated through the submit route")
    ok(os.path.normpath(UNRATED) not in sync._PENDING,
       "a folder with NO rating is skipped")
    ok(os.path.normpath(ARTIST) not in sync._PENDING,
       "and an ARTIST rating is not submitted (no MB entity for it)")

    # The rating write itself feeds the queue — track scope hands over the
    # FILE's folder, album scope the folder, artist scope nothing.
    sync._PENDING.clear()
    recorded = []
    sync.enqueue = lambda folder: (recorded.append(os.path.normpath(str(folder))),
                                   True)[1]
    try:
        recorded.clear()
        ratings_mod.rate(QA1, 9, mbid=REC, cfg={})
        ok(os.path.normpath(QA) in recorded,
           "a track rating enqueues the folder the file sits in")
        recorded.clear()
        ratings_mod.rate(QA, 9, scope="album", mbid=RG, cfg={})
        ok(os.path.normpath(QA) in recorded,
           "an album rating enqueues the folder itself")
        recorded.clear()
        ratings_mod._enqueue_mb_sync("artist", QA)
        eq(recorded, [], "an artist rating enqueues NOTHING")

        recorded.clear()
        ratings_mod.bulk_rate([QA1], 7, cfg={})
        ok(os.path.normpath(QA) in recorded, "a bulk track rating enqueues too")
    finally:
        sync.enqueue = _real_sync_enqueue
finally:
    tagcache_mod.read_track = _real_read2
    sync.load_config = _real_sync_load
    sync.COALESCE_SECONDS = 5.0
    # Leave no work for a worker that would then try the REAL network.
    sync._PENDING.clear()
    sync._RECENT.clear()
    sync._WAKE.set()
    for _ in range(40):
        if sync._WORKER is None or not sync._WORKER.is_alive():
            break
        time.sleep(0.05)

# --------------------------------------------------------------------------- #
print("\na real scratch server (8017+) — every route, nothing connected")
# --------------------------------------------------------------------------- #
PORT = 0
for cand in range(8017, 8031):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", cand))
            PORT = cand
            break
        except OSError:
            continue
if not PORT:
    print("  SKIP  no free port in 8017..8030")
    raise SystemExit(2 if FAILED else 0)


def _request(port, method, path, body=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


try:
    import uvicorn
    from server.main import app as mlo_app
    from server import api_mb_auth
except Exception as e:                                                # pragma: no cover
    print(f"  SKIP  the app cannot be imported here: {e}")
    raise SystemExit(2 if FAILED else 0)

# The mount `server/main.py` gets for this router (the include line handed to
# the owner): adding it here is what lets this suite prove the routes on a real
# standing server before main.py knows them. The routes are moved to the FRONT
# because this app is imported already built — its SPA catch-all GET
# (`/{full_path:path}`) would otherwise swallow the status route — while a real
# main.py includes them before that catch-all, exactly as they end up here.
_before = len(mlo_app.router.routes)
mlo_app.include_router(api_mb_auth.router)
_added = mlo_app.router.routes[_before:]
del mlo_app.router.routes[_before:]
mlo_app.router.routes[:0] = _added

# Nothing connected, and no client id saved: exactly what a fresh install has.
cfgmod.save_config({mb.CLIENT_ID_KEY: "", mb.CLIENT_SECRET_KEY: "",
                    mb.REFRESH_KEY: "", mb.ACCESS_KEY: "",
                    mb.EXPIRES_KEY: 0, mb.USERNAME_KEY: "",
                    mb.SUBMIT_KEY: True})

server = uvicorn.Server(uvicorn.Config(mlo_app, host="127.0.0.1", port=PORT,
                                       log_level="warning", lifespan="off"))
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
for _ in range(200):
    if server.started:
        break
    time.sleep(0.05)
ok(server.started, f"the scratch server is listening on 127.0.0.1:{PORT}")

if server.started:
    status_code, body = _request(PORT, "GET", "/api/mb/auth/status")
    eq(status_code, 200, "GET /api/mb/auth/status answers 200")
    eq(body.get("connected"), False, "and says nothing is connected")
    eq(body.get("submit_enabled"), True, "with the submission switch reported")
    eq(body.get("client_id_set"), False, "and no client id saved")
    eq(body.get("pending"), 0, "with the upload queue's pending count (empty)")

    status_code, body = _request(PORT, "GET", "/api/mb/ratings/queue")
    eq(status_code, 200, "GET /api/mb/ratings/queue answers 200")
    eq(body.get("ok"), True, "and reports the queue")
    eq((body.get("connected"), body.get("enabled")), (False, True),
       "naming the login and switch state")
    ok(isinstance(body.get("pending"), int) and isinstance(body.get("recent"), list),
       "with a numeric pending count and a recent list")

    status_code, body = _request(PORT, "POST", "/api/mb/ratings/submit-all")
    eq(status_code, 200, "POST /api/mb/ratings/submit-all answers 200")
    eq(body.get("ok"), True, "and answers ok")
    eq(body.get("queued"), 0, "queuing nothing while no login is connected")

    status_code, body = _request(PORT, "POST", "/api/mb/auth/connect")
    eq(status_code, 200, "POST /api/mb/auth/connect answers 200")
    eq(body.get("ok"), False, "and refuses without a client id")
    ok("client id" in body.get("detail", ""), "in a sentence naming what to paste")

    status_code, body = _request(PORT, "POST", "/api/mb/auth/finish",
                                 {"code": "x", "verifier": "y"})
    eq(status_code, 200, "POST /api/mb/auth/finish answers 200")
    eq(body.get("ok"), False, "and refuses with nothing to exchange")

    status_code, body = _request(PORT, "POST", "/api/mb/auth/disconnect")
    eq(status_code, 200, "POST /api/mb/auth/disconnect answers 200")
    eq(body.get("connected"), False, "leaving nothing connected")

    status_code, body = _request(PORT, "POST", "/api/mb/ratings/submit",
                                 {"paths": [os.path.join(MF, "x.flac")]})
    eq(status_code, 400, "POST /api/mb/ratings/submit refuses with no login")
    ok("no MusicBrainz login" in json.dumps(body), "naming the missing login")

    # a saved client id is NOT a connection: connect now builds a real URL
    cfgmod.save_config({mb.CLIENT_ID_KEY: CID, mb.CLIENT_SECRET_KEY: SECRET})
    status_code, body = _request(PORT, "POST", "/api/mb/auth/connect")
    eq(status_code, 200, "connect answers 200 with a client id saved")
    ok(body.get("ok") and "code_challenge_method=S256" in body.get("url", ""),
       "and hands back the PKCE authorize URL")
    ok(len(body.get("verifier", "")) >= 43, "with the verifier to keep")

    # switched off: the submit route says so before anything else
    cfgmod.save_config({mb.SUBMIT_KEY: False})
    status_code, body = _request(PORT, "POST", "/api/mb/ratings/submit",
                                 {"paths": [os.path.join(MF, "x.flac")]})
    eq(status_code, 400, "submit refuses when submissions are switched off")
    ok("switched off" in json.dumps(body), "with the switch named")
    cfgmod.save_config({mb.SUBMIT_KEY: True, mb.CLIENT_ID_KEY: "",
                        mb.CLIENT_SECRET_KEY: ""})

    server.should_exit = True
    thread.join(timeout=10)
    ok(not thread.is_alive(), "and the scratch server stops cleanly")

# --------------------------------------------------------------------------- #
print(f"\n{PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for label in FAILED:
    print("  FAILED:", label)
raise SystemExit(1 if FAILED else 0)