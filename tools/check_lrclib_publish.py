#!/usr/bin/env python3
"""End-to-end check of the LRCLIB submission path against a stub that enforces
LRCLIB's OWN contract (docs: POST /api/request-challenge → proof of work →
POST /api/publish with the metadata in the JSON body and a single-use
`X-Publish-Token` header). The stub verifies the token exactly as LRCLIB's
solver does (sha256(prefix+nonce) <= target), so a passing run proves the app
satisfies the documented rule without writing anything to the public database.

  node/python: this file runs both halves — the stub and the app server it
  points at (LRCLIB_BASE patched) — then calls POST /api/lyrics/publish.

Run:  python tools/check_lrclib_publish.py
"""
import hashlib
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

APP_PORT = int(os.environ.get("MLO_CHECK_PORT", "8012"))
STUB_PORT = int(os.environ.get("MLO_STUB_PORT", "8099"))
MUSIC = os.environ.get("MLO_MUSIC_FOLDER", "F:/tmp/mlo-cols")

FAILS = []


def ok(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


# ---- the stub: LRCLIB's contract, enforced ---------------------------------
TARGET = "000000FF" + "00" * 28          # LRCLIB's current shape: 3 zero bytes
CHALLENGE_PREFIX = "stub-prefix-0123456789"
SEEN = []
TOKENS = set()


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, status, doc):
        body = json.dumps(doc).encode()
        # One request per connection: keep-alive on a stub this small only
        # makes the next caller wait for a socket nobody will reuse.
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        SEEN.append({"path": self.path, "headers": dict(self.headers), "body": raw.decode("utf-8", "replace")})
        if self.path.endswith("/request-challenge"):
            TOKENS.add(CHALLENGE_PREFIX)
            return self._json(200, {"prefix": CHALLENGE_PREFIX, "target": TARGET})
        if self.path.endswith("/publish"):
            token = self.headers.get("X-Publish-Token") or ""
            prefix, _, nonce = token.partition(":")
            doc = {}
            try:
                doc = json.loads(raw.decode("utf-8"))
            except Exception:
                pass
            # LRCLIB's own acceptance rule, in its own order: the token first,
            # then the body's required fields.
            if (prefix not in TOKENS or not nonce.isdigit()
                    or hashlib.sha256((prefix + nonce).encode()).digest()[:4]
                    > bytes.fromhex(TARGET)[:4]):
                return self._json(400, {"code": 400, "name": "IncorrectPublishTokenError",
                                        "message": "The provided publish token is incorrect"})
            TOKENS.discard(prefix)           # single use
            missing = [k for k in ("trackName", "artistName", "albumName", "duration")
                       if not doc.get(k)]
            if missing:
                return self._json(422, {"code": 422, "name": "UnprocessableEntity",
                                        "message": f"missing {missing}"})
            return self._json(201, {"id": 1, "trackName": doc["trackName"]})
        return self._json(404, {"code": 404, "name": "TrackNotFound", "message": "no"})

    def do_GET(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        SEEN.append({"path": self.path, "headers": dict(self.headers), "body": ""})
        # The existence pre-check: this recording is not on LRCLIB.
        return self._json(404, {"code": 404, "name": "TrackNotFound",
                                "message": "Failed to find specified track"})


def main():
    stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    print(f"== LRCLIB stub on {STUB_PORT} ==")

    # The app server, with the LRCLIB base URL pointed at the stub. Both
    # clients move: the engine's (mlo.lyrics_providers) and the integration's
    # own GET client (server.integrations, httpx).
    import mlo.lyrics_providers as lp
    lp.LRCLIB_BASE = f"http://127.0.0.1:{STUB_PORT}/api"
    import server.integrations as intg
    intg.LRCLIB_BASE = f"http://127.0.0.1:{STUB_PORT}/api"
    import uvicorn
    from server import main as app_main

    cfg = {"music_folder": MUSIC, "first_run_done": True}
    threading.Thread(
        target=lambda: uvicorn.run(app_main.app, host="127.0.0.1", port=APP_PORT, log_level="warning"),
        daemon=True).start()

    base = f"http://127.0.0.1:{APP_PORT}"
    for _ in range(120):
        try:
            urllib.request.urlopen(base + "/api/config", timeout=1)
            break
        except Exception:
            import time
            time.sleep(0.5)
    req = urllib.request.Request(base + "/api/config", data=json.dumps(cfg).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)

    body = {
        "artist": "la musica check", "track": "Instrumental Company",
        "album": "Check Album", "duration": 187,
        "synced": "[00:01.00]one\n[00:03.00]two",
        "plain": "one\ntwo",
    }
    req = urllib.request.Request(base + "/api/lyrics/publish", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    import time
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        reply = json.load(r)
    print(f"  publish answered in {time.time() - t0:.1f} s: {reply}")
    ok(reply.get("ok") is True, f"the app reports a published submission ({reply})")

    publish = [s for s in SEEN if s["path"].endswith("/publish")]
    ok(len(publish) == 1, f"exactly one submission reached the stub ({len(publish)})")
    if publish:
        p = publish[0]
        sent = json.loads(p["body"])
        ok(sent.get("trackName") == "Instrumental Company"
           and sent.get("artistName") == "la musica check"
           and sent.get("albumName") == "Check Album"
           and sent.get("duration") == 187
           and sent.get("syncedLyrics") == "[00:01.00]one\n[00:03.00]two"
           and sent.get("plainLyrics") == "one\ntwo",
           f"the metadata and both texts are in the body ({sent})")
        tok = p["headers"].get("X-Publish-Token", "")
        pre, _, nonce = tok.partition(":")
        ok(pre == CHALLENGE_PREFIX and nonce.isdigit()
           and hashlib.sha256((pre + nonce).encode()).digest()[:4] <= bytes.fromhex(TARGET)[:4],
           f"the token is the solved challenge, and the stub verified it ({tok[:24]}…)")
        ok(p["headers"].get("User-Agent", "").startswith("la musica v"),
           f"the User-Agent names the app and its version ({p['headers'].get('User-Agent')})")
    ok(any(s["path"].endswith("/request-challenge") for s in SEEN),
       "the submission asked for a challenge first")

    # A synced text submitted alone still goes up with its plain half: LRCLIB
    # has refused a synced-only body before, so the endpoint derives it.
    SEEN.clear()
    body = {"artist": "la musica check", "track": "Synced Only", "album": "Check Album",
            "duration": 100, "synced": "[00:02.00]alpha\n[00:04.00]beta"}
    req = urllib.request.Request(base + "/api/lyrics/publish", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        reply = json.load(r)
    ok(reply.get("ok") is True, f"a synced-only submission is published ({reply})")
    sent = json.loads([s for s in SEEN if s["path"].endswith("/publish")][0]["body"])
    ok(sent.get("plainLyrics") == "alpha\nbeta" and sent.get("syncedLyrics") == "[00:02.00]alpha\n[00:04.00]beta",
       f"the plain half is derived from the stamps ({sent.get('plainLyrics')!r})")

    print("\n" + ("all LRCLIB publish checks passed" if not FAILS else f"FAILED: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
