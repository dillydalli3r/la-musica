#!/usr/bin/env python
"""Cloudflare layer: challenge detection, the solver contract, cookie reuse.

Runs offline against a LOCAL stub that speaks FlareSolverr's wire contract
(`POST /v1 {"cmd":"request.get","url":...}` -> `{"solution":{"response",
"cookies","userAgent","status"}}`), so the one thing this file cannot prove —
that a real Cloudflare challenge is solved — is the one thing no offline test
can. Everything the app controls is proved here: a challenge is recognised, an
unconfigured/unreachable solver is None (never a crash), a solved page is
returned and its clearance cookies are remembered for the fast path, and a
solved-but-still-challenged answer is a failure, not a page.
"""
import http.server
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import cfchallenge  # noqa: E402  (imports the shared client)

PASS = 0
FAIL = 0


def ok(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAILED: {label}")


CHALLENGE = ("<!DOCTYPE html><html><head><title>Just a moment...</title>"
             "</head><body>checking your browser</body></html>")
PAGE = "<html><head><title>The Bends</title></head><body>real page</body></html>"


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
ok(cfchallenge.is_challenge(CHALLENGE) is True, "the interstitial is detected")
ok(cfchallenge.is_challenge(CHALLENGE, 403) is True, "…at 403")
ok(cfchallenge.is_challenge(PAGE) is False, "a real page is not a challenge")
ok(cfchallenge.is_challenge("") is False, "an empty body is not a challenge")
ok(cfchallenge.is_challenge("<html>enable JavaScript and cookies to continue</html>")
   is True, "the JS-required sentence is a challenge")


# --------------------------------------------------------------------------- #
# A local FlareSolverr-shaped stub, and a stub site behind it
# --------------------------------------------------------------------------- #
class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            req = {}
        url = str(req.get("url") or "")
        if self.path != "/v1" or req.get("cmd") != "request.get":
            return self._json({"status": "error", "message": "bad request"}, 400)
        if "refuse" in url:
            # A solver that answered but still got the challenge.
            return self._json({"status": "ok", "solution": {
                "response": CHALLENGE, "status": 403, "cookies": [],
                "userAgent": "stub/1.0"}})
        if "error" in url:
            return self._json({"status": "error", "message": "solver failed"})
        return self._json({"status": "ok", "solution": {
            "response": PAGE, "status": 200,
            "userAgent": "stub/1.0",
            "cookies": [{"name": "cf_clearance", "value": "cleared-token"},
                        {"name": "__cf_bm", "value": "bm"}]}})

    def do_GET(self):
        # The site itself: challenge without the clearance cookie, the page with.
        if "cf_clearance=cleared-token" in (self.headers.get("Cookie") or ""):
            body = PAGE.encode("utf-8")
            self.send_response(200)
        else:
            body = CHALLENGE.encode("utf-8")
            self.send_response(403)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"
site = f"{base}/release/album/radiohead/the-bends/"

try:
    # ----------------------------------------------------------------------- #
    # The solver contract
    # ----------------------------------------------------------------------- #
    cfchallenge.clear()
    cfg = {"cf_solver_url": base}
    got = cfchallenge.solve(site, cfg)
    ok(got is not None and got["html"] == PAGE, "the solver returns the page")
    ok(got["cookies"].get("cf_clearance") == "cleared-token",
       "…and its clearance cookie comes back")
    ok(cfchallenge.solve(site, {}) is None, "no solver configured -> None")
    bad = cfchallenge.solve("http://127.0.0.1:1/nothing", {"cf_solver_url": "http://127.0.0.1:1"})
    ok(bad is None, "an unreachable solver is None, not a crash")
    refused = cfchallenge.solve(f"{base}/refuse", cfg)
    ok(refused is not None and refused["html"] == CHALLENGE,
       "a solver that still got a challenge returns its body")

    # ----------------------------------------------------------------------- #
    # fetch_page: fast path, solver path, and cookie reuse
    # ----------------------------------------------------------------------- #
    # 1. A page answered with a clearance cookie never touches the solver.
    cfchallenge.clear()
    direct = cfchallenge.fetch_page(site, cfg, host="127.0.0.1",
                                    cookies={"cf_clearance": "cleared-token"})
    ok(direct["ok"] and direct["via"] == "http",
       f"a cleared request is a plain GET: {direct}")
    # The stub GET challenges without the cookie, so a fetch with none must go
    # through the solver, remember the cookies, and the SECOND fetch must take
    # the fast path with them.
    cfchallenge.clear()
    first = cfchallenge.fetch_page(site, cfg, host="albumoftheyear.org")
    ok(first["ok"] and first["via"] == "solver", f"solve on challenge: {first}")
    ok(cfchallenge.solved_cookies("albumoftheyear.org").get("cf_clearance")
       == "cleared-token", "the solved cookie is remembered per host")
    second = cfchallenge.fetch_page(site, cfg, host="albumoftheyear.org")
    ok(second["ok"] and second["via"] == "http",
       f"the next fetch reuses the clearance (fast path): {second}")

    # 2. No solver + a challenge = ok False, challenge True (never "no data").
    cfchallenge.clear()
    refused = cfchallenge.fetch_page(f"{base}/x", {}, host="elsewhere")
    ok(refused["ok"] is False and refused["challenge"] is True,
       f"a challenge with no solver is reported as one: {refused}")

    # 3. A cleared page is returned untouched, with no solver configured.
    cfchallenge.clear()
    clean = cfchallenge.fetch_page(f"{base}/plain", {}, host="127.0.0.1",
                                   cookies={"cf_clearance": "cleared-token"},
                                   solve_on_challenge=False)
    ok(clean["ok"] is True and clean["via"] == "http" and clean["html"] == PAGE,
       f"a plain page is read without a solver: {clean}")
finally:
    server.shutdown()
    cfchallenge.clear()

print(f"cfchallenge: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
