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
import threading
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

# How long a host whose challenge the solver FAILED to pass is left alone.
#
# MEASURED, 2026-10-10, on this machine: FlareSolverr 3.5.2 boots, launches its
# bundled Chromium and serves; then it cannot pass rateyourmusic.com's
# challenge — "Error solving the challenge. Timeout after 60.0 seconds", the
# interstitial still up, re-measured with tabs_till_verify too. That is a fact
# about the host/network (an interactive Turnstile, a flagged IP), not a
# timeout to retry: without this, every lookup pays the full 60 s before
# reporting the same failure — the genre chain asks per album, and AOTY asks
# once per page, so one import spent minutes doing nothing. Ten minutes, not
# for ever: a challenge is meant to be passed once, and the user may fix the
# network, clear it with a cleared cookie, or press Test.
FAIL_TTL = 10 * 60.0

# In-memory, per host: {"cookies": {name: value}, "user_agent": str, "at": ts}.
_SOLVED = {}
# Per host: {"at": ts, "message": str} — the last solve the solver FAILED. It
# gates both the next solve attempt and what the genre chain reports, so a
# challenge the solver cannot pass costs its 60 s ONCE. See FAIL_TTL.
_FAILED = {}
# The host we last tried to bring the managed solver up for, so a burst of
# lookups does not each try to spawn one (and so a machine with no install does
# not pay a detect pass per request). Reset with clear_failures().
_STARTED_FOR = {"at": 0.0, "ok": False}
_START_TTL = 60.0

# Outbound pacing for the live scrapers. `fetch_page` is the ONE gate: it keeps
# consecutive GETs to the same host at least this far apart, so a caller that
# paced as well (aoty.fetch used to) would only make every request pay twice.
# Both scraped sources ask for one request a second.
FETCH_MIN_INTERVAL = 1.0
# A 429 or a 5xx that is NOT the interstitial is the site being busy, not an
# answer, so it is retried this many times before the fetch fails.
FETCH_RETRIES = 2
# {host: [lock, last_ts]} — one lock and one timestamp PER host, so two hosts
# never wait on each other while each host's own requests stay serialised.
_FETCH_GATES: dict = {}
_FETCH_GATES_LOCK = threading.Lock()


def _pace(host):
    """Sleep until *host*'s previous request is ``FETCH_MIN_INTERVAL`` old.

    A blank host has nothing to key the gate on and is not paced."""
    if not host:
        return
    with _FETCH_GATES_LOCK:
        gate = _FETCH_GATES.get(host)
        if gate is None:
            gate = [threading.Lock(), 0.0]
            _FETCH_GATES[host] = gate
    with gate[0]:
        wait = FETCH_MIN_INTERVAL - (time.time() - gate[1])
        if wait > 0:
            time.sleep(wait)
        gate[1] = time.time()


def _retry_after(r):
    """Seconds a response's ``Retry-After`` asks us to wait, or 0.0.

    The header is delta-seconds or an HTTP-date; anything else, a header already
    in the past, or a body-less response is 0.0 — the per-host pace then spaces
    the retry regardless."""
    try:
        raw = str(r.headers.get("Retry-After") or "").strip()
    except Exception:
        return 0.0
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        import calendar
        from email.utils import parsedate
        when = parsedate(raw)
        if when is None:
            return 0.0
        return max(0.0, float(calendar.timegm(when)) - time.time())
    except Exception:
        return 0.0


def host_of(url):
    """The hostname of *url* — the key everything per-host is kept under.

    `www.` is stripped: a site's requests are not all made to the same spelling
    (`albumoftheyear.org` resolves through `www.albumoftheyear.org`, whose
    SEARCH page is fetched first), and a latch that only matched one of the two
    would let the other ask the solver again — measured live: AOTY's failure was
    recorded under `www.albumoftheyear.org` while the skip check looked for
    `albumoftheyear.org`, so nothing was skipped and the import paid the
    timeout twice.
    """
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def host_failed(host, ttl=FAIL_TTL):
    """Whether a solve for *host* failed inside the TTL — the reason or "".

    `""` means "no known failure, try it", so callers can use the truthiness
    directly; the string is the solver's own error, which the report quotes.
    """
    entry = _FAILED.get(str(host or "").lower())
    if not entry or time.time() - entry.get("at", 0) > ttl:
        return ""
    return str(entry.get("message") or "the solver could not pass it")


