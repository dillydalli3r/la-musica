"""Multi-source lyrics lookup: LRCLIB, NetEase, lyrics.ovh, Kugou.

Stdlib urllib only. Every request is throttled to one per ``_MIN_GAP``
seconds and retried on 429/5xx (these hosts throttle IPs), and each provider
swallows its own failures, so the chain always falls through to the next
source: ``fetch_lyrics`` returns None — never raises — when nothing answers.

Request shapes below were verified live (2026-09, non-CN IP) so the payload
keys stayed pinned instead of guessed:

* LRCLIB — ``/api/get?artist_name=..&track_name=..[&album_name=..]`` and
  ``/api/search?track_name=..&artist_name=..``; record keys ``syncedLyrics``,
  ``plainLyrics``, ``instrumental``, ``duration`` (seconds).
* NetEase — the documented ``/api/search/get/web`` answers an ENCRYPTED hex
  blob to non-CN callers (``{"code": 200, "abroad": true, "result":
  "35b17489…"}``), so the plain ``/api/search/get`` endpoint is used instead.
  GET (Referer ``https://music.163.com/`` + desktop UA + ``Cookie:
  appver=2.0.2; os=pc``) →
  ``result.songs[]`` = ``{id, name, artists[].name, album.name, duration}``
  (duration in ms); then ``/api/song/lyric?id=<id>&lv=1&kv=1&tv=-1`` →
  ``lrc.lyric`` (synced LRC, "" when the track has none), ``tlyric.lyric``
  (translation, often ""), ``nolyric``/``uncollected`` flags.
  Verified: artist "Slowdive" / title "Alison" → id 4281481, 230295 ms,
  album "Souvlaki", synced LRC.
* lyrics.ovh — ``/v1/<artist>/<title>`` → ``{"lyrics": str}``, plain only
  (404 when unknown). Verified with Slowdive/Alison.
* Kugou — ``krcs.kugou.com/search?ver=1&man=yes&client=mobi&keyword=..[&duration=<ms>]``
  → ``candidates[]`` = ``{id, accesskey, singer, song, duration}`` (ms); then
  ``lyrics.kugou.com/download?ver=1&client=pc&id=..&accesskey=..&fmt=lrc&charset=utf8``
  → ``content`` = base64 of the LRC (``fmt``: "lrc"; it can also answer "krc",
  which is skipped). Verified with Slowdive/Alison.
"""
import base64
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "MusicLibraryOptimizer/2 (la musica)"
_DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

LRCLIB_BASE = "https://lrclib.net/api"
NETEASE_BASE = "https://music.163.com/api"
OVH_BASE = "https://api.lyrics.ovh/v1"
KUGOU_SEARCH = "https://krcs.kugou.com/search"
KUGOU_DOWNLOAD = "https://lyrics.kugou.com/download"

_NETEASE_HEADERS = {
    "User-Agent": _DESKTOP_UA,
    "Referer": "https://music.163.com/",
    "Accept": "application/json",
    "Cookie": "appver=2.0.2; os=pc",
}
_KUGOU_HEADERS = {"User-Agent": _DESKTOP_UA, "Referer": "https://www.kugou.com/"}

_MIN_GAP = 0.4
_throttle_lock = threading.Lock()
_last_request = 0.0

