"""Import a playlist from a streaming service into the app's own playlists.

Four services, and exactly how each one is read — no provider framework, no
plugin registry: one function per service and a URL recogniser that refuses
anything else.

  * **Deezer** — the public Web API, no key of any kind:
    ``GET https://api.deezer.com/playlist/<id>``, following the ``tracks.next``
    link it prints until there is none (each page is checked against itself, so
    a ``next`` that points back at a page already read cannot loop). Deezer
    states each track's ISRC, which is the strongest identity the library
    holds.
  * **Spotify** — the Web API with the client-credentials token this app
    already knows how to get (``server.integrations._spotify_token``, the same
    token the ISRC advisory and the genre sources use). A public playlist is
    readable with the app's own ``spotify_client_id`` + ``spotify_client_secret``
    and nothing else; with either of them unset the error names that setting,
    the way ``GET /api/sources/health`` does. ``tracks.next`` paginates.
  * **YouTube Music** — the app's OWN yt-dlp probe (``server.youtube``: the
    vendored module/binary, the app's cookie jar, one set of flat-playlist
    flags). One request, flat: no per-entry page, no second yt-dlp invocation
    style anywhere in this module.
  * **Apple Music** — there is NO public playlist API without a developer
    token, so the public playlist PAGE is read and the JSON Apple embeds in it
    (``<script id="serialized-server-data">``) is parsed. That is fragile by
    construction: it is Apple's own page payload, it changes without notice,
    and the error says exactly what came back (status, content type, size)
    when it does not parse.

The import is two phases, both of them the user's to see:

  1. **Match** every fetched track against the library with the app's existing
     identity helpers — ``server.mbresolve`` for the MusicBrainz id, the
     library payload's own ISRC, and ``server.integrations._norm_compare`` /
     ``title_matches`` for the name comparison (never a second fuzzy matcher).
     Each row reports whether it matched and, when it did not, WHY.
  2. **Create** the playlist from the matched paths, in the service's order,
     deduplicated, with the path form ``server.playlists.add_tracks`` already
     stores. When ``playlist_import_create_empty`` is off, an import that
     matched nothing creates nothing.

The queue is only ever touched for a track whose album is not already in the
library, and only when ``playlist_import_parent_albums`` is on: the PARENT
ALBUM is queued through the existing add-by-name path
(``server.api_add.library_add`` — MusicBrainz match, then a wish / framework
album, whichever that path picks) — albums, never tracks. Off, the import is
informational: the playlist holds the matched paths and nothing is queued.
"""
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import httpx

# Importing it installs the ONE shared client behind the module-level
# `httpx.get` the playlist fetches use (server/httpclient.py).
from server import httpclient  # noqa: F401

from mlo.config import load_config

# --------------------------------------------------------------------------- #
# The four services
# --------------------------------------------------------------------------- #
SERVICES = ("deezer", "spotify", "youtube", "apple")
SERVICE_LABELS = {"deezer": "Deezer", "spotify": "Spotify",
                  "youtube": "YouTube Music", "apple": "Apple Music"}
# The playlist URL each service's links look like — quoted back when a link
# names the wrong service, so the sentence says what to paste instead.
SERVICE_HINTS = {
    "deezer": "deezer.com/playlist/<id>",
    "spotify": "open.spotify.com/playlist/<id>",
    "youtube": "music.youtube.com/playlist?list=<id>",
    "apple": "music.apple.com/<country>/playlist/<name>/pl.<id>",
}

TIMEOUT = 20.0        # one JSON API answer
PAGE_TIMEOUT = 40.0   # Apple's playlist page is a ~0.5 MB document
DEEZER_API = "https://api.deezer.com/playlist"
SPOTIFY_API = "https://api.spotify.com/v1/playlists"
SPOTIFY_FIELDS = ("name,tracks.items(track(name,artists(name),album(name),"
                  "duration_ms,external_ids)),tracks.next")
_APPLE_JSON_RX = re.compile(
    r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>',
    re.S | re.I)
# A desktop browser's UA: the storefront APIs (Deezer, Apple's page) answer a
# browser and refuse some bare client strings.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class StreamingError(Exception):
    """Anything that stopped the import, in the service's own words."""


