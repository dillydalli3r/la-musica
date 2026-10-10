#!/usr/bin/env python
"""The browser fetch route (`server/browserfetch.py`) — offline contract.

Runs against a LOCAL stub that speaks the DevTools subset this module uses
(`/json/list`, and `Page.enable` / `Runtime.enable` / `Page.navigate` /
`Runtime.evaluate` over a WebSocket), so the one thing no offline test can
prove — that a real Chrome clears Cloudflare's challenge — is the one thing
this file does not claim. Everything the app controls is proved here:

  * the route is OFF unless the live config SAYS so (`cf_browser_fetch`), so a
    unit test (or any caller with its own cfg) can never start a browser on the
    machine running it;
  * a page is returned only when it is NOT the interstitial — a stub that keeps
    serving "Just a moment…" is a failure, never a page;
  * `cfchallenge.fetch_page` prefers this route over the solver, and still
    falls back to the solver when it is off.

WHY the route exists at all is measured, not assumed — see the module docstring
in `server/browserfetch.py`: FlareSolverr cannot pass either host's challenge,
and RateYourMusic refuses a cleared cookie from every non-browser client tried.
"""
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import browserfetch, cfchallenge  # noqa: E402
from websockets.sync.server import serve  # noqa: E402

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
PAGE = "<html><head><title>Search - Rate Your Music</title></head><body>real</body></html>"


# --------------------------------------------------------------------------- #
# A stub DevTools endpoint: real framing, no browser
# --------------------------------------------------------------------------- #
STATE = {"url": "", "reads": 0, "connections": 0, "evaluates": 0}


def _handler(ws):
    STATE["connections"] += 1
    for raw in ws:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        result = {}
        if method == "Page.navigate":
            STATE["url"] = params.get("url") or ""
            STATE["reads"] = 0
        elif method == "Runtime.evaluate":
            STATE["evaluates"] += 1
            expr = params.get("expression") or ""
            if "readyState" in expr:
                value = "complete"
            else:
                STATE["reads"] += 1
                # "never" keeps serving the interstitial — the challenge this
                # module must NOT mistake for a page. Anything else answers.
                value = CHALLENGE if "never" in STATE["url"] else PAGE
            result = {"result": {"type": "string", "value": value}}
        ws.send(json.dumps({"id": mid, "result": result}))


ws_server = serve(_handler, "127.0.0.1", 0)
ws_port = ws_server.socket.getsockname()[1]
threading.Thread(target=ws_server.serve_forever, daemon=True).start()


def _install_stub():
    """Point the module at the stub instead of a real browser."""
    browserfetch.ensure = lambda timeout=40.0: "http://127.0.0.1:1"
    browserfetch._version = lambda timeout=1.5: {"Browser": "stub"}
    browserfetch._target = lambda: f"ws://127.0.0.1:{ws_port}/"


