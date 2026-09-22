"""MusicBrainz / LRCLIB / RateYourMusic integrations for the import wizard.

MusicBrainz is queried with proper rate limiting (1 req/s) and a UA string
per their API etiquette. RYM has no public API — links are user-supplied
URLs stored as tags, but we validate/parse them here.
"""
import asyncio
import contextlib
from datetime import datetime, timezone
import hashlib
import html as _html
import json
import os
import random
import re
import threading
import time
import unicodedata
import uuid

import httpx

from mlo import cover_choice as _cover_choice
from mlo import release_choice

MB_BASE = "https://musicbrainz.org/ws/2"
LRCLIB_BASE = "https://lrclib.net/api"
USER_AGENT = "la-musica/2.0 (https://github.com/dillydalli3r/la-musica)"

_last_request = 0.0
_mb_lock = threading.Lock()


class MusicBrainzError(RuntimeError):
    """MusicBrainz did not answer, or refused the request and said why.

    Raised after the retries are exhausted (429/5xx/connection) with a "busy,
    try again" reason, and also when MusicBrainz rejects a request with a 4xx
    whose body names its own reason (a query its search index refused) — that
    message is MusicBrainz's, verbatim, so a caller can show it to the user.

    Typed so a caller can report it as a PER-ITEM reason — "MusicBrainz is
    busy, try again" — instead of a request or a job that sits on an outage.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


_MB_MIN_INTERVAL = 1.0     # MusicBrainz etiquette: one request per second
_MB_RETRY_STATUS = (429, 500, 502, 503, 504)
_MB_BACKOFF_BASE = 1.0     # retry wait: base * 2**(attempt-1), plus jitter
_MB_BACKOFF_MAX = 8.0
# One mb_get — throttle, retries and backoff included — never runs longer than
# this. A 503ing MusicBrainz used to hold its caller (and, for an auto-import
# job, the single-job slot) for as long as the retry loop felt like it.
MB_RETRY_DEADLINE = 45.0


def _raise_mb_refusal(r):
    """MusicBrainz's OWN reason for refusing a request, when it gave one.

    A refused WS/2 request answers 4xx with a JSON body naming the reason —
    {"error": "You submitted a blank search query. You must include a
    non-blank 'query=' parameter with your search."} — which httpx's own
    message ("Client error '400 Bad Request' for url ...") throws away. The
    in-app search box shows MusicBrainz's words for a query MusicBrainz
    rejected, so the body wins whenever it carries one. A 404 is left alone:
    callers branch on it to mean "no such entity", and its body only ever
    says "Not Found".
    """
    if r.status_code == 404:
        return
    try:
        body = r.json()
    except Exception:
        return
    message = str((body or {}).get("error") or "").strip()
    if message:
        raise MusicBrainzError(message, status=r.status_code)


def mb_get(endpoint, params=None, timeout=30.0, retries=3, deadline=MB_RETRY_DEADLINE):
    """Rate-limited MusicBrainz WS/2 GET returning parsed JSON.

    Retries 429/5xx and connection errors with exponential backoff + jitter,
    keeping MusicBrainz's 1 request/second etiquette and a hard overall
    `deadline`; when the retry budget or the deadline runs out it raises
    MusicBrainzError — a typed, reportable reason. Any other status raises
    httpx's own error, except that a 4xx whose body names MusicBrainz's own
    reason raises MusicBrainzError carrying THAT text (`_raise_mb_refusal`) —
    and a 404 keeps meaning "no such entity".
    """
    global _last_request
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    started = time.time()
    attempts = 0
    status = None
    reason = "no response"
    while attempts < max(1, int(retries)):
        attempts += 1
        retry_after = None
        with _mb_lock:
            elapsed = time.time() - _last_request
            if elapsed < _MB_MIN_INTERVAL:
                time.sleep(min(_MB_MIN_INTERVAL - elapsed,
                               max(0.0, deadline - (time.time() - started))))
            try:
                r = httpx.get(
                    f"{MB_BASE}/{endpoint}",
                    params=params or {},
                    headers=headers,
                    # never sit on the socket past the deadline either
                    timeout=min(timeout, max(1.0, deadline - (time.time() - started))),
                )
            except httpx.HTTPError as e:
                status, reason = None, f"connection error ({e.__class__.__name__})"
            else:
                if r.status_code not in _MB_RETRY_STATUS:
                    _raise_mb_refusal(r)
                    r.raise_for_status()
                    return r.json()
                status, reason = r.status_code, f"HTTP {r.status_code}"
                retry_after = r.headers.get("Retry-After")
            _last_request = time.time()
        if attempts >= retries:
            break
        wait = min(_MB_BACKOFF_BASE * (2 ** (attempts - 1)), _MB_BACKOFF_MAX)
        wait += random.uniform(0.0, wait / 2.0)   # jitter: retries must not sync
        if retry_after:
            try:
                wait = max(wait, float(retry_after))
            except ValueError:
                pass
        if time.time() - started + wait > deadline:
            break
        time.sleep(wait)
    raise MusicBrainzError(
        f"MusicBrainz is busy ({reason}) for {endpoint} after {attempts} "
        f"attempt(s) in {int(time.time() - started)}s — try again in a moment",
        status=status,
    )


# --------------------------------------------------------------------------- #
# Tiny TTL cache for browse endpoints (release/artist pages get re-fetched on
# every navigation; MB etiquette caps us at 1 req/s, so repeat views must not
# re-hit the network). Keyed by (endpoint, sorted params), 10-minute TTL.
# --------------------------------------------------------------------------- #
_BROWSE_CACHE: dict = {}
_BROWSE_LOCK = threading.Lock()
_INFLIGHT: dict = {}
# MB data moves slowly; an hour of TTL keeps repeat views instant (and well
# within the 1 req/s etiquette) without serving anything meaningfully stale.
_BROWSE_TTL = 1800.0


def mb_get_cached(endpoint, params=None, timeout=30.0, retries=5):
    """Cached MB GET with stale-while-revalidate and single-flight.

    A fresh cached copy returns instantly. A stale copy ALSO returns
    instantly while a background thread refreshes it — only genuinely
    unknown payloads block on the rate-limited network, and identical
    concurrent requests share one flight instead of queueing several
    1-second-spaced calls."""
    key = (endpoint, tuple(sorted((k, str(v)) for k, v in (params or {}).items())))
    now = time.time()
    with _BROWSE_LOCK:
        hit = _BROWSE_CACHE.get(key)
        flight = _INFLIGHT.get(key)
    if hit and now - hit[0] < _BROWSE_TTL:
        return hit[1]
    if hit and flight is None:
        # serve stale, refresh in the background
        def _refresh():
            try:
                data = mb_get(endpoint, params, timeout=timeout, retries=retries)
                with _BROWSE_LOCK:
                    _BROWSE_CACHE[key] = (time.time(), data)
            except Exception:
                pass  # keep the stale copy on refresh failure
            finally:
                with _BROWSE_LOCK:
                    ev = _INFLIGHT.pop(key, None)
                if ev is not None:
                    ev.set()  # wake waiters — they read the cache we just wrote

        with _BROWSE_LOCK:
            if _INFLIGHT.get(key) is None:
                _INFLIGHT[key] = threading.Event()
            else:
                return hit[1]  # someone else is already refreshing
        threading.Thread(target=_refresh, daemon=True).start()
        return hit[1]
    if flight is not None:
        # an identical request is already on the wire — wait for it instead
        # of queueing a second rate-limited call behind it
        flight.wait(timeout=45)
        with _BROWSE_LOCK:
            hit = _BROWSE_CACHE.get(key)
        if hit:
            return hit[1]
        raise RuntimeError("concurrent MusicBrainz request did not complete")
    with _BROWSE_LOCK:
        flight = _INFLIGHT.get(key)
        if flight is None:
            flight = _INFLIGHT[key] = threading.Event()
    try:
        data = mb_get(endpoint, params, timeout=timeout, retries=retries)
        with _BROWSE_LOCK:
            # Write the cache BEFORE waking waiters: a woken reader that
            # found an empty cache would 500 a fetch that actually worked.
            _BROWSE_CACHE[key] = (time.time(), data)
            # keep the cache from growing without bound
            if len(_BROWSE_CACHE) > 600:
                for k in list(_BROWSE_CACHE)[:200]:
                    _BROWSE_CACHE.pop(k, None)
    finally:
        with _BROWSE_LOCK:
            _INFLIGHT.pop(key, None)
        flight.set()
    return data


_DETECT_MISSES: dict = {}          # mbid -> when it last failed, for the TTL below
_DETECT_MISS_TTL = 900.0           # a wrong paste is re-probed at most every 15 min


def detect_mbid(mbid):
    """Which MusicBrainz entity kind does this MBID belong to?

    Tries a minimal lookup per browsable entity (cache-shared with the
    entity pages) and reports the first hit — lets the UI route a pasted
    bare ID without the user picking a type. An ID that is not in MB costs
    one request per entity kind, and the search box re-asks while the user
    looks at the answer, so a miss is remembered for a while instead of
    being probed four more times."""
    key = str(mbid or "").lower()
    if key and time.time() - _DETECT_MISSES.get(key, 0.0) < _DETECT_MISS_TTL:
        raise LookupError("no MusicBrainz entity found for this ID")
    for entity in MB_ENTITIES:
        try:
            data = mb_get_cached(f"{entity}/{mbid}", {"fmt": "json"})
        except Exception:
            continue
        return {
            "type": entity,
            "id": data.get("id") or mbid,
            "title": data.get("title") or data.get("name") or "",
        }
    if key:
        if len(_DETECT_MISSES) > 200:
            _DETECT_MISSES.clear()
        _DETECT_MISSES[key] = time.time()
    raise LookupError("no MusicBrainz entity found for this ID")


# MusicBrainz alias `type` values that are not a NAME: "Search hint" aliases are
# the search index's own spellings (misspellings, abbreviations) and belong to
# queries, never to a page a reader is looking at.
_ALIAS_SKIP_TYPES = frozenset({"search hint"})


def _locale_preference(cfg=None):
    """The locale the reader wants names in — `locale`, folded.

    The SAME setting the managed beets import translates names with
    (Settings -> Import & tags: "Preferred locale for aliases"), so a page and
    the files it will produce agree on what an entity is called.
    """
    cfg = cfg if isinstance(cfg, dict) else _release_cfg()
    return str((cfg or {}).get("locale") or "").strip().lower()


def _has_latin(value):
    """Whether *value* carries a Latin letter — a romanization's tell.

    MusicBrainz states a Japanese/Chinese/Korean/Cyrillic name's reading as an
    alias in `*-Latn` or a translation in a Latin-script language; both contain
    Latin letters while the name does not, which is what makes them the useful
    parentheses for a reader who cannot read the script.
    """
    return any("a" <= ch.lower() <= "z" for ch in str(value or ""))


def alias_for(aliases, cfg=None, name=""):
    """The alias to show beside *name* in parentheses, or "".

    MusicBrainz states an entity's other-language names as aliases, each with
    its own `locale` and `type`. The ladder, in order:

      1. an alias in the reader's locale flagged `primary` — MusicBrainz's own
         "this is the name in this language",
      2. any other alias in exactly that locale,
      3. the same two again for a COUSIN locale: MusicBrainz separates a script
         with a hyphen (`ja-Latn`) and a region with an underscore (`en_PH`), so
         a reader who asked for `ja` is answered by its romanization and one who
         asked for `en` by `en_PH` — only when the exact locale has nothing,
      4. the entity's `primary` alias whatever its locale — a reader who chose
         no locale, or whose locale this entity has no alias for, still gets
         the name it is also known by rather than a foreign-language guess,
      5. a Latin reading of a name that has none — MusicBrainz states a
         romanization (`*-Latn`) or a translation as an alias, and for a reader
         looking at `ロストアンブレラ` the useful parentheses are "Lost Umbrella";
         a transliteration is preferred over a translation, then MusicBrainz's
         own order decides,
      6. nothing, and the caller shows the name alone.

    A search-hint alias is never chosen, and a value that only differs from the
    name by case or spacing is "no alias": a page must not render "X (X)".
    """
    rows = [a for a in (aliases or []) if isinstance(a, dict)
            and str(a.get("name") or "").strip()
            and str(a.get("type") or "").strip().lower() not in _ALIAS_SKIP_TYPES]
    if not rows:
        return ""
    want = _locale_preference(cfg)

    def in_locale(alias):
        got = str(alias.get("locale") or "").strip().lower()
        if not got or not want:
            return False
        if got == want:
            return True
        # MusicBrainz separates a SCRIPT with a hyphen ("ja-Latn") and a REGION
        # with an underscore ("en_PH"), so both are folded before the language
        # prefix is compared: a reader who asked for `ja` wants the `ja-Latn`
        # romanization, and one who asked for `en` wants `en_PH`.
        def base(value):
            return re.split(r"[-_]", value, 1)[0]
        return base(got) == base(want)

    def pick(candidates):
        folded = str(name or "").strip().casefold()
        for alias in candidates:
            value = str(alias["name"]).strip()
            if value.casefold() != folded:
                return value
        return ""

    def is_exact(alias):
        return str(alias.get("locale") or "").strip().lower() == want

    if want:
        # Exact locale before a cousin: a reader who asked for `en` wants the
        # `en` alias, not `en_PH`, and one who asked for `ja` wants the `ja`
        # alias before the `ja-Latn` romanization — the cousins are what is
        # left when the exact one does not exist.
        for candidates in ([a for a in rows if is_exact(a) and a.get("primary")],
                           [a for a in rows if is_exact(a)],
                           [a for a in rows if in_locale(a) and a.get("primary")],
                           [a for a in rows if in_locale(a)]):
            chosen = pick(candidates)
            if chosen:
                return chosen
    chosen = pick([a for a in rows if a.get("primary")])
    if chosen:
        return chosen
    # The last resort, and the one the Japanese/Chinese/Korean pages live on: a
    # Latin reading of a name that has none. `ロストアンブレラ` with no locale
    # configured and no primary alias is still best shown as "Lost Umbrella" —
    # MusicBrainz's own order decides among equals, with a transliteration
    # (`*-Latn`) preferred over a translation.
    if not _has_latin(name):
        latin = [a for a in rows if _has_latin(a.get("name"))]
        latin.sort(key=lambda a: 0 if "latn" in str(a.get("locale") or "").lower() else 1)
        return pick(latin)
    return ""


def _browse_collect(endpoint, extra_params, list_key, count_key, limit=300, offset=0):
    """Browse rows across MusicBrainz's 100-per-request pages.

    Browse has NO server-side sort, so a single arbitrary 100-row slice
    misrepresents a discography (one page can be all albums, the next all
    singles) and any date ordering would be a lie. This walks the pages
    (still 1 req/s) up to `limit` rows starting at `offset` so the caller
    can sort and filter over an honest window. Returns (rows, total, served)
    — `served` being the raw rows MusicBrainz handed back for this window,
    which is what the next offset must follow.

    Rows are de-duplicated by MBID (a browse page that repeats an entity
    must not list it twice); paging stays MusicBrainz's own, which is why
    the raw batch length advances `pos`, not the de-duplicated one."""
    items = []
    seen = set()
    pos = offset
    total = None
    while pos < offset + limit:
        data = mb_get_cached(
            endpoint,
            {**extra_params, "limit": min(100, offset + limit - pos), "offset": pos, "fmt": "json"},
        )
        batch = [r for r in (data.get(list_key) or []) if r.get("id")]
        total = data.get(count_key) or total
        for row in batch:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            items.append(row)
        pos += len(batch)
        if not batch or pos >= min(total or 0, offset + limit):
            break
    if total is None:
        total = len(items)
    # `pos - offset` is how many RAW rows MusicBrainz served for this window;
    # the caller pages by that, so a de-duplicated row can never make the
    # next request overlap the one before it.
    return items, total, pos - offset


def _mbid(value):
    """Extract a MusicBrainz ID from an ID or a musicbrainz.org URL."""
    if not value:
        return None
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value, re.I)
    return m.group(0).lower() if m else None


def _genres(node):
    """MusicBrainz's own genre names for one entity, in its own order.

    The spelling is left exactly as MusicBrainz publishes it (lowercase): the
    writers canonicalize through `mlo.genres.normalize_genres`, and a second
    casing rule here is what used to make the same genre land under two names
    ("Shoegaze" from the tagger, "shoegaze" from the vocabulary).
    """
    return [g["name"] for g in (node.get("genres") or [])]


# --------------------------------------------------------------------------- #
# Release lookups
# --------------------------------------------------------------------------- #
def release_countries(node, preferred="", release_id=""):
    """Every country a release was released in, with its date.

    MusicBrainz keeps a release's events ON the release entity — no `inc` asks
    for them — as one {area, date} per country the pressing appeared in, while
    the entity's own singular `country`/`date` are only the FIRST of them. A
    release group is released in as many countries as its editions cover, so
    the pages show the whole list rather than that first event.

    Returns [{country, code, date, preferred}] ordered by date, then name:
    `country` is MusicBrainz's own area name, `code` its ISO 3166-1 code ("" for
    an area that carries none, which happens for a historic area), `date` the
    event's date, and `preferred` whether the code is the configured
    `prefer_release_country`. An event without an area carries no country at
    all and is dropped — missing data is absent, never invented. `release_id`
    stamps the release that carries the event, for a list that is a release
    group's union over its editions.
    """
    preferred = str(preferred or "").strip().upper()
    out, seen = [], set()
    for event in (node.get("release-events") or []):
        area = event.get("area") or {}
        name = str(area.get("name") or "").strip()
        codes = [str(c).strip().upper() for c in (area.get("iso-3166-1-codes") or []) if str(c).strip()]
        code = codes[0] if codes else ""
        if not name and not code:
            continue                    # a placeless event states no country
        key = (code or name.lower(), str(event.get("date") or ""))
        if key in seen:
            continue
        seen.add(key)
        entry = {
            "country": name or code,
            "code": code,
            "date": str(event.get("date") or ""),
            "preferred": bool(preferred) and code == preferred,
        }
        if release_id:
            entry["release_id"] = release_id
        out.append(entry)
    out.sort(key=lambda e: (e["date"] or "9999", e["country"]))
    return out


def release_group_countries(releases, preferred=""):
    """The union of a release group's editions' events, newest editions
    included.

    `releases` are raw browse rows (they carry `release-events` themselves), in
    the order the caller ranks them, so an event shared by several editions is
    credited to the first-ranked edition that carries it — the same edition
    "Add to library" would take. The union covers the editions of the pages
    loaded so far; a group with more editions than one page is paged, and its
    countries grow as its editions do.
    """
    out, seen = [], set()
    for r in releases or []:
        for event in release_countries(r, preferred, str(r.get("id") or "")):
            key = (event["code"] or event["country"], event["date"])
            if key in seen:
                continue
            seen.add(key)
            out.append(event)
    out.sort(key=lambda e: (e["date"] or "9999", e["country"]))
    return out


def preferred_release_country(cfg=None):
    """`prefer_release_country` — the country the release/release-group country
    chips mark as the user's preference ("" when they set none). Read through
    the same ONE config load the release-choice policy uses, so the marking and
    the policy can never disagree about what the user prefers."""
    return str(_release_cfg(cfg).get("prefer_release_country") or "")


def release_lookup(mbid):
    """Full release: media/discs, recordings, artist credits, genres and
    labels (catalog numbers). Country comes from the release entity.

    Through `mb_get_cached`: the release page, the import wizard's release
    picker and the match step all read this same payload, and at 1 req/s a
    second view of the same release must not cost a second request (the
    cache is what MB's etiquette asks for — this call used to bypass it)."""
    data = mb_get_cached(
        f"release/{mbid}",
        {"inc": "artists+recordings+media+release-groups+artist-credits+genres"
                "+labels+isrcs+aliases", "fmt": "json"},
    )
    # Normalize media into a flat list of {disc, position, title, length, recording mbid, artist mbids}
    tracks = []
    for medium in data.get("media", []):
        disc = medium.get("position", 1)
        for trk in medium.get("tracks", []):
            rec = trk.get("recording", {})
            artists = []
            for ac in trk.get("artist-credit", []):
                if "artist" in ac:
                    artists.append({
                        "name": ac.get("name", ""),
                        "mbid": ac["artist"].get("id"),
                    })
            tracks.append({
                "position": trk.get("position"),
                "disc": disc,
                "title": trk.get("title"),
                "length": trk.get("length"),
                "recording_mbid": rec.get("id"),
                # Whether MusicBrainz states this recording IS a video. The
                # acquisition branch reads it to tell a music-video release
                # from an album: with the medium (media[].format) it is what
                # decides that a Digital Media video release is fetched from
                # YouTube rather than searched for on the network — see
                # server.soulseek_auto.acquisition_route.
                "video": bool(rec.get("video")),
                "artist_mbids": [a["mbid"] for a in artists],
                "artist_credit": "".join(
                    (ac.get("name", "") + (ac.get("joinphrase", "") or ""))
                    for ac in trk.get("artist-credit", [])
                ),
                "genres": _genre_names(_genres(rec)),
                # ISRCs come free with this request (inc=isrcs) and are what
                # the advisory fetch looks a track up by.
                "isrcs": [v for v in (_isrc(i) for i in (rec.get("isrcs") or [])) if v],
            })
    release_artists = [
        {"name": ac.get("name", ""), "mbid": ac["artist"].get("id")}
        for ac in data.get("artist-credit", []) if "artist" in ac
    ]
    rg_obj = data.get("release-group") or {}
    primary = (rg_obj.get("primary-type") or "").lower()
    secondary = [s.lower() for s in (rg_obj.get("secondary-types") or [])]
    release_type = "+".join([primary] + secondary) if primary else ""

    # labels -> label name + every catalog number. A release can carry several
    # (one per label/pressing) and auto-import searches each as its OWN query,
    # so keeping only the first lost every other pressing's number. Order is
    # MusicBrainz's own; blanks and duplicates dropped. `catalog_number` stays
    # the first one for the callers that only ever wanted one.
    label_name = ""
    catalog_numbers = []
    for lab in data.get("label-info", []) or []:
        if not label_name:
            label_name = str(((lab.get("label") or {}).get("name")) or "").strip()
        cn = (lab.get("catalog-number") or "").strip()
        if cn and cn not in catalog_numbers:
            catalog_numbers.append(cn)
    catalog_number = catalog_numbers[0] if catalog_numbers else ""
    country = data.get("country") or ""
    # Every country this pressing was released in (the singular `country` above
    # is MusicBrainz's first event, not the whole story — a worldwide digital
    # reissue of a CD carries both).
    countries = release_countries(data, preferred_release_country())

    return {
        "id": data.get("id"),
        "title": data.get("title"),
        # The title in the reader's locale, when MusicBrainz states one (see
        # `alias_for`) — the page shows it in parentheses beside the title.
        "alias": alias_for(data.get("aliases"), None, data.get("title")),
        # MusicBrainz's own pressing comment ("Deluxe Edition", "2011
        # remaster") — empty when it states none, which is what every reader
        # treats as "no disambiguation".
        "disambiguation": data.get("disambiguation") or "",
        "date": (data.get("date") or ""),
        # the release-group's first-release-date — the "original" release
        # date shown next to this specific release's own date
        "originaldate": (rg_obj.get("first-release-date") or ""),
        "barcode": (data.get("barcode") or ""),
        "country": country,
        # every country of this pressing, with its date and the preference mark
        "countries": countries,
        # Status (Official/Promotion/Bootleg/…) + the medium of the first
        # medium — the release page meta line shows both, and the auto-import
        # policy keys on them.
        "status": data.get("status") or "",
        "medium": next((m.get("format") or "" for m in data.get("media", [])), ""),
        "catalog_number": catalog_number,
        # All of them, in MusicBrainz order — the auto-import search expands
        # this into one query per number.
        "catalog_numbers": catalog_numbers,
        "label": label_name,
        "release_group_id": (data.get("release-group") or {}).get("id"),
        # `release_type` keeps its historical lowercase "+"-joined spelling —
        # it is written into the RELEASETYPE tag and drives the naming script
        # and grading. The structured pair below is for display: MusicBrainz
        # splits release types into primary (Album/EP/Single/Broadcast/Other)
        # and secondary (Soundtrack/Live/Compilation/Remix/Demo/...).
        "release_type": release_type,
        "primary_type": rg_obj.get("primary-type") or "",
        "secondary_types": [s for s in (rg_obj.get("secondary-types") or [])],
        "artists": release_artists,
        "genres": _genre_names(_genres(data)),
        "media": tracks,
        "medium_count": len(data.get("media", [])),
        "medium_formats": [m.get("format") or "" for m in data.get("media", [])],
    }


def recording_isrcs(recording_mbid):
    """ISRCs MusicBrainz holds for a recording (usually exactly one)."""
    if not recording_mbid:
        return []
    try:
        data = mb_get_cached(f"recording/{recording_mbid}",
                             {"inc": "isrcs", "fmt": "json"})
    except Exception:
        return []
    return [v for v in (_isrc(i) for i in (data.get("isrcs") or [])) if v]


# --------------------------------------------------------------------------- #
# Content advisory (ITUNESADVISORY): 0 = not explicit, 1 = explicit, 2 = safe.
# --------------------------------------------------------------------------- #
# EVERY applicable source is asked for EVERY track — one pass, no short
# circuit — and the answers are collected per source and MERGED by a single
# documented rule (`merge_advisory`), so a source that states "not explicit"
# is heard even after another one already said "explicit", and the caller can
# show what each source said:
#
#   deezer-isrc    api.deezer.com/track/isrc:<ISRC>      (verified working)
#   spotify-isrc   Spotify search by ISRC                (needs client id+secret)
#   apple-album    iTunes artist → its albums → THE EXPLICIT EDITION →
#                  lookup?entity=song                    (verified working)
#   itunes-song    iTunes song search, exact title only  (last resort)
#   discogs-parental  Discogs edition format/description (token only, WEAK,
#                  album level — explicit-only, ranks last)
#   youtube-age    yt-dlp `age_limit` >= 18 for the track's OWN video id
#                  (explicit-only, last, never a blind per-track search)
#
# ISRC path: the two ISRC sources are asked for every ISRC the track has —
# the file's own tag, or every ISRC MusicBrainz holds for its recording.
# APPLE HAS NO ISRC LOOKUP, which is why it is reached by artist → album
# edition instead; it is still cross-referenced with both ISRC sources.
#
# VERIFIED, on this machine: Apple's own `lookup?isrc=` and `lookup?upc=`
# endpoints answer resultCount 0 — Apple does not serve identity lookups, so
# Apple is reached through the artist's album list (or the exact-title song
# search). APPLE'S ALBUM *SEARCH* IS NOT USABLE FOR THIS: `search?entity=album`
# answers ONE edition — for System Of A Down's "Steal This Album!" the CLEANED
# one — whose tracks then read `cleaned`/`notExplicit` over the whole album,
# i.e. an explicitly clean re-release of an explicit master. The artist route
# (`lookup?id=<artistId>&entity=album`) lists every edition with its own
# `collectionExplicitness`, so the explicit edition is the one whose tracks are
# read, and the cleaned edition is only a cross-check.
#
# Deezer's ISRC endpoint is per-track and needs no title guessing at all.
# Spotify is optional and never load-bearing; it is skipped entirely until
# `spotify_client_id`/`spotify_client_secret` are set in Settings.
#
# Honesty rules, which every route below obeys:
#   * a route only ever returns an answer it can attribute to itself, and an
#     answer it cannot state is NO answer (never a guess, never a default);
#   * a `cleaned` Apple entry (or a `Clean` contentAdvisoryRating) is a CLEANED
#     EDITION, states nothing about the original master and is NO ANSWER — it
#     must never become 0 or 2;
#   * a name-based match never accepts a variant of the track, and a variant
#     never accepts the original (`title_matches`);
#   * the merged value is 0 when nobody stated anything at all: an unstated
#     advisory is written as "not explicit", the user's policy, and the
#     `answers` map is what says whether any source actually spoke.
# Every value a route may report as `source`. A caller may only write a value
# it can attribute to one of these.
ADVISORY_SOURCES = frozenset({"deezer-isrc", "spotify-isrc", "apple-album",
                              "itunes-song", "discogs-parental",
                              "youtube-age"})

_ITUNES_LOOKUP = "https://itunes.apple.com"
_DEEZER_TRACK_ISRC = "https://api.deezer.com/track/isrc:"
_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SPOTIFY_SEARCH = "https://api.spotify.com/v1/search"
# Album titles are compared with difflib below this ratio = not this release.
_APPLE_ALBUM_SIMILARITY = 0.6
# iTunes rate-limits hard: hammering it earns an EMPTY body (not JSON), and a
# library pass re-reads the same artist and the same album's tracks over and
# over. Calls are spaced out AND cached — in memory by the transport, on disk
# below so a restart does not re-ask what Apple already told us.
_APPLE_MIN_INTERVAL = 3.0
_APPLE_CACHE_TTL = 30 * 86400.0
_APPLE_CACHE_NAME = "apple_cache"
_apple_lock = threading.Lock()
_apple_last = 0.0

# One answer per (source, identity). A library pass asks each track once;
# `None` (asked, nobody stated a value) is cached too, so a rerun that found
# nothing does not re-hit the APIs either. Bounded — it is a memo, not a store.
_ADVISORY_CACHE: dict = {}
_ADVISORY_LOCK = threading.Lock()
_ADVISORY_MISS = object()
_SPOTIFY_TOKEN: dict = {}
# Why Spotify last refused the client credentials: `{reason, at}`. The token
# cache only ever holds a success, so without this a rejected client id/secret
# left the sources silently skipped — see `spotify_last_error`.
_SPOTIFY_LAST: dict = {}
_ADVISORY_CACHE_MAX = 20000


def _advisory_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _advisory_json(url, params=None, headers=None, timeout=None, host=None):
    """The one HTTP seam every advisory route goes through: discovery's
    per-host-throttled, TTL-cached JSON client, so a library pass is polite
    and a repeat ask costs no request. None on any failure."""
    from server import discovery
    return discovery._json(url, params, headers=headers, timeout=timeout, host=host)


def _advisory_post(url, data=None, headers=None, timeout=None):
    """POST sibling of `_advisory_json` (only Spotify's token endpoint needs
    one). -> `(body, error)`, where `error` is the endpoint's own refusal in
    its own words ("" when it answered).

    The body is returned even for a refusal: OAuth2 states a rejected client
    IN the 400 body (`{"error": "invalid_client", "error_description": ...}`),
    and a caller that could only see None could not tell a wrong client secret
    from an unreachable host — which is exactly the difference a Test button
    has to report."""
    try:
        r = httpx.post(url, data=data, headers=headers, timeout=timeout or 15.0)
        if r.status_code >= 400:
            return None, f"HTTP {r.status_code}: {(r.text or '').strip()[:300]}"
        return r.json(), ""
    except Exception as e:
        return None, f"no answer ({type(e).__name__}: {e})"


def _advisory_cached(key, producer):
    """Memoized `producer()` — answer or None, cached either way."""
    with _ADVISORY_LOCK:
        hit = _ADVISORY_CACHE.get(key)
    if hit is not None:
        return None if hit is _ADVISORY_MISS else hit
    answer = producer()
    with _ADVISORY_LOCK:
        if len(_ADVISORY_CACHE) >= _ADVISORY_CACHE_MAX:
            _ADVISORY_CACHE.clear()
        _ADVISORY_CACHE[key] = _ADVISORY_MISS if answer is None else answer
    return answer


def _deezer_advisory(isrc, timeout=None):
    """(value, source) for one track from Deezer's ISRC lookup, or None.

    Deezer states two things and both are read:
      * `explicit_content_lyrics` 1 (explicit lyrics) or 2 (explicit content /
        artwork), or `explicit_lyrics: true` → 1. BOTH are explicit statements:
        this app's 2 means "safe" (mlo/autotag.py), so mapping an explicit flag
        to 2 would relabel an explicit track as safe.
      * `explicit_lyrics: false` (whatever the content flag says) or
        `explicit_content_lyrics: 0` → 0 — Deezer is stating the track is not
        explicit.
      * anything else (the unclassified value Deezer reports as 3) is NO
        ANSWER, and never 0.
    """
    code = str(isrc or "").strip()
    if not code:
        return None
    data = _advisory_json(_DEEZER_TRACK_ISRC + code, timeout=timeout,
                          host="api.deezer.com")
    if not isinstance(data, dict) or data.get("error"):
        return None
    content = _advisory_int(data.get("explicit_content_lyrics"))
    lyrics = data.get("explicit_lyrics")
    if content in (1, 2) or lyrics is True:
        return (1, "deezer-isrc")
    if content == 0 or lyrics is False:
        return (0, "deezer-isrc")
    return None


def _spotify_token_post(cid, secret, timeout=None):
    """One `POST /api/token` with the client-credentials grant.

    -> `(body, error)`; the ONE place the Basic header is built, so the token
    the sources use and the token a Test button asks for cannot drift apart.
    """
    import base64
    return _advisory_post(
        _SPOTIFY_TOKEN_URL,
        data={"grant_type": "client_credentials"},
        headers={"Authorization": "Basic " + base64.b64encode(
            f"{cid}:{secret}".encode("utf-8")).decode("ascii"),
            "Content-Type": "application/x-www-form-urlencoded"},
        timeout=timeout)


def _spotify_token(cfg, timeout=None):
    """Client-credentials token, or None when Spotify is unconfigured/down.

    `spotify_client_id` + `spotify_client_secret` are optional; without them
    this returns None and the route is skipped, never defaulted. A REFUSED
    client keeps its refusal in `_SPOTIFY_LAST` so the caller can report
    Spotify's own words instead of an empty skip — see `spotify_last_error`.
    """
    cid = str((cfg or {}).get("spotify_client_id") or "").strip()
    secret = str((cfg or {}).get("spotify_client_secret") or "").strip()
    if not cid or not secret:
        return None
    with _ADVISORY_LOCK:
        token = _SPOTIFY_TOKEN.get("token")
        if token and _SPOTIFY_TOKEN.get("expires", 0) > time.time():
            return token
    body, error = _spotify_token_post(cid, secret, timeout=timeout)
    token = (body or {}).get("access_token")
    if not token:
        _record_spotify_refusal(error or "no access_token in the answer")
        return None
    with _ADVISORY_LOCK:
        _SPOTIFY_TOKEN["token"] = token
        _SPOTIFY_TOKEN["expires"] = (
            time.time() + float((body or {}).get("expires_in") or 3600) - 60)
    return token


def _record_spotify_refusal(reason):
    with _ADVISORY_LOCK:
        _SPOTIFY_LAST.update(reason=str(reason or ""), at=time.time())


def spotify_last_error(since=None):
    """Why Spotify last refused the client credentials, or "" — see
    `spotify_check`. `since` filters out a refusal from an earlier run."""
    with _ADVISORY_LOCK:
        got = dict(_SPOTIFY_LAST)
    if not got or (since and float(got.get("at") or 0) < float(since)):
        return ""
    return str(got.get("reason") or "")


def spotify_check(cfg=None, timeout=None):
    """Are the saved client credentials accepted? -> `{ok, checked, detail}`.

    `POST /api/token` with the client-credentials grant IS the authentication
    the sources then depend on, so this asks the same endpoint they do (through
    the same `_spotify_token_post` seam). A rejected pair answers
    `{"error": "invalid_client", …}` in a 400 body, which is reported
    verbatim. Live by construction: the token cache is bypassed so a Test
    after pasting a new secret asks Spotify again."""
    cid = str((cfg or {}).get("spotify_client_id") or "").strip()
    secret = str((cfg or {}).get("spotify_client_secret") or "").strip()
    if not cid or not secret:
        return {"ok": False, "checked": "",
                "detail": "no spotify_client_id/spotify_client_secret is set"}
    started = time.time()
    body, error = _spotify_token_post(cid, secret, timeout=timeout)
    token = str((body or {}).get("access_token") or "").strip()
    if token:
        return {"ok": True, "checked": "POST /api/token (client_credentials)",
                "detail": "client credentials accepted — POST /api/token "
                          "issued a token"}
    _record_spotify_refusal(error or "no access_token in the answer")
    return {"ok": False, "checked": "POST /api/token (client_credentials)",
            "detail": "Spotify rejected the client credentials — "
                      + (error or spotify_last_error(started))}


def _spotify_advisory(isrc, cfg, timeout=None):
    """(value, source) from Spotify's ISRC search, or None.

    Only a hit whose own `external_ids.isrc` is the ISRC we asked about
    counts — a search hit is a candidate, not an identity.
    """
    code = str(isrc or "").strip()
    token = _spotify_token(cfg, timeout=timeout) if code else None
    if not token:
        return None
    data = _advisory_json(
        _SPOTIFY_SEARCH,
        {"q": f"isrc:{code}", "type": "track", "limit": 1},
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout, host="api.spotify.com")
    for item in (((data or {}).get("tracks") or {}).get("items") or []):
        got = str((item.get("external_ids") or {}).get("isrc") or "").strip()
        if got.upper() != code.upper():
            continue
        return (1 if item.get("explicit") else 0, "spotify-isrc")
    return None


def _data_cache_dir(name):
    """<music>/.mlo/data/<name> — the app's folder-state dir for *name*."""
    from mlo.paths import app_data_dir
    music = ""
    try:
        from mlo.config import load_config
        music = str(load_config().get("music_folder") or "")
    except Exception:
        music = ""
    d = app_data_dir(music or None) or ""
    return os.path.join(d, name) if d else None


def _apple_cache_dir():
    """Disk cache for the artist→albums and album→tracks payloads."""
    return _data_cache_dir(_APPLE_CACHE_NAME)


def _apple_json(path, params, timeout=None):
    """One iTunes GET: ~1 per _APPLE_MIN_INTERVAL, disk-cached for a month.

    Apple rate-limits hard — rapid repeats answer an EMPTY body, not JSON —
    and these payloads (an artist's whole album list, one album's tracks) are
    what a library pass re-reads for every file. None on any failure; this
    never raises.
    """
    global _apple_last
    import hashlib
    from urllib.parse import urlencode
    key = hashlib.sha1((path + "?" + urlencode(
        sorted((str(k), str(v)) for k, v in (params or {}).items())
    )).encode("utf-8")).hexdigest()
    d = _apple_cache_dir()
    fp = os.path.join(d, key + ".json") if d else None
    if fp:
        try:
            if (os.path.isfile(fp)
                    and time.time() - os.path.getmtime(fp) < _APPLE_CACHE_TTL):
                with open(fp, encoding="utf-8") as fh:
                    return json.load(fh)
        except (OSError, ValueError):
            pass
    with _apple_lock:
        wait = _APPLE_MIN_INTERVAL - (time.time() - _apple_last)
        if wait > 0:
            time.sleep(wait)
        _apple_last = time.time()
        data = _advisory_json(f"{_ITUNES_LOOKUP}{path}", dict(params or {}),
                              timeout=timeout, host="itunes.apple.com")
    if data is None or not fp:
        return data
    try:
        os.makedirs(d, exist_ok=True)
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh)
        os.replace(tmp, fp)
    except OSError:
        pass
    return data


