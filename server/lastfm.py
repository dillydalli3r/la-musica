"""Last.fm scrobbling — the ONE seam every write to Last.fm goes through.

Reading Last.fm (charts, tags, biographies) lives in ``server/discovery.py``
and needs nothing but the API key. WRITING to it — a now-playing notice and a
scrobble — needs an authenticated session, and an authenticated call has to be
SIGNED: ``api_sig = md5(<every parameter except format/callback, sorted by
name, each as name+value, concatenated><shared secret>)``. Getting that wrong
is silent (Last.fm answers ``error 13: Invalid method signature``), so the
signer here is pinned against the API docs' own worked example in
``tools/test_lastfm.py``.

The two calls this module owns for playback:

    track.updateNowPlaying  what is playing right now (artist/track/…)
    track.scrobble          ONE play, at the moment it STARTED (timestamp),
                            counted once; Last.fm accepts up to 50 per call,
                            which is what the spool's flush uses.

A scrobble is only ever sent when Last.fm's own rule is met — the track is at
least 30 s long AND the play reached max(50 % of its length, 4 minutes),
whichever comes first — computed from the FILE's own duration and the seconds
the client reported playing, never guessed (see ``meets_rule``). A scrobble
the service or the network refuses is written to a spool and re-sent later
(``queue_scrobble`` / ``flush_spool``), so a restart or a short outage costs a
scrobble nothing. NOTHING is ever sent without a session key and
``lastfm_scrobble_enabled`` — ``_guard`` is the only door.

This module never imports ``server.main``; ``server/api_lastfm.py`` is its HTTP
surface and ``server/credential_checks.py`` reads ``verify_session``.
"""
import hashlib
import json
import os
import threading
from typing import Dict, List, Optional, Tuple

# Last.fm's single API host — the same one ``server/discovery.py`` throttles as
# `_LASTFM_HOST`, spelled here for the write side.
API_URL = "https://ws.audioscrobbler.com/2.0/"
_API_HOST = "ws.audioscrobbler.com"

# Where the user authorizes a request token. The token is what auth.getSession
# then exchanges for the session key (a token is consumed once).
AUTH_URL = "https://www.last.fm/api/auth/"

# Last.fm's own scrobble rule, as numbers: a track shorter than this cannot be
# scrobbled at all, and the play has to reach `lastfm_scrobble_percent` of the
# track (default 70) — a stricter bar than the service's own 50 %, which is
# the FLOOR the config clamps to: Last.fm discards a submission under its own
# rule, so letting the setting go below it would only file scrobbles nobody
# counts. The 4-minute cap the rule used to carry is gone with the owner's
# call ("scrobbling … should only occur if a certain amount of the song is
# listened to"): a percentage is what "listened to" means, and a fixed ceiling
# made a 10-minute track scrobble at 40 % while a 3-minute one needed 80 %.
MIN_TRACK_SECONDS = 30
MIN_PERCENT = 50
MAX_PERCENT = 100
DEFAULT_PERCENT = 70

# One `track.scrobble` call carries at most this many plays (Last.fm's own
# ceiling); the flush sends the queue in slices of this size.
BATCH = 50

# How many queued scrobbles the spool may hold. honey: the ceiling exists so a
# long outage cannot grow the file without end — Last.fm refuses a scrobble
# older than 14 days anyway, so the OLDEST entries are dropped first and the
# newest plays are the ones kept. Reaching it is announced through `status`'s
# `queued`, so it is visible rather than silent.
SPOOL_MAX = 5000

# A transport failure must not leave a request hanging on the playback path.
DEFAULT_TIMEOUT_S = 10.0

# The one spool file, under the app's own state dir (<music>/.mlo/data).
SPOOL_NAME = "lastfm_scrobbles.jsonl"


# --------------------------------------------------------------------------- #
# config helpers
# --------------------------------------------------------------------------- #
def _cfg(cfg: Optional[dict]) -> dict:
    if cfg is not None:
        return cfg or {}
    from mlo import load_config
    return load_config() or {}


def _text(cfg: dict, key: str) -> str:
    return str((cfg or {}).get(key) or "").strip()


def api_key(cfg=None) -> str:
    return _text(_cfg(cfg), "lastfm_api_key")


