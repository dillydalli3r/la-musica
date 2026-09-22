"""Multi-source lyrics lookup: LRCLIB, NetEase, QQ Music, Kuwo, Kugou,
YouTube captions.

The built-in order below is a ranking, and each step of it is a reason:

* ``lrclib``  — open, community-maintained, synced, no key, best global
  coverage: the only source here that is both open data and worldwide.
* ``netease`` — very large catalogue, synced + translations, strong for CJK.
* ``qq``      — synced + translations, strong mainstream coverage.
* ``kuwo``    — synced + translations.
* ``kugou``   — synced, weaker match quality than the four above.
* ``youtube`` — auto-generated captions, last resort, only when a video id is
  known (nothing is ever searched).

The first provider that answers wins, so this order IS the policy; a saved
``lyrics_sources`` list replaces it wholesale (see ``provider_order``).

SYNCED LYRICS ONLY. Every provider here answers with timestamps, and an answer
without them is no answer at all — see ``_accept``, the one gate every hit
passes before it is returned. ``lyrics_allow_plain`` (off by default) is the
explicit opt-in that lets LRCLIB's untimed text through.

Every source is free — no key, no paid tier anywhere — and stdlib urllib is all
they need; the captions one additionally needs yt-dlp and a video id the file
already carries. Every request is throttled to one per ``_MIN_GAP`` seconds and
retried on 429/5xx (these hosts throttle IPs), and each provider swallows its
own failures, so the chain always falls through to the next source:
``fetch_lyrics`` returns None — never raises — when nothing answers.

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
* Kugou — ``krcs.kugou.com/search?ver=1&man=yes&client=mobi&keyword=..[&duration=<ms>]``
  → ``candidates[]`` = ``{id, accesskey, singer, song, duration}`` (ms); then
  ``lyrics.kugou.com/download?ver=1&client=pc&id=..&accesskey=..&fmt=lrc&charset=utf8``
  → ``content`` = base64 of the LRC (``fmt``: "lrc"; it can also answer "krc",
  which is skipped). Verified with Slowdive/Alison.
* QQ Music — ``c.y.qq.com/soso/fcgi-bin/client_search_cp?w=..&format=json&new_json=1``
  (Referer ``https://y.qq.com/``) → ``data.song.list[]`` =
  ``{mid, name, interval, singer[].name, album.name}`` (interval in seconds);
  then ``c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid=..&format=json&nobase64=1``
  → ``lyric`` = synced LRC; ``trans`` is the translation and is left unused,
  exactly like NetEase's ``tlyric``. Verified: Slowdive/Alison → mid
  ``004WLmvD0bzutK``, 230 s, LRC starting ``[00:21.55]Listen close…``.
* Kuwo — ``search.kuwo.cn/r.s?all=..&ft=music&itemset=web_2013&client=kt&pn=0&rn=8&rformat=json&encoding=utf8``
  → ``abslist[]`` = ``{MUSICRID, SONGNAME, ARTIST, DURATION, ALBUM}`` (DURATION
  in seconds; the body is single-quoted and HTML-escaped, both fixed before
  parsing; the ``ver=kwplayer_ar_9.2.2.1`` client is what makes it rank the
  actual recording first, ``client=kt`` alone answers unrelated songs by the
  same artist); then ``m.kuwo.cn/newh5/singles/songinfoandlrc?musicId=..`` →
  ``data.lrclist[]`` = ``{lineLyric, time}`` with ``time`` in seconds
  ("32.29"), a translation repeating its line's timestamp, and cues padded
  with whitespace-only rows. The lyric module answers per id — some ids reply
  ``音乐查询失败`` — so a miss is normal and the chain walks on. Verified:
  Radiohead/Creep → MUSIC_1250107 with 80 cues (39 of them translation
  duplicates), while MUSIC_16996995 has none.
* YouTube — captions of a KNOWN video id, converted to the same LRC shape the
  other synced providers answer with. Nothing is ever searched: the id comes
  from a tag on the file or from the caller (the video download writes it into
  the file name, ``%(title)s [%(id)s].%(ext)s``, see ``server/youtube.py``),
  so a track without one has no YouTube provider at all. yt-dlp is optional —
  missing means one log line and a skip, never a failed fetch. Manual
  subtitles are preferred; automatic captions are the fallback and can
  mishear (that is stated in Settings, not hidden).
"""
import base64
import html
import json
import os
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
KUGOU_SEARCH = "https://krcs.kugou.com/search"
KUGOU_DOWNLOAD = "https://lyrics.kugou.com/download"
QQ_SEARCH = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
QQ_LYRIC = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
KUWO_SEARCH = "http://search.kuwo.cn/r.s"
# The "kwplayer" client is the one whose ranking puts the actual recording
# first; client=kt alone answers unrelated songs by the same artist.
_KUWO_CLIENT = "kwplayer_ar_9.2.2.1"
KUWO_LYRIC = "https://m.kuwo.cn/newh5/singles/songinfoandlrc"
_QQ_HEADERS = {"User-Agent": _DESKTOP_UA, "Referer": "https://y.qq.com/"}
_KUWO_HEADERS = {"User-Agent": _DESKTOP_UA,
                 "Referer": "https://m.kuwo.cn/"}

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
# Last failed request status ("403", 0 for a network error) — diagnostics for
# `probe_source` only, which is what turns "nothing came back" into "the host
# refused us". Whoever wrote it last wins: it labels one probe, nothing else.
last_http_error = None