def _apple_country(cfg=None):
    """The storefront Apple is asked about — the app's region setting."""
    return (str((cfg or {}).get("cover_country") or "").strip().lower()
            or "us")


def _apple_artist_id(artist, timeout=None):
    """Apple's artist id for a name, or None.

    Only the artist route lists EVERY edition of a release, so this is where
    the advisory route starts.
    """
    name = str(artist or "").strip()
    if not name:
        return None
    data = _apple_json("/search", {"term": name, "entity": "musicArtist",
                                   "limit": 5}, timeout=timeout)
    want = _norm_compare(name)
    for row in (data or {}).get("results") or []:
        if _norm_compare(row.get("artistName")) == want:
            return _advisory_int(row.get("artistId"))
    return None


def _apple_editions(artist, album, track_count=None, cfg=None, timeout=None):
    """Apple's editions of one album, the EXPLICIT edition first.

    `lookup?id=<artistId>&entity=album` lists every edition Apple holds, each
    with its own `collectionExplicitness`. An edition counts as this album
    when its normalized `collectionName` matches — a `trackCount` match only
    corroborates (it sorts such an edition up, and lets a near-identical title
    through). Every failure — no artist, no editions, a rate-limited empty
    body — returns [].
    """
    artist_id = _apple_artist_id(artist, timeout=timeout)
    if not artist_id:
        return []
    data = _apple_json("/lookup",
                       {"id": artist_id, "entity": "album", "limit": 200,
                        "country": _apple_country(cfg)}, timeout=timeout)
    want_artist = _norm_compare(artist)
    want_album = _norm_compare(album)
    want_count = _advisory_int(track_count)
    rows = []
    for row in (data or {}).get("results") or []:
        cid = _advisory_int(row.get("collectionId"))
        if not cid:
            continue
        got_artist = _norm_compare(row.get("artistName"))
        if want_artist and got_artist and want_artist not in got_artist:
            continue
        name = _norm_compare(row.get("collectionName"))
        # An instrumental/karaoke edition of the album is not the album: its
        # tracks would rate the wrong recording, so a variant mismatch is out
        # for both the name match and the similarity fallback below.
        kind = title_variant_kind(album)
        got_kind = title_variant_kind(row.get("collectionName"))
        if (kind or got_kind) and kind != got_kind:
            continue
        count_ok = bool(want_count
                        and _advisory_int(row.get("trackCount")) == want_count)
        named = bool(want_album and name == want_album)
        if not (named or (count_ok and _similarity(
                album, str(row.get("collectionName") or ""))
                >= _APPLE_ALBUM_SIMILARITY)):
            continue
        explicit = str(row.get("collectionExplicitness") or "").strip().lower()
        rows.append((explicit == "explicit", count_ok, named, row))
    rows.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    return [row for *_rank, row in rows]


def _apple_value(item):
    """Apple's track payload → 1/0/None.

    `trackExplicitness` is the field that matters. `cleaned` is a CLEANED
    EDITION — an edited master — and `Clean` in `contentAdvisoryRating` marks
    the same edition; neither states anything about the original, so both are
    NO ANSWER. Without that guard the clean re-release of an explicit album
    would quietly relabel every track safe.
    """
    exp = str(item.get("trackExplicitness") or "").strip().lower()
    rating = str(item.get("contentAdvisoryRating") or "").strip().lower()
    if exp == "cleaned" or rating == "clean":
        return None
    if exp == "explicit" or rating == "explicit":
        return 1
    if exp == "notexplicit":
        return 0
    return None


def _apple_collection_value(cid, title="", disc=None, track=None,
                            positions_ok=True, timeout=None, cfg=None):
    """The advisory Apple states for the file inside one collection.

    The file is mapped to its track by discNumber/trackNumber (the position
    decides — another track's rating is not this file's), falling back to a
    title match that refuses variants (`title_matches`): an instrumental or
    karaoke version of the track sits in the same album and is a different
    recording. `positions_ok=False` disables the position mapping entirely:
    that collection does not hold the album's track count, so the same
    disc/track number may be a different song and only a title match may be
    trusted.

    Returns None when the matched track is `cleaned` or carries no rating:
    that collection simply cannot answer, and the CALLER may still ask the
    album's other editions.
    """
    songs = _apple_json("/lookup",
                        {"id": cid, "entity": "song", "limit": 200,
                         "country": _apple_country(cfg)}, timeout=timeout)
    tracks = [item for item in (songs or {}).get("results") or []
              if str(item.get("wrapperType") or "").lower() == "track"]
    want_disc, want_track = _advisory_int(disc), _advisory_int(track)
    if positions_ok and want_disc is not None and want_track is not None:
        for item in tracks:
            if (_advisory_int(item.get("discNumber")) == want_disc
                    and _advisory_int(item.get("trackNumber")) == want_track):
                return _apple_value(item)
    if str(title or "").strip():
        for item in tracks:
            if not title_matches(title, item.get("trackName")):
                continue
            value = _apple_value(item)
            if value is not None:
                return value
            break   # the title matched a `cleaned` entry — that is the answer
    return None


def _apple_album_advisory(artist, album, title="", disc=None, track=None,
                          track_count=None, timeout=None, cfg=None):
    """(value, source) from the album's editions on Apple, or None.

    The long way round, on purpose: artist search → artist id → the artist's
    album list → the EXPLICIT edition first (`_apple_editions`), then the
    cleaned / other editions as a cross-check. Apple's album *search* answers
    one edition only — often the cleaned one, whose tracks then read
    `cleaned`/`notExplicit` even where the master is explicit.

    A `cleaned` match never produces a value — it only proves that THIS
    edition cannot rate the track — so the next candidate is tried (bounded to
    three track lookups) before giving up. Candidates whose track count is
    known and differs are title-matched only, because position alone would
    then be matching a different song.
    """
    artist = str(artist or "").strip()
    album = str(album or "").strip()
    if not artist or not album:
        return None
    want_count = _advisory_int(track_count)
    for row in _apple_editions(artist, album, track_count, cfg,
                               timeout=timeout)[:3]:
        cid = _advisory_int(row.get("collectionId"))
        if not cid:
            continue
        count_ok = bool(want_count
                        and _advisory_int(row.get("trackCount")) == want_count)
        value = _apple_collection_value(
            cid, title, disc, track,
            positions_ok=want_count is None or count_ok,
            timeout=timeout, cfg=cfg)
        if value is not None:
            return (value, "apple-album")
    return None


def _norm_compare(text):
    """Comparison key for titles/artists.

    discovery's own normalizer, deliberately: the app must not grow a second
    opinion about when two titles are the same string.
    """
    from server import discovery
    return discovery.norm(text)


# --------------------------------------------------------------------------- #
# Title variants: an instrumental / karaoke / demo / cover / tribute version
# is NOT the track itself. A per-track value may never be taken from one of
# them (`title_variant_kind`), and a file whose own name says it is a variant
# may never take the original's data. `title_matches` is that guard, and every
# name-based match in this module goes through it.
# --------------------------------------------------------------------------- #
# (kind, pattern) — first hit wins; checked in this order on purpose, so
# "(Karaoke Instrumental Version)" reads instrumental.
_VARIANT_MARKERS = (
    ("instrumental", r"\(\s*instrumental\s*\)"),
    ("instrumental", r"\binstrumental\s+version\b"),
    ("instrumental", r"^\s*instrumental\s*[-–—:]"),
    ("karaoke", r"\bkaraoke\b"),
    ("karaoke", r"\bbacking\s+track\b"),
    ("demo", r"\(\s*demo\s*\)"),
    ("cover", r"\bcover\s+version\b"),
    ("tribute", r"\btribute\b"),
    ("tribute", r"\bmade\s+famous\s+by\b"),
    ("tribute", r"\bin\s+the\s+style\s+of\b"),
)


def title_variant_kind(title):
    """None, or which kind of variant this title says it is.

    "karaoke", "instrumental", "demo", "cover" or "tribute" — the markers are
    the ones releases actually carry: `(Instrumental)`, `Instrumental
    Version`, a leading `Instrumental -`/`Karaoke -`, `Karaoke`, `Backing
    Track`, `(Demo)`, `Cover Version`, `Tribute`, `Made Famous By`, `In The
    Style Of`. Anything else (including a plain title) is None.
    """
    text = str(title or "")
    for kind, pattern in _VARIANT_MARKERS:
        if re.search(pattern, text, re.I):
            return kind
    return None


def title_matches(query_title, candidate_title, *, allow_variant=False):
    """True when `candidate_title` is the same title as `query_title`.

    Normalized comparison (discovery's normalizer), with the variant guard on
    top: a candidate that is an instrumental/karaoke/… version of the query is
    NOT the query, and a variant query does not accept the original either —
    unless `allow_variant`, which ignores the guard and compares names only.
    """
    query = _norm_compare(query_title)
    candidate = _norm_compare(candidate_title)
    if not query or not candidate:
        return False
    if not allow_variant:
        want = title_variant_kind(query_title)
        got = title_variant_kind(candidate_title)
        if (want or got) and want != got:
            return False
    return query == candidate


def merge_advisory(answers, fallback=None):
    """The ONE merge rule for ITUNESADVISORY — `{source: 0|1|2}` → 0|1|2|None.

    The user's policy, in this order:

      1. ANY source states explicit (1) → 1. One source finding it explicit
         settles it, however many others say otherwise.
      2. else ANY source states clean (0) → 0. In the clean family 0 plays the
         same role 1 plays in the explicit family: a source that says the
         track is not explicit outranks a source that only says a clean
         EDITION exists ("most sources say 2 but one says 0" → 0).
      3. else ANY source states a clean edition (2) → 2.
      4. else `fallback` — None by default, which says "nobody stated
         anything" instead of inventing a value. The callers that must write
         something (the import's advisory step) run the rest of the ladder
         first: see `mlo.advisory.decide_advisory`, and `advisory_fallback`
         for the last resort.

    The same rank settles ONE source's several answers before this runs
    (`_strongest_advisory`, which `resolve_advisory_route` applies as each
    answer arrives): a source asked once per pressing states "explicit" for
    the same track its other answer calls clean, and ask order must not
    decide which of the two counts.
    """
    values = {_advisory_int(v) for v in (answers or {}).values()}
    for value in (1, 0, 2):
        if value in values:
            return value
    return fallback


def _strongest_advisory(current, answer):
    """The stronger of one source's two answers — 1 beats 0 beats 2.

    A source can answer more than once for the SAME track: Deezer and Spotify
    are asked once per ISRC the file states, plus every ISRC MusicBrainz holds
    for its recording. A recording whose pressings disagree would otherwise be
    settled by ASK ORDER — the first pressing's clean answer would suppress a
    later pressing's explicit one, and a track Deezer itself flags explicit
    would be written 0. The rank is `merge_advisory`'s own (explicit anywhere
    wins, then a stated clean, then a clean edition), applied to one source's
    answers instead of to the whole map.
    """
    current, answer = _advisory_int(current), _advisory_int(answer)
    if current is None:
        return answer
    if answer is None:
        return current
    for value in (1, 0, 2):
        if value in (current, answer):
            return value
    return current


def _record_advisory(answers, answer):
    """Record one source's ``(value, source)`` answer, keeping its strongest.

    The one place an answer enters the per-source map, so "which of a source's
    several answers counts" is answered once — see `_strongest_advisory`.
    """
    key, value = answer[1], answer[0]
    answers[key] = _strongest_advisory(answers.get(key), value)


def _winning_source(answers, value):
    """The first source (in ask order) whose answer is the merged `value`."""
    for source, answer in (answers or {}).items():
        if _advisory_int(answer) == value:
            return source
    return None


def _itunes_song_advisory(title, artist="", timeout=None):
    """(value, source) from Apple's song search — the LAST resort.

    A search hit is a candidate, not an identity, so a hit is only accepted
    when `title_matches` says it is the file's title AND its own artist credit
    contains the file's artist. `title_matches` is also what refuses an
    instrumental/karaoke/cover hit (and the reverse): search answers those
    generously, and a variant's rating is not this track's. A `cleaned` hit is
    never accepted (see `_apple_value`).
    """
    title = str(title or "").strip()
    if not title:
        return None
    data = _advisory_json(f"{_ITUNES_LOOKUP}/search",
                          {"term": " ".join(t for t in (artist, title) if t),
                           "entity": "song", "limit": 10}, timeout=timeout)
    want_artist = _norm_compare(artist)
    for item in (data or {}).get("results") or []:
        if not title_matches(title, item.get("trackName")):
            continue
        if want_artist and want_artist not in _norm_compare(item.get("artistName")):
            continue
        value = _apple_value(item)
        if value is not None:
            return (value, "itunes-song")
    return None


def _spotify_configured(cfg):
    cfg = cfg or {}
    return bool(str(cfg.get("spotify_client_id") or "").strip()
                and str(cfg.get("spotify_client_secret") or "").strip())


def _discogs_parental_advisory(artist, album, cfg=None):
    """(1, "discogs-parental") when Discogs lists a Parental Advisory format.

    Album-level and WEAK — a sticker on the edition Discogs matched, not a
    per-track statement — so it ranks LAST and can only ever add an explicit
    signal: it never clears a track it did not rate. Requires `discogs_token`,
    like every Discogs route; without one (or without a hit) it answers None.
    """
    from server import discovery

    if not str((cfg or {}).get("discogs_token") or "").strip():
        return None
    if not (artist and album):
        return None
    try:
        flagged = discovery.discogs_parental_advisory(artist, album, cfg)
    except Exception:
        return None
    return (1, "discogs-parental") if flagged else None


# The tags a track's own video origin is recorded in: the video pipeline
# writes SOURCE, and YOUTUBEID/VIDEOID are the explicit spellings.
_YOUTUBE_TAG_KEYS = ("YOUTUBEID", "YOUTUBE_ID", "VIDEOID", "SOURCE")
_YOUTUBE_ID_RX = re.compile(r"[A-Za-z0-9_-]{11}")


def youtube_video_id(tags):
    """The YouTube id a track's OWN tags record, or "".

    Only a recorded origin counts: an `11`-character id, or a URL carrying
    `v=`/`/shorts/`/`youtu.be/`. Nothing here searches YouTube — a search's
    first hit is not this track, and rating the wrong video is precisely the
    failure this rule exists to prevent.
    """
    if not isinstance(tags, dict):
        return ""
    for key in _YOUTUBE_TAG_KEYS:
        value = str(tags.get(key) or "").strip()
        if not value:
            continue
        if "youtube.com" in value or "youtu.be" in value:
            match = re.search(r"(?:v=|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})",
                              value)
            if match:
                return match.group(1)
            continue
        if _YOUTUBE_ID_RX.fullmatch(value):
            return value
    return ""


def youtube_age_advisory(video_id, cfg=None):
    """(1, "youtube-age") when YouTube itself flags the video 18+, else None.

    Only asked about a video the caller already knows belongs to this track
    (see `youtube_video_id`) or passes itself. `age_limit >= 18` states
    explicit; anything else — a normal video, an unavailable one, yt-dlp not
    installed, YouTube disabled — states NOTHING, because "no age gate" is not
    a statement about the music.
    """
    from server import youtube

    ident = str(video_id or "").strip()
    if not _YOUTUBE_ID_RX.fullmatch(ident):
        return None
    if not youtube.enabled(cfg):
        return None
    module = youtube._load_ytdlp()
    if module is None:
        return None
    try:
        with module.YoutubeDL(youtube._ydl_opts(cfg)) as ydl:
            info = ydl.extract_info(youtube._watch_url(ident), download=False)
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    try:
        limit = int(info.get("age_limit") or 0)
    except (TypeError, ValueError):
        return None
    return (1, "youtube-age") if limit >= 18 else None


def _isrc_codes(isrc):
    """Every ISRC the caller named, in ask order, deduplicated.

    `AudioFile.get_tag` reads repeated fields back "; "-joined (several ISRCs
    on one recording arrive as "A; B"), and a caller may hand over a list of
    its own, so both spellings mean the same thing here: ask EVERY one of
    them. Taking the first alone made the file's own ISRC tag contribute one
    code while this route's contract says "every ISRC the track has".
    """
    raw = list(isrc) if isinstance(isrc, (list, tuple, set)) else [isrc]
    codes = []
    for value in raw:
        for piece in str(value or "").split(";"):
            code = piece.strip()
            if code and code.upper() not in {c.upper() for c in codes}:
                codes.append(code)
    return codes


def resolve_advisory_route(isrc="", recording_mbid="", title="", artist="",
                           album="", disc=None, track=None, track_count=None,
                           cfg=None, youtube_id="", tags=None):
    """{"value": 0|1|2|None, "source": key|None, "checked": [key, ...],
        "answers": {key: 0|1}}.

    EVERY applicable source is asked, in one pass, and none of them
    short-circuits:

      1. Deezer by ISRC, then 2. Spotify by ISRC — for EVERY ISRC the track
         has: every code on the file's own ISRC tag (see `_isrc_codes`, which
         is also what a caller's list of codes means) plus every ISRC
         MusicBrainz holds for its recording (Apple serves no ISRC lookup, so
         it is reached by edition instead and is still cross-referenced with
         both);
      3. Apple's artist route — artist → its album list → the explicit
         edition → track (disc/track, then a title match);
      4. Apple's song search — title match only;
      5. Discogs' edition (`format`/`description` "Parental Advisory", token
         only) and 6. YouTube's 18+ gate for a video the track records — two
         LAST, explicit-only, album/edition-level signals that never clear a
         track and stay silent when their input is absent.

    `answers` is the per-source map in ask order, `source` the first source
    that stated the merged value, `checked` every route that was asked. A
    source asked more than once — one ISRC per pressing is the normal case —
    contributes its STRONGEST answer (`_strongest_advisory`), so the later
    pressing's explicit flag cannot lose to the earlier one's clean answer.
    The merge itself is `merge_advisory` — one rule, one place — so `value` is
    1, 0, 2, or None, and None (an empty `answers`) is the only signal that
    nobody stated anything: the caller decides what to do about that
    (`mlo.advisory.decide_advisory` is that decision, used by the import).
    """
    if cfg is None:
        try:
            from mlo.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    codes = _isrc_codes(isrc)
    if recording_mbid:
        # The file's own ISRC is asked first, then every ISRC MusicBrainz
        # holds for the recording — both ISRC sources see all of them.
        for code in recording_isrcs(recording_mbid):
            if code.upper() not in {c.upper() for c in codes}:
                codes.append(code)
    spotify_on = _spotify_configured(cfg)
    checked = []
    answers = {}
    for code in codes:
        checked.append("deezer-isrc")
        answer = _advisory_cached(("deezer-isrc", code.upper()),
                                  lambda c=code: _deezer_advisory(c))
        if answer is not None:
            _record_advisory(answers, answer)
        if spotify_on:
            checked.append("spotify-isrc")
            answer = _advisory_cached(("spotify-isrc", code.upper()),
                                      lambda c=code: _spotify_advisory(c, cfg))
            if answer is not None:
                _record_advisory(answers, answer)
    if artist and album:
        checked.append("apple-album")
        key = ("apple-album", _norm_compare(artist), _norm_compare(album),
               _advisory_int(disc), _advisory_int(track), _norm_compare(title))
        answer = _advisory_cached(key, lambda: _apple_album_advisory(
            artist, album, title, disc, track, track_count, cfg=cfg))
        if answer is not None:
            _record_advisory(answers, answer)
    if title:
        checked.append("itunes-song")
        key = ("itunes-song", _norm_compare(artist), _norm_compare(title))
        answer = _advisory_cached(key,
                                  lambda: _itunes_song_advisory(title, artist))
        if answer is not None:
            _record_advisory(answers, answer)
    # The last two are extra EXPLICIT-only signals, both album/edition level
    # and both silent when their input is missing: a Discogs Parental
    # Advisory sticker (a configured token only), and YouTube's own 18+ gate
    # for a video the track already records. Neither can clear a track.
    if artist and album and str((cfg or {}).get("discogs_token") or "").strip():
        checked.append("discogs-parental")
        key = ("discogs-parental", _norm_compare(artist), _norm_compare(album))
        answer = _advisory_cached(
            key, lambda: _discogs_parental_advisory(artist, album, cfg))
        if answer is not None:
            _record_advisory(answers, answer)
    video = str(youtube_id or "").strip() or youtube_video_id(tags)
    if video:
        checked.append("youtube-age")
        answer = _advisory_cached(("youtube-age", video),
                                  lambda: youtube_age_advisory(video, cfg))
        if answer is not None:
            _record_advisory(answers, answer)
    value = merge_advisory(answers)
    return {"value": value, "source": _winning_source(answers, value),
            "checked": checked, "answers": answers}


def resolve_advisory(isrc="", recording_mbid="", **context):
    """ITUNESADVISORY for one track — the merged answer of every source.

    Deezer by ISRC, Spotify by ISRC when configured, Apple's artist→album
    route and Apple's song search are all asked (see
    `resolve_advisory_route`), and the merged value follows `merge_advisory`:
    1 when ANY of them says explicit, else 0 when any says clean, else 2 when
    any says clean edition, else None — nobody stated anything, which is a
    caller's decision, not a value to write.

    `context` may carry `title`, `artist`, `album`, `disc`, `track`,
    `track_count` and `cfg` (the Apple routes need them; the ISRC sources do
    not). `isrc` may be the file's own ISRC tag exactly as `AudioFile.get_tag`
    reads it — several codes "; "-joined — or a list of codes; every one of
    them is asked. MusicBrainz supplies the ISRCs when the caller has only a
    recording ID. Use `resolve_advisory_route` when the per-source answers
    matter too.
    """
    return resolve_advisory_route(isrc=isrc, recording_mbid=recording_mbid,
                                  **context)["value"]


def release_advisories(mbid, sources=None, answers=None):
    """{f'{disc}:{position}': 0|1} for every track of a release.

    Every track gets the merged value of the sources that were asked (0 when
    none of them stated anything — the merge rule, not a gap to fill in).
    `sources` (optional) is filled with the source that stated each value and
    `answers` (optional) with the whole per-track `{source: 0|1}` map, so a
    caller can report provenance alongside the value.

    Every ISRC the release states for a track is asked, not just the first:
    MusicBrainz lists one per pressing, and the route's own contract (and
    `_isrc_codes`) is that all of them are put to the ISRC sources. Taking
    `isrcs[0]` alone let a clean first pressing hide the explicit second one —
    the same miss the file's own ISRC tag had.
    """
    release = release_lookup(mbid)
    fallback_artist = next((a.get("name") for a in release.get("artists") or []
                            if a.get("name")), "")
    track_count = len(release.get("media") or [])
    out = {}
    for t in release.get("media") or []:
        key = f"{int(t.get('disc') or 1)}:{int(t.get('position') or 0)}"
        route = resolve_advisory_route(
            isrc=t.get("isrcs") or "",
            recording_mbid=t.get("recording_mbid") or "",
            title=t.get("title") or "",
            artist=t.get("artist_credit") or fallback_artist,
            album=release.get("title") or "",
            disc=t.get("disc"),
            track=t.get("position"),
            track_count=track_count,
        )
        if route["value"] is not None:
            out[key] = route["value"]
            if sources is not None and route["source"]:
                sources[key] = route["source"]
            if answers is not None and route.get("answers"):
                answers[key] = route["answers"]
    return out


def release_group_genres(rg_mbid):
    try:
        data = mb_get_cached(f"release-group/{rg_mbid}", {"inc": "genres", "fmt": "json"})
        return _genres(data)
    except Exception:
        return []


def artist_genres(artist_mbid):
    try:
        data = mb_get_cached(f"artist/{artist_mbid}", {"inc": "genres", "fmt": "json"})
        return _genres(data)
    except Exception:
        return []


def recording_genres(recording_mbid):
    """A recording's OWN genres — the track-level sibling of `artist_genres`.

    MusicBrainz states genres on the recording entity too, and a track page
    has a recording id in hand, so this asks about the track itself rather than
    its artist. Most recordings state none (genres are voted at artist and
    release-group level far more often), so a caller reads an empty list as
    "nothing stated HERE" and falls back to the artist.
    """
    try:
        data = mb_get_cached(f"recording/{recording_mbid}", {"inc": "genres", "fmt": "json"})
        return _genres(data)
    except Exception:
        return []


def genre_cascade(release, limit=None):
    """Cascading genre import: track -> release -> release-group -> artist.

    Genres are merged across levels (deduped, in popularity order, in
    MusicBrainz's own spelling) and capped at `limit` per track. limit=None
    imports everything. Returns per-track genres plus the fallback chain used
    for each track. The caller that writes these to a file canonicalizes them
    (`_write_album_genres` in server.main), so the family slot and the cap are
    applied once, by the one writer."""
    rg = release.get("release_group_id")
    rg_genres = release_group_genres(rg) if rg else []
    artist_genres_all = []
    for a in release.get("artists", []):
        if a.get("mbid"):
            artist_genres_all.extend(artist_genres(a["mbid"]))
    artist_genres_all = list(dict.fromkeys(artist_genres_all))
    release_genres = release.get("genres", [])

    per_track = []
    for trk in release.get("media", []):
        ordered = []
        sources = []
        for level, lst in (
            ("track", trk.get("genres") or []),
            ("release", release_genres),
            ("release-group", rg_genres),
            ("artist", artist_genres_all),
        ):
            if lst and not sources:
                sources.append(level)
            for g in lst:
                if g not in ordered:
                    ordered.append(g)
        merged = ordered[:limit] if limit else ordered
        per_track.append({
            "position": trk["position"],
            "disc": trk["disc"],
            "title": trk["title"],
            "genres": merged,
            "source": sources[0] if sources else None,
            "levels_used": sources,
        })
    return {
        "per_track": per_track,
        "levels": {
            "track": any(t.get("genres") for t in release.get("media", [])),
            "release": bool(release_genres),
            "release_group": bool(rg_genres),
            "artist": bool(artist_genres_all),
        },
    }


# --------------------------------------------------------------------------- #
# RateYourMusic (scraped — no public API)
# --------------------------------------------------------------------------- #
# VERIFIED, on the machines this app has run on: rateyourmusic.com refuses an
# automated client outright — Cloudflare answers the TLS handshake/challenge
# and both urllib and curl get no usable page. A plain scraper therefore does
# NOT work from an unattended install and cannot be made to: this source is
# unverifiable from automated access, and it is kept FIRST in the genre chain
# only because the order is the user's own preference.
#
# What a user must do to make it work: paste the `Cookie` header of a
# logged-in rateyourmusic.com browser tab into Settings (`rym_cookie`); that
# cookie carries Cloudflare's cf_clearance for their IP/session, and with it
# the same requests below do return real pages. WITHOUT one the genre chain
# does not even ask: a source with no credential is skipped before any request
# (`_genre_source_skip`), and the report names `rym_cookie` as what is missing.
# WITH a cookie that RYM then refuses — a stale paste, or a datacenter IP,
# where Cloudflare blocks regardless — this module sends browser-like headers,
# gets nothing, logs ONE line and the chain falls through to the next source.
# It never invents a genre from a partial page.
#
# A good cookie is not enough by itself, and that is the difference this
# module is built around: the WAF also hands out cookies of its OWN on a plain
# top-level navigation, and it refuses a cold client that never made one. So
# the paste seeds a cookie jar (`_rym_cookiejar`) that every request carries,
# the FIRST request under a paste is ONE warm-up navigation to RYM's home page
# (`_rym_warm`) whose Set-Cookie joins that jar, and every request sends the
# full Chrome header set (client hints and fetch metadata included) — see
# RYM_HEADERS and `_rym_headers`. None of it is a credential or a trick: it is
# what a browser does before a page it is allowed to read.
#
# A cookie is the better route when there is one — a live page states the
# artist's genres as they are today — but it is no longer the ONLY one. The
# Wayback Machine keeps copies of these pages and serves them to an unattended
# client, so a release RYM will not serve directly is read from its newest
# archived snapshot instead (`_rym_archive_get`). That answer keeps
# `rateyourmusic` as its source and says in the report that it came from an
# archived snapshot, with the capture's date when the snapshot states one; the
# route is only ever used when the live site could not answer (no cookie, a
# latched refusal, or a refusal during this call). `rym_archive_fallback`
# (Settings → Discovery, default ON) turns it off — off, a missing cookie means
# exactly what it always did: the source contributes nothing and is reported as
# skipped.
#
# Scraping is polite and cheap: one request per second, and a 30-day disk
# cache under <music>/.mlo/data/rym_cache so repeat imports never re-fetch.
# The archive route shares BOTH: it goes through the same throttle and the same
# 30-day cache, so a fallback costs a couple of seconds once per album and
# nothing on the next import.
# (Deezer and Apple, by contrast, are keyless public APIs and their advisory
# routes are verified working — see ADVISORY_SOURCES.)
#
# That ONE line is also the whole cost: a refusal latches the source off
# (`_rym_warned`), and every later request returns "no answer" without going
# out (see `_rym_get`), so a blocked RYM costs the import ONE probe instead
# of a walk through every slug candidate of every album. The latch is keyed
# to the credential that earned it and expires, though — see `_rym_blocked` —
# so a cookie the user has since replaced (or a block that lifted on its own)
# is asked again instead of looking blocked until the backend is restarted.
# And the LINKS the app tags from RYM come from MusicBrainz, which states the
# same pages as url relations — a blocked RYM no longer costs them at all
# (see "RYM link
# resolution" below).
RYM_BASE = "https://rateyourmusic.com"
# What a normal Chrome window sends, field for field. Everything here is a
# header a browser ALWAYS sends, so nothing in it is a claim the client cannot
# back up: a bare library UA — or the UA without the client hints, the fetch
# metadata and the wide Accept set that go with it — is what the WAF filters
# on, and a request that succeeds with a good cookie must not be refused for
# looking like an unattended scraper.
RYM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Referer": RYM_BASE + "/",
    "Upgrade-Insecure-Requests": "1",
    # Client hints. Chrome sends the same three on every request; the UA above
    # without them is a combination no real Chrome ever produces.
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", '
                 '"Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    # Fetch metadata: a document navigation, same-site (RYM is the Referer).
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
}
RYM_MIN_INTERVAL = 1.0        # seconds between requests, per their etiquette
RYM_CACHE_TTL = 30 * 86400.0  # genre data moves slowly
# A whole RYM lookup — the slug ladder included — never runs longer than this.
# One hung socket (each request carries a 20s timeout) or a long run of
# candidates must not hold an import behind a source that is not answering.
RYM_MAX_WALL = 20.0
# A refusal is a politeness guard for a run, never a verdict on the cookie:
# it stops standing after this long, so a session that went stale, a transient
# block, or a server that has been up for days all recover without a restart.
RYM_BLOCK_TTL = 300.0
# 429/5xx/timeout are transient — RYM is busy or rate-limiting, not refusing —
# so each gets a retry, spaced by the same 1 req/s as any other request. A
# Cloudflare interstitial is the refusal itself and is NOT retried.
RYM_RETRIES = 2
# The ARCHIVE route (see RYM_BASE): the Wayback Machine serves the copies it
# made of these pages to anybody, which is what makes genre importing work on
# an install with no `rym_cookie` at all.
#
# `id_` asks for the capture's own bytes. VERIFIED, 2026-09: `web/2id_/<url>`
# redirects to the newest capture (`/web/<timestamp>id_/<url>`) and returns it
# WITHOUT Wayback's toolbar and WITHOUT its rewritten links. That matters more
# than it looks — the wrapper rewrites every href to
# `https://web.archive.org/web/<timestamp>/https://rateyourmusic.com/genre/...`
# and `_RYM_GENRE_RE` reads `href="/genre/<slug>/"`, so a wrapped page looks
# like a page that states no genres at all. `_rym_archive_body` strips the
# wrapper anyway, in case one comes back.
RYM_ARCHIVE_BASE = "https://web.archive.org/web"
# The capture index, asked only when the newest capture is not a page: RYM's
# WAF answers the CRAWLER too, so recent captures of a popular release page are
# often the Cloudflare interstitial replayed with its original 403. VERIFIED,
# 2026-09 (a 2010 release): `2id_` answered 403 "Just a moment…", while CDX
# listed the page's 200 captures from 2021 — the real thing. `statuscode:200`
# drops the junk captures, `collapse=digest` drops copies repeating content,
# `output=json` gives one row per capture, and `limit` closes it with the
# NEWEST few (CDX lists ascending).
RYM_ARCHIVE_INDEX = "https://web.archive.org/cdx/search/cdx"
RYM_ARCHIVE_LATEST = "2id_"    # the newest capture, in its own bytes
RYM_ARCHIVE_TRIES = 3          # indexed captures tried after the newest one
# Archive requests do NOT wear the RYM header set: a Referer of
# rateyourmusic.com and `Sec-Fetch-Site: same-origin` on a request to
# web.archive.org is a shape no browser produces. The archive is a public
# service that asks for nothing but a UA saying who is calling, and this is it.
RYM_ARCHIVE_HEADERS = {
    "User-Agent": USER_AGENT + " (genre fallback)",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}
# How many release-page spellings the ARCHIVE walk tries. The live ladder can
# afford four (`_rym_release_paths`) because a 404 is one cheap request; an
# archive answer costs a snapshot fetch and — when the newest capture is not a
# page — an index request behind it. The page MusicBrainz states (`album_url`)
# already resolves the common case without guessing a slug at all.
RYM_ARCHIVE_PATHS = 2
_rym_lock = threading.Lock()
_rym_last = 0.0
_rym_warned = False           # RYM refused since `_rym_blocked_at`, under
_rym_blocked_cookie = ""      # this cookie — the latch `_rym_get` reads via
_rym_blocked_at = 0.0         # `_rym_blocked` (the test harnesses reset these)
_rym_failures = 0             # how often RYM failed to answer at all
_rym_jar = None               # httpx.Cookies for `_rym_jar_paste`: the user's
_rym_jar_paste = None         # own pairs PLUS whatever RYM's Set-Cookie added
_rym_warmed = None            # the paste whose warm-up navigation has run
_rym_last_info = {}           # what RYM last answered — `rym_last_response()`
_rym_route = {}               # which ROUTE answered (a live page or an
                              # archived snapshot, with its timestamp and URL):
                              # `_rym_note` reads it, and the genre chain
                              # clears it before each call so the note belongs
                              # to the answer it is reported next to
