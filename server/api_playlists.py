"""Playlists: the manual and smart lists, their .m3u8 import/export.

The storage and the smart-filter rules live in ``server.playlists`` (``pl_mod``)
and each list is scoped to the user the session names; the routes here are the
HTTP shape of list/create/rename/add/order/remove/filter/evaluate/export/import.
"""
import os
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from server import auth as auth_mod
from server import library as lib_mod
from server import playlists as pl_mod
from server.api_common import (_in_music_folder, _music_folder,
                               re_safe_filename, load_config)

router = APIRouter(tags=["playlists"])


class PlaylistCreate(BaseModel):
    name: str
    kind: str = "manual"
    filter: Optional[dict] = None


class PlaylistUpdate(BaseModel):
    """Partial playlist update (currently: rename)."""
    name: str | None = None


class PlaylistTracks(BaseModel):
    paths: List[str]
    position: Optional[int] = None


class SmartFilter(BaseModel):
    filter: dict


# --------------------------------------------------------------------------- #
# Playlists
# --------------------------------------------------------------------------- #
@router.get("/api/playlists")
def playlists_list(request: Request = None):
    return pl_mod.list_playlists(auth_mod.current_user(request))


@router.post("/api/playlists")
def playlists_create(req: PlaylistCreate, request: Request = None):
    if not req.name.strip():
        raise HTTPException(400, "name required")
    user = auth_mod.current_user(request)
    pid = pl_mod.create_playlist(req.name.strip(), req.kind, req.filter, user)
    return pl_mod.get_playlist(pid, user)


@router.get("/api/playlists/{pid}")
def playlists_get(pid: int, request: Request = None):
    pl = pl_mod.get_playlist(pid, auth_mod.current_user(request))
    if pl is None:
        raise HTTPException(404, "playlist not found")
    return pl


@router.patch("/api/playlists/{pid}")
def playlists_rename(pid: int, req: PlaylistUpdate, request: Request = None):
    # only the fields the client actually sent (icon: null CLEARS the icon)
    fields = {k: getattr(req, k) for k in req.model_fields_set}
    user = auth_mod.current_user(request)
    if not pl_mod.update_playlist(pid, fields, user):
        raise HTTPException(404, "playlist not found")
    return pl_mod.get_playlist(pid, user)


@router.delete("/api/playlists/{pid}")
def playlists_delete(pid: int, request: Request = None):
    if not pl_mod.delete_playlist(pid, auth_mod.current_user(request)):
        raise HTTPException(404, "playlist not found")
    return {"ok": True}


@router.post("/api/playlists/{pid}/tracks")
def playlists_add(pid: int, req: PlaylistTracks, request: Request = None):
    user = auth_mod.current_user(request)
    if pl_mod.get_playlist(pid, user) is None:
        raise HTTPException(404, "playlist not found")
    paths = [os.path.normpath(p) for p in req.paths]
    for p in paths:
        if not _in_music_folder(p, _music_folder()):
            raise HTTPException(400, f"file outside music folder: {p}")
    n = pl_mod.add_tracks(pid, paths, req.position, user)
    return {"added": n}


@router.put("/api/playlists/{pid}/tracks")
def playlists_order(pid: int, req: PlaylistTracks, request: Request = None):
    """Full reorder: body paths replace the playlist order entirely."""
    user = auth_mod.current_user(request)
    if pl_mod.get_playlist(pid, user) is None:
        raise HTTPException(404, "playlist not found")
    paths = [os.path.normpath(p) for p in req.paths]
    for p in paths:
        if not _in_music_folder(p, _music_folder()):
            raise HTTPException(400, f"file outside music folder: {p}")
    pl_mod.set_order(pid, paths, user)
    return {"ok": True}


@router.delete("/api/playlists/{pid}/tracks")
def playlists_remove(pid: int, req: PlaylistTracks, request: Request = None):
    user = auth_mod.current_user(request)
    if pl_mod.get_playlist(pid, user) is None:
        raise HTTPException(404, "playlist not found")
    pl_mod.remove_tracks(pid, [os.path.normpath(p) for p in req.paths], user)
    return {"ok": True}


@router.post("/api/playlists/{pid}/filter")
def playlists_filter(pid: int, req: SmartFilter, request: Request = None):
    user = auth_mod.current_user(request)
    pl = pl_mod.get_playlist(pid, user)
    if pl is None:
        raise HTTPException(404, "playlist not found")
    if pl["kind"] != "smart":
        raise HTTPException(400, "not a smart playlist")
    pl_mod.set_smart_filter(pid, req.filter, user)
    return pl_mod.get_playlist(pid, user)


@router.post("/api/playlists/{pid}/evaluate")
def playlists_evaluate(pid: int, request: Request = None):
    user = auth_mod.current_user(request)
    pl = pl_mod.get_playlist(pid, user)
    if pl is None:
        raise HTTPException(404, "playlist not found")
    if pl["kind"] != "smart":
        raise HTTPException(400, "not a smart playlist")
    library = lib_mod.build_library(load_config())
    hits = pl_mod.evaluate_smart(pid, library, user=user)
    return {"paths": hits or []}


@router.get("/api/playlists/{pid}/export")
def playlists_export(pid: int, request: Request = None):
    user = auth_mod.current_user(request)
    content = pl_mod.export_m3u8(pid, user)
    if content is None:
        raise HTTPException(404, "playlist not found")
    pl = pl_mod.get_playlist(pid, user)
    name = re_safe_filename(pl["name"]) or "playlist"
    return PlainTextResponse(
        content,
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{name}.m3u8"'},
    )


@router.post("/api/playlists/import")
async def playlists_import(name: str = Query(...), file: UploadFile = File(...),
                           request: Request = None):
    content = (await file.read()).decode("utf-8", errors="replace")
    base = load_config().get("music_folder") or os.getcwd()
    user = auth_mod.current_user(request)
    pid = pl_mod.import_m3u8(name.strip() or file.filename or "imported", content, base, user)
    return pl_mod.get_playlist(pid, user)
