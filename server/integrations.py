"""MusicBrainz / LRCLIB / RateYourMusic integrations for the import wizard.

MusicBrainz is queried with proper rate limiting (1 req/s) and a UA string
per their API etiquette. RYM has no public API — links are user-supplied
URLs stored as tags, but we validate/parse them here.
"""
import asyncio
import json
import re
import threading
import time
import uuid

import httpx

MB_BASE = "https://musicbrainz.org/ws/2"
LRCLIB_BASE = "https://lrclib.net/api"
USER_AGENT = "la-musica/2.0 (https://github.com/dillydalli3r/la-musica)"

_last_request = 0.0
_mb_lock = threading.Lock()


def mb_get(endpoint, params=None, timeout=30.0, retries=3):
    """Rate-limited MusicBrainz WS/2 GET returning parsed JSON.

    Retries 429/5xx (MusicBrainz rate-limits and has occasional 503s) with
    Retry-After-aware backoff, while keeping the 1 request/second etiquette.
    """
    global _last_request
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    for attempt in range(retries):
        with _mb_lock:
            elapsed = time.time() - _last_request
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)
            try:
                r = httpx.get(
                    f"{MB_BASE}/{endpoint}",
                    params=params or {},
                    headers=headers,
                    timeout=timeout,
                )
            except httpx.HTTPError:
                _last_request = time.time()
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
            _last_request = time.time()
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 1.5 * (attempt + 1)
            retry_after = r.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = max(wait, float(retry_after))
                except ValueError:
                    pass
            if attempt < retries - 1:
                time.sleep(wait)
                continue
        r.raise_for_status()
        return r.json()


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


def detect_mbid(mbid):
    """Which MusicBrainz entity kind does this MBID belong to?

    Tries a minimal lookup per browsable entity (cache-shared with the
    entity pages) and reports the first hit — lets the UI route a pasted
    bare ID without the user picking a type."""
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
    raise LookupError("no MusicBrainz entity found for this ID")


def _browse_collect(endpoint, extra_params, list_key, count_key, limit=300, offset=0):
    """Browse rows across MusicBrainz's 100-per-request pages.

    Browse has NO server-side sort, so a single arbitrary 100-row slice
    misrepresents a discography (one page can be all albums, the next all
    singles) and any date ordering would be a lie. This walks the pages
    (still 1 req/s) up to `limit` rows starting at `offset` so the caller
    can sort and filter over an honest window. Returns (rows, total)."""
    items = []
    pos = offset
    total = None
    while pos < offset + limit:
        data = mb_get_cached(
            endpoint,
            {**extra_params, "limit": min(100, offset + limit - pos), "offset": pos, "fmt": "json"},
        )
        batch = data.get(list_key) or []
        total = data.get(count_key) or total
        items.extend(batch)
        pos += len(batch)
        if not batch or pos >= min(total or 0, offset + limit):
            break
    if total is None:
        total = len(items)
    return items, total


def _mbid(value):
    """Extract a MusicBrainz ID from an ID or a musicbrainz.org URL."""
    if not value:
        return None
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value, re.I)
    return m.group(0).lower() if m else None


def _genres(node):
    return [g["name"] for g in (node.get("genres") or [])]


def _title_genres(node):
    """Genres in Title Case ('nu metal' -> 'Nu Metal') for tag import."""
    seen, out = set(), []
    for g in _genres(node):
        key = g.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(g.strip().title())
    return out


