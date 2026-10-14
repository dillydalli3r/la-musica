"""Last.fm routes — the Settings → Scrobbling surface.

    GET  /api/lastfm/status          the one shape the panel reads (no network)
    POST /api/lastfm/connect         -> {url} the user authorizes, using the
                                     app's own key + secret from config
    POST /api/lastfm/connect/finish  {token, wait?} -> stores the session key
                                     and the username Last.fm reports; with
                                     `wait`, a not-yet-authorized token is a
                                     200 {ok:false, waiting:true} instead of an
                                     error, so a UI can poll
    POST /api/lastfm/disconnect      forget the session (the key/secret stay)
    POST /api/lastfm/now-playing     {path} — fire-and-forget from the player
    POST /api/lastfm/scrobble        {path, played_at, played_seconds}

Two rules hold across every route here:

  * NOTHING is ever sent without a session key and ``lastfm_scrobble_enabled``
    (``server.lastfm``'s own guard), and
  * a route REFUSES politely rather than raising — a settings panel and a
    playback path both have to read a sentence, not a stack trace. The ONE
    exception is a config write that fails, which is a genuine 500 (it is the
    same failure every other settings route reports the same way).

A scrobble that reaches Last.fm but is refused is QUEUED (``queued: true`` with
the new count) and never fails the play; one refused BEFORE anything is sent —
scrobbling off, no session — is not queued at all, because it would only be
refused again.

Mounted by ``server/main.py`` (``include_router``); this module never imports
it.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mlo.config import load_config, save_config
from server import lastfm

router = APIRouter(tags=["lastfm"])


class TokenPost(BaseModel):
    token: str = ""
    wait: bool = False


class PathPost(BaseModel):
    path: str = ""


class ScrobblePost(BaseModel):
    path: str = ""
    played_at: Optional[float] = None
    played_seconds: float = 0.0


def _save(cfg: dict) -> None:
    """Write the session keys back through the app's own config writer.

    Same path every settings route uses, so the atomic write, the music-folder
    migration and the failure sentence are not re-invented here."""
    if not save_config(cfg):
        reason = getattr(save_config, "last_error", "") or ""
        raise HTTPException(
            500, f"Failed to save config{(': ' + reason) if reason else ''}")


@router.get("/api/lastfm/status")
def status_get():
    """Everything the panel shows: enabled, connected, who, and the queue."""
    return lastfm.status(load_config())


@router.post("/api/lastfm/connect")
def connect_post():
    """Start the authorization: a request token, and where to send the user.

    Needs the app's OWN application — an API key AND the shared secret issued
    beside it (the secret is what signs auth.getSession). Missing either is a
    sentence, not an exception: the panel says what to paste and where.
    """
    cfg = load_config()
    if not lastfm.api_key(cfg):
        return {"ok": False, "error": "no lastfm_api_key is set — paste one on "
                                      "Settings → Sources first"}
    if not lastfm.api_secret(cfg):
        return {"ok": False, "error": "no lastfm_api_secret is set — the API "
                                      "key alone cannot sign a session; it is "
                                      "shown beside the key on last.fm"}
    token, err = lastfm.get_token(cfg)
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "token": token, "url": lastfm.auth_url(cfg, token)}


@router.post("/api/lastfm/connect/finish")
def connect_finish_post(req: TokenPost):
    """Exchange the authorized token for a session and store it.

    The token is consumed once, so a stale one is Last.fm's own refusal back.
    With ``wait=true`` the ONE refusal a user causes by not having finished in
    the browser yet — Last.fm error 14, NOT_AUTHORIZED — answers 200
    ``{ok: false, waiting: true, reason: …}`` so a polling UI need not read it
    as a failure; every other refusal keeps today's shape, and success is
    unchanged. The route never blocks: one upstream call per request.
    """
    cfg = load_config()
    out = lastfm.get_session(cfg, req.token)
    if not out.get("ok"):
        if req.wait and out.get("code") == "NOT_AUTHORIZED":
            return {"ok": False, "waiting": True, "reason": out.get("reason") or ""}
        return {"ok": False, "error": out.get("reason") or ""}
    cfg["lastfm_session_key"] = out["key"]
    cfg["lastfm_username"] = out.get("name") or ""
    _save(cfg)
    return {"ok": True, "username": out.get("name") or "",
            "status": lastfm.status(load_config())}


@router.post("/api/lastfm/disconnect")
def disconnect_post():
    """Forget the session (the app's key and secret stay saved).

    The queue is left alone: a scrobble already collected is not thrown away
    by disconnecting."""
    cfg = load_config()
    cfg["lastfm_session_key"] = ""
    cfg["lastfm_username"] = ""
    _save(cfg)
    return {"ok": True, "status": lastfm.status(load_config())}


@router.post("/api/lastfm/now-playing")
def now_playing_post(req: PathPost):
    """Announce what started playing. Fire-and-forget: `ok: false` with a
    reason is a normal answer, and the player never waits on it."""
    cfg = load_config()
    why = lastfm.refusal(cfg)
    if why:
        return {"ok": False, "error": why}
    meta = lastfm.track_meta(req.path)
    if not meta["ok"]:
        return {"ok": False, "error": meta["error"]}
    ok, err = lastfm.now_playing(cfg, meta)
    return {"ok": ok, "error": err}


@router.post("/api/lastfm/scrobble")
def scrobble_post(req: ScrobblePost):
    """Report ONE play. The rule is re-checked HERE against the file's own
    duration and the seconds the client reported, so a client that crossed its
    threshold early cannot smuggle a play past Last.fm's.

    A send that fails is queued and answered 200 (``queued: true``); a refusal
    BEFORE the send — scrobbling off, no session — queues nothing and says
    why, because it would only be refused again."""
    cfg = load_config()
    why = lastfm.refusal(cfg)
    if why:
        return {"ok": False, "scrobbled": False, "queued": False,
                "queued_count": lastfm.queued_count(cfg), "error": why}
    meta = lastfm.track_meta(req.path)
    if not meta["ok"]:
        return {"ok": False, "scrobbled": False, "queued": False,
                "queued_count": lastfm.queued_count(cfg),
                "error": meta["error"]}
    played = req.played_seconds or 0.0
    if not lastfm.meets_rule(meta["duration"], played, cfg):
        need = lastfm.scrobble_threshold(meta["duration"], cfg)
        reason = ("the track is shorter than 30 s, so it is never scrobbled"
                  if need is None else
                  f"played {int(played)} s of the {int(need)} s this library "
                  f"waits for ({int(lastfm.scrobble_percent(cfg))} % of the track)")
        return {"ok": False, "scrobbled": False, "queued": False,
                "queued_count": lastfm.queued_count(cfg),
                "error": "", "reason": reason}
    played_at = req.played_at
    if not played_at:
        # The play's start, derived when the client did not say: it began
        # `played` seconds ago.
        import time as _time
        played_at = _time.time() - float(played)
    entry = {
        "artist": meta["artist"], "track": meta["title"],
        "album": meta["album"], "album_artist": meta["album_artist"],
        "duration": int(round(meta["duration"])),
        "track_number": meta["track_number"], "mbid": meta["mbid"],
        "timestamp": int(played_at),
    }
    # Opportunistic: every send is a chance to drain what an earlier outage
    # left behind, before this play adds to it.
    lastfm.flush_spool(cfg)
    accepted, err = lastfm.send_scrobbles(cfg, [entry])
    if accepted >= 1:
        return {"ok": True, "scrobbled": True, "queued": False,
                "queued_count": lastfm.queued_count(cfg), "error": ""}
    count = lastfm.queue_scrobble(entry, cfg)
    return {"ok": False, "scrobbled": False, "queued": True,
            "queued_count": count, "error": err or "Last.fm did not accept it"}