# The built-in RANKING — see the module docstring for why each step sits where
# it does. `lyrics_sources` in config overrides it wholesale; this is the
# default and the order the settings list shows.
SOURCES = ["lrclib", "netease", "qq", "kuwo", "kugou", "youtube"]
SOURCE_LABELS = {
    "lrclib": "LRCLIB",
    "netease": "NetEase",
    "kugou": "Kugou",
    "qq": "QQ Music",
    "kuwo": "Kuwo",
    "youtube": "YouTube captions",
}
# One honest line per provider for the settings list and the setup wizard: the
# coverage it is good at, the caveats it comes with, and nothing else implied.
# Every one of them is a SYNCED source and every one is free; the four Chinese
# services are unofficial APIs (undocumented, can change or refuse), which is
# stated rather than hidden.
SOURCE_NOTES = {
    "lrclib": "Synced lyrics (timestamps) from the open, community-maintained "
              "database; no key needed, best global coverage of the six. An "
              "untimed record counts as no answer unless plain lyrics are "
              "switched on.",
    "netease": "Synced LRC (timestamps) with translations; a very large "
               "catalogue and the strongest of the six for CJK releases. "
               "Unofficial API.",
    "qq": "Synced LRC (timestamps) with translations; strong mainstream "
          "coverage. Unofficial API, and a loose search — the match score "
          "decides what is the track.",
    "kuwo": "Synced LRC (timestamps) with translations; a large catalogue. "
            "Unofficial API, and a loose search — the match score decides "
            "what is the track (originals only are written).",
    "kugou": "Synced LRC (timestamps); large catalogue but weaker match "
             "quality than the sources above. Unofficial API.",
    "youtube": "Synced captions of a KNOWN video id, auto-generated ones "
               "included — those can mishear. Only for tracks that carry a "
               "YouTube id; needs yt-dlp.",
}


_LOGGED = set()
_LOGGED_LOCK = threading.Lock()


def _log_once(pid, message):
    """One log line per provider per process.

    Something that cannot run — yt-dlp missing, a video id the track does not
    have — is a *skip*, not a failure, and it must be visible exactly once
    rather than repeated for every track of a run. Logging failures are
    swallowed: this is diagnostics, never load-bearing.
    """
    with _LOGGED_LOCK:
        if pid in _LOGGED:
            return
        _LOGGED.add(pid)
    try:
        from .ui import log
        log(f"{SOURCE_LABELS.get(pid, pid)}: {message}")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _request(url, headers=None, data=None, timeout=15, retries=3):
    """Rate-throttled request with retry on 429/5xx. Returns (status, body).

    The throttle lock only guards the timestamp bookkeeping: the request and
    the retry backoff happen OUTSIDE it, so one slow provider cannot block
    every other lyrics worker in the process (script 13, an import chain and a
    manual lookup all share this module). `data` turns the request into a POST
    — used by the LRCLIB submission, which needs its 201 and its error body.
    """
    global _last_request, last_http_error
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
            try:
                body = e.read()
            except Exception:
                body = b""
        except Exception:
            status = 0
        if status in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(2.0 * (attempt + 1))
            continue
        if status != 200:
            last_http_error = status or "network"
        return status, body
    return 0, b""