SOURCES = ["lrclib", "netease", "lyricsovh", "kugou"]
SOURCE_LABELS = {
    "lrclib": "LRCLIB",
    "netease": "NetEase",
    "lyricsovh": "lyrics.ovh",
    "kugou": "Kugou",
}
SOURCE_NOTES = {
    "lrclib": "Synced + plain lyrics, community-run, no key needed.",
    "netease": "Cloud Music: synced LRC, best coverage for CJK releases.",
    "lyricsovh": "Plain lyrics only, no timestamps.",
    "kugou": "Synced LRC, strong for CJK and mainstream pop.",
}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _http(url, headers=None, data=None, timeout=15, retries=3):
    """Rate-throttled GET (or POST when ``data``) with retry on 429/5xx.
    Returns the response body, or None for any error / non-200.

    The throttle lock only guards the timestamp bookkeeping: the request and
    the retry backoff happen OUTSIDE it, so one slow provider cannot block
    every other lyrics worker in the process (script 13, an import chain and a
    manual lookup all share this module).
    """
    global _last_request
    for attempt in range(retries):
        with _throttle_lock:
            elapsed = time.time() - _last_request
            wait = _MIN_GAP - elapsed
            if wait > 0:
                time.sleep(wait)
            _last_request = time.time()
        status, body = 0, b""
        try:
            req = urllib.request.Request(
                url, data=data,
                headers=headers or {"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status, body = r.status, r.read()
        except urllib.error.HTTPError as e:
            status = e.code
        except Exception:
            status = 0
        if status in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(2.0 * (attempt + 1))
            continue
        return body if status == 200 else None
    return None


def _get_json(url, headers=None, timeout=15, retries=3):
    body = _http(url, headers=headers, timeout=timeout, retries=retries)
    if body is None:
        return None
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_MIN_SCORE = 0.6
_MIN_ARTIST = 0.4


def _norm(text):
    """Comparison key: casefolded, accent-folded, punctuation-free."""
    s = unicodedata.normalize("NFKD", str(text or "")).casefold()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(_PUNCT_RE.sub(" ", s).split())


def _name_score(hit, want):
    """1.0 identical, 0.8 when one extends the other ("Alison (Remaster)"),
    else the token-overlap ratio. 0.0 when either side is empty."""
    a, b = _norm(hit), _norm(want)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a.startswith(b) or b.startswith(a):
        return 0.8
    ta, tb = set(a.split()), set(b.split())
    return len(ta & tb) / len(ta | tb)


def _match_score(hit_artists, hit_title, hit_duration, artist, title, duration):
    """0..1 confidence that a provider hit is the wanted track. The title
    counts most, the artist is a gate (a namesake cover with the same title
    is the usual false positive), and a duration more than 20s off kills the
    hit outright. A hit whose artist is unknown still counts."""
    t = _name_score(hit_title, title)
    if t <= 0:
        return 0.0
    names = [n for n in (hit_artists or []) if n]
    a = max([_name_score(n, artist) for n in names] or _MIN_ARTIST)
    if a < _MIN_ARTIST:
        return 0.0
    score = 0.65 * t + 0.35 * min(a, 1.0)
    if duration and hit_duration:
        delta = abs(float(hit_duration) - float(duration))
        if delta > 20:
            return 0.0
        score += 0.15 * (1.0 - delta / 20.0)
    return score


def _ranked(rows, artist, title, duration, artist_of, title_of, duration_of):
    """Candidate rows ordered best-first, everything below _MIN_SCORE dropped."""
    scored = []
    for row in rows or []:
        score = _match_score(artist_of(row), title_of(row), duration_of(row),
                             artist, title, duration)
        if score >= _MIN_SCORE:
            scored.append((score, row))
    scored.sort(key=lambda item: -item[0])
    return [row for _score, row in scored]


_TS_RE = re.compile(r"\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]")
_META_RE = re.compile(r"^\[[a-zA-Z#][^\]]*\]$")


def _lrc_to_plain(lrc):
    """Unsynced view of an LRC: timestamps and metadata tags removed."""
    out = []
    for line in (lrc or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = _TS_RE.sub("", line).strip()
        if s and not _META_RE.match(s):
            out.append(s)
    return "\n".join(out)


def _hit(synced, plain, matched_artist, matched_title, matched_album=None,
         duration=None, instrumental=False):
    return {
        "synced": (synced or "").strip() or None,
        "plain": (plain or "").strip() or None,
        "instrumental": bool(instrumental),
        "duration": float(duration) if duration else None,
        "matched_artist": matched_artist,
        "matched_title": matched_title,
        "matched_album": matched_album or None,
    }


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
def _lrclib_get(endpoint, params, timeout=15, retries=3):
    """Rate-throttled LRCLIB GET. Decoded JSON body, or None."""
    url = f"{LRCLIB_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    return _get_json(url, timeout=timeout, retries=retries)


def lrclib_fetch(artist, track, album=None, duration=None):
    """Best LRCLIB record for a track: exact /get lookup first, then a
    /search fallback preferring synced lyrics and the closest duration.
    Returns the record dict (syncedLyrics / plainLyrics) or None."""
    params = {"artist_name": artist, "track_name": track}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = int(round(duration))
    rec = _lrclib_get("get", params)
    if isinstance(rec, dict) and (rec.get("syncedLyrics") or rec.get("plainLyrics")):
        return rec
    # The exact lookup is strict — retry as a search, without the album
    # filter first (it can hurt matches).
    for album_filter in dict.fromkeys((None, album)):
        search = {"track_name": track, "artist_name": artist}
        if album_filter:
            search["album_name"] = album_filter
        hits = _lrclib_get("search", search)
        if isinstance(hits, list) and hits:
            synced = [h for h in hits if h.get("syncedLyrics")]
            pool = synced or hits
            if duration:
                pool = sorted(pool, key=lambda h: abs(int(h.get("duration") or 0) - int(duration)))
            return pool[0]
    return None


def _lrclib(artist, title, album=None, duration=None):
    rec = lrclib_fetch(artist, title, album, duration)
    if not isinstance(rec, dict):
        return None
    return _hit(rec.get("syncedLyrics"), rec.get("plainLyrics"),
                rec.get("artistName") or artist, rec.get("trackName") or title,
                rec.get("albumName") or album, rec.get("duration"))


def _netease(artist, title, album=None, duration=None):
    query = urllib.parse.urlencode(
        {"s": f"{artist} {title}".strip(), "type": 1, "limit": 8, "offset": 0})
    data = _get_json(f"{NETEASE_BASE}/search/get?{query}", headers=_NETEASE_HEADERS)
    result = (data or {}).get("result")
    songs = result.get("songs") if isinstance(result, dict) else None

    def artists_of(song):
        return [a.get("name") for a in (song.get("artists") or [])
                if isinstance(a, dict)]

    def ms_of(song):
        return (song.get("duration") or 0) / 1000.0

    rows = [s for s in (songs or []) if isinstance(s, dict) and s.get("id")]
    # Only the top few get a lyric request: one per hit, and each is throttled.
    for song in _ranked(rows, artist, title, duration, artists_of,
                        lambda s: s.get("name"), ms_of)[:3]:
        lyr = _get_json(
            f"{NETEASE_BASE}/song/lyric?id={song['id']}&lv=1&kv=1&tv=-1",
            headers=_NETEASE_HEADERS)
        if not isinstance(lyr, dict):
            continue
        synced = ((lyr.get("lrc") or {}).get("lyric") or "").strip()
        if not synced:
            continue
        return _hit(synced, _lrc_to_plain(synced),
                    ", ".join(n for n in artists_of(song) if n) or artist,
                    song.get("name") or title,
                    (song.get("album") or {}).get("name"),
                    ms_of(song))
    return None


def _lyricsovh(artist, title, album=None, duration=None):
    url = "{}/{}/{}".format(
        OVH_BASE,
        urllib.parse.quote(str(artist or "").strip(), safe=""),
        urllib.parse.quote(str(title or "").strip(), safe=""),
    )
    data = _get_json(url)
    text = data.get("lyrics") if isinstance(data, dict) else None
    if not (text or "").strip():
        return None
    return _hit(None, text, artist, title, None, duration)


def _kugou(artist, title, album=None, duration=None):
    params = {"ver": 1, "man": "yes", "client": "mobi",
              "keyword": f"{artist} - {title}".strip()}
    if duration:
        params["duration"] = int(round(duration * 1000))
    data = _get_json(f"{KUGOU_SEARCH}?{urllib.parse.urlencode(params)}",
                     headers=_KUGOU_HEADERS)
    cands = (data or {}).get("candidates")
    rows = [c for c in (cands or [])
            if isinstance(c, dict) and c.get("id") and c.get("accesskey")]

    def ms_of(cand):
        return (cand.get("duration") or 0) / 1000.0

    for cand in _ranked(rows, artist, title, duration,
                        lambda c: [c.get("singer")], lambda c: c.get("song"),
                        ms_of)[:3]:
        dl = _get_json(f"{KUGOU_DOWNLOAD}?{urllib.parse.urlencode({
            'ver': 1, 'client': 'pc', 'id': cand['id'],
            'accesskey': cand['accesskey'], 'fmt': 'lrc', 'charset': 'utf8'})}",
            headers=_KUGOU_HEADERS)
        if not isinstance(dl, dict) or dl.get("fmt") != "lrc" or not dl.get("content"):
            continue
        try:
            synced = base64.b64decode(dl["content"]).decode("utf-8", "replace").strip()
        except Exception:
            continue
        if not synced:
            continue
        return _hit(synced, _lrc_to_plain(synced), cand.get("singer") or artist,
                    cand.get("song") or title, None, ms_of(cand))
    return None