# Cloudflare's interstitial instead of a release page. Cached or parsed it
# would be an empty page at best, so it counts as unreachable.
_RYM_CHALLENGE_RE = re.compile(
    r"just a moment|cf-?challenge|_cf_chl|checking your browser|"
    r"enable javascript and cookies", re.I)
# RYM renders genres, styles and descriptors as /genre/<slug> anchors, in that
# order on a release page (primary genres first) — the anchors are the whole
# scrape, so a markup change degrades to "no genres", never to wrong ones.
#
# The slug class is NOT just lowercase-and-dashes, and the path closes with a
# slash: RYM writes the genre's own name into it, `+` for every space, capitals
# kept — VERIFIED against an archived copy of a real release page
# (web.archive.org/web/20210325091401/https://rateyourmusic.com/release/album/
#  grouper/dragging-a-dead-deer-up-a-hill/): `href="/genre/Psychedelic+Folk/"`,
# `href="/genre/Dream+Pop/"`, `href="/genre/Ethereal+Wave/"`. The old class
# matched neither the `+` nor the trailing `/`, so it found NO genre on a real
# page at all (the fixtures here used a slash-less anchor the site never
# serves) — and a scrape that finds nothing is indistinguishable from a page
# that states nothing.
_RYM_GENRE_RE = re.compile(
    r'href="/genre/([A-Za-z0-9%+_./\-]+)"[^>]*>([^<]{1,60})</a>', re.I)
# Descriptors ("Concept Album", "Death", "Lo-Fi") are RYM's other album-level
# classification. They are not genres, but they are the only classification a
# release page carries when it carries no /genre/ anchor, so they are read as
# a LAST-RESORT album-level answer and labelled as such in the provenance.
_RYM_DESCRIPTOR_RE = re.compile(
    r'href="/descriptor/([A-Za-z0-9%+_./\-]+)"[^>]*>([^<]{1,60})</a>', re.I)
# The track list, in the two shapes RYM serves. The desktop page is a
# `<tr class="tracklist_row">` table; the page an unauthenticated client gets
# is the same list as `<div class="tracklist_line">` — VERIFIED in that same
# archived copy, whose rows are
# `<div class="tracklist_line"><span class="tracklist_num">1</span>
#  <span class="tracklist_title"><span><span class="rendered_text">Disengaged`
# so each pattern accepts either spelling. A row that carries its own /genre/
# anchor (some releases do tag a track) is that track's genre — everything
# else is album-level and says so.
_RYM_TRACK_ROW_RE = re.compile(
    r"<(?:tr|div)[^>]*tracklist_(?:row|line)[^>]*>(.*?)</(?:tr|div)>", re.I | re.S)
_RYM_TRACK_NUM_RE = re.compile(r"tracklist_(?:track_)?num[^>]*>\s*(\d+)", re.I)
_RYM_TRACK_TITLE_RE = re.compile(
    r"tracklist_(?:track_)?title[^>]*>(.*?)</(?:t[dh]|span)>", re.I | re.S)
_RYM_ARTIST_LINK_RE = re.compile(r'href="(/artist/[^"]+)"', re.I)


def _rym_labels(pattern, html):
    """Trimmed, de-duplicated anchor labels for one RYM link pattern.

    Entities are decoded: a genre anchor's text is HTML ("R&amp;B", "Lo&#45;Fi"
    style spellings appear in the wild), and the name goes straight into the
    merge, so `&amp;` must not travel as its own genre.
    """
    out = []
    for _slug, label in pattern.findall(html or ""):
        name = _html.unescape(re.sub(r"\s+", " ", label)).strip()
        if name and name.lower() not in {g.lower() for g in out}:
            out.append(name)
    return out


def _rym_genres_from(html):
    """Genres (the /genre/ anchors) from a RYM release or artist page."""
    return _rym_labels(_RYM_GENRE_RE, html)


def _rym_tracks_from(html):
    """[{position, title, genres}] from a release page's track list.

    RYM states genres per RELEASE, not per track, so `genres` is normally
    empty and the release's own list is what applies (the caller marks that
    `level: album`). A row that does carry a /genre/ anchor is used for that
    row, mapped onto our tracks by position first and by title second — and a
    title that matches nothing is dropped rather than guessed at.

    The title is entity-decoded for the same reason the genres are: RYM writes
    "Heavy Water / I&#39;d Rather Be Sleeping", and the title is what maps a
    row onto OUR track when the numbering does not line up.
    """
    out = []
    for row in _RYM_TRACK_ROW_RE.findall(html or ""):
        match = _RYM_TRACK_TITLE_RE.search(row)
        if not match:
            continue
        title = _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "",
                                                          match.group(1)))).strip()
        if not title:
            continue
        num = _RYM_TRACK_NUM_RE.search(row)
        out.append({
            "position": int(num.group(1)) if num else len(out) + 1,
            "title": title,
            "genres": _rym_labels(_RYM_GENRE_RE, row),
        })
    return out


def _rym_cookie(cfg=None):
    """The user's `rym_cookie` — a browser session cookie, or "".

    Read from the live config on every request so pasting one into Settings
    takes effect without a restart. An empty cookie is not an error: it is
    the documented "RYM is skipped" state.

    The paste is normalised: people copy the value from wherever their
    browser shows it, so a leading "Cookie:" (the devtools row label), a
    wrapped line, or the newlines a terminal adds must not silently produce a
    header RYM refuses. Every separator is re-emitted as the single "; " the
    header grammar wants."""
    try:
        if cfg is None:
            from mlo.config import load_config
            cfg = load_config()
        raw = str((cfg or {}).get("rym_cookie") or "")
    except Exception:
        return ""
    raw = raw.replace("\r", "\n")
    if "\n" in raw:
        # A copied header wraps: keep the pairs, drop the line breaks.
        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
        raw = "; ".join(ln.rstrip(";") for ln in lines)
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    pairs = [p.strip() for p in raw.split(";") if p.strip()]
    return "; ".join(pairs)


def _rym_cookiejar(cfg=None):
    """The cookie jar for the current paste: the user's pairs, plus every
    Set-Cookie RYM has answered with since (see `_rym_warm`).

    The credential travels in THIS jar rather than in a `Cookie:` header for
    one reason: the WAF's own cookies join it on the warm-up, and a header
    built once would never carry them. Rebuilt whenever the paste changes —
    the value the user pastes in Settings is the whole credential — so nothing
    from a refused cookie leaks into the next one."""
    global _rym_jar, _rym_jar_paste
    paste = _rym_cookie(cfg)
    if _rym_jar is None or _rym_jar_paste != paste:
        jar = httpx.Cookies()
        for pair in paste.split(";"):
            name, _, value = pair.strip().partition("=")
            if name:
                # RYM's own host: the paste is a rateyourmusic.com session and
                # must never be sent anywhere else by accident.
                jar.set(name, value, domain="rateyourmusic.com")
        _rym_jar, _rym_jar_paste = jar, paste
    return _rym_jar


def _rym_headers(warm=False):
    """The full Chrome header set — see RYM_HEADERS.

    The cookie is NOT part of this: it belongs to the jar (`_rym_jar`), which
    is what lets the WAF's own cookies ride along. `warm=True` is the
    top-level navigation to RYM's home page, which a browser sends with no
    Referer, `Sec-Fetch-Site: none` and `Sec-Fetch-User: ?1` — a request that
    claims RYM referred it while asking for RYM's root is a shape no browser
    produces."""
    headers = dict(RYM_HEADERS)
    if warm:
        headers.pop("Referer", None)
        headers["Sec-Fetch-Site"] = "none"
        headers["Sec-Fetch-User"] = "?1"
    return headers


def _rym_fetch(url, params, headers, jar):
    """One GET at RYM's etiquette: the 1 req/s spacing and the request itself
    under the same lock, so the Sources panel's parallel probes (genre and
    links rows) can never have two requests in flight at once."""
    global _rym_last
    with _rym_lock:
        wait = RYM_MIN_INTERVAL - (time.time() - _rym_last)
        if wait > 0:
            time.sleep(wait)
        _rym_last = time.time()
        return httpx.get(url, params=params or {}, headers=headers,
                         cookies=jar, timeout=20.0, follow_redirects=True)


def _rym_warm(cfg=None):
    """ONE navigation to RYM's home page per cookie paste, before the first
    request that paste makes anywhere else.

    This is the difference between a page and an interstitial for a cookie
    that is otherwise fine: the pasted session cookie (cf_clearance) is the
    user's half of the handshake, but the WAF also hands out cookies of its
    own (`__cf_bm`, `_cfuvid`) on a plain top-level navigation, and it treats
    a cold client that never made one as a scraper. Those Set-Cookie values go
    into the jar, so the request the caller actually wanted carries the whole
    set Chrome would send.

    Best effort, and per PASTE rather than per call: the request below reports
    its own failure if RYM still refuses (and no second warm-up can rescue a
    paste that is stale), so nothing is raised or logged here. Two probes
    starting at the same moment can both see an unwarmed paste and both
    navigate once — a duplicated polite request, never a wrong answer."""
    global _rym_warmed
    paste = _rym_cookie(cfg)
    if not paste or _rym_warmed == paste:
        return
    try:
        r = _rym_fetch(RYM_BASE + "/", None, _rym_headers(warm=True),
                       _rym_cookiejar(cfg))
    except httpx.HTTPError:
        # Marked warmed only AFTER the navigation comes back: latching first
        # meant one timeout (a flaky DNS, a busy WAF) disabled the warm-up for
        # that paste until the app restarted, and every later RYM request went
        # out cold — which is the state the challenge detection exists for.
        return
    _rym_warmed = paste
    # `Response.cookies` is httpx's own parse of this answer's Set-Cookie.
    _rym_cookiejar(cfg).update(getattr(r, "cookies", None) or {})


def _rym_blocked(cfg=None):
    """Whether an earlier refusal still stands for the CURRENT credential.

    The latch is what makes a blocked RYM cost an import one probe instead of
    a walk through every slug candidate of every album. It must not outlive
    its cause, though: a refusal found with a different cookie — the user
    pasted a fresh one in Settings — or one older than RYM_BLOCK_TTL is
    forgotten here, so the next lookup really asks RYM again instead of
    answering "blocked" from a state the credential has moved past."""
    global _rym_warned
    if not _rym_warned:
        return False
    if _rym_blocked_cookie != _rym_cookie(cfg) or \
            time.time() - _rym_blocked_at > RYM_BLOCK_TTL:
        _rym_warned = False
        return False
    return True


def _rym_clear_block():
    """Forget a refusal — what a user-initiated probe does before it asks."""
    global _rym_warned
    _rym_warned = False


def _rym_record(status, url, challenge=False, reason=""):
    """What RYM last answered, in module state — `rym_last_response()` serves
    it to the Sources panel.

    `status` None means nothing answered at all (a timeout, a refused
    connection); `challenge` is whether the Cloudflare interstitial was in the
    body; `reason` is the sentence `_rym_reason` wrote for a refusal ("" when
    RYM answered); `url` and `at` say WHICH request got this answer, which is
    what turns "RYM refused the request" into something a user can act on."""
    global _rym_last_info
    _rym_last_info = {"status": status, "challenge": bool(challenge),
                      "url": str(url or ""), "at": time.time(),
                      "reason": str(reason or "")}


# How to fix a refusal, in one place: the health row and the last-response
# record both quote it, so the two cannot drift apart. Kept short and free of
# the site name: it lands in a log line that is already long, and the panel
# prints it next to the field it names.
_RYM_HOWTO = ("paste the whole `Cookie:` header (every name=value pair, not "
              "just one token) into Settings → Discovery → rym_cookie")


def _rym_reason(cfg=None, status=None, challenge=False, tries=1):
    """WHY RYM would not answer. Five cases, five different fixes, so one
    "403/challenge" sentence is not enough:

      * no cookie configured — nothing the WAF could accept was ever sent;
      * a challenge page on a 200 — the cookie is stale (or belongs to another
        network), and a fresh paste is what fixes it;
      * a 403 with NO challenge marker — the WAF refused the client outright:
        a datacenter/VPN network is blocked whatever the cookie says;
      * a 429 — RYM is throttling this network; and
      * a 5xx — RYM itself is unwell. The last two are transient, and both
        were already retried (RYM_RETRIES) before this sentence was written.

    The cookie advice is only appended where a cookie could help: telling a
    user to paste one while RYM answers 503 sends them after the wrong thing."""
    cookie = _rym_cookie(cfg)
    if challenge:
        why = "Cloudflare challenge instead of a page (HTTP 200) — " + \
            ("the rym_cookie has expired or is not for this network" if cookie
             else "no rym_cookie is set")
        return why + "; " + _RYM_HOWTO
    if status == 429:
        return ("HTTP 429 — RYM is throttling this network (retried %dx)"
                % tries)
    if status is not None and status >= 500:
        return "HTTP %d — RYM server error (retried %dx)" % (status, tries)
    if status == 403:
        why = ("HTTP 403 refused without a Cloudflare challenge — the WAF is "
               "blocking this network")
        why += (", or the cookie is for another session" if cookie
                else ", and no rym_cookie is set")
        return why + "; " + _RYM_HOWTO
    return "HTTP %s refused the request" % status


def rym_last_response():
    """RYM's last answer, for the Sources panel — {} before the first request.

    `status` is the HTTP status (None = nothing answered), `challenge` whether
    the Cloudflare interstitial was in the body, `url` what was asked for,
    `at`/`at_iso` when, and `reason` why it was refused ("" when RYM
    answered). It exists because `_rym_get`'s latch makes a refusal cost ONE
    probe: the log line is over by the time the user looks, and this is what
    the panel prints instead of a second guess at what went wrong."""
    info = dict(_rym_last_info)
    if info:
        info["at_iso"] = datetime.fromtimestamp(info["at"], timezone.utc) \
            .isoformat(timespec="seconds")
    return info


def _rym_note(cfg=None, answered=False):
    """RYM's line in the genre chain's report — "" when there is nothing to say.

    RYM is the one source with a SECOND route, so "no data" is never enough for
    it: the user has to be able to tell "the archive answered, and this is a
    snapshot captured 2021-03-25" from "neither route answered, and here is the
    setting that fixes the live one". `answered` is the caller's own fact
    (whether this source contributed anything); WHICH route answered comes from
    `_rym_route`, written by the request that answered and cleared by the caller
    before it asks.
    """
    route = str(_rym_route.get("route") or "")
    if answered:
        if route != "archive":
            return ""      # a live page answered: nothing to explain
        when = _rym_archive_when(_rym_route.get("snapshot") or "")
        why = ("the live site needs a rym_cookie" if not _rym_cookie(cfg)
               else "the live page refused this request")
        return ("from an archived snapshot%s on web.archive.org — %s"
                % (f" captured {when}" if when else "", why))
    if not _rym_archive_on(cfg):
        return ""          # the skip/failure line for this source already fits
    if not _rym_cookie(cfg):
        return ("no rym_cookie in Settings → Discovery, and no archived "
                "snapshot of this page answered (RateYourMusic refuses an "
                "automated client without one)")
    return ("RateYourMusic refused this cookie and no archived snapshot of "
            "this page answered — set a fresh rym_cookie in Settings → "
            "Discovery")


def _rym_unreachable(reason, cfg=None, status=None, challenge=False, url=""):
    """Record the last response and log ONE concise line per refusal — not per
    album, not per candidate.

    A per-album traceback would bury the import log for a source that is
    simply unavailable, so this is logged once and the chain moves on. The
    counter is what lets a caller tell "RYM is not answering" (stop asking —
    the next candidate cannot do better) from "that slug was wrong" (try the
    next one); `_rym_warned`, set here, is the same "stop asking": one refused
    request and `_rym_get` answers None without going out again, which is what
    keeps a blocked RYM off the import's critical path. The refusal records
    the cookie it was found with and when — `_rym_blocked` is what reads
    those, and a cookie the user replaces clears the latch on the spot.

    The response that earned the refusal is recorded (`_rym_record`) BEFORE
    the latch can answer the next caller from state: the line is printed once,
    but the panel has to be able to say WHICH refusal this was."""
    global _rym_warned, _rym_failures, _rym_blocked_cookie, _rym_blocked_at
    _rym_record(status, url, challenge, reason)
    _rym_failures += 1
    if _rym_warned:
        return
    _rym_warned = True
    _rym_blocked_cookie = _rym_cookie(cfg)
    _rym_blocked_at = time.time()
    print(f"[mlo] rateyourmusic: {reason} — skipping RYM (rym_cookie in "
          "Settings → Discovery is the credential)")


def _rym_cache_dir():
    """<music>/.mlo/data/rym_cache — the app's folder-state dir."""
    return _data_cache_dir("rym_cache")


# The NEGATIVE beside a cached page: "RYM has no such page" for this exact
# path, written after a 404 (or the redirect to search/home a wrong slug gets)
# and read back through `_rym_cache_read` like any other answer. A slug that
# does not exist does not start existing, so the next album whose ladder
# guesses the same candidate must not spend a request and a second of the
# 1 req/s walk learning it again — it reads this instead and moves on.
_RYM_MISS = "#mlo:rym-no-such-page"


def _rym_cache_read(key, ttl):
    d = _rym_cache_dir()
    if not d:
        return None
    fp = os.path.join(d, key + ".html")
    try:
        if os.path.isfile(fp) and time.time() - os.path.getmtime(fp) < ttl:
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
    except OSError:
        return None
    return None


def _rym_cache_write(key, text):
    d = _rym_cache_dir()
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, key + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, os.path.join(d, key + ".html"))
    except OSError:
        pass


def _rym_archive_on(cfg=None):
    """Whether the archived-snapshot route may be used for this call.

    The key ships True (`mlo.config.DEFAULT_CONFIG`), so every config that came
    through `load_config` — every real run, and every Settings save — carries it
    and the fallback is on unless the user turned it off. It is read live, like
    the cookie, so unticking it takes effect on the next import.

    A cfg that never went through the config layer and OMITS the key is read as
    OFF, deliberately. Such a caller can only mean "ask the source and see", and
    without a credential that is exactly the refused walk this gate exists to
    prevent; a hand-built cfg asks for the archive by setting the key.
    """
    try:
        if cfg is None:
            from mlo.config import load_config
            cfg = load_config()
        return bool((cfg or {}).get("rym_archive_fallback"))
    except Exception:
        return False


def _rym_archive_body(text):
    """A Wayback response with Wayback's own wrapper removed, when it has one.

    The `id_` form normally returns the capture's own bytes (VERIFIED), so this
    is the safety net for the other shape. Two rewrites, both URL-only — the
    page's text is not touched:

      * the toolbar Wayback injects between its two markers goes; and
      * the URLs it rewrites come back: the `/web/<timestamp>/` prefix goes,
        and so does the target's own origin in front of an anchor. That second
        one is what makes the scrape work: the scrapers read
        `href="/genre/<slug>/"` — the anchor RYM serves — and
        `href="https://rateyourmusic.com/genre/<slug>/"`, the same link in a
        shape they cannot match, is what a wrapped href is reduced to once the
        prefix is gone. Without it a wrapped page parses as a page that states
        no genres at all.
    """
    out = text or ""
    if "WAYBACK TOOLBAR INSERT" in out:
        out = re.sub(r"<!--\s*BEGIN WAYBACK TOOLBAR INSERT\s*-->.*?"
                     r"<!--\s*END WAYBACK TOOLBAR INSERT\s*-->", "",
                     out, flags=re.I | re.S)
    out = re.sub(r"https?://web\.archive\.org/web/[^/]+/", "", out)
    return re.sub(r'href="https?://(?:www\.)?rateyourmusic\.com/', 'href="/', out)


def _rym_archive_stamp(url):
    """The capture's own timestamp from a Wayback URL, or "".

    The `id_` form redirects to `/web/<timestamp><flags>/<target>` (VERIFIED),
    so the copy that answered states its date in the URL it resolved to. A
    request that did not redirect has none, and then the report says the
    snapshot is undated rather than inventing a date for it.
    """
    m = re.search(r"/web/(\d{4,14})[a-z_]*/", str(url or ""), re.I)
    return m.group(1) if m else ""


def _rym_archive_when(stamp):
    """A capture timestamp as "2021-03-25" (or "2021-03", or "")."""
    digits = re.sub(r"\D", "", str(stamp or ""))
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) >= 6:
        return f"{digits[:4]}-{digits[4:6]}"
    return digits if len(digits) == 4 else ""


def _rym_archive_snapshot(url):
    """{"snapshot", "url"} describing the capture a Wayback URL resolved to."""
    return {"snapshot": _rym_archive_stamp(url), "url": str(url or "")}


# The capture's identity travels WITH the cached bytes, as one comment line at
# the top of the file: the report says which snapshot answered and how old it
# is, and a cached copy must be able to say the same thing (nor may a date
# outlive the bytes it belongs to, which is what a sidecar file beside the HTML
# could let happen).
_RYM_ARCHIVE_MARK = "<!--mlo-archive-snapshot:"


def _rym_archive_pack(html, archive):
    """The cache file for one archived page: WHICH capture it is, then its bytes."""
    return "%s%s %s-->\n%s" % (_RYM_ARCHIVE_MARK,
                               (archive or {}).get("snapshot") or "",
                               (archive or {}).get("url") or "", html or "")


def _rym_archive_unpack(text):
    """(html, archive) from a cached snapshot — an empty html means NO copy.

    A cached "archive.org has nothing for this page" is a real answer worth
    keeping: without it every import of an album RYM never archived would go
    back to archive.org to learn the same thing, once per album, forever.
    """
    text = text or ""
    if not text.startswith(_RYM_ARCHIVE_MARK):
        return text, {}
    head, _, rest = text.partition("-->")
    fields = head[len(_RYM_ARCHIVE_MARK):].split(" ", 1)
    return ((rest[1:] if rest.startswith("\n") else rest),
            {"snapshot": fields[0].strip(),
             "url": fields[1].strip() if len(fields) > 1 else ""})


def _rym_archive_fetch(url, params=None):
    """One GET to archive.org: (page, final_url, answered).

    `page` is the capture with Wayback's wrapper removed, or None when what came
    back is not a page this module may read: nothing answered, a non-200 replay
    (the `id_` form replays the capture's OWN status, so a capture of RYM's
    refusal is a 403 here too), an empty body, or the Cloudflare interstitial RYM
    served the crawler that day. That last check is the plain `_RYM_CHALLENGE_RE`
    the live path uses — the same regex, one parser — and it is why an archived
    refusal can never be read as genres.

    `answered` says a response arrived at all, which is what tells a page
    archive.org does not HAVE from archive.org being unable to answer: the first
    is worth remembering for the full cache TTL, the second is not.
    """
    try:
        r = _rym_fetch(url, params, RYM_ARCHIVE_HEADERS, None)
    except httpx.HTTPError:
        return None, "", False
    final = str(getattr(r, "url", "") or url)
    if getattr(r, "status_code", None) != 200:
        return None, final, True
    text = _rym_archive_body(getattr(r, "text", "") or "")
    if not text.strip() or _RYM_CHALLENGE_RE.search(text[:4000]):
        return None, final, True
    return text, final, True


def _rym_archive_captures(path):
    """The capture timestamps worth trying for one RYM path, NEWEST first, or
    None when the index itself could not be read.

    An empty list is a real answer — archive.org holds no 200 capture of this
    page — and it is what makes the negative cache honest: nothing to try,
    nothing to find, do not ask again for a month. None means nothing is known,
    so nothing is written down.
    """
    text, _final, answered = _rym_archive_fetch(
        RYM_ARCHIVE_INDEX,
        {"url": f"{RYM_BASE}{path}", "output": "json",
         "filter": "statuscode:200", "collapse": "digest",
         "limit": "-%d" % RYM_ARCHIVE_TRIES})
    if not text:
        return None if not answered else []
    try:
        rows = json.loads(text) or []
    except ValueError:
        return None
    if not rows:
        return []
    # Row 0 NAMES the columns ("urlkey","timestamp","original","mimetype",
    # "statuscode","digest","length" for a plain CDX query — VERIFIED live), so
    # the timestamp is found by name instead of assumed to be the first field.
    # CDX lists ascending, so the newest capture is LAST and the walk wants it
    # first.
    head = [str(c).strip().lower() for c in (rows[0] or [])]
    col = head.index("timestamp") if "timestamp" in head else 0
    stamps = [str(row[col]) for row in rows[1:]
              if len(row) > col and str(row[col]).isdigit()]
    stamps.reverse()
    return stamps


def _rym_archive_get(path, cfg=None, started=None):
    """The ARCHIVED copy of one rateyourmusic.com page: (html, archive).

    An html of None means this module has no copy it may read, which every
    caller treats exactly like a live page it could not read — never as "the
    source states nothing". `archive` is {"snapshot", "url"} for the copy that
    answered, and is filled in for a CACHED copy too: the age of the data is part
    of the answer.

    The newest capture is asked first, in the `id_` form — one request, and for a
    page archived while RYM still served it, the whole answer. When that capture
    is not a page, the capture INDEX is asked and its newest 200 captures are
    tried in turn (`RYM_ARCHIVE_TRIES` of them): a 2021 capture is still RYM's
    own data about the release, and the alternative to reading it is reading
    nothing at all.

    Every request here goes through `_rym_fetch`, so it shares the live route's
    1 req/s and the module's single request lock: the fallback costs a couple of
    seconds and never a burst. `started` is the caller's wall-clock budget
    (`_rym_expired`), checked between captures. Nothing here touches the refusal
    latch or records a refusal (`_rym_unreachable`) — archive.org is a different
    host, and a capture that is junk is not RYM refusing anything.
    """
    if not path or not _rym_archive_on(cfg):
        return None, {}
    target = f"{RYM_BASE}{path}"
    key = "wayback-" + hashlib.sha1(target.encode("utf-8")).hexdigest()
    cached = _rym_cache_read(key, RYM_CACHE_TTL)
    if cached is not None:
        html, archive = _rym_archive_unpack(cached)
        if not html:
            return None, {}
        _rym_route.update({"route": "archive", "cached": True,
                           "url": archive.get("url") or "",
                           "snapshot": archive.get("snapshot") or ""})
        return html, archive
    tried, html, archive = [], None, {}
    text, final, _answered = _rym_archive_fetch(
        f"{RYM_ARCHIVE_BASE}/{RYM_ARCHIVE_LATEST}/{target}")
    if text:
        html, archive = text, _rym_archive_snapshot(final)
    else:
        tried.append(_rym_archive_stamp(final))
        stamps = _rym_archive_captures(path)
        if stamps is None:
            # The index could not be read, so what archive.org holds for this
            # page is unknown: nothing is remembered about it either, or a
            # "no snapshot" cached now would outlive the outage that caused it.
            return None, {}
        for stamp in stamps:
            if stamp in tried:
                continue
            if started is not None and _rym_expired(started):
                break
            text, final, _answered = _rym_archive_fetch(
                f"{RYM_ARCHIVE_BASE}/{stamp}id_/{target}")
            tried.append(stamp)
            if text:
                html, archive = text, _rym_archive_snapshot(final)
                break
    if not html:
        _rym_cache_write(key, _rym_archive_pack("", {}))
        return None, {}
    _rym_cache_write(key, _rym_archive_pack(html, archive))
    _rym_route.update({"route": "archive", "cached": False,
                       "url": archive.get("url") or "",
                       "snapshot": archive.get("snapshot") or ""})
    return html, archive


def _rym_expired(started):
    """Whether a lookup that began at *started* has spent its whole budget."""
    return time.time() - started > RYM_MAX_WALL


def _rym_get(path, params=None, cfg=None, expect=None):
    """Polite GET: 1 req/s, disk-cached, browser-like headers, None on any
    failure (a blocked RYM is logged once — see `_rym_unreachable`).

    A request under a NEW cookie is preceded by ONE warm-up navigation
    (`_rym_warm`) — RYM's home page — which is what makes the WAF hand over
    its own cookies before the page the caller actually wants.

    *expect* is the path the caller asked for: a 404 or a redirect to
    somewhere else (RYM sends an unknown slug to search/home) then counts as
    "no such page" — a miss the caller can move on from, not a sign that RYM
    is unreachable, so neither is logged as one.

    A 429, a 5xx or a timeout is RYM being busy, not refusing, so each gets a
    retry (RYM_RETRIES of them) — spaced by the same 1 req/s as every other
    request, and only then does the source count as unreachable. A user-
    initiated Test clears the latch first (`_rym_clear_block`), because "does
    this cookie work?" is a question the panel has to ask RYM for real."""
    import hashlib
    from urllib.parse import urlencode, urlsplit
    key = hashlib.sha1(
        (path + "?" + urlencode(sorted((params or {}).items()))).encode("utf-8")
    ).hexdigest()
    hit = _rym_cache_read(key, RYM_CACHE_TTL)
    if hit is not None:
        # A page, or the negative beside one (`_RYM_MISS`): the second is a
        # 404 this ladder already paid for, and "no such page" is an answer.
        return None if hit == _RYM_MISS else hit
    if _rym_blocked(cfg):
        # RYM refused an earlier request under this same cookie (a challenge,
        # a 403, no connection). The candidates left cannot do better, and
        # asking them at 1 req/s is how an import of a few dozen albums used
        # to burn minutes on a source that was never going to answer.
        return None
    url = f"{RYM_BASE}{path}"
    _rym_warm(cfg)
    jar, headers = _rym_cookiejar(cfg), _rym_headers()
    reason = ""
    for attempt in range(RYM_RETRIES + 1):
        try:
            r = _rym_fetch(url, params, headers, jar)
        except httpx.HTTPError as e:
            r, reason = None, f"no connection ({type(e).__name__})"
        busy = r is None or r.status_code == 429 or r.status_code >= 500
        if not busy or attempt >= RYM_RETRIES:
            break
    tried = attempt + 1
    if r is None:
        _rym_unreachable(reason, cfg, url=url)
        return None
    if r.status_code != 200:
        # 404 is a slug that does not exist, not a blocked source: the caller
        # tries its next candidate instead of declaring RYM unreachable. The
        # negative is CACHED — "no such page" is an answer that does not get
        # truer by being asked again, and the ladder spends several spellings
        # per album at 1 req/s, so without this every album whose title has
        # more than one word paid the same 404s on every run.
        if r.status_code != 404:
            _rym_unreachable(_rym_reason(cfg, r.status_code, tries=tried), cfg,
                             status=r.status_code, url=url)
        else:
            _rym_cache_write(key, _RYM_MISS)
        return None
    if not r.text:
        _rym_unreachable("empty response", cfg, status=r.status_code, url=url)
        return None
    if _RYM_CHALLENGE_RE.search(r.text[:4000]):
        _rym_unreachable(_rym_reason(cfg, r.status_code, challenge=True), cfg,
                         status=r.status_code, challenge=True, url=url)
        return None
    if expect is not None:
        final = urlsplit(str(getattr(r, "url", "") or "")).path
        if not final.startswith(expect):
            # RYM answered 200 with a DIFFERENT page: a slug it does not know
            # is served the search page (or the home page), which is the same
            # statement as a 404 — "this path is not that page" — and is cached
            # the same way, or the next run pays it again.
            _rym_cache_write(key, _RYM_MISS)
            return None
    # A usable answer is also "the last response": the panel must not keep
    # showing a refusal RYM has since moved past. And WHICH route this was is
    # recorded too (`_rym_route`): the archive fallback answers the same
    # question from another host, and the report has to be able to tell the two
    # apart — see `_rym_note`.
    _rym_record(r.status_code, url)
    _rym_route.update({"route": "live", "cached": False, "url": url,
                       "snapshot": ""})
    _rym_cache_write(key, r.text)
    return r.text


def _rym_slug(value):
    """RYM's ARTIST-page slug: lowercase, `&` → `and` (RYM's own spelling),
    accents transliterated, apostrophes dropped ("Sgt. Pepper's" →
    `sgt-peppers`), other punctuation/whitespace collapsed to dashes.

    VERIFIED live against the artist pages MusicBrainz itself states: artist
    pages are dash-separated ("The Beatles" → `/artist/the-beatles`, "Simon &
    Garfunkel" → `/artist/simon-and-garfunkel`). A RELEASE page is a DIFFERENT
    spelling — see `_rym_release_slug` — and asking for one with this slug is
    what made every multi-word album a 404 on the first candidate.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"['`]", "", text.replace("&", " and "))
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


def _rym_release_slug(value):
    """RYM's RELEASE-page spelling of one `/release/album/<artist>/<album>/`
    segment: lowercase, accents folded, `&` → `and`, apostrophes DELETED, and
    every other run of punctuation/whitespace replaced by the SAME NUMBER of
    underscores — RYM does not collapse the run.

    VERIFIED live, against the release pages MusicBrainz itself states (each
    `/release/album/...` relation below was read off the release group):

      "In Rainbows" → in_rainbows, "Kid A" → kid_a,
      "The Beatles" → the_beatles, "Kendrick Lamar" → kendrick_lamar,
      "Sgt. Pepper's Lonely Hearts Club Band" →
        sgt__peppers_lonely_hearts_club_band   (the `'` goes, "." and the
        space each leave their own `_`),
      "Simon & Garfunkel" → simon_and_garfunkel.

    Both spellings coexist in the wild: RYM's OLDER release pages kept the
    dash form and were never rewritten (`the-dark-side-of-the-moon`,
    `the-wall`), so this is the spelling to try FIRST, not the only one —
    `_rym_release_paths` is what tries the rest.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = text.replace("'", "").replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", lambda m: "_" * len(m.group(0)), text)
    return text.strip("_")


def _rym_release_candidates(name):
    """The slugs RYM may use for *name* in a `/release/` path, best first.

    The underscore spelling is the one RYM generates for a release (see
    `_rym_release_slug`); the dash spelling is kept because RYM's older pages
    were slugged with dashes and were never rewritten (VERIFIED live: "The
    Dark Side of the Moon" → `the-dark-side-of-the-moon`, "The Wall" →
    `the-wall`, "Godspeed You! Black Emperor" → `godspeed-you-black-emperor`).
    "The" stays in a release slug (`the_beatles`, `the_smiths`), which is why,
    unlike an artist page, there is no de-`the`-ed candidate here. Every
    candidate is only ever TRIED — it is confirmed as this release before its
    genres are read."""
    out = []
    for slug in (_rym_release_slug(name), _rym_slug(name)):
        if slug and slug not in out:
            out.append(slug)
    return out


def _rym_release_paths(artist, album):
    """The `/release/album/<artist>/<album>/` paths to try, best first.

    Both segments of a RYM release page use the SAME spelling (VERIFIED live:
    `the_beatles/abbey_road`, `radiohead/in_rainbows`, `simon_and_garfunkel/
    bridge_over_troubled_water` are all-underscore, while the older
    `pink-floyd/the-wall` is all-dash), so the two homogeneous spellings come
    first and the mixed ones are only tried after them: one request for the
    common case, two for a legacy page, four in the worst case — each still
    inside the one wall clock the caller checks.
    """
    forms = [_rym_release_candidates(artist), _rym_release_candidates(album)]
    picked, seen = [], set()

    def add(a, b):
        if (a, b) not in seen:
            seen.add((a, b))
            picked.append((forms[0][a], forms[1][b]))

    # The homogeneous spellings first — both segments as RYM generates them,
    # then both in the legacy dash form — and only then the mixed ones.
    for a, b in ((0, 0), (1, 1), (0, 1), (1, 0)):
        if a < len(forms[0]) and b < len(forms[1]):
            add(a, b)
    return [f"/release/album/{a}/{b}/" for a, b in picked]


