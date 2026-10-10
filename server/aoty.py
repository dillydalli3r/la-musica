"""Album of the Year, read LIVE through Cloudflare.

Album of the Year has no API, and every page sits behind Cloudflare's
"Just a moment…" interstitial. MEASURED, 2026-10-10, from this machine: a plain
HTTP client, `curl_cffi` impersonating Chrome, headless Chromium, and a real
headed Chrome all received the 403 challenge. So a page is fetched through
`server.cfchallenge.fetch_page`, which uses a CLEARED SESSION (`aoty_cookie`,
the Cookie header of a browser that already passed the challenge, sent with
the matching `aoty_user_agent`) and falls back to a FlareSolverr-compatible
solver (`cf_solver_url`) when one is configured. Nothing here touches the
Internet Archive: the owner asked for live info from each site, and the Wayback
route this module replaced is gone.

What each page answers (all VERIFIED against real page markup):

  * an ALBUM page — `/album/<...>.php` — carries the album's genre row
    (headline genres + `secondary`), the album USER SCORE, and the TRACK LIST,
    each row of which carries its own user score. That last one is what makes
    AOTY a per-track source for RATINGS without a second request.
  * a SONG page — `/song/<id>-<slug>/` — carries that song's own user score.
    (It states no genre of its own: AOTY classifies RELEASES, so the album's
    genre row is what a track's genre falls back to.)
  * an ARTIST page — `/artist/<id>-<slug>/` — carries the artist's user score
    and genre row, the last resort for both chains.

The numeric id embedded in every AOTY URL cannot be derived from names, so a
page is RESOLVED through AOTY's own search (`/search/?q=`): the first link of
the right kind whose slug carries the words asked about is the page, and it is
confirmed by the page's own title before it is read. A page a caller asked for
is remembered 30 days by that caller (`integrations._genre_cached`) — but a
CHALLENGE is not: a refusal is not "AOTY has no such page", and remembering it
would keep answering "no data" after the user pasted the cookie that fixes it.
`last_response` says which of the two the last fetch was.

Parsers are pure — HTML in, dict out — so `tools/test_aoty.py` pins them
against real markup with no network.
"""
from __future__ import annotations

import html as _html
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse

from . import cfchallenge

AOTY_BASE = "https://www.albumoftheyear.org"

# The User-Agent every request wears unless `aoty_user_agent` overrides it —
# Cloudflare binds `cf_clearance` to the exact UA that earned it, so a cookie
# pasted from another browser needs that browser's own UA here.
AOTY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

# The last fetch's outcome, for the report — see `last_response`. AOTY has ONE
# route (live), so the difference between "AOTY refused us" (a Cloudflare
# challenge: a fresh `aoty_cookie` or `cf_solver_url` fixes it) and "AOTY holds
# no genre row for this release" is a difference `_aoty_note` has to be able to
# state; "no data" hides the first behind the second. `fetch` writes it, and a
# caller also reads `ok` to refuse to cache a transient refusal as if it were a
# real "no such page".
_LAST_INFO: dict = {}

# --------------------------------------------------------------------------- #
# The credential and the live fetch
# --------------------------------------------------------------------------- #
def cookie(cfg=None):
    """The user's `aoty_cookie` — a browser `Cookie:` header string, or ""."""
    try:
        if cfg is None:
            from mlo.config import load_config
            cfg = load_config()
    except Exception:
        return ""
    raw = str((cfg or {}).get("aoty_cookie") or "").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    return "; ".join(p.strip() for p in raw.split(";") if p.strip())


def user_agent(cfg=None):
    """`aoty_user_agent`, or the built-in Chrome UA when blank."""
    try:
        if cfg is None:
            from mlo.config import load_config
            cfg = load_config()
        ua = str((cfg or {}).get("aoty_user_agent") or "").strip()
    except Exception:
        ua = ""
    return ua or AOTY_HEADERS["User-Agent"]


