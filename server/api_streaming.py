"""Import a playlist from a streaming service — the HTTP surface.

One route:

  ``POST /api/playlists/import/streaming`` — body
  ``{url, name?, service?, parent_albums?, dry_run?}``. Reads the playlist with
  ``server.streaming_playlists.fetch`` (Deezer's public API, Spotify's Web API
  with the app's own client-credentials token, YouTube Music through the
  app's own yt-dlp probe, Apple Music's page JSON), matches every track against
  the library, and answers the REPORT first and the created playlist second.

  ``dry_run`` is the same read and the same match with no write at all: no
  playlist, no queue — what the dialog's "Check" button asks for.

The fetching, matching and writing all live in ``server.streaming_playlists``;
this module owns the request shape, the session's user scope and the HTTP error
a refusal becomes. The router is registered by server/main.py
(``include_router``); this module never imports it.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from mlo.config import load_config
from server.auth import current_user
from server import streaming_playlists as sp

router = APIRouter()


class StreamingImportRequest(BaseModel):
    """A streaming playlist to import.

    `service` names the service the caller believes the URL belongs to
    ("deezer", "spotify", "youtube", "apple"); empty lets the URL decide. A URL
    that belongs to another service is refused with a sentence saying so, not
    read as the wrong thing.

    `name` is the playlist name to create; empty uses the service's own
    playlist title. `parent_albums` overrides `playlist_import_parent_albums`
    for this one import (None = the configured value). `dry_run` matches and
    reports without creating or queueing anything.
    """
    url: str = ""
    name: str = ""
    service: str = ""
    parent_albums: Optional[bool] = None
    dry_run: bool = False


@router.post("/api/playlists/import/streaming")
def playlists_import_streaming(req: StreamingImportRequest, request: Request = None):
    """Import one streaming playlist into the app's own playlists.

    Every refusal (a URL no service here reads, a service whose credentials are
    not set, a Spotify 404, an Apple page that changed) is a 400 or 502 whose
    detail is the service's own sentence — see
    ``server.streaming_playlists.StreamingError``.
    """
    user = current_user(request)
    try:
        return sp.import_playlist(
            req.url, req.name, load_config(), service=req.service, user=user,
            parent_albums=req.parent_albums, dry_run=req.dry_run)
    except sp.StreamingError as e:
        raise HTTPException(400, str(e))