def _rym_path_from_url(url):
    """The RYM path of a stored release URL, or "" when it is not one.

    MusicBrainz states the RYM page itself (a `url` relation, see
    `_mb_rym_links`), so the scraper starts from THAT page when it has one:
    no slug is guessed at all, and a title RYM spells with characters
    `_rym_slug` cannot fold (VERIFIED: F# A# ∞ → `f%CF%AFa%CF%AF%E2%88%9E`)
    still resolves. An artist or song URL is NOT a release page and yields ""
    — a title's genres must never be read off the artist's page.
    """
    from urllib.parse import urlsplit

    text = str(url or "").strip()
    if not text or not RYM_RE.match(text) or rym_url_kind(text) != "album":
        return ""
    path = urlsplit(text).path or ""
    if not path.endswith("/"):
        path += "/"
    return path if path.startswith("/release/") else ""


def _rym_album_answer(html, url, archive=None):
    """The genre answer one RYM RELEASE page holds, or None.

    The track list is read first and then REMOVED: a row that states its own
    genre must not have that genre promoted to the whole release. Descriptors
    are the page's other classification and are read only when it carries no
    `/genre/` anchor at all — labelled as the album-level answer they are.

    A page whose ONLY classification is per-track — its rows state genres and
    the page itself states none — is an answer too: it IS the per-track tier.
    Returning None here (as this did) threw that away and fell through to the
    artist page, which can only say less, so `genres`/`descriptors` are empty in
    that case and the tracks carry the whole answer.

    `archive` is the snapshot this page came from (see `_rym_archive_get`); it
    is recorded in the answer, and the URL it reports is the snapshot's, because
    a caller that shows the user where a genre came from must not point them at
    a live page that refused to serve it.
    """
    rows = _rym_tracks_from(html)
    head = _RYM_TRACK_ROW_RE.sub("", html or "")
    genres = _rym_genres_from(head)
    descriptors = [] if genres else _rym_labels(_RYM_DESCRIPTOR_RE, head)
    if not genres and not descriptors and not any(r.get("genres") for r in rows):
        return None
    out = {"genres": genres or descriptors, "descriptors": descriptors,
           "level": "album", "tracks": rows,
           "source_url": f"{RYM_BASE}{url}", "source": "rym"}
    if archive:
        out["archive"] = dict(archive)
        out["source_url"] = archive.get("url") or out["source_url"]
    return out


def _rym_live_album_answer(artist, album, cfg, album_url=""):
    """The three LIVE routes of `rym_genres`, best first, or None.

    Split out so the archived route can stand BESIDE this ladder instead of
    inside it: `rym_genres` runs these when it has a credential to run them
    with, and falls through to a snapshot when they cannot answer. The routes
    themselves are unchanged — every one of them is VERIFIED before it is read
    (`_rym_verified`: the answer must have stayed on the path that was asked
    for, and the page must state the artist AND the album — a genre list lifted
    from a same-named cover version is worse than no genres at all):

      1. the page MusicBrainz itself states (`album_url`, or looked up by
         `rym_links`) — an identity, so no slug is guessed at all;
      2. RYM's own release slugs, in the spelling RYM generates them
         (`_rym_release_candidates`);
      3. RYM's own search page, whose release hits are each confirmed the same
         way.

    Returns {"genres": [...], "descriptors": [...], "level": "album",
    "tracks": [...], "source_url": ...} or None — see RYM_BASE's note on the
    blocked-by-RYM failure mode. Chart data is NOT scraped: nothing in the app
    consumes a RYM chart, so only the genre path is implemented. `level` is
    always "album" here: RYM classifies releases, and a release's genres are
    applied to every one of its tracks — the caller records that in the
    provenance rather than pretending the answer was per-track.
    `descriptors` are the page's /descriptor/ anchors, used only when the page
    carries no /genre/ anchor at all, and `tracks` carries the track list so a
    row that DOES state its own genre can be mapped by position, then title.
    """
    started = time.time()
    # 1) The page MusicBrainz states. One request, and the only route that
    # survives a title whose slug this module cannot derive.
    path = _rym_path_from_url(album_url)
    if path:
        html = _rym_verified(path, cfg, artist, album)
        if html:
            got = _rym_album_answer(html, path)
            if got:
                return got
    # 2) RYM's own release slugs (see `_rym_release_paths` for the order).
    if not _rym_expired(started) and not _rym_blocked(cfg):
        for url in _rym_release_paths(artist, album):
            html = _rym_verified(url, cfg, artist, album)
            if html:
                got = _rym_album_answer(html, url)
                if got:
                    return got
            if _rym_expired(started) or _rym_blocked(cfg):
                return None
    # 3) RYM's own search page. The search answers a QUERY, not a question:
    # each hit is confirmed as the release asked about before its genres are
    # read.
    if _rym_expired(started) or _rym_blocked(cfg):
        return None
    index = _rym_get("/search", {"searchterm": f"{artist} {album}", "type": "a"},
                     cfg=cfg, expect="/search")
    if not index:
        return None
    m = _RYM_ARTIST_LINK_RE.search(index)
    for rel_url in _RYM_RELEASE_LINK_RE.findall(index)[:3]:
        page = _rym_verified(rel_url, cfg, artist, album)
        if not page:
            if _rym_expired(started) or _rym_blocked(cfg):
                return None
            continue
        got = _rym_album_answer(page, rel_url)
        if not got:
            continue
        got["artist_page"] = f"{RYM_BASE}{m.group(1)}" if m else ""
        return got
    return None


def _rym_archived_answer(artist, album, cfg, album_url=""):
    """The archived-snapshot route of `rym_genres`, or None.

    The paths tried are the one MusicBrainz states first (an identity, no slug
    guessed) and then RYM's own spellings — and only the first few: the live
    ladder can afford four candidates because a 404 is one cheap request, while
    an archive answer costs a snapshot fetch and, when the newest capture is not
    a page, an index request behind it.

    The search page is NOT read from the archive. Its hit list is the site's
    answer to a QUERY — the one page whose content the site itself would answer
    differently today — and a RYM genre is only ever read off a page confirmed
    to BE this release.
    """
    started = time.time()
    stated = _rym_path_from_url(album_url)
    paths = ([stated] if stated else []) + \
        [p for p in _rym_release_paths(artist, album) if p != stated]
    for url in paths[:RYM_ARCHIVE_PATHS + (1 if stated else 0)]:
        html, snapshot = _rym_archive_get(url, cfg, started)
        if html and _rym_mentions(html, artist, album):
            got = _rym_album_answer(html, url, archive=snapshot)
            if got:
                return got
        if _rym_expired(started):
            break
    return None


def rym_genres(artist, album, cfg=None, album_url="", archive=False):
    """RateYourMusic genres for an album, or None when RYM cannot answer.

    Two routes decide it, and which one is asked is settled before a request
    goes out: the LIVE release page (`_rym_live_album_answer` — the page
    MusicBrainz states, then RYM's own slugs, then its search page), and the
    ARCHIVED copy of that page (`_rym_archived_answer`).

    The archive is a route the CALLER asks for, not a default of this function
    (`archive=True` is what `_genre_source_answers` passes) — and the config may
    still veto it: with `rym_archive_fallback` off, or in a cfg that omits the
    key, an `archive=True` call behaves exactly like an `archive=False` one
    (`_rym_archive_on`). Everything that is not a genre read leaves it False on
    purpose: the link resolver has MusicBrainz's own route to each page, and a
    Wayback request must never become what IT falls back to.

    Without a `rym_cookie` the live site is a known refusal (see RYM_BASE), so a
    genre read with the archive permitted does not ask it at all: the snapshot
    answers instead, and the request, the second of throttle and the refusal
    latch are all saved. With a cookie the live page is asked first and the
    snapshot is what answers behind it — after a refusal, after a latch, or when
    the live page is not this release.

    An answer that came from a snapshot carries `"archive": {"snapshot", "url"}`
    (the capture's timestamp and its Wayback URL) and its `source_url` is that
    snapshot's URL, not the live page's. Everything else — `genres`,
    `descriptors`, `level: "album"`, `tracks` — is exactly what a live page
    would have produced, because it is parsed by the same scrapers.

    A user-initiated check (the Sources panel's Test) clears the refusal latch
    first — `_rym_clear_block` — so the saved cookie is really put to RYM
    instead of being answered "blocked" from an earlier run.
    """
    artist = str(artist or "").strip()
    album = str(album or "").strip()
    if not artist or not album:
        return None
    archive = bool(archive) and _rym_archive_on(cfg)
    if bool(_rym_cookie(cfg)) or not archive:
        got = _rym_live_album_answer(artist, album, cfg, album_url)
        if got:
            return got
    if not archive:
        return None
    return _rym_archived_answer(artist, album, cfg, album_url)


def rym_artist_genres(artist, cfg=None, archive=False):
    """RateYourMusic genres for an artist (its /artist/ page), or None.

    Confirmed as that artist's page before its genres are read, exactly like
    the album path — a label or another act's page must not supply them.

    The archived copy of the page is the same fallback `rym_genres` has, asked
    for the same way (`archive=True`, and `rym_archive_fallback` still vetoes
    it) and under the same rule otherwise: with no credential the live site is
    not asked at all when the archive may be used, so a default install still
    reaches this tier instead of it silently not existing. An answer that came
    from a snapshot carries `"archive": {"snapshot", "url"}` and reports the
    snapshot's URL.

    `cfg` is threaded through for the same reason `rym_genres` takes it: the
    refusal latch and the cookie are keyed to the CALLER's credential, and a
    request that went out without it would both re-ask a source that already
    refused and log a second line saying the wrong thing about why.
    """
    artist = str(artist or "").strip()
    if not artist:
        return None
    url = f"/artist/{_rym_slug(artist)}"
    archive = bool(archive) and _rym_archive_on(cfg)
    html, snapshot = None, {}
    if bool(_rym_cookie(cfg)) or not archive:
        html = _rym_verified(url, cfg, artist)
    if not html and archive:
        html, snapshot = _rym_archive_get(url, cfg)
        if html and not _rym_mentions(html, artist):
            html = None
    genres = _rym_genres_from(html or "")
    if not genres:
        return None
    out = {"genres": genres, "source_url": f"{RYM_BASE}{url}"}
    if snapshot:
        out["archive"] = dict(snapshot)
        out["source_url"] = snapshot.get("url") or out["source_url"]
    return out


# --------------------------------------------------------------------------- #
# RateYourMusic charts (scraped — no API, see RYM_BASE)
# --------------------------------------------------------------------------- #
# RYM's charts live at `/charts/top/<entity>/<period>/`, where the period is
# `all-time` or a single YEAR (`/charts/top/song/2025`, VERIFIED: the page
# titles itself "Best songs of 2025"). RYM publishes no month and no week
# chart, so those two windows are REFUSED BY NAME — `RYM_CHART_PERIODS` below
# is what the registry reads, so nothing can quietly fold "this week" into
# all-time.
#
# Everything else is the same scrape as the genre readers: the live page with a
# `rym_cookie`, the archived snapshot behind it (still gated by
# `rym_archive_fallback`), the same 1 req/s throttle and the same 30-day disk
# cache, so a chart costs one request and is then free for a month. A refusal
# is recorded verbatim (status, challenge marker, the reason sentence) and the
# caller reports those words — a blocked RYM must never read as "no results".
RYM_CHART_KINDS = ("tracks",)
RYM_CHART_PERIODS = ("all", "year")
_RYM_CHART_ENTITY = {"tracks": "song", "albums": "album", "artists": "artist"}
_RYM_CHART_ROOT = "/charts/top/"
# A chart can be NARROWED to one genre or one artist, as a further path
# segment after the window: `/charts/top/song/all-time/g:shoegaze/`. The two
# prefixes are RYM's own, and the shape is VERIFIED against the chart URLs it
# has served (Wayback CDX for `rateyourmusic.com/charts/top/song/`, all
# status 200): `/charts/top/song/1950s/g:rock-and-roll/` and
# `/charts/top/song/all-time/a:bad-bunny/`. Those two captured pages were read
# back with `rym_chart_rows` — same `<div id="posN">` items, same
# `page_charts_section_charts_item_link` anchors — and they title themselves
# "Best Rock & Roll songs of the 1950s" and "Best Bad Bunny songs of all time",
# which is what `rym_chart_states` checks before a filtered chart is believed.
# The album charts (`/charts/top/album,ep,…/`) are a different item shape (no
# `/song/` link), so they are not read here at all.
RYM_CHART_GENRE = "g:"
RYM_CHART_ARTIST = "a:"
# One chart page's items: RYM wraps every entry in `<div id="posN" …>` and
# links its subject with a kind-specific anchor class.
_RYM_CHART_ITEM_RE = re.compile(r'<div id="pos(\d+)"', re.I)
_RYM_CHART_LINK_RE = re.compile(
    r'<a class="page_charts_section_charts_item_link [^"]*"\s*'
    r'href="(/song/[^"]+)"', re.I)
_RYM_CHART_ARTIST_LINK_RE = re.compile(
    r'<a class="artist" href="(/artist/[^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
_RYM_CHART_TITLE_RE = re.compile(
    r'<span class="ui_name_locale_original">(.*?)</span>', re.I | re.S)
_RYM_CHART_FALLBACK_TITLE_RE = re.compile(
    r'<span class="ui_name_locale[^"]*">(.*?)</span>', re.I | re.S)
_RYM_CHART_NAME_RE = re.compile(r"<title>(.*?)\s*-\s*Rate Your Music\s*</title>",
                                re.I | re.S)
_RYM_TAG_RE = re.compile(r"<[^>]+>")


def _rym_text(fragment):
    """The words inside an HTML fragment, entities resolved. RYM renders a
    name as nested locale spans, so tags are stripped rather than parsed."""
    return _html.unescape(_RYM_TAG_RE.sub("", fragment or "")).strip()


def _rym_chart_slug(path):
    """The subject of a chart row's own URL: `/song/<artist>/<title>/`."""
    parts = [p for p in str(path or "").split("/") if p]
    return parts[-1] if parts else ""


def rym_chart_name(html):
    """The chart's own name from the page title ("Best songs of all time"), or
    "" — it is what the row's reason line quotes, so it is never guessed."""
    hit = _RYM_CHART_NAME_RE.search(html or "")
    return _rym_text(hit.group(1)) if hit else ""


def _rym_chart_filters(genre="", artist=""):
    """RYM's own filter segments for a chart path, or "" for the plain chart.

    Every filter is spelled here, once, and a filter with no value contributes
    nothing: a chart asked for without a genre IS the plain chart, never a
    chart narrowed to the empty string (which RYM would read as a genre of its
    own). The slugs come from `_rym_slug`, the module's one spelling of an RYM
    path segment — a genre's own spelling is RYM's ("Rock & Roll" →
    `rock-and-roll`, the slug its chart URL carries)."""
    segments = []
    for prefix, value in ((RYM_CHART_GENRE, genre), (RYM_CHART_ARTIST, artist)):
        slug = _rym_slug(value)
        if slug:
            segments.append(prefix + slug)
    return ("/" + "/".join(segments) + "/") if segments else ""


def rym_chart_states(chart, name):
    """Whether a FILTERED chart's own name states what it was filtered to.

    RYM titles a narrowed chart after its filter ("Best Shoegaze songs of all
    time"), and that title is the one piece of evidence the page gives that the
    filter was applied rather than dropped: a genre or artist RYM does not know
    takes the client to a chart without it, and believing THAT would present
    RYM's all-time chart as "the shoegaze chart" — the one lie a chart source
    must never tell. A chart whose name does not state the filter is reported
    by the caller as no such genre/artist, never as that filter's answer."""
    want = _rym_ref(name)
    return bool(want) and want in _rym_ref(chart)


def rym_chart_rows(html, limit=None):
    """One RYM chart page as rows, in the chart's own order.

    Each row is the subject's page (a `/song/…` URL), its title and its artist,
    with `rank` = RYM's own position from the wrapper's `id="posN"`. A row whose
    title cannot be read falls back to its URL slug (the title RYM put in the
    link) — never to a made-up name — and nothing is invented for an entry the
    page did not state."""
    text = str(html or "")
    marks = [(m.start(), int(m.group(1))) for m in _RYM_CHART_ITEM_RE.finditer(text)]
    rows = []
    for i, (at, pos) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        chunk = text[at:end]
        hit = _RYM_CHART_LINK_RE.search(chunk)
        if not hit:
            continue
        link = hit.group(1)
        title_hit = (_RYM_CHART_TITLE_RE.search(chunk)
                     or _RYM_CHART_FALLBACK_TITLE_RE.search(chunk))
        title = _rym_text(title_hit.group(1)) if title_hit else ""
        if not title:
            # The link's own slug is the title RYM put in the URL — a fallback
            # that is still RYM's word, never a guessed name.
            title = _rym_chart_slug(link).replace("-", " ").replace("_", " ").strip()
        artist_hit = _RYM_CHART_ARTIST_LINK_RE.search(chunk)
        rows.append({
            "kind": "track",
            "title": title,
            "artist": _rym_text(artist_hit.group(2)) if artist_hit else "",
            "link": f"{RYM_BASE}{link}",
            "rym_path": link,
            "rank": pos or (len(rows) + 1),
            "popularity": None,
            "popularity_label": None,
            "source": "rym",
        })
        if limit and len(rows) >= limit:
            break
    return rows


def rym_charts(kind="tracks", period="all", limit=50, cfg=None, now=None,
               genre="", artist=""):
    """RYM's own chart for one kind and one window, or a raise that says why.

    `all` is `/charts/top/song/all-time` and `year` is the current year's
    chart; every other period is refused (`ValueError`) because RYM publishes
    no such chart — the caller reports that as unsupported, never as an
    all-time answer. `genre` and `artist` NARROW the same chart to RYM's own
    filter (`_rym_chart_filters`), which is what turns a genre seed and an
    artist page into real rows instead of a text search RYM never ran; the
    caller confirms the answer with `rym_chart_states` before it believes the
    filter was applied. A refusal from RYM itself raises `RuntimeError` carrying
    its own words (`rym_last_response`: the status, whether a Cloudflare
    challenge came back instead of a page, and the reason sentence), so the
    endpoint's note chip shows exactly what RYM said.

    An answer may come from the archived snapshot (same cookie gate, throttle
    and cache as the live route); when it does, the row's `page_url` is the
    live page either way and `archive` names the capture the data came from.
    """
    kind = str(kind or "").strip().lower()
    if kind not in RYM_CHART_KINDS:
        raise ValueError("RateYourMusic charts only: " + ", ".join(RYM_CHART_KINDS))
    period = str(period or "all").strip().lower()
    if period not in RYM_CHART_PERIODS:
        raise ValueError("RateYourMusic publishes no %s chart" % period)
    year = datetime.fromtimestamp(now if now is not None else time.time()).year
    slug = "all-time" if period == "all" else str(year)
    path = "%s%s/%s%s" % (_RYM_CHART_ROOT, _RYM_CHART_ENTITY[kind], slug,
                          _rym_chart_filters(genre, artist))
    limit = max(1, int(limit or 50))

    archive = _rym_archive_on(cfg)
    html, snapshot = None, {}
    live = False
    if bool(_rym_cookie(cfg)) or not archive:
        _rym_route.clear()
        html = _rym_get(path, cfg=cfg, expect=_RYM_CHART_ROOT)
        live = True
    if not html and archive:
        _rym_route.clear()
        html, snapshot = _rym_archive_get(path, cfg)
    if not html:
        # Neither route answered. When BOTH were tried, say which one was
        # missing rather than letting one refusal sentence cover two different
        # states: a page the archive never captured is not RYM refusing us.
        why = rym_refusal_text(cfg, path)
        if live and archive:
            why += " (and web.archive.org holds no capture of that page)"
        raise RuntimeError(why)
    rows = rym_chart_rows(html, limit)
    if not rows:
        raise RuntimeError(
            "RateYourMusic answered %s but it stated no chart rows we could "
            "read (the page's own bytes are cached under the app's rym_cache "
            "folder)" % path)
    name = rym_chart_name(html)
    out = {"rows": rows, "total": None,
           "chart": name or ("RateYourMusic %s chart" % period),
           "url": f"{RYM_BASE}{path}"}
    if snapshot:
        out["archive"] = dict(snapshot)
    return out


def rym_refusal_text(cfg=None, path="", now=None):
    """WHY RYM last refused, in its own words — status, challenge, reason.

    The one sentence the chart endpoint's note chip prints, built from
    `rym_last_response()` (which `_rym_get`/the archive route write on every
    answer): "HTTP 403 refused …". A refusal that never reached a status (a
    timeout, no connection) says so. It is never "no results"."""
    info = _rym_last_info or {}
    status = info.get("status")
    parts = []
    if status is not None:
        parts.append("HTTP %s" % status)
    if info.get("challenge"):
        parts.append("Cloudflare challenge instead of a page")
    reason = str(info.get("reason") or "").strip()
    if reason:
        parts.append(reason)
    elif status is None:
        parts.append("no connection")
    where = str(info.get("url") or (f"{RYM_BASE}{path}" if path else ""))
    text = "RateYourMusic refused: " + "; ".join(parts) if parts \
        else "RateYourMusic did not answer"
    return text + (f" ({where})" if where else "")


# --------------------------------------------------------------------------- #
# RYM link resolution (album + artist)
# --------------------------------------------------------------------------- #
# MusicBrainz is the FIRST source: it holds the rateyourmusic.com page as a
# `url` relation ("other databases") on the release GROUP and on the artist —
# VERIFIED live: release-group/6e335887… (In Rainbows) →
# /release/album/radiohead/in_rainbows/, artist/a74b1b7f… (Radiohead) →
# /artist/radiohead, and the same for Nirvana / MTV Unplugged in New York. A
# link MusicBrainz states IS that page, so it costs no RYM request, needs no
# cookie, and nothing has to be confirmed or guessed.
#
# Only what MusicBrainz could not state is scraped — with the `rym_cookie`
# configured, if there is one: without a cookie rateyourmusic.com challenges
# the request (there is no cookie gate, the source simply refuses), which
# latches RYM off for a while — see `_rym_blocked`. Its URLs are derived from
# the names and a candidate is fetched once to prove it exists:
#
#   /release/album/<artist-slug>/<album-slug>/     /artist/<artist-slug>
#
# A candidate is fetched once (1 req/s, 30-day cache) and accepted only when
# the page that comes back IS that page: HTTP 200 (a 404 or a redirect to
# search/home means the slug is wrong), no Cloudflare interstitial, and the
# page states the artist — and for a release, the album too. Nothing is
# guessed from a partial page, so a candidate that cannot be confirmed yields
# NO link and the user pastes their own (the manual editor is unchanged).
# After a refusal the ladder is not walked again for a while — RYM costs ONE
# probe at most, never minutes — but a new cookie (or the same one once the
# refusal has aged out) re-asks, so a corrected credential is never stuck.
_RYM_RELEASE_LINK_RE = re.compile(r'href="(/release/album/[^"]+)"', re.I)
# What the user can act on when nothing resolved because RYM would not answer.
_RYM_BLOCKED_NOTE = ("could not resolve on RateYourMusic — blocked by "
                     "Cloudflare; set rym_cookie in Settings, or MusicBrainz "
                     "links are used")


def _rym_slug_candidates(name):
    """The slugs *name* may use on RYM, best first: its own slug, then the
    de-`the`-ed one ("The Beatles" → "beatles")."""
    slug = _rym_slug(name)
    out = [slug]
    if slug.startswith("the-"):
        out.append(slug[4:])
    return [s for s in out if s]


def _rym_ref(text):
    """Comparison form of a name or page: accents folded, lowercase, `&`→and,
    alphanumerics only — so "Simon & Garfunkel" matches "Simon &amp;
    Garfunkel" on a page and "BJÖRK" matches "Bjork"."""
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = t.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "", t.replace("&", " and "))


def _rym_page_text(page):
    """A page's visible text: tags stripped, entities decoded."""
    return _html.unescape(re.sub(r"<[^>]+>", " ", page or ""))


def _rym_mentions(page, *names):
    """Whether the page states every one of *names* (normalised compare)."""
    text = _rym_ref(_rym_page_text(page))
    return all(_rym_ref(n) in text for n in names if str(n or "").strip())


def _rym_verified(path, cfg, *names):
    """The page at *path*, or None when it is not that page (see above)."""
    page = _rym_get(path, cfg=cfg, expect=path)
    if not page or not _rym_mentions(page, *names):
        return None
    return page


# A rateyourmusic.com page anywhere in MusicBrainz's relations. `www.` and the
# http scheme are accepted because MB stores whatever the editor typed.
_MB_RYM_URL_RE = re.compile(r"^https?://(?:www\.)?rateyourmusic\.com/", re.I)


def _mb_query(value):
    """A name as a MusicBrainz Lucene term: a quote or backslash inside the
    quoted phrase would otherwise break the whole query."""
    return re.sub(r'["\\]', " ", str(value or "")).strip()


def attach_recording_aliases(release, mbid=""):
    """Fill `tracks[].alias` on a release payload from ONE browse call.

    MusicBrainz has no nested `inc` for aliases: a release lookup's
    `inc=recordings` carries the recordings but never their aliases, and a
    lookup per track would spend a request per row on a 1 req/s budget. The
    BROWSE endpoint answers the whole release at once (`recording?release=<id>
    &inc=aliases`, 100 rows a page) and is cached like every other MusicBrainz
    read, so a release page costs one request the first time and none after.

    Returns how many rows got an alias; a payload that fails to arrive leaves
    every track as it was — no alias is better than a broken page.
    """
    # The payload carries the tracklist TWICE: `tracks` (the flat list the
    # wizard and the importers read) and `media[].tracks` (the per-disc shape
    # the release page renders), so both get the alias.
    rows = [t for t in (release.get("tracks") or []) if isinstance(t, dict)]
    for medium in (release.get("media") or []):
        if isinstance(medium, dict):
            rows.extend(t for t in (medium.get("tracks") or []) if isinstance(t, dict))
    rid = str(mbid or release.get("id") or "").strip()
    if not rows or not rid:
        return 0
    try:
        data = mb_get_cached("recording", {"release": rid, "inc": "aliases",
                                           "limit": 100, "fmt": "json"})
    except Exception:
        return 0
    by_id = {r.get("id"): r for r in (data.get("recordings") or [])}
    filled = 0
    for track in rows:
        row = by_id.get(track.get("recording_mbid"))
        if not row:
            continue
        alias = alias_for(row.get("aliases"), None, track.get("title"))
        if alias:
            track["alias"] = alias
            filled += 1
    return filled


def _mb_rym_relations(entity, mbid, inc="url-rels"):
    """(RYM urls, entity data) for one MusicBrainz MBID.

    ([], {}) when MusicBrainz cannot answer — this is a source that may be
    busy, never a reason to fail the lookup."""
    try:
        data = mb_get_cached(f"{entity}/{mbid}", {"inc": inc, "fmt": "json"})
    except Exception:
        return [], {}
    data = data or {}
    urls = []
    for rel in data.get("relations") or []:
        url = str((rel.get("url") or {}).get("resource") or "").strip()
        if _MB_RYM_URL_RE.match(url):
            urls.append(url)
    # A release-group carries the /release/ page; prefer it over anything else
    # (a /label/ or /artist/ relation) when MB states both.
    urls.sort(key=lambda u: "/release/" not in u)
    return urls, data


def _mb_credit_id(entity):
    """The first credited artist's MBID of a release/release-group payload."""
    credit = (entity or {}).get("artist-credit") or [{}]
    return ((credit[0].get("artist") or {}).get("id") or "")


def _mb_release_group_ids(artist, album, limit=5):
    """Release-group MBIDs MusicBrainz's own search returns for this album.

    Only a row whose TITLE is the album asked for is kept: a same-titled
    release by another artist must never contribute a link."""
    q = f'releasegroup:"{_mb_query(album)}"'
    if artist:
        q += f' AND artist:"{_mb_query(artist)}"'
    try:
        rows = (search_mb("release-group", q, limit=limit) or {}).get("rows") or []
    except Exception:
        return []
    return [r["id"] for r in rows
            if r.get("id") and _rym_ref(r.get("title")) == _rym_ref(album)]


def _mb_rym_links(artist="", album="", mbid=None):
    """The RYM pages MusicBrainz itself states: ``{"album", "artist"}``.

    A link MusicBrainz states needs no confirmation — it IS the canonical
    page — so this is the resolver's first source and the one that works with
    no `rym_cookie` at all. `mbid` is the caller's own MusicBrainz ID: the
    release GROUP's where the caller has one (`release["release_group_id"]`),
    a release's otherwise (a release usually carries no RYM relation where its
    group does, so the group is asked first). Without an MBID the group is
    found with MusicBrainz's search. Either key of the result may be missing.
    """
    out, group = {}, {}
    if mbid:
        for entity in ("release-group", "release"):
            urls, data = _mb_rym_relations(entity, mbid,
                                           "url-rels+artist-credits")
            if data:
                group = data
            if urls:
                out["album"] = urls[0]
                break
    if not out.get("album") and album:
        for group_mbid in _mb_release_group_ids(artist, album):
            urls, data = _mb_rym_relations("release-group", group_mbid,
                                           "url-rels+artist-credits")
            if urls:
                out["album"], group = urls[0], data
                break
    if artist and not out.get("artist"):
        artist_mbid = _mb_credit_id(group)
        if not artist_mbid:
            try:
                from server import discovery
                artist_mbid = discovery.resolve_artist_mbid(artist) or ""
            except Exception:
                artist_mbid = ""
        if artist_mbid:
            urls, _data = _mb_rym_relations("artist", artist_mbid)
            if urls:
                out["artist"] = urls[0]
    return out


def rym_links(artist="", album="", cfg=None, mbid=None):
    """Verified RateYourMusic links for an album:
    ``{"album", "artist", "note"}``.

    MusicBrainz is asked FIRST (see `_mb_rym_links`): it states the RYM page
    as a url relation on the release group and on the artist, which resolves
    both links with no cookie and no scraping on an install where
    rateyourmusic.com refuses an automated client (RYM_BASE).

    Only a link MusicBrainz did not state is scraped — the `rym_cookie` is
    what makes that scrape answer, not a gate this function applies (there is
    no cookie check here: with no cookie, or a stale one, RYM challenges the
    request and the ladder stops, see `_rym_blocked`): the album as
    `/release/album/<artist>/<album>/` in RYM's own release spelling (see
    `_rym_release_candidates`), then from RYM's own search page — each
    candidate confirmed before it is accepted. The artist link comes from the
    album page's own `/artist/` link when it is one of the artist's slugs,
    else from `/artist/<slug>` directly.

    Either link is None when it could not be confirmed, and `note` says so
    ("could not resolve …") — that is the user-pastes-the-URL state, never an
    error: when the reason is a blocked RYM the note says what to do about it.
    Gated by `rym_links_auto` (mlo.config, default True): off means no request
    at all. `mbid` (optional) is the release group's MusicBrainz ID, or a
    release's. A user-initiated check (the Sources panel's Test) clears the
    refusal latch first — `_rym_clear_block` — so the configured cookie is
    really put to RYM instead of being answered "blocked" from an earlier run.
    """
    if cfg is None:
        try:
            from mlo.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    out = {"album": None, "artist": None, "note": ""}
    artist = str(artist or "").strip()
    album = str(album or "").strip()
    if not cfg.get("rym_links_auto", True):
        out["note"] = "automatic lookup is off"
        return out
    if not artist and not album:
        out["note"] = "nothing to look up"
        return out

    # 1) MusicBrainz, which states the RYM page itself.
    try:
        stated = _mb_rym_links(artist, album, mbid)
    except Exception:
        stated = {}
    out["album"] = stated.get("album")
    out["artist"] = stated.get("artist")

    # 2) Scrape only what MB could not state. A failure that is not a miss (no
    # connection, a challenge) is counted, and `_rym_get` refuses everything
    # after the first one: RYM costs ONE probe, never a slug walk.
    started = time.time()
    fails = _rym_failures
    page = None

    def stop():
        """Whether RYM has already answered for this lookup — a refusal
        (`_rym_failures`, or `_rym_blocked`, the latch `_rym_get` reads) or
        the wall clock."""
        return _rym_blocked(cfg) or _rym_failures != fails or _rym_expired(started)

    if artist and album and not out["album"]:
        for path in _rym_release_paths(artist, album):
            page = _rym_verified(path, cfg, artist, album)
            if page:
                out["album"] = f"{RYM_BASE}{path}"
                break
            if stop():
                break
        if not out["album"] and not stop():
            # RYM's own search: the first release hits for the query, each
            # confirmed the same way (so a cover version cannot slip through).
            index = _rym_get("/search", {"searchterm": f"{artist} {album}",
                                         "type": "a"}, cfg=cfg)
            for rel in _RYM_RELEASE_LINK_RE.findall(index or "")[:3]:
                page = _rym_verified(rel, cfg, artist, album)
                if page:
                    out["album"] = f"{RYM_BASE}{rel}"
                    break
                if stop():
                    break

    if artist and not out["artist"] and not stop():
        # The album page links its own artist: try RYM's own answer first,
        # but only inside the slug set this name can legitimately produce.
        slugs = list(_rym_slug_candidates(artist))
        if page:
            m = _RYM_ARTIST_LINK_RE.search(page)
            slug = m.group(1).rstrip("/").rsplit("/", 1)[-1] if m else ""
            if slug in slugs:
                slugs.remove(slug)
                slugs.insert(0, slug)
        for a in slugs:
            path = f"/artist/{a}"
            if _rym_verified(path, cfg, artist):
                out["artist"] = f"{RYM_BASE}{path}"
                break
            if stop():
                break

    if not out["album"] and not out["artist"]:
        if not artist:
            out["note"] = "no artist name to look up"
        elif _rym_failures != fails or _rym_blocked(cfg) or not _rym_cookie(cfg):
            # RYM refused (this call, or earlier in the process) and nothing
            # on MusicBrainz either: say what the user can do instead of
            # "no link found".
            out["note"] = _RYM_BLOCKED_NOTE
        else:
            out["note"] = "could not resolve on RateYourMusic"
    elif artist and album and not out["album"]:
        out["note"] = "could not resolve the album link on RateYourMusic"
    elif artist and not out["artist"]:
        out["note"] = "could not resolve the artist link on RateYourMusic"
    return out


# --------------------------------------------------------------------------- #
# Bandcamp (scraped — no public API)
# --------------------------------------------------------------------------- #
# An album page carries the release's crowd-sourced tags as `.tralbum-tags`
# anchors, and its own JSON blob (`data-tralbum`) holds the album title plus
# one `trackinfo` entry per track. Bandcamp states NO per-track genre at all
# — every tag it has is the album's — so this source is an ALBUM-level
# crowdsourced tier and the chain labels its row `level: album`.
#
# VERIFIED here: bandcamp.com's own search/tag pages answer this client with
# Cloudflare's interstitial, but the per-artist album pages do NOT — and the
# album URL is derivable from the names
# (`https://<artist-slug>.bandcamp.com/album/<album-slug>`), so the source
# costs one request per album. A page is accepted only when it states THIS
# album: the tralbum's own title matches, and (when the caller knows its own
# track titles) the page's track titles line up with them — a wrong album's
# tags are NO answer. Politeness is the same as RYM's: browser headers, 1
# req/s, one line per process on a hard failure, and the caller's 30-day cache
# wraps the whole source so a repeat import re-fetches nothing.
BANDCAMP_BASE = "https://bandcamp.com"
BANDCAMP_MIN_INTERVAL = 1.0
_bandcamp_lock = threading.Lock()
_bandcamp_last = 0.0
_bandcamp_warned = False
_BANDCAMP_HEADERS = {
    "User-Agent": RYM_HEADERS["User-Agent"],
    "Accept": RYM_HEADERS["Accept"],
    "Accept-Language": RYM_HEADERS["Accept-Language"],
    "Referer": BANDCAMP_BASE + "/",
}
# The attribute value is HTML-escaped (`&quot;`), so no raw `"` can appear
# inside it and a plain non-greedy match is safe.
_BANDCAMP_TRALBUM_RE = re.compile(r'data-tralbum="([^"]*)"', re.S)
# Tags are `<a class="tag" href=".../discover/<slug>">Label</a>` inside the
# tag block. Read as labels; the block is found by its own class so no anchor
# elsewhere on the page can be mistaken for a tag.
_BANDCAMP_TAG_RE = re.compile(r'<a class="tag"[^>]*>([^<]+)</a>', re.I)


