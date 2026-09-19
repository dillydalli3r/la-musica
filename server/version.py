"""Is this install behind the newest release?

`mlo.__version__` is the single source of truth for what this build is; the
newest release comes from GitHub's releases API, asked at most once every six
hours and cached on disk (in the app data dir) so a restart — or a second
client asking — does not ask again.

Nothing here is allowed to be fatal. An offline box, a rate limit, a proxy in
the way or an answer this code cannot parse all read as `source:
"unavailable"`, `latest: None`, `update_available: False`: a version banner is
a courtesy, never a reason for a health check to fail. There is deliberately
no periodic poller: `check()` (the API route) asks GitHub when the cache is
older than six hours, and `cached()` — what `/api/health` answers with — never
touches the network at all, because the launchers probe health with a
two-second timeout to decide whether this port is even ours. A health reply
that finds the cache stale starts ONE background refresh (`refresh_soon`).
"""

import json
import os
import threading
import time
import urllib.request

REPO = "dillydalli3r/la-musica"
_RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"

# Six hours: long enough that the API is asked a handful of times a day at
# most, short enough that a fresh release shows up in the same session.
CACHE_TTL_S = 6 * 3600
# A version check must never hold a request open; the answer is cached anyway,
# so giving up after a few seconds costs nothing.
TIMEOUT_S = 5


def cache_path() -> str:
    from mlo.paths import app_data_dir
    return os.path.join(app_data_dir(), "update_check.json")


def _read_cache() -> dict:
    """The cached answer, or {} when there is none this code can use."""
    try:
        with open(cache_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(data: dict) -> None:
    """Best effort: an unwritable state dir just means the next call asks
    GitHub again, which is not worth failing a request over."""
    try:
        path = cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


def _version_parts(text: str) -> tuple:
    """`1.2.3` / `v1.2.3` as `(1, 2, 3)`; `()` when it is not a version."""
    parts = []
    for chunk in str(text or "").strip().lstrip("vV").split("."):
        num = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            num += ch
        if not num:
            return ()
        parts.append(int(num))
    return tuple(parts)


def _fetch_latest() -> dict:
    """`{latest, release_url}` from GitHub, or raise (the caller catches).

    Isolated as its own function so a test can stub the network by replacing
    this one attribute.
    """
    from mlo import __version__
    req = urllib.request.Request(
        _RELEASES_API,
        headers={"User-Agent": f"la-musica/{__version__}",
                 "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    return {
        "latest": str(data.get("tag_name") or "").strip() or None,
        "release_url": str(data.get("html_url") or "").strip() or None,
    }


def _result(fresh: dict) -> dict:
    """The public shape, from a cached (or freshly fetched) answer."""
    from mlo import __version__

    now = time.time()
    latest = fresh.get("latest") or None
    current = _version_parts(__version__)
    newer = _version_parts(latest) if latest else ()
    return {
        "version": __version__,
        "latest": latest,
        "update_available": bool(newer and current and newer > current),
        "release_url": fresh.get("release_url") or None,
        "checked_at": float(fresh.get("checked_at") or now),
        "source": str(fresh.get("source") or "unavailable"),
    }


def _fresh_enough(cached: dict, now: float) -> bool:
    try:
        age = now - float(cached.get("checked_at") or 0)
    except (TypeError, ValueError):
        return False
    return bool(cached.get("checked_at")) and 0 <= age < CACHE_TTL_S


def cached() -> dict:
    """The last answer, and NEVER the network.

    `/api/health` is what the launchers (tray.py, start_app.py, the Tauri
    shell) probe with a two-second timeout to prove the port is ours, so a
    health reply must not wait on GitHub: it reports what the last check
    found, or the unavailable shape when nothing has been checked yet.
    """
    return _result(_read_cache())


# One background refresh at a time, started by a request that found the cache
# stale (see refresh_soon) — not a poller: nothing runs unless someone asks.
_refresh_lock = threading.Lock()
_refreshing = False


def refresh_soon() -> None:
    """Re-check in the background, unless a fresh answer already exists or a
    refresh is already running. Returns immediately."""
    global _refreshing
    with _refresh_lock:
        if _refreshing or _fresh_enough(_read_cache(), time.time()):
            return
        _refreshing = True

    def run():
        global _refreshing
        try:
            check(force=True)
        except Exception:
            pass  # check() already answers "unavailable" instead of raising
        finally:
            with _refresh_lock:
                _refreshing = False

    threading.Thread(target=run, name="mlo-version-check", daemon=True).start()


def check(force: bool = False) -> dict:
    """`{version, latest, update_available, release_url, checked_at, source}`.

    The cached answer is reused until it is `CACHE_TTL_S` old (`force` skips
    that), and a failed check is cached too — an unreachable GitHub must not
    mean a fresh five-second wait on every health poll.
    """
    now = time.time()
    cached_answer = _read_cache()
    if not force and _fresh_enough(cached_answer, now):
        return _result(cached_answer)
    try:
        fresh = dict(_fetch_latest(), checked_at=now, source="github")
    except Exception:
        fresh = {"latest": None, "release_url": None,
                 "checked_at": now, "source": "unavailable"}
    _write_cache(fresh)
    return _result(fresh)