# --------------------------------------------------------------------------- #
# The one HTTP seam
# --------------------------------------------------------------------------- #
def _get(url, params=None, headers=None, timeout=None, expect="json"):
    """GET *url* → ``(body, error, meta)``.

    `error` is the answer's own words and "" when it answered — the same shape
    ``server.integrations._advisory_post`` reports a refusal in
    ("HTTP 404: …", "no answer (ConnectError: …)"), because an import that
    cannot read a playlist has to say what the service said. `meta` carries
    the status, content type and size for the errors that describe a page
    (Apple's), and is unused by the JSON callers. `expect="text"` returns the
    body undecoded.
    """
    sent = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
    if expect == "text":
        sent["Accept"] = "text/html,application/xhtml+xml"
    sent.update(headers or {})
    try:
        r = httpx.get(url, params=params, headers=sent,
                      timeout=timeout or TIMEOUT, follow_redirects=True)
    except Exception as e:
        return None, f"no answer ({type(e).__name__}: {e})", {}
    body = r.text or ""
    meta = {
        "status": r.status_code,
        "content_type": (r.headers.get("content-type") or "").split(";")[0].strip(),
        "bytes": len(r.content or b""),
        "url": str(r.url),
    }
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}: {body.strip()[:300]}", meta
    if expect == "text":
        return body, "", meta
    try:
        return r.json(), "", meta
    except ValueError:
        return None, (f"answered HTTP {r.status_code} "
                      f"{meta['content_type'] or 'with no content type'} and no "
                      f"JSON: {body.strip()[:200]}"), meta


def _page_facts(meta):
    """The page an error is about, as the sentence states it."""
    if not meta:
        return "no answer"
    kb = float(meta.get("bytes") or 0) / 1024.0
    return (f"HTTP {meta.get('status')}, "
            f"{meta.get('content_type') or 'no content type'}, {kb:.0f} KB")


# --------------------------------------------------------------------------- #
# URL recognition
# --------------------------------------------------------------------------- #
def _playlist_id(rx, text):
    m = re.search(rx, text or "", re.I)
    return m.group(1) if m else ""


