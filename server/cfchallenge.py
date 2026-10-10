"""Live pages through Cloudflare, for the two scraped sources (RYM, AOTY).

Both sites answer a plain client with Cloudflare's "Just a moment…"
interstitial. MEASURED, 2026-10-10, from this machine, against both hosts:
plain HTTP, `curl_cffi` impersonating Chrome, headless Playwright/Chromium,
and a real headed Chrome driven by `nodriver` ALL received the 403 challenge —
so a live page needs one of the two things a challenge cannot refuse:

  * a CLEARED SESSION — the `cf_clearance` cookie (and the matching
    `rym_user_agent`) of a browser that already passed the challenge. The user
    pastes it; it is sent as a normal cookie, and the request is then a plain
    fast HTTP GET (see `server.integrations._rym_get` for RYM and
    `aoty_page` for AOTY).
  * a SOLVER — a FlareSolverr-compatible service (`cf_solver_url`), a real
    browser it keeps warm, which solves the challenge once per host and hands
    back the page AND the cookies that did it. Those cookies are then reused
    for the plain-HTTP fast path until they expire, so one solve buys many
    fast requests.

Nothing here pretends the archive is live: the Wayback route was removed from
both scrapers on the owner's instruction ("I want live info from each site").

The one entry point the scrapers use is `fetch_page`: try the fast HTTP path
with whatever clearance cookies are in hand, and only when Cloudflare answers
with a challenge does the solver get asked. A site that is not behind a
challenge (MusicBrainz, or an already-cleared RYM) never pays for the solver.
"""
from __future__ import annotations

import json
import os
import re
import time

# The marker every one of these interstitials carries. Deliberately broad — a
# page that merely MENTIONS "checking your browser" in prose is not a thing
# RYM or AOTY publishes — and matched case-insensitively against the first few
# KB of the body, where Cloudflare puts it.
CHALLENGE_RE = re.compile(
    r"just a moment|cf-?challenge|_cf_chl|checking your browser|"
    r"enable javascript and cookies|attention required", re.I)

# How much of a body to scan for the marker. The interstitial's title is in the
# first lines; a challenge is never found past this.
CHALLENGE_SCAN = 4000

# FlareSolverr's own default request budget (ms); a solve spins a real browser
# and a hard challenge can take the better part of a minute.
DEFAULT_SOLVER_TIMEOUT_MS = 60000

# Solved cookies are reused for the fast path for this long. Cloudflare's
# clearance lives ~30 minutes; reusing for less keeps a stale cookie from being
# what a request fails on, and reusing for more would pay a solve per album.
COOKIE_TTL = 20 * 60.0

# In-memory, per host: {"cookies": {name: value}, "user_agent": str, "at": ts}.
_SOLVED = {}


def is_challenge(text, status=None):
    """Whether *text* is Cloudflare's interstitial rather than the page asked for.

    A challenge is a challenge regardless of status: Cloudflare serves it with
    403 to most clients and 503 to others, and a cleared-but-expired cookie can
    draw a 200 whose body is still the interstitial. The status is accepted so
    a caller that already knows it is not a page can ask without re-reading the
    body, but the body is what decides.
    """
    if status is not None and status == 200 and not text:
        return False
    return bool(text and CHALLENGE_RE.search(text[:CHALLENGE_SCAN]))


def solver_url(cfg=None):
    """The configured FlareSolverr-compatible endpoint, or ""."""
    try:
        if cfg is None:
            from mlo.config import load_config
            cfg = load_config()
    except Exception:
        return ""
    return str((cfg or {}).get("cf_solver_url") or "").strip().rstrip("/")


def solver_timeout(cfg=None):
    """The solver's own per-request budget, in seconds (FlareSolverr ms → s)."""
    try:
        raw = (cfg or {}).get("cf_solver_timeout")
        ms = int(float(raw)) if raw not in (None, "") else DEFAULT_SOLVER_TIMEOUT_MS
    except (TypeError, ValueError):
        ms = DEFAULT_SOLVER_TIMEOUT_MS
    return max(5.0, min(180.0, ms / 1000.0))


def _remember(host, cookies, user_agent=""):
    if cookies:
        _SOLVED[str(host)] = {"cookies": dict(cookies),
                              "user_agent": str(user_agent or ""),
                              "at": time.time()}


