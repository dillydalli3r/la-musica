"""Add to library — one button that puts a MusicBrainz entity in the library.

The album the user asks for exists the moment they ask for it: this route
creates the FRAMEWORK album on disk (``server.pending_albums``: the folder the
naming script names, the release's tracklist as its manifest, the release-group
cover, a pending marker) and saves the release on the EXISTING wish queue, which
the existing worker searches (``server.wishes_worker`` → ``server.soulseek_auto``).
Nothing here is a second pipeline: the search, the download, the verify and the
import all belong to the wish queue that already had them.

A framework album is visibly pending until its audio arrives (the library scan
reads the marker), the import that fills it clears the marker and the
placeholder cover (``server.pending_albums.clear_if_filled``), and cancelling
takes the folder with the wish.

The router is registered by server/main.py (``include_router``); this module
never imports it.
"""
import os
import threading
import traceback
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mlo.config import load_config

router = APIRouter()

_KINDS = ("auto", "release", "release_group", "artist", "recording")


class AddToLibraryRequest(BaseModel):
    """A MusicBrainz entity to add. `mbid` is the id or a musicbrainz.org URL,
    of whatever kind `kind` says (or of whatever kind it turns out to be when
    `kind` is "auto"). `release_mbid` names the release a RECORDING belongs to,
    or the EDITION the caller picked inside a release GROUP — the release
    page's own rows always know it, and passing it saves the lookup that would
    otherwise pick an edition by policy (a pick naming an edition of ANOTHER
    group is reported as skipped, never added)."""
    mbid: str = ""
    kind: str = "auto"
    mode: str = "best"          # release_group: "best" edition | "all" editions
    release_mbid: Optional[str] = None
    title: str = ""
    artist: str = ""
    year: str = ""
    queries: Optional[List[str]] = None


class CancelAddRequest(BaseModel):
    """Undo an "Add to library": the wish and its framework folder."""
    album_path: str = ""
    wish_id: Optional[int] = None


def _recording_targets(recording_mbid, release_mbid=""):
    """([{mbid,title}], skipped) for a recording: the release it belongs to.

    The release page's track rows carry their release id, so that is used
    directly; a bare recording id is browsed and the auto-import release policy
    picks the edition — the same policy a release group goes through.
    """
    from server import integrations as intg

    rid = intg._mbid(release_mbid)
    if rid:
        return [{"mbid": rid, "title": ""}], []
    node = intg.recording_browse(recording_mbid, limit=100, offset=0)
    rows = node.get("releases") or []
    if not rows:
        return [], [{"mbid": recording_mbid,
                     "reason": "MusicBrainz knows no release carrying this recording"}]
    best = intg.pick_release(rows) or rows[0]
    return [{"mbid": best.get("id"), "title": best.get("title") or ""}], []


def _intended_kind(mbid, kind):
    """The entity kind to resolve *mbid* as, detecting it when not stated."""
    from server import integrations as intg

    kind = str(kind or "auto").strip().lower()
    if kind in ("", "auto"):
        kind = intg._kind_for(mbid) or "release"
    if kind not in _KINDS:
        raise HTTPException(400, "kind must be one of " + ", ".join(_KINDS))
    return "release_group" if kind == "releasegroup" else kind