def api_secret(cfg=None) -> str:
    return _text(_cfg(cfg), "lastfm_api_secret")


def session_key(cfg=None) -> str:
    return _text(_cfg(cfg), "lastfm_session_key")


def username(cfg=None) -> str:
    return _text(_cfg(cfg), "lastfm_username")


def enabled(cfg=None) -> bool:
    """The switch that gates EVERY send — a saved session is not consent."""
    return bool(_cfg(cfg).get("lastfm_scrobble_enabled", True))


def connected(cfg=None) -> bool:
    """A session key is what "connected" means: the one-time authorization."""
    return bool(session_key(cfg))


# --------------------------------------------------------------------------- #
# signing
# --------------------------------------------------------------------------- #
def sign(params: dict, secret: str) -> str:
    """The documented ``api_sig`` for *params* under *secret*.

    Every parameter EXCEPT ``format`` and ``callback`` (the two Last.fm never
    signs) and ``api_sig`` itself, sorted by name, each written as name+value
    with no separator, the shared secret appended, md5'd as lower-case hex.
    Values are stringified as Last.fm receives them (the timestamp is an int in
    the params dict, not a float), and the bytes are UTF-8 — the two things the
    docs' worked example turns on.
    """
    parts = []
    for name in sorted(params):
        if name in ("format", "callback", "api_sig"):
            continue
        parts.append(f"{name}{params[name]}")
    raw = "".join(parts) + str(secret or "")
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# transport — the ONE seam tests stub
# --------------------------------------------------------------------------- #
class LastfmError(Exception):
    """A refusal Last.fm itself stated (its own envelope, or an HTTP status)."""


def _http_post(data: dict, timeout: Optional[float] = None):
    """POST one form-encoded call. The only place a socket is opened."""
    from server import httpclient
    return httpclient.shared_post(API_URL, data=data,
                                  timeout=timeout or DEFAULT_TIMEOUT_S)


def _error_of(payload, status: int) -> str:
    """Last.fm's own words for a refusal, or "" when it accepted the call.

    An error is either an HTTP status (403 on a bad signature) or — far more
    often — an ordinary 200 carrying ``{"error": n, "message": "..."}``, which
    a caller that only checked the status code would read as success.
    """
    if isinstance(payload, dict) and payload.get("error"):
        message = str(payload.get("message") or "").strip()
        return (f"error {payload['error']}: {message}" if message
                else f"error {payload['error']}")
    if status and status >= 400:
        return f"HTTP {status}"
    return ""


# Last.fm's codes that mean the SAVED SESSION is gone rather than the service
# being away: 9 "Invalid session key" (the user revoked the app in their
# Last.fm settings, or Last.fm invalidated it) and 26 "Suspended API key".
# Queueing a scrobble either refused would grow the spool for ever while the
# panel still said "Connected as …" — the only fix is to reconnect, so the app
# has to say so instead of pretending it is an outage.
_DEAD_SESSION_ERRORS = ("error 9", "error 26")


def session_dead(error: str) -> bool:
    """Whether a refusal means the stored session no longer works.

    Read off the sentence `_error_of` builds, so it is the SAME answer however
    the call was made (a scrobble write, a now-playing notice, a verify).
    """
    text = str(error or "").strip().lower()
    return any(text.startswith(code) for code in _DEAD_SESSION_ERRORS)


def _call(method: str, params: dict, cfg: dict, *, signed: bool,
          timeout: Optional[float] = None) -> Tuple[Optional[dict], str]:
    """One call, or ``(None, reason)`` — never an exception.

    `signed` adds the api_sig (authenticated methods); every call carries the
    saved API key and asks for JSON. A refusal travels back as Last.fm's own
    sentence, because that is what the user has to read.
    """
    cfg = _cfg(cfg)
    key = api_key(cfg)
    if not key:
        return None, "no lastfm_api_key is set"
    sent = dict(params)
    sent["method"] = method
    sent["api_key"] = key
    sent["format"] = "json"
    if signed:
        secret = api_secret(cfg)
        if not secret:
            return None, "no lastfm_api_secret is set"
        sent["api_sig"] = sign(sent, secret)
    try:
        resp = _http_post(sent, timeout=timeout)
    except Exception as exc:                     # network, DNS, TLS, timeout
        return None, f"{type(exc).__name__}: {exc}"
    status = int(getattr(resp, "status_code", 0) or 0)
    try:
        payload = resp.json()
    except Exception:
        payload = None
    reason = _error_of(payload, status)
    if reason:
        return payload, reason
    if payload is None:
        return None, f"Last.fm answered HTTP {status} with no JSON"
    return payload, ""


