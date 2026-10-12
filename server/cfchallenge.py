"""Live pages through Cloudflare, for the two scraped sources (RYM, AOTY).

Both sites answer a plain client with Cloudflare's "Just a moment…"
interstitial. MEASURED, 2026-10-10, from this machine:

  * the interstitial is served to EVERY client on its first navigation — that
    includes the real browsers, so "it met the challenge" is not the question.
    The question is what CLEARS it, and on these two hosts the answer differs.
  * a REAL BROWSER clears both, by itself, in seconds: a plain Chrome and a
    plain Firefox, pointed at the URL, hand back the live page.
  * a CLEARED SESSION alone is enough for Album of the Year — its
    `cf_clearance`, read out of the owner's own Firefox and sent with that
    browser's User-Agent, answers `200` with the real page over plain `httpx`.
  * a CLEARED SESSION is NOT enough for RateYourMusic: the same fresh, genuine
    cookie set is refused `403 Just a moment...` over `httpx` AND over
    `curl_cffi` impersonating Firefox 135, 144 and 157.
  * HEADLESS is detected: `--headless=new` Chrome and `--headless` Firefox both
    end on the interstitial. A real window is load-bearing.
  * FlareSolverr's bundled Chromium never passes either challenge — "Error
    solving the challenge. Timeout after 60.0 seconds", every attempt, on a
    clean process with 20 GB free.

So a live page comes from whichever of these can be had, in this order:

  1. a CLEARED SESSION, as a plain fast HTTP GET (see
     `server.integrations._rym_get` for RYM and `aoty_page` for AOTY);
  2. a REAL BROWSER this machine already has (`server/browserfetch.py`, gated
     on `cf_browser_fetch`) — the only route that clears RYM, and it caches the
     clearance in the profile the app owns, so later pages are fast;
  3. a SOLVER (`cf_solver_url`), a FlareSolverr-compatible service. Kept
     because the failure above is a fact about THIS network, not a law: where
     one can pass, it wins the same clearance cookies and the plain path takes
     over. It is asked only when the browser is off or could not answer.

Nothing here pretends the archive is live: the Wayback route was removed from
both scrapers on the owner's instruction ("I want live info from each site").

The one entry point the scrapers use is `fetch_page`. A site that is not behind
a challenge (MusicBrainz, or an already-cleared RYM) pays for none of this.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time

# The route that actually clears the two scraped hosts: a real Chrome/Edge
# driven over DevTools (`server/browserfetch.py`). Imported at module level on
# purpose — it imports nothing from here, so there is no cycle, and a name
# imported INSIDE one function would be local to it and shadow this one.
from . import browserfetch

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

# One shared per-host gate for every live HTTP GET and browser navigation.
# `fetch_page` uses it for plain GETs; `browserfetch.fetch` uses it for CDP
# navigations. Source callsites add no second gate (or the wait doubles).
# Both scraped sources ask for one request a second.
FETCH_MIN_INTERVAL = 1.0
# A 429 or a 5xx that is NOT the interstitial is the site being busy, not an
# answer, so it is retried this many times before the fetch fails.
FETCH_RETRIES = 2
# {host: [lock, last_ts]} — one lock and one timestamp PER host, so two hosts
# never wait on each other while each host's own requests stay serialised.
_FETCH_GATES: dict = {}
_FETCH_GATES_LOCK = threading.Lock()

# Per host: a solve is running RIGHT NOW. FlareSolverr drives ONE browser, so
# two callers asking for the same host do not run side by side — the second
# queues behind the first and the request the user is watching pays the whole
# solve twice. MEASURED, 2026-10-10: boot's warm-up probe and the first genre
# import were in flight for rateyourmusic.com a second apart, and the import
# waited out BOTH (~2.5 minutes for that source alone). A caller that finds one
# already running starts none; `solve` answers None and records no failure —
# "not known yet" is not "known bad".
_SOLVING: dict = {}
_SOLVING_LOCK = threading.Lock()

# The smallest solve worth starting: a browser handed less than this cannot
# get through a challenge, so with less left the attempt is not made at all.
MIN_SOLVE_BUDGET = 5.0

# The calling THREAD's solver deadline (an epoch time, or absent when
# unbounded), set by a request that must answer in bounded time — the genre
# import. `solve` clamps its own budget to what is left and starts nothing once
# the budget is gone, so a host that cannot answer cannot hold a user-facing
# request open. Thread-local on purpose: FastAPI runs sync routes in a
# threadpool, and a background import must not inherit a user's deadline.
_BUDGET = threading.local()


@contextlib.contextmanager
def budget(seconds):
    """Cap solver work on THIS thread to *seconds* from now (re-entrant).

    A nested call can only TIGHTEN the deadline: the outer caller's stays the
    ceiling. Nothing set means unbounded, which is what every caller that is
    not a user-facing request wants.
    """
    prev = getattr(_BUDGET, "until", None)
    # `None` means unbounded; 0 (or anything else) is a real deadline, so
    # `budget(0)` reads as "no time left" rather than as "no budget".
    until = None if seconds is None else time.time() + float(seconds)
    if prev is not None and (until is None or prev < until):
        until = prev
    _BUDGET.until = until
    try:
        yield
    finally:
        _BUDGET.until = prev


def budget_left():
    """Seconds left on this thread's solver budget, or None when unbounded."""
    until = getattr(_BUDGET, "until", None)
    return None if until is None else max(0.0, until - time.time())