def _group_edition_targets(group_mbid, release_mbid, mode, cfg=None):
    """([{mbid,title}], skipped) when the caller picked an EDITION.

    The release-group page lists the group's pressings and the user picks one;
    without this the pick was silently dropped and the policy's own edition was
    queued instead. The chosen release is used as-is — but only after checking
    it really is an edition of the group it was sent with, so a mismatched pair
    (a stale row, a wrong link) is reported as skipped rather than quietly
    adding some other album.

    ``mode`` "all" is untouched by the pick: it still means every eligible
    edition of the group.
    """
    from server import integrations as intg

    if str(mode or "best").lower() == "all":
        return None
    rid = intg._mbid(release_mbid)
    if not rid:
        return None
    group = str(intg._mbid(group_mbid) or "").strip().lower()
    try:
        release, resolved = intg.resolve_release(rid)
    except Exception:
        release, resolved = None, rid
    rid = resolved or rid
    if not release:
        return [], [{"mbid": rid,
                     "reason": "no MusicBrainz release matches the chosen edition"}]
    belongs = str(release.get("release_group_id") or "").strip().lower()
    if group and belongs and belongs != group:
        return [], [{"mbid": rid,
                     "reason": "that release is not part of this release group"}]
    # Same owned check the policy path pays, so a picked edition already in the
    # library is reported the same way instead of being added twice.
    try:
        from mlo.config import load_config
        from server import wishes
        owned = wishes.owned_mbids(cfg or load_config())
        if rid.lower() in owned or (belongs and belongs in owned):
            return [], [{"mbid": rid, "reason": "already in the library"}]
    except Exception:
        pass
    return [{"mbid": rid, "title": release.get("title") or ""}], []


def _targets(mbid, kind, mode, release_mbid="", cfg=None):
    """([{mbid,title}], skipped) — the releases to create albums for."""
    from server import integrations as intg

    if kind == "recording":
        return _recording_targets(mbid, release_mbid)
    if kind == "release_group" and release_mbid:
        picked = _group_edition_targets(mbid, release_mbid, mode, cfg)
        if picked is not None:
            return picked
    return intg.auto_import_targets(mbid, kind, mode)


def _create_all(targets, cfg, *, queries=None, title="", artist="", year=""):
    """Create one framework album + wish per target release.

    The album and its wish are the RECORD of what the user asked for and are
    created either way; the SEARCH is what `auto_acquisition_enabled` owns. Off,
    no download is started and the wish sits in the queue until the user runs
    it by hand (its own Search now), which is the whole difference between
    "not recorded" and "not started".
    """
    from mlo import import_policy
    from server import integrations as intg
    from server import pending_albums, wishes_worker

    auto = import_policy.auto_acquisition_enabled(cfg)
    albums, errors = [], []
    for t in targets:
        try:
            release, rid = intg.resolve_release(t["mbid"])
            if not release:
                errors.append({"mbid": t["mbid"], "reason": "no release matches this ID"})
                continue
            row = pending_albums.create(
                release, cfg, queries=queries,
                title=t.get("title") or title, artist=artist, year=year)
            albums.append(row)
            if auto and row.get("wish_id") and row.get("created"):
                # The existing worker searches it; nothing is searched here.
                wishes_worker.trigger(row["wish_id"])
        except Exception as e:
            traceback.print_exc()
            errors.append({"mbid": t["mbid"], "reason": str(e)})
    return albums, errors


def _prepare_artist(mbid, mode, cfg, req):
    """Resolve an artist's discography and create every framework album.

    Runs on a daemon thread: the artist path is one MusicBrainz browse per
    release group (MusicBrainz answers one request a second), so it cannot
    finish inside a request. Each album appears — and its wish starts, unless
    ``auto_acquisition_enabled`` is off — as it is prepared, and the batch
    reports itself over the event channel.
    """
    from mlo import import_policy
    from server import events

    albums, errors = [], []
    try:
        targets, skipped = _targets(mbid, "artist", mode, cfg=cfg)
        for s in skipped:
            errors.append({"mbid": s.get("mbid"), "reason": s.get("reason")})
        albums, more = _create_all(targets, cfg, queries=req.queries,
                                   title=req.title, artist=req.artist, year=req.year)
        errors.extend(more)
    except Exception as e:
        traceback.print_exc()
        errors.append({"mbid": mbid, "reason": str(e)})
    if not albums:
        body = "Nothing could be added."
    elif import_policy.auto_acquisition_enabled(cfg):
        body = "Soulseek is searching for them now."
    else:
        # The albums ARE in the library: saying nothing about the search would
        # leave the user waiting for a download nothing is doing.
        body = import_policy.AUTO_OFF_NOTE
    try:
        events.emit("library_add",
                    f"Added {len(albums)} album(s) to your library",
                    body,
                    {"mbid": mbid, "albums": [a.get("album_path") for a in albums],
                     "errors": errors[:20]}, config=cfg)
    except Exception:
        pass


