"""Lyrics routes — the provider chain and its per-track auto-import.

Script 13 writes lyrics for a whole library; this router exposes the same
engine call for one track (the track page's "Auto-import lyrics" button) so a
click and a script run produce byte-identical files. Provider order and the
plain-vs-synced policy come from the config the settings page edits.
"""
import os

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from mlo import load_config
from mlo.lyrics_fetch import fetch_one
from mlo.lyrics_providers import SOURCES, SOURCE_LABELS, SOURCE_NOTES, available_sources, provider_order
from server import tagcache

router = APIRouter(tags=["lyrics"])


class LyricsAutoRequest(BaseModel):
    paths: list[str] = []
    force: bool = False


# Each path runs the whole provider chain (up to four providers, each with
# retries), and the handler is synchronous, so it occupies an API worker
# thread. Cap the batch: a whole-album or whole-library fetch belongs to
# script 13 (`POST /api/run`), which reports progress and can be cancelled.
MAX_PATHS = 100


@router.get("/api/lyrics/providers")
def lyrics_providers():
    """Selectable lyrics sources, the saved order, and the plain-lyrics policy."""
    cfg = load_config()
    return {
        "sources": available_sources(),
        "default_order": list(SOURCES),
        "order": provider_order(cfg),
        "saved": cfg.get("lyrics_sources") or [],
        "allow_plain": bool(cfg.get("lyrics_allow_plain", True)),
        "labels": dict(SOURCE_LABELS),
        "notes": dict(SOURCE_NOTES),
    }


@router.post("/api/lyrics/auto")
def lyrics_auto(req: LyricsAutoRequest):
    """Auto-import lyrics for one or more tracks through the provider chain.

    Falls back provider by provider (LRCLIB → NetEase → lyrics.ovh → Kugou by
    default), then writes per `lyrics_format` exactly like script 13. A track
    that already has lyrics is left alone unless *force* is set, and an
    INSTRUMENTAL track is never touched.
    """
    from server.main import _in_music_folder, _music_folder
    cfg = load_config()
    folder = _music_folder(cfg)
    if len(req.paths) > MAX_PATHS:
        raise HTTPException(
            413, f"too many paths in one request ({len(req.paths)} > {MAX_PATHS}) — "
                 f"run script 13 (Fetch lyrics) for a whole album or library")
    results = []
    for path in req.paths:
        full = os.path.normpath(path)
        if not folder or not _in_music_folder(full, folder):
            results.append({"path": path, "status": "failed",
                            "error": "path outside music folder"})
            continue
        if not os.path.isfile(full):
            results.append({"path": path, "status": "failed", "error": "file not found"})
            continue
        res = fetch_one(full, cfg, force=bool(req.force))
        res["path"] = path
        results.append(res)
    if any(r.get("status") == "ok" for r in results):
        tagcache.invalidate_all()
    return {
        "results": results,
        "order": provider_order(cfg),
        "ok": sum(1 for r in results if r.get("status") == "ok"),
        "skipped": sum(1 for r in results if r.get("status") == "skipped"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
    }


@router.get("/api/lyrics/find")
def lyrics_find(artist: str = Query(""), title: str = Query(""),
                album: str = Query(""), duration: float = Query(0.0)):
    """Look lyrics up in the chain without writing anything (the lyrics
    manager's "search online" box)."""
    from mlo.lyrics_providers import fetch_lyrics
    cfg = load_config()
    if not title:
        raise HTTPException(400, "title is required")
    hit = fetch_lyrics(cfg, artist, title, album or None, duration or None)
    if not hit:
        return {"found": False, "order": provider_order(cfg)}
    return {"found": True, "order": provider_order(cfg), **hit}