def _http(url, headers=None, data=None, timeout=15, retries=3):
    """Rate-throttled GET (or POST when ``data``). The 200 body, or None."""
    status, body = _request(url, headers=headers, data=data, timeout=timeout,
                            retries=retries)
    return body if status == 200 else None


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
    # default=: a hit that carries no artist at all scores the unknown-artist
    # weight. (The old `max([...] or _MIN_ARTIST)` raised TypeError on exactly
    # that case, which turned every artist-less hit into a provider error.)
    a = max((_name_score(n, artist) for n in names), default=_MIN_ARTIST)
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


def _variant_guard(want, got):
    """False when `got` names a different *kind* of recording than `want`.

    A karaoke/instrumental/demo/cover/tribute hit is another recording and may
    never be written as this track's lyrics — and a variant query never takes
    the original either (mirror of the rule `server.integrations` applies to
    every name-based match it makes). The marker list lives there, so it is
    imported lazily: mlo must not import server at module level.

    Never raises, and anything it cannot decide is allowed through — the guard
    is a hygiene filter, not the match itself.
    """
    try:
        from server.integrations import title_variant_kind
        a, b = title_variant_kind(want), title_variant_kind(got)
    except Exception:
        return True
    return not ((a or b) and a != b)


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


def lrclib_publish(artist, track, album, duration, plain=None, synced=None):
    """Submit lyrics to LRCLIB (POST /api/publish). Returns (ok, message).

    The one implementation of the submission: script 18 (auto-publishing for
    tracks LRCLIB does not have yet) and the manual "Publish to LRCLIB" panel
    (through server.integrations) both land here, so the request body, the
    required User-Agent and the error wording exist once. At least one of
    plain/synced must carry text; a synced text is best sent with its plain
    form beside it, which is what the script does."""
    artist = (artist or "").strip()
    track = (track or "").strip()
    album = (album or "").strip()
    plain = (plain or "").strip() or None
    synced = (synced or "").strip() or None
    if not artist or not track:
        return False, "artist and track name are required"
    if not plain and not synced:
        return False, "nothing to publish — no lyrics text"
    try:
        duration = int(round(float(duration or 0)))
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        return False, "track duration is required for publishing"
    params = urllib.parse.urlencode({
        "artist_name": artist, "track_name": track,
        "album_name": album or track, "duration": duration,
    })
    body = json.dumps({"plainLyrics": plain or "",
                       "syncedLyrics": synced or ""}).encode("utf-8")
    status, raw = _request(
        f"{LRCLIB_BASE}/publish?{params}",
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        data=body, timeout=20)
    if status in (200, 201):
        return True, "published to LRCLIB — thank you for contributing!"
    if status == 409:
        return False, "LRCLIB already has this track"
    if status == 429:
        return False, "LRCLIB is rate-limiting this IP — try again in a minute"
    detail = (raw or b"").decode("utf-8", "replace").strip()[:200]
    return False, f"LRCLIB refused ({status or 'no answer'}): {detail or 'unknown error'}"


def _lrclib(artist, title, album=None, duration=None, cfg=None, youtube_id=None):
    rec = lrclib_fetch(artist, title, album, duration)
    if not isinstance(rec, dict):
        return None
    return _hit(rec.get("syncedLyrics"), rec.get("plainLyrics"),
                rec.get("artistName") or artist, rec.get("trackName") or title,
                rec.get("albumName") or album, rec.get("duration"))


def _netease(artist, title, album=None, duration=None, cfg=None, youtube_id=None):
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


def _kugou(artist, title, album=None, duration=None, cfg=None, youtube_id=None):
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