def record_failure(host, message=""):
    """Remember that a solve failed for *host* (see host_failed)."""
    if host:
        _FAILED[str(host).lower()] = {"at": time.time(),
                                      "message": str(message or "")}


def clear_failures(host=None):
    """Forget a failed solve — the Sources panel's Test, and Start.

    A user who changes the network, pastes a cleared cookie or restarts the
    solver is asking the question again, so the answer must not come from the
    latch.
    """
    if host:
        _FAILED.pop(str(host).lower(), None)
    else:
        _FAILED.clear()
    _STARTED_FOR["at"] = 0.0


# What the background warm-up probes, and the cookie that makes a probe
# pointless for that host.
WARM_URLS = (
    ("rateyourmusic.com", "rym_cookie", "https://rateyourmusic.com/"),
    ("albumoftheyear.org", "aoty_cookie", "https://www.albumoftheyear.org/"),
)


def warm_solver(cfg=None):
    """Find out — once, at boot, in the background — what the solver can pass.

    MEASURED, 2026-10-10: FlareSolverr runs but cannot pass either scraped
    host's challenge from the owner's network, and the ANSWER takes a full
    solve to learn (~60 s per host). Asking that question lazily means the
    first genre import is the thing that waits for it — which is exactly how
    "Importing genres…" sat for minutes. So the app asks it here instead: one
    request per host, off the request path, recorded in the same failure latch
    the chain reads, so an import afterward is instant and its report already
    knows why the source was skipped.

    Skipped when a cleared cookie answers that host anyway (the live path needs
    no solver), and when the host is already solved or already latched. The
    thread is a daemon: a shutdown must never wait for a browser.
    """
    def _run():
        for host, cookie_key, url in WARM_URLS:
            try:
                if host_failed(host) or solved_cookies(host):
                    continue
                if str((cfg or {}).get(cookie_key) or "").strip():
                    continue
                solve(url, cfg)
            except Exception:
                continue

    threading.Thread(target=_run, daemon=True, name="solver-warm").start()


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
    """The solver endpoint to use, or "".

    The whole resolution — `cf_solver_url`, then `MLO_SOLVER_URL` /
    `FLARESOLVERR_URL`, then the managed FlareSolverr this app installs and
    runs (server/solver.py), then anything already answering on the default
    local port — lives in `server.solver`, because a solver is now something
    the app can OWN rather than only be pointed at. This stays the one name the
    rest of this module calls; if the manager cannot be imported (a stripped
    build), the configured value still works on its own.
    """
    try:
        from server import solver
        return solver.base_url(cfg)
    except Exception:
        pass
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
    """Forget the solver's own state: solved cookies, failed hosts, the lazy
    start's rate-limit.

    Used by the tests, and by a credential change in Settings — after which
    every earlier answer (a clearance, or "this host beat me") was about a
    state that no longer holds.
    """
    _SOLVED.clear()
    _FAILED.clear()
    _STARTED_FOR["at"] = 0.0
    _STARTED_FOR["ok"] = False