def probing(host):
    """Whether a solve for *host* is running right now (see `_SOLVING`)."""
    with _SOLVING_LOCK:
        return str(host or "").lower() in _SOLVING


def _solving_begin(host):
    """Claim *host*'s one solve slot — False when the slot is already taken."""
    with _SOLVING_LOCK:
        if host in _SOLVING:
            return False
        _SOLVING[host] = True
        return True


def _solving_end(host):
    with _SOLVING_LOCK:
        _SOLVING.pop(host, None)


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


# Host, saved cookie key (for the solver warm-up), and URL. `warm_solver` skips
# a host with a stored cookie; `warm_browser` still visits it because a
# clearance bound to the user's other browser is dropped before Chrome sees it.
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
                got = solve(url, cfg)
                if got and got.get("html") and not is_challenge(got["html"]):
                    # A solve that WORKED is proof as much as a failure is, and
                    # caching its clearance HERE is what lets the chain take the
                    # fast path from its first import instead of having to win a
                    # solve of its own. This is the one caller allowed to spend
                    # a browser on a host nothing has answered for yet.
                    _remember(host, got.get("cookies"), got.get("user_agent"))
            except Exception:
                continue

    threading.Thread(target=_run, daemon=True, name="solver-warm").start()


def warm_browser(cfg=None):
    """Clear both hosts' challenge at boot, in the background, with the browser.

    The OTHER warm-up (`warm_solver`) asks what the solver can pass; this one
    starts from the route that actually works. WHY at boot: MEASURED,
    2026-10-11 — the FIRST navigation to a host after the profile goes cold
    spends the whole challenge (Album of the Year ~27 s, RateYourMusic ~49 s),
    which is most of what a page fetch is willing to wait for and more than a
    genre lookup's own budget. A cold start therefore reported "no data" for a
    host that answers in a second once its clearance is in the profile, and the
    user's first import of the session was the thing that paid it. One
    successful navigation per host, off the request path, and every fetch
    afterwards is the cleared fast path (`solved_cookies` is filled by the
    browser itself — see `browserfetch._publish_clearance`).

    Skipped per host when the MANAGED browser profile already holds its own
    fresh clearance, when no browser can be driven, or when the caller did not
    carry `cf_browser_fetch` (the same gate `browserfetch.enabled` applies: a
    suite's own cfg must never launch a browser on the machine running it). The
    user's pasted clearance belongs to a different UA and is discarded before
    it reaches Chrome; a saved login session does not clear Chrome's challenge.

    A host that answers nothing on the first navigation is asked ONCE more:
    MEASURED, 2026-10-11, Album of the Year's front page is heavy enough that a
    cold clearance can outlast even the raised ceiling, while the same URL
    answers in ~3 s the moment that first navigation is done — so a second
    attempt is what turns "the warm-up did nothing" into a cleared profile.
    """
    def _run():
        for host, _cookie_key, url in WARM_URLS:
            try:
                if solved_cookies(host):
                    continue
                for _attempt in range(2):
                    got = browserfetch.fetch(url, cfg)
                    if got and got.get("html"):
                        break
            except Exception:
                continue

    if not browserfetch.available(cfg):
        return
    threading.Thread(target=_run, daemon=True, name="browser-warm").start()


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