# --------------------------------------------------------------------------- #
# Release lookups
# --------------------------------------------------------------------------- #
def release_lookup(mbid):
    """Full release: media/discs, recordings, artist credits, genres and
    labels (catalog numbers). Country comes from the release entity."""
    data = mb_get(
        f"release/{mbid}",
        {"inc": "artists+recordings+media+release-groups+artist-credits+genres+labels", "fmt": "json"},
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
                "artist_mbids": [a["mbid"] for a in artists],
                "artist_credit": "".join(
                    (ac.get("name", "") + (ac.get("joinphrase", "") or ""))
                    for ac in trk.get("artist-credit", [])
                ),
                "genres": _title_genres(rec),
            })
    release_artists = [
        {"name": ac.get("name", ""), "mbid": ac["artist"].get("id")}
        for ac in data.get("artist-credit", []) if "artist" in ac
    ]
    rg_obj = data.get("release-group") or {}
    primary = (rg_obj.get("primary-type") or "").lower()
    secondary = [s.lower() for s in (rg_obj.get("secondary-types") or [])]
    release_type = "+".join([primary] + secondary) if primary else ""

    # labels -> label name + first catalog number
    catalog_number = ""
    label_name = ""
    for lab in data.get("label-info", []) or []:
        if not label_name:
            label_name = str(((lab.get("label") or {}).get("name")) or "").strip()
        cn = (lab.get("catalog-number") or "").strip()
        if cn and not catalog_number:
            catalog_number = cn
        if label_name and catalog_number:
            break
    country = data.get("country") or ""

    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "date": (data.get("date") or ""),
        # the release-group's first-release-date — the "original" release
        # date shown next to this specific release's own date
        "originaldate": (rg_obj.get("first-release-date") or ""),
        "barcode": (data.get("barcode") or ""),
        "country": country,
        "catalog_number": catalog_number,
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
        "genres": _title_genres(data),
        "media": tracks,
        "medium_count": len(data.get("media", [])),
        "medium_formats": [m.get("format") or "" for m in data.get("media", [])],
    }


def release_group_genres(rg_mbid):
    try:
        data = mb_get(f"release-group/{rg_mbid}", {"inc": "genres", "fmt": "json"})
        return _genres(data)
    except Exception:
        return []


def artist_genres(artist_mbid):
    try:
        data = mb_get(f"artist/{artist_mbid}", {"inc": "genres", "fmt": "json"})
        return _genres(data)
    except Exception:
        return []


def genre_cascade(release, limit=None):
    """Cascading genre import: track -> release -> release-group -> artist.

    Genres are merged across levels (deduped, in popularity order, Title
    Case) and capped at `limit` per track. limit=None imports everything.
    Returns per-track genres plus the fallback chain used for each track.
    """
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
        return [{"id": a.get("id"), "name": a.get("name"), "type": a.get("type")}
                for a in data.get("artists", [])]
    except Exception as e:
        return {"error": str(e)}


# --------------------------------------------------------------------------- #
# Generic browse: search + artist / release-group / recording pages
# (used by the in-app MusicBrainz browser)
# --------------------------------------------------------------------------- #
MB_ENTITIES = ("artist", "release-group", "release", "recording")


def _credit(node):
    return "".join(
        (ac.get("name", "") + (ac.get("joinphrase", "") or ""))
        for ac in (node.get("artist-credit") or [])
    )


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