def _qq(artist, title, album=None, duration=None, cfg=None, youtube_id=None):
    """QQ Music: search → lyric module. Synced LRC, free, no key."""
    query = urllib.parse.urlencode(
        {"w": f"{artist} {title}".strip(), "new_json": 1, "cr": 1, "p": 1,
         "n": 8, "format": "json"})
    data = _get_json(f"{QQ_SEARCH}?{query}", headers=_QQ_HEADERS)
    songs = (((data or {}).get("data") or {}).get("song") or {}).get("list")

    def artists_of(song):
        return [s.get("name") for s in (song.get("singer") or [])
                if isinstance(s, dict)]

    def album_of(song):
        alb = song.get("album") or {}
        return alb.get("name") or alb.get("title")

    rows = [s for s in (songs or []) if isinstance(s, dict) and s.get("mid")]
    # Only the top few get a lyric request: one per hit, and each is throttled.
    for song in _ranked(rows, artist, title, duration, artists_of,
                        lambda s: s.get("name"),
                        lambda s: s.get("interval"))[:3]:
        params = urllib.parse.urlencode({"format": "json", "nobase64": 1,
                                         "songmid": song["mid"]})
        lyr = _get_json(f"{QQ_LYRIC}?{params}", headers=_QQ_HEADERS)
        # `trans` holds the translation and is deliberately unused — the chain
        # writes the original text only, the rule NetEase's tlyric follows.
        synced = ((lyr or {}).get("lyric") or "").strip()
        if not synced:
            continue
        return _hit(synced, _lrc_to_plain(synced),
                    ", ".join(n for n in artists_of(song) if n) or artist,
                    song.get("name") or title, album_of(song),
                    song.get("interval"))
    return None


def _kuwo_text(value):
    """Kuwo escapes its own tags ("An&apos;&nbsp;Clear") — decode, collapse."""
    return " ".join(html.unescape(str(value or "")).split())


def _kuwo_json(url):
    """Kuwo's JSON, whichever dialect it answered with.

    The search endpoint wraps keys AND values in single quotes, so it is only
    valid after the quote repair; the lyric endpoint answers proper JSON, whose
    values may hold a real apostrophe ("We're rotten fruit") that the repair
    would corrupt — hence strict first, repair second.
    """
    body = _http(url, headers=_KUWO_HEADERS)
    if not body:
        return None
    text = body.decode("utf-8", "replace").lstrip()
    for candidate in (text, text.replace("'", '"')):
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _kuwo_lrc(rows):
    """Kuwo's ``lrclist`` → LRC ``[mm:ss.xx]line``, its cue shape.

    A translation repeats its line's timestamp, and the chain writes originals
    only — the rule NetEase's ``tlyric`` follows. Kuwo flags neither of them,
    but it does keep one script per line, so the line with the most Latin
    letters wins its timestamp.

    ponytail: a CJK song whose translation is in Latin script would keep the
    translation; nothing else here can tell the two apart.
    """
    groups = []                                     # [(seconds, [lines])]
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            seconds = float(row.get("time"))
        except (TypeError, ValueError):
            continue
        text = " ".join(str(row.get("lineLyric") or "").split())
        if not text:
            continue                                # Kuwo pads cues with "   "
        if groups and groups[-1][0] == seconds:
            groups[-1][1].append(text)
        else:
            groups.append((seconds, [text]))
    out = []
    for seconds, lines in groups:
        text = max(lines, key=lambda line: sum(ch.isascii() and ch.isalpha()
                                               for ch in line))
        minutes, rest = divmod(seconds, 60)
        out.append(f"[{int(minutes):02d}:{rest:05.2f}]{text}")
    return "\n".join(out)


def _kuwo(artist, title, album=None, duration=None, cfg=None, youtube_id=None):
    """Kuwo: search → ``songinfoandlrc``. Synced LRC, free, no key."""
    query = urllib.parse.urlencode(
        {"all": f"{artist} {title}".strip(), "ft": "music", "client": "kt",
         "pn": 0, "rn": 8, "rformat": "json", "encoding": "utf8",
         "ver": _KUWO_CLIENT, "vipver": "1", "show_copyright_off": "1"})
    data = _kuwo_json(f"{KUWO_SEARCH}?{query}")
    rows = [r for r in ((data or {}).get("abslist") or [])
            if isinstance(r, dict)]
    for row in _ranked(rows, artist, title, duration,
                       lambda r: [_kuwo_text(r.get("ARTIST"))],
                       lambda r: _kuwo_text(r.get("SONGNAME")),
                       lambda r: r.get("DURATION"))[:3]:
        music_id = str(row.get("MUSICRID") or "").replace("MUSIC_", "").strip()
        if not music_id:
            continue
        params = urllib.parse.urlencode({"musicId": music_id})
        lyr = _kuwo_json(f"{KUWO_LYRIC}?{params}")
        synced = _kuwo_lrc(((lyr or {}).get("data") or {}).get("lrclist"))
        if not synced:
            continue
        return _hit(synced, _lrc_to_plain(synced),
                    _kuwo_text(row.get("ARTIST")) or artist,
                    _kuwo_text(row.get("SONGNAME")) or title,
                    _kuwo_text(row.get("ALBUM")), row.get("DURATION"))
    return None