def _bandcamp_unreachable(reason):
    """One concise line per process when Bandcamp cannot answer at all."""
    global _bandcamp_warned
    if _bandcamp_warned:
        return
    _bandcamp_warned = True
    print(f"[mlo] bandcamp.com: {reason} — that source is skipped this run")


def _bandcamp_get(url, timeout=20.0):
    """One polite GET of a Bandcamp page (browser UA, 1 req/s), or "".

    "" covers everything that is not that page: a 404 slug, an empty body, a
    connection failure. A 404 is a miss the caller moves on from, so only a
    connection failure is logged.
    """
    global _bandcamp_last
    with _bandcamp_lock:
        wait = BANDCAMP_MIN_INTERVAL - (time.time() - _bandcamp_last)
        if wait > 0:
            time.sleep(wait)
        _bandcamp_last = time.time()
        try:
            r = httpx.get(url, headers=_BANDCAMP_HEADERS, timeout=timeout,
                          follow_redirects=True)
        except httpx.HTTPError as e:
            _bandcamp_unreachable(f"no connection ({type(e).__name__})")
            return ""
    if r.status_code != 200 or not r.text:
        return ""
    return r.text


def _bandcamp_slug(value, sep):
    """`value` as a Bandcamp slug: folded, `&` → `and`, other junk → *sep*."""
    text = str(value or "").strip().lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", sep, text).strip(sep)


def _bandcamp_subdomain_candidates(artist):
    """The Bandcamp subdomains *artist* may use, best first.

    Bandcamp subdomains are written without separators ("King Buffalo" →
    `kingbuffalo`); the hyphenated form is kept as a second candidate because
    some artists do register that way.
    """
    out = [_bandcamp_slug(artist, ""), _bandcamp_slug(artist, "-")]
    return [s for i, s in enumerate(out) if s and s not in out[:i]]


def _bandcamp_tralbum(page):
    """The page's `data-tralbum` JSON, or None when the page carries none."""
    match = _BANDCAMP_TRALBUM_RE.search(page or "")
    if not match:
        return None
    try:
        data = json.loads(_html.unescape(match.group(1)))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _bandcamp_tracks(tralbum):
    """[{position, title}] from a tralbum's `trackinfo` (its own track list)."""
    out = []
    for row in (tralbum or {}).get("trackinfo") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if title:
            out.append({"position": row.get("track_num"), "title": title})
    return out


def _bandcamp_tags(page):
    """The album's own tags, junk filtered like ListenBrainz free tags.

    A tag is kept when it is not a mood word (`LB_MOOD_WORDS`) and is not the
    same word as one already kept once punctuation and spacing are ignored —
    Bandcamp lists literal duplicates of its own slugs side by side ("doom
    metal" and "doommetal"), and those are one tag, not two.
    """
    from server import discovery

    start = page.find("tralbum-tags tralbum-tags-nu")
    if start < 0:
        start = page.find("tralbumData tralbum-tags")
    if start < 0:
        return []
    end = page.find("</div>", start)
    block = page[start:end if end > start else len(page)]
    out, seen = [], set()
    for match in _BANDCAMP_TAG_RE.finditer(block):
        name = _html.unescape(match.group(1)).strip()
        key = re.sub(r"[^a-z0-9]+", "", name.lower())
        if not name or not key or key in seen:
            continue
        if name.lower() in discovery.LB_MOOD_WORDS:
            continue
        seen.add(key)
        out.append(name)
    return out


def _bandcamp_is_album(tralbum, album, titles=None):
    """Whether a tralbum is the album we asked about (never a guess)."""
    from server import discovery

    if album:
        stated = discovery.norm((tralbum.get("current") or {}).get("title"))
        if stated != discovery.norm(album):
            return False
    page_titles = {discovery.norm(t["title"]) for t in _bandcamp_tracks(tralbum)}
    ours = [discovery.norm(t) for t in titles or [] if str(t or "").strip()]
    if ours and page_titles and not (set(ours) & page_titles):
        return False
    return True


def bandcamp_album(artist="", album="", titles=None):
    """Bandcamp's tags for one album: {"genres", "tracks", "url"} or None.

    `titles` (optional) are the caller's own track titles for that album: they
    are what confirms the page that came back is this release, so a
    same-named record by another band cannot donate its tags. None means "no
    such album for this client" (a wrong slug, no Bandcamp presence, the
    interstitial) — never an empty answer dressed up as one.
    """
    name = str(album or "").strip()
    if not name:
        return None
    for sub in _bandcamp_subdomain_candidates(artist):
        url = f"https://{sub}.bandcamp.com/album/{_bandcamp_slug(name, '-')}"
        page = _bandcamp_get(url)
        if not page:
            continue
        tralbum = _bandcamp_tralbum(page)
        if not tralbum or not _bandcamp_is_album(tralbum, name, titles):
            continue
        return {"genres": _bandcamp_tags(page),
                "tracks": _bandcamp_tracks(tralbum), "url": url}
    return None


# --------------------------------------------------------------------------- #
# Genre chain — one merge point for every genre source
# --------------------------------------------------------------------------- #
# Order used when mlo.config `genre_sources` is empty. It is a PRIORITY LIST:
# the sources are asked in this order until every track is full — the merge
# takes each answer in this order and STOPS once the writer's own policy can
# write a complete list from what it has (see `_genre_complete`), so the
# position of a source is what a track's genre picks look like and the ones
# below it are the fallbacks, not a second opinion.
# `mlo.config.DEFAULT_CONFIG["genre_sources"]` is this list and
# `normalize_config` migrates both previously shipped
# defaults into it (`tools/test_genres.py` asserts the two are equal).
#
#   a. rateyourmusic  release page — per TRACK where the page states one, else
#                     album, then the artist page. FIRST by the user's own
#                     requirement: its curated genre + descriptor
#                     classification is the one they want. Its pages are read
#                     live when a `rym_cookie` is configured and from an
#                     archived snapshot when it is not (`rym_archive_fallback`,
#                     default ON), so this source answers on a default install
#                     too; blocked or never archived, it reports exactly what
#                     happened — see RYM_BASE and `_rym_note`.
#   b. musicbrainz    recording → release → release group → artist. Curated
#                     per recording, and the app's identity anchor. SECOND by
#                     the user's own requirement — it sits above every other
#                     keyless source, which is also what makes a default
#                     install answer with open data when RYM has nothing.
#   c. listenbrainz   recording tags → release-group → artist. Crowdsourced
#                     PER RECORDING, free, no key, MBID-native (no title
#                     guessing for a release MusicBrainz knows).
#   d. itunes         per-track `primaryGenreName`. Free, keyless, and its
#                     per-track genre is reliable for mainstream releases.
#   e. lastfm         track.getTopTags → artist.getTopTags. Crowdsourced per
#                     track and broad, but needs a free API key.
#   f. theaudiodb     searchtrack.php per track → album genre/mood. Per-track
#                     plus album metadata (biography, art), keyless.
#   g. wikidata       P136 on the recording entity, then the release group /
#                     searched entity. Curated but sparse, per recording where
#                     one is stated.
#   h. bandcamp       album page tags. Crowdsourced and strong for
#                     indie/self-released records; ALBUM level (Bandcamp
#                     states no per-track genre at all), keyless.
#   i. discogs        release styles + genres. Curated, album level, needs a
#                     token.
#   j. deezer         album genres only — album level, keyless.
#   k. spotify        ARTIST genres — artist level, needs client id+secret,
#                     the last resort (the chain only ever adds a source's
#                     answer, so the weakest one goes last).
#
# Every per-track source sits ABOVE every album-only one (bandcamp, discogs,
# deezer, spotify), which is what keeps a track's own answer ahead of an
# album-wide guess (`tools/test_genres.py` asserts that property of this list).
#
# `soulseek` is NOT in the default list: peers advertise folders and file
# names, not genres, so it can never state one — it stays handled as a
# documented no-op so a saved config listing it keeps working.
GENRE_SOURCES = ["rateyourmusic", "musicbrainz", "listenbrainz", "itunes",
                 "lastfm", "theaudiodb", "wikidata", "bandcamp", "discogs",
                 "deezer", "spotify"]


# The key an ALBUM- or ARTIST-wide answer is filed under: it applies to every
# track of the release, and the row's `level` is what says so.
_ALL_TRACKS = ""
_LEVEL_RANK = {"track": 0, "album": 1, "artist": 2}


def _genre_track_key(disc, position):
    """`"disc:position"` — the key one track's answer is filed under."""
    return f"{int(disc or 1)}:{int(position or 0)}"


def _genre_names(names):
    """Trimmed, case-insensitively deduped names, original spelling kept."""
    out = []
    for name in names or []:
        text = str(name or "").strip()
        if text and text.lower() not in {g.lower() for g in out}:
            out.append(text)
    return out


def _genre_row(level, names, title=""):
    """{"level", "genres"[, "title"]} for one answer, or None when empty."""
    clean = _genre_names(names)
    if not clean:
        return None
    row = {"level": level, "genres": clean}
    if title:
        row["title"] = str(title)
    return row


def _genre_source_skip(source, cfg):
    """WHY a source is not asked at all, or None when it is asked.

    A source that cannot answer is skipped HERE — before any request, not
    after a timeout: a missing credential (RateYourMusic's cookie, Discogs'
    token, Last.fm's key, Spotify's client id+secret) and a RateYourMusic that
    already refused this cookie (`_rym_blocked`) are both known without
    spending a request on them. The reason is what the caller reports, so "no
    data" never has to stand in for "there was nothing to ask with".
    """
    cfg = cfg or {}
    if source == "rateyourmusic":
        # No `rym_cookie` is no longer "contribute nothing". With the archive
        # fallback on, the Wayback Machine holds copies of these same pages and
        # serves them to an unattended client, so the source IS asked and what
        # the archive did with the request is reported by `_rym_note` instead of
        # a skip line: "try the archive, then say exactly what happened".
        archive = _rym_archive_on(cfg)
        # What is still skipped, because a request could only confirm it: RYM
        # refuses a client with no credential outright (see RYM_BASE), and a
        # refusal already latched this cookie off. Both cost a request and a
        # second of throttle on EVERY import to learn what the config already
        # said — which is only worth paying when no archive route exists.
        if not _rym_cookie(cfg) and not archive:
            return ("skipped: no rym_cookie in Settings → Discovery "
                    "(RateYourMusic refuses an automated client without one)")
        if _rym_blocked(cfg) and not archive:
            return ("skipped: RateYourMusic refused this cookie — set a fresh "
                    "rym_cookie in Settings → Discovery")
        return None
    if source == "lastfm" and not str(cfg.get("lastfm_api_key") or "").strip():
        return "skipped: no lastfm_api_key in Settings → Discovery"
    if source == "discogs" and not str(cfg.get("discogs_token") or "").strip():
        return "skipped: no discogs_token in Settings → Discovery"
    if source == "spotify" and not _spotify_configured(cfg):
        return ("skipped: no spotify_client_id/spotify_client_secret in "
                "Settings → Discovery")
    if source == "soulseek":
        return "skipped: Soulseek states no genres (folders and file names)"
    return None


def _genre_complete(names, limit):
    """Whether *names* fill every slot the writer would write.

    This is the chain's one notion of "the answer is already there", and it is
    judged by the policy the WRITERS apply (`mlo.genres.normalize_genres`),
    not by a second rule of the chain's own: `limit` names, and — past a
    single slot — the family in the first one. At `mb_genre_count = 2` that is
    exactly "the derived family plus one specific genre", which is what makes
    a single good answer enough; a name with no family in the vocabulary
    (an unrecognised one, or a RYM descriptor like "Concept Album") cannot
    complete a track and keeps the chain going.
    """
    if not names:
        return False
    try:
        from mlo.genres import normalize_genres
        got = normalize_genres(names, limit)
    except Exception:
        return False
    if len(got) < max(1, int(limit)):
        return False
    if int(limit) <= 1:
        return True
    try:
        from mlo.genre_vocab import is_parent
        return any(is_parent(n) for n in got)
    except Exception:
        return False


def _genre_needs_ai(genres, limit, picks):
    """Whether a track's merged list still wants the model (a tie-breaker).

    The sources are the first move: when they already fill every slot the
    writer would write (see `_genre_complete`) and they tell ONE story, there
    is nothing for the model to settle and it is not called. It IS called for
    the two cases that need it — the sources disagree (`picks` is the first
    genre of each contributing source, so two different names is a genuine
    tie), or their answer cannot fill the slots at all (thin, or empty).
    """
    if not _genre_complete(genres, limit):
        return True
    return len(set(picks or [])) > 1


# --------------------------------------------------------------------------- #
# 30-day value cache — genre answers (per album / recording) and, through the
# same helper, the cover-search image dimension probes (per image URL)
# --------------------------------------------------------------------------- #
# Genre data moves slowly, one album pass asks the same album (and the same
# recording) over and over, and every crowdsourced source here is throttled to
# ~1 req/s; an image's size likewise never changes for a URL. Answers —
# including "nothing", which must not be re-asked either — are kept in memory
# and on disk under <music>/.mlo/data/genre_cache, keyed by kind so the two
# users never collide.
_GENRE_CACHE_TTL = 30 * 86400.0
_GENRE_CACHE: dict = {}
_GENRE_CACHE_LOCK = threading.Lock()
_GENRE_CACHE_MAX = 5000
_GENRE_MISS = object()


def _genre_cache_file(ident):
    import hashlib

    folder = _data_cache_dir("genre_cache")
    if not folder:
        return None
    return os.path.join(folder, hashlib.sha1(ident.encode("utf-8")).hexdigest()
                        + ".json")


def _genre_cached(kind, key, producer):
    """`producer()` memoized 30 days by (kind, key) — memory, then disk.

    The app's one 30-day disk cache. `kind` is "album" (identity: artist +
    album, or a release-group/artist MBID), "recording" (identity: a recording
    MBID or artist + title) or "cover_dim" (identity: an image URL).
    `producer` must return JSON-serializable data or None and must never
    raise: a failing source is not allowed to take the chain down, and "no
    answer" is cached like any other answer so a rerun costs no request.
    """
    ident = f"{kind}|{key}"
    with _GENRE_CACHE_LOCK:
        hit = _GENRE_CACHE.get(ident)
    if hit is not None:
        return None if hit is _GENRE_MISS else hit
    path = _genre_cache_file(ident)
    if path:
        try:
            if (os.path.isfile(path)
                    and time.time() - os.path.getmtime(path) < _GENRE_CACHE_TTL):
                with open(path, encoding="utf-8") as fh:
                    value = json.load(fh)
                with _GENRE_CACHE_LOCK:
                    _GENRE_CACHE[ident] = _GENRE_MISS if value is None else value
                return value
        except (OSError, ValueError):
            pass
    value = producer()
    with _GENRE_CACHE_LOCK:
        if len(_GENRE_CACHE) >= _GENRE_CACHE_MAX:
            _GENRE_CACHE.clear()
        _GENRE_CACHE[ident] = _GENRE_MISS if value is None else value
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


# --------------------------------------------------------------------------- #
# Per-source answers
# --------------------------------------------------------------------------- #
def _mb_wikidata_qid(mbid, entity="release-group"):
    """The Wikidata entity a MusicBrainz entity points at, or None.

    MusicBrainz holds the relation itself (`inc=url-rels`), which is what
    makes the Wikidata tier an identity lookup instead of a title guess.
    `entity` is a MusicBrainz endpoint name ("release-group", "recording",
    "work"): the genre chain asks a RECORDING first, then the WORK it performs
    (see `_mb_recording_ids`), because a track's own item carries P136 where
    the release group carries none.
    """
    if not mbid:
        return None
    try:
        data = mb_get_cached(f"{entity}/{mbid}",
                             {"inc": "url-rels", "fmt": "json"})
    except Exception:
        return None
    return _mb_wikidata_qid_in((data or {}).get("relations"))


def _mb_wikidata_qid_in(relations):
    """The Wikidata QID of a MusicBrainz relation list, or ""."""
    for rel in relations or []:
        if str(rel.get("type") or "").strip().lower() != "wikidata":
            continue
        match = re.search(r"Q\d+",
                          str((rel.get("url") or {}).get("resource") or ""))
        if match:
            return match.group(0)
    return ""


def _mb_recording_ids(recording_mbid):
    """{"qid", "work"} for one MusicBrainz recording, both "" when none.

    ONE request (`inc=url-rels+work-rels`) answers both questions: whether the
    recording itself links a Wikidata item (rare — every popular recording
    probed on this machine had no such link), and which WORK it performs. The
    work is the practical route and it is still an identity, never a title
    guess: VERIFIED here, Queen's "Bohemian Rhapsody" recording → work
    `41c94a08…` → Wikidata Q187745, whose P136 is progressive rock / hard rock
    / progressive pop, and Pink Floyd's "Time" → work → Q641913 ("popular
    music"). A multi-word ENTITY SEARCH is not usable for this at all —
    `wbsearchentities` matches labels by prefix, so "artist title" answers
    nothing (VERIFIED: no hits for four-word research queries).
    """
    if not recording_mbid:
        return {"qid": "", "work": ""}
    try:
        data = mb_get_cached(f"recording/{recording_mbid}",
                             {"inc": "url-rels+work-rels", "fmt": "json"})
    except Exception:
        return {"qid": "", "work": ""}
    relations = (data or {}).get("relations") or []
    work = ""
    for rel in relations:
        if work:
            break
        work = str((rel.get("work") or {}).get("id") or "")
    return {"qid": _mb_wikidata_qid_in(relations), "work": work}


# --------------------------------------------------------------------------- #
# Credits / performers
# --------------------------------------------------------------------------- #
# MusicBrainz keeps the players on the RECORDING: `artist-rels` returns one
# relation per person — the relation TYPE is the role (performer, instrument,
# vocal, producer, engineer, mix, mastering, arranger, composer, lyricist,
# conductor, remixer, …), the instrument or vocal part rides in `attributes`
# ("double bass", "lead vocals"), and the person's name + MBID come along in
# the same relation, so no second request is needed to name them. `work-rels`
# says which WORK the recording performs. ONE request per recording, ONE per
# release (`recording-level-rels` + `work-level-rels` bring each track's own
# relations with the release, VERIFIED against MB while writing this), both
# through mb_get_cached so the cache and the 1 req/s etiquette apply like any
# other MB call.
CREDITS_MAX_RECORDINGS = 200   # a box set must not become 200 requests' worth
CREDITS_MAX_ROWS = 500         # …and the UI gets a capped list either way
# The tags a tagger writes when MB has nothing (Vorbis PERFORMER is the
# common one, per player, as "Name (instrument)").
CREDIT_TAG_ROLES = ("PERFORMER", "COMPOSER", "LYRICIST", "ARRANGER",
                    "CONDUCTOR", "REMIXER", "ENGINEER", "PRODUCER")


def _credit_rows(relations, rows=None):
    """Append one row per MusicBrainz relation to *rows*:
    ``{role, attributes, artist, mbid}``.

    Every artist-target relation is kept whatever its type — MB reports the
    role itself, and a release's players are split across `performer` (Vorbis
    era) and `instrument` (current schema), so filtering by type would drop
    half of them. A relation pointing at a WORK becomes a `work` row whose
    `artist` is the work's title (its own type, usually "performance", rides
    in `attributes`), which is how the recording's work stays visible.
    """
    rows = [] if rows is None else rows
    for rel in relations or []:
        if len(rows) >= CREDITS_MAX_ROWS:
            break
        attributes = [str(a).strip() for a in (rel.get("attributes") or [])
                      if str(a).strip()]
        if rel.get("target-type") == "work":
            work = rel.get("work") or {}
            title = str(work.get("title") or "").strip()
            if title:
                rows.append({"role": "work", "attributes": attributes or
                             [str(rel.get("type") or "").strip()],
                             "artist": title, "mbid": work.get("id") or ""})
            continue
        artist = rel.get("artist") or {}
        name = str(artist.get("name") or rel.get("target-credit") or "").strip()
        if not name:
            continue
        rows.append({"role": str(rel.get("type") or "credit").lower(),
                     "attributes": attributes, "artist": name,
                     "mbid": artist.get("id") or ""})
    return rows


def tidy_credit_rows(rows):
    """De-duplicated, role-grouped, capped credit rows — what the UI renders.

    Albums repeat the same player on every track and the fallback repeats the
    same tag on every file, so the same (role, person, instrument) is kept
    once; sorting by role groups the list the way it is displayed.
    """
    seen = set()
    out = []
    for row in rows or []:
        key = (row.get("role"), row.get("artist", "").lower(), row.get("mbid"),
               tuple(row.get("attributes") or []))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    out.sort(key=lambda r: (r.get("role") or "", r.get("artist", "").lower()))
    return out[:CREDITS_MAX_ROWS]


def credit_rows_from_tags(tags):
    """Credit rows from a file's OWN tags, for when MusicBrainz has nothing.

    `tags` is a raw tag dump (mlo.audio all_tags, keys already canonicalised to
    PERFORMER/COMPOSER/… when the container maps them). Vorbis writes
    `PERFORMER=Name (instrument)` once per player and some taggers join several
    names with "; ", so both shapes are split back apart. ponytail: a
    multi-value Vorbis tag collapses to its first value in all_tags — enough
    for a fallback, read af.audio.tags directly if every player must show.
    """
    rows = []
    if not isinstance(tags, dict):
        return rows
    by_key = {str(k).upper(): v for k, v in tags.items()}
    for role in CREDIT_TAG_ROLES:
        for part in re.split(r"\s*;\s*", str(by_key.get(role) or "")):
            part = part.strip()
            if not part:
                continue
            m = re.match(r"^(.*?)\s*\(([^()]*)\)$", part)
            name, attributes = ((m.group(1).strip(), [m.group(2).strip()])
                                if m else (part, []))
            if name:
                rows.append({"role": role.lower(), "attributes": attributes,
                             "artist": name, "mbid": ""})
    return tidy_credit_rows(rows)


def recording_credits(recording_mbid):
    """Credit rows for ONE recording (its players, plus its work).

    Raises MusicBrainzError (or httpx's own error) when MB cannot answer — the
    caller reports that reason rather than an empty result.
    """
    data = mb_get_cached(f"recording/{recording_mbid}",
                         {"inc": "artist-rels+work-rels", "fmt": "json"})
    return tidy_credit_rows(_credit_rows((data or {}).get("relations")))


def release_credits(release_mbid):
    """Credit rows for a whole RELEASE — every track's recording.

    The release request carries each track's own relations, so this stays ONE
    request per album; only the aggregate is capped, not the request count.
    """
    data = mb_get_cached(
        f"release/{release_mbid}",
        {"inc": "recordings+artist-rels+recording-level-rels+work-rels"
                "+work-level-rels", "fmt": "json"})
    rows = _credit_rows((data or {}).get("relations"))
    recordings = 0
    for medium in (data or {}).get("media") or []:
        for track in medium.get("tracks") or []:
            if (recordings >= CREDITS_MAX_RECORDINGS
                    or len(rows) >= CREDITS_MAX_ROWS):
                break
            recordings += 1
            _credit_rows((track.get("recording") or {}).get("relations"), rows)
    return tidy_credit_rows(rows)


def _itunes_track_genres(artist, album, cfg=None):
    """{"disc:position": [genre]} from Apple's per-track `primaryGenreName`.

    Free and streaming-tier: the album's edition (explicit edition first — the
    same resolution the advisory route uses) is looked up with `entity=song`,
    and every row carries its own genre. Apple serves it per track, which is
    why this source beats the album-level ones below it. {} when Apple holds
    no such album.
    """
    editions = _apple_editions(artist, album, cfg=cfg)
    cid = _advisory_int((editions[0] if editions else {}).get("collectionId"))
    if not cid:
        return {}
    data = _apple_json("/lookup", {"id": cid, "entity": "song", "limit": 200,
                                   "country": _apple_country(cfg)})
    out = {}
    for row in (data or {}).get("results") or []:
        genre = str(row.get("primaryGenreName") or "").strip()
        position = _advisory_int(row.get("trackNumber"))
        if not genre or not position:
            continue
        key = _genre_track_key(_advisory_int(row.get("discNumber")) or 1, position)
        out[key] = _genre_names((out.get(key) or []) + [genre])
    return out


def _release_artist_mbids(artist, album, release, cfg):
    """The release's artist MBIDs, resolved from the name when it has none."""
    from server import discovery

    ids = [a.get("mbid") for a in (release or {}).get("artists") or []
           if a.get("mbid")]
    if not ids and artist:
        try:
            resolved = discovery.resolve_artist_mbid(artist)
        except Exception:
            resolved = None
        if resolved:
            ids.append(resolved)
    return ids


def _genre_source_answers(source, artist, album, release, cfg, tracks):
    """One source's answers: {key: {"level", "genres"[, "title"]}}.

    `key` is a track key for a per-track answer, `_ALL_TRACKS` for an
    album/artist-wide one; both may be present and `genre_chain` merges the
    track's own answer first. Every source is asked at its best available
    level — a per-track source that cannot answer per track answers nothing
    rather than promoting an album guess to a track. Never raises: the chain
    wraps the call, and every network path inside returns "no answer".
    """
    from server import discovery

    if source == "rateyourmusic":
        # Which ROUTE answers is recorded by the request itself (`_rym_route`),
        # and `_rym_note` turns that into the report's line for this source.
        # Cleared first so the note belongs to THIS call and not to a probe that
        # ran a moment ago on another album.
        _rym_route.clear()
        # MusicBrainz's own stated page first (step 1 of `rym_genres`): it is
        # an identity, it needs no slug guess, and MusicBrainz states it as a
        # `url` relation, so learning the page costs no RYM request at all —
        # `rym_genres` then reads that page, live or from an archived snapshot
        # when there is no cookie to read the live one with.
        stated = {}
        try:
            stated = _mb_rym_links(artist, album,
                                   (release or {}).get("release_group_id")) or {}
        except Exception:
            stated = {}
        data = (rym_genres(artist, album, cfg, stated.get("album") or "",
                           archive=True)
                or rym_artist_genres(artist, cfg, archive=True))
        if not data:
            return {}
        wide = _genre_row(data.get("level") or "artist",
                          (data.get("genres") or []) + (data.get("descriptors") or []))
        answers = {_ALL_TRACKS: wide} if wide else {}
        for row in data.get("tracks") or []:
            # RYM renders one medium per page, so the row number is the
            # position on disc 1; `genre_chain` re-maps by title when that
            # does not line up with this release's own numbering.
            own = _genre_row("track", row.get("genres"), row.get("title"))
            if own:
                answers[_genre_track_key(1, row.get("position"))] = own
        return answers

    if source == "listenbrainz":
        answers = {}
        for track in tracks:
            recording = track.get("recording_mbid")
            if not recording:
                continue
            got = _genre_cached(
                "recording", f"listenbrainz|{recording}",
                lambda r=recording: discovery.listenbrainz_genre_tags(r, "recording"))
            row = _genre_row("track", ((got or {}).get("genres") or [])
                             + ((got or {}).get("tags") or []))
            if row:
                answers[_genre_track_key(track.get("disc"), track.get("position"))] = row
        rg_names = []
        rg = (release or {}).get("release_group_id")
        if rg:
            got = _genre_cached(
                "album", f"listenbrainz|release_group|{rg}",
                lambda: discovery.listenbrainz_genre_tags(rg, "release_group"))
            rg_names = ((got or {}).get("genres") or []) + ((got or {}).get("tags") or [])
        artist_names = []
        for mbid in _release_artist_mbids(artist, album, release, cfg):
            got = _genre_cached(
                "album", f"listenbrainz|artist|{mbid}",
                lambda m=mbid: discovery.listenbrainz_genre_tags(m, "artist"))
            artist_names += ((got or {}).get("genres") or []) + ((got or {}).get("tags") or [])
            break
        wide = _genre_row("album" if rg_names else "artist", rg_names + artist_names)
        if wide:
            answers[_ALL_TRACKS] = wide
        return answers

    if source == "musicbrainz":
        answers = {}
        for track in tracks:
            row = _genre_row("track", track.get("genres"))
            if row:
                answers[_genre_track_key(track.get("disc"), track.get("position"))] = row
        # The ALBUM tier: the release's own genres, then its release group's.
        wide, level = [], "album"
        if release:
            wide += list(release.get("genres") or [])
            rg = release.get("release_group_id")
            if rg:
                wide += _genre_cached("album", f"musicbrainz|release_group|{rg}",
                                      lambda r=rg: release_group_genres(r)) or []
        else:
            # No release in hand: the release group and the artist are what a
            # genre cascade normally falls back on, resolved from the names.
            rg = None
            try:
                rg = discovery.resolve_release_group(artist, album, cfg)
            except Exception:
                rg = None
            if rg and rg.get("mbid"):
                wide += _genre_cached("album", f"musicbrainz|release_group|{rg['mbid']}",
                                      lambda m=rg["mbid"]: release_group_genres(m)) or []
        # …then the ARTIST tier, which is what answers when both of those state
        # nothing. Which one the row IS has to be said by where its names came
        # from rather than assumed: an artist's genre labelled `album` would be
        # reported as something it is not AND would outrank the album tier of
        # the next source in the chain (`merge` ranks by this label).
        artist_names = []
        for mbid in _release_artist_mbids(artist, album, release, cfg):
            artist_names = _genre_cached(
                "album", f"musicbrainz|artist|{mbid}",
                lambda m=mbid: artist_genres(m)) or []
            break
        if not wide:
            level = "artist"
        wide += artist_names
        row = _genre_row(level, wide)
        if row:
            answers[_ALL_TRACKS] = row
        return answers

    if source == "itunes":
        def look():
            per_track = _itunes_track_genres(artist, album, cfg)
            if per_track:
                return {"tracks": per_track}
            # Apple's album search is the fallback when its per-track lookup
            # has nothing: one primary genre for the album (still better than
            # no answer, and the caller marks it album level).
            names = _genre_names([r.get("genre") for r in
                                  discovery.itunes_search_album(artist, album,
                                                                limit=1)])
            return {"album": names} if names else None

        got = _genre_cached(
            "album", f"itunes|album|{_norm_compare(artist)}|{_norm_compare(album)}",
            look) or {}
        answers = {}
        for key, names in (got.get("tracks") or {}).items():
            row = _genre_row("track", names)
            if row:
                answers[key] = row
        row = _genre_row("album", got.get("album"))
        if row:
            answers[_ALL_TRACKS] = row
        return answers

    if source == "wikidata":
        answers = {}
        # The TRACK's own item first, and by identity: the recording's
        # Wikidata relation when it has one, else the Wikidata item of the
        # WORK that recording performs (see `_mb_recording_ids`). Both are
        # `level: track` answers.
        for track in tracks:
            recording = str(track.get("recording_mbid") or "").strip()
            title = str(track.get("title") or "").strip()
            if not recording:
                continue
            ids = _genre_cached("recording", f"wikidata|recording|{recording}",
                                lambda r=recording: _mb_recording_ids(r)) or {}
            qid = str(ids.get("qid") or "")
            work = str(ids.get("work") or "")
            if not qid and work:
                qid = _genre_cached("recording", f"wikidata|work|{work}",
                                    lambda w=work: _mb_wikidata_qid(w, "work")) or ""
            if not qid:
                continue
            got = _genre_cached("recording", f"wikidata|qid|{qid}",
                                lambda q=qid: discovery.wikidata_genres(qid=q)) or {}
            row = _genre_row("track", got.get("genres"), title)
            if row:
                answers[_genre_track_key(track.get("disc"),
                                         track.get("position"))] = row
        # Then the release group's own entity (or the searched "artist album"
        # entity) as today — album level, the fallback tier.
        rg = (release or {}).get("release_group_id") or ""
        term = " ".join(t for t in (artist, album) if t)
        got = _genre_cached(
            "album", f"wikidata|{rg}|{_norm_compare(term)}",
            lambda: discovery.wikidata_genres(qid=_mb_wikidata_qid(rg), term=term)) or {}
        row = _genre_row("album", got.get("genres"))
        if row:
            answers[_ALL_TRACKS] = row
        return answers

    if source == "lastfm":
        answers = {}
        for track in tracks:
            title = track.get("title")
            if not title:
                continue
            names = _genre_cached(
                "recording", f"lastfm|track|{_norm_compare(artist)}|{_norm_compare(title)}",
                lambda t=title: discovery.lastfm_track_genres(artist, t, cfg)) or []
            row = _genre_row("track", names)
            if row:
                answers[_genre_track_key(track.get("disc"), track.get("position"))] = row
        names = _genre_cached("album", f"lastfm|artist|{_norm_compare(artist)}",
                              lambda: discovery.lastfm_artist_genres(artist, cfg)) or []
        row = _genre_row("artist", names)
        if row:
            answers[_ALL_TRACKS] = row
        return answers

    if source == "discogs":
        names = _genre_cached("album", f"discogs|{_norm_compare(artist)}|{_norm_compare(album)}",
                              lambda: discovery.discogs_album_genres(artist, album, cfg)) or []
        row = _genre_row("album", names)
        return {_ALL_TRACKS: row} if row else {}

    if source == "theaudiodb":
        answers = {}
        # `searchtrack.php` first — TheAudioDB serves a track row (genre,
        # style, mood) per track, and it is the source's per-track tier.
        for track in tracks:
            title = str(track.get("title") or "").strip()
            if not title:
                continue
            names = _genre_cached(
                "recording",
                f"theaudiodb|track|{_norm_compare(artist)}|{_norm_compare(title)}",
                lambda t=title: _audiodb_track_genre_names(artist, t)) or []
            row = _genre_row("track", names, title)
            if row:
                answers[_genre_track_key(track.get("disc"),
                                         track.get("position"))] = row
        # The album row stays as the fallback tier for the tracks it did not
        # answer for (and for a release whose tracks have no names yet).
        names = _genre_cached("album", f"theaudiodb|{_norm_compare(artist)}|{_norm_compare(album)}",
                              lambda: _audiodb_genre_names(artist, album)) or []
        row = _genre_row("album", names)
        if row:
            answers[_ALL_TRACKS] = row
        return answers

    if source == "bandcamp":
        # Album-level by nature (see the Bandcamp section above): its own
        # track titles confirm the page is this release, and the tags are the
        # album's. One request per album, cached 30 days like every source.
        titles = [t.get("title") for t in tracks if str(t.get("title") or "").strip()]
        got = _genre_cached(
            "album", f"bandcamp|{_norm_compare(artist)}|{_norm_compare(album)}",
            lambda: bandcamp_album(artist, album, titles)) or {}
        row = _genre_row("album", got.get("genres"))
        return {_ALL_TRACKS: row} if row else {}

    if source == "deezer":
        names = _genre_cached("album", f"deezer|{_norm_compare(artist)}|{_norm_compare(album)}",
                              lambda: discovery.album_genres(artist, album, cfg=cfg)) or []
        row = _genre_row("album", names)
        return {_ALL_TRACKS: row} if row else {}

    if source == "spotify":
        # Artist-level, honest about it: Spotify states no per-track genre at
        # all. Skipped entirely (no request) until its credentials are set.
        if not _spotify_configured(cfg):
            return {}
        names = _genre_cached(
            "album", f"spotify|artist|{_norm_compare(artist)}",
            lambda: discovery.spotify_artist_genres(artist, cfg)) or []
        row = _genre_row("artist", names)
        return {_ALL_TRACKS: row} if row else {}

    if source == "soulseek":
        # Peers advertise folders and file names, not genres. Nothing here can
        # state a genre, so this source is a no-op by design — it stays in the
        # order so a saved config listing it keeps working.
        return {}
    return {}


def _audiodb_genre_names(artist, album):
    """TheAudioDB's album genre + mood (its two genre-ish fields)."""
    from server import discovery

    row = discovery.audiodb_album(artist, album) or {}
    return [g for g in (row.get("genre"), row.get("mood")) if g]


def _audiodb_track_genre_names(artist, title):
    """TheAudioDB's genre + style for ONE track, [] when it states none.

    `strMood` is a mood word ("Sad") and never a genre, so it is left out of
    the per-track row exactly as ListenBrainz's mood tags are.
    """
    from server import discovery

    row = discovery.audiodb_track(artist, title) or {}
    return [g for g in (row.get("genre"), row.get("style")) if g]


def _answer_by_title(answers, title):
    """The per-track answer whose own title is ours (the fallback mapping).

    RYM's rows carry the release page's titles; when its numbering does not
    line up with ours the title is what maps them. An unmatched title yields
    None — never a neighbouring track's genres.
    """
    want = _norm_compare(title)
    if not want:
        return None
    for key, row in answers.items():
        if key == _ALL_TRACKS or not row.get("title"):
            continue
        if _norm_compare(row["title"]) == want:
            return row
    return None