def _pending_album_payload(row):
    return {"album_path": row.get("album_path"), "title": row.get("title"),
            "artist": row.get("artist"), "year": row.get("year"),
            "release_id": row.get("release_id"),
            "release_group_id": row.get("release_group_id"),
            "wish_id": row.get("wish_id"), "cover": row.get("cover"),
            "created": bool(row.get("created")),
            "already_in_library": bool(row.get("existing"))}


@router.post("/api/library/add")
def library_add(req: AddToLibraryRequest):
    """Add a MusicBrainz release / release group / artist / recording to the
    library: framework album on disk, wish on the queue, search started.

    The album is there immediately and visibly pending and the search starts
    with it — unless ``auto_acquisition_enabled`` is off, in which case the
    album and its wish are recorded and `note` says why nothing is searching.
    A batch too large for a request (an artist's discography is one MusicBrainz
    browse per release group) is prepared on a background thread and reported
    over ``/ws/events`` as ``library_add``.
    """
    from mlo import import_policy
    from server import integrations as intg

    mbid = intg._mbid(req.mbid or req.release_mbid)
    if not mbid:
        raise HTTPException(400, "a MusicBrainz ID or URL is required")
    mode = (req.mode or "best").strip().lower()
    if mode not in ("best", "all"):
        raise HTTPException(400, "mode must be 'best' or 'all'")
    kind = _intended_kind(mbid, req.kind)
    cfg = load_config()
    auto = import_policy.auto_acquisition_enabled(cfg)

    if kind == "artist":
        threading.Thread(target=_prepare_artist, args=(mbid, mode, cfg, req),
                         daemon=True).start()
        return {"ok": True, "background": True,
                "albums": [], "skipped": [], "errors": [],
                "note": ("Preparing the discography — each album appears in the "
                         "library as it is added, and the search starts with it."
                         if auto else
                         "Preparing the discography — each album appears in the "
                         "library as it is added. " + import_policy.AUTO_OFF_NOTE)}

    try:
        targets, skipped = _targets(mbid, kind, mode, req.release_mbid or "", cfg=cfg)
    except Exception as e:
        # A MusicBrainz outage is not "nothing matches".
        raise HTTPException(503, f"MusicBrainz did not answer: {e}")

    albums, errors = _create_all(targets, cfg, queries=req.queries,
                                 title=req.title, artist=req.artist, year=req.year)
    return {"ok": True, "background": False,
            "albums": [_pending_album_payload(a) for a in albums],
            "skipped": skipped, "errors": errors,
            "note": "" if auto else import_policy.AUTO_OFF_NOTE}


@router.post("/api/library/add/cancel")
def library_add_cancel(req: CancelAddRequest):
    """Undo an "Add to library": the wish goes, and its framework folder with
    it. A folder whose audio has already arrived is a real album by then, so it
    stays — only the placeholder is ever deleted here."""
    from mlo.paths import load_pending
    from server import pending_albums, wishes

    wid = req.wish_id
    folder = str(req.album_path or "").strip()
    if wid is None and folder:
        info = load_pending(os.path.normpath(folder))
        if info:
            wid = info.get("wish_id")
    if wid is None and not folder:
        raise HTTPException(400, "album_path or wish_id is required")
    removed = pending_albums.remove_for_wish(wid) if wid is not None else \
        pending_albums.remove_folder(folder)
    deleted = wishes.delete_wish(wid) if wid is not None else False
    if not removed and not deleted:
        raise HTTPException(404, "no pending album or wish to cancel")
    return {"ok": True, "removed": removed, "wish_deleted": deleted,
            "wish_id": wid}