_YT_URL_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|embed/|shorts/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})", re.I)
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# ponytail: the video download leaves "[<id>]" in the file name, so a bracketed
# 11-char token is read as one — an unrelated tag of exactly that length would
# cost one failed yt-dlp run, nothing worse.
_YT_BRACKET_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
# MM:SS.mmm / HH:MM:SS,mmm (VTT and SRT cues), at the start of a line.
_CUE_TIME_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->")
_CUE_TAG_RE = re.compile(r"<[^>]*>")
_YT_CAPTION_TIMEOUT = 120


def youtube_id_from(*values):
    """The 11-char video id in a YouTube URL, a bare id, or ``[<id>]``, else None.

    Values are checked in order. The bracket form is the one the app's own
    video downloads leave in the file name (``%(title)s [%(id)s].%(ext)s``),
    and anything else — a Bandcamp link in a URL tag, a shorter bracketed tag —
    answers None instead of a hopeful guess.
    """
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        for pattern in (_YT_URL_RE, _YT_BRACKET_RE):
            match = pattern.search(text)
            if match:
                return match.group(1)
        if _YT_ID_RE.match(text):
            return text
    return None


def _cue_seconds(match):
    hours = int(match.group(1) or 0)
    milli = int(match.group(4).ljust(3, "0"))
    return hours * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + milli / 1000.0


