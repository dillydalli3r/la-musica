"""Add to library — one button that puts a MusicBrainz entity in the library.

The album the user asks for exists the moment they ask for it: this route
creates the FRAMEWORK album on disk (``server.pending_albums``: the folder the
naming script names, the release's tracklist as its manifest, the release-group
cover, a pending marker) so the album is listed with its track list while its
audio is still missing. The user fills the folder with the audio themselves.

A framework album is visibly pending until its audio arrives (the library scan
reads the marker), and the import that fills it clears the marker and the
placeholder cover (``server.pending_albums.clear_if_filled``). Cancelling takes
the folder away.

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

    An add may also name the release instead of identifying it: with no `mbid`
    and no `release_mbid`, `title` + `artist` are all it takes, and the route
    searches MusicBrainz for them (see `_name_match`). That is for the rows
    that carry no id at all — a streaming recommendation — and `kind` there
    says which of them it is: "album" (a release group is searched), "track"
    (the recording), or "artist".
    """
    mbid: str = ""
    kind: str = "auto"
    mode: str = "best"          # release_group: "best" edition | "all" editions
    release_mbid: Optional[str] = None
    types: List[str] = []
    title: str = ""
    artist: str = ""
    year: str = ""
    queries: Optional[List[str]] = None


class CancelAddRequest(BaseModel):
    """Undo an "Add to library": its framework folder goes."""
    album_path: str = ""


def _unless_owned(targets, cfg=None):
    """(kept, skipped) — targets the library does NOT already hold.

    The paths that go through `integrations.auto_import_targets` are already
    answered by the policy and a picked edition pays the same check; these two
    do not, and an add for a release the library holds is a framework album
    NOTHING will ever fill: the empty folder would sit in the library for good.
    The answer is the same one the policy gives — a skipped row saying why.
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


def _create_all(targets, cfg, *, queries=None, title="", artist="", year="",
                deferred=None):
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

    NO album has its page content fetched INLINE — not even the single one a
    "Add to library" press is about. Measured on a real add by release id
    (`.pi/profile_add.py`, case b): 10-20 s of the request's ~11-21 s were
    `pending_albums.prefetch_content`'s provider calls (`cover_search` ~3-8 s,
    the RYM link lookup ~2.7 s, MusicBrainz metadata ~3-5 s) — content that
    is only ever read when the user OPENS the album's page, and that the
    reply does not need: the folder, its manifest, its cover and its wish are
    already on disk by then, which is what the album row shows. So every
    album's page content goes to `prefetch_content(background=True)` — the
    same call the batch caller already used — and the request answers with
    the record it wrote.
    """
    from mlo import import_policy
    from server import integrations as intg
    from server import pending_albums, wishes_worker

    auto = import_policy.auto_acquisition_enabled(cfg)
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
                prefetch=False,
                # The ranked fallback list (spec R150) the target already
                # carries from `integrations.group_targets`: the release
                # group's eligible editions, best first. Recording it is what
                # makes the wish's search walk PAST a pressing that has nothing
                # usable instead of ending at the one edition this add
                # resolved. A target with none (an add that named ONE release)
                # keeps the single-candidate behavior it always had.
                candidates=t.get("candidates") or None,
                # The FIRST album this resolution creates takes over the
                # framework album the add already put on disk (`deferred`);
                # every later one (mode="all") gets its own folder, exactly as
                # it always has. A target that fails leaves `albums` empty, so
                # the next one adopts instead — the placeholder is the ADD's,
                # not one edition's.
                deferred=deferred if not albums else None)
            albums.append(row)
            if row.get("created") and row.get("album_path"):
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
    finish inside a request. Each album appears as it is prepared, reporting
    itself over the event channel.

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
    elif all(a.get("existing") for a in albums if a):
        # Every group this call touched is one the library already holds: the
        # one honest sentence is that nothing was added.
        body = _already_note()
    else:
        body = "Each album is in your library — add its audio when you have it."
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