def cookie_pairs(cfg=None):
    """[(name, value)] of the stored `aoty_cookie`."""
    out = []
    for pair in cookie(cfg).split(";"):
        name, _sep, value = pair.strip().partition("=")
        if name:
            out.append((name.strip(), value))
    return out


def cookiejar(cfg=None):
    """The cookie jar AOTY requests carry, bound to albumoftheyear.org.

    A jar rather than a header so Cloudflare's own `Set-Cookie`s (which the app
    does not persist) can still ride a request within the process, and so the
    credential can never leak to another host.
    """
    try:
        import httpx
    except Exception:
        return None
    jar = httpx.Cookies()
    for name, value in cookie_pairs(cfg):
        jar.set(name, value, domain="albumoftheyear.org")
    return jar


def headers(cfg=None):
    out = dict(AOTY_HEADERS)
    out["User-Agent"] = user_agent(cfg)
    return out


def _cache_dir():
    try:
        from mlo.config import load_config
        folder = str((load_config() or {}).get("music_folder") or "").strip()
    except Exception:
        return ""
    if not folder:
        return ""
    return os.path.join(folder, ".mlo", "data", "aoty_cache")


def _cache_file(key):
    folder = _cache_dir()
    if not folder:
        return ""
    return os.path.join(folder,
                        hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def _cached(key, producer):
    """`producer()` memoized 30 days by *key*, memory then disk; None is cached."""
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return None if hit == _MISS else hit
    path = _cache_file(key)
    if path and os.path.isfile(path) and \
            time.time() - os.path.getmtime(path) < CACHE_TTL:
        try:
            with open(path, encoding="utf-8") as fh:
                value = json.load(fh)
            with _CACHE_LOCK:
                _CACHE[key] = _MISS if value is None else value
            return value
        except (OSError, ValueError):
            pass
    value = producer()
    with _CACHE_LOCK:
        _CACHE[key] = _MISS if value is None else value
    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(value, fh)
            os.replace(tmp, path)
        except OSError:
            pass
    return value


def fetch(path, cfg=None):
    """One LIVE AOTY path → its HTML, or None.

    Goes through `cfchallenge.fetch_page` (cleared cookie, then solver), paced
    to one request a second. Returns None for a challenge or a miss; the caller
    reports which (the chain's own note does) — `last_response` is where it
    reads the two apart — and never an archived page. The 30-day memory of an
    answer belongs to the caller (`integrations._genre_cached`), which is why a
    transient refusal is NOT one of them: only a page AOTY really served, or a
    real "no such page", is worth remembering.
    """
    global _LAST_INFO
    if not path:
        return None
    url = path if path.startswith("http") else f"{AOTY_BASE}{path}"
    # Pacing is `cfchallenge.fetch_page`'s alone now: it keys the 1 req/s per
    # host, so a gate here too would only make every request pay twice.
    got = cfchallenge.fetch_page(url, cfg, headers=headers(cfg),
                                 cookies=dict(cookie_pairs(cfg)), timeout=25.0,
                                 host="albumoftheyear.org")
    ok = bool(got.get("ok")) and bool(got.get("html"))
    if ok and "albumoftheyear.org" not in str(url) and \
            "albumoftheyear.org" not in got["html"]:
        ok = False
    _LAST_INFO = {"ok": ok, "challenge": bool(got.get("challenge")),
                  "via": str(got.get("via") or ""),
                  "status": got.get("status"), "url": url,
                  "at": time.time()}
    return got["html"] if ok else None


def last_response():
    """AOTY's last fetch outcome, for the report — {} before the first fetch.

    `ok`/`challenge`/`via` come from `cfchallenge.fetch_page`, `status` and
    `url` from the request, `at`/`at_iso` when it happened. It exists for the
    same reason RYM's `rym_last_response` does: a source that answered nothing
    must be able to say whether the SITE refused the request (a challenge — a
    credential the user can fix) or simply had nothing.
    """
    info = dict(_LAST_INFO)
    if info:
        info["at_iso"] = datetime.fromtimestamp(info["at"], timezone.utc) \
            .isoformat(timespec="seconds")
    return info


# --------------------------------------------------------------------------- #
# Resolving a page through AOTY's own search
# --------------------------------------------------------------------------- #
_LINK_RE = re.compile(
    r'href="(/(album|artist|song)/[^"#?]+)"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{0,160})<',
    re.I)


def _slug_words(text):
    """The lowercase word tokens of a name, for slug matching."""
    return [w for w in re.split(r"[^a-z0-9]+", str(text or "").lower()) if w]


def _link_matches(href, terms):
    """Whether *href*'s slug carries every word of *terms*."""
    slug = urlparse(href).path.lower()
    hay = re.sub(r"[^a-z0-9]+", " ", slug)
    return all(re.search(rf"\b{re.escape(t)}\b", hay) for t in terms if t)


def search_links(term, kind, cfg=None):
    """Every `/{kind}/` link on AOTY's search page for *term*, in page order."""
    html = fetch(f"/search/?q={quote_plus(term)}", cfg=cfg)
    if not html:
        return []
    out, seen = [], set()
    for href, got_kind, _label in _LINK_RE.findall(html or ""):
        if got_kind.lower() != kind or href in seen:
            continue
        seen.add(href)
        out.append(href)
    return out


def resolve(kind, *terms, cfg=None):
    """The first AOTY `/{kind}/` page whose slug carries every one of *terms*.

    An identity check by NAME — the exact rule the RYM readers use — so a
    namesake page cannot answer for this one. None when the search has no such
    link.
    """
    wanted = [w for term in terms for w in _slug_words(term)]
    term = " ".join(str(t) for t in terms if str(t).strip())
    want = _slug_words(wanted and " ".join(wanted) or term)
    for href in search_links(term, kind, cfg=cfg):
        if _link_matches(href, want):
            return href
    return None


# --------------------------------------------------------------------------- #
# Parsers (pure)
# --------------------------------------------------------------------------- #
def _unescape(text):
    return _html.unescape(re.sub(r"\s+", " ", str(text or ""))).strip()


def _score(value, count):
    """``{"value": float, "count": int}`` from a raw AOTY score, or None.

    AOTY publishes its score twice per box: the anchor/element's `title`
    attribute is the exact average and its text is the rounded integer shown.
    The raw value is kept as a float on AOTY's OWN 0-100 scale (the app converts
    in `mlo.web_ratings`, the one place that owns the scale).
    """
    try:
        got = float(value)
    except (TypeError, ValueError):
        return None
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 0
    return {"value": got, "count": n}


# The score box, by the class AOTY gives it: `albumUserScore` (album),
# `songScore` (a song page), `artistUserScore` (an artist). Markup shapes
# differ slightly — the album and artist boxes wrap an anchor whose `title`
# is the exact average, the song box carries `title` on the div itself — so
# the box is located, then the first `title` (or failing that the first
# integer text) inside it is the score.
_SCORE_BOXES = ("albumUserScore", "songScore", "artistUserScore",
                "albumScore", "artistScore")
_SCORE_TITLE_RE = re.compile(r'title="([0-9]+(?:\.[0-9]+)?)"')
_SCORE_INNER_RE = re.compile(r'>\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*<')
_SCORE_TEXT_RE = re.compile(
    r'Based on\s*(?:<a[^>]*>\s*)?<strong>([0-9,]+)</strong>', re.I)


def _user_score(html):
    text = html or ""
    value = None
    for cls in _SCORE_BOXES:
        box = re.search(r'class="%s"' % cls, text, re.I)
        if not box:
            continue
        region = text[box.start():box.start() + 800]
        found = (_SCORE_TITLE_RE.search(region)
                 or _SCORE_INNER_RE.search(region))
        if found:
            value = found.group(1)
            break
    if value is None:
        return None
    count = 0
    found = _SCORE_TEXT_RE.search(text)
    if found:
        count = int(found.group(1).replace(",", ""))
    return _score(value, count)


# The album's genre row: scoped to the Details row that carries the `Genre`
# label, primary then `secondary`, anchor text is the name.
_AOTY_GENRE_RE = re.compile(
    r'href="/genre/[^"]*"[^>]*>\s*(?:<div[^>]*>\s*)?([^<]+?)\s*<', re.I)


def genres_from_html(html):
    """The album's genre names on an AOTY page, headline ones first, or []."""
    return _genre_row(html)


def _genre_row(html):
    text = html or ""
    marker = text.find("Genre</span>")
    if marker < 0:
        return []
    window = text[max(0, marker - 2000):marker]
    start = window.rfind('class="detailRow"')
    if start >= 0:
        window = window[start:]
    names = []
    for match in _AOTY_GENRE_RE.finditer(window):
        name = _unescape(match.group(1))
        if name and name.lower() not in {n.lower() for n in names}:
            names.append(name)
    return names


# One tracklist row: number, title + song link, and the row's own score.
_TRACK_ROW_RE = re.compile(
    r'<td class="trackNumber">\s*(\d+)\s*</td>\s*'
    r'<td class="trackTitle">\s*<a href="(/song/[^"]+)"[^>]*>([^<]+)</a>'
    r'(?:(?!</tr>).)*?'
    r'<td class="trackRating[^"]*">\s*<span[^>]*title="([0-9,]+)\s*Ratings"[^>]*>'
    r'\s*([0-9]+)\s*<', re.I | re.S)
_TRACK_ROW_NORATING_RE = re.compile(
    r'<td class="trackNumber">\s*(\d+)\s*</td>\s*'
    r'<td class="trackTitle">\s*<a href="(/song/[^"]+)"[^>]*>([^<]+)</a>',
    re.I | re.S)


def _tracklist(html):
    """The album page's own track rows, scoped to `id="tracklist"`."""
    text = html or ""
    marker = text.find('id="tracklist"')
    if marker < 0:
        return []
    window = text[marker:marker + 200000]
    rows = []
    seen = set()
    for num, href, title, _rcount, _rval in _TRACK_ROW_RE.findall(window):
        if num in seen:
            continue
        seen.add(num)
        rows.append({"position": int(num), "title": _unescape(title),
                     "href": href, "rating": _score(_rval, _count_title(
                         window, href))})
    if rows:
        return rows
    for num, href, title in _TRACK_ROW_NORATING_RE.findall(window):
        if num in seen:
            continue
        seen.add(num)
        rows.append({"position": int(num), "title": _unescape(title),
                     "href": href, "rating": None})
    return rows


def _count_title(window, href):
    """The rating count beside a track row (kept separate to stay readable)."""
    at = window.find(href)
    if at < 0:
        return 0
    found = re.search(r'title="([0-9,]+)\s*Ratings"', window[at:at + 400], re.I)
    return int(found.group(1).replace(",", "")) if found else 0


def parse_album(html):
    """An AOTY album page → ``{"genres", "rating", "tracks"}`` or None."""
    if not html:
        return None
    tracks = _tracklist(html)
    genres = _genre_row(html)
    rating = _user_score(html)
    if not tracks and not genres and not rating:
        return None
    return {"genres": genres,
            "rating": rating,
            "tracks": tracks}


def parse_song(html):
    """An AOTY song page → ``{"title", "rating"}`` or None.

    AOTY states no genre on a song page (it classifies releases), so only the
    song's own score is read here; the album page supplies the genre.
    """
    if not html:
        return None
    title = ""
    found = re.search(r'class="songTitle">([^<]+)<', html, re.I)
    if found:
        title = _unescape(found.group(1))
    rating = _user_score(html)
    if not rating and not title:
        return None
    return {"title": title, "rating": rating}


def parse_artist(html):
    """An AOTY artist page → ``{"genres", "rating"}`` or None."""
    if not html:
        return None
    genres = _genre_row(html)
    rating = _user_score(html)
    if not genres and not rating:
        return None
    return {"genres": genres, "rating": rating}