def _vtt_to_lrc(captions):
    """Caption track (VTT or SRT) → LRC: ``[mm:ss.xx]text`` per cue.

    The shape is the one NetEase and Kugou answer with, so the writer and the
    ELRC path need no special case. Inline cue tags (``<c>``, karaoke timing)
    and the ``align:``/``position:`` cue settings are dropped.

    ponytail: repeated cue text is only dropped, never reconstructed — the
    rolling windows of auto captions therefore stay as YouTube worded and
    timed them, and nothing is invented from them.
    """
    cues, seen, start, lines = [], set(), None, []

    def flush():
        if start is None:
            return
        text = " ".join(" ".join(lines).split())
        if text and text not in seen:
            seen.add(text)
            cues.append((start, text))

    lines_in = (captions or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, raw in enumerate(lines_in):
        line = raw.strip()
        match = _CUE_TIME_RE.match(line)
        if match:
            flush()
            start, lines = _cue_seconds(match), []
        elif start is not None and line and not line.startswith("NOTE"):
            following = lines_in[index + 1].strip() if index + 1 < len(lines_in) else ""
            if line.isdigit() and _CUE_TIME_RE.match(following):
                continue          # the cue number SRT puts before its timing
            # tags become spaces: karaoke cues separate their words with them
            lines.append(_CUE_TAG_RE.sub(" ", line))
        # everything before the first cue is the WEBVTT header / Kind / Language
    flush()

    out = []
    for seconds, text in cues:
        minutes, rest = divmod(seconds, 60)
        out.append(f"[{int(minutes):02d}:{rest:05.2f}]{text}")
    return "\n".join(out)


def _caption_files(exe, video_id, tmpdir, flags):
    """Run yt-dlp for one caption flavour; the files it wrote (often none)."""
    from .subproc import run_tool
    cmd = [exe, "--skip-download", "--no-playlist", "--no-warnings",
           "--no-progress", "--sub-format", "vtt", "--sub-langs", "en.*,en",
           "-o", os.path.join(tmpdir, "%(id)s"), *flags,
           f"https://www.youtube.com/watch?v={video_id}"]
    try:
        run_tool(cmd, capture_output=True, text=True,
                 timeout=_YT_CAPTION_TIMEOUT)
    except Exception:
        return []
    try:
        return sorted(os.path.join(tmpdir, name) for name in os.listdir(tmpdir)
                      if name.lower().endswith((".vtt", ".srt")))
    except Exception:
        return []


def _youtube_captions(exe, video_id, cookie_flags=()):
    """Time-synced captions for one video id, or None.

    Manual subtitles first — those are the ones a person typed; automatic ones
    are the (often misheard) fallback. yt-dlp writes into a temp dir that is
    removed again, so a fetch leaves nothing behind.

    *cookie_flags* are yt-dlp's own `--cookies …` arguments when the user
    configured a jar (see `_cookie_flags`): a video old enough to be age-gated
    has captions too, and yt-dlp will not see them without the same cookies the
    video download needs.
    """
    import tempfile
    with tempfile.TemporaryDirectory(prefix="mlo-yt-captions-") as tmp:
        paths = _caption_files(exe, video_id, tmp,
                               ("--write-subs", *cookie_flags))
        if not paths:
            paths = _caption_files(exe, video_id, tmp,
                                   ("--write-subs", "--write-auto-subs",
                                    *cookie_flags))
        for path in sorted(paths,
                           key=lambda p: (".en." not in os.path.basename(p).lower(), p)):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    lrc = _vtt_to_lrc(fh.read())
            except Exception:
                continue
            if lrc:
                return lrc
    return None


def _cookie_flags(cfg):
    """yt-dlp's `--cookies …` arguments for *cfg*, or [] when none is set.

    The captions fetch is the THIRD yt-dlp call site — the search and the video
    download live in `server/youtube.py`, which owns the setting — and it needs
    the same jar for the same reason: an age-gated video is exactly the kind
    that has captions and no anonymous access. Imported lazily because `mlo/`
    runs without `server/` in the CLI; a checkout that never configured cookies
    loses nothing by that import failing.
    """
    try:
        from server.youtube import cookie_args
        return list(cookie_args(cfg or {}))
    except Exception:
        return []


def _youtube(artist, title, album=None, duration=None, cfg=None, youtube_id=None):
    """Captions of a KNOWN YouTube video, as synced LRC.

    ``youtube_id`` is the only way in: a blind YouTube search would happily
    attach a stranger's captions to the file. Callers pass the id the download
    recorded (a tag, or the ``[<id>]`` in the file name the app itself writes),
    so a track without one simply has no YouTube provider.
    """
    if not youtube_id:
        return None
    if not bool((cfg or {}).get("lyrics_youtube_captions", True)):
        return None
    exe = _ytdlp_exe()
    if not exe:
        _log_once("youtube", "yt-dlp is not installed — skipped")
        return None
    synced = _youtube_captions(exe, youtube_id, _cookie_flags(cfg))
    if not synced:
        return None
    return _hit(synced, _lrc_to_plain(synced), artist, title, None, None)


def _ytdlp_exe():
    """The yt-dlp executable (vendored .dependencies first, then PATH) or None."""
    try:
        from .tools import detect_all_tools
        return (detect_all_tools().get("yt-dlp") or {}).get("ytdlp_exe")
    except Exception:
        return None


_PROVIDERS = {
    "lrclib": _lrclib,
    "netease": _netease,
    "kugou": _kugou,
    "qq": _qq,
    "kuwo": _kuwo,
    "youtube": _youtube,
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
    """The providers, in built-in RANK order, for the settings UI and wizard.

    Every entry is self-describing, and every one of them is true by
    construction: all of these sources answer with timestamps (``synced``),
    all of them are free with no paid tier (``free``), and ``needs`` lists the
    config keys a provider requires — empty for every one of them today (the
    captions provider needs yt-dlp *installed*, not configured).

    ``rank`` is 1-based over ``SOURCES``: the built-in preference order, which
    is what the settings list shows as "#1 preferred". A saved
    ``lyrics_sources`` list replaces the order without changing these ranks —
    the rank says what the DEFAULT chain thinks of a provider, not what the
    user's own list does.
    """
    return [{"id": pid, "kind": "lyrics", "label": SOURCE_LABELS[pid],
             "synced": True, "free": True, "needs": [],
             "rank": n, "notes": SOURCE_NOTES[pid]}
            for n, pid in enumerate(SOURCES, 1)]


# The one fixed sample every probe uses, so two runs are comparable. Radiohead
# / Creep is on all six sources (verified live), duration in seconds.
PROBE_SAMPLE = ("Radiohead", "Creep", "Pablo Honey", 238.0)


def probe_source(pid, cfg=None):
    """One cheap lookup of the sample track → ``{id, kind, status, detail, ms}``.

    ``status``: "ok" when synced lyrics came back, "skipped" when this machine
    cannot run the provider at all (yt-dlp missing, no video id to probe, the
    host refusing us), "fail" when it ran and had nothing. Never raises and
    never writes anything — the wizard calls it once per provider.
    """
    global last_http_error
    result = {"id": pid, "kind": "lyrics", "status": "fail", "detail": "",
              "ms": 0}
    provider = _PROVIDERS.get(pid)
    if provider is None:
        result.update(status="skipped", detail="unknown provider")
        return result
    started = time.time()
    try:
        if pid == "youtube":
            # Captions are per-track by design: there is no sample to probe
            # without a video id, and nothing here ever searches YouTube.
            if not _ytdlp_exe():
                result.update(status="skipped",
                              detail="yt-dlp is not installed")
            else:
                result.update(status="skipped",
                              detail="needs a track's YouTube id")
            return result
        last_http_error = None
        artist, title, album, duration = PROBE_SAMPLE
        hit = provider(artist, title, album, duration, cfg or {}, None)
    except Exception as e:
        hit = None
        result.update(detail=f"raised: {e}")
    finally:
        result["ms"] = int((time.time() - started) * 1000)
    if isinstance(hit, dict) and hit.get("synced"):
        result.update(status="ok",
                      detail="synced lyrics, %d lines"
                             % len(hit["synced"].splitlines()))
    elif isinstance(hit, dict) and hit.get("plain"):
        result.update(detail="plain only — rejected (synced lyrics only)")
    elif last_http_error:
        result.update(status="skipped", detail=f"host refused ({last_http_error})")
    elif not result["detail"]:
        result.update(detail="no lyrics for the sample")
    return result


def _accept(hit, allow_plain):
    """THE gate every provider answer passes: is this a usable hit?

    Synced lyrics only, by policy: an answer without timestamps is no answer at
    all — not a lower-priority answer — unless ``lyrics_allow_plain`` is
    explicitly on, which is the one opt-in that lets untimed text through.
    """
    if not isinstance(hit, dict):
        return False
    synced = str(hit.get("synced") or "").strip()
    plain = str(hit.get("plain") or "").strip()
    if not (synced or plain):
        return False
    return bool(synced) or bool(allow_plain)


def hit_score(hit, artist, title, duration):
    """0..1 confidence that *hit* is the track that was asked for.

    The same measure the candidate ranking uses (_match_score), re-run on the
    winning hit so a caller can refuse a weak match instead of writing it —
    automatic fetching does (see mlo/lyrics_fetch)."""
    try:
        return _match_score([hit.get("matched_artist")], hit.get("matched_title"),
                            hit.get("duration"), artist, title, duration)
    except Exception:
        return 0.0


def fetch_lyrics(cfg, artist, title, album=None, duration=None, allow_plain=None,
                 youtube_id=None, min_score=None):
    """First provider hit for a track, or None when none of them has it.

    ``allow_plain`` (default from ``cfg["lyrics_allow_plain"]``, False) is the
    opt-in for untimed lyrics: off — the default — a plain-only answer is
    rejected outright and the chain walks on to the next synced source.
    ``min_score`` raises the bar above the search floor (_MIN_SCORE): a hit
    below it is skipped and the next provider is tried, which is how the
    automatic write path only accepts lyrics it is confident about.
    ``youtube_id`` (optional) is the video the file came from — the only thing
    that lets the YouTube provider answer, since it never searches. Providers
    never raise — a failure, a timeout or a parse error is just a miss."""
    cfg = cfg or {}
    if allow_plain is None:
        allow_plain = bool(cfg.get("lyrics_allow_plain", False))
    if not (artist and title):
        return None
    floor = _MIN_SCORE if min_score is None else max(_MIN_SCORE, float(min_score))
    for pid in provider_order(cfg):
        try:
            hit = _PROVIDERS[pid](artist, title, album, duration, cfg,
                                  youtube_id)
        except Exception:
            hit = None
        if not _accept(hit, allow_plain):
            continue
        # A karaoke/instrumental/cover hit is another recording: reject it and
        # let the next provider answer (the query may be a variant itself).
        if hit.get("matched_title") and not _variant_guard(title,
                                                          hit["matched_title"]):
            continue
        score = hit_score(hit, artist, title, duration)
        if score < floor:
            continue
        hit = dict(hit)
        hit["provider"] = pid
        hit["provider_label"] = SOURCE_LABELS[pid]
        hit["score"] = round(score, 3)
        return hit
    return None