def _prepare_add(mbid, kind, mode, release_mbid, cfg, req, deferred, types=None):
    """Resolve a deferred add's MusicBrainz identity and finish its album.

    Runs on a daemon thread, exactly like the artist path: the reply already
    carries the framework album and the wish the REQUEST could name
    (`pending_albums.create_from_request`), and what is left is the lookup that
    used to hold the button down — a browse plus an edition resolution per
    release group, tens of seconds on MusicBrainz's own one-request-a-second
    budget. The resolution itself is unchanged (`_targets` + `_create_all`),
    and one kick starts the search for everything it recorded.

    It ends in exactly one of three places, and none of them leaves the
    placeholder claiming to be an album nothing will fill:

    * recorded — the created album adopts the placeholder folder, which is the
      name the release really has (`pending_albums.finish_deferred`);
    * the library already holds the release — the placeholder goes and the wish
      is marked imported AT the album that is really there, so the queue row
      states the truth instead of promising a download;
    * MusicBrainz could not answer, or has nothing — the placeholder goes and
      the wish keeps its place on the queue WITH the reason, so the user sees
      what happened and the search can still be run from the row.

    A successful resolution says nothing more: the frame the press already sent
    ("Asking MusicBrainz what this release is…") is followed by the album being
    in the library and the search running. The two ends that could not record an
    album report themselves — an add that failed after its reply must not be
    silent.
    """
    from server import events, pending_albums, wishes

    albums, errors = [], []
    try:
        targets, skipped = _targets(mbid, kind, mode, release_mbid, cfg=cfg,
                                    types=types)
        for s in skipped:
            errors.append({"mbid": s.get("mbid"), "reason": s.get("reason")})
        albums, more = _create_all(targets, cfg, queries=req.queries,
                                   title=req.title, artist=req.artist or "",
                                   year=req.year, deferred=deferred)
        errors.extend(more)
    except Exception as e:
        traceback.print_exc()
        errors.append({"mbid": mbid, "reason": str(e)})

    if albums:
        return

    wid = deferred.get("wish_id")
    try:
        pending_albums.remove_for_wish(wid, cfg)    # the placeholder goes
    except Exception:
        traceback.print_exc()
    reason = next((str((e or {}).get("reason") or "") for e in errors
                   if e.get("reason")),
                  "MusicBrainz has no release to add for this request.")
    where = _owned_path(deferred, mbid, cfg)
    try:
        if wid and where:
            # The album IS in the library, so "imported" is the truth about this
            # wish — and nothing searches for what is already here.
            wishes.mark_imported(int(wid), where)
        elif wid:
            # The request stands and keeps its place on the queue, with the
            # reason on the row: whatever MusicBrainz answered, the user still
            # has a wish they can search or cancel.
            wishes.mark_wanted(int(wid), error=reason)
    except Exception:
        traceback.print_exc()
    try:
        label = f"{req.artist} — {req.title}".strip(" —")
        events.emit("library_add",
                    (f"Already in your library: {label}" if where
                     else f"Could not add: {label}"),
                    (_already_note() if where else
                     f"{reason} It is not in your library — the request stays on "
                     f"the queue."),
                    {"mbid": mbid, "wish_id": wid, "reason": reason,
                     "errors": errors[:20]}, config=cfg)
    except Exception:
        pass


def _owned_path(deferred, mbid, cfg):
    """The library folder holding this add's release, or "".

    The deferred add's own "already in your library" answer, asked of the
    LIBRARY (`wishes.owned_mbids` — the albums on disk with their own MBID tags)
    rather than of the wish: the wish is keyed by whatever id the request held,
    which is not always the id the resolution learned.
    """
    from server import wishes

    try:
        owned = wishes.owned_mbids(cfg)
    except Exception:
        return ""
    return wishes.owned_path(owned, deferred.get("release_id"),
                             deferred.get("release_group_id"), mbid)