try:
    ON = {"cf_browser_fetch": True}
    OFF = {"cf_browser_fetch": False}

    # ----------------------------------------------------------------------- #
    # The switch: no cfg, or a cfg that does not carry it, means NO browser
    # ----------------------------------------------------------------------- #
    ok(browserfetch.enabled({}) is False,
       "a hand-built cfg with no key does not start a browser")
    ok(browserfetch.enabled(None) is False,
       "…and neither does cfg=None (it never loads a config of its own)")
    ok(browserfetch.enabled(ON) is True, "cf_browser_fetch=True allows it")
    ok(browserfetch.enabled(OFF) is False, "cf_browser_fetch=False refuses it")
    ok(browserfetch.enabled({"cf_browser_fetch": "off"}) is False,
       "…and the string spellings of off are read as off")
    ok(browserfetch.available({}) is False,
       "and availability follows the switch")

    _exe = browserfetch.browser_exe
    browserfetch.browser_exe = lambda: ""
    ok(browserfetch.available(ON) is False,
       "allowed but no browser installed is still not available")
    browserfetch.browser_exe = lambda: r"C:\fake\chrome.exe"
    ok(browserfetch.available(ON) is True, "allowed + installed is available")
    browserfetch.browser_exe = _exe

    # ----------------------------------------------------------------------- #
    # The fetch itself, against the stub
    # ----------------------------------------------------------------------- #
    _install_stub()
    STATE["evaluates"] = 0
    off = browserfetch.fetch("https://rateyourmusic.com/", {})
    ok(off is None, "the route off answers None")
    ok(STATE["connections"] == 0, "…without touching the browser at all")

    got = browserfetch.fetch("https://rateyourmusic.com/search?x=1", ON, timeout=2.0)
    ok(got is not None and got["html"] == PAGE and got["via"] == "browser",
       f"a cleared page comes back as the page: {got}")
    ok(STATE["evaluates"] > 0, "…by asking the browser for the document")

    never = browserfetch.fetch("https://rateyourmusic.com/never", ON, timeout=1.6)
    ok(never is None,
       "a page that stays on the interstitial is a FAILURE, never a page")

    # A browser that cannot be driven at all is None, not an exception.
    _target = browserfetch._target
    browserfetch._target = lambda: ""
    ok(browserfetch.fetch("https://rateyourmusic.com/", ON, timeout=1.0) is None,
       "no page target -> None, never a crash")
    browserfetch._target = _target

    # ----------------------------------------------------------------------- #
    # cfchallenge prefers it over the solver, and still falls back
    # ----------------------------------------------------------------------- #
    interstitial = CHALLENGE
    _saved = (cfchallenge.solve, cfchallenge.solver_url)
    solver_calls = []

    def _fake_solve(url, cfg=None, timeout=None):
        solver_calls.append(url)
        return {"html": PAGE, "status": 200, "cookies": {}, "user_agent": ""}

    cfchallenge.solve = _fake_solve
    cfchallenge.solver_url = lambda cfg=None: "http://solver:8191"

    _fetch = browserfetch.fetch
    browserfetch.fetch = lambda url, cfg=None, **kw: {"html": PAGE, "status": 200}
    try:
        import httpx

        # `fetch_page`'s fast path is a plain `httpx.get`, imported inside the
        # function — so the stub belongs on the module, not on `cfchallenge`.
        _saved_get = httpx.get

        def _site_get(url, **kw):
            return httpx.Response(403, text=interstitial,
                                  request=httpx.Request("GET", url))

        httpx.get = _site_get
        try:
            via = cfchallenge.fetch_page("https://rateyourmusic.com/x",
                                         {"cf_browser_fetch": True},
                                         host="rateyourmusic.com")
            ok(via.get("via") == "browser" and via.get("html") == PAGE,
               f"the browser route is preferred: {via}")
            ok(solver_calls == [], "…and the solver is not asked at all")

            browserfetch.fetch = lambda url, cfg=None, **kw: None
            via2 = cfchallenge.fetch_page("https://rateyourmusic.com/x",
                                          {"cf_browser_fetch": True},
                                          host="rateyourmusic.com")
            ok(via2.get("via") == "solver",
               f"a browser that cannot answer falls back to the solver: {via2}")
            ok(len(solver_calls) == 1, "…and the solver was asked once")

            solver_calls.clear()
            via3 = cfchallenge.fetch_page("https://rateyourmusic.com/x", {},
                                          host="rateyourmusic.com")
            ok(via3.get("via") == "solver",
               "with the route off the solver is the only answer")
        finally:
            httpx.get = _saved_get
    finally:
        cfchallenge.solve, cfchallenge.solver_url = _saved
        browserfetch.fetch = _fetch

    # ----------------------------------------------------------------------- #
    # Lifecycle: stopping a browser we never started is a no-op, not an error
    # ----------------------------------------------------------------------- #
    browserfetch.stop()
    ok(True, "stop() with nothing running does not raise")
finally:
    try:
        ws_server.shutdown()
    except Exception:
        pass

print(f"browserfetch: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)