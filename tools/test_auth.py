#!/usr/bin/env python3
"""The login gate and the event bus — server/auth.py, server/api_auth.py,
server/events.py.

The gate protects a music library that can be deleted, retagged and downloaded
into, so the checks here are about the things that would make it useless
rather than about HTTP plumbing: a wrong password never passes, a token cannot
be forged or reused after a revoke or expiry, the backoff actually locks a
guessing client out, and the "required" decision follows the bind address
(loopback stays open, everything else does not — including the `off`/LAN
misconfiguration, which must NOT silently publish an open library).

Runs with no network and no server: the HTTP checks drive the real FastAPI app
through TestClient with the gate forced on, over a temp auth database.
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{('  — ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILED.append(name)


from server import auth as auth_mod  # noqa: E402
from server import events as events_mod  # noqa: E402

print("== password hashing ==")
stored = auth_mod.hash_password("correct horse battery")
check("hash is not the password", "correct horse battery" not in stored)
check("hash carries its own rounds/salt", stored.startswith("pbkdf2$") and len(stored.split("$")) == 4)
check("correct password verifies", auth_mod.verify_password("correct horse battery", stored))
check("wrong password does not", not auth_mod.verify_password("correct horse batteru", stored))
check("empty password does not", not auth_mod.verify_password("", stored))
check("garbage stored hash does not pass", not auth_mod.verify_password("x", "pbkdf2$0$$"))
check("plain text stored value does not pass", not auth_mod.verify_password("hunter2", "hunter2"))
check("missing hash does not pass", not auth_mod.verify_password("hunter2", ""))
other = auth_mod.hash_password("correct horse battery")
check("same password hashes differently (salt)", other != stored)

print("== password policy ==")
check("empty rejected", bool(auth_mod.password_problem("")))
check("too short rejected", bool(auth_mod.password_problem("short")))
check("mismatch rejected", bool(auth_mod.password_problem("longenough1", "longenough2")))
check("good password accepted", auth_mod.password_problem("longenough1", "longenough1") == "")

print("== sessions ==")
tmp = tempfile.mkdtemp(prefix="mlo-auth-")
auth_mod.db_path = lambda: os.path.join(tmp, "auth.db")
auth_mod._initialized = False
token, expires = auth_mod.create_session(30, label="test")
check("fresh token is valid", auth_mod.valid_session(token))
check("expiry is in the future", expires > time.time() + 29 * 86400)
check("session is counted", auth_mod.session_count() == 1)
auth_mod.revoke_session(token)
check("revoked token is not valid", not auth_mod.valid_session(token))
check("revoke cleared the count", auth_mod.session_count() == 0)
check("a made-up token is not valid", not auth_mod.valid_session("not-a-real-token"))
check("an empty token is not valid", not auth_mod.valid_session(""))

live, _ = auth_mod.create_session(1)
expired, _ = auth_mod.create_session(1)
import sqlite3  # noqa: E402
conn = sqlite3.connect(auth_mod.db_path())
conn.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
             (time.time() - 10, __import__("hashlib").sha256(expired.encode()).hexdigest()))
conn.commit()
conn.close()
check("an expired token is refused", not auth_mod.valid_session(expired))
check("expiry also drops the row", auth_mod.session_count() == 1)
auth_mod.revoke_all()
check("revoke_all signs everything out", auth_mod.session_count() == 0)

print("== backoff ==")
ip = "203.0.113.7"
check("no wait before any failure", auth_mod.retry_after(ip) == 0)
for _ in range(4):
    auth_mod.note_failure(ip)
check("no wait below the threshold", auth_mod.retry_after(ip) == 0)
auth_mod.note_failure(ip)
check("wait after the threshold", auth_mod.retry_after(ip) > 0)
auth_mod.note_success(ip)
check("success clears the wait", auth_mod.retry_after(ip) == 0)

print("== when the gate applies ==")
for mode, host, want in (
    ("auto", "127.0.0.1", False),
    ("auto", "localhost", False),
    ("auto", "::1", False),
    ("auto", "0.0.0.0", True),
    ("auto", "192.168.1.20", True),
    ("required", "127.0.0.1", True),
    ("off", "127.0.0.1", False),
    ("off", "0.0.0.0", True),   # misconfiguration: never publish an open library
    ("off", "10.0.0.5", True),
):
    got = auth_mod.gate_required({"auth_mode": mode, "server_host": host})
    check(f"auth_mode={mode} on {host} -> {want}", got is want)
check("off + LAN warns", bool(auth_mod.gate_warning({"auth_mode": "off", "server_host": "0.0.0.0"})))
check("loopback auto is quiet", auth_mod.gate_warning({"auth_mode": "auto", "server_host": "127.0.0.1"}) == "")

print("== what is public ==")
check("/api/health public", auth_mod.is_public("/api/health"))
check("/api/auth/status public", auth_mod.is_public("/api/auth/status"))
check("/api/auth/login public", auth_mod.is_public("/api/auth/login"))
check("/api/library gated", not auth_mod.is_public("/api/library"))
check("/api/soulseek/import-all gated", not auth_mod.is_public("/api/soulseek/import-all"))
check("the SPA shell is public", auth_mod.is_public("/") and auth_mod.is_public("/assets/x.js"))
# The API's own docs sit OUTSIDE /api, so the shell rule would publish them:
# /openapi.json is a full map of a password-protected server's routes.
check("/docs is gated", not auth_mod.is_public("/docs"))
check("/openapi.json is gated", not auth_mod.is_public("/openapi.json"))
check("/redoc is gated", not auth_mod.is_public("/redoc"))

print("== token transport ==")


class _Req:
    def __init__(self, headers=None, cookies=None, query=None):
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.query_params = query or {}
        self.client = type("C", (), {"host": "127.0.0.1"})()


check("bearer header", auth_mod.token_from_request(
    _Req(headers={"authorization": "Bearer abc"})) == "abc")
check("cookie", auth_mod.token_from_request(_Req(cookies={"mlo_session": "def"})) == "def")
check("query token (websocket/shells)", auth_mod.token_from_request(
    _Req(query={"token": "ghi"})) == "ghi")
check("header wins over cookie", auth_mod.token_from_request(
    _Req(headers={"authorization": "Bearer abc"}, cookies={"mlo_session": "def"})) == "abc")
check("no token at all", auth_mod.token_from_request(_Req()) == "")

print("== events ==")
events_mod._events.clear()
payload = events_mod.emit("wish_found", "Wish found: X", "body", {"wish_id": 1},
                          config={"notify_wish_found": True})
check("emit returns the frame", payload.get("event") == "wish_found" and payload.get("seq"))
check("the frame is in the ring", any(e.get("event") == "wish_found" for e in events_mod.recent(0)))
check("recent(since=now) skips older frames", events_mod.recent(time.time() + 1) == [])
events_mod.emit("wish_found", "Off", config={"notify_wish_found": False})
check("a switched-off kind is not published",
      sum(1 for e in events_mod.recent(0) if e.get("title") == "Off") == 0)
for i in range(events_mod._MAX_EVENTS + 20):
    events_mod.emit("download_done", f"n{i}", config={"notify_download_done": True})
check("the ring stays bounded", len(events_mod.recent(0, limit=10 ** 6)) <= events_mod._MAX_EVENTS)
check("a bad config never silences the event",
      events_mod.emit("download_done", "boom", config=None).get("event") == "download_done")

print("== HTTP: the gate in front of the real app ==")
try:
    from fastapi.testclient import TestClient
    from server import main as main_mod
except Exception as e:  # pragma: no cover — a missing extra is a SKIP, not a failure
    print(f"  SKIP  TestClient unavailable: {e}")
    print(f"\n{len(FAILED)} failure(s)")
    sys.exit(2 if not FAILED else 1)

# Gate ON, over the temp database: same code path a LAN install runs.
auth_mod._initialized = False
_gate = {"required": True, "has_password": True, "username": "",
         "host": "0.0.0.0", "public_url": "", "session_days": 30}
auth_mod.cached_state = lambda: dict(_gate)
auth_mod.current_state = lambda refresh=False: dict(_gate)

import mlo.config as config_mod  # noqa: E402
_real_load = config_mod.load_config
password = "a-very-good-password"
config_mod.load_config = lambda *a, **k: {"auth_password_hash": auth_mod.hash_password(password),
                                         "server_host": "0.0.0.0", "auth_mode": "auto",
                                         "auth_session_days": 30}

# Not a `with` block: the lifespan would start the wishes worker and the
# slskd watcher, neither of which this suite is about.
client = TestClient(main_mod.app)

r = client.get("/api/health")
check("health answers without a session", r.status_code == 200 and r.json()["status"] == "ok")

r = client.get("/api/library")
check("a library read is refused without a session", r.status_code == 401,
      f"got {r.status_code}")
check("the refusal says why", (r.json() or {}).get("needs_login") is True)

r = client.get("/api/config")
check("a config read is refused too", r.status_code == 401)

r = client.post("/api/auth/login", json={"password": "wrong-password-here"})
check("a wrong password is refused", r.status_code == 401)

r = client.post("/api/auth/login", json={"password": password})
check("the right password signs in", r.status_code == 200, r.text[:200])
token = (r.json() or {}).get("token") or ""
check("login returns a token", bool(token))
check("login sets the session cookie", "mlo_session" in r.cookies)

r = client.get("/api/config", headers={"Authorization": f"Bearer {token}"})
check("the bearer token opens a gated route", r.status_code == 200, r.text[:200])
r = client.get("/api/config", cookies={"mlo_session": token})
check("the cookie opens it as well (media tags cannot send headers)", r.status_code == 200)
r = client.get(f"/api/config?token={token}")
check("the query token opens it too (websocket/shell case)", r.status_code == 200)
r = client.get("/api/config", headers={"Authorization": "Bearer forged-token"})
check("a forged token does not", r.status_code == 401)

r = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
check("logout answers", r.status_code == 200)
r = client.get("/api/config", headers={"Authorization": f"Bearer {token}"})
check("the token is dead after logout", r.status_code == 401)

print("== HTTP: the websockets follow the same gate ==")
try:
    from starlette.websockets import WebSocketDisconnect
    # No token: the socket is accepted and then closed with our own 4401, so a
    # client can tell "sign in again" from "the server is down" (a close
    # BEFORE accept would be an HTTP 403 handshake rejection, i.e. code 1006).
    try:
        with client.websocket_connect("/ws/progress") as ws:
            ws.receive_text()
        check("ws/progress without a token is refused", False)
    except WebSocketDisconnect as e:
        check("ws/progress without a token closes 4401", e.code == 4401, str(e.code))
    try:
        with client.websocket_connect("/ws/events") as ws:
            ws.receive_text()
        check("ws/events without a token is refused", False)
    except WebSocketDisconnect as e:
        check("ws/events without a token closes 4401", e.code == 4401, str(e.code))
    live_token, _ = auth_mod.create_session(1, label="ws")
    opened = False
    try:
        with client.websocket_connect(f"/ws/events?token={live_token}") as ws:
            opened = True
            # The replay of an empty ring is nothing, so the first frame after
            # a connect is the keep-alive ping (30s) — a clean accept is the
            # proof we need here, not a frame.
    except WebSocketDisconnect:
        opened = False
    check("a live token opens the event socket", opened)
except ImportError:  # pragma: no cover
    print("  SKIP  starlette websocket client unavailable")

print("== HTTP: no password set yet ==")
config_mod.load_config = lambda *a, **k: {"auth_password_hash": "", "server_host": "0.0.0.0",
                                         "auth_mode": "auto", "auth_session_days": 30}
_gate.update(has_password=False)
r = client.get("/api/library")
check("an unclaimed server says needs_setup, not 'wrong password'",
      r.status_code == 428 and (r.json() or {}).get("needs_setup") is True)
r = client.get("/api/auth/status")
check("status answers while unclaimed", r.status_code == 200)
check("status reports no password", r.json().get("has_password") is False)
r = client.post("/api/auth/login", json={"password": password})
check("login on an unclaimed server asks for setup", r.status_code == 428)

print("== the config endpoint never carries the password hash ==")
config_mod.load_config = lambda *a, **k: {"auth_password_hash": "pbkdf2$1$aa$bb",
                                         "server_host": "0.0.0.0", "auth_mode": "auto",
                                         "auth_session_days": 30, "music_folder": "X"}
check("the live config hides the hash",
      "auth_password_hash" not in client.get("/api/config").json())
check("/api/config/defaults has no real hash",
      not client.get("/api/config/defaults").json().get("auth_password_hash"))

config_mod.load_config = _real_load

print(f"\n{len(FAILED)} failure(s)")
sys.exit(1 if FAILED else 0)