def _pending_album_payload(row):
    return {"album_path": row.get("album_path"), "title": row.get("title"),
            "artist": row.get("artist"), "year": row.get("year"),
            "release_id": row.get("release_id"),
            "release_group_id": row.get("release_group_id"),
            "wish_id": row.get("wish_id"), "cover": row.get("cover"),
            "created": bool(row.get("created")),
            "resolving": bool(row.get("resolving")),
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


def _artist_note(queued, label):
    """What a handover says while the albums are still being prepared."""
    scope = f"{queued} {label} release group(s)" if label else "the discography"
    return (f"Preparing {scope} — each album appears in the library as it is "
            "added. Add its audio when you have it.")


def _added_note():
    """What an in-request add answers.

    One sentence for every add: the album is in the library, waiting for the
    user to add its audio.
    """
    return "Added to your library — add its audio when you have it."


def _deferred_note():
    """What an add answers when it has recorded the album but MusicBrainz has
    not answered yet."""
    return ("Added to your library — MusicBrainz is still being asked what this "
            "release is.")


def _already_note():
    """What an add answers when the library already holds the release.

    The one case where an add must NOT claim anything was added: a framework
    folder standing for an album that is already here is one nothing can fill
    (``_unless_owned``, the policy's own check), so nothing is created.
    """
    return "It is already in your library."


def _owned_skip(skipped):
    """Whether the reasons an add reported are "the library already has it"."""
    return any(str((s or {}).get("reason") or "").strip().lower() ==
               "already in the library" for s in (skipped or []))


# A row with no MusicBrainz id says what it IS in the provider's own words, and
# these are those words → the entity MusicBrainz has to be asked about. An
# unknown/absent kind is an ALBUM: a name-only row is a release or a track (a
# track never arrives without its artist), and an album is what a release-group
# search answers.
_NAME_KINDS = {"album": "release_group", "release_group": "release_group",
               "release": "release", "track": "recording",
               "recording": "recording", "artist": "artist"}
_NAME_ENTITIES = {"release_group": "release-group", "recording": "recording",
                  "artist": "artist", "release": "release"}


def _name_match(stated, req):
    """(mbid, kind) for an add that named its release instead of identifying
    it, or ("", "") when MusicBrainz has nothing that matches.

    One search, for what the row actually says: an album is a release GROUP
    (the editions come later, exactly as they do for an id-given add — the
    policy picks one, `mode="all"` takes every one), a track is the RECORDING
    it names, and an artist row is the artist. The provider's `year` is a hint
    and not a filter: it is preferred among the rows MusicBrainz returns
    (the index's own relevance order decides otherwise) and never narrows the
    query, because a provider's date is of the release it lists — asking
    MusicBrainz to match it exactly is how a real album comes back as "no
    match". The type the row names is a hint for the same reason, one step
    ahead of the year (see `wanted` below).

    Raises whatever the search raises: the route says the difference between
    "MusicBrainz does not have it" and "MusicBrainz did not answer".
    """
    from server import integrations as intg
    from mlo import release_choice

    kind = _NAME_KINDS.get(str(stated or "").strip().lower(), "release_group")
    entity = _NAME_ENTITIES.get(kind) or "release-group"
    title = str(req.title or "").strip()
    artist = str(req.artist or "").strip()
    # An artist row puts the name in either field; a release/recording search
    # takes the album or the track title as its free text.
    query = artist if kind == "artist" else title
    if not query:
        query = artist
    if not query:
        raise HTTPException(400, "an artist or a title is required")
    rows = intg.search_mb(entity, query, limit=5,
                          artist="" if kind == "artist" else artist,
                          year="" if kind == "artist" else str(req.year or "").strip())
    found = rows.get("rows") or []
    if not found:
        return "", ""
    year = str(req.year or "").strip()[:4]
    # WHAT the row is LOOKING for, as a release-group type: the caller's own
    # `types` selection when it made one, else the kind it stated — "album" is
    # what a Discover row says about the album it names.
    #
    # A PREFERENCE among the rows MusicBrainz returned, never a filter on the
    # query. The index lists same-named release groups of every type, and its
    # own order for "All Hope Is Gone" by Slipknot puts the 1-track DIGITAL
    # SINGLE above the 14-track album: an album row searched, the single
    # answered, and the framework album that landed carried one track — the
    # album's own title (owner report). A preference cannot lose a match the
    # old code would have found: a row that names no type, or a search where
    # nothing states the wanted type, falls through to the year hint and then
    # to the provider's own order, exactly as it did.
    wanted = _types_filter(req.types) if req.types else []
    if not wanted:
        try:
            wanted = release_choice.type_names([stated])
        except ValueError:
            wanted = []          # this kind names no release type: no leaning

    def rank(row):
        typ = release_choice.type_matches(row.get("primary_type") or row.get("release_type") or "",
                                          row.get("secondary_types"),
                                          wanted)
        date = str(row.get("first_release_date") or row.get("date") or "")
        return (0 if typ else 1, 0 if (year and date[:4] == year) else 1)

    best = min(found, key=rank)      # `min` keeps the provider's order on a tie
    return str(best.get("id") or ""), kind


@router.post("/api/library/add")
def library_add(req: AddToLibraryRequest):
    """Add a MusicBrainz release / release group / artist / recording to the
    library: a framework album on disk so the album is listed while its audio
    is still missing.

    An artist's discography is prepared on a background thread and reported
    over ``/ws/events`` as ``library_add`` (one MusicBrainz browse per release
    group does not fit in a request); a `types`-filtered artist call answers
    at once with what it is preparing and what the filter left out, because
    that much is known from one browse the caller has already made.

    Every OTHER add answers the same way whenever the request itself is enough
    to name the album: `pending_albums.create_from_request` writes the framework
    album from the title, the artist and the year it already holds, and the
    release lookup runs on a daemon thread (`_prepare_add`). The caller that
    gave only an id (a bare MBID or URL — the web's discovery rows, an old
    client) still pays the resolution inside the request, because there is
    nothing to name the folder with until MusicBrainz answers; `resolving` in
    the reply says which of the two happened, and the album row carries the
    (live) flag of the same name while its identity is still being resolved.

    A request with NO id at all is an add that named its release (`title` +
    `artist`, and `kind` saying which of them it is): one MusicBrainz search
    (`_name_match`) either finds the entity — the reply carries
    ``"matched": true`` and the add continues exactly as an id-given one — or it
    does not, and the add reports that nothing was added (there is no id to
    name a folder from). ``"matched"`` is only sent when the request had to be
    matched by name: an id-given add identified the release itself and has
    nothing to report.
    """
    from server import integrations as intg
    from server import pending_albums

    mbid = intg._mbid(req.mbid or req.release_mbid)
    mode = (req.mode or "best").strip().lower()
    if mode not in ("best", "all"):
        raise HTTPException(400, "mode must be 'best' or 'all'")
    stated = str(req.kind or "auto").strip().lower()
    types = _types_filter(req.types)
    cfg = load_config()

    # No id at all: the caller NAMED the release (a recommendation row with no
    # MusicBrainz id). One search answers it: a match is then treated exactly
    # like an id-given add, and no match reports that nothing was added — there
    # is no id to name a folder from.
    named = not mbid
    matched = False
    if named:
        why = ""
        try:
            mbid, stated = _name_match(stated, req)
        except HTTPException:
            raise
        except Exception as e:
            mbid, stated = "", ""
            why = f"MusicBrainz did not answer ({e})"
        if not mbid:
            note = (f"{why} — nothing was added." if why else
                    "MusicBrainz has no match for it — nothing was added.")
            return {"ok": True, "matched": False, "queued": 0,
                    "albums": [], "skipped": [], "errors": [], "note": note}
        matched = True
    hit = {"matched": matched} if named else {}

    # Deferred: the caller stated a concrete kind AND holds the two fields a
    # folder is named from. `kind="auto"` is not deferred even then — what kind
    # of entity the id names is itself a MusicBrainz lookup, and guessing it
    # would record the wrong album. A name-matched add states its kind above.
    if not types and stated in ("release", "release_group", "releasegroup",
                                "recording"):
        deferred = pending_albums.create_from_request(
            mbid, release_mbid=req.release_mbid or "", kind=stated,
            title=req.title, artist=req.artist, year=req.year, cfg=cfg)
        if deferred is not None:
            threading.Thread(
                target=_prepare_add,
                args=(mbid, stated, mode, req.release_mbid or "", cfg, req,
                      deferred, types),
                daemon=True).start()
            return {"ok": True, "background": True, "resolving": True,
                    **hit, "queued": 1,
                    "albums": [_pending_album_payload(deferred)],
                    "skipped": [], "errors": [],
                    "note": _deferred_note()}

    kind = _intended_kind(mbid, stated)

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
            return {"ok": True, **hit, "background": False, "queued": 0,
                    "albums": [], "skipped": scope["skipped"], "errors": [],
                    "note": ("No release group of this artist is one of the "
                             "types you asked for." if scope["total"] else
                             "This artist has no release groups on MusicBrainz.")}
        threading.Thread(target=_prepare_artist, args=(mbid, mode, cfg, req, types),
                         daemon=True).start()
        return {"ok": True, **hit, "background": True, "queued": scope["queued"],
                "albums": [], "skipped": scope["skipped"], "errors": [],
                "note": _artist_note(scope["queued"], _types_label(types))}

    try:
        targets, skipped = _targets(mbid, kind, mode, req.release_mbid or "",
                                    cfg=cfg, types=types)
    except Exception as e:
        # A MusicBrainz outage is not "nothing matches".
        raise HTTPException(503, f"MusicBrainz did not answer: {e}")

    albums, errors = _create_all(targets, cfg,
                                 title=req.title, artist=req.artist, year=req.year)
    # The note has to be TRUE about what this add did. The library's own
    # "already in your library" answer is a skipped row, and a framework folder
    # standing for an album that is already here is one nothing can fill — so
    # that case says so instead of claiming the album was added (see
    # `_owned_skip`).
    if not albums and _owned_skip(skipped):
        note = _already_note()
    elif albums and all(a.get("existing") for a in albums):
        note = _already_note()
    elif albums:
        note = _added_note()
    else:
        note = "Nothing could be added."
    return {"ok": True, **hit, "background": False, "resolving": True,
            "queued": len(targets),
            "albums": [_pending_album_payload(a) for a in albums],
            "skipped": skipped, "errors": errors,
            "note": note}


@router.post("/api/library/add/cancel")
def library_add_cancel(req: CancelAddRequest):
    """Undo an "Add to library": its framework folder goes. A folder whose
    audio has already arrived is a real album by then, so it stays — only the
    placeholder is ever deleted here."""
    from server import pending_albums

    folder = os.path.normpath(str(req.album_path or "").strip())
    if not folder:
        raise HTTPException(400, "album_path is required")
    if not pending_albums.remove_folder(folder):
        raise HTTPException(404, "no pending album to cancel")
    return {"ok": True, "removed": True}