def search_mb(entity, query, limit=100, mode="free", offset=0,
              primary_type="", secondary_type=""):
    """Normalized MB search rows for the four browsable entities.

    mode="free" is the plain full-text search; for releases, mode="catno" /
    "barcode" search by catalog number / barcode (catalog numbers like
    'SRCS 8757' are how pressings are identified). primary_type /
    secondary_type narrow releases and release groups with MusicBrainz's own
    type qualifiers — the only way to ask the index for "albums that are
    soundtracks" instead of filtering the rows afterwards. Returns
    {rows, total} — total is MusicBrainz's match count so the UI can offer
    deeper paging (searches cap at 100 rows per request)."""
    if entity not in MB_ENTITIES:
        raise ValueError("entity must be artist, release-group, release or recording")
    q = query
    if entity == "release" and mode == "catno":
        q = f'catno:"{query}"'
    elif entity == "release" and mode == "barcode":
        q = f"barcode:{query}"
    if entity in ("release", "release-group"):
        # quoted: several secondary types are multi-word ("Audio drama",
        # "DJ-mix", "Field recording")
        if primary_type:
            q = f'{q} AND primarytype:"{primary_type}"'
        if secondary_type:
            q = f'{q} AND secondarytype:"{secondary_type}"'
    data = mb_get_cached(entity, {"query": q, "limit": limit, "offset": offset, "fmt": "json"})
    # MB search responses use plural collection keys
    key = {"artist": "artists", "release-group": "release-groups",
           "release": "releases", "recording": "recordings"}[entity]
    rows = []
    for item in data.get(key, []):
        row = {
            "id": item.get("id"),
            "score": item.get("score"),
            "title": item.get("title") or item.get("name"),
            "disambiguation": item.get("disambiguation") or "",
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
            row.update({
                "artist": _credit(item),
                "length": item.get("length"),
                "first_release_date": item.get("first-release-date") or "",
            })
        rows.append(row)
    return {"rows": rows, "total": data.get("count") or len(rows)}


def artist_browse(mbid, limit=300, offset=0):
    """Artist page: identity + genres + full discography (release groups).

    Discography comes from the *browse* endpoint (release-group?artist=…)
    rather than a lookup's inc= subquery — lookups silently cap the related
    list. Pages are collected (up to `limit`) so type filters and the
    chronological order are honest across MusicBrainz's unsorted pages."""
    data = mb_get_cached(f"artist/{mbid}", {"inc": "genres", "fmt": "json"})
    rgs, total = _browse_collect(
        "release-group", {"artist": mbid}, "release-groups", "release-group-count",
        limit=limit, offset=offset,
    )
    area = data.get("area") or {}
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "disambiguation": data.get("disambiguation") or "",
        "type": data.get("type") or "",
        "country": area.get("name") or "",
        "life_span": [
            (data.get("life-span") or {}).get("begin") or "",
            (data.get("life-span") or {}).get("end") or "",
        ],
        "genres": _title_genres(data),
        "tags": [t.get("name") for t in (data.get("tags") or [])[:8]],
        "total": total,
        "offset": offset,
        "release_groups": [
            {
                "id": rg.get("id"),
                "title": rg.get("title"),
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


def release_group_browse(mbid, limit=300, offset=0):
    """Release-group page: identity + its releases (editions), each with
    media so every row carries format, disc count and its '10 + 11' track
    breakdown. Releases come from the browse endpoint (collected across
    pages) because the lookup's release subquery both truncates and omits
    media."""
    data = mb_get_cached(
        f"release-group/{mbid}",
        {"inc": "artist-credits+genres", "fmt": "json"},
    )
    rel_rows, total = _browse_collect(
        "release", {"release-group": mbid, "inc": "media"}, "releases", "release-count",
        limit=limit, offset=offset,
    )
    releases = []
    for r in sorted(
        rel_rows,
        key=lambda r: r.get("date") or "9999",
    ):
        track_count, track_breakdown = _release_counts(r)
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
            "barcode": r.get("barcode") or "",
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
        "genres": _title_genres(data),
        "first_release_date": data.get("first-release-date") or "",
        "total": total,
        "offset": offset,
        "releases": releases,
    }


def recording_browse(mbid, limit=300, offset=0):
    """Recording ('track') page: identity + releases carrying it (browsed,
    with media, for the same reasons as the release-group page)."""
    data = mb_get_cached(
        f"recording/{mbid}",
        {"inc": "artist-credits+isrcs+genres", "fmt": "json"},
    )
    rel_rows, total = _browse_collect(
        "release",
        {"recording": mbid, "inc": "media+artist-credits+release-groups"},
        "releases", "release-count",
        limit=limit, offset=offset,
    )
    releases = []
    for r in sorted(
        rel_rows,
        key=lambda r: r.get("date") or "9999",
    ):
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
        "genres": _title_genres(data),
        # a recording lookup returns bare ISRC strings ("USRC17607839") while
        # some other entities wrap them in {"isrc": ...} — .get() on a string
        # raised AttributeError and 502'd the whole recording page.
        "isrcs": [v for v in (_isrc(i) for i in data.get("isrcs") or []) if v],
        "total": total,
        "offset": offset,
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
            for m in release_media:
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

    At least one of plain/synced must be non-empty; both may be sent.
    Returns (ok, message). The public API requires a descriptive
    User-Agent, which _LRCLIB_HEADERS already carries."""
    artist = (artist or "").strip()
    track = (track or "").strip()
    album = (album or "").strip()
    plain = (plain or "").strip() or None
    synced = (synced or "").strip() or None
    if not artist or not track:
        return False, "artist and track name are required"
    if not plain and not synced:
        return False, "nothing to publish — add plain or synced lyrics"
    try:
        duration = int(duration or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        return False, "track duration is required for publishing"
    params = {
        "artist_name": artist,
        "track_name": track,
        "album_name": album or track,
        "duration": duration,
    }
    body = {"plainLyrics": plain or "", "syncedLyrics": synced or ""}
    try:
        r = httpx.post(f"{LRCLIB_BASE}/publish", params=params, json=body,
                       headers=_LRCLIB_HEADERS, timeout=20)
    except Exception as e:
        return False, f"publish failed: {e}"
    if r.status_code in (200, 201):
        return True, "published to LRCLIB — thank you for contributing!"
    if r.status_code == 429:
        return False, "LRCLIB is rate-limiting this IP — try again in a minute"
    detail = (r.text or "").strip()[:200]
    return False, f"LRCLIB refused ({r.status_code}): {detail or 'unknown error'}"



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
COV_SOURCE_PRIORITY = [
    "qobuz", "applemusic", "tidal", "bandcamp", "deezer", "spotify",
    "itunes", "discogs", "musicbrainz",
]
COV_MAX_SOURCES = 9
COV_FALLBACK_SOURCES = [
    "qobuz", "applemusic", "tidal", "bandcamp", "deezer", "spotify",
    "itunes", "discogs", "musicbrainz",
]

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


def cover_search(artist, album, limit=40, timeout=60.0, sources=None,
                 country=None, cfg=None):
    """Search COV for album covers. Returns a list of
    {source, small, big, title, artist, tracks, url} dicts sorted by the
    site's relevance order, capped at *limit*.

    `sources` and `country` override the saved defaults for this one search
    (the finder's source picker and region dropdown)."""
    if not artist and not album:
        raise ValueError("artist or album is required")
    src_ids, ctry = resolve_cov_search(sources, country, cfg)
    body = {"country": ctry, "sources": src_ids}
    if artist:
        body["artist"] = artist
    if album:
        body["album"] = album
    headers = {
        "User-Agent": COV_UA,
        "Referer": f"{COV_BASE}/",
        "Origin": COV_BASE,
        "X-Session": uuid.uuid4().hex,
    }
    results = []
    with httpx.Client(timeout=httpx.Timeout(timeout, read=timeout)) as client:
        with client.stream("POST", f"{COV_BASE}/api/search", json=body,
                           headers=headers) as r:
            r.raise_for_status()
            for line in r.iter_lines():
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
                results.append({
                    "source": ev.get("source"),
                    "small": ev.get("smallCoverUrl"),
                    "big": ev.get("bigCoverUrl"),
                    "title": rel.get("title"),
                    "artist": rel.get("artist"),
                    "tracks": rel.get("tracks"),
                    "url": rel.get("url"),
                })
                if len(results) >= limit:
                    break
    return results


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
    """First provider hit for a track across the lyrics chain (LRCLIB, NetEase,
    lyrics.ovh, Kugou) — same dict as mlo.lyrics_providers.fetch_lyrics, or
    None. Imported lazily so the engine module stays out of this header."""
    from mlo.lyrics_providers import fetch_lyrics
    return fetch_lyrics(cfg, artist, track, album, duration)