def _genre_ai_rank(cfg, artist, album, title, candidates, count, extra=None):
    """The AI's take on a track's genres, canonicalized — or None.

    `server.genre_ai.infer_genres` behind the two gates that make this step
    invisible when it is off (`ai_genre_inference`, and an endpoint actually
    configured), and behind a blanket except: a model that times out, refuses
    or answers junk must leave the source list EXACTLY as it was, because the
    import it feeds has to finish either way. None means "no opinion".

    Imported lazily, like the engine imports above: `server.ai` imports THIS
    module for the User-Agent, so a module-level import would be a cycle.

    The answer goes through `mlo.genres.normalize_genres` before anyone sees
    it: the model named the SPECIFIC genres, and the normalizer resolves each
    to MusicBrainz's own spelling, derives the FAMILY of the first one and
    puts it first — so the list this returns is the full one the writers
    store and the grader reads.
    """
    if not (cfg or {}).get("ai_genre_inference"):
        return None
    try:
        from server import ai as ai_mod
        if not ai_mod.ai_configured(cfg):
            return None
        from server.genre_ai import infer_genres
        from mlo.genres import normalize_genres
        names = infer_genres(artist=artist, album=album, title=title or "",
                             candidates=candidates, count=count, extra=extra)
        return normalize_genres(names, count) if names else None
    except Exception:
        return None


def genre_chain(artist="", album="", release=None, limit=None, sources=None,
                cfg=None, files=None, progress=None):
    """Per-track genres for a release, merged from the configured sources.

    The sources are asked IN ORDER until every track is full, and no further:
    the default order (`GENRE_SOURCES`, and the shipped
    `mlo.config.DEFAULT_CONFIG["genre_sources"]`) is RateYourMusic (per track
    where its page states one, else album) → MusicBrainz (recording → release
    → release group → artist) → ListenBrainz (recording → release group →
    artist) → iTunes (`primaryGenreName`, per track) → Last.fm (track →
    artist) → TheAudioDB (`searchtrack.php` per track → album) → Wikidata (the
    recording's P136 → release group/entity) → Bandcamp (album tags) → Discogs
    (release styles) → Deezer (album genres) → Spotify (artist genres).
    `mlo.config` migrates every previously shipped default list onto this one,
    so an install that never chose an order gets it; a customised list is
    honoured as written.

    "Full" is the WRITER's own policy applied to the merged list
    (`_genre_complete`): at `mb_genre_count = 2` one good specific genre plus
    its derived family IS the track's answer, so the sources below the one
    that supplied it are never asked — `stopped_after` names the source that
    filled the release, and `asked` lists what was actually consulted. A
    source that cannot answer at all is skipped BEFORE any request —
    without its credential (RateYourMusic's `rym_cookie` and no archive to fall
    back on, Discogs' token, Last.fm's key, Spotify's id+secret), already
    refused this run (RateYourMusic), or stating no genres by design
    (Soulseek) — and reported by name in `skipped`.

    `notes` carries one sentence per source that could not be used, and for
    RateYourMusic it carries more: that source has a second route (an archived
    snapshot of the same page, `_rym_archive_get`), so an answer that came from
    one is reported there — which capture, and its date — and so is "neither
    route answered". Nothing else ever writes a note for a source that DID
    answer.

    Every source that answers contributes; the merged list is deduped
    case-insensitively and capped at `limit` (default `mb_genre_count` from
    Settings → Import, whose shipped value lives in
    `mlo.config.DEFAULT_CONFIG`) **per track**. The names keep the spelling
    their source used — this is the merge point, not a writer: the tag writers
    canonicalize through `mlo.genres.normalize_genres` (which resolves each
    name to MusicBrainz's own, derives the family and puts it first).

    Each track's own answer is merged first, then the release-wide one, so a
    track that states its own genre keeps it ahead of the album's fallback.
    A source that only knows album- or artist-wide answers (Bandcamp, Discogs,
    Deezer, Spotify, and RYM/TheAudioDB/Wikidata when their per-track tier is
    silent) is marked `level: "album"`/`"artist"` in the provenance, never
    `track` — nothing pretends to be per-track. `level_counts` totals those
    tiers over the release's files, and a track whose `levels` entry is not
    `track` was answered by its ALBUM (or artist) — which, with two slots, is
    often the whole answer.

    `files` (optional) are the album's audio files; they key the provenance
    maps by path (`sources`, `levels`), which is what the UI shows. A source
    that cannot answer contributes nothing and is reported in `notes` — it
    is never filled in from a guess.

    `progress` (optional) is called as ``progress(i, total, source)`` before
    each source is asked, so a caller can show which source the chain is
    waiting on; a hook that raises is ignored.

    With `ai_genre_inference` on AND an AI endpoint configured, the model is a
    TIE-BREAKER rather than the first move: it is handed the merged list only
    when the sources did not settle the track themselves — they disagree (two
    of them name a different top genre) or their answer cannot fill the slots
    (`_genre_needs_ai`) — and a usable answer REPLACES the merged list. It
    ranks the SPECIFIC genres (never the family, which the app derives) and
    nothing else orders them. The answer is canonicalized through
    `mlo.genres.normalize_genres`, the disk cache/gates are unchanged, and its
    path gains "ai" in `sources`. With the setting off, no endpoint, or an
    unusable answer, every field is exactly what the sources alone produced.

    Returns {"genres": [...], "per_track": {(disc, position): [...]},
    "per_track_sources": {...}, "per_track_levels": {...},
    "per_track_trimmed": {(disc, position): [name, ...]},
    "sources": {path: [source, ...]}, "levels": {path: "track"|"album"|
    "artist"}, "level_counts": {tier: n}, "per_source": {source: [...]},
    "per_source_counts": {source: {"names": n, "tracks": n}}, "asked":
    [source, ...], "order": [source, ...] (the configured list, in that
    order), "stopped_after": source|None, "skipped": {source: reason},
    "trimmed": [name, ...], "notes": {source: reason}}.
    """
    if cfg is None:
        try:
            from mlo.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    order = [str(s).strip().lower()
             for s in (sources if sources is not None
                       else (cfg.get("genre_sources") or GENRE_SOURCES))
             if str(s).strip()]
    if limit is None:
        from mlo.config import DEFAULT_CONFIG
        try:
            # The shipped default has ONE home: a literal here drifts the
            # moment `mb_genre_count` changes (it is Settings → Import's value).
            limit = max(1, int(cfg.get("mb_genre_count")
                               or DEFAULT_CONFIG["mb_genre_count"]))
        except (TypeError, ValueError):
            limit = DEFAULT_CONFIG["mb_genre_count"]
    artist = str(artist or "").strip()
    album = str(album or "").strip()
    tracks = list((release or {}).get("media") or [])

    # The merge comes FIRST, before any source is asked: it is what decides
    # when the sources can stop being asked (`tracks_full` below), and it is
    # the same function the answer is built with afterwards.
    def merge(track):
        """(genres, sources, level, picks, dropped) in ask order.

        `track=None` is the release-wide answer (every row every source has,
        the release-wide ones first); for a track it is that track's own
        answer, then the release-wide one. `picks` is the first genre of each
        contributing source — the tie-break signal `_genre_needs_ai` reads —
        and `dropped` is what the cap left out, for the report.
        """
        pairs = []
        for source in order:
            answers = answers_by_source.get(source) or {}
            if track is None:
                rows = [answers[key] for key in
                        sorted(answers, key=lambda k: (k != _ALL_TRACKS, k))]
            else:
                rows = []
                key = _genre_track_key(track.get("disc"), track.get("position"))
                row = answers.get(key) or _answer_by_title(answers, track.get("title"))
                if row:
                    rows.append(row)
                wide = answers.get(_ALL_TRACKS)
                if wide and wide is not row:
                    rows.append(wide)
            for row in rows:
                for name in row.get("genres") or []:
                    pairs.append((str(name).strip(), source,
                                  row.get("level") or "album"))
        kept, seen = [], set()
        for name, source, level in pairs:
            low = name.lower()
            if not name or low in seen:
                continue
            seen.add(low)
            kept.append((name, source, level))
        dropped = [n for n, _s, _l in kept[limit:]]
        kept = kept[:limit]
        level = None
        for _name, _source, got in kept:
            if level is None or _LEVEL_RANK.get(got, 9) < _LEVEL_RANK.get(level, 9):
                level = got
        picks = {}
        for name, source, _level in kept:
            picks.setdefault(source, name)
        return ([n for n, _s, _l in kept],
                list(dict.fromkeys(s for _n, s, _l in kept)), level,
                list(picks.values()), dropped)

    def tracks_full():
        """Whether every track already holds what the writer would write.

        This is the early stop. At `mb_genre_count = 2` one good specific
        genre plus its derived family IS the track's answer, so the sources
        below the one that supplied it are never asked — a source that would
        only repeat the answer must not be paid a request for it. Every track
        has to be full: a release whose tracks are answered unevenly keeps
        going for the sake of the ones still empty. A release with no track
        identity at all (the Auto-tagging hook) is judged on its album-wide
        answer instead.
        """
        if not tracks:
            return _genre_complete(merge(None)[0], limit)
        return all(_genre_complete(merge(t)[0], limit) for t in tracks)

    answers_by_source, per_source, per_source_counts = {}, {}, {}
    notes, skipped, asked = {}, {}, []
    stopped_after = None
    for i, source in enumerate(order, 1):
        skip = _genre_source_skip(source, cfg)
        if skip:
            # Known-blocked or unconfigured: NOT asked at all, and the report
            # says which of the two it was — "no data" would hide a setting
            # the user can fix.
            notes[source] = skipped[source] = skip
            continue
        if progress is not None:
            # "source i/N", for a caller that shows a live bar: a source can
            # spend seconds on the network, and the hook must never be able
            # to break the chain.
            try:
                progress(i, len(order), source)
            except Exception:
                pass
        asked.append(source)
        try:
            answers = _genre_source_answers(source, artist, album, release,
                                            cfg, tracks) or {}
        except Exception as e:
            notes[source] = f"failed: {e}"
            continue
        if not answers:
            # A source that just refused itself says so instead of "no data":
            # RateYourMusic latches its refusal, so the same helper that skips
            # it up front now reports WHY, and the wizard shows the setting to
            # fix rather than an empty answer. RYM's sentence is its own
            # (`_rym_note`): it has a SECOND route — an archived snapshot — so
            # the report says which of the two was tried and what came of it.
            note = _rym_note(cfg) if source == "rateyourmusic" else ""
            notes[source] = note or _genre_source_skip(source, cfg) or "no data"
            continue
        if source == "rateyourmusic":
            # The one source whose provenance the report has to carry: an
            # answer read off an archived snapshot is RYM's own data, but it is
            # a capture from a stated date, and the user has to see that.
            note = _rym_note(cfg, answered=True)
            if note:
                notes[source] = note
        answers_by_source[source] = answers
        # The release-wide answer first, then the per-track ones: this list is
        # the album summary the caller may still want for a release whose
        # tracks carry no identity of their own.
        every = []
        for key in sorted(answers, key=lambda k: (k != _ALL_TRACKS, k)):
            every += answers[key].get("genres") or []
        per_source[source] = _genre_names(every)
        # How much this source actually said: a release-wide row answers every
        # track of the release, a per-track row exactly one.
        wide = len(tracks) if answers.get(_ALL_TRACKS) else 0
        own = len([k for k in answers if k != _ALL_TRACKS])
        per_source_counts[source] = {"names": len(per_source[source]),
                                     "tracks": wide + own}
        if tracks_full():
            stopped_after = source
            break

    per_track, per_track_sources, per_track_levels = {}, {}, {}
    per_track_trimmed = {}
    # What the prompt may know about the release beyond its genres; empty
    # values are dropped by the prompt builder, so a release with no date or
    # country simply names itself.
    extra = {"year": str((release or {}).get("date") or "")[:4],
             "country": (release or {}).get("country") or ""}

    for track in tracks:
        genres, contributors, level, picks, dropped = merge(track)
        if not genres:
            continue
        # The sources are the candidates (best first) and the model ranks
        # them into the hierarchy; a track the model has nothing to say about
        # keeps the merged order untouched. It is only ASKED when the sources
        # could not settle the track themselves (`_genre_needs_ai`).
        ranked = (_genre_ai_rank(cfg, artist, album, track.get("title"),
                                 genres, limit, extra)
                  if _genre_needs_ai(genres, limit, picks) else None)
        if ranked:
            genres = ranked
            contributors = list(dict.fromkeys(contributors + ["ai"]))
        key = (int(track.get("disc") or 1), int(track.get("position") or 0))
        per_track[key] = genres
        per_track_sources[key] = contributors
        per_track_levels[key] = level
        if dropped:
            per_track_trimmed[key] = dropped

    merged, album_contributors, album_level, album_picks, album_dropped = merge(None)
    ranked = (_genre_ai_rank(cfg, artist, album, "", merged, limit, extra)
              if _genre_needs_ai(merged, limit, album_picks) else None)
    if ranked:
        merged = ranked
        album_contributors = list(dict.fromkeys(album_contributors + ["ai"]))
    out_sources, out_levels = {}, {}
    for path in files or []:
        key = None
        try:
            from server import soulseek_auto
            key = soulseek_auto._parse_trackno(str(path))
        except Exception:
            key = None
        if key in per_track_sources:
            out_sources[str(path)] = list(per_track_sources[key])
            out_levels[str(path)] = per_track_levels.get(key)
        else:
            # No release identity to key a track on (the Auto-tagging hook has
            # only the file): the release-wide answer is what that file gets,
            # and it says so.
            out_sources[str(path)] = list(album_contributors)
            out_levels[str(path)] = album_level

    # How many tracks each tier ended up answering, for the report: with two
    # slots an ALBUM-level genre is often the whole answer ("shoegaze" +
    # "rock"), and a caller that shows this can say so instead of presenting
    # it as a track's own fact.
    level_counts = {name: 0 for name in _LEVEL_RANK}
    for got in out_levels.values():
        if got in level_counts:
            level_counts[got] += 1

    return {"genres": merged[:limit], "per_track": per_track,
            "per_track_sources": per_track_sources,
            "per_track_levels": per_track_levels,
            "per_track_trimmed": per_track_trimmed,
            "trimmed": album_dropped,
            "sources": out_sources, "levels": out_levels,
            "level_counts": level_counts,
            "per_source": per_source, "per_source_counts": per_source_counts,
            "asked": asked, "order": list(order),
            "stopped_after": stopped_after,
            "skipped": skipped, "notes": notes}



# Fixed genre buckets for GET /api/genres/facets. Every library genre lands in
# exactly one bucket (the first keyword match wins, "Other" for the rest) so
# the UI can offer coarse filters without carrying a full taxonomy. Order
# matters: "folk metal" must count as Metal before Folk sees it.
GENRE_CATEGORIES = [
    ("Metal", ("metal", "doom", "sludge", "djent", "thrash", "grindcore",
               "deathcore", "metalcore", "blackgaze")),
    ("Rock", ("rock", "grunge", "punk", "shoegaze", "psychedelic", "emo",
              "indie", "britpop", "garage", "gothic", "post-punk", "surf")),
    ("Electronic", ("electronic", "techno", "house", "trance", "ambient",
                    "idm", "drum and bass", "dnb", "dubstep", "synth", "electro",
                    "breakbeat", "edm", "downtempo", "dub", "jungle", "glitch")),
    ("Hip-Hop", ("hip hop", "hip-hop", "rap", "trap", "grime", "boom bap")),
    ("Jazz", ("jazz", "bebop", "swing", "fusion", "bossa")),
    ("Classical", ("classical", "baroque", "romantic", "opera", "chamber",
                   "symphony", "orchestral", "choral", "medieval", "renaissance")),
    ("Folk", ("folk", "country", "bluegrass", "celtic", "world", "americana",
              "traditional")),
    ("Soul & Funk", ("soul", "funk", "r&b", "rhythm and blues", "disco",
                     "motown", "gospel")),
    ("Pop", ("pop", "vocal", "ballad", "schlager")),
]
OTHER_CATEGORY = "Other"


def genre_category(name):
    """The fixed bucket a genre name belongs to (never None)."""
    text = str(name or "").strip().lower()
    if not text:
        return OTHER_CATEGORY
    for category, keys in GENRE_CATEGORIES:
        if any(k in text for k in keys):
            return category
    return OTHER_CATEGORY


def search_releases(query, limit=10, mode="release"):
    """Release search with multiple strategies.

    mode:
      * release  — free-text title/artist search
      * track    — search by track title
      * catno    — search by catalog number  (catno:"CK 62240")
      * barcode  — search by barcode         (barcode:074643924526)
    """
    q = (query or "").strip()
    if not q:
        return []
    if mode == "catno":
        q = f'catno:"{q}"'
    elif mode == "barcode":
        q = f"barcode:{q}"
    elif mode == "track":
        q = f'track:"{q}"'
    try:
        data = mb_get_cached("release", {"query": q, "limit": limit, "fmt": "json"})
        out = []
        for r in data.get("releases", []):
            credit = "".join(
                (ac.get("name", "") + (ac.get("joinphrase", "") or ""))
                for ac in r.get("artist-credit", [])
            )
            label = ""
            for li in r.get("label-info", []) or []:
                if li.get("catalog-number"):
                    label = li["catalog-number"]
                    break
            out.append({
                "id": r.get("id"),
                "title": r.get("title"),
                "date": r.get("date"),
                "artist": credit,
                "country": r.get("country"),
                "status": r.get("status"),
                "catalog_number": label,
                "barcode": r.get("barcode") or "",
            })
        return out
    except Exception as e:
        return {"error": str(e)}


def search_artists(query, limit=5):
    try:
        data = mb_get_cached("artist", {"query": query, "limit": limit, "fmt": "json"})
        # The search index stores artists' aliases, so the top bar's dropdown
        # can show "稲葉曇 (inabakumori)" without a second request per row (see
        # `alias_for`).
        return [{"id": a.get("id"), "name": a.get("name"), "type": a.get("type"),
                 "alias": alias_for(a.get("aliases"), None, a.get("name"))}
                for a in data.get("artists", [])]
    except Exception as e:
        return {"error": str(e)}


# --------------------------------------------------------------------------- #
# Generic browse: search + artist / release-group / recording pages
# (used by the in-app MusicBrainz browser)
# --------------------------------------------------------------------------- #
MB_ENTITIES = ("artist", "release-group", "release", "recording")