def ensure_solver(cfg=None):
    """Bring the app's own FlareSolverr up when a scrape needs one — or False.

    The solver is a dependency the app installs and runs (server/solver.py), so
    "installed but not started" must not read as "no Cloudflare answer": that is
    exactly the state a run that installed it mid-session was left in, and the
    one a user meets after a crash. Called from `solve`/`fetch_page` at the
    moment a challenge is in hand, so the cost is only paid when it is needed.
    Cheap after the first call (the manager no-ops when something answers), and
    rate-limited so a burst of albums cannot each try to spawn one.

    A hand-built cfg that carries neither solver key means "ask only what I
    gave you" — the rule `server.solver.resolve` applies to the ambient URL —
    so a unit test can never spawn a FlareSolverr on the machine running it.
    """
    if solver_url(cfg):
        return True
    if cfg is not None:
        cfg = cfg or {}
        if "cf_solver_url" not in cfg and "cf_solver_autostart" not in cfg:
            return False
    now = time.time()
    if now - _STARTED_FOR["at"] < _START_TTL:
        return _STARTED_FOR["ok"]
    ok = False
    try:
        from server import solver
        # `ensure_started` respects `cf_solver_autostart`: that switch means
        # "do not bring it up by yourself", and a lazy start is by ourselves.
        ok, _msg = solver.ensure_started(cfg)
    except Exception:
        ok = False
    _STARTED_FOR["at"], _STARTED_FOR["ok"] = now, bool(ok)
    return bool(ok)


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

    A host whose challenge the solver FAILED is not asked again for FAIL_TTL
    (`host_failed`), and no solver at all is an answer too: the app's own
    install is started on demand (`ensure_solver`) rather than reported as a
    missing configuration.
    """
    target = host_of(url)
    if target and host_failed(target):
        return None      # known-bad host: do not pay the timeout again
    base = solver_url(cfg)
    if not base and ensure_solver(cfg):
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
    except Exception as e:
        record_failure(target, f"the solver did not answer ({type(e).__name__})")
        return None
    try:
        body = r.json() if r.content else {}
    except ValueError:
        record_failure(target, "the solver sent something that is not JSON")
        return None
    solution = body.get("solution") if isinstance(body, dict) else None
    if not isinstance(solution, dict):
        # FlareSolverr's failure shape: {"status": "error", "message": "Error
        # solving the challenge. Timeout after 60.0 seconds."}. Its own words
        # are what the report needs, so they are kept, not paraphrased.
        message = ""
        if isinstance(body, dict):
            message = str(body.get("message") or body.get("error") or "")
        if not message and getattr(r, "status_code", 0) >= 400:
            message = f"the solver answered HTTP {r.status_code}"
        record_failure(target, message or "the solver could not pass the challenge")
        return None
    html = solution.get("response")
    if html is None or is_challenge(html):
        record_failure(target, str(body.get("message") or "")
                       or "the solver ended on the challenge page")
        return None
    cookies = {}
    for cookie in solution.get("cookies") or []:
        name = str((cookie or {}).get("name") or "").strip()
        if name:
            cookies[name] = str((cookie or {}).get("value") or "")
    user_agent = str(solution.get("userAgent") or "")
    return {"html": html,
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
    r = None
    for attempt in range(FETCH_RETRIES + 1):
        _pace(host)                 # the ONE per-host gate — see FETCH_MIN_INTERVAL
        try:
            from server import httpclient  # noqa: F401
            import httpx
            r = httpx.get(url, params=params or {}, headers=request_headers,
                          cookies=jar or None, timeout=timeout,
                          follow_redirects=True)
            status, text = r.status_code, (r.text or "")
        except Exception as e:
            r = None
            status, text = None, f"__error__:{type(e).__name__}"
        # A 429 or a 5xx that is NOT the interstitial is the site being busy,
        # not an answer, so it must not be parsed as a page: retry it, spaced by
        # the per-host pace above plus any Retry-After the site named.
        busy = (r is not None and not is_challenge(text, status)
                and (status == 429 or (status or 0) >= 500))
        if not busy or attempt >= FETCH_RETRIES:
            break
        wait = _retry_after(r)
        if wait > 0:
            time.sleep(wait)

    if not text.startswith("__error__") and not is_challenge(text, status):
        if r is not None and (status == 429 or (status or 0) >= 500):
            # The retries above were not enough: an exhausted refusal is no
            # page — the caller must report a failure, not "no data".
            return {"ok": False, "html": "", "status": status,
                    "challenge": False, "via": "none"}
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
