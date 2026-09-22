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
    group is reported as skipped, never added).

    `types` restricts an ARTIST (or release-group) add to MusicBrainz's own
    release-group types — "album", "compilation", or a combined spelling like
    "Album + Compilation" — matched by `mlo.release_choice.type_matches`. It
    is empty by default, and empty means EVERY type: the behaviour every
    caller had before the filter existed.

    `download` is ACCEPTED AND IGNORED, kept so a client that still sends it
    is not refused. It used to be the difference between "Add to library"
    (record it, the queue's next pass picks it up — up to a tick later) and
    "Download all" (record it and start searching now); an add now always
    starts the search for what it just recorded (see `_create_all`), so there
    is nothing left for the flag to decide.
    """
    mbid: str = ""
    kind: str = "auto"
    mode: str = "best"          # release_group: "best" edition | "all" editions
    release_mbid: Optional[str] = None
    types: List[str] = []
    download: bool = False
    title: str = ""
    artist: str = ""
    year: str = ""
    queries: Optional[List[str]] = None


class CancelAddRequest(BaseModel):
    """Undo an "Add to library": the wish and its framework folder."""
    album_path: str = ""
    wish_id: Optional[int] = None


def _unless_owned(targets, cfg=None):
    """(kept, skipped) — targets the library does NOT already hold.

    The paths that go through `integrations.auto_import_targets` are already
    answered by the policy and a picked edition pays the same check; these two
    do not, and an add for a release the library holds is a framework album
    NOTHING will ever fill: the pipeline refuses to download an album it
    already has, so the empty folder would sit in the library for good. The
    answer is the same one the policy gives — a skipped row saying why.
    """
    from mlo.config import load_config
    from server import wishes

    try:
        owned = wishes.owned_mbids(cfg or load_config())
    except Exception:
        return list(targets), []       # a library that cannot be read: unchanged
    kept, skipped = [], []
    for t in targets:
        if str(t.get("mbid") or "").strip().lower() in owned:
            skipped.append({"mbid": t.get("mbid"), "reason": "already in the library"})
        else:
            kept.append(t)
    return kept, skipped


def _recording_targets(recording_mbid, release_mbid="", cfg=None):
    """([{mbid,title}], skipped) for a recording: the release it belongs to.

    The release page's track rows carry their release id, so that is used
    directly; a bare recording id is browsed and the auto-import release policy
    picks the edition — the same policy a release group goes through.
    """
    from server import integrations as intg

    rid = intg._mbid(release_mbid)
    if rid:
        return _unless_owned([{"mbid": rid, "title": ""}], cfg)
    node = intg.recording_browse(recording_mbid, limit=100, offset=0)
    rows = node.get("releases") or []
    if not rows:
        return [], [{"mbid": recording_mbid,
                     "reason": "MusicBrainz knows no release carrying this recording"}]
    best = intg.pick_release(rows) or rows[0]
    return _unless_owned([{"mbid": best.get("id"),
                           "title": best.get("title") or ""}], cfg)


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


def _targets(mbid, kind, mode, release_mbid="", cfg=None, types=None):
    """([{mbid,title}], skipped) — the releases to create albums for.

    `types` (MusicBrainz's own release-group types, `mlo.release_choice`'s
    vocabulary) is a filter on what may be queued, and it rides all the way
    into the policy's own edition choice — an add for "album" is answered with
    the album edition. A caller who picked an EDITION (`release_group` with
    `release_mbid`) has already named the release, so the pick wins and no
    type filter is applied to it.
    """
    from server import integrations as intg

    if kind == "recording":
        return _recording_targets(mbid, release_mbid, cfg)
    if kind == "release_group" and release_mbid:
        picked = _group_edition_targets(mbid, release_mbid, mode, cfg)
        if picked is not None:
            return picked
    # No per-call bound: `auto_import_targets`'s default caps how many of an
    # artist's release groups ONE call expands, which is the right shape for a
    # request that has to answer. Every artist add that reaches this line runs
    # on the background prepare instead (`_prepare_artist`), which exists
    # precisely because one MusicBrainz browse per group takes minutes — so a
    # bound there truncated the row the button named: "Album + Compilation ·
    # 106 release group(s)" prepared 50 and reported the other 56 as "per-call
    # limit reached — call again" for an action the user had already asked for
    # in full. The row is the unit; it is expanded whole.
    return intg.auto_import_targets(mbid, kind, mode, types=types, limit=None)


def _create_all(targets, cfg, *, queries=None, title="", artist="", year=""):
    """Create one framework album + wish per target release, and start the
    search for what this call created.

    The album and its wish are the RECORD of what the user asked for; the
    SEARCH is started here, by the one queue this app has — `wishes_worker`'s
    own pass, never a second queue beside it. A plain "Add to library" used to
    only record and leave the searching to the worker's next tick, which can be
    two minutes away with nothing on screen in between; it now kicks that pass
    at the end of the call, so the release is being searched for as the reply
    is written.

    The kick names no wish: the pass is the worker's OWN, so it searches every
    wish that is due by its own policy (`wishes.due_at` — the interval, or a
    retry's backoff, whichever is later). A brand-new wish is due immediately
    and is picked up on this pass; a release that already failed and is waiting
    out its backoff keeps that wait, which is what stops a re-add from spending
    another attempt on a network that just answered. ONE kick covers the whole
    call — an artist's dozens of albums start on the one pass, not one pass
    each.

    With `auto_acquisition_enabled` off NOTHING is started: the wish sits on
    the queue until the user runs it by hand (its own Search now), which is the
    whole difference between "not recorded" and "not started".
    """
    from mlo import import_policy
    from server import integrations as intg
    from server import pending_albums, wishes_worker

    auto = import_policy.auto_acquisition_enabled(cfg)
    # One album is pre-created INLINE (the user is waiting for exactly that one
    # and its page is a click away); a batch — a discography, mode="all" — has
    # its page content fetched per album on a daemon thread instead, so one
    # request does not become dozens of provider calls (see
    # `pending_albums.create`'s own `prefetch` argument).
    batch = len(targets) > 1
    albums, errors = [], []
    recorded = False
    for t in targets:
        try:
            release, rid = intg.resolve_release(t["mbid"])
            if not release:
                errors.append({"mbid": t["mbid"], "reason": "no release matches this ID"})
                continue
            row = pending_albums.create(
                release, cfg, queries=queries,
                title=t.get("title") or title, artist=artist, year=year,
                prefetch=not batch)
            albums.append(row)
            if batch and row.get("created") and row.get("album_path"):
                pending_albums.prefetch_content(row["album_path"], cfg, background=True)
            recorded = recorded or bool(row.get("created") and row.get("wish_id"))
        except Exception as e:
            traceback.print_exc()
            errors.append({"mbid": t["mbid"], "reason": str(e)})
    if recorded and auto:
        # The worker's own pass, started now: it reads the wish store itself,
        # so what it searches (and what it leaves to its own retry) is the
        # store's decision and not this call's.
        wishes_worker.trigger()
    return albums, errors


def _prepare_artist(mbid, mode, cfg, req, types=None):
    """Resolve an artist's discography and create every framework album.

    Runs on a daemon thread: the artist path is one MusicBrainz browse per
    release group (MusicBrainz answers one request a second), so it cannot
    finish inside a request. Each album appears as it is prepared, and the
    batch's one kick starts the search for all of them (see `_create_all`),
    reporting itself over the event channel.

    `types` is the call's own type filter (see `AddToLibraryRequest`): the
    groups it leaves out are reported as skipped, by `auto_import_targets`.
    """
    from mlo import import_policy
    from server import events

    albums, errors = [], []
    try:
        targets, skipped = _targets(mbid, "artist", mode, cfg=cfg, types=types)
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
    elif not import_policy.auto_acquisition_enabled(cfg):
        # The albums ARE in the library: saying nothing about the search would
        # leave the user waiting for a download nothing is doing.
        body = import_policy.AUTO_OFF_NOTE
    else:
        body = "Soulseek is searching for them now."
    if errors:
        body = f"{body} {len(errors)} release group(s) skipped."
    try:
        events.emit("library_add",
                    f"Added {len(albums)} album(s) to your library",
                    body,
                    {"mbid": mbid, "types": list(types or ()),
                     "albums": [a.get("album_path") for a in albums],
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


def _types_filter(types):
    """The request's `types` as MusicBrainz's own type SELECTIONS, or 400.

    `mlo.release_choice.type_names` is the ONE normalizer: it lowercases,
    validates every part against the published vocabulary — so a typo comes
    back as the name it did not recognize instead of a filter that silently
    matches nothing — and keeps each selection WHOLE. "Album + Compilation"
    therefore reaches the matcher as "album + compilation" and means an album
    that IS a compilation (`mlo.release_choice.type_matches`), not "album or
    compilation", which is what the artist page's own row would otherwise
    queue: the plain albums, and every compilation of another type.
    """
    from mlo import release_choice

    try:
        return release_choice.type_names(types)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _types_label(types):
    """"Album + Compilation" — a type selection as MusicBrainz spells types."""
    from mlo import naming

    return naming.mb_style_release_type("+".join(types)).replace("; ", " + ")


def _type_scope(mbid, types):
    """What a type-filtered ARTIST add is about to queue, before it starts.

    ONE browse of the artist's release groups — `mb_get_cached`, so the page
    that sent this request has just paid for it — answers the two things the
    handover owes the user: how many release groups the filter keeps (one
    album is prepared for each) and every group it leaves out, with the type
    that group actually is. Raises when MusicBrainz cannot answer, which the
    route reports as an outage rather than as "no albums".
    """
    from server import integrations as intg

    groups = intg.artist_release_groups(mbid, limit=300, offset=0).get("release_groups") or []
    kept, skipped = intg.groups_of_types(groups, types)
    return {"queued": len(kept), "skipped": skipped, "total": len(groups)}


def _artist_note(queued, label, auto):
    """What a handover says while the albums are still being prepared."""
    from mlo import import_policy

    scope = f"{queued} {label} release group(s)" if label else "the discography"
    head = f"Preparing {scope} — each album appears in the library as it is added"
    if not auto:
        return head + ". " + import_policy.AUTO_OFF_NOTE
    return head + ", and the search starts with it."


def _added_note(auto):
    """What an in-request add answers about the SEARCH it did or did not start.

    One sentence for every add: it started the search for what it recorded (see
    `_create_all`), so "Add to library" and "Download all" answer the same
    thing. Off, the switch's own sentence says nothing is searching — the album
    and its wish are still recorded either way.
    """
    from mlo import import_policy

    if not auto:
        return import_policy.AUTO_OFF_NOTE
    return "Soulseek is searching for them now."


@router.post("/api/library/add")
def library_add(req: AddToLibraryRequest):
    """Add a MusicBrainz release / release group / artist / recording to the
    library: framework album on disk, wish on the queue, and the search for it
    started.

    The album is there immediately and visibly pending, and the acquisition
    starts as the reply is written — the add path kicks the worker's own pass
    rather than waiting up to a tick for it (see `_create_all`). With
    `auto_acquisition_enabled` off nothing is started and `note` says so in
    that switch's own words: the album and its wish are still recorded.

    An artist's discography is prepared on a background thread and reported
    over ``/ws/events`` as ``library_add`` (one MusicBrainz browse per release
    group does not fit in a request); a `types`-filtered artist call answers
    at once with what it is queueing and what the filter left out, because
    that much is known from one browse the caller has already made.
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
    types = _types_filter(req.types)
    cfg = load_config()
    auto = import_policy.auto_acquisition_enabled(cfg)

    if kind == "artist":
        scope = {"queued": None, "skipped": [], "total": None}
        if types:
            try:
                scope = _type_scope(mbid, types)
            except Exception as e:
                raise HTTPException(503, f"MusicBrainz did not answer: {e}")
        if scope["queued"] == 0:
            # No group of this artist has the type that was asked for: nothing
            # is prepared, and the skipped rows say which types there are.
            return {"ok": True, "background": False, "queued": 0,
                    "albums": [], "skipped": scope["skipped"], "errors": [],
                    "note": ("No release group of this artist is one of the "
                             "types you asked for." if scope["total"] else
                             "This artist has no release groups on MusicBrainz.")}
        threading.Thread(target=_prepare_artist, args=(mbid, mode, cfg, req, types),
                         daemon=True).start()
        return {"ok": True, "background": True, "queued": scope["queued"],
                "albums": [], "skipped": scope["skipped"], "errors": [],
                "note": _artist_note(scope["queued"], _types_label(types), auto)}

    try:
        targets, skipped = _targets(mbid, kind, mode, req.release_mbid or "",
                                    cfg=cfg, types=types)
    except Exception as e:
        # A MusicBrainz outage is not "nothing matches".
        raise HTTPException(503, f"MusicBrainz did not answer: {e}")

    albums, errors = _create_all(targets, cfg, queries=req.queries,
                                 title=req.title, artist=req.artist, year=req.year)
    return {"ok": True, "background": False, "queued": len(targets),
            "albums": [_pending_album_payload(a) for a in albums],
            "skipped": skipped, "errors": errors,
            "note": _added_note(auto)}


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