def remember_clearance(host, cookies, user_agent=""):
    """Cache a cleared session for *host* — the fast plain path's credential.

    `solve` calls this with what the solver won; `server/browserfetch.py` calls
    it with what a real browser holds after it fetched a page. Both are the same
    thing to this module (the cookies a cleared client sends and the UA they are
    bound to), and caching a clearance a browser WON is what makes the pages
    after the first plain fast GETs instead of more browser navigations.
    """
    if host:
        _remember(host, cookies, user_agent)


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

    ONE solve per host at a time (`_SOLVING`): a second caller finding one in
    flight gets None immediately rather than a browser's worth of queued wait.
    A caller with a deadline (`budget`) gets its own shortened budget, and gets
    None — again without recording a failure — once the deadline leaves less
    than `MIN_SOLVE_BUDGET`: the host is not known-bad, the clock is.
    """
    target = host_of(url)
    if target and host_failed(target):
        return None      # known-bad host: do not pay the timeout again
    left = budget_left()
    if left is not None and left < MIN_SOLVE_BUDGET:
        return None      # out of time: no answer, and NOT a failure
    if target and not _solving_begin(target):
        return None      # one solve per host at a time — see `_SOLVING`
    try:
        base = solver_url(cfg)
        if not base and ensure_solver(cfg):
            base = solver_url(cfg)
        if not base or not url:
            return None
        allow = timeout if timeout is not None else solver_timeout(cfg)
        if left is not None:
            allow = min(allow, left)
        payload = {"cmd": "request.get", "url": str(url),
                   "maxTimeout": int(allow * 1000)}
        # The grace above a solve is for the solver's own response overhead, but
        # it is NOT a licence to outlive a caller's deadline: a request someone
        # is watching gets its clock back.
        wait = allow + (1.0 if left is not None else 10.0)
        try:
            from server import httpclient  # noqa: F401  (shared client, one seam)
            import httpx
            r = httpx.post(f"{base}/v1", json=payload, timeout=wait)
        except Exception as e:
            record_failure(target,
                           f"the solver did not answer ({type(e).__name__})")
            return None
        try:
            body = r.json() if r.content else {}
        except ValueError:
            record_failure(target, "the solver sent something that is not JSON")
            return None
        solution = body.get("solution") if isinstance(body, dict) else None
        if not isinstance(solution, dict):
            # FlareSolverr's failure shape: {"status": "error", "message":
            # "Error solving the challenge. Timeout after 60.0 seconds."}. Its
            # own words are what the report needs, so they are kept, not
            # paraphrased.
            message = ""
            if isinstance(body, dict):
                message = str(body.get("message") or body.get("error") or "")
            if not message and getattr(r, "status_code", 0) >= 400:
                message = f"the solver answered HTTP {r.status_code}"
            record_failure(target,
                           message or "the solver could not pass the challenge")
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
    finally:
        if target:
            _solving_end(target)


def fetch_page(url, cfg=None, *, headers=None, cookies=None, params=None,
               timeout=20.0, host=None, solve_on_challenge=True, plain=True):
    """A LIVE page: ``{"ok", "html", "status", "challenge", "via"}``.

    The fast path is a plain GET through the shared client, wearing whatever
    cleared-session cookies the caller holds (its own paste, plus anything the
    solver last won for this host). Only if that answer IS the interstitial —
    or the request failed outright — is the solver asked, and only when one is
    configured. `via` names which route produced `html` ("http", "browser" or
    "solver"), so a caller can report where a page came from.

    `plain=False` skips that GET entirely and asks the browser first: a host
    that answers a plain client with a DEGRADED page (not a challenge — a real
    `200` carrying less than the real one, which is what Album of the Year does
    without the user's own session — see `server/aoty.py::fetch`) must not have
    that page mistaken for an answer.

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
    for attempt in range(FETCH_RETRIES + 1 if plain else 0):
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

    if plain and not text.startswith("__error__") and not is_challenge(text, status):
        if r is not None and (status == 429 or (status or 0) >= 500):
            # The retries above were not enough: an exhausted refusal is no
            # page — the caller must report a failure, not "no data".
            return {"ok": False, "html": "", "status": status,
                    "challenge": False, "via": "none"}
        return {"ok": True, "html": text, "status": status,
                "challenge": False, "via": "http"}

    if solve_on_challenge:
        # A REAL BROWSER FIRST, and the solver only if there is none: MEASURED,
        # 2026-10-10 — the solver cannot pass either host's challenge (a 60 s
        # timeout, every attempt, on a clean process), while a real Chrome
        # clears both in seconds and keeps the clearance in the profile this
        # app owns. Asking the solver first would spend its whole timeout
        # before reaching the route that works. `server/browserfetch.py`.
        try:
            got = browserfetch.fetch(url, cfg)
        except Exception:
            got = None
        if got and got.get("html") and not is_challenge(got["html"]):
            return {"ok": True, "html": got["html"],
                    "status": got.get("status") or 200,
                    "challenge": False, "via": "browser"}
        solved = solve(url, cfg)
        if solved and solved.get("html") and not is_challenge(solved["html"]):
            _remember(host, solved.get("cookies"), solved.get("user_agent"))
            return {"ok": True, "html": solved["html"],
                    "status": solved.get("status") or 200,
                    "challenge": False, "via": "solver"}

    return {"ok": False, "html": "", "status": status,
            "challenge": is_challenge(text, status) or not text.startswith("__error__"),
            "via": "none"}
