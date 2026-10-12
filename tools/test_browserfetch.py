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
from server import browsercookies  # noqa: E402
REAL_BROWSER_SESSION = browserfetch._browser_session
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
# Chrome's own network-error document, byte-for-byte from the copy a library's
# `rym_cache` was found holding (see `browserfetch.is_error_page`): a 185 KB
# "page" with the site's markup nowhere in it.
ERROR_PAGE = ('<html dir="ltr" lang="en"><head>'
              "<style>/* Copyright 2017 The Chromium Authors\n"
              " * Use of this source code is governed by a BSD-style license"
              " that can be found in the LICENSE file. */\n"
              "body { --error-code-color: var(--google-gray-700); }</style>"
              '<title>rateyourmusic.com</title></head>'
              '<body class="neterror" style="font-family: \'Segoe UI\'">'
              '<div id="content"><div id="main-frame-error" '
              'class="interstitial-wrapper"></div></div></body></html>')


# --------------------------------------------------------------------------- #
# A stub DevTools endpoint: real framing, no browser
# --------------------------------------------------------------------------- #
STATE = {"url": "", "reads": 0, "connections": 0, "evaluates": 0}
# What the app SET on the browser (`Network.setCookie`), in order — the session
# it hands the browser before navigating.
SET_COOKIES = []
DELETED_COOKIES = []


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
        elif method == "Network.setCookie":
            SET_COOKIES.append(params)
        elif method == "Network.deleteCookies":
            DELETED_COOKIES.append(params)
        elif method == "Network.getCookies":
            # What a real browser holds after it fetched a page: the clearance
            # it earned plus the session the site set. `fetch` is expected to
            # hand these to `cfchallenge` (see `_publish_clearance`).
            result = {"cookies": [{"name": "cf_clearance", "value": "won"},
                                  {"name": "ulv", "value": "session-1"}]}
        elif method == "Runtime.evaluate":
            STATE["evaluates"] += 1
            expr = params.get("expression") or ""
            if "readyState" in expr:
                value = "complete"
            else:
                STATE["reads"] += 1
                # "never" keeps serving the interstitial — the challenge this
                # module must NOT mistake for a page. "dead" serves Chrome's
                # OWN error document, which is not a page either. Anything
                # else answers.
                url = STATE["url"]
                value = (CHALLENGE if "never" in url
                         else ERROR_PAGE if "dead" in url else PAGE)
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
    # The session fallback reads THIS machine's own browsers through yt-dlp
    # (see `browserfetch._browser_session`); a suite must never do that — the
    # case below patches it again to exercise the fallback itself.
    browserfetch._browser_session = lambda host: ""
    browserfetch._BROWSER_SESSION.clear()


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
    # The clearance the browser won is published for the plain fast path.
    ok(cfchallenge.solved_cookies("rateyourmusic.com").get("cf_clearance") == "won",
       "a fetched page hands the browser's cookies to cfchallenge")

    # The session the user STORED goes into the browser before it navigates —
    # and Cloudflare's OWN cookies are dropped on the way in (a clearance is
    # bound to the browser+UA that earned it, so it can never validate here).
    # The site's own signed-in tokens travel, RYM's `sec_*` rotation included:
    # MEASURED 2026-10-11, dropping those fails the release page.
    SET_COOKIES.clear()
    DELETED_COOKIES.clear()
    cfchallenge.clear()
    browserfetch.fetch("https://rateyourmusic.com/search?x=1",
                       dict(ON, rym_cookie="ulv=s1; cf_clearance=stale; __cf_bm=x; sec_id=jar1"),
                       timeout=2.0)
    ok([c["name"] for c in SET_COOKIES] == ["ulv", "sec_id"],
       f"Cloudflare's cookies are dropped, the site's own session travels: {SET_COOKIES}")
    ok([c["name"] for c in DELETED_COOKIES] == ["ulv", "sec_id"],
       f"the borrowed session is removed from Chrome after the fetch: {DELETED_COOKIES}")

    ok(browserfetch._session_cookie({"rym_cookie": "a=1"},
                                    "https://rateyourmusic.com/x") == "a=1",
       "the stored RYM paste is what a RYM URL is given")
    ok(browserfetch._session_cookie({"aoty_cookie": "b=2"},
                                    "https://www.albumoftheyear.org/x") == "b=2",
       "…and the AOTY paste for an AOTY URL")
    ok(browserfetch._session_cookie({"rym_cookie": "a=1"},
                                    "https://example.com/x") == "",
       "a host this app keeps no session for is given none")

    # Exercise the actual browser-profile reader without reading THIS machine's
    # browser: yt-dlp's result and the installed-profile list are deterministic
    # fakes, but host filtering, pair conversion and the 45-minute cache are the
    # production helper (`_browser_session`) itself.
    _installed, _jar = browsercookies.installed, browsercookies.jar
    _jar_reads = []
    _netscape = (
        "# Netscape HTTP Cookie File\n"
        ".rateyourmusic.com\tTRUE\t/\tTRUE\t0\tusername\talice\n"
        "rateyourmusic.com\tFALSE\t/\tTRUE\t0\tis_logged_in\t1\n"
        ".albumoftheyear.org\tTRUE\t/\tTRUE\t0\trememberMe\tseen\n")
    browsercookies.installed = lambda: {"firefox": 1}
    browsercookies.jar = lambda browser: (_jar_reads.append(browser) or _netscape)
    browserfetch._BROWSER_SESSION.clear()
    try:
        _rym = REAL_BROWSER_SESSION("rateyourmusic.com")
        ok(_rym == "username=alice; is_logged_in=1",
           f"the borrowed RYM session contains only RYM cookies: {_rym!r}")
        ok(REAL_BROWSER_SESSION("rateyourmusic.com") == _rym and len(_jar_reads) == 1,
           "the filtered browser jar is reused within its 45-minute cache")
        _aoty = REAL_BROWSER_SESSION("albumoftheyear.org")
        ok(_aoty == "rememberMe=seen" and "username" not in _aoty,
           f"the borrowed AOTY session cannot contain RYM cookies: {_aoty!r}")
    finally:
        browsercookies.installed, browsercookies.jar = _installed, _jar
        browserfetch._BROWSER_SESSION.clear()

    # The real helper takes a browser-export-shaped jar, filters to the
    # requested host, caches only the filtered cookie string, and never reads
    # the machine's actual profile in a suite (the functions are patched here).
    _real_installed, _real_jar = browsercookies.installed, browsercookies.jar
    _calls = []
    _fake_jar = (
        "# Netscape HTTP Cookie File\n"
        ".rateyourmusic.com\tTRUE\t/\tTRUE\t0\tusername\talice\n"
        "rateyourmusic.com\tFALSE\t/\tTRUE\t0\tis_logged_in\t1\n"
        ".albumoftheyear.org\tTRUE\t/\tTRUE\t0\trememberMe\tseen\n")
    browsercookies.installed = lambda: {"firefox": 1}
    browsercookies.jar = lambda browser: (_calls.append(browser) or _fake_jar)
    browserfetch._BROWSER_SESSION.clear()
    try:
        _rym = REAL_BROWSER_SESSION("rateyourmusic.com")
        ok(_rym == "username=alice; is_logged_in=1",
           f"borrowed RYM cookies are filtered to that host: {_rym!r}")
        ok(REAL_BROWSER_SESSION("rateyourmusic.com") == _rym and len(_calls) == 1,
           "the browser jar is cached, not read again for the same host")
        _aoty = REAL_BROWSER_SESSION("albumoftheyear.org")
        ok(_aoty == "rememberMe=seen" and "username" not in _aoty,
           f"the AOTY fallback cannot receive RYM cookies: {_aoty!r}")
    finally:
        browsercookies.installed, browsercookies.jar = _real_installed, _real_jar
        browserfetch._BROWSER_SESSION.clear()

    # Nothing stored: the session the user's own browser is signed in with is
    # used (patched here — a suite must never read the cookies of the machine
    # running it), and it is remembered so the read happens once.
    _real_session = browserfetch._browser_session
    _reads = []
    browserfetch._BROWSER_SESSION.clear()
    browserfetch._browser_session = lambda host: (_reads.append(host) or "ulv=frombrowser")
    try:
        ok(browserfetch._session_cookie({}, "https://rateyourmusic.com/x")
           == "ulv=frombrowser",
           "with nothing stored, the browser's own session is borrowed")
        ok(browserfetch._session_cookie({"rym_cookie": "stored=1"},
                                        "https://rateyourmusic.com/x") == "stored=1",
           "…while a stored paste still wins")
    finally:
        browserfetch._browser_session = _real_session
        browserfetch._BROWSER_SESSION.clear()

    never = browserfetch.fetch("https://rateyourmusic.com/never", ON, timeout=1.6)
    ok(never is None,
       "a page that stays on the interstitial is a FAILURE, never a page")

    # Chrome's own error document is not a page either — MEASURED 2026-10-10,
    # RYM's 503 to an anonymous client renders exactly this, and one that got
    # cached read as "the album page with no genres" for 30 days.
    dead = browserfetch.fetch("https://rateyourmusic.com/dead", ON, timeout=1.6)
    ok(dead is None, "the browser's OWN error page is a FAILURE, never a page")
    ok(browserfetch.is_error_page(ERROR_PAGE)
       and not browserfetch.is_error_page(PAGE)
       and not browserfetch.is_error_page(CHALLENGE)
       and not browserfetch.is_error_page(""),
       "and the predicate that says so is exact")

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
    # The invisible desktop (Windows): created once, reused, given back on stop
    # ----------------------------------------------------------------------- #
    _name, _handle = browserfetch._hidden_desktop()
    if os.name == "nt":
        ok(bool(_name) and bool(_handle), f"a hidden desktop is created ({_name!r})")
        ok(browserfetch._hidden_desktop() == (_name, _handle),
           "…the same one is reused, never a second")
        browserfetch._close_desktop()
        ok(browserfetch._hidden_desktop()[0] == _name,
           "…and it is made again after a stop")
        browserfetch._close_desktop()
    else:
        ok((_name, _handle) == ("", 0),
           "off Windows there is no desktop to hide on (headless is the fallback)")

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