# --------------------------------------------------------------------------- #
# the five calls
# --------------------------------------------------------------------------- #
def get_token(cfg=None, timeout: Optional[float] = None) -> Tuple[str, str]:
    """``auth.getToken`` -> a request token (no signature: no session yet)."""
    data, err = _call("auth.getToken", {}, cfg, signed=False, timeout=timeout)
    if err:
        return "", err
    token = str((data or {}).get("token") or "").strip()
    return (token, "") if token else ("", "Last.fm answered with no token")


def auth_url(cfg=None, token: str = "") -> str:
    """Where the USER authorizes *token* for this app's key."""
    from urllib.parse import quote
    return (f"{AUTH_URL}?api_key={quote(api_key(cfg))}&token={quote(token)}")


def get_session(cfg=None, token: str = "",
                timeout: Optional[float] = None) -> dict:
    """``auth.getSession`` -> an OUTCOME dict for that authorized token.

    Signed with the shared secret and carrying NO session key (the token is
    what stands in); the token is consumed by this call.

    -> ``{ok, name, key, code, reason}``. `ok` is True only with a session key.
    `code` names the ONE refusal a connect UI must WAIT OUT rather than treat
    as a failure — ``"NOT_AUTHORIZED"`` is Last.fm's own error 14, "This token
    has not been authorized", which just means the user has not finished in the
    browser yet — and is "" for every other refusal (a blank token, a bad
    signature, a network error). `reason` is always Last.fm's own words, so a
    caller can show them either way."""
    token = str(token or "").strip()
    if not token:
        return {"ok": False, "name": "", "key": "", "code": "",
                "reason": "no token was given"}
    data, err = _call("auth.getSession", {"token": token}, cfg, signed=True,
                      timeout=timeout)
    if err:
        code = ""
        try:
            if int((data or {}).get("error") or 0) == 14:
                code = "NOT_AUTHORIZED"
        except (TypeError, ValueError):
            code = ""
        return {"ok": False, "name": "", "key": "", "code": code, "reason": err}
    session = (data or {}).get("session") or {}
    name = str(session.get("name") or "").strip()
    key = str(session.get("key") or "").strip()
    if not key:
        return {"ok": False, "name": "", "key": "", "code": "",
                "reason": "Last.fm answered with no session key"}
    return {"ok": True, "name": name, "key": key, "code": "", "reason": ""}


def _guard(cfg: dict, *, need=("enabled", "session")) -> str:
    """Why nothing may be sent, or "" — the only door on the write path.

    Missing credentials are named BEFORE "not connected": on a fresh install
    the actionable answer is which key to paste, not that there is no session
    yet.
    """
    cfg = _cfg(cfg)
    if not api_key(cfg):
        return "no lastfm_api_key is set"
    if not api_secret(cfg):
        return "no lastfm_api_secret is set"
    if "session" in need and not session_key(cfg):
        return "not connected — no Last.fm session (use Connect)"
    if "enabled" in need and not enabled(cfg):
        return "scrobbling is off (lastfm_scrobble_enabled)"
    return ""


def refusal(cfg=None) -> str:
    """Why nothing may be sent right now, or "" — the public form of `_guard`
    (the routes read it BEFORE deciding whether a failure is worth queueing:
    "not connected" is never a queued scrobble, it is a refusal with a reason)."""
    return _guard(_cfg(cfg))


def _meta_params(meta: dict) -> Dict[str, str]:
    """The track fields Last.fm takes, from what the file carries — blanks are
    OMITTED rather than sent empty (an empty album is not an album)."""
    out = {}
    for tag, key in (("title", "track"), ("artist", "artist"),
                     ("album", "album"), ("album_artist", "albumArtist"),
                     ("duration", "duration"), ("track_number", "trackNumber"),
                     ("mbid", "mbid")):
        value = meta.get(tag)
        if value in (None, "", 0, 0.0):
            continue
        if tag == "duration":
            # Last.fm takes whole seconds; a header's 214.7 s would be a
            # malformed `duration`.
            value = int(round(float(value)))
        out[key] = str(value)
    return out