def service_of(url):
    """Which service a playlist URL belongs to, or "" (see `_unreadable`)."""
    text = str(url or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if not parts.netloc:                      # a bare id is not a URL
        return ""
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    if host == "deezer.com" or host.endswith(".deezer.com"):
        return "deezer" if _playlist_id(r"/playlist/(\d+)", path) else ""
    if host == "open.spotify.com":
        return "spotify" if _playlist_id(r"/playlist/([A-Za-z0-9]+)", path) else ""
    if host == "music.youtube.com" or host in ("youtube.com", "www.youtube.com",
                                               "m.youtube.com", "youtu.be"):
        return "youtube" if parse_qs(parts.query).get("list") else ""
    if host == "music.apple.com" or host.endswith(".music.apple.com"):
        return "apple" if "/playlist/" in path else ""
    return ""


# The link a service's OWN pages use for a single entity, so a track/album link
# pasted where a playlist link belongs is answered with what it actually is.
_WRONG_KIND = (
    ("deezer.com", r"/(track|album|artist|episode|show)/\d+",
     ("a Deezer {0} link — the import reads a Deezer PLAYLIST link "
      "(deezer.com/playlist/<id>)")),
    ("open.spotify.com", r"/(album|track|artist|episode|show|user)/",
     ("a Spotify {0} link — the import reads a Spotify PLAYLIST link "
      "(open.spotify.com/playlist/<id>)")),
    ("music.apple.com", r"/(album|song|artist)/",
     ("an Apple Music {0} link — the import reads an Apple Music PLAYLIST link "
      "(music.apple.com/<country>/playlist/<name>/pl.<id>)")),
)


def _unreadable(url):
    """Why *url* is not a playlist this module reads, in one sentence."""
    text = str(url or "").strip()
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if not text:
        return "no playlist URL was given"
    if not parts.netloc:
        return (f"“{text}” is not a link — paste a playlist URL from Deezer, "
                "Spotify, YouTube Music or Apple Music")
    for suffix, rx, sentence in _WRONG_KIND:
        if host == suffix or host.endswith("." + suffix):
            m = re.search(rx, parts.path or "", re.I)
            if m:
                return sentence.format(m.group(1))
    if "youtube.com" in host or host.endswith("youtu.be"):
        return ("that is a YouTube video link — the import reads a YouTube "
                "Music PLAYLIST link (music.youtube.com/playlist?list=<id>)")
    return (f"nothing here reads {text} — paste a playlist link from Deezer "
            f"({SERVICE_HINTS['deezer']}), Spotify ({SERVICE_HINTS['spotify']}), "
            f"YouTube Music ({SERVICE_HINTS['youtube']}) or Apple Music "
            f"({SERVICE_HINTS['apple']})")


# --------------------------------------------------------------------------- #
# Deezer — the public API, no key
# --------------------------------------------------------------------------- #
def _deezer_track(row):
    artist = row.get("artist") or {}
    album = row.get("album") or {}
    return {
        "title": str(row.get("title") or row.get("title_short") or "").strip(),
        "artist": str((artist or {}).get("name") or "").strip(),
        "album": str((album or {}).get("title") or "").strip(),
        "duration": row.get("duration") or 0,
        "isrc": str(row.get("isrc") or "").strip().upper(),
        "url": str(row.get("link") or "").strip(),
        "mbid": "",
    }


def _fetch_deezer(url, cfg=None):
    pid = _playlist_id(r"/playlist/(\d+)", url)
    data, error, _meta = _get(f"{DEEZER_API}/{pid}")
    if error:
        raise StreamingError(
            f"Deezer answered {error} for playlist {pid}. The public API "
            f"(GET {DEEZER_API}/{pid}) needs no key, so this is Deezer's own "
            "answer about the playlist.")
    if not isinstance(data, dict) or data.get("error"):
        detail = (data or {}).get("error") or {}
        raise StreamingError(
            "Deezer has no playlist to answer with — "
            + (f"{detail.get('message')} (code {detail.get('code')})"
               if isinstance(detail, dict) and detail else "its answer carried "
               "no playlist")
            + f". A playlist that was deleted, or is private, reads exactly like this.")
    tracks = [_deezer_track(t) for t in (data.get("tracks") or {}).get("data") or []]
    nxt = str((data.get("tracks") or {}).get("next") or "").strip()
    seen = {nxt} if nxt else set()
    while nxt:
        page, error, _meta = _get(nxt)
        if error:
            raise StreamingError(f"Deezer answered {error} for the next page of "
                                 f"playlist {pid} ({nxt}).")
        if not isinstance(page, dict) or page.get("error"):
            raise StreamingError(f"Deezer's next page for playlist {pid} carried "
                                 "no tracks.")
        tracks.extend(_deezer_track(t) for t in page.get("data") or [])
        nxt = str(page.get("next") or "").strip()
        if nxt in seen:            # a `next` pointing back would never end
            break
        seen.add(nxt)
    return {"title": str(data.get("title") or "").strip(), "tracks": tracks}


# --------------------------------------------------------------------------- #
# Spotify — the Web API with the app's own client-credentials token
# --------------------------------------------------------------------------- #
def _spotify_track(item):
    """One `tracks.items[]` entry. A row that names no track (a podcast
    episode, a local file) is kept as an empty row: the report then says so,
    instead of an import quietly dropping a row the playlist really has."""
    row = (item or {}).get("track") or {}
    artists = row.get("artists") or []
    album = row.get("album") or {}
    ms = row.get("duration_ms")
    return {
        "title": str(row.get("name") or "").strip(),
        "artist": ", ".join(str((a or {}).get("name") or "").strip()
                            for a in artists if (a or {}).get("name")),
        "album": str((album or {}).get("name") or "").strip(),
        "duration": int(ms / 1000) if ms else 0,
        "isrc": str((row.get("external_ids") or {}).get("isrc") or "").strip().upper(),
        "url": str((row.get("external_urls") or {}).get("spotify")
                   or ((item or {}).get("track") or {}).get("uri") or "").strip(),
        "mbid": "",
    }


def _fetch_spotify(url, cfg=None):
    from server import integrations as intg

    cid = str((cfg or {}).get("spotify_client_id") or "").strip()
    secret = str((cfg or {}).get("spotify_client_secret") or "").strip()
    if not cid or not secret:
        raise StreamingError(
            "Spotify needs spotify_client_id and spotify_client_secret (Settings "
            "→ Discovery): the import reads the public Web API (GET "
            f"{SPOTIFY_API}/<id>) with the token those two keys issue, and "
            "Spotify refuses an anonymous caller. Set them, then try again.")
    started = time.time()
    token = intg._spotify_token(cfg)
    if not token:
        raise StreamingError(
            "Spotify did not issue a token for the saved client credentials — "
            + (intg.spotify_last_error(started)
               or "POST https://accounts.spotify.com/api/token answered no "
                  "access_token"))
    pid = _playlist_id(r"/playlist/([A-Za-z0-9]+)", url)
    headers = {"Authorization": f"Bearer {token}"}
    data, error, _meta = _get(f"{SPOTIFY_API}/{pid}",
                              params={"fields": SPOTIFY_FIELDS}, headers=headers)
    if error:
        hint = (" A private playlist, or one owned by another account, reads "
                "exactly like this: the client-credentials token sees public "
                "playlists only." if "HTTP 404" in error else "")
        raise StreamingError(
            f"Spotify answered {error} for playlist {pid} "
            f"(GET {SPOTIFY_API}/{pid}).{hint}")
    if not isinstance(data, dict):
        raise StreamingError(f"Spotify's answer for playlist {pid} was not a "
                             "playlist document.")
    tracks: List[dict] = list(_spotify_tracks(data))
    nxt = str((data.get("tracks") or {}).get("next") or "").strip()
    seen = {nxt} if nxt else set()
    while nxt:
        page, error, _meta = _get(nxt, headers=headers)
        if error:
            raise StreamingError(f"Spotify answered {error} for the next page of "
                                 f"playlist {pid} ({nxt}).")
        tracks.extend(_spotify_tracks(page))
        nxt = str(((page or {}).get("tracks") or page or {}).get("next")
                  or "").strip()
        if nxt in seen:
            break
        seen.add(nxt)
    return {"title": str(data.get("name") or "").strip(), "tracks": tracks}


def _spotify_items(page):
    """The `items` of a Spotify playlist page OR of a `tracks.next` paging
    object — the two shapes the same walk reads."""
    return ((page or {}).get("tracks") or {}).get("items") or \
        (page or {}).get("items") or []


def _spotify_tracks(page):
    return [_spotify_track(i) for i in _spotify_items(page)]


# --------------------------------------------------------------------------- #
# YouTube Music — the app's own flat yt-dlp probe
# --------------------------------------------------------------------------- #
def _youtube_entries(url, cfg=None):
    """Flat `(title, entries)` from the app's own yt-dlp probe.

    The seam is a function of its own so the suite can answer with a fixture:
    `server.youtube.flat_playlist` is the real thing — the vendored yt-dlp,
    the app's cookie jar, the existing flat-playlist flags.
    """
    from server import youtube

    return youtube.flat_playlist(url, cfg)


def _youtube_track(entry):
    """One flat yt-dlp entry as an import row.

    YouTube states no artist and no album: the CHANNEL names the uploader (the
    official one is "<Artist> - Topic") and a plain upload usually titles
    itself "Artist - Song". So the channel is the artist, and the title's own
    "Artist - Song" split is used ONLY when there is no channel at all — never
    both, and never as a rewrite of a title that came with an artist.
    """
    title = str((entry or {}).get("title") or "").strip()
    channel = str((entry or {}).get("channel") or (entry or {}).get("uploader")
                  or "").strip()
    artist = re.sub(r"\s*-\s*Topic$", "", channel, flags=re.I).strip()
    if not artist and " - " in title:
        left, _, right = title.partition(" - ")
        if left.strip() and right.strip():
            artist, title = left.strip(), right.strip()
    vid = str((entry or {}).get("id") or "").strip()
    return {
        "title": title,
        "artist": artist,
        "album": "",
        "duration": (entry or {}).get("duration") or 0,
        "isrc": "",
        "url": str((entry or {}).get("url")
                   or (f"https://www.youtube.com/watch?v={vid}" if vid else "")).strip(),
        "mbid": "",
    }


def _fetch_youtube(url, cfg=None):
    from server import youtube

    if not youtube.ytdlp_available(cfg):
        raise StreamingError(
            "YouTube Music is read with yt-dlp — the same copy the video "
            "downloads use — and it is not installed. Install it under "
            "Dependencies, then try again.")
    try:
        title, entries = _youtube_entries(url, cfg)
    except Exception as e:
        raise StreamingError(
            f"yt-dlp could not read {url}: {e}. A private or region-locked "
            "playlist reads exactly like this — and the cookie jar under "
            "Settings → Videos is what a playlist that wants a signed-in "
            "listener is read with.")
    tracks = [_youtube_track(e) for e in (entries or []) if e]
    if not tracks:
        raise StreamingError(
            f"yt-dlp read {url} and found no entries in it — an empty or "
            "private playlist reads exactly like this.")
    return {"title": str(title or "").strip(), "tracks": tracks}


# --------------------------------------------------------------------------- #
# Apple Music — the page's embedded JSON (no public playlist API)
# --------------------------------------------------------------------------- #
def _apple_sections(data):
    """Every `sections` list the embedded JSON carries, at any depth.

    Apple's block is a document, not an API answer: the shape below ``data``
    changes between page kinds, so the sections are looked for rather than
    assumed. (This is the fragility the module docstring states.)
    """
    found = []
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            secs = node.get("sections")
            if isinstance(secs, list):
                found.append(secs)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _apple_track(item):
    dur = item.get("duration")            # milliseconds
    subs = item.get("subtitleLinks") or []
    ter = item.get("tertiaryLinks") or []
    cd = item.get("contentDescriptor") or {}
    return {
        "title": str(item.get("title") or "").strip(),
        "artist": str(item.get("artistName")
                      or ((subs[0] or {}).get("title") if subs else "") or "").strip(),
        "album": str(((ter[0] or {}).get("title") if ter else "") or "").strip(),
        "duration": int(round(int(dur) / 1000.0)) if dur else 0,
        "isrc": "",
        "url": str(cd.get("url") or "").strip(),
        "mbid": "",
    }


def _apple_tracks_from_page(html):
    """(playlist title, tracks) out of Apple's embedded page JSON.

    Raises `StreamingError` naming what was wrong with the document; the
    caller adds the page facts (status, content type, size), which is what the
    caller who reads a changing page owes the user when it changes.
    """
    blocks = _APPLE_JSON_RX.findall(html or "")
    if not blocks:
        raise StreamingError('the page carried no <script '
                             'id="serialized-server-data"> block')
    try:
        data = json.loads(blocks[-1])
    except ValueError as e:
        raise StreamingError(f"its serialized-server-data block is not JSON ({e})")
    title = ""
    tracks: List[dict] = []
    for sections in _apple_sections(data):
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            kind = str(sec.get("itemKind") or "")
            for item in sec.get("items") or []:
                if not isinstance(item, dict):
                    continue
                cd_kind = str((item.get("contentDescriptor") or {}).get("kind") or "")
                if kind == "trackLockup" or cd_kind in ("song", "musicVideo"):
                    row = _apple_track(item)
                    if row["title"]:
                        tracks.append(row)
                elif not title and kind == "containerDetailHeaderLockup":
                    title = str(item.get("title") or "").strip()
    if not tracks:
        raise StreamingError("its serialized-server-data block holds no track "
                            "lockups (Apple answered a page, not a track list)")
    return title, tracks


def _fetch_apple(url, cfg=None):
    body, error, meta = _get(url, timeout=PAGE_TIMEOUT, expect="text")
    if error:
        raise StreamingError(
            f"Apple Music answered {error} — the playlist page itself is read "
            "(GET the URL as a browser would) because Apple has no public "
            "playlist API without a developer token. A playlist that is not "
            "shared publicly reads exactly like this.")
    try:
        title, tracks = _apple_tracks_from_page(body)
    except StreamingError as e:
        raise StreamingError(
            f"Apple Music's playlist page could not be read: {e}. Apple renders "
            'the track list as JSON embedded in the page (<script '
            'id="serialized-server-data">), which is what this import parses — '
            "there is no public playlist API without a developer token — and "
            "that block changes with the site. What came back was "
            f"{_page_facts(meta)}.")
    return {"title": title, "tracks": tracks}


_FETCHERS = {"deezer": _fetch_deezer, "spotify": _fetch_spotify,
             "youtube": _fetch_youtube, "apple": _fetch_apple}


def fetch(url, cfg=None, service=""):
    """Read one streaming playlist URL.

    → ``{service, service_label, url, title, tracks: [...]}`` where every track
    is ``{title, artist, album, duration, isrc, url, mbid}``. Raises
    `StreamingError` — with the service's own words, the setting it needs, or
    exactly what came back — when the URL is not a playlist this app reads.
    `service` names the service the caller believes it is talking to; a URL
    that belongs to another one is refused instead of silently read as the
    wrong thing.
    """
    text = str(url or "").strip()
    named = str(service or "").strip().lower()
    if named and named not in SERVICES:
        raise StreamingError(
            f"“{service}” is not one of the services this app reads — a playlist "
            "URL has to come from Deezer, Spotify, YouTube Music or Apple Music.")
    found = service_of(text)
    if not found:
        raise StreamingError(_unreadable(text))
    if named and named != found:
        raise StreamingError(
            f"that is a {SERVICE_LABELS[found]} playlist URL — the "
            f"{SERVICE_LABELS[named]} import reads {SERVICE_HINTS[named]}. "
            "Paste the link for the service you picked, or clear the service "
            "and let the URL decide.")
    payload = _FETCHERS[found](text, cfg if cfg is not None else load_config())
    payload.update(service=found, service_label=SERVICE_LABELS[found], url=text)
    return payload


# --------------------------------------------------------------------------- #
# Matching against the library
# --------------------------------------------------------------------------- #
def _library_index(library):
    """The library payload as the lookup tables a match needs.

    Titles/artists are keyed with the app's own normalizer
    (`server.integrations._norm_compare` → `server.discovery.norm`) so an
    accent, a case or a "feat." spelling cannot decide a match here and
    somewhere else in the app differently.
    """
    from server import integrations as intg

    index = {"by_isrc": {}, "by_title": {}, "albums": {}}
    for artist in (library or {}).get("artists") or []:
        for album in artist.get("albums") or []:
            meta = album.get("meta") or {}
            album_artist = str(album.get("album_artist")
                               or meta.get("ALBUMARTIST")
                               or artist.get("name") or "").strip()
            album_title = str(album.get("album") or meta.get("ALBUM") or "").strip()
            entry = {"album": album_title, "artist": album_artist}
            if album_title:
                index["albums"].setdefault(intg._norm_compare(album_title),
                                           []).append(entry)
            for track in album.get("tracks") or []:
                tags = track.get("tags") or {}
                title = str(tags.get("TITLE") or "").strip()
                if not title:
                    continue
                row = {
                    "path": str(track.get("path") or ""),
                    "title": title,
                    "artist": str(tags.get("ARTIST") or album_artist).strip(),
                    "album": str(tags.get("ALBUM") or album_title).strip(),
                }
                isrc = str(tags.get("ISRC") or "").strip().upper()
                if isrc:
                    index["by_isrc"].setdefault(isrc, []).append(row)
                index["by_title"].setdefault(intg._norm_compare(title),
                                             []).append(row)
    return index


def _artist_same(want, got):
    """Whether two artist strings name the same artist, the app's way.

    Exact under `_norm_compare` first, then the containment rule the discovery
    providers already use for a folder that says more than the provider does
    ("Radiohead" against "Radiohead & Friends", a "feat." credit). Never fuzzier
    than that: a different artist is not this track.
    """
    from server import integrations as intg

    a, b = intg._norm_compare(want), intg._norm_compare(got)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _find(index, track):
    """The library row a fetched track IS, or None."""
    from server import integrations as intg

    mbid = str(track.get("mbid") or "").strip().lower()
    if mbid:
        from server import mbresolve
        path = mbresolve.get_index().get("tracks", {}).get(mbid)
        if path:
            for row in index["by_title"].values():
                for cand in row:
                    if cand["path"] == path:
                        return cand
            return {"path": path, "title": track.get("title") or "",
                    "artist": track.get("artist") or "", "album": ""}
    isrc = str(track.get("isrc") or "").strip().upper()
    if isrc and index["by_isrc"].get(isrc):
        return index["by_isrc"][isrc][0]
    key = intg._norm_compare(track.get("title") or "")
    if not key:
        return None
    candidates = index["by_title"].get(key) or []
    if not candidates:
        return None
    artist = str(track.get("artist") or "").strip()
    if not artist:
        # No artist on the row at all: one candidate is this track, several
        # are not a match any error-free rule could pick from.
        return candidates[0] if len(candidates) == 1 else None
    for cand in candidates:
        if _artist_same(artist, cand["artist"]):
            return cand
    return None


def _unmatched_reason(index, track):
    """Why a fetched track is not in the library, in one sentence."""
    from server import integrations as intg

    title = str(track.get("title") or "").strip()
    artist = str(track.get("artist") or "").strip()
    isrc = str(track.get("isrc") or "").strip().upper()
    if not title:
        return "the playlist row names no track title"
    candidates = index["by_title"].get(intg._norm_compare(title)) or []
    if not candidates:
        return ("no track in the library has this title"
                + (f" (and none carries ISRC {isrc})" if isrc else ""))
    artists = []
    for cand in candidates:
        if cand["artist"] and cand["artist"] not in artists:
            artists.append(cand["artist"])
    if not artist:
        return (f"the library has this title {len(candidates)} times and the "
                "playlist row names no artist to choose between them"
                if len(candidates) > 1 else
                "the library has this title by another artist")
    listed = ", ".join(artists[:3]) or "an unnamed artist"
    return (f"the library has this title by {listed} — not by {artist}"
            + (f" (and no track carries ISRC {isrc})" if isrc else ""))


def match_tracks(tracks, library=None, cfg=None):
    """Match fetched tracks to the library → the report's rows.

    Every row carries the fetched identity (title/artist/album), whether it
    matched, the library path it matched (forward slashes — the form the
    library payload and the web client use), that recording's MusicBrainz id
    when the library states one, and — when it did not match — the reason.
    """
    from server import library as lib_mod

    library = library if library is not None else lib_mod.build_library(
        cfg if cfg is not None else load_config())
    index = _library_index(library)
    rows = []
    for i, track in enumerate(tracks or []):
        hit = _find(index, track)
        row = {
            "index": i,
            "title": str(track.get("title") or "").strip(),
            "artist": str(track.get("artist") or "").strip(),
            "album": str(track.get("album") or "").strip(),
            "duration": track.get("duration") or 0,
            "isrc": str(track.get("isrc") or "").strip().upper(),
            "url": str(track.get("url") or "").strip(),
            "matched": hit is not None,
            "path": hit["path"] if hit else None,
            "library_title": hit["title"] if hit else "",
            "library_artist": hit["artist"] if hit else "",
            "library_album": hit["album"] if hit else "",
            "mbid": "",
            "duplicate": False,
            "reason": "" if hit else _unmatched_reason(index, track),
            "queued": "",
        }
        if hit:
            try:
                from server import mbresolve
                row["mbid"] = mbresolve.track_mbid_for(hit["path"])
            except Exception:
                row["mbid"] = ""
        rows.append(row)
    return rows


def _album_in_library(index, album, artist):
    """Whether the library already holds *album* by *artist* (by name)."""
    from server import integrations as intg

    for cand in index["albums"].get(intg._norm_compare(album)) or []:
        if not artist or _artist_same(artist, cand["artist"]):
            return True
    return False


# --------------------------------------------------------------------------- #
# Queueing — the app's existing add-by-name path, never a second one
# --------------------------------------------------------------------------- #
def _queue_album(title, artist, source, page_url):
    """Queue ONE album through the existing add-by-name path.

    `server.api_add.library_add` with a name-only request is exactly the
    semantics this needs — MusicBrainz is searched for the album, and whatever
    that path decides (a release/edition added, or a name-keyed wish the queue
    searches) is what happens. This function is the seam the suite stubs.
    """
    from server import api_add

    return api_add.library_add(api_add.AddToLibraryRequest(
        kind="album", title=title, artist=artist, source=source,
        page_url=page_url))


def _queue_track(title, artist, source, page_url):
    """Queue ONE track by name (the same path, kind="track")."""
    from server import api_add

    return api_add.library_add(api_add.AddToLibraryRequest(
        kind="track", title=title, artist=artist, source=source,
        page_url=page_url))


def _queue_row(kind, title, artist, source, page_url, album=""):
    """One queue attempt, reported whatever it answered.

    A MusicBrainz outage must not lose the playlist the user asked for, so the
    failure is a row of its own with the reason the add gave.
    """
    from fastapi import HTTPException

    row = {"kind": kind, "title": title, "artist": artist, "album": album,
           "queued": False, "matched": False, "by_name": False,
           "wish_id": None, "mbid": "", "note": "", "error": ""}
    try:
        answer = (_queue_album if kind == "album" else _queue_track)(
            title, artist, source, page_url) or {}
    except HTTPException as e:
        row["error"] = str(e.detail)
        return row
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"
        return row
    row["note"] = str(answer.get("note") or "")
    row["matched"] = bool(answer.get("matched", True))
    row["by_name"] = bool(answer.get("by_name"))
    row["wish_id"] = answer.get("wish_id")
    row["queued"] = bool(answer.get("queued")) or bool(answer.get("wish_id"))
    albums = answer.get("albums") or []
    if albums:
        row["mbid"] = str((albums[0] or {}).get("mbid") or "")
    return row


# --------------------------------------------------------------------------- #
# The import
# --------------------------------------------------------------------------- #
def _dedupe_paths(rows):
    """The playlist's paths: matched rows, in order, deduplicated."""
    from server import playlists as pl_mod

    paths, seen = [], set()
    for row in rows:
        if not row["matched"] or not row["path"]:
            continue
        key = pl_mod._pathkey(row["path"])
        if key in seen:
            row["duplicate"] = True
            continue
        seen.add(key)
        paths.append(row["path"])
    return paths


def import_playlist(url, name="", cfg=None, service="", user="",
                    parent_albums=None, dry_run=False):
    """Read a streaming playlist, match it, and make it the app's playlist.

    The two phases, in order and both visible in the answer: `report` says what
    every row matched (and why not, when it did not), then the playlist is
    created from the matched paths in the service's order, deduplicated.

    `parent_albums` overrides `playlist_import_parent_albums` for this import
    only (None = the configured value). `dry_run` matches and reports without
    creating anything or queueing anything.
    """
    from server import library as lib_mod
    from server import playlists as pl_mod

    cfg = cfg if cfg is not None else load_config()
    payload = fetch(url, cfg, service=service)
    library = lib_mod.build_library(cfg)          # one walk for both phases
    index = _library_index(library)
    rows = match_tracks(payload["tracks"], library=library)
    paths = _dedupe_paths(rows)

    want_albums = (bool(cfg.get("playlist_import_parent_albums"))
                   if parent_albums is None else bool(parent_albums))
    unmatched_mode = str(cfg.get("playlist_import_unmatched", "skip") or "skip")
    create_empty = bool(cfg.get("playlist_import_create_empty", True))

    report = {
        "service": payload["service"],
        "service_label": payload["service_label"],
        "source_url": payload["url"],
        "title": payload["title"],
        "total": len(rows),
        "matched": sum(1 for r in rows if r["matched"]),
        "unmatched": sum(1 for r in rows if not r["matched"]),
        "duplicates": sum(1 for r in rows if r["duplicate"]),
        "tracks": rows,
        "parent_albums": {"enabled": want_albums, "queued": []},
        "unmatched_tracks": {"mode": unmatched_mode, "queued": []},
    }

    # Phase 2 — the queue, then the playlist. Nothing is queued for a dry run.
    if not dry_run:
        if want_albums:
            _queue_parent_albums(report, payload, index)
        if unmatched_mode == "wish":
            _queue_unmatched_tracks(report, payload, index)

    created = False
    playlist = None
    if paths or create_empty:
        label = str(name or "").strip() or payload["title"] or \
            f"{payload['service_label']} playlist"
        if dry_run:
            report["note"] = _note(report, paths, label, dry_run=True)
        else:
            pid = pl_mod.create_playlist(
                label, kind="manual", user=user,
                origin=payload["service"], origin_url=payload["url"])
            pl_mod.add_tracks(pid, [os.path.normpath(p) for p in paths],
                              user=user)
            playlist = pl_mod.get_playlist(pid, user)
            created = True
            report["note"] = _note(report, paths, label, dry_run=False)
    else:
        report["note"] = (
            f"No track matched, and a playlist is not created empty here "
            "(Settings → Streaming playlist import) — nothing was written.")
    return {"ok": True, "report": report, "playlist": playlist,
            "created": created, "dry_run": bool(dry_run)}


def _note(report, paths, label, dry_run):
    """The sentence that says what the import did. `paths` is the matched
    list, `label` the name the playlist would carry or carries."""
    head = (f"{len(paths)} of {report['total']} tracks are in the library"
            if report["total"] else "the playlist holds no tracks")
    if report["duplicates"]:
        head += f" ({report['duplicates']} duplicate row(s) skipped)"
    if dry_run:
        return (f"{head}. Nothing was created or queued — this was a check.")
    if not paths:
        return f"{head}. “{label}” was created with no tracks to hold."
    return f"{head}. They are in “{label}”, in the playlist's own order."


def _queue_parent_albums(report, payload, index):
    """Queue the parent ALBUM of every imported track the library does not have
    — one add per album, never one per track.

    A track that MATCHED queued nothing: its album is the library album the row
    matched, so it is in the library by construction. A track that did not
    match and names no album (a YouTube row carries none) is reported as such
    rather than left silently out of the list of what was queued.
    """
    done = {}
    for row in report["tracks"]:
        if row["matched"]:
            continue
        album = row["album"] or row["library_album"]
        artist = row["artist"] or row["library_artist"]
        if not album:
            report["parent_albums"]["queued"].append({
                "kind": "album", "title": row["title"], "artist": artist,
                "album": "", "queued": False, "matched": False,
                "by_name": False, "wish_id": None, "mbid": "", "error": "",
                "note": "", "reason": "the playlist row names no album"})
            continue
        key = (album.strip().lower(), artist.strip().lower())
        if key in done:
            # The same album, another track: it was queued once, and this row
            # reports what THAT attempt did rather than claiming its own.
            row["queued"] = "album" if done[key] else ""
            continue
        if _album_in_library(index, album, artist):
            row["queued"] = ""
            done[key] = False
            report["parent_albums"]["queued"].append({
                "kind": "album", "title": album, "artist": artist,
                "queued": False, "matched": True, "by_name": False,
                "wish_id": None, "mbid": "", "error": "", "note": "",
                "reason": "already in the library", "album": album})
            continue
        queued = _queue_row("album", album, artist, payload["service_label"],
                            row["url"], album=album)
        done[key] = bool(queued["queued"])
        report["parent_albums"]["queued"].append(queued)
        row["queued"] = "album" if queued["queued"] else ""


def _queue_unmatched_tracks(report, payload, index):
    """`playlist_import_unmatched: "wish"` — queue every unmatched track by
    name, unless the parent-album pass already queued the album it is on."""
    seen = set()
    for row in report["tracks"]:
        if row["matched"] or not row["title"]:
            continue
        if row["queued"] == "album":
            # The album this track is missing from is already on its way: the
            # track is covered, and queueing it by name as well would be two
            # searches for one missing song.
            continue
        key = (row["title"].strip().lower(), row["artist"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        queued = _queue_row("track", row["title"], row["artist"],
                            payload["service_label"], row["url"],
                            album=row["album"])
        report["unmatched_tracks"]["queued"].append(queued)
        row["queued"] = "wish" if queued["queued"] else ""
