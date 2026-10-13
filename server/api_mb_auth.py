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
    POST /api/mb/ratings/submit   -> per-FILE rows  (paths: [file, …])

The submit route is where the BATCHING lives: one album's recordings are one
MusicBrainz request, and the album's release group rides in the SAME request
(``POST /ws/2/rating`` takes many entities at once), grouped by the album
folder each file sits in. The user's own rating is read from the app's store
(``server.ratings``, half-stars) and snapped to MusicBrainz's 20-point grid in
``mb_submit``; a file with no rating, no id, or a value that would snap to 0
is reported as skipped with its own sentence, never silently dropped.
"""
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from server import auth as auth_mod
from server import mb_submit
from server import ratings as ratings_mod
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
def _album_release_group(paths):
    """The release-group MBID the files of one album carry, or "".

    Read off the files' own tags (MUSICBRAINZ_RELEASEGROUPID) — the same
    evidence the import wrote — and the first file that states one decides for
    the folder."""
    from server import tagcache
    for path in paths:
        try:
            tags, _ = tagcache.read_track(path, ["MUSICBRAINZ_RELEASEGROUPID"])
        except Exception:
            continue
        got = str((tags or {}).get("MUSICBRAINZ_RELEASEGROUPID") or "").strip()
        if got:
            return got
    return ""


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

    # The app's own ratings: the store's half-stars (a stored row wins; a
    # track with no row takes its file's own RATING tag — `adopt`).
    values = ratings_mod.map_for(paths=paths, user=user, adopt=True, scope="track")

    # One MusicBrainz request per album folder: its recordings, and its release
    # group in the same request.
    groups = {}
    for path in paths:
        groups.setdefault(os.path.dirname(os.path.normpath(path)), []).append(path)

    rows, details, all_ok = [], [], True
    for folder, files in groups.items():
        owners, items = [], []
        for path in files:
            half = int(values.get(ratings_mod.api_path(path)) or 0)
            value = half * ratings_mod.TAG_STEP
            items.append(("recording", ratings_mod.mbid_for(path), value))
            owners.append(("track", path, value))
        rg_mbid = _album_release_group(files)
        album_half = int(ratings_mod.value(folder, user=user, scope="album") or 0)
        if rg_mbid or album_half:
            items.append(("release_group", rg_mbid,
                          album_half * ratings_mod.TAG_STEP))
            owners.append(("album", folder, album_half * ratings_mod.TAG_STEP))
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