def now_playing(cfg=None, meta: Optional[dict] = None,
                timeout: Optional[float] = None) -> Tuple[bool, str]:
    """``track.updateNowPlaying`` (what is on right now) -> ``(ok, reason)``."""
    cfg = _cfg(cfg)
    reason = _guard(cfg)
    if reason:
        return False, reason
    params = _meta_params(meta or {})
    if not params.get("artist") or not params.get("track"):
        return False, "the file carries no artist/title to announce"
    params["sk"] = session_key(cfg)
    data, err = _call("track.updateNowPlaying", params, cfg, signed=True,
                      timeout=timeout)
    if err:
        return False, err
    return True, ""


def send_scrobbles(cfg=None, entries: Optional[List[dict]] = None,
                   timeout: Optional[float] = None) -> Tuple[int, str]:
    """``track.scrobble`` for up to ``BATCH`` entries -> ``(accepted, reason)``.

    Each entry is the plain shape the spool stores (artist/track/timestamp/…).
    The indexed parameter form (``artist[0]``, ``track[0]``, ``timestamp[0]``…)
    is what Last.fm documents for a batch; the signature sorts them by name
    like any other parameter.
    """
    cfg = _cfg(cfg)
    reason = _guard(cfg)
    if reason:
        return 0, reason
    entries = [e for e in (entries or []) if e.get("artist") and e.get("track")
               and e.get("timestamp")]
    if not entries:
        return 0, "nothing to scrobble"
    params: Dict[str, str] = {"sk": session_key(cfg)}
    for i, entry in enumerate(entries[:BATCH]):
        params[f"artist[{i}]"] = str(entry["artist"])
        params[f"track[{i}]"] = str(entry["track"])
        # The play's START, in whole unix seconds — Last.fm refuses anything
        # older than 14 days, so the spool drops old entries first.
        params[f"timestamp[{i}]"] = str(int(entry["timestamp"]))
        for tag, key in (("album", "album"), ("album_artist", "albumArtist"),
                         ("duration", "duration"),
                         ("track_number", "trackNumber"), ("mbid", "mbid")):
            value = entry.get(tag)
            if value not in (None, "", 0, 0.0):
                params[f"{key}[{i}]"] = str(value)
    data, err = _call("track.scrobble", params, cfg, signed=True,
                      timeout=timeout)
    if err:
        return 0, err
    attr = ((data or {}).get("scrobbles") or {}).get("@attr") or {}
    try:
        accepted = int(attr.get("accepted"))
    except (TypeError, ValueError):
        accepted = len(entries)
    return accepted, ""


def verify_session(cfg=None, timeout: Optional[float] = None) -> dict:
    """Is the SAVED session accepted? -> ``{ok, checked, detail}``.

    Proved with an AUTHENTICATED read — ``user.getInfo`` on the session's own
    user, the cheapest call an sk alone can make — so a stale or revoked
    session is a `fail` in Last.fm's own words, never a green row over a
    credential that no longer works. A blank key/secret/session is `checked:
    ""` and a sentence saying which one is missing, and never an exception.
    """
    cfg = _cfg(cfg)
    if not api_key(cfg):
        return {"ok": False, "checked": "",
                "detail": "no lastfm_api_key is set"}
    if not api_secret(cfg):
        return {"ok": False, "checked": "",
                "detail": "no lastfm_api_secret is set"}
    if not session_key(cfg):
        return {"ok": False, "checked": "",
                "detail": "not connected — no Last.fm session (use Connect)"}
    params = {"sk": session_key(cfg)}
    who = username(cfg)
    if who:
        params["user"] = who
    data, err = _call("user.getInfo", params, cfg, signed=True, timeout=timeout)
    if err:
        return {"ok": False, "checked": "user.getInfo",
                "detail": f"Last.fm rejected the session — {err}"}
    user = (data or {}).get("user") or {}
    name = str(user.get("name") or "").strip() or who or "the saved session"
    return {"ok": True, "checked": "user.getInfo",
            "detail": f"session accepted — signed in as {name}"}