_PROVIDERS = {
    "lrclib": _lrclib,
    "netease": _netease,
    "lyricsovh": _lyricsovh,
    "kugou": _kugou,
}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def provider_order(cfg=None):
    """The provider ids to try, in order. ``cfg["lyrics_sources"]`` wins when
    it names any known id (order preserved, unknown ids dropped); otherwise
    the built-in order."""
    cfg = cfg or {}
    wanted = cfg.get("lyrics_sources")
    if isinstance(wanted, str):
        wanted = [wanted]
    order, seen = [], set()
    for raw in wanted if isinstance(wanted, (list, tuple)) else ():
        pid = str(raw or "").strip().lower()
        if pid in _PROVIDERS and pid not in seen:
            seen.add(pid)
            order.append(pid)
    return order or list(SOURCES)


def available_sources():
    """The providers, in built-in order, for the settings UI."""
    return [{"id": pid, "label": SOURCE_LABELS[pid], "notes": SOURCE_NOTES[pid]}
            for pid in SOURCES]


def fetch_lyrics(cfg, artist, title, album=None, duration=None, allow_plain=None):
    """First provider hit for a track, or None when none of them has it.

    ``allow_plain`` (default from ``cfg["lyrics_allow_plain"]``, True) off
    means synced or nothing: a provider that only has plain lyrics is skipped
    and the chain moves on. Providers never raise — a failure, a timeout or a
    parse error is just a miss."""
    cfg = cfg or {}
    if allow_plain is None:
        allow_plain = bool(cfg.get("lyrics_allow_plain", True))
    if not (artist and title):
        return None
    for pid in provider_order(cfg):
        try:
            hit = _PROVIDERS[pid](artist, title, album, duration)
        except Exception:
            hit = None
        if not isinstance(hit, dict):
            continue
        if not (hit.get("synced") or hit.get("plain")):
            continue
        if not hit.get("synced") and not allow_plain:
            continue
        hit = dict(hit)
        hit["provider"] = pid
        hit["provider_label"] = SOURCE_LABELS[pid]
        return hit
    return None
