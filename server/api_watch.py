"""Artist watches over HTTP — the list, the create, the edit, the check.

The router is registered by server/main.py (``include_router``); this module
never imports it. Creation and editing never evaluate anything: a watch is a
standing instruction, and ONLY a cycle (the periodic worker, or
``POST /api/watches/{id}/check``) may queue an album — that is what keeps a
brand-new watch from dumping a discography the moment it is created.

``GET /api/watches/candidates`` is the same verdict a cycle would reach, served
for the picker: the agent of the rules (date, type, allow-list, veto) plus the
holdings checks, so what the UI shows ticked is exactly what the watcher would
download.
"""
from typing import List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from mlo.config import load_config

from server import artist_watch, artist_watch_worker

router = APIRouter()


class WatchInput(BaseModel):
    """A watch to create. `include`/`exclude` are release-group MBIDs; a
    non-empty `include` is an allow-list, `exclude` is a veto."""
    artist_mbid: str
    artist: Optional[str] = None
    policy: Optional[str] = None
    release_types: Optional[List[str]] = None
    include: Optional[List[str]] = None
    exclude: Optional[List[str]] = None
    max_per_cycle: Optional[int] = None
    auto_add: Optional[bool] = None
    note: Optional[str] = None


class WatchPatch(BaseModel):
    """A change to an existing watch — only the fields sent are applied."""
    artist: Optional[str] = None
    enabled: Optional[bool] = None
    policy: Optional[str] = None
    release_types: Optional[List[str]] = None
    include: Optional[List[str]] = None
    exclude: Optional[List[str]] = None
    max_per_cycle: Optional[int] = None
    auto_add: Optional[bool] = None
    note: Optional[str] = None


def _intg():
    from server import integrations
    return integrations


def _artist(mbid_text):
    """(mbid, name) for a MusicBrainz artist, or the right HTTP error.

    A MusicBrainz OUTAGE is 503 (the id may well be fine); an id MusicBrainz
    does not know as an artist is 400 — the two must not be conflated, or a
    busy afternoon would read as "this artist does not exist".
    """
    intg = _intg()
    rid = intg._mbid(mbid_text)
    if not rid:
        raise HTTPException(400, "a MusicBrainz artist ID or URL is required")
    if not artist_watch.clean_mbid(rid):
        raise HTTPException(400, f"not a MusicBrainz ID: {mbid_text!r}")
    try:
        ident = intg.artist_identity(rid)
    except intg.MusicBrainzError as e:
        raise HTTPException(503, f"MusicBrainz did not answer: {e}")
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else 0
        if status == 404:
            raise HTTPException(400, "MusicBrainz has no artist with this ID")
        raise HTTPException(503, f"MusicBrainz did not answer: {e}")
    except Exception as e:
        raise HTTPException(503, f"MusicBrainz did not answer: {e}")
    if not ident.get("id"):
        raise HTTPException(400, "MusicBrainz has no artist with this ID")
    return str(rid).lower(), ident.get("name") or ""


@router.get("/api/watches")
def watches_list():
    """Every watched artist, what each has queued/imported, and the worker's
    own state (whether it is running, when it last ran, what it is doing)."""
    cfg = load_config()
    return {"watches": artist_watch.watches_payload(cfg),
            "worker": artist_watch_worker.status()}


@router.post("/api/watches")
def watches_add(req: WatchInput):
    """Watch an artist. Queues NOTHING — the first check does that."""
    mbid, name = _artist(req.artist_mbid)
    if artist_watch.get_watch_for_artist(mbid):
        raise HTTPException(409, f"already watching {name or mbid}")
    try:
        watch = artist_watch.add_watch(
            mbid, req.artist or name, policy=req.policy,
            release_types=req.release_types, include=req.include,
            exclude=req.exclude, max_per_cycle=req.max_per_cycle,
            auto_add=req.auto_add, note=req.note or "")
    except artist_watch.WatchExists:
        raise HTTPException(409, f"already watching {name or mbid}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "watch": artist_watch.watch_payload(watch)}


@router.get("/api/watches/candidates")
def candidates_for_artist(
    artist_mbid: str = Query(...),
    artist: str = Query(""),
    policy: Optional[str] = Query(None),
    release_types: Optional[List[str]] = Query(None),
    include: Optional[List[str]] = Query(None),
    exclude: Optional[List[str]] = Query(None),
):
    """What the rules would do with this artist's release groups — the form a
    NOT-yet-watched artist needs (the rules come from the query). Values may be
    repeated or comma/semicolon-separated."""
    intg = _intg()
    rid = intg._mbid(artist_mbid)
    if not rid or not artist_watch.clean_mbid(rid):
        raise HTTPException(400, f"not a MusicBrainz ID: {artist_mbid!r}")
    cfg = load_config()
    try:
        out = artist_watch.candidates(
            rid, None, cfg, artist=artist, policy=policy,
            release_types=_split(release_types), include=_split(include),
            exclude=_split(exclude))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except intg.MusicBrainzError as e:
        raise HTTPException(503, f"MusicBrainz did not answer: {e}")
    if not out["items"]:
        # No release groups is a real answer; "no such artist" is a 404. Only
        # the empty case pays for the lookup that tells them apart.
        name = _artist(rid)[1]
        if not out["artist"]:
            out["artist"] = name
    return out


@router.get("/api/watches/{wid}/candidates")
def candidates_for_watch(wid: int):
    """The same verdict, with an existing watch's stored rules."""
    watch = artist_watch.get_watch(wid)
    if watch is None:
        raise HTTPException(404, "watch not found")
    cfg = load_config()
    intg = _intg()
    try:
        return artist_watch.candidates(watch["artist_mbid"], watch, cfg)
    except intg.MusicBrainzError as e:
        raise HTTPException(503, f"MusicBrainz did not answer: {e}")


@router.patch("/api/watches/{wid}")
def watches_update(wid: int, req: WatchPatch):
    """Enable/disable a watch, change its policy, its filters or its cap."""
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if "artist" in fields:
        fields["name"] = fields.pop("artist")
    try:
        watch = artist_watch.update_watch(wid, fields)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if watch is None:
        raise HTTPException(404, "watch not found")
    return {"ok": True, "watch": artist_watch.watch_payload(watch)}


@router.delete("/api/watches/{wid}")
def watches_delete(wid: int):
    """Stop watching an artist (the albums it already queued stay)."""
    if not artist_watch.delete_watch(wid):
        raise HTTPException(404, "watch not found")
    return {"ok": True, "id": int(wid)}


@router.post("/api/watches/{wid}/check")
def watches_check(wid: int):
    """Check ONE watch now, honouring its interval-free force and its cap.

    A disabled watch is not checked: disabling is how a user stops one, and a
    "check now" must not be the way around it. The check runs INLINE (one
    cached browse, the same work a tick does) so the answer is what this check
    actually found and queued, rather than a promise about the future.
    """
    watch = artist_watch.get_watch(wid)
    if watch is None:
        raise HTTPException(404, "watch not found")
    cfg = load_config()
    if not watch.get("enabled"):
        return {"ok": True, "checked": 0, "queued": [], "notified": [],
                "errors": [], "summary": "disabled"}
    out = artist_watch.run_watch(watch, cfg, force=True)
    return {"ok": True, "checked": out["checked"], "queued": out["queued"],
            "notified": out["notified"], "errors": out["errors"],
            "summary": out["summary"]}


def _split(values):
    """Query values as a list, allowing "a,b;c" in one (and `None` for none)."""
    out = []
    for raw in values or []:
        for part in str(raw or "").replace(";", ",").split(","):
            if part.strip():
                out.append(part.strip())
    return out