def solved_cookies(host):
    """{'name': 'value'} solved for *host* and still fresh, or {}."""
    entry = _SOLVED.get(str(host))
    if not entry or time.time() - entry.get("at", 0) > COOKIE_TTL:
        return {}
    return dict(entry.get("cookies") or {})


def solved_user_agent(host):
    entry = _SOLVED.get(str(host))
    if not entry or time.time() - entry.get("at", 0) > COOKIE_TTL:
        return ""
    return str(entry.get("user_agent") or "")


def clear():
    """Forget every solved cookie (tests, and a credential change in Settings)."""
    _SOLVED.clear()


def solve(url, cfg=None, timeout=None):
    """Ask the solver for *url*: ``{"html", "status", "cookies", "user_agent"}``
    or None.

    The wire contract is FlareSolverr's (`POST {base}/v1`, ``cmd:
    request.get``); any service that speaks it works — FlareSolverr itself, or
    a thin wrapper. `html` is the page the browser ended on (None when the
    solver answered but returned nothing useful), `cookies` the browser's jar
    as a plain ``{name: value}`` map (kept in memory for the fast path), and
    None means the solver is unconfigured, unreachable, or refused — the caller
    then reports the challenge, never "no data".
    """
    base = solver_url(cfg)
    if not base or not url:
        return None
    budget = timeout if timeout is not None else solver_timeout(cfg)
    payload = {"cmd": "request.get", "url": str(url),
               "maxTimeout": int(budget * 1000)}
    try:
        from server import httpclient  # noqa: F401  (shared client, one seam)
        import httpx
        r = httpx.post(f"{base}/v1", json=payload, timeout=budget + 10.0)
    except Exception:
        return None
    try:
        body = r.json() if r.content else {}
    except ValueError:
        return None
    solution = body.get("solution") if isinstance(body, dict) else None
    if not isinstance(solution, dict):
        return None
    html = solution.get("response")
    cookies = {}
    for cookie in solution.get("cookies") or []:
        name = str((cookie or {}).get("name") or "").strip()
        if name:
            cookies[name] = str((cookie or {}).get("value") or "")
    user_agent = str(solution.get("userAgent") or "")
    return {"html": html if isinstance(html, str) else None,
            "status": solution.get("status"),
            "cookies": cookies, "user_agent": user_agent}


def fetch_page(url, cfg=None, *, headers=None, cookies=None, params=None,
               timeout=20.0, host=None, solve_on_challenge=True):
    """A LIVE page: ``{"ok", "html", "status", "challenge", "via"}``.

    The fast path is a plain GET through the shared client, wearing whatever
    cleared-session cookies the caller holds (its own paste, plus anything the
    solver last won for this host). Only if that answer IS the interstitial —
    or the request failed outright — is the solver asked, and only when one is
    configured. `via` names which route produced `html` ("http" or "solver"),
    so a caller can report where a page came from.

    Never raises: a network error is ``ok: False`` with `challenge: False`, and
    a cleared page that Cloudflare still refused is ``ok: False`` with
    `challenge: True` — two different things the report must not blur.
    """
    host = host or ""
    request_headers = dict(headers or {})
    jar = dict(cookies or {})
    jar.update(solved_cookies(host))
    ua = solved_user_agent(host)
    if ua:
        request_headers.setdefault("User-Agent", ua)
    status = None
    text = ""
    try:
        from server import httpclient  # noqa: F401
        import httpx
        r = httpx.get(url, params=params or {}, headers=request_headers,
                      cookies=jar or None, timeout=timeout,
                      follow_redirects=True)
        status, text = r.status_code, (r.text or "")
    except Exception as e:
        status, text = None, f"__error__:{type(e).__name__}"

    if not text.startswith("__error__") and not is_challenge(text, status):
        return {"ok": True, "html": text, "status": status,
                "challenge": False, "via": "http"}

    if solve_on_challenge:
        solved = solve(url, cfg)
        if solved and solved.get("html") and not is_challenge(solved["html"]):
            _remember(host, solved.get("cookies"), solved.get("user_agent"))
            return {"ok": True, "html": solved["html"],
                    "status": solved.get("status") or 200,
                    "challenge": False, "via": "solver"}

    return {"ok": False, "html": "", "status": status,
            "challenge": is_challenge(text, status) or not text.startswith("__error__"),
            "via": "none"}