# --------------------------------------------------------------------------- #
# The search field catalogue — what the search box may put in a query
# --------------------------------------------------------------------------- #
# MusicBrainz's own index fields, transcribed from the "Search Fields" tables of
# its API documentation (https://musicbrainz.org/doc/MusicBrainz_API/Search, one
# table per index). NOT invented: every name below is a field that index really
# answers, and every `example` is a value that works against the live index.
# This dict is the ONE source of truth: `search_help()` serves it to the client,
# so the box's completion list and its syntax help cannot drift from what the
# server sends to the index.
#
#   field    the index field, spelled as MusicBrainz spells it in a query
#   kind     text    — free text (a name, a title, a comment)
#            enum    — a closed vocabulary ("Album", "Official", …)
#            date    — a date ("1997-05-21", "1997", "1997-05")
#            number  — a count, a position, milliseconds
#            boolean — "true" / "false"
#            id      — a MusicBrainz ID (a UUID)
#            code    — a fixed-format code (ISO 3166-1 country, 639-3 language,
#                      15924 script, IPI, ISNI, barcode, ASIN, ISRC, ISWC)
#   quotes   whether the value may be quoted: true for `text` and `enum` values,
#            where quoting is what keeps a multi-word value ("Digital Media",
#            "Not applicable") ONE phrase — verified live, `format:Digital Media`
#            drops from 2.8M hits to 25k without quotes — and harmless on a
#            single word. Fixed-format values (dates, ids, codes, numbers,
#            booleans) are compared as-is and are never quoted. Attached below
#            from `_QUOTED_KINDS` rather than spelled out per field: the payload
#            carries the flag, and the rule itself lives in ONE line.
#   meaning  one line: what MusicBrainz matches, in MusicBrainz's own words
#   example  a value to insert after `field:`
MB_SEARCH_FIELDS = {
    "artist": [
        {"field": "alias", "kind": "text", "example": '"Thom Yorke"',
         "meaning": "(part of) any alias attached to the artist (diacritics are ignored)"},
        {"field": "primary_alias", "kind": "text", "example": '"Thom Yorke"',
         "meaning": "(part of) any primary alias attached to the artist (diacritics are ignored)"},
        {"field": "area", "kind": "text", "example": '"United Kingdom"',
         "meaning": "(part of) the name of the artist's main associated area"},
        {"field": "arid", "kind": "id", "example": "9a1bb5ba-1f1e-4f23-a1a4-9ef0e5d39c6c",
         "meaning": "the artist's MBID"},
        {"field": "artist", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the artist's name (diacritics are ignored)"},
        {"field": "artistaccent", "kind": "text", "example": '"Sigur Rós"',
         "meaning": "(part of) the artist's name, diacritics included"},
        {"field": "begin", "kind": "date", "example": "1985-10-01",
         "meaning": "the artist's begin date"},
        {"field": "beginarea", "kind": "text", "example": '"Oxford"',
         "meaning": "(part of) the name of the artist's begin area"},
        {"field": "comment", "kind": "text", "example": '"US progressive rock band"',
         "meaning": "(part of) the artist's disambiguation comment"},
        {"field": "country", "kind": "code", "example": "GB",
         "meaning": "the 2-letter code (ISO 3166-1 alpha-2) for the artist's main associated country"},
        {"field": "end", "kind": "date", "example": "1980-12-08",
         "meaning": "the artist's end date"},
        {"field": "endarea", "kind": "text", "example": '"London"',
         "meaning": "(part of) the name of the artist's end area"},
        {"field": "ended", "kind": "boolean", "example": "true",
         "meaning": "whether the artist has ended (is dissolved or deceased)"},
        {"field": "gender", "kind": "enum", "example": "female",
         "meaning": "the artist's gender (male, female, other or not applicable)"},
        {"field": "ipi", "kind": "code", "example": "00016388225",
         "meaning": "an IPI code associated with the artist"},
        {"field": "isni", "kind": "code", "example": "0000000122809875",
         "meaning": "an ISNI code associated with the artist"},
        {"field": "sortname", "kind": "text", "example": '"Yorke, Thom"',
         "meaning": "(part of) the artist's sort name"},
        {"field": "tag", "kind": "text", "example": '"art rock"',
         "meaning": "(part of) a tag attached to the artist"},
        {"field": "type", "kind": "enum", "example": "Group",
         "meaning": "the artist's type (person, group, orchestra, choir, character, other)"},
    ],
    "release-group": [
        {"field": "alias", "kind": "text", "example": '"OK Computer"',
         "meaning": "(part of) any alias attached to the release group (diacritics are ignored)"},
        {"field": "arid", "kind": "id", "example": "9a1bb5ba-1f1e-4f23-a1a4-9ef0e5d39c6c",
         "meaning": "the MBID of any of the release group artists"},
        {"field": "artist", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the combined credited artist name, join phrases included"},
        {"field": "artistname", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the name of any of the release group artists"},
        {"field": "comment", "kind": "text", "example": '"UK pressing"',
         "meaning": "(part of) the release group's disambiguation comment"},
        {"field": "creditname", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the credited name of any of the release group artists here"},
        {"field": "firstreleasedate", "kind": "date", "example": "1997-05-21",
         "meaning": "the release date of the earliest release in this release group"},
        {"field": "primarytype", "kind": "enum", "example": "Album",
         "meaning": "the release group's primary type (Album, Single, EP, Broadcast, Other)"},
        {"field": "reid", "kind": "id", "example": "1834eae1-741b-3c03-9ca5-0df3decb43ea",
         "meaning": "the MBID of any of the releases in the release group"},
        {"field": "release", "kind": "text", "example": '"OK Computer"',
         "meaning": "(part of) the title of any of the releases in the release group"},
        {"field": "releasegroup", "kind": "text", "example": '"OK Computer"',
         "meaning": "(part of) the release group's title (diacritics are ignored)"},
        {"field": "releasegroupaccent", "kind": "text", "example": '"Ágætis byrjun"',
         "meaning": "(part of) the release group's title, diacritics included"},
        {"field": "releases", "kind": "number", "example": "12",
         "meaning": "the number of releases in the release group"},
        {"field": "rgid", "kind": "id", "example": "b1392450-e666-3926-a536-22c65f834433",
         "meaning": "the release group's MBID"},
        {"field": "secondarytype", "kind": "enum", "example": "Compilation",
         "meaning": "any of the release group's secondary types (Soundtrack, Live, Compilation, "
                    "Remix, Demo, DJ-mix, Mixtape/Street, Field recording, Audio drama, …)"},
        {"field": "status", "kind": "enum", "example": "Official",
         "meaning": "the status of any of the releases in the release group (Official, Promotion, "
                    "Bootleg, Pseudo-Release, Withdrawn, Cancelled)"},
        {"field": "tag", "kind": "text", "example": '"art rock"',
         "meaning": "(part of) a tag attached to the release group"},
        {"field": "type", "kind": "enum", "example": "Album",
         "meaning": "the legacy single release-group type that predates multiple types"},
    ],
    "release": [
        {"field": "alias", "kind": "text", "example": '"OK Computer"',
         "meaning": "(part of) any alias attached to the release (diacritics are ignored)"},
        {"field": "arid", "kind": "id", "example": "9a1bb5ba-1f1e-4f23-a1a4-9ef0e5d39c6c",
         "meaning": "the MBID of any of the release artists"},
        {"field": "artist", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the combined credited artist name, join phrases included"},
        {"field": "artistname", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the name of any of the release artists"},
        {"field": "asin", "kind": "code", "example": "B000002U82",
         "meaning": "an Amazon ASIN for the release"},
        {"field": "barcode", "kind": "code", "example": "724385522925",
         "meaning": "the barcode for the release"},
        {"field": "catno", "kind": "text", "example": '"CDP 7 46001 2"',
         "meaning": "any catalog number for this release (insensitive to case, spaces and separators)"},
        {"field": "comment", "kind": "text", "example": '"UK pressing"',
         "meaning": "(part of) the release's disambiguation comment"},
        {"field": "country", "kind": "code", "example": "GB",
         "meaning": "the 2-letter code (ISO 3166-1 alpha-2) for any country the release was "
                    "released in"},
        {"field": "creditname", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the credited name of any of the release artists on this release"},
        {"field": "date", "kind": "date", "example": "1997-05-21",
         "meaning": "a release date for the release"},
        {"field": "discids", "kind": "number", "example": "2",
         "meaning": "the total number of disc IDs attached to all mediums of the release"},
        {"field": "discidsmedium", "kind": "number", "example": "1",
         "meaning": "the number of disc IDs attached to any one medium of the release"},
        {"field": "format", "kind": "text", "example": '"Digital Media"',
         "meaning": "the format of any medium in the release (insensitive to case, spaces and "
                    "separators)"},
        {"field": "laid", "kind": "id", "example": "df7d1c7f-ef95-425f-8eef-445b3d7bcbd9",
         "meaning": "the MBID of any of the release labels"},
        {"field": "label", "kind": "text", "example": '"Parlophone"',
         "meaning": "(part of) the name of any of the release labels"},
        {"field": "lang", "kind": "code", "example": "eng",
         "meaning": "the ISO 639-3 code for the release language"},
        {"field": "mediumid", "kind": "id", "example": "060959f3-0d81-38b3-be26-c32f80ca68e9",
         "meaning": "the MBID of any of the mediums in the release"},
        {"field": "mediums", "kind": "number", "example": "2",
         "meaning": "the number of mediums in the release"},
        {"field": "packaging", "kind": "text", "example": '"Jewel Case"',
         "meaning": "the packaging of the release (insensitive to case, spaces and separators)"},
        {"field": "primarytype", "kind": "enum", "example": "Album",
         "meaning": "the primary type of the release group for this release"},
        {"field": "quality", "kind": "number", "example": "2",
         "meaning": "the listed data quality of the release (2 for high, 1 for normal)"},
        {"field": "reid", "kind": "id", "example": "1834eae1-741b-3c03-9ca5-0df3decb43ea",
         "meaning": "the release's MBID"},
        {"field": "release", "kind": "text", "example": '"OK Computer"',
         "meaning": "(part of) the release's title (diacritics are ignored)"},
        {"field": "releaseaccent", "kind": "text", "example": '"Ágætis byrjun"',
         "meaning": "(part of) the release's title, diacritics included"},
        {"field": "rgid", "kind": "id", "example": "b1392450-e666-3926-a536-22c65f834433",
         "meaning": "the MBID of the release group for this release"},
        {"field": "script", "kind": "code", "example": "Latn",
         "meaning": "the ISO 15924 code for the release script"},
        {"field": "secondarytype", "kind": "enum", "example": "Compilation",
         "meaning": "any of the release group's secondary types for this release"},
        {"field": "status", "kind": "enum", "example": "Official",
         "meaning": "the status of the release (Official, Promotion, Bootleg, Pseudo-Release, "
                    "Withdrawn, Cancelled)"},
        {"field": "tag", "kind": "text", "example": '"art rock"',
         "meaning": "(part of) a tag attached to the release"},
        {"field": "tracks", "kind": "number", "example": "12",
         "meaning": "the total number of tracks on the release"},
        {"field": "tracksmedium", "kind": "number", "example": "11",
         "meaning": "the number of tracks on any one medium of the release"},
        {"field": "type", "kind": "enum", "example": "Album",
         "meaning": "the legacy single release-group type that predates multiple types"},
    ],
    "recording": [
        {"field": "alias", "kind": "text", "example": '"Airbag"',
         "meaning": "(part of) any alias attached to the recording (diacritics are ignored)"},
        {"field": "arid", "kind": "id", "example": "9a1bb5ba-1f1e-4f23-a1a4-9ef0e5d39c6c",
         "meaning": "the MBID of any of the recording artists"},
        {"field": "artist", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the combined credited artist name, join phrases included"},
        {"field": "artistname", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the name of any of the recording artists"},
        {"field": "comment", "kind": "text", "example": '"live, 1997-08-22: Les Eurockéennes, France"',
         "meaning": "(part of) the recording's disambiguation comment"},
        {"field": "country", "kind": "code", "example": "GB",
         "meaning": "the 2-letter code (ISO 3166-1 alpha-2) for a country any release of this "
                    "recording was released in"},
        {"field": "creditname", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the credited name of any of the recording artists on this recording"},
        {"field": "date", "kind": "date", "example": "1997-05-21",
         "meaning": "the release date of any release including this recording"},
        {"field": "dur", "kind": "number", "example": "284400",
         "meaning": "the recording duration in milliseconds"},
        {"field": "firstreleasedate", "kind": "date", "example": "1997-05-21",
         "meaning": "the release date of the earliest release including this recording"},
        {"field": "format", "kind": "text", "example": '"CD"',
         "meaning": "the format of any medium including this recording (insensitive to case, "
                    "spaces and separators)"},
        {"field": "isrc", "kind": "code", "example": "GBAYE9701274",
         "meaning": "any ISRC associated with the recording"},
        {"field": "number", "kind": "text", "example": '"A4"',
         "meaning": "the free-text number of the track on any medium including this recording"},
        {"field": "position", "kind": "number", "example": "4",
         "meaning": "the position inside its release of any medium including this recording (from 1)"},
        {"field": "primarytype", "kind": "enum", "example": "Album",
         "meaning": "the primary type of any release group including this recording"},
        {"field": "qdur", "kind": "number", "example": "142",
         "meaning": "the recording duration, quantized (duration in milliseconds / 2000)"},
        {"field": "recording", "kind": "text", "example": '"Airbag"',
         "meaning": "(part of) the recording's name, or the name of a track connected to it "
                    "(diacritics are ignored)"},
        {"field": "recordingaccent", "kind": "text", "example": '"Hoppípolla"',
         "meaning": "(part of) the recording's name, diacritics included"},
        {"field": "reid", "kind": "id", "example": "1834eae1-741b-3c03-9ca5-0df3decb43ea",
         "meaning": "the MBID of any release including this recording"},
        {"field": "release", "kind": "text", "example": '"OK Computer"',
         "meaning": "(part of) the name of any release including this recording"},
        {"field": "rgid", "kind": "id", "example": "b1392450-e666-3926-a536-22c65f834433",
         "meaning": "the MBID of any release group including this recording"},
        {"field": "rid", "kind": "id", "example": "4a7fea2e-545b-4c63-bc9a-9943cc3a29d7",
         "meaning": "the recording's MBID"},
        {"field": "secondarytype", "kind": "enum", "example": "Compilation",
         "meaning": "any of the secondary types of any release group including this recording"},
        {"field": "status", "kind": "enum", "example": "Official",
         "meaning": "the status of any release including this recording"},
        {"field": "tag", "kind": "text", "example": '"art rock"',
         "meaning": "(part of) a tag attached to the recording"},
        {"field": "tid", "kind": "id", "example": "ff7733a6-6903-3e2e-b683-6dbbb0f59ec6",
         "meaning": "the MBID of a track connected to this recording"},
        {"field": "tnum", "kind": "number", "example": "4",
         "meaning": "the position of the track on any medium including this recording (from 1, "
                    "pre-gaps at 0)"},
        {"field": "tracks", "kind": "number", "example": "12",
         "meaning": "the number of tracks on any medium including this recording"},
        {"field": "tracksrelease", "kind": "number", "example": "12",
         "meaning": "the number of tracks on any release as a whole including this recording"},
        {"field": "type", "kind": "enum", "example": "Album",
         "meaning": "the legacy single release-group type that predates multiple types"},
        {"field": "video", "kind": "boolean", "example": "false",
         "meaning": "whether the recording is a video recording"},
    ],
    # MusicBrainz documents this index too, so the catalogue covers it; the
    # browser's own search tabs are the four entities above.
    "work": [
        {"field": "alias", "kind": "text", "example": '"Airbag"',
         "meaning": "(part of) any alias attached to the work (diacritics are ignored)"},
        {"field": "arid", "kind": "id", "example": "9a1bb5ba-1f1e-4f23-a1a4-9ef0e5d39c6c",
         "meaning": "the MBID of an artist related to the work (a composer or lyricist)"},
        {"field": "artist", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the name of an artist related to the work (a composer or lyricist)"},
        {"field": "comment", "kind": "text", "example": '"orchestral version"',
         "meaning": "(part of) the work's disambiguation comment"},
        {"field": "iswc", "kind": "code", "example": "T-010.257.770-7",
         "meaning": "any ISWC associated with the work"},
        {"field": "lang", "kind": "code", "example": "eng",
         "meaning": "the ISO 639-3 code for any of the languages of the work's lyrics"},
        {"field": "recording", "kind": "text", "example": '"Airbag"',
         "meaning": "(part of) the title of a recording related to the work"},
        {"field": "recording_count", "kind": "number", "example": "12",
         "meaning": "the number of recordings related to the work"},
        {"field": "rid", "kind": "id", "example": "4a7fea2e-545b-4c63-bc9a-9943cc3a29d7",
         "meaning": "the MBID of a recording related to the work"},
        {"field": "tag", "kind": "text", "example": '"art rock"',
         "meaning": "(part of) a tag attached to the work"},
        {"field": "type", "kind": "enum", "example": "Song",
         "meaning": "the work's type (Song, Opera, Symphony, …)"},
        {"field": "wid", "kind": "id", "example": "6bba3dc1-acc7-3319-84fd-c93883b1049c",
         "meaning": "the work's MBID"},
        {"field": "work", "kind": "text", "example": '"Airbag"',
         "meaning": "(part of) the work's title (diacritics are ignored)"},
        {"field": "workaccent", "kind": "text", "example": '"Hoppípolla"',
         "meaning": "(part of) the work's title, diacritics included"},
    ],
}

_QUOTED_KINDS = ("text", "enum")     # the kinds whose value may be quoted

for _fields in MB_SEARCH_FIELDS.values():
    for _entry in _fields:
        _entry["quotes"] = _entry["kind"] in _QUOTED_KINDS

# Lucene syntax MusicBrainz's search index accepts, as the box's help shows it.
# Each one verified against the live index (tools/test_mb_search.py replays
# them against a stub; the counts below are what the live index answers):
# `artist:"Radiohead" OR artist:"Portishead"` 1,347 releases, `-format:*`
# 240,613, `date:[1990 TO 1999] AND artist:"Radiohead"` 214, `ac\/dc` 1,310
# artists, `"OK Computer"` 54. `form` is what a click inserts into the box.
MB_SEARCH_SYNTAX = [
    {"form": 'artist:"Radiohead"',
     "meaning": "a field and its value — quote a value with spaces so it stays one phrase"},
    {"form": 'artist:"Radiohead" AND releasegroup:"OK Computer"',
     "meaning": "AND narrows (OR widens; NOT or a leading - excludes)"},
    {"form": '"OK Computer"',
     "meaning": "a quoted phrase with no field searches the index's default fields"},
    {"form": "date:[1990 TO 1999]",
     "meaning": "a range, on a date or a number field (release groups: firstreleasedate)"},
    {"form": "-format:*",
     "meaning": "entities with nothing in that field at all"},
    {"form": "ac\\/dc",
     "meaning": "escape a special character with a backslash"},
]


def search_help():
    """The search box's own help: the fields it may use and the syntax to use
    them. Served to the client (`GET /api/mb/search/fields`) from this dict, so
    the completion list and the syntax hint are the same data the server sends
    to the index — a field can never be offered that MusicBrainz would not
    answer."""
    return {"fields": MB_SEARCH_FIELDS, "syntax": MB_SEARCH_SYNTAX}


def _credit(node):
    return "".join(
        (ac.get("name", "") + (ac.get("joinphrase", "") or ""))
        for ac in (node.get("artist-credit") or [])
    )


def _credit_mbid(node):
    """The first credited artist's MBID, or "" — search rows carry the
    artist credit inline, so a row can link to its artist page without a
    second request per row."""
    for ac in node.get("artist-credit") or []:
        aid = ((ac.get("artist") or {}).get("id")) if isinstance(ac, dict) else None
        if aid:
            return aid
    return ""


def _rg_types(node):
    """(primary_type, secondary_types) of a node's release group.

    MusicBrainz embeds the release group as an OBJECT for release SEARCH
    results but as a bare id STRING for browse results (inc=release-groups);
    an id, or anything else unexpected, must read as "type unknown" instead
    of raising AttributeError and failing the whole page."""
    rg = node.get("release-group") if isinstance(node, dict) else None
    if not isinstance(rg, dict):
        return "", []
    return ((rg.get("primary-type") or ""),
            [s for s in (rg.get("secondary-types") or []) if s])


def _isrc(node):
    """The ISRC string of an isrcs entry (an entity wraps it, a recording
    lookup returns it bare)."""
    if isinstance(node, dict):
        return str(node.get("isrc") or "")
    return str(node or "")


def _media_summary(node):
    """'2×CD + DVD' style summary of an entity's media list."""
    parts = []
    for m in node.get("media") or []:
        fmt = m.get("format") or "Unknown"
        if parts and parts[-1][0] == fmt:
            parts[-1][1] += 1
        else:
            parts.append([fmt, 1])
    return " + ".join((f"{n}×{f}" if n > 1 else f) for f, n in parts)


def _release_counts(node):
    """Track/disc numbers for a release: (total, per-disc breakdown).

    The breakdown keeps one number per medium joined with ' + ' — a two-disc
    edition with 10 then 11 tracks reads '10 + 11'; a single disc collapses
    to its plain count."""
    counts = [(m.get("track-count") or 0) for m in node.get("media") or []]
    total = sum(counts)
    breakdown = " + ".join(str(c) for c in counts) if len(counts) > 1 else str(total)
    return total, breakdown


SEARCH_LIMIT_MAX = 100        # WS/2 search refuses limit > 100


def _search_year(field, value):
    """`date:` / `firstreleasedate:` clause for a year or a year range.

    WS/2 indexes these as dates and answers a same-year range with that
    year's entities (verified live: `date:[1999 TO 1999]` and `date:1999`
    return the same count), so one spelling covers a bare year and the
    "1990-1999" range the search box accepts."""
    m = re.fullmatch(r"(\d{4})\s*(?:[-/]\s*(\d{4}))?", str(value or "").strip())
    if not m:
        return ""
    start, end = m.group(1), m.group(2) or m.group(1)
    return f"{field}:[{start} TO {end}]"


def search_query(entity, query="", mode="free", primary_type="", secondary_type="",
                 artist="", year="", label="", catno="", artist_id=""):
    """The Lucene query `search_mb` runs for these inputs.

    Every constraint is its own AND clause, because the index is the only
    place "albums by this artist from 1999 on that label with that catalog
    number" can be answered — filtering the returned rows afterwards cannot
    (the rows that would match are not in the page). Returned to the client
    so the browser can show the user the query behind a result list.
    Field names and value spellings are MusicBrainz's own (release:
    artist/label/catno/date/primarytype/secondarytype/arid; release-group:
    firstreleasedate and no label/catno — a release group has neither).

    WHAT THE APP DOES TO `query`, exhaustively:

      * trims leading/trailing whitespace (a query MusicBrainz would see as
        blank is not one), and
      * ANDs the constraint boxes (and the mode's catno/barcode clause) onto
        it as separate clauses.

    Nothing else: the query is NOT escaped, re-quoted, tokenised or wrapped in
    parentheses, because it is not a phrase to search for but a query the
    index parses — `artist:"Radiohead" AND releasegroup:"OK Computer"` typed
    in the box reaches MusicBrainz byte for byte (bar the trim), quoting and
    operators intact, which is the only way the field syntax the box's help
    teaches can work. Plain text behaves as before: a bare `kid a` is the
    same free-text clause it always was. The constraint values the APP wraps
    are quoted here and nowhere else, and a query that already holds the same
    catno clause does not get a second, identical one.
    """
    clauses = []
    q = str(query or "").strip()
    if entity == "release" and mode == "catno":
        q = f'catno:"{q}"'
    elif entity == "release" and mode == "barcode":
        q = f"barcode:{q}"
    if q:
        clauses.append(q)
    # An explicit Cat # field is a clause of its own; when the mode already
    # turned the same number into one, a second identical clause adds nothing.
    if entity == "release" and catno:
        same_as_query = (mode == "catno"
                         and catno.strip().lower() == str(query or "").strip().lower())
        if not same_as_query:
            clauses.append(f'catno:"{catno.strip()}"')
    if entity == "release" and label:
        clauses.append(f'label:"{label.strip()}"')
    if entity != "artist" and artist:
        clauses.append(f'artist:"{artist.strip()}"')
    if artist_id and entity in ("release-group", "release", "recording"):
        clauses.append(f"arid:{artist_id}")
    if entity in ("release", "release-group", "recording"):
        # a release group carries the earliest release date; releases and
        # recordings carry the date of the release they appear on
        year_clause = _search_year(
            "firstreleasedate" if entity == "release-group" else "date", year)
        if year_clause:
            clauses.append(year_clause)
    if entity in ("release", "release-group"):
        # quoted: several secondary types are multi-word ("Audio drama",
        # "DJ-mix", "Field recording")
        if primary_type:
            clauses.append(f'primarytype:"{primary_type}"')
        if secondary_type:
            clauses.append(f'secondarytype:"{secondary_type}"')
    return " AND ".join(clauses)


def search_mb(entity, query, limit=100, mode="free", offset=0,
              primary_type="", secondary_type="", artist="", year="",
              label="", catno="", artist_id=""):
    """Normalized MB search rows for the four browsable entities.

    mode="free" is the plain full-text search; for releases, mode="catno" /
    "barcode" search by catalog number / barcode (catalog numbers like
    'SRCS 8757' are how pressings are identified). primary_type /
    secondary_type narrow releases and release groups with MusicBrainz's own
    type qualifiers, and artist / year / label / catno are combinable
    constraints (see `search_query`). The request carries ALL of them, so a
    narrow query is answered by the index rather than by throwing rows away.

    Returns {rows, total, offset, next, query}: `total` is MusicBrainz's
    match count, `next` the offset of the following page (None at the end),
    and `query` the Lucene query actually run. Rows keep MusicBrainz's own
    relevance order, and are de-duplicated by MBID — the index repeats an
    entity when a query matches it more than once (a multi-disc release), and
    the same row twice in a table is a bug, not a second result."""
    if entity not in MB_ENTITIES:
        raise ValueError("entity must be artist, release-group, release or recording")
    # WS/2 rejects limit > 100 outright, so a caller asking for more would
    # get an error page instead of a first page: clamp, and let `next` page.
    limit = max(1, min(SEARCH_LIMIT_MAX, int(limit or SEARCH_LIMIT_MAX)))
    offset = max(0, int(offset or 0))
    q = search_query(entity, query, mode, primary_type, secondary_type,
                     artist, year, label, catno, artist_id)
    data = mb_get_cached(entity, {"query": q, "limit": limit, "offset": offset, "fmt": "json"})
    # MB search responses use plural collection keys
    key = {"artist": "artists", "release-group": "release-groups",
           "release": "releases", "recording": "recordings"}[entity]
    raw = [i for i in data.get(key, []) if i.get("id")]
    rows = []
    seen = set()
    for item in raw:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        title = item.get("title") or item.get("name")
        row = {
            "id": item.get("id"),
            "score": item.get("score"),
            "title": title,
            "disambiguation": item.get("disambiguation") or "",
            # The alias the reader's locale names this entity by, when the
            # SEARCH INDEX states one — it does for artists (MusicBrainz stores
            # their aliases in the index) and for nothing else, so a release
            # group or a recording row simply carries "". Those rows show their
            # alias once the page has fetched it (see `alias_for` and the
            # release page's own browse call).
            "alias": alias_for(item.get("aliases"), None, title),
        }
        if entity == "artist":
            area = item.get("area") or {}
            row.update({
                "type": item.get("type") or "",
                "country": area.get("name") or "",
                "life_span": [
                    (item.get("life-span") or {}).get("begin") or "",
                    (item.get("life-span") or {}).get("end") or "",
                ],
                "tags": [t.get("name") for t in (item.get("tags") or [])[:3]],
            })
        elif entity == "release-group":
            row.update({
                "artist": _credit(item),
                "artist_mbid": _credit_mbid(item),
                "primary_type": item.get("primary-type") or "",
                "secondary_types": [s for s in (item.get("secondary-types") or [])],
                "first_release_date": item.get("first-release-date") or "",
            })
        elif entity == "release":
            catalog_number = ""
            for li in item.get("label-info") or []:
                if li.get("catalog-number"):
                    catalog_number = li["catalog-number"]
                    break
            # MB's release search embeds the release group with its PRIMARY
            # type only (the search index carries no secondary types) — that
            # is still what separates an album pressing from a single/EP.
            # `_rg_types` keeps a bare-id embed from crashing the request.
            rg_primary, rg_secondary = _rg_types(item)
            row.update({
                "artist": _credit(item),
                "artist_mbid": _credit_mbid(item),
                "date": item.get("date") or "",
                "country": item.get("country") or "",
                "status": item.get("status") or "",
                "formats": _media_summary(item),
                "track_count": sum((m.get("track-count") or 0) for m in item.get("media") or []),
                "catalog_number": catalog_number,
                "primary_type": rg_primary,
                "secondary_types": rg_secondary,
            })
        else:  # recording
            # The release the recording was pressed on, kept for its COVER:
            # Cover Art Archive answers by release and release group and has no
            # recording endpoint, so a MusicBrainz-only TRACK row's artwork can
            # only come from here (see `discovery._mb_recording_row`). The
            # index repeats a release per matching track, so the first one is
            # MusicBrainz's own ordering, not a pick of ours.
            pressing = (item.get("releases") or [{}])[0]
            row.update({
                "artist": _credit(item),
                "artist_mbid": _credit_mbid(item),
                "length": item.get("length"),
                "first_release_date": item.get("first-release-date") or "",
                "release_mbid": pressing.get("id") or "",
                "release_group_mbid": (pressing.get("release-group") or {}).get("id") or "",
            })
        rows.append(row)
    total = data.get("count") or len(rows)
    # Paging follows the offsets MusicBrainz itself served (the raw slice),
    # never the de-duplicated count, or a dropped duplicate would shift the
    # next page and skip a row.
    nxt = offset + len(raw)
    return {"rows": rows, "total": total, "offset": offset, "query": q,
            "next": nxt if raw and nxt < total else None,
            "duplicates": len(raw) - len(rows)}


def artist_identity(mbid):
    """An artist's identity: name, area, life span, genres, tags.

    Deliberately split from the discography: MusicBrainz answers one request
    per second, so the artist page paints this header while the release
    groups are still being collected."""
    data = mb_get_cached(f"artist/{mbid}",
                         {"inc": "genres+aliases", "fmt": "json"})
    area = data.get("area") or {}
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        # The name in the reader's locale, when MusicBrainz has one (see
        # `alias_for`): the page shows it in parentheses beside `name`.
        "alias": alias_for(data.get("aliases"), None, data.get("name")),
        "disambiguation": data.get("disambiguation") or "",
        "type": data.get("type") or "",
        "country": area.get("name") or "",
        "life_span": [
            (data.get("life-span") or {}).get("begin") or "",
            (data.get("life-span") or {}).get("end") or "",
        ],
        "genres": _genre_names(_genres(data)),
        "tags": [t.get("name") for t in (data.get("tags") or [])[:8]],
    }


def artist_release_groups(mbid, limit=100, offset=0, primary_type="", secondary_type=""):
    """One page of an artist's release groups, oldest first.

    The discography comes from the *browse* endpoint (release-group?artist=…)
    rather than a lookup's inc= subquery — lookups silently cap the related
    list. It has NO server-side sort, so a single arbitrary 100-row slice
    misrepresents a discography: `_browse_collect` walks the pages (still
    1 req/s) up to `limit` rows so the caller sorts an honest window.

    With a TYPE filter the list comes from the search index instead
    (`arid:` + `primarytype:`/`secondarytype:`): browse cannot filter by type
    at all, and filtering the loaded window is what made an artist page claim
    no albums for an artist whose albums sat beyond the first page. The index
    filters and counts on the server, so the chips and the "N of M" line are
    about the whole discography, not about what happened to be loaded."""
    if primary_type or secondary_type:
        page = search_mb("release-group", "", limit, "free", offset,
                         primary_type=primary_type, secondary_type=secondary_type,
                         artist_id=mbid)
        return {
            "total": page["total"],
            "offset": offset,
            "next": page["next"],
            "release_groups": sorted(
                (
                    {
                        "id": rg.get("id"),
                        "title": rg.get("title"),
                        "alias": alias_for(rg.get("aliases"), None, rg.get("title")),
                        "primary_type": rg.get("primary_type") or "",
                        "secondary_types": rg.get("secondary_types") or [],
                        "first_release_date": rg.get("first_release_date") or "",
                    }
                    for rg in page["rows"]
                ),
                key=lambda g: g.get("first_release_date") or "9999",
            ),
        }
    # `inc=aliases` on the browse: every row then carries its own aliases, so
    # the alias costs no extra request (the search path below cannot — the
    # search index returns no aliases at all).
    rgs, total, served = _browse_collect(
        "release-group", {"artist": mbid, "inc": "aliases"},
        "release-groups", "release-group-count",
        limit=limit, offset=offset,
    )
    return {
        "total": total,
        "offset": offset,
        "next": (offset + served) if 0 < total and offset + served < total else None,
        "release_groups": [
            {
                "id": rg.get("id"),
                "title": rg.get("title"),
                # This branch reads the BROWSE payload, which carries the
                # aliases (`inc=aliases` above); the search branch above cannot
                # — the index stores aliases for artists only.
                "alias": alias_for(rg.get("aliases"), None, rg.get("title")),
                "primary_type": rg.get("primary-type") or "",
                "secondary_types": rg.get("secondary-types") or [],
                "first_release_date": rg.get("first-release-date") or "",
            }
            for rg in sorted(
                rgs,
                key=lambda g: g.get("first-release-date") or "9999",
            )
        ],
    }


def artist_browse(mbid, limit=300, offset=0, primary_type="", secondary_type=""):
    """Identity + a page of release groups together — the auto-import path
    wants both at once, the artist page does not (see artist_identity). The
    type filter is the page's own (see artist_release_groups)."""
    return {**artist_identity(mbid),
            **artist_release_groups(mbid, limit, offset, primary_type, secondary_type)}


def _release_cfg(cfg=None):
    """The config the release-choice policy reads, safe defaults on a CLI.

    `mlo.release_choice` ranks the payloads it is handed against a config
    dict and nothing else, so the ONE config load the policy ever needs lives
    here, next to the MusicBrainz access that consumes the ranking.
    """
    if isinstance(cfg, dict):
        return cfg
    from mlo.config import load_config
    try:
        return load_config()
    except Exception:
        return {}


def _release_group_node(release_group):
    """The release-group fields `mlo.release_choice` reads.

    An absent page (a caller that only holds editions) reads as "no group
    facts": the policy then measures editions against the fullest one offered
    and takes the earliest edition as the original.
    """
    node = release_group if isinstance(release_group, dict) else {}
    return {
        "title": node.get("title"),
        "first_release_date": node.get("first_release_date"),
        "primary_type": node.get("primary_type"),
        "secondary_types": node.get("secondary_types"),
        "track_count": node.get("track_count"),
    }


def ranked_releases(release_group, releases, cfg=None, **kw):
    """`mlo.release_choice.rank_releases` against this app's config.

    The ONE entry point the server uses, so the policy module never sees a
    None config and the "which key, which default" question is answered in
    mlo.config alone.
    """
    return release_choice.rank_releases(_release_group_node(release_group),
                                        releases, _release_cfg(cfg), **kw)


def _best_release(release_group, releases, cfg=None, **kw):
    """(release payload, candidate) for the policy's pick, or (None, None)."""
    rows = list(releases or [])
    ranked = ranked_releases(release_group, rows, cfg, strict=True, **kw)
    best = release_choice.pick(ranked, strict=True)
    return (rows[best.index], best) if best else (None, None)


def pick_releases(releases, cfg=None):
    """The ELIGIBLE releases of *releases*, best first, as the payloads they
    came in as.

    Strict mode of the one release-choice policy (`mlo.release_choice`): a
    promotional/bootleg/pseudo edition is dropped while
    `auto_import_avoid_promo` is on, and an edition with no RELEASECOUNTRY
    while `auto_import_require_country` is on, so no unattended download can
    queue one. The ranking itself — status, medium, track count, date,
    original-vs-reissue, plain title, preferred country — lives there and
    nowhere else (this used to be `release_choice_key`/`release_medium_rank`/
    `_date_rank` here: one policy now).
    """
    rows = list(releases or [])
    ranked = ranked_releases(None, rows, cfg, strict=True)
    return [rows[c.index] for c in ranked if c.eligible and c.type_ok]


def pick_release(releases, cfg=None):
    """The single best release of a group per `pick_releases`, or None."""
    kept = pick_releases(releases, cfg)
    return kept[0] if kept else None


def resolve_release(mbid):
    """(release, release_mbid) for a release id, a release-group id or a URL.

    Auto-import works on a *release* — a concrete pressing with a track list
    — while the ids pasted into a wish or an import are usually release
    GROUPS, and a group id passed to the release endpoint 404s. Group ids
    are resolved to their best edition via the release-choice policy so no
    caller (HTTP route, wishes worker, bulk import) can queue a group job.
    Returns (None, mbid) when the id is a group with no usable edition, and
    (None, None) when nothing matches at all. A MusicBrainz OUTAGE is not
    "nothing matches" — that raises MusicBrainzError so the caller reports
    "MusicBrainz is busy" instead of claiming the release does not exist."""
    rid = _mbid(mbid)
    if not rid:
        return None, None
    try:
        return release_lookup(rid), rid
    except MusicBrainzError:
        raise
    except Exception:
        pass
    try:
        rg = release_group_browse(rid, limit=100, offset=0)
    except MusicBrainzError:
        raise
    except Exception:
        return None, None
    best, _pick = _best_release(rg, rg.get("releases") or []) if rg.get("id") else (None, None)
    if not best or not best.get("id"):
        return None, rid
    try:
        return release_lookup(best["id"]), best["id"]
    except MusicBrainzError:
        raise
    except Exception:
        return None, rid


_NO_EDITION = ("no edition eligible for auto-import (promotional/bootleg "
               "editions are skipped while auto_import_avoid_promo is on, and "
               "editions with no release country while "
               "auto_import_require_country is on)")
# Release groups one bulk call expands: each costs a MusicBrainz browse
# (1 req/s), so an artist with hundreds of groups would take minutes — the
# remainder is reported as skipped instead of silently dropped or wedging.
# It is the DEFAULT bound, for a call that has to answer (the auto-import
# route's quick attempt); a caller working off the request passes
# `limit=None` and expands every group its own row named (see
# `auto_import_targets`).
BULK_MAX_GROUPS = 50


def no_edition_reason(ranked):
    """Why the strict policy refused every edition of a group, in words.

    The policy's own first eligibility sentence when it has one — so a caller
    (and the watch's `last_error`) reads "no release country …" or "release
    group type is not the type asked for" rather than a generic refusal.
    """
    if not ranked:
        return "MusicBrainz lists no edition of this release group"
    refused = next((c for c in ranked if not c.eligible), None)
    detail = refused.reasons[0] if refused and refused.reasons else ""
    return f"no eligible edition for auto-import ({detail})" if detail else _NO_EDITION


def group_targets(rg_mbid, mode, *, types=None, primary_type="", secondary_type=""):
    """([{mbid,title,score,reasons}], error) — the releases of a group to queue.

    Ranked by the one release-choice policy in strict mode (see
    `pick_releases`); `mode` "best" keeps only the pick, "all" every eligible
    edition, best first. `types` / `primary_type` / `secondary_type` restrict
    the pick to the release-group type the caller is after — a watch's own
    type filter — so a watch for albums can never queue a single. `error` is a
    readable reason and never an exception, so one unusable group cannot abort
    a whole discography."""
    try:
        rg = release_group_browse(rg_mbid, limit=100, offset=0)
    except Exception as e:
        return [], f"MusicBrainz release-group lookup failed: {e}"
    if not rg.get("id"):
        return [], "not a MusicBrainz release group"
    # The caller's type filter gates the GROUP itself, by the ONE rule the
    # choice then applies to its editions: a group of another type is reported
    # with the type it actually is, never answered with an edition of a group
    # that was not asked for.
    if types and not release_choice.type_matches(
            rg.get("primary_type"), rg.get("secondary_types"), types):
        return [], type_skip_reason(rg.get("primary_type"),
                                    rg.get("secondary_types"))
    ranked = ranked_releases(rg, rg.get("releases") or [], strict=True,
                             wanted_types=types, primary_type=primary_type,
                             secondary_type=secondary_type)
    rows = [c for c in ranked if c.eligible and c.type_ok]
    if not rows:
        return [], no_edition_reason(ranked)
    if mode != "all":
        rows = rows[:1]
    return [{"mbid": c.release_mbid, "title": c.title, "score": c.score,
             "reasons": list(c.reasons)} for c in rows], None


def type_skip_reason(primary_type, secondary_types):
    """Why a release group is NOT one of the types a caller asked for.

    The group's own type is named the way MusicBrainz spells it ("Album +
    Compilation"), so a skipped row says what the group IS, not only that it
    was left out.
    """
    label = " + ".join([str(primary_type or "").strip()]
                       + [str(s).strip() for s in (secondary_types or [])
                          if str(s).strip()])
    return ("release-group type not requested ("
            + (label or "MusicBrainz states no type") + ")")


def groups_of_types(groups, types):
    """(kept, skipped) — the release-group rows a `types` filter keeps.

    `types` is MusicBrainz's own vocabulary (a combined form like "Album +
    Compilation" included) and every row is judged by the ONE rule in
    `mlo.release_choice.type_matches` — the filter the artist watch and the
    release choice already apply. An EMPTY selection keeps every group: no
    filter means no filter, which is what every caller did before the filter
    existed. Each dropped row comes back as `{mbid, reason}`
    (`type_skip_reason`), so a filtered-out group is REPORTED and never
    silently dropped.
    """
    if not types:
        return list(groups or []), []
    kept, skipped = [], []
    for g in groups or []:
        if release_choice.type_matches(g.get("primary_type"),
                                       g.get("secondary_types"), types):
            kept.append(g)
        else:
            skipped.append({"mbid": str(g.get("id") or ""),
                            "reason": type_skip_reason(g.get("primary_type"),
                                                       g.get("secondary_types"))})
    return kept, skipped


def _kind_for(mbid):
    """release / release_group / artist for an ID that did not say which."""
    t = (detect_mbid(mbid) or {}).get("type") or ""
    return {"release-group": "release_group"}.get(t, t) or None


def auto_import_targets(mbid, kind=None, mode="best", types=None,
                        limit=BULK_MAX_GROUPS):
    """([{mbid,title}], [{mbid,reason}]) — what a bulk auto-import should queue.

    ONE resolution path, shared by the HTTP route's bounded quick attempt and
    by the auto-import job that redoes the whole thing when that attempt did
    not finish: a release resolves to the edition the policy picks, a release
    group to its best (or every eligible) edition, an artist to one best
    release per release group it does not already own. A MusicBrainz outage
    raises MusicBrainzError — reported per item, never as "does not exist".

    `types` restricts an ARTIST or release-group request to MusicBrainz's own
    release-group types, matched by `mlo.release_choice.type_matches` — the
    same rule (and the same vocabulary) the artist watch's type filter and
    the release choice use, so "Album + Compilation" selects those groups and
    not the Singles. An EMPTY selection is every type, exactly the behaviour
    every caller had before the filter existed; a group the filter leaves out
    is reported in the second list WITH its reason, never silently dropped.

    `limit` is how many of an artist's release groups one call may EXPAND
    (each costs a MusicBrainz browse, 1 req/s), the default being the bound a
    call that has to answer keeps. `limit=None` expands every group the filter
    keeps: that is what a caller working off the request (the artist page's
    discography prepare, one album appearing at a time) owes the button that
    named the type — with a bound there, the row's own list was truncated and
    the rest came back as "call again" for an action the user had already
    asked for in full.

    A user's own edition choice does NOT come through here: the route resolves
    it (`server.api_add._group_edition_targets`) so the group-membership check
    sits with the request that named both ids.
    """
    mode = "all" if str(mode or "").lower() == "all" else "best"
    kind = str(kind or "auto").strip().lower()
    if kind in ("", "auto"):
        kind = _kind_for(mbid) or "release"
    # "Already in the library" is checked for every kind, not just artists:
    # queuing a release or a group the library already holds downloaded the
    # same album a second time, and the duplicate then landed beside it.
    from mlo.config import load_config
    from server import wishes
    owned = wishes.owned_mbids(load_config())
    if kind == "release":
        rel, rid = resolve_release(mbid)
        if not rid:
            return [], [{"mbid": mbid, "reason": "no MusicBrainz release or "
                                                 "release group matches this ID"}]
        if not rel:
            return [], [{"mbid": rid, "reason": _NO_EDITION}]
        rg = str(rel.get("release_group_id") or "").strip().lower()
        if rg and rg in owned:
            return [], [{"mbid": rid, "reason": "already in the library"}]
        return [{"mbid": rid, "title": rel.get("title") or ""}], []
    if kind == "release_group":
        if str(_mbid(mbid) or "").lower() in owned:
            return [], [{"mbid": mbid, "reason": "already in the library"}]
        rows, err = group_targets(mbid, mode, types=types)
        return rows, ([{"mbid": mbid, "reason": err}] if err else [])
    if kind != "artist":
        return [], [{"mbid": mbid, "reason": f"unknown MusicBrainz kind {kind!r}"}]

    artist = artist_browse(mbid, limit=500, offset=0)
    groups = artist.get("release_groups") or []
    if not groups:
        return [], [{"mbid": mbid,
                     "reason": "this artist has no release groups on MusicBrainz"}]
    # The type filter is applied to the GROUP LIST first: a group the caller
    # did not ask for is never even browsed (each one costs a MusicBrainz
    # request), and it leaves a skip row naming the type it actually is.
    groups, skipped = groups_of_types(groups, types)
    rows, done = [], 0
    for g in groups:
        gid = str(g.get("id") or "")
        if not gid:
            continue
        if gid.lower() in owned:
            skipped.append({"mbid": gid, "reason": "already in the library"})
            continue
        if limit is not None and done >= limit:
            skipped.append({"mbid": gid,
                            "reason": f"per-call limit of {limit} "
                                      "release groups reached — call again"})
            continue
        sub, err = group_targets(gid, "best", types=types)
        done += 1
        if err:
            skipped.append({"mbid": gid, "reason": err})
            continue
        rows.extend(sub)
    return rows, skipped


def release_group_browse(mbid, limit=300, offset=0):
    """Release-group page: identity + its releases (editions), each with
    media so every row carries format, disc count and its '10 + 11' track
    breakdown. Releases come from the browse endpoint (collected across
    pages) because the lookup's release subquery both truncates and omits
    media.

    The payload also carries `countries`: every (country, date) the group's
    editions were released in, each stamped with the edition that carries it
    (a release carries its own events, so the union costs no extra request).
    The editions of the pages loaded so far are what it covers.
    """
    data = mb_get_cached(
        f"release-group/{mbid}",
        {"inc": "artist-credits+genres+aliases", "fmt": "json"},
    )
    rel_rows, total, served = _browse_collect(
        "release", {"release-group": mbid, "inc": "media+aliases"},
        "releases", "release-count",
        limit=limit, offset=offset,
    )
    # One config load for both readers of it (the ranking and the country
    # chips' preference mark).
    cfg = _release_cfg()
    releases, ranked_rows = [], []
    # The editions are listed in the policy's own order (mlo.release_choice),
    # so the first row is the edition auto-import would take — and the sort is
    # deliberately NOT strict: a promo or a country-less edition still belongs
    # on the page, ranked where the policy puts it.
    for cand in ranked_releases(
            {"title": data.get("title"),
             "first_release_date": data.get("first-release-date"),
             "primary_type": data.get("primary-type"),
             "secondary_types": data.get("secondary-types")},
            rel_rows, cfg):
        r = rel_rows[cand.index]
        ranked_rows.append(r)
        track_count, track_breakdown = _release_counts(r)
        releases.append({
            "id": r.get("id"),
            "title": r.get("title"),
            "alias": alias_for(r.get("aliases"), None, r.get("title")),
            "date": r.get("date") or "",
            "country": r.get("country") or "",
            "status": r.get("status") or "",
            "disambiguation": r.get("disambiguation") or "",
            "medium": r.get("format") or "",
            "formats": _media_summary(r),
            "disc_count": len(r.get("media") or []),
            "track_count": track_count,
            "track_breakdown": track_breakdown,
            "barcode": r.get("barcode") or "",
            "score": cand.score,
            "reasons": list(cand.reasons),
        })
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "disambiguation": data.get("disambiguation") or "",
        "artist": _credit(data),
        "artist_mbid": next(
            (ac["artist"]["id"] for ac in data.get("artist-credit") or [] if "artist" in ac), None
        ),
        "primary_type": data.get("primary-type") or "",
        "secondary_types": data.get("secondary-types") or [],
        "genres": _genre_names(_genres(data)),
        "first_release_date": data.get("first-release-date") or "",
        "countries": release_group_countries(
            ranked_rows, cfg.get("prefer_release_country")),
        "total": total,
        "offset": offset,
        "next": (offset + served) if 0 < total and offset + served < total else None,
        "releases": releases,
    }


def recording_browse(mbid, limit=300, offset=0):
    """Recording ('track') page: identity + releases carrying it (browsed,
    with media, for the same reasons as the release-group page)."""
    data = mb_get_cached(
        f"recording/{mbid}",
        {"inc": "artist-credits+isrcs+genres", "fmt": "json"},
    )
    rel_rows, total, served = _browse_collect(
        "release",
        {"recording": mbid, "inc": "media+artist-credits+release-groups"},
        "releases", "release-count",
        limit=limit, offset=offset,
    )
    releases = []
    # Policy order (mlo.release_choice), so the first row is the release
    # "Add to library" would take and a caller that falls back to `rows[0]`
    # falls back to the pick, never to an arbitrary edition. No group payload
    # is in hand here, so the policy's "original" is the earliest release
    # carrying this recording.
    for cand in ranked_releases(None, rel_rows):
        r = rel_rows[cand.index]
        track_count, track_breakdown = _release_counts(r)
        rg_primary, rg_secondary = _rg_types(r)
        releases.append({
            "id": r.get("id"),
            "title": r.get("title"),
            "date": r.get("date") or "",
            "country": r.get("country") or "",
            "status": r.get("status") or "",
            "formats": _media_summary(r),
            "disc_count": len(r.get("media") or []),
            "track_count": track_count,
            "track_breakdown": track_breakdown,
            # the release group's full type: primary (Album/EP/Single/...) plus
            # secondary (Soundtrack/Live/Compilation/...), so a score album
            # reads "Album + Soundtrack" instead of a bare "Album".
            "primary_type": rg_primary,
            "secondary_types": rg_secondary,
        })
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "disambiguation": data.get("disambiguation") or "",
        "artist": _credit(data),
        "artist_mbid": next(
            (ac["artist"]["id"] for ac in data.get("artist-credit") or [] if "artist" in ac), None
        ),
        "length": data.get("length"),
        "genres": _genre_names(_genres(data)),
        # a recording lookup returns bare ISRC strings ("USRC17607839") while
        # some other entities wrap them in {"isrc": ...} — .get() on a string
        # raised AttributeError and 502'd the whole recording page.
        "isrcs": [v for v in (_isrc(i) for i in data.get("isrcs") or []) if v],
        "total": total,
        "offset": offset,
        "next": (offset + served) if 0 < total and offset + served < total else None,
        "releases": releases,
    }


# --------------------------------------------------------------------------- #
# Track matching (local files <-> release media)
# --------------------------------------------------------------------------- #
def match_tracks(local_tracks, release):
    """Suggest release track/disc for each local track.

    local_tracks: list of {path, file, tracknumber, discnumber, title, duration}
    Matching: exact (disc,position) hit first, then title-similarity fallback.
    """
    release_media = release.get("media", [])
    by_pos = {(m["disc"], m["position"]): m for m in release_media}
    suggestions = []
    for lt in local_tracks:
        tn = lt.get("tracknumber")
        dn = lt.get("discnumber") or 1
        match = None
        score = 0.0
        if tn is not None:
            m = by_pos.get((dn, tn)) or by_pos.get((1, tn))
            if m:
                match, score = m, 1.0
        if match is None and lt.get("title"):
            best, best_score = None, 0.0
            kind = title_variant_kind(lt["title"])
            for m in release_media:
                # A variant of the track (instrumental/karaoke/…) is another
                # recording: it may never be suggested as this file's track,
                # whatever the name similarity says.
                got = title_variant_kind(m.get("title"))
                if (kind or got) and kind != got:
                    continue
                s = _similarity(lt["title"], m.get("title", ""))
                if s > best_score:
                    best, best_score = m, s
            if best and best_score >= 0.6:
                match, score = best, best_score
        suggestions.append({
            "local": lt.get("path"),
            "file": lt.get("file"),
            "matched": match is not None,
            "confidence": score,
            "release_track": match,
        })
    return suggestions


def _similarity(a, b):
    import difflib
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


# --------------------------------------------------------------------------- #
# LRCLIB
# --------------------------------------------------------------------------- #
_LRCLIB_HEADERS = {"User-Agent": USER_AGENT}
_lrclib_last = 0.0
_lrclib_lock = threading.Lock()


def _lrclib_get(endpoint, params, timeout=15, retries=3):
    """Rate-throttled LRCLIB GET with retry on 429/5xx (they throttle IPs)."""
    global _lrclib_last
    for attempt in range(retries):
        with _lrclib_lock:
            elapsed = time.time() - _lrclib_last
            if elapsed < 0.4:
                time.sleep(0.4 - elapsed)
            r = httpx.get(f"{LRCLIB_BASE}/{endpoint}", params=params,
                          headers=_LRCLIB_HEADERS, timeout=timeout)
            _lrclib_last = time.time()
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(2.0 * (attempt + 1))
            continue
        return r
    return r


def lrclib_search(artist, track, album=None, duration=None):
    params = {"track_name": track, "artist_name": artist}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = duration
    r = _lrclib_get("search", params)
    if r.status_code == 200:
        return r.json()
    if r.status_code in (400, 404):
        return []
    raise httpx.HTTPStatusError(f"lrclib search {r.status_code}", request=r.request, response=r)


def lrclib_get(artist, track, album=None, duration=None):
    """Exact-match lyrics lookup with a search fallback.

    Falls back to /search (preferring synced lyrics, then closest duration)
    when the exact /get comes back empty; 400/404 are treated as not-found.
    """
    params = {"artist_name": artist, "track_name": track}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = duration
    r = _lrclib_get("get", params)
    if r.status_code == 200:
        return r.json()
    if r.status_code in (400, 404):
        # retry the search without the album filter — it can hurt matches
        for album_filter in (None, album):
            try:
                hits = lrclib_search(artist, track, album_filter, duration)
            except Exception:
                hits = []
            if isinstance(hits, list) and hits:
                synced = [h for h in hits if h.get("syncedLyrics")]
                pool = synced or hits
                if duration:
                    pool = sorted(pool, key=lambda h: abs(int(h.get("duration") or 0) - int(duration)))
                return pool[0]
        return None
    raise httpx.HTTPStatusError(f"lrclib get {r.status_code}", request=r.request, response=r)

def lrclib_publish(artist, track, album, duration, plain=None, synced=None):
    """Submit lyrics to LRCLIB (POST /api/publish).

    Delegates to `mlo.lyrics_providers.lrclib_publish` — the engine client
    script 18 publishes with — so the request body, the required User-Agent
    and the throttle that keeps this IP out of LRCLIB's rate limit exist once
    for both the automatic and the manual path. Returns (ok, message);
    "LRCLIB already has this track" is the duplicate answer, not an error.
    """
    from mlo.lyrics_providers import lrclib_publish as _publish

    return _publish(artist, track, album, duration, plain=plain, synced=synced)



# --------------------------------------------------------------------------- #
# RateYourMusic (no public API — link helpers only)
# --------------------------------------------------------------------------- #
# RYM slugs vary (artist/album, album/format, %-encoding, apostrophes…).
# Keep it lenient: any rateyourmusic.com URL is a valid link to store.
RYM_RE = re.compile(r"^https?://(?:www\.)?rateyourmusic\.com/.+$", re.I)


def parse_rym_album_url(url):
    """Validate a RYM URL, returning the canonical URL or None."""
    url = (url or "").strip()
    if RYM_RE.match(url):
        return url
    return None


# What a RYM URL points AT. Every one of these is a valid URL, but only an
# album page belongs in RATEYOURMUSIC_ALBUM: a pasted artist or song page
# stored there would look like a resolved link forever (the import stamp never
# overwrites an existing one) and would block the automatic album lookup.
# `/release/song/` is matched before `/release/`, or a song page would pass as
# an album; every other release type (single, EP, comp…) IS an album.
_RYM_KIND_RES = (
    ("song", re.compile(r"^/(?:release/)?song/", re.I)),
    ("album", re.compile(r"^/release/[^/]+/", re.I)),
    ("artist", re.compile(r"^/artist/", re.I)),
)


def rym_url_kind(url):
    """What a RYM URL points at: "album", "artist", "song" or "other".

    None means it is not a rateyourmusic.com URL at all — the caller's signal
    to refuse it outright; "other" is a real page (a label, a list, a genre)
    that is simply not one of the three link fields the UI writes.
    """
    url = (url or "").strip()
    if not RYM_RE.match(url):
        return None
    path = re.split(r"[?#]", re.sub(r"^https?://(?:www\.)?rateyourmusic\.com",
                                    "", url, count=1, flags=re.I))[0]
    for kind, rx in _RYM_KIND_RES:
        if rx.match(path):
            return kind
    return "other"


# --------------------------------------------------------------------------- #
# covers.musichoarders.xyz (COV) — album cover meta-search
# --------------------------------------------------------------------------- #
# COV aggregates cover art from streaming services and databases. Its search
# endpoint is the same one the website's frontend calls: a POST that streams
# newline-delimited JSON events (source/cover/count/done/error).
COV_BASE = "https://covers.musichoarders.xyz"
# The site's API gate rejects non-browser User-Agents (401), so COV
# requests use a plain browser UA while MB/LRCLIB keep the app UA.
COV_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
# The API allows at most 9 active sources per search; prefer high-quality
# art sources first and fill up with whatever else is enabled.
COV_SOURCE_PRIORITY = list(_cover_choice.DEFAULT_SOURCE_ORDER)
COV_MAX_SOURCES = 9
COV_FALLBACK_SOURCES = list(_cover_choice.DEFAULT_SOURCE_ORDER)

_cov_info_cache = {"at": 0.0, "info": {}}
_cov_sources_cache = {"at": 0.0, "ids": []}
# COV's own default when a request omits `country`.
COV_DEFAULT_COUNTRY = "us"


def cov_catalog(timeout=15.0):
    """Everything the UI needs to configure a cover search: the selectable
    sources, the regions, and COV's own active-source cap.

    Cached for an hour — it is static metadata, and the UI asks for it every
    time the finder opens. A failure falls back to the built-in source list so
    the finder still works offline (with the region list reduced to the one
    default, since the real list comes from the server)."""
    import time as _time
    now = _time.time()
    if _cov_info_cache["info"] and now - _cov_info_cache["at"] < 3600:
        return _cov_info_cache["info"]
    try:
        raw = httpx.get(f"{COV_BASE}/api/info",
                        headers={"User-Agent": COV_UA},
                        timeout=timeout).json()
    except Exception:
        raw = {}
    sources = []
    for s in raw.get("sources") or []:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        sources.append({
            "id": s["id"],
            "name": s.get("name") or s["id"],
            "enabled": bool(s.get("enabled", True)),
            "color": s.get("color"),
            "countries": [str(c).lower() for c in (s.get("countries") or [])],
        })
    if not sources:
        sources = [{"id": i, "name": i, "enabled": True, "color": None,
                    "countries": []} for i in COV_FALLBACK_SOURCES]
    countries = [str(c).lower() for c in (raw.get("countries") or [])]
    info = {
        "sources": sources,
        "countries": countries or [COV_DEFAULT_COUNTRY],
        "active_source_limit": int(raw.get("activeSourceLimit") or COV_MAX_SOURCES),
    }
    _cov_info_cache.update(at=now, info=info)
    return info


def _cov_enabled_ids(timeout=15.0):
    """Source ids COV reports as enabled, in priority order."""
    enabled = [s["id"] for s in cov_catalog(timeout)["sources"] if s.get("enabled", True)]
    ordered = [s for s in COV_SOURCE_PRIORITY if s in enabled]
    ordered += [s for s in enabled if s not in ordered]
    return ordered or list(COV_FALLBACK_SOURCES)


def _cov_sources(timeout=15.0):
    """Default source ids for a search, capped at COV's active limit."""
    ids = _cov_enabled_ids(timeout)[:cov_catalog(timeout)["active_source_limit"]]
    return ids or list(COV_FALLBACK_SOURCES)


def resolve_cov_search(sources=None, country=None, cfg=None):
    """(source_ids, country) for a search, honouring the caller, then the
    saved defaults, then COV's own defaults.

    `sources`/`country` come from the finder UI (a per-search override);
    `cfg` holds the SAVED defaults (`cover_sources`, `cover_country`) used
    when the caller passes nothing. Every id is validated against the catalog
    so a stale saved setting cannot silently search nothing, and the list is
    trimmed to COV's cap — it rejects a longer one outright."""
    cat = cov_catalog()
    known = {s["id"] for s in cat["sources"]}
    limit = cat["active_source_limit"]

    chosen = [str(s).strip() for s in (sources or []) if str(s).strip()]
    if not chosen:
        try:
            from mlo.config import load_config
            chosen = [str(s).strip() for s in
                      ((cfg or load_config()).get("cover_sources") or [])
                      if str(s).strip()]
        except Exception:
            chosen = []
    chosen = [s for s in chosen if s in known]
    if not chosen:
        chosen = _cov_enabled_ids()

    c = str(country or "").strip().lower()
    if not c:
        try:
            from mlo.config import load_config
            c = str((cfg or load_config()).get("cover_country") or "").strip().lower()
        except Exception:
            c = ""
    if c not in cat["countries"]:
        c = COV_DEFAULT_COUNTRY
    return chosen[:limit], c


def _cover_row(source, small, big, title=None, artist=None, tracks=None,
               url=None, width=None, height=None, front=None, kind=None,
               release_cover=None, rank=None, format=None, nbytes=None):
    """One search result. Every provider (COV and each fallback) answers this
    exact shape, so the finder never has to know which one answered: the keys
    it already reads (`source`, `small`, `big`, `title`, `artist`, `tracks`,
    `url`) plus what the candidate was MEASURED from rather than what its URL
    suggests.

    `width`/`height` are the REAL pixel size read from the image's own header
    bytes (``None`` means unknown, never a guess) and `format`/`nbytes` are
    what that same probe found — the container the bytes really are and how
    many bytes the URL answered with (0 = an empty answer). `front`/`kind` are
    the provider's own labelling ("front" / "back" / "other"), and
    `release_cover` says whether this is the release GROUP's image (False —
    the album's own art, which the policy prefers) or one RELEASE's own front
    cover (True, an edition's art) or neither stated (``None``).
    `rank` is the provider's own order (0 = its first answer), which
    `mlo.cover_choice` reads as its last tiebreak."""
    return {"source": source, "small": small or None, "big": big or None,
            "title": title or None, "artist": artist or None, "tracks": tracks,
            "url": url or None, "width": width, "height": height,
            "format": format, "bytes": nbytes, "front": front,
            "kind": kind, "release_cover": release_cover, "rank": rank}


def _cov_headers():
    """The exact header set COV's own frontend sends.

    The API gate 401s without it, and it must be these headers ONLY: adding
    `Accept: application/json` makes the endpoint answer 200 with an EMPTY
    stream (verified live), which reads as "no covers exist" — the silent
    nothing this module exists to avoid.
    """
    return {"User-Agent": COV_UA, "Referer": f"{COV_BASE}/", "Origin": COV_BASE,
            "X-Session": uuid.uuid4().hex}


@contextlib.contextmanager
def _cov_stream(body, headers, timeout=60.0):
    """POST a search and yield COV's streamed JSON lines (the HTTP seam)."""
    with httpx.Client(timeout=httpx.Timeout(timeout, read=timeout)) as client:
        with client.stream("POST", f"{COV_BASE}/api/search", json=body,
                           headers=headers) as r:
            r.raise_for_status()
            yield r.iter_lines()


_CAA_GROUP_URL_RE = re.compile(r"coverartarchive\.org/release-group/", re.I)
_CAA_RELEASE_URL_RE = re.compile(r"coverartarchive\.org/release/([0-9a-f-]{36})/", re.I)
_CAA_BACK_URL_RE = re.compile(r"/back(?:-\d+)?(?:\.\w+)?(?:$|[?#])", re.I)
_CAA_FRONT_URL_RE = re.compile(r"/front(?:-\d+)?(?:\.\w+)?(?:$|[?#])", re.I)


def cover_url_labels(url):
    """(kind, release_cover) a URL itself states, or (None, None).

    Only the Cover Art Archive's URLs say this much for free, and they are the
    only ones the app can read without the provider's own metadata:
    `/release-group/<rg>/…` is the GROUP's image — the album's own art, and the
    reference the cover policy prefers, `/release/<mbid>/front…` is that one
    release's own front cover (a specific edition's art, which the policy ranks
    below a name-searched row), and a `/back` names the back. A store's CDN
    path states nothing, which the cover policy treats as unknown rather than
    guessing (`mlo.cover_choice`: a name-searched row sits between the two).
    """
    text = str(url or "")
    if "coverartarchive.org" not in text.lower():
        return None, None
    if _CAA_BACK_URL_RE.search(text):
        return "back", False
    if _CAA_FRONT_URL_RE.search(text):
        return "front", False if _CAA_GROUP_URL_RE.search(text) else True
    if _CAA_GROUP_URL_RE.search(text):
        return None, False               # a group image with no type stated
    if _CAA_RELEASE_URL_RE.search(text):
        return None, True                # one release's own image
    return None, None


def _cov_results(artist, album, limit, timeout, src_ids, ctry):
    """COV's own covers for one query, in the site's relevance order.

    COV states no dimensions and no type: `width`/`height`/`format` are filled
    afterwards by the probe (`_attach_dimensions`), `rank` is the position in
    this very order (COV's own relevance is the last tiebreak the cover policy
    reads), and the front/stand-in labels are read from the URL's own shape
    where CAA URLs state it.

    Each row keeps its event's own `releaseInfo` — the title, artist and track
    count of the release that source matched — because a name search answers
    with OTHER releases too (karaoke, tribute, 8-bit, another album by the same
    artist), and that block is what says which release a cover belongs to.
    `mlo.cover_choice`'s album-identity rule is what reads it.
    """
    body = {"country": ctry, "sources": src_ids}
    if artist:
        body["artist"] = artist
    if album:
        body["album"] = album
    rows = []
    with _cov_stream(body, _cov_headers(), timeout) as lines:
        for line in lines:
            line = (line or "").strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") != "cover":
                continue
            rel = ev.get("releaseInfo") or {}
            big = ev.get("bigCoverUrl")
            kind, release_cover = cover_url_labels(big)
            rows.append(_cover_row(
                ev.get("source"), ev.get("smallCoverUrl"), big,
                rel.get("title"), rel.get("artist"), rel.get("tracks"),
                rel.get("url"), ev.get("width"), ev.get("height"),
                front=True if kind == "front" else (False if kind == "back" else None),
                kind=kind, release_cover=release_cover, rank=len(rows)))
            if len(rows) >= limit:
                break
    return rows


# --------------------------------------------------------------------------- #
# Real image dimensions
# --------------------------------------------------------------------------- #
# COV's streamed lines carry no dimensions at all, and a CDN URL's own
# "500x0w"/"-250" is a REQUEST hint, not the size the URL answers with — so the
# size is read from the image's own header bytes. A ranged GET of the first
# 64 KB is enough for every JPEG SOF marker (they sit near the front), a PNG
# IHDR and all three WebP headers; anything larger is a download, and a search
# must not become one.
COVER_PROBE_BYTES = 65536
# Only the first results are probed — 40 covers must not become 40 image
# fetches. The rest keep `width`/`height` None, which the finder reads as
# "unknown" and shows as such.
COVER_PROBE_LIMIT = 24
COVER_PROBE_WORKERS = 8


def _image_size(data):
    """(width, height) read from an image's own header bytes, or None.

    JPEG (SOI + SOFn), PNG (IHDR) and WebP (VP8X / VP8 / VP8L) only — those
    are what every cover provider here serves; anything else reads unknown
    rather than being guessed at.
    """
    if len(data) < 26:
        return None
    if data[:2] == b"\xff\xd8":                              # JPEG
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker == 0xDA:                               # scan: no SOF after
                break
            seg = int.from_bytes(data[i + 2:i + 4], "big")
            if seg < 2:
                break
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (int.from_bytes(data[i + 7:i + 9], "big"),
                        int.from_bytes(data[i + 5:i + 7], "big"))
            i += 2 + seg
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return (int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"))
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8X":                                  # extended
            return (int.from_bytes(data[24:27], "little") + 1,
                    int.from_bytes(data[27:30], "little") + 1)
        if kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":   # lossy
            return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                    int.from_bytes(data[28:30], "little") & 0x3FFF)
        if kind == b"VP8L" and data[20] == 0x2F:             # lossless
            bits = int.from_bytes(data[21:25], "little")
            return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    return None


def _image_format(data):
    """"jpeg"/"png"/"webp" for bytes whose container is one of those, else "".

    The same magic numbers `_image_size` reads, and the answer the cover policy
    judges the format tier on — a PNG photo is re-encoded when it is written,
    a JPEG is already the shape the library stores. Bytes that are not one of
    the three (an HTML error page, a GIF, something exotic) say "" instead of
    being called a cover.
    """
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def _probe_get(url, nbytes=COVER_PROBE_BYTES, timeout=10.0):
    """The first *nbytes* of an image URL (a Range request).

    b"" when the URL ANSWERED with nothing (an empty body, a 404 body-less
    reply) and None when the request itself failed (a dead host, a refused
    connection, a timeout) — the difference between "this URL has no image" and
    "this URL could not be asked", which the cover policy needs: an empty
    answer is a candidate it rejects out loud, an unasked one is merely
    unknown. Never raises: a probe that cannot answer is a valid answer.

    Range is a request, not a promise: a server may answer 200 with the whole
    file, so the body is read in chunks and dropped once *nbytes* are in hand.
    """
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, read=timeout),
                          follow_redirects=True) as client:
            with client.stream("GET", url,
                               headers={"Range": f"bytes=0-{nbytes - 1}",
                                        "User-Agent": COV_UA}) as r:
                r.raise_for_status()
                out = bytearray()
                for chunk in r.iter_bytes(nbytes):
                    out += chunk
                    if len(out) >= nbytes:
                        break
                return bytes(out[:nbytes])
    except Exception:
        return None


def _probe_dimensions(url, timeout=10.0):
    """One probe of one URL: header bytes only, public hosts only, no raise.

    Answers ``{"width", "height", "format", "bytes"}`` — what the image's own
    bytes say it is (a dimension pair, a container) plus how many bytes the URL
    answered with, which is how an empty answer (0 bytes) is told apart from
    bytes that are not an image at all. A URL that cannot be probed at all
    answers None (unknown), never a guess.

    Nothing here may raise — `_genre_cached` requires its producer to be total,
    and an unknown size is a valid answer while a dead search is not.
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if (parsed.scheme not in ("http", "https")
                or not _public_host(parsed.hostname)):
            return None                 # the same trust boundary as the download
        data = _probe_get(url, timeout=timeout)
        if data is None:
            return None                 # the URL could not be asked at all
        size = _image_size(data)
        out = {"format": _image_format(data), "bytes": len(data)}
        if size:
            out["width"], out["height"] = size[0], size[1]
        return out
    except Exception:
        return None


def image_dimensions(url, timeout=10.0):
    """The image at *url* as the probe found it, or None when unknown.

    ``{"width", "height", "format", "bytes"}`` — the real pixel size, the real
    container and how many bytes the URL answered with (0 = an empty answer).
    Memoized 30 days (memory + <music>/.mlo/data, the app's shared value
    cache) — a probe that found nothing is cached too, so a rerun never re-asks
    a host that already said no.
    """
    url = str(url or "").strip()
    if not url:
        return None
    got = _genre_cached("cover_dim", url,
                        lambda: _probe_dimensions(url, timeout))
    return got if isinstance(got, dict) else None


def _attach_dimensions(rows, limit=COVER_PROBE_LIMIT, timeout=10.0):
    """Fill what each row's own image says: size, container, byte count.

    The first *limit* rows, probed in a small pool — these are 20+ different
    CDNs, and a search that waits on them one at a time is a search that hangs
    on one slow host. A row COV already sized is probed only when its CONTAINER
    is still unknown (the format tier of the cover policy reads it, and a row
    whose size came from COV's own line has no format yet); a row that answers
    nothing at all keeps `bytes: 0`, which is the one thing the policy must be
    able to tell apart from "never asked".
    """
    todo = [r for r in rows[:limit]
            if r.get("width") is None or r.get("format") is None]
    if not todo:
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=COVER_PROBE_WORKERS) as pool:
        sizes = list(pool.map(
            lambda r: image_dimensions(r.get("big") or r.get("small"), timeout),
            todo))
    for row, size in zip(todo, sizes):
        if not size:
            continue
        if size.get("width"):
            # The probe's OWN reading wins over anything the provider stated:
            # that number describes the file the provider holds, while this one
            # describes the bytes this URL answers with. ``None`` (unknown) stays
            # unknown either way.
            row["width"], row["height"] = size["width"], size["height"]
        if "format" in size:
            # "" is an answer too: the bytes are not a JPEG/PNG/WebP image.
            row["format"] = size["format"]
        if size.get("bytes") is not None:
            row["bytes"] = size["bytes"]


# --------------------------------------------------------------------------- #
# The identity providers — asked by ID, not by name
# --------------------------------------------------------------------------- #
# The meta-search (COV) is a NAME search: it is fast, it carries the big store
# artwork, and it can also answer with another artist's album. The Cover Art
# Archive is an IDENTITY read: asked about the release GROUP it answers with
# the album's own front cover — the reference `mlo.cover_choice` prefers and
# the one the finder shows the candidates beside — and asked about a specific
# release it answers with that edition's own front cover. Both are asked when
# both are known, and the group's image is what wins: one pressing's sleeve is
# not the album's art.
#
# The fallbacks below are reached only when neither has anything; each is a
# single lookup, and each states in the search's `sources` report what it did.
CAA_BASE = "https://coverartarchive.org"
DEEZER_API = "https://api.deezer.com"
COVER_FALLBACKS = ("coverartarchive", "deezer", "itunes")


def _artwork_big(url):
    """`…/100x100bb.jpg` → `…/3000x3000bb.jpg` (Apple's largest artwork).

    `artworkUrl100` is the only artwork URL Apple's search returns, and the
    same asset is served at any size in that segment.
    """
    return re.sub(r"/\d+x\d+bb\.", "/3000x3000bb.", str(url or "")) or None


def _name_rank(name, want):
    """0 when *name* is exactly *want* (folded), else 1 — sorting only.

    Whether a row is THIS album is not decided here: a search is allowed to
    answer with a tribute, a karaoke or an 8-bit release carrying the same
    names, and `mlo.cover_choice`'s album-identity rule rejects such a row
    (with its own stated artist/title/tracks, which is why every row carries
    them). Sorting only means exactly that — a name that does not match does
    not hide the row, it puts it later.
    """
    return 0 if (want and _norm_compare(name) == want) else 1


def _caa_rows(mbid, scope, limit, artist, album, timeout, release_cover):
    """CAA images for one id, front cover first, labelled with what they are.

    `scope` is "release" or "release-group" — the endpoint, and therefore the
    answer's meaning: a group's images are the album's own art
    (`release_cover` False, the reference the policy prefers), while a
    specific release's are that edition's own front/back/other images
    (`release_cover` True). CAA publishes no dimensions, which is exactly why
    the probe exists.
    """
    ident = str(mbid or "").strip()
    if not ident:
        return []
    data = _advisory_json(f"{CAA_BASE}/{scope}/{ident}",
                          headers={"User-Agent": USER_AGENT},
                          timeout=timeout, host="coverartarchive.org")
    images = [i for i in ((data or {}).get("images") or []) if i.get("image")]
    images.sort(key=lambda i: not i.get("front"))
    page = f"{CAA_BASE}/{scope}/{ident}"
    rows = []
    for i, img in enumerate(images[:limit]):
        th = img.get("thumbnails") or {}
        types = [str(t).strip().lower() for t in (img.get("types") or [])]
        front = bool(img.get("front"))
        kind = ("front" if front else
                "back" if "back" in types else
                types[0] if types else "")
        rows.append(_cover_row(
            "coverartarchive",
            th.get("large") or th.get("small") or img["image"],
            img["image"], album, artist, None, page,
            front=front, kind=kind, release_cover=release_cover, rank=i))
    return rows


def _caa_release_covers(release_mbid, limit, artist="", album="", timeout=30.0):
    """The RELEASE's own images (its front cover first) — the identity read."""
    return _caa_rows(release_mbid, "release", limit, artist, album, timeout, True)


def _caa_covers(rg_mbid, limit, artist="", album="", timeout=30.0):
    """Cover Art Archive images for a RELEASE GROUP, front cover first.

    Asked about the release-group MBID the caller has (the import wizard holds
    one): CAA is browsed BY IDENTITY — its search is per release, and matching
    it by name is the name-guessing this app does not do. These are stand-ins
    for the group, not the release's own cover, and the policy says so.
    """
    return _caa_rows(rg_mbid, "release-group", limit, artist, album, timeout, False)


def _deezer_covers(artist, album, limit, timeout=30.0):
    """Deezer's album cover — `cover_xl` is the biggest Deezer serves
    (1000×1000), so nothing here is expected to be larger than that.

    Rows are ORDERED, never dropped: a tribute/karaoke album that carries the
    same title is a bad first hit, not a reason to hide the real covers. The
    matched album's own artwork is a front cover of THAT album (not a group
    stand-in), which is what `release_cover` says — and every row keeps the
    matched album's own title/artist/`nb_tracks`, so `mlo.cover_choice` can
    reject a row whose release is not the album being covered.
    """
    term = " ".join(x for x in (f'artist:"{artist}"' if artist else "",
                                f'album:"{album}"' if album else "") if x)
    if not term:
        return []
    data = _advisory_json(f"{DEEZER_API}/search/album",
                          {"q": term, "limit": max(1, min(limit, 25))},
                          timeout=timeout, host="api.deezer.com")
    want_artist, want_album = _norm_compare(artist), _norm_compare(album)
    rows = [a for a in ((data or {}).get("data") or [])
            if a.get("cover_xl") or a.get("cover_big")]
    rows.sort(key=lambda a: (_name_rank((a.get("artist") or {}).get("name"),
                                        want_artist),
                             _name_rank(a.get("title"), want_album)))
    return [_cover_row("deezer", a.get("cover_big") or a.get("cover_medium"),
                       a.get("cover_xl") or a.get("cover_big"),
                       a.get("title"), (a.get("artist") or {}).get("name"),
                       a.get("nb_tracks"), a.get("link"),
                       front=True, kind="front", release_cover=True, rank=i)
            for i, a in enumerate(rows[:limit])]


def _itunes_covers(artist, album, limit, cfg=None, timeout=30.0):
    """Apple's album artwork at its 3000×3000 size (see `_artwork_big`).

    Reached through the app's cached, throttled iTunes client — this is the
    same endpoint the advisory routes already use, and Apple rate-limits.
    """
    term = " ".join(x for x in (artist, album) if x)
    if not term:
        return []
    data = _apple_json("/search", {"term": term, "entity": "album",
                                   "limit": max(1, min(limit, 25)),
                                   "country": _apple_country(cfg)},
                       timeout=timeout)
    want_artist, want_album = _norm_compare(artist), _norm_compare(album)
    rows = [a for a in ((data or {}).get("results") or [])
            if a.get("artworkUrl100")]
    rows.sort(key=lambda a: (_name_rank(a.get("artistName"), want_artist),
                             _name_rank(a.get("collectionName"), want_album)))
    return [_cover_row("itunes", a["artworkUrl100"],
                       _artwork_big(a["artworkUrl100"]),
                       a.get("collectionName"), a.get("artistName"),
                       a.get("trackCount"), a.get("collectionViewUrl"),
                       front=True, kind="front", release_cover=True, rank=i)
            for i, a in enumerate(rows[:limit])]


def _source_row(pid, status, *, count=None, detail=""):
    """One line of the search's own report: who was asked, and what happened.

    `status` is "used" (it answered with candidates), "empty" (it answered with
    nothing), "error" (it refused or failed) or "skipped" (it was not asked at
    all — and `detail` says why: no id to ask about, no key configured, the
    source switched off). A source that contributed nothing is never left
    silent: the covers policy turns these into the pick's own notes.
    """
    row = {"id": pid, "status": status}
    if count is not None:
        row["count"] = int(count)
    if detail:
        row["detail"] = str(detail)[:300]
    return row


def _cover_fallback(artist, album, limit, cfg, rg_mbid, timeout):
    """(rows, provider, report) from the FIRST fallback that answers.

    Only ever reached when the meta-search and the release's own cover came
    back empty: one lookup per provider in COVER_FALLBACKS order, stopping at
    the first non-empty list. A provider that errors, or that has no identity
    to be asked about at all, says so in the report instead of being skipped in
    silence — and none of them is fatal.
    """
    report = []
    for pid in COVER_FALLBACKS:
        if pid == "coverartarchive" and not str(rg_mbid or "").strip():
            report.append(_source_row(pid, "skipped",
                                      detail="no release-group id to ask about"))
            continue
        try:
            if pid == "coverartarchive":
                rows = _caa_covers(rg_mbid, limit, artist, album, timeout)
            elif pid == "deezer":
                rows = _deezer_covers(artist, album, limit, timeout)
            else:
                rows = _itunes_covers(artist, album, limit, cfg, timeout)
        except Exception as e:
            report.append(_source_row(pid, "error", detail=f"{type(e).__name__}: {e}"))
            continue
        report.append(_source_row(pid, "used" if rows else "empty", count=len(rows)))
        if rows:
            return rows, pid, report
    return [], None, report


def cover_search(artist, album, limit=40, timeout=60.0, sources=None,
                 country=None, cfg=None, release_group_mbid="", release_mbid=""):
    """Album covers for artist/album → ``{"results", "provider", "sources"}``.

    Every row is ``{source, small, big, title, artist, tracks, url, width,
    height, format, bytes, front, kind, release_cover, rank}``: the keys the
    finder already reads, plus what the candidate was MEASURED from — the
    image's real pixel size, container and byte count, read from the file
    itself for the first ``COVER_PROBE_LIMIT`` rows (``None`` means unknown — a
    probe can never fail the search), whether the provider labels it the front
    cover of the release it belongs to, and its own rank in that provider's
    order.

    ``sources`` is the report `mlo.cover_choice.source_notes` turns into the
    pick's notes: one row per source that was asked — used / empty / error /
    skipped, with the reason — so a query that found nothing says what was
    tried and which source was never asked (no id, no key, switched off).

    ``provider`` names who answered first: ``"cov"`` for the meta-search, the
    fallback id that filled in (``coverartarchive``/``deezer``/``itunes``), and
    ``None`` when nobody had anything at all — an empty answer is STATED, never
    left as a silent zero-result.

    ``sources``/``country`` override the SAVED defaults (`cover_sources`,
    `cover_country`) for this one search only — nothing here writes config.
    ``release_group_mbid``/``release_mbid``, when the caller has them, are the
    identities the Cover Art Archive is asked about: the album's own front
    cover by the GROUP id (the reference the policy prefers) and one specific
    release's own by the release id. The name-based fallbacks (Deezer, iTunes)
    run only when the meta-search and the identity read both came back empty
    (or refused) — a dead meta-search is a fallback case, not an error the user
    has to understand.
    """
    if not artist and not album:
        raise ValueError("artist or album is required")
    src_ids, ctry = resolve_cov_search(sources, country, cfg)
    report = []
    # A source the catalogue reports as switched off is never asked, and that
    # is stated rather than looked like "it had nothing" (several of these
    # sources need a key the catalogue's operator holds, not the user).
    try:
        enabled = {str(s.get("id") or ""): bool(s.get("enabled", True))
                   for s in (cov_catalog(timeout).get("sources") or [])}
        for sid in src_ids:
            if enabled.get(sid) is False:
                report.append(_source_row(
                    sid, "skipped",
                    detail="switched off in the source catalogue — not asked"))
    except Exception:
        pass
    results = []
    try:
        cov = _cov_results(artist, album, limit, timeout, src_ids, ctry)
    except Exception as e:
        cov = []
        report.append(_source_row("covers.musichoarders.xyz", "error",
                                  detail=f"{type(e).__name__}: {e}"))
    else:
        counts = {}
        for r in cov:
            counts[str(r.get("source") or "?")] = counts.get(str(r.get("source") or "?"), 0) + 1
        report.append(_source_row(
            "covers.musichoarders.xyz", "used" if cov else "empty", count=len(cov),
            detail=", ".join(f"{k} {v}" for k, v in sorted(counts.items()))))
    results.extend(cov)
    provider = "cov" if cov else None

    # The album's own art first: the Cover Art Archive asked about the release
    # GROUP. It is the reference the finder shows beside the candidates and the
    # one the policy prefers, so it is asked whenever the caller knows the
    # group id — leaving it to the fallback chain (its old place) meant a
    # release with its own cover never produced the reference at all, and the
    # pick could only ever be a single edition's sleeve.
    if str(release_group_mbid or "").strip():
        try:
            ref = _caa_covers(release_group_mbid, limit, artist, album, timeout)
        except Exception as e:
            ref = []
            report.append(_source_row("coverartarchive", "error",
                                      detail=f"{type(e).__name__}: {e}"))
        else:
            report.append(_source_row(
                "coverartarchive", "used" if ref else "empty", count=len(ref),
                detail="the release group's own images, by release-group id"))
        results.extend(ref)
        if ref and not provider:
            provider = "coverartarchive"
    else:
        report.append(_source_row(
            "coverartarchive", "skipped",
            detail="no release-group id to ask about — only the release"))

    if str(release_mbid or "").strip():
        try:
            own = _caa_release_covers(release_mbid, limit, artist, album, timeout)
        except Exception as e:
            own = []
            report.append(_source_row("coverartarchive", "error",
                                      detail=f"{type(e).__name__}: {e}"))
        else:
            report.append(_source_row("coverartarchive",
                                      "used" if own else "empty", count=len(own),
                                      detail="the release's own images, by release id"))
        results.extend(own)
        if own and not provider:
            provider = "coverartarchive"
    else:
        report.append(_source_row(
            "coverartarchive", "skipped",
            detail="no release id to ask about — only the release group"))

    if not results:
        rows, pid, more = _cover_fallback(artist, album, limit, cfg,
                                          release_group_mbid, timeout)
        report.extend(more)
        results = rows
        provider = pid
    else:
        for pid in COVER_FALLBACKS:
            report.append(_source_row(pid, "skipped",
                                      detail="not asked — the sources above answered"))
    _attach_dimensions(results)
    return {"results": results, "provider": provider, "sources": report}




# Image downloads are the one place a caller supplies a URL the server then
# fetches. Provider CDNs do redirect (Cover Art Archive → archive.org), so
# redirects are followed — but each hop is re-validated, the destination must
# be a public host, and the body is capped: artwork that is not a few
# megabytes is a mistake or an attack, not a cover.
IMAGE_MAX_BYTES = 20 * 1024 * 1024
IMAGE_MAX_REDIRECTS = 5


def _public_host(host):
    """False for localhost, loopback, private, link-local, reserved, multicast
    and unspecified addresses — the SSRF guard for caller-supplied URLs."""
    import ipaddress
    import socket
    name = str(host or "").strip().strip("[]")
    if not name or name.lower().endswith(".local"):
        return False
    if name.lower() in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return False
    try:
        infos = socket.getaddrinfo(name, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def fetch_image_bytes(url, timeout=60.0):
    """Download an image URL and return (data, content_type).

    Only http(s) to a *public* host is allowed, every redirect hop is
    re-checked (a redirect chain must not reach an internal address after the
    first check), and the body is capped at IMAGE_MAX_BYTES so a huge or slow
    endpoint cannot be used as a memory/time hold.
    """
    from urllib.parse import urljoin, urlparse
    headers = {"User-Agent": COV_UA, "Referer": f"{COV_BASE}/"}
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("invalid image url")
    if not _public_host(parsed.hostname):
        raise ValueError("image url is not a public host")
    with httpx.Client(timeout=httpx.Timeout(timeout, read=timeout),
                      follow_redirects=False) as client:
        for _hop in range(IMAGE_MAX_REDIRECTS + 1):
            with client.stream("GET", url, headers=headers) as r:
                if r.is_redirect:
                    target = urljoin(url, str(r.headers.get("location") or ""))
                    hop = urlparse(target)
                    if hop.scheme not in ("http", "https") or not hop.netloc:
                        raise ValueError("invalid image redirect")
                    if not _public_host(hop.hostname):
                        raise ValueError("image redirect leaves the public internet")
                    url = target
                    continue
                r.raise_for_status()
                ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                chunks, size = [], 0
                for chunk in r.iter_bytes(65536):
                    size += len(chunk)
                    if size > IMAGE_MAX_BYTES:
                        raise ValueError("image is too large")
                    chunks.append(chunk)
                return b"".join(chunks), ctype
    raise ValueError("too many image redirects")


def lyrics_chain(cfg, artist, track, album=None, duration=None):
    """First provider hit for a track across the lyrics chain (the order lives
    in ``mlo.lyrics_providers.SOURCES``: LRCLIB, NetEase, QQ Music, Kuwo,
    Kugou, YouTube captions) — same dict as
    ``mlo.lyrics_providers.fetch_lyrics``, or None. Imported lazily so the
    engine module stays out of this header."""
    from mlo.lyrics_providers import fetch_lyrics
    return fetch_lyrics(cfg, artist, track, album, duration)
