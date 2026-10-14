"""MusicBrainz login (OAuth2) + rating SUBMISSIONS — the WRITE surface.

The in-app MusicBrainz browser (``server/api_mb.py``) reads anonymously and
changes nothing on MusicBrainz. This module is the other half: the OAuth2
connect flow that authorizes the user's own account, and the one route that
pushes the user's ratings back. The network work — token exchange/renewal,
``/oauth2/userinfo``, and the MMD-2.0 rating POST — lives in
``server.mb_submit``; these routes stay thin (gate, group the files, hand over,
answer).

    GET  /api/mb/auth/status      -> {connected, username, submit_enabled, …}
    POST /api/mb/auth/connect     -> {ok, url, verifier, detail}
    POST /api/mb/auth/finish      -> {ok, detail, username, status}   (code, verifier)
    POST /api/mb/auth/disconnect  -> the status afterwards
    GET  /api/mb/ratings/queue    -> the background upload queue's own state
    POST /api/mb/ratings/submit   -> per-FILE rows  (paths: [file, …])
    POST /api/mb/ratings/submit-all -> {ok, queued}  (every rated folder)

The submit route is where the BATCHING lives: one album's recordings are one
MusicBrainz request, and the album's release group rides in the SAME request
(``POST /ws/2/rating`` takes many entities at once), grouped by the album
folder each file sits in. The user's own rating is read from the app's store
(``server.ratings``, half-stars) and snapped to MusicBrainz's 20-point grid in
``mb_submit``; a file with no rating, no id, or a value that would snap to 0
is reported as skipped with its own sentence, never silently dropped.

``submit-all`` and the automatic post-rating upload do NOT submit here: they
enqueue the folder on ``server.mb_ratings_sync``'s daemon worker, which builds
the SAME items (via ``mb_ratings_sync.album_submission``) and submits them off
the request path.
"""
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from server import auth as auth_mod
from server import mb_ratings_sync
from server import mb_submit
from server.api_common import load_config


router = APIRouter(tags=["mb"])


class FinishBody(BaseModel):
    code: str = ""
    verifier: str = ""


class SubmitBody(BaseModel):
    paths: list[str] = []


# --------------------------------------------------------------------------- #
# The login
# --------------------------------------------------------------------------- #
@router.get("/api/mb/auth/status")
def mb_auth_status():
    """Is a MusicBrainz login saved, WHO is it, and is submitting switched on."""
    return mb_submit.status(load_config())


@router.post("/api/mb/auth/connect")
def mb_auth_connect():
    """The authorize URL to open plus the PKCE verifier the panel must keep."""
    return mb_submit.connect_url(load_config())


@router.post("/api/mb/auth/finish")
def mb_auth_finish(body: FinishBody):
    """Exchange the pasted code + verifier for a login (refusal in its words)."""
    return mb_submit.finish_connect(body.code, body.verifier, load_config())


@router.post("/api/mb/auth/disconnect")
def mb_auth_disconnect():
    """Forget the login, keeping the saved app registration."""
    return mb_submit.disconnect(load_config())


# --------------------------------------------------------------------------- #
# The submission
# --------------------------------------------------------------------------- #
def _file_row(owner, decided):
    """One per-file result row: what was sent (and from what) or why not."""
    kind, path, value = owner
    return {
        "path": path,
        "kind": kind,                     # "track" or "album"
        "title": os.path.basename(os.path.normpath(path)),
        "entity": decided.get("entity", ""),
        "mbid": decided.get("mbid", ""),
        "value": value,                   # the app's 0-100, before snapping
        "sent": decided.get("sent"),      # the snapped value actually sent
        "status": "sent" if decided.get("sent") else "skipped",
        "reason": decided.get("reason", ""),
    }


@router.post("/api/mb/ratings/submit")
def mb_ratings_submit(body: SubmitBody, request: Request = None):
    """Submit the user's ratings for these files, one request per album.

    Refuses outright when submissions are switched off or nothing is connected
    — there is no partial answer to give for a login that does not exist. When
    connected, every file is accounted for: a row says the snapped value that
    was sent (and the rating it came from), or the sentence explaining the
    skip. MusicBrainz's own refusal is returned in `detail`, never raised."""
    cfg = load_config()
    if not cfg.get("musicbrainz_submit_enabled", True):
        raise HTTPException(
            400, "MusicBrainz rating submissions are switched off — turn them "
                 "on in Settings → Scrobbling & submissions")
    state = mb_submit.status(cfg)
    if not state.get("connected"):
        raise HTTPException(
            400, "no MusicBrainz login is saved — connect the app to your "
                 "MusicBrainz account first")
    user = auth_mod.current_user(request)
    paths = [str(p).strip() for p in (body.paths or []) if str(p or "").strip()]
    if not paths:
        raise HTTPException(400, "no files were given to submit")

    # One MusicBrainz request per album folder: its recordings, and its release
    # group in the same request. What a folder contributes is decided by ONE
    # builder — the queue worker calls the very same one.
    groups = {}
    for path in paths:
        groups.setdefault(os.path.dirname(os.path.normpath(path)), []).append(path)

    rows, details, all_ok = [], [], True
    for folder, files in groups.items():
        owners, items = mb_ratings_sync.album_submission(folder, files, user=user)
        result = mb_submit.submit_ratings(items, cfg)
        if not result.get("ok"):
            all_ok = False
            if result.get("detail"):
                details.append(result["detail"])
        for owner, decided in zip(owners, result.get("rows") or []):
            rows.append(_file_row(owner, decided))

    sent = sum(1 for r in rows if r["status"] == "sent")
    return {
        "ok": all_ok,
        "sent": sent,
        "skipped": len(rows) - sent,
        "detail": " · ".join(details),
        "username": state.get("username", ""),
        "rows": rows,
    }


@router.get("/api/mb/ratings/queue")
def mb_ratings_queue():
    """The background upload queue: is it on, what is pending, what it did.

    No network and no submission — just the worker's own state, so the panel
    can poll it cheaply while a rating settles."""
    return {"ok": True, **mb_ratings_sync.state()}


@router.post("/api/mb/ratings/submit-all")
def mb_ratings_submit_all():
    """Queue EVERY folder that carries a rating; -> how many joined the queue.

    Fast and offline: it only walks the app's own store and enqueues, never
    waits for a submission. A login that is missing or switched off queues
    nothing (``queued: 0``) — the queue itself refuses folders it cannot
    submit."""
    return {"ok": True, "queued": mb_ratings_sync.enqueue_all(load_config())}