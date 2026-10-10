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
import time

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
    busy_hits = 0               # GETs to /busy, to count the transient retries

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
        if self.path.startswith("/busy"):
            # A site that is merely BUSY, not challenging: a 429 with a body
            # that is not the interstitial. Never a page, always retried.
            type(self).busy_hits += 1
            body = b"busy"
            self.send_response(429)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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
    ok("did not answer" in cfchallenge.host_failed("127.0.0.1"),
       f"…and that is remembered per host: {cfchallenge.host_failed('127.0.0.1')!r}")
    # Both stubs are on 127.0.0.1 (only the PORT differs), so the failure above
    # latched the same host the working stub uses. Unlike production, where a
    # dead solver and a live one are different hosts, this suite has to say
    # which one it means.
    cfchallenge.clear_failures()
    refused = cfchallenge.solve(f"{base}/refuse", cfg)
    ok(refused is None, "a solver that ended ON the challenge is a failure, "
                        "not a page")
    ok("challenge page" in cfchallenge.host_failed("127.0.0.1")
       or "solver failed" in cfchallenge.host_failed("127.0.0.1"),
       f"…and it is recorded as such: {cfchallenge.host_failed('127.0.0.1')!r}")

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

    # 4. A 429/5xx that is NOT a challenge is transient, never a page: it is
    # retried (FETCH_RETRIES) and, still busy, reported as a failure — so a
    # caller reports a refusal instead of parsing it to None as "no data".
    cfchallenge.clear()
    _Handler.busy_hits = 0
    _real_interval = cfchallenge.FETCH_MIN_INTERVAL
    cfchallenge.FETCH_MIN_INTERVAL = 0.0    # the pace is proved in the next test
    try:
        busy = cfchallenge.fetch_page(f"{base}/busy", {}, host="busy.example",
                                      solve_on_challenge=False)
    finally:
        cfchallenge.FETCH_MIN_INTERVAL = _real_interval
    ok(busy["ok"] is False and busy["status"] == 429
       and busy["challenge"] is False and busy["html"] == ""
       and busy["via"] == "none",
       f"an exhausted 429 is a failure, not a page: {busy}")
    ok(_Handler.busy_hits == cfchallenge.FETCH_RETRIES + 1,
       f"a busy host is tried {cfchallenge.FETCH_RETRIES} times more: "
       f"{_Handler.busy_hits}")

    # Retry-After is read as delta-seconds OR an HTTP-date; nonsense is ignored
    # (the per-host pace still spaces the retry).
    class _Resp:
        def __init__(self, value):
            self.headers = {"Retry-After": value}

    ok(cfchallenge._retry_after(_Resp("2")) == 2.0, "Retry-After delta-seconds")
    ok(cfchallenge._retry_after(_Resp("Wed, 21 Oct 2099 07:28:00 GMT")) > 0,
       "Retry-After HTTP-date")
    ok(cfchallenge._retry_after(_Resp("nonsense")) == 0.0,
       "a nonsense Retry-After is not honoured")

    # ----------------------------------------------------------------------- #
    # Pacing lives in fetch_page, not the caller: consecutive GETs to ONE host
    # are >= FETCH_MIN_INTERVAL apart, and two hosts never stall each other. A
    # fake clock makes the sleeps measurable with no real waiting.
    # ----------------------------------------------------------------------- #
    class _Clock:
        def __init__(self):
            self.t = 1000.0
            self.slept = 0.0

        def time(self):
            return self.t

        def sleep(self, seconds):
            self.slept += seconds
            self.t += seconds

    _real_time = cfchallenge.time
    cfchallenge._FETCH_GATES.clear()
    clock = _Clock()
    cfchallenge.time = clock
    try:
        for _ in range(4):
            cfchallenge.fetch_page(f"{base}/plain", {}, host="pace-a.example",
                                   solve_on_challenge=False)
        ok(clock.slept >= 3 * cfchallenge.FETCH_MIN_INTERVAL,
           f"4 fetches to one host sleep >= 3 intervals: {clock.slept}")
        before = clock.slept
        cfchallenge.fetch_page(f"{base}/plain", {}, host="pace-b.example",
                               solve_on_challenge=False)
        ok(clock.slept == before,
           f"a different host is not stalled by the first: {clock.slept - before}")
    finally:
        cfchallenge.time = _real_time
        cfchallenge._FETCH_GATES.clear()

    # ----------------------------------------------------------------------- #
    # A solve the solver LOST is remembered per host — the fix for the import
    # that spent minutes asking again (measured live, 2026-10-10: FlareSolverr
    # cannot pass rateyourmusic.com's challenge from the owner's network, and
    # the genre chain used to pay its 60 s once per album).
    # ----------------------------------------------------------------------- #
    cfchallenge.clear()
    cfg = {"cf_solver_url": base}
    failed = cfchallenge.solve(f"{base}/error", cfg)
    ok(failed is None, "a solver error is None")
    note = cfchallenge.host_failed("127.0.0.1")
    ok("solver failed" in note, f"…and its own words are kept: {note!r}")
    ok(cfchallenge.host_failed("") == "" and
       cfchallenge.host_failed("elsewhere.example") == "",
       "another host is not blamed for it")
    # The latch is what makes the second lookup cheap: no request goes out.
    ok(cfchallenge.solve(site, cfg) is None,
       "a latched host is not asked again (no second solve)")
    ok(cfchallenge.solve(f"{base}/error", cfg) is None,
       "…even for the URL that failed")
    # A user asking again (Test, Start, a credential change) clears it.
    cfchallenge.clear_failures("127.0.0.1")
    ok(cfchallenge.host_failed("127.0.0.1") == "",
       "clearing one host forgets it")
    cfchallenge.clear()
    got = cfchallenge.solve(site, cfg)
    ok(got is not None and got["html"] == PAGE,
       "after clearing, the solver is asked again and answers")

    # A host the solver DID pass is never latched.
    ok(cfchallenge.host_failed("127.0.0.1") == "",
       "a successful solve leaves no failure behind")

    # The lazy start: with no solver configured, a hand-built cfg must NOT try
    # to bring one up (that is what keeps this suite hermetic), while a real
    # config may — and the attempt is rate-limited, never per request.
    cfchallenge.clear()
    asked = {"n": 0}

    class _FakeSolver:
        @staticmethod
        def ensure_started(cfg=None):
            asked["n"] += 1
            return False, "not installed"

    import server.solver as _solver_mod
    _real_ensure = _solver_mod.ensure_started
    # A solver answering on the default port would short-circuit `ensure_solver`
    # before it ever reaches the manager — true on a machine that runs one, and
    # nothing to do with this rule. The resolution itself is proved in
    # tools/test_solver.py; here it is pinned to "nothing answers".
    _real_url = cfchallenge.solver_url
    cfchallenge.solver_url = lambda cfg=None: ""
    _solver_mod.ensure_started = _FakeSolver.ensure_started
    try:
        ok(cfchallenge.ensure_solver({}) is False and asked["n"] == 0,
           "a hand-built cfg never spawns a solver")
        ok(cfchallenge.ensure_solver({"cf_solver_url": "", "cf_solver_autostart": True})
           is False and asked["n"] == 1,
           "a real config asks the manager once")
        cfchallenge.ensure_solver({"cf_solver_url": "", "cf_solver_autostart": True})
        ok(asked["n"] == 1, "…and the retry is rate-limited, not per request")
    finally:
        _solver_mod.ensure_started = _real_ensure
        cfchallenge.solver_url = _real_url

    # ----------------------------------------------------------------------- #
    # The boot warm-up: one request per host, in the background, so the first
    # genre import is not the thing that waits ~60 s per host to learn what the
    # solver can pass. (Measured live: 135 s of warm-up, then a 0.7 s import.)
    # ----------------------------------------------------------------------- #
    cfchallenge.clear()
    warmed = []

    def _warm_solve(url, cfg=None, timeout=None):
        warmed.append(url)
        return None                     # as a real failure does; the latch is set by solve()

    _real_solve = cfchallenge.solve
    cfchallenge.solve = _warm_solve
    try:
        cfchallenge.warm_solver({"cf_solver_url": base})
        for _ in range(50):             # the warm-up is a thread
            if len(warmed) >= len(cfchallenge.WARM_URLS):
                break
            time.sleep(0.1)
        ok(warmed == [u for _h, _k, u in cfchallenge.WARM_URLS],
           f"the warm-up probes every scraped host once: {warmed}")
        # A host already settled is not probed again (the latch is the point).
        warmed.clear()
        cfchallenge.record_failure("rateyourmusic.com", "already lost")
        cfchallenge.warm_solver({"cf_solver_url": base})
        for _ in range(30):
            if warmed:
                break
            time.sleep(0.1)
        ok(warmed == ["https://www.albumoftheyear.org/"],
           f"a host already latched is skipped: {warmed}")
        # …and a CLEARED COOKIE makes the probe pointless: that source answers
        # without the solver at all.
        cfchallenge.clear_failures()
        warmed.clear()
        cfchallenge.warm_solver({"cf_solver_url": base,
                                 "rym_cookie": "cf_clearance=x",
                                 "aoty_cookie": "cf_clearance=y"})
        time.sleep(0.6)
        ok(warmed == [], f"a source with a cleared cookie is not probed: {warmed}")
    finally:
        cfchallenge.solve = _real_solve
        import server.solver as _solver_mod2
        cfchallenge.clear()
        ok(_solver_mod2.blocked_hosts() == [],
           "the card's blocked list is empty once the latch is cleared")

    # What the card SHOWS when the latch is set: the host, its label and the
    # solver's own words (the whole point of `solver.blocked_hosts`).
    cfchallenge.clear()
    cfchallenge.record_failure("albumoftheyear.org",
                               "Error solving the challenge. Timeout after 60.0 seconds.")
    import server.solver as _solver_mod3
    blocked = _solver_mod3.blocked_hosts()
    ok([b["host"] for b in blocked] == ["albumoftheyear.org"], blocked)
    ok(blocked[0]["label"] == "Album of the Year"
       and "Timeout after 60.0 seconds." in blocked[0]["message"], blocked)
    cfchallenge.clear()
    ok(_solver_mod3.blocked_hosts() == [],
       "…and it goes away with the latch")
finally:
    server.shutdown()
    cfchallenge.clear()

print(f"cfchallenge: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