# --------------------------------------------------------------------------- #
# the spool — a refused scrobble survives the process
# --------------------------------------------------------------------------- #
def spool_path() -> str:
    """``<music>/.mlo/data/lastfm_scrobbles.jsonl`` — one scrobble per line."""
    from mlo.paths import app_data_dir
    return os.path.join(app_data_dir(), SPOOL_NAME)


def _read_spool() -> List[dict]:
    """Every queued scrobble, oldest first. A torn/unreadable line is skipped
    (a half-written line is not a scrobble)."""
    out: List[dict] = []
    try:
        with open(spool_path(), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("artist") and row.get("track"):
                    out.append(row)
    except FileNotFoundError:
        return []
    except OSError:
        return out
    return out


def _write_spool(rows: List[dict]) -> None:
    path = spool_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # The temp name carries the THREAD as well as the process: two scrobbles
    # handled by two FastAPI worker threads would otherwise share one temp
    # path and overwrite each other's half-written file.
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ONE lock for the spool's read-modify-write. Both `queue_scrobble` and
# `flush_spool` rewrite the WHOLE file, so overlapping them lost a play: the
# flush reads [p1], spends seconds on the network, meanwhile a second scrobble
# appends p2 (writing [p1, p2]), and the flush then writes back its own list —
# p2 gone although its caller was told "queued". FastAPI runs these sync routes
# in a threadpool, so the overlap is real, and it is likeliest exactly when it
# matters: during an outage, when several plays queue while the opportunistic
# flush is in flight. The promise this module makes is "a refused scrobble is
# queued and retried, never dropped".
_spool_lock = threading.Lock()


def queue_scrobble(entry: dict, cfg=None) -> int:
    """Append one refused scrobble and return the queue length.

    The cap (``SPOOL_MAX``) drops the OLDEST first: Last.fm refuses a scrobble
    older than 14 days, so the oldest queued entries are the ones already
    nearest to being unusable.
    """
    with _spool_lock:
        # The whole read-modify-write lives inside the lock: a list read
        # before it would be seconds stale by the time a concurrent flush
        # finished, and writing that list back is how a just-queued play was
        # dropped.
        rows = _read_spool()
        rows.append(dict(entry))
        if len(rows) > SPOOL_MAX:
            rows = rows[-SPOOL_MAX:]
        try:
            _write_spool(rows)
        except OSError:
            return len(rows)
        return len(rows)


def queued_count(cfg=None) -> int:
    return len(_read_spool())


def flush_spool(cfg=None, timeout: Optional[float] = None) -> dict:
    """Send what is queued, oldest first, in ``BATCH`` slices.

    Stops at the first slice the service refuses (the rest would be refused
    for the same reason), keeping everything not accepted — so a flush is
    idempotent-safe: it never drops a scrobble it could not send.
    """
    cfg = _cfg(cfg)
    reason = _guard(cfg)
    if reason:
        return {"ok": False, "flushed": 0, "queued": queued_count(cfg),
                "error": reason}
    # The whole read → send → write-back runs under the spool lock. Sending
    # takes seconds, and a scrobble queued in the meantime used to be erased
    # by the write-back of a list read before it (see _spool_lock). The lock
    # is held across the network on purpose: the alternative is losing plays,
    # and this runs at most once per play.
    with _spool_lock:
        rows = _read_spool()
        if not rows:
            return {"ok": True, "flushed": 0, "queued": 0, "error": ""}
        flushed = 0
        while rows:
            slice_ = rows[:BATCH]
            accepted, err = send_scrobbles(cfg, slice_, timeout=timeout)
            if err or accepted < len(slice_):
                # A slice the service took only PART of is kept WHOLE: Last.fm
                # does not say which entries it ignored in a way that maps back
                # to the spool's order, and re-sending an accepted scrobble is
                # a lesser sin than dropping a play that was never counted.
                try:
                    _write_spool(rows)
                except OSError:
                    pass
                return {"ok": False, "flushed": flushed,
                        "queued": len(rows),
                        "error": err or "Last.fm accepted only part of the batch"}
            flushed += accepted
            rows = rows[accepted:]
            if accepted == 0:
                break
        try:
            _write_spool(rows)
        except OSError:
            pass
        return {"ok": True, "flushed": flushed, "queued": len(rows), "error": ""}


# --------------------------------------------------------------------------- #
# the track's own metadata (never invented)
# --------------------------------------------------------------------------- #
def track_meta(path: str) -> dict:
    """What the FILE says about itself: title/artist/album/…/duration.

    Read through ``mlo.audio.AudioFile`` — the same reader the rest of the app
    tags with — so an untagged file reports blanks and the send is refused by
    name, rather than Last.fm being handed a filename dressed up as metadata.
    """
    from mlo.audio import AudioFile
    out = {"title": "", "artist": "", "album": "", "album_artist": "",
           "duration": 0.0, "track_number": "", "mbid": "", "ok": False,
           "error": ""}
    if not path or not os.path.isfile(path):
        out["error"] = "the file is not there"
        return out
    try:
        af = AudioFile(path)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    for tag, key in (("TITLE", "title"), ("ARTIST", "artist"),
                     ("ALBUM", "album"), ("ALBUMARTIST", "album_artist"),
                     ("TRACKNUMBER", "track_number"),
                     ("MUSICBRAINZ_TRACKID", "mbid")):
        try:
            value = af.get_tag(tag)
        except Exception:
            value = None
        out[key] = str(value).strip() if value not in (None, "") else ""
    # ALBUMARTIST is what Last.fm's `albumArtist` means; a file that only
    # carries ARTIST has no album artist to state, so nothing is invented.
    if not out["album_artist"]:
        out["album_artist"] = ""
    out["duration"] = _duration_of(af)
    out["ok"] = bool(out["artist"] and out["title"])
    if not out["ok"]:
        out["error"] = "the file carries no artist/title tags"
    return out


def _duration_of(af) -> float:
    """Seconds the file's own header states, or 0.0 (video uses ffprobe's)."""
    tech = getattr(af, "tech", None)
    if isinstance(tech, dict) and tech.get("length"):
        try:
            return float(tech["length"])
        except (TypeError, ValueError):
            pass
    try:
        return float(af.audio.info.length)
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
# Last.fm's own scrobble rule
# --------------------------------------------------------------------------- #
def scrobble_percent(cfg=None) -> float:
    """The share of a track a play must reach — `lastfm_scrobble_percent`,
    clamped to 50–100.

    The floor is Last.fm's own: it discards a scrobble below half the track,
    so a setting under it would only file plays nobody counts. A missing,
    blank or unparsable value reads as the shipped 70; 100 means "only a play
    that reached the end counts".
    """
    raw = _cfg(cfg).get("lastfm_scrobble_percent")
    try:
        percent = float(raw)
    except (TypeError, ValueError):
        percent = float(DEFAULT_PERCENT)
    return min(MAX_PERCENT, max(MIN_PERCENT, percent))


def scrobble_threshold(duration: float, cfg=None) -> Optional[float]:
    """Seconds a play must reach to count, or None for a track too short.

    A track must be at least ``MIN_TRACK_SECONDS`` long, and the play reaches
    ``scrobble_percent`` of it — 70 % by default, configurable per library.
    """
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        return None
    if duration < MIN_TRACK_SECONDS:
        return None
    return duration * scrobble_percent(cfg) / 100.0


def meets_rule(duration: float, played_seconds: float, cfg=None) -> bool:
    """Does this play count under the scrobble rule? (see `scrobble_threshold`)."""
    need = scrobble_threshold(duration, cfg)
    if need is None:
        return False
    try:
        played = float(played_seconds)
    except (TypeError, ValueError):
        return False
    return played >= need


# --------------------------------------------------------------------------- #
# the status the panel reads
# --------------------------------------------------------------------------- #
def status(cfg=None) -> dict:
    """Everything Settings → Scrobbling needs in one shape.

    ``enabled`` is the switch, ``connected`` a saved session, ``username``
    who it belongs to, ``queued`` how many refused scrobbles wait, and
    ``key``/``secret`` whether the app's own Last.fm application is configured
    (the secret is what the Connect flow signs with). No network call.
    """
    cfg = _cfg(cfg)
    key, secret, session = api_key(cfg), api_secret(cfg), session_key(cfg)
    return {
        "enabled": enabled(cfg),
        "connected": bool(session),
        "username": username(cfg),
        "queued": queued_count(cfg),
        "key": bool(key),
        "secret": bool(secret),
        "ready": bool(key and secret and session and enabled(cfg)),
    }