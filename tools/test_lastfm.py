#!/usr/bin/env python3
"""Last.fm scrobbling (`server.lastfm`, `server.api_lastfm`) — the wire shape,
OFFLINE, plus a standing scratch server.

What this pins, and what it refuses to guess:

  * the SIGNATURE against the API docs' own worked example — the md5 hex for
    two fixed inputs is hardcoded here (both values were computed OUTSIDE the
    code under test with hashlib on the documented concatenation), so a signer
    that sorted wrong, kept `format`, or encoded non-UTF-8 fails loudly;
  * the SCROBBLE RULE (Last.fm's own): 29 s never counts; 30 s counts at 50 %;
    a 10-minute track counts at 4:00; 9:59 at 3:59 does not — half the track
    OR four minutes, whichever comes FIRST;
  * the wire shape of the two authenticated writes: `sk` + `api_sig` travel,
    a scrobble carries the play's START (indexed `artist[0]`/`track[0]`/
    `timestamp[0]`), and one call never carries more than 50;
  * the SPOOL: a refused send queues, the next flush sends it and clears the
    file, and a reload (a restart) keeps it;
  * the REFUSAL paths: no key / no secret / no session / switch off each send
    NOTHING and say why, and `verify_session` is a `checked: ""` sentence
    rather than an exception;
  * that the four routes answer sanely on a real server (8017+): status and
    connect refuse politely with nothing saved, a scrobble queues nothing and
    says why, then the full connect → status → scrobble → disconnect round
    trip — with the network stubbed at `lastfm._http_post` throughout.

No live Last.fm call is possible here: a real scrobble needs the owner's own
registered application (its API key AND shared secret) plus one authorization,
which this suite does not have (see `left` in the run's report).

Run:  python tools/test_lastfm.py   (exit 0 = pass, 1 = failure, 2 = skip)
"""
import atexit
import importlib
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
SCRATCH = os.environ.get("MLO_LASTFM_SCRATCH") or os.path.join(_BASE, "mlo-lastfm")
MF = os.path.join(SCRATCH, "music")
STATE = os.path.join(SCRATCH, "state")
for _d in (MF, STATE):
    os.makedirs(_d, exist_ok=True)
os.environ["MLO_MUSIC_FOLDER"] = MF
os.environ["MLO_APP_DATA_DIR"] = STATE
atexit.register(lambda: shutil.rmtree(SCRATCH, ignore_errors=True))
atexit.register(lambda: os.environ.pop("MLO_MUSIC_FOLDER", None))
atexit.register(lambda: os.environ.pop("MLO_APP_DATA_DIR", None))

import mlo.config as cfgmod                                          # noqa: E402
from server import lastfm                                            # noqa: E402

# Seed the scratch config where the app actually reads it (the folder the
# routes resolve through), so nothing here touches the developer's config.
with open(cfgmod.active_config_file(), "w", encoding="utf-8") as _f:
    json.dump({"music_folder": MF}, _f)

PASSED = 0
FAILED = []

KEY = "YOUR_API_KEY"
SECRET = "YOUR_SECRET"
SESSION = "YOUR_SESSION_KEY"


def ok(cond, label):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok: {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL: {label}")


def eq(got, want, label):
    ok(got == want, label if got == want
       else f"{label}\n        got  {got!r}\n        want {want!r}")


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class FakeHTTP:
    """`lastfm._http_post` stand-in: records every call, answers from a queue."""

    def __init__(self):
        self.calls = []
        self.queue = []

    def __call__(self, data, timeout=None):
        self.calls.append({"data": dict(data), "timeout": timeout})
        if not self.queue:
            raise AssertionError(f"unexpected Last.fm call: {data}")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


REAL_POST = lastfm._http_post
fake = FakeHTTP()


def connected_cfg(**over):
    cfg = {"lastfm_api_key": KEY, "lastfm_api_secret": SECRET,
           "lastfm_session_key": SESSION, "lastfm_username": "listener",
           "lastfm_scrobble_enabled": True}
    cfg.update(over)
    return cfg


# --------------------------------------------------------------------------- #
print("\nthe signature — the API docs' own worked example")
# --------------------------------------------------------------------------- #
# Both hex values were produced with hashlib on the documented concatenations
# (see the module docstring) and are the values the docs print.
eq(lastfm.sign({"method": "auth.getSession", "api_key": "YOUR_API_KEY",
                "token": "YOUR_REQUESTED_TOKEN", "format": "json"},
               "YOUR_SECRET"),
   "94539006de89b3c6b3c030bb1e52b9c4",
   "auth.getSession example signs to the documented md5 (format excluded)")
eq(lastfm.sign({"method": "track.love", "artist": "KITANO REM",
                "track": "RAINSICK", "api_key": "YOUR_API_KEY",
                "sk": "YOUR_SESSION_KEY"}, "YOUR_SECRET"),
   "800b8884b00c9343d1d425ed271e0f42",
   "track.love example signs to the documented md5 (sk included)")
# format and callback are the two Last.fm never signs, and api_sig is never
# re-signed whichever order the dict was built in.
eq(lastfm.sign({"b": "2", "a": "1", "format": "json", "callback": "cb",
                "api_sig": "deadbeef"}, SECRET),
   lastfm.sign({"a": "1", "b": "2"}, SECRET),
   "format, callback and api_sig are excluded from the signature")

# --------------------------------------------------------------------------- #
print("\nthe scrobble rule — a percentage of the track, 70 % by default")
# --------------------------------------------------------------------------- #
# The owner's call: "scrobbling to last.fm should only occur if a certain
# amount of the song is listened to" — a PERCENTAGE, so a 2-minute track and a
# 10-minute one are judged by the same share of the listen. The floor stays
# Last.fm's own 50 % (it discards a scrobble below half the track), and the
# shipped default is 70.
eq(lastfm.scrobble_percent(), 70.0, "the shipped default is 70 %")
eq(lastfm.scrobble_threshold(29), None, "a 29 s track is never scrobbled")
eq(lastfm.meets_rule(29, 29), False, "…not even played to the end")
eq(lastfm.scrobble_threshold(100, {"lastfm_scrobble_percent": 70}), 70.0,
   "a 100 s track needs 70 s")
eq(lastfm.meets_rule(100, 70, {"lastfm_scrobble_percent": 70}), True,
   "…which it reaches at 70 s")
eq(lastfm.meets_rule(100, 69.9, {"lastfm_scrobble_percent": 70}), False,
   "…but not a tenth of a second earlier")
eq(lastfm.scrobble_threshold(600, {"lastfm_scrobble_percent": 70}), 420.0,
   "a 10-minute track needs 7 minutes — no 4-minute cap")
eq(lastfm.meets_rule(600, 420, {"lastfm_scrobble_percent": 70}), True, "…at 7:00")
eq(lastfm.meets_rule(600, 419, {"lastfm_scrobble_percent": 70}), False, "…not at 6:59")
eq(lastfm.scrobble_percent({"lastfm_scrobble_percent": 50}), 50.0,
   "the config is honoured down to Last.fm's own 50 %")
eq(lastfm.scrobble_threshold(100, {"lastfm_scrobble_percent": 50}), 50.0,
   "…a 100 s track then needs 50 s")
eq(lastfm.scrobble_percent({"lastfm_scrobble_percent": 30}), 50.0,
   "below 50 % is clamped: Last.fm would discard the scrobble")
eq(lastfm.scrobble_percent({"lastfm_scrobble_percent": 100}), 100.0,
   "100 % means only a play that reached the end counts")
eq(lastfm.meets_rule(100, 100, {"lastfm_scrobble_percent": 100}), True, "…at the end")
eq(lastfm.meets_rule(100, 99, {"lastfm_scrobble_percent": 100}), False, "…not before it")
eq(lastfm.scrobble_percent({"lastfm_scrobble_percent": "junk"}), 70.0,
   "junk reads as the shipped default")
eq(lastfm.scrobble_percent({}), 70.0, "a config without the key reads as the default")
eq(lastfm.scrobble_threshold("junk"), None, "junk duration is no threshold")
eq(lastfm.meets_rule(600, "junk"), False, "junk played seconds never count")
eq(lastfm.meets_rule(600, "junk", {"lastfm_scrobble_percent": "junk"}), False,
   "junk on both sides still never counts")

# --------------------------------------------------------------------------- #
print("\nthe track's own metadata — read from the file, never invented")
# --------------------------------------------------------------------------- #
import mlo.audio as audio_mod                                        # noqa: E402

REAL_AF = audio_mod.AudioFile
AF_TAGS = {"TITLE": "Rainsick", "ARTIST": "KITANO REM", "ALBUM": "Rain",
           "ALBUMARTIST": "KITANO", "TRACKNUMBER": "3",
           "MUSICBRAINZ_TRACKID": "abc-123"}


class FakeAF:
    def __init__(self, path):
        self._tags = dict(AF_TAGS)
        self.tech = {}
        self.audio = type("A", (), {"info": type("I", (), {"length": 214.7})()})()

    def get_tag(self, name):
        return self._tags.get(name)


audio_mod.AudioFile = FakeAF
try:
    meta = lastfm.track_meta("F:/nowhere/does-not-exist.flac")
    eq(meta["ok"], False, "a missing file reports not-ok")
    eq(meta["error"], "the file is not there", "and says the file is not there")
except Exception as exc:                                              # pragma: no cover
    ok(False, f"a missing file raised {exc!r}")

# The reader only ever runs on a path that exists; a real temp file stands in.
_probe = os.path.join(MF, "probe.flac")
open(_probe, "wb").close()
meta = lastfm.track_meta(_probe)
eq(meta["ok"], True, "a readable file with tags is ok")
eq((meta["title"], meta["artist"]), ("Rainsick", "KITANO REM"),
   "title and artist come from the tags")
eq(meta["album_artist"], "KITANO", "album artist comes from ALBUMARTIST")
eq(meta["mbid"], "abc-123", "the MusicBrainz track id comes from its tag")
eq(meta["duration"], 214.7, "duration comes from the audio header")
AF_TAGS.clear()
meta = lastfm.track_meta(_probe)
eq(meta["ok"], False, "an untagged file is not ok")
eq((meta["title"], meta["artist"]), ("", ""), "and nothing is invented for it")
eq(meta["error"], "the file carries no artist/title tags", "the reason is stated")
AF_TAGS.update({"TITLE": "Rainsick", "ARTIST": "KITANO REM", "ALBUM": "Rain",
                "ALBUMARTIST": "KITANO", "TRACKNUMBER": "3",
                "MUSICBRAINZ_TRACKID": "abc-123"})
# The tags are back, so re-read the good metadata for the wire-shape checks —
# still through the stub reader (the real one cannot parse a 0-byte file).
meta = lastfm.track_meta(_probe)
eq(meta["ok"], True, "the file reads ok again once its tags are back")
audio_mod.AudioFile = REAL_AF

# --------------------------------------------------------------------------- #
print("\nthe wire shape — sk + api_sig, and the play's START as the timestamp")
# --------------------------------------------------------------------------- #
lastfm._http_post = fake
try:
    fake.calls.clear()
    fake.queue.append(Resp(200, {"nowplaying": {"artist": {"#text": "KITANO REM"}}}))
    ok_, err = lastfm.now_playing(connected_cfg(), meta)
    eq((ok_, err), (True, ""), "now-playing on a connected cfg is ok")
    data = fake.calls[0]["data"]
    eq(data["method"], "track.updateNowPlaying", "it calls track.updateNowPlaying")
    eq(data["sk"], SESSION, "with the session key")
    eq(data["api_key"], KEY, "the app's key")
    eq(data["format"], "json", "asking for JSON")
    eq(data["api_sig"], lastfm.sign(data, SECRET),
       "and a signature over exactly the parameters sent")
    eq(data["artist"], "KITANO REM", "the artist tag travels")
    eq(data["track"], "Rainsick", "the title travels as `track`")
    eq(data["duration"], "215", "the duration travels as whole seconds")
    eq(data["mbid"], "abc-123", "the recording id travels as `mbid`")

    fake.calls.clear()
    fake.queue.append(Resp(200, {"scrobbles": {"@attr": {"accepted": 1,
                                                         "ignored": 0}}}))
    accepted, err = lastfm.send_scrobbles(connected_cfg(), [{
        "artist": "KITANO REM", "track": "Rainsick", "album": "Rain",
        "album_artist": "KITANO", "duration": 215, "track_number": "3",
        "mbid": "abc-123", "timestamp": 1700000000}])
    eq((accepted, err), (1, ""), "a scrobble reports accepted=1")
    data = fake.calls[0]["data"]
    eq(data["method"], "track.scrobble", "it calls track.scrobble")
    eq(data["sk"], SESSION, "with the session key")
    eq(data["timestamp[0]"], "1700000000", "the play's START is the timestamp")
    eq(data["artist[0]"], "KITANO REM", "the batch form is indexed from 0")
    eq(data["track[0]"], "Rainsick", "the title is indexed too")
    eq(data["duration[0]"], "215", "and so is the duration")
    eq(data["api_sig"], lastfm.sign(data, SECRET),
       "the signature covers the indexed parameters")

    # A batch is sliced at Last.fm's own ceiling of 50 per call.
    fake.calls.clear()
    fake.queue.append(Resp(200, {"scrobbles": {"@attr": {"accepted": 50}}}))
    many = [{"artist": "A", "track": f"t{i}", "timestamp": 1700000000 + i}
            for i in range(51)]
    lastfm.send_scrobbles(connected_cfg(), many)
    data = fake.calls[0]["data"]
    ok("timestamp[49]" in data and "timestamp[50]" not in data,
       "one call carries at most 50 scrobbles")
finally:
    lastfm._http_post = REAL_POST

# --------------------------------------------------------------------------- #
print("\nrefusals send NOTHING and say why")
# --------------------------------------------------------------------------- #
lastfm._http_post = fake
try:
    fake.calls.clear()
    for cfg, needle, label in (
        ({}, "lastfm_api_key", "no API key"),
        ({"lastfm_api_key": KEY}, "lastfm_api_secret", "no shared secret"),
        ({"lastfm_api_key": KEY, "lastfm_api_secret": SECRET},
         "not connected", "no session"),
        (connected_cfg(lastfm_scrobble_enabled=False), "scrobbling is off",
         "the switch is off"),
    ):
        why = lastfm.refusal(cfg)
        ok(needle in why, f"{label} refuses with a sentence ({why!r})")
        sent = lastfm.now_playing(cfg, meta)
        eq(sent[0], False, f"{label}: now-playing sends nothing")
        eq(lastfm.send_scrobbles(cfg, [{"artist": "A", "track": "B",
                                        "timestamp": 1}])[0], 0,
           f"{label}: no scrobble is sent")
    eq(fake.calls, [], "not one request left the machine for any refusal")

    st = lastfm.status({})
    eq((st["enabled"], st["connected"], st["key"], st["secret"], st["queued"]),
       (True, False, False, False, 0),
       "status names exactly what is missing on a fresh install")
    eq(lastfm.status(connected_cfg())["ready"], True,
       "status says ready only with key + secret + session + switch")

    # verify_session: a blank credential is a sentence, never a raise
    got = lastfm.verify_session({})
    eq((got["ok"], got["checked"]), (False, ""),
       "verify_session with no key is not-ok and claims no check")
    ok("lastfm_api_key" in got["detail"], "…saying which credential is missing")
    got = lastfm.verify_session({"lastfm_api_key": KEY, "lastfm_api_secret": SECRET})
    eq((got["ok"], got["checked"]), (False, ""),
       "verify_session with no session is not-ok and claims no check")
    ok("not connected" in got["detail"], "…saying it is not connected")

    fake.calls.clear()
    fake.queue.append(Resp(200, {"user": {"name": "listener"}}))
    got = lastfm.verify_session(connected_cfg())
    eq((got["ok"], got["checked"]), (True, "user.getInfo"),
       "a saved session is proved with an AUTHENTICATED read")
    ok("listener" in got["detail"], "and the account is named in its own words")
    eq(fake.calls[0]["data"]["method"], "user.getInfo", "user.getInfo is the read")
    eq(fake.calls[0]["data"]["sk"], SESSION, "…authenticated with the session")

    fake.queue.append(Resp(200, {"error": 9, "message": "Invalid session key"}))
    got = lastfm.verify_session(connected_cfg())
    eq(got["ok"], False, "a refused session is not-ok")
    ok("Invalid session key" in got["detail"],
       "reported in Last.fm's own words")

    # auth.getToken / auth.getSession
    fake.calls.clear()
    fake.queue.append(Resp(200, {"token": "tok-1"}))
    token, err = lastfm.get_token({"lastfm_api_key": KEY})
    eq((token, err), ("tok-1", ""), "auth.getToken hands back a request token")
    eq(fake.calls[0]["data"].get("api_sig"), None,
       "…unsigned: there is no session yet to sign with")
    ok(lastfm.auth_url({"lastfm_api_key": KEY}, "tok-1")
       == f"{lastfm.AUTH_URL}?api_key={KEY}&token=tok-1",
       "the auth URL carries the key and the token")

    fake.calls.clear()
    fake.queue.append(Resp(200, {"session": {"name": "listener",
                                             "key": "sk-1"}}))
    out = lastfm.get_session(connected_cfg(), "tok-1")
    eq((out["ok"], out["name"], out["key"], out["code"], out["reason"]),
       (True, "listener", "sk-1", "", ""),
       "auth.getSession exchanges the token for a session key")
    data = fake.calls[0]["data"]
    eq(data["token"], "tok-1", "the token is what the call carries")
    eq(data.get("sk"), None, "and NOT a session key — none exists yet")
    eq(data["api_sig"], lastfm.sign(data, SECRET), "the call is signed")

    # The ONE refusal the connect UI must WAIT OUT: error 14 is
    # distinguishable, in Last.fm's own words, and never raises.
    fake.calls.clear()
    fake.queue.append(Resp(200, {"error": 14,
                                 "message": "This token has not been authorized"}))
    out = lastfm.get_session(connected_cfg(), "tok-1")
    eq((out["ok"], out["code"], out["key"]), (False, "NOT_AUTHORIZED", ""),
       "an unauthorized token is a NOT_AUTHORIZED outcome, not an error")
    ok("This token has not been authorized" in out["reason"],
       "carrying Last.fm's own sentence")

    # Any OTHER refusal is not the wait case.
    fake.calls.clear()
    fake.queue.append(Resp(200, {"error": 4, "message": "Invalid method signature"}))
    out = lastfm.get_session(connected_cfg(), "tok-1")
    eq((out["ok"], out["code"]), (False, ""),
       "a different refusal is not NOT_AUTHORIZED")

    # A blank token never touches the network.
    fake.calls.clear()
    out = lastfm.get_session(connected_cfg(), "")
    eq((out["ok"], out["code"], fake.calls), (False, "", []),
       "a blank token answers not-ok without a request")
finally:
    lastfm._http_post = REAL_POST

# --------------------------------------------------------------------------- #
print("\nthe spool — a refused scrobble survives a restart")
# --------------------------------------------------------------------------- #
spool = lastfm.spool_path()
ok(spool.startswith(os.path.join(MF, ".mlo")),
   f"the spool lives under the app's own state dir ({spool})")

entry = {"artist": "KITANO REM", "track": "Rainsick", "album": "Rain",
         "album_artist": "KITANO", "duration": 215, "track_number": "3",
         "mbid": "", "timestamp": int(time.time())}
eq(lastfm.queue_scrobble(entry), 1, "a refused scrobble queues")
eq(lastfm.queued_count(), 1, "and the queue counts it")
ok(os.path.isfile(spool), "the spool file exists on disk")

lastfm._http_post = fake
try:
    fake.calls.clear()
    fake.queue.append(Resp(200, {"scrobbles": {"@attr": {"accepted": 1}}}))
    out = lastfm.flush_spool(connected_cfg())
    eq((out["ok"], out["flushed"], out["queued"]), (True, 1, 0),
       "the next flush sends it and reports the queue empty")
    eq(fake.calls[0]["data"]["track[0]"], "Rainsick",
       "…sending the queued play itself")
    eq(lastfm.queued_count(), 0, "and the file is cleared")

    # A flush that fails keeps every row.
    lastfm.queue_scrobble(entry)
    fake.queue.append(Resp(503, None))
    out = lastfm.flush_spool(connected_cfg())
    eq((out["ok"], out["queued"]), (False, 1),
       "a refused flush keeps the queued scrobble")
    eq(lastfm.queued_count(), 1, "and the file still holds it")
    lastfm._write_spool([])
finally:
    lastfm._http_post = REAL_POST

# A restart: queue, reload the module (fresh process state), and the row is
# still there — the spool is on disk, not in memory.
lastfm.queue_scrobble(entry)
reloaded = importlib.reload(lastfm)
eq(reloaded.queued_count(), 1, "a reload (a restart) still sees the queued row")
eq(reloaded._read_spool()[0]["track"], "Rainsick", "and it is the same play")
reloaded._http_post = fake
try:
    fake.calls.clear()
    fake.queue.append(Resp(200, {"scrobbles": {"@attr": {"accepted": 1}}}))
    out = reloaded.flush_spool(connected_cfg())
    eq(out["flushed"], 1, "and the restarted process can flush it")
finally:
    reloaded._http_post = REAL_POST
lastfm._http_post = REAL_POST
lastfm = reloaded
lastfm._write_spool([])

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
    from server import api_lastfm
except Exception as e:                                                # pragma: no cover
    print(f"  SKIP  the app cannot be imported here: {e}")
    raise SystemExit(2 if FAILED else 0)

# The mount `server/main.py` gets for this router (the include line handed to
# the owner). Moved to the FRONT because this app is imported already built —
# its SPA catch-all GET (`/{full_path:path}`) would otherwise swallow the
# status route — while a real main.py includes them before that catch-all.
_before = len(mlo_app.router.routes)
mlo_app.include_router(api_lastfm.router)
_added = mlo_app.router.routes[_before:]
del mlo_app.router.routes[_before:]
mlo_app.router.routes[:0] = _added

# Nothing saved: exactly what a fresh install has.
cfgmod.save_config({"lastfm_api_key": "", "lastfm_api_secret": "",
                    "lastfm_session_key": "", "lastfm_username": "",
                    "lastfm_scrobble_enabled": True})
# The routes read metadata through the real reader; this 0-byte scratch file
# cannot be parsed, so the same stub stands in for the route checks (its
# 214.7 s duration is what the rule is judged against).
audio_mod.AudioFile = FakeAF
lastfm._http_post = fake
fake.calls.clear()

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
    status_code, body = _request(PORT, "GET", "/api/lastfm/status")
    eq(status_code, 200, "GET /api/lastfm/status answers 200")
    eq(body.get("connected"), False, "and says nothing is connected")
    eq(body.get("enabled"), True, "with the switch reported")
    eq(body.get("key"), False, "and no API key saved")
    eq(body.get("queued"), 0, "and an empty queue")

    status_code, body = _request(PORT, "POST", "/api/lastfm/connect")
    eq(status_code, 200, "POST /api/lastfm/connect answers 200")
    eq(body.get("ok"), False, "and refuses without an API key")
    ok("lastfm_api_key" in body.get("error", ""),
       "in a sentence naming what to paste")

    status_code, body = _request(PORT, "POST", "/api/lastfm/now-playing",
                                 {"path": os.path.join(MF, "probe.flac")})
    eq(status_code, 200, "POST /api/lastfm/now-playing answers 200")
    eq(body.get("ok"), False, "and sends nothing with no session")
    ok("lastfm_api_key" in body.get("error", ""),
       "saying which credential is missing")

    status_code, body = _request(PORT, "POST", "/api/lastfm/scrobble",
                                 {"path": os.path.join(MF, "probe.flac"),
                                  "played_at": int(time.time()),
                                  "played_seconds": 200})
    eq(status_code, 200, "POST /api/lastfm/scrobble answers 200 (never a 500)")
    eq(body.get("ok"), False, "it does not scrobble with no session")
    eq(body.get("queued"), False, "and queues NOTHING — it would only be refused again")
    ok("lastfm_api_key" in body.get("error", ""), "saying why")
    eq(fake.calls, [], "and no Last.fm request was made at all")

    # A saved key+secret (but no session) is enough for the Connect flow to
    # build a real authorization URL — the network is still stubbed.
    cfgmod.save_config({"lastfm_api_key": KEY, "lastfm_api_secret": SECRET})
    fake.calls.clear()
    fake.queue.append(Resp(200, {"token": "tok-1"}))
    status_code, body = _request(PORT, "POST", "/api/lastfm/connect")
    eq(status_code, 200, "connect answers 200 once key + secret are saved")
    eq(body.get("ok"), True, "and hands back a token to authorize")
    eq(body.get("token"), "tok-1", "the request token itself")
    ok(body.get("url", "").endswith("token=tok-1"), "plus the authorization URL")

    # A not-yet-authorized token: wait=true is a 200 "keep polling" answer...
    fake.queue.append(Resp(200, {"error": 14,
                                 "message": "This token has not been authorized"}))
    status_code, body = _request(PORT, "POST", "/api/lastfm/connect/finish",
                                 {"token": "tok-1", "wait": True})
    eq(status_code, 200, "connect/finish while unauthorized still answers 200")
    eq((body.get("ok"), body.get("waiting")), (False, True),
       "…with waiting:true, not an error")
    ok("not been authorized" in body.get("reason", ""),
       "and Last.fm's own words for the reason")
    # ...while every other refusal keeps today's error shape.
    fake.queue.append(Resp(200, {"error": 14,
                                 "message": "This token has not been authorized"}))
    status_code, body = _request(PORT, "POST", "/api/lastfm/connect/finish",
                                 {"token": "tok-1"})
    eq(status_code, 200, "connect/finish without wait answers 200")
    eq((body.get("ok"), body.get("waiting")), (False, None),
       "and an unauthorized token is a plain error there")
    ok("error" in body, "with the error key a UI already handles")

    fake.queue.append(Resp(200, {"session": {"name": "listener", "key": "sk-1"}}))
    status_code, body = _request(PORT, "POST", "/api/lastfm/connect/finish",
                                 {"token": "tok-1"})
    eq(status_code, 200, "connect/finish answers 200")
    eq(body.get("ok"), True, "and stores the session")
    eq(body.get("username"), "listener", "naming the account Last.fm reported")

    status_code, body = _request(PORT, "GET", "/api/lastfm/status")
    eq((body.get("connected"), body.get("username")), (True, "listener"),
       "status now says who is connected")

    # Connected, but the service refuses the scrobble: it is QUEUED, 200.
    fake.queue.append(Resp(503, None))
    status_code, body = _request(PORT, "POST", "/api/lastfm/scrobble",
                                 {"path": os.path.join(MF, "probe.flac"),
                                  "played_at": int(time.time()),
                                  "played_seconds": 200})
    eq(status_code, 200, "a refused scrobble is still answered 200")
    eq(body.get("queued"), True, "…and lands in the queue")
    eq(body.get("queued_count"), 1, "with the count back")

    # Under the rule: the file's own duration says 214.7 s, so 10 s is not
    # enough and nothing is sent or queued.
    fake.calls.clear()
    status_code, body = _request(PORT, "POST", "/api/lastfm/scrobble",
                                 {"path": os.path.join(MF, "probe.flac"),
                                  "played_at": int(time.time()),
                                  "played_seconds": 10})
    eq(status_code, 200, "a play under the rule answers 200")
    eq(body.get("scrobbled"), False, "it is not scrobbled")
    eq(body.get("queued"), False, "and not queued either")
    ok("waits for" in body.get("reason", "") and "70 %" in body.get("reason", ""),
       "with the rule stated back")
    eq(fake.calls, [], "and no request for it")

    status_code, body = _request(PORT, "POST", "/api/lastfm/disconnect")
    eq(status_code, 200, "POST /api/lastfm/disconnect answers 200")
    eq(body.get("ok"), True, "and disconnects")
    eq(body.get("status", {}).get("connected"), False, "leaving nothing connected")

    server.should_exit = True
    thread.join(timeout=10)
    ok(not thread.is_alive(), "and the scratch server stops cleanly")

lastfm._http_post = REAL_POST
audio_mod.AudioFile = REAL_AF
lastfm._write_spool([])

# --------------------------------------------------------------------------- #
print(f"\n{PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for label in FAILED:
    print("  FAILED:", label)
raise SystemExit(1 if FAILED else 0)