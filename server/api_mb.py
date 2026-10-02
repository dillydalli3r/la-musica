"""MusicBrainz / LRCLIB / RYM: the metadata and lyrics lookups.

GET /api/mb/* is the MusicBrainz browser (release, release-group, search,
artist, recording, detect) and the import wizard's own match/assign writes;
/api/lyrics/* is the LRCLIB-backed lyrics surface (search/get/write/publish);
/api/rym/* validates and resolves RateYourMusic links. The network clients
live in ``server.integrations`` and the release resolution in
``server.mbresolve``, so these routes stay thin: parse, guard the path, hand
over, answer.
"""
import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mlo import autotag as _autotag
from mlo.stats import worker_count
from server import integrations as intg
from server import job_locks
from server import library as lib_mod
from server import mbresolve, tagcache
from server.api_common import (_allow_staged,
                               _guard_folder,
                               _in_music_folder,
                               _music_folder,
                               _skip_names,
                               is_audio_file,
                               load_config)


router = APIRouter(tags=["mb"])


class MatchRequest(BaseModel):
    album_path: str
    release_id: str
    staged: bool = False  # the import wizard's not-yet-imported album


class AssignTagsRequest(BaseModel):
    """Write MB/RYM links to tags. `tracks` maps track path -> {tag: value}."""
    tracks: dict
    staged: bool = False  # the import wizard's not-yet-imported album


# --------------------------------------------------------------------------- #
# MusicBrainz / LRCLIB / RYM
# --------------------------------------------------------------------------- #
@router.get("/api/mb/release")
def mb_release_query(mbid: str = Query(...)):
    """Release lookup by ID or full URL (query param — URLs contain slashes
    and cannot travel inside the path segment)."""
    rid = intg._mbid(mbid)
    if not rid:
        raise HTTPException(400, "invalid MusicBrainz ID or URL")
    try:
        release = intg.release_lookup(rid)
        # The tracklist's aliases, in ONE browse call (MusicBrainz carries none
        # inside the lookup): the page renders each track's title with the name
        # it is also known by, exactly as the artist and release rows do.
        intg.attach_recording_aliases(release, rid)
        return release
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz lookup failed: {e}")


@router.get("/api/mb/release-genres")
def mb_release_genres_query(mbid: str = Query(...), limit: Optional[int] = Query(None)):
    rid = intg._mbid(mbid)
    if not rid:
        raise HTTPException(400, "invalid MusicBrainz ID or URL")
    try:
        release = intg.release_lookup(rid)
        return intg.genre_cascade(release, limit=max(0, int(limit)) if limit else None)
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz genre lookup failed: {e}")


# ---- release-group browse (the import wizard's release picker) -------------
@router.get("/api/mb/release-group/{mbid}")
def mb_release_group(mbid: str, limit: int = Query(300), offset: int = Query(0)):
    rid = intg._mbid(mbid)
    if not rid:
        raise HTTPException(400, "invalid MusicBrainz ID or URL")
    try:
        return intg.release_group_browse(rid, max(1, limit), max(0, offset))
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz lookup failed: {e}")


# ---- generic MusicBrainz browser (search + entity pages) -------------------
@router.get("/api/mb/search/fields")
def mb_search_fields():
    """The fields the search box may put in a query, and the syntax to use
    them: MusicBrainz's own index fields per entity kind (field, kind, example,
    whether it takes quotes, and what it means), served from the same dict the
    server's own query builder is written against, so the UI's completion and
    its help can never offer a field the index would not answer."""
    return intg.search_help()


@router.get("/api/mb/search")
def mb_search(q: str = Query(""), type: str = Query("release"),
              limit: int = Query(100), offset: int = Query(0),
              mode: str = Query("free"), primary_type: str = Query(""),
              secondary_type: str = Query(""), artist: str = Query(""),
              year: str = Query(""), label: str = Query(""), catno: str = Query("")):
    """Search MusicBrainz for the in-app browser: type = artist |
    release-group | release | recording; mode = free | catno | barcode
    (catno/barcode only apply to releases); primary_type/secondary_type narrow
    releases and release groups to MusicBrainz release types (Album, EP,
    Single, Soundtrack, Live, ...). artist / year / label / catno are further
    constraints and COMBINE with all of the above — the whole query goes to
    the index, which is the only way to answer "albums by X from 1999 on
    label Y". `q` may be empty when a constraint says enough (a label/year
    browse). Returns {rows, total, offset, next, query}: `next` is the offset
    of the following page (null at the end) and `query` the Lucene query the
    index was asked, so the UI can show it.

    `q` travels to the index AS THE USER WROTE IT — `artist:"Radiohead" AND
    releasegroup:"OK Computer"` is a query the index answers, not a phrase to
    search for, so the app neither escapes nor re-quotes it (see
    `intg.search_query`, the one place that says what the app does to it:
    trim, and AND the constraint boxes on). A query MusicBrainz itself
    refuses answers 400 carrying MusicBrainz's own words, which the browser
    shows verbatim rather than a generic failure.

    `GET /api/mb/search/fields` serves the catalogue of what may go in it."""
    if type not in intg.MB_ENTITIES:
        raise HTTPException(400, "type must be one of " + ", ".join(intg.MB_ENTITIES))
    if mode not in ("free", "catno", "barcode"):
        raise HTTPException(400, "mode must be free, catno or barcode")
    if not (q.strip() or any(x.strip() for x in
                             (primary_type, secondary_type, artist, year, label, catno))):
        raise HTTPException(400, "q or a search constraint is required")
    try:
        return intg.search_mb(type, q, limit, mode, max(0, offset),
                              primary_type=primary_type.strip(),
                              secondary_type=secondary_type.strip(),
                              artist=artist.strip(), year=year.strip(),
                              label=label.strip(), catno=catno.strip())
    except intg.MusicBrainzError as e:
        # MusicBrainz REFUSED this query and said why (its search server names
        # the reason in the body). That is the request's fault, not an outage:
        # 400 with MusicBrainz's own text, never "MusicBrainz search failed".
        if e.status and 400 <= int(e.status) < 500:
            raise HTTPException(400, str(e))
        raise HTTPException(502, f"MusicBrainz search failed: {e}")
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz search failed: {e}")


@router.get("/api/mb/artist/{mbid}")
def mb_artist(mbid: str, limit: int = Query(300), offset: int = Query(0),
              primary_type: str = Query(""), secondary_type: str = Query("")):
    """Artist page: identity + a page of release groups. A type filter is
    answered by the search index (browse cannot filter by type at all), so
    the discography it reports is the whole one, not the loaded window."""
    rid = intg._mbid(mbid)
    if not rid:
        raise HTTPException(400, "invalid MusicBrainz ID or URL")
    try:
        return intg.artist_browse(rid, max(1, limit), max(0, offset),
                                  primary_type.strip(), secondary_type.strip())
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz lookup failed: {e}")


@router.get("/api/mb/recording/{mbid}")
def mb_recording(mbid: str, limit: int = Query(300), offset: int = Query(0)):
    rid = intg._mbid(mbid)
    if not rid:
        raise HTTPException(400, "invalid MusicBrainz ID or URL")
    try:
        return intg.recording_browse(rid, max(1, limit), max(0, offset))
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz lookup failed: {e}")


@router.get("/api/mb/detect/{mbid}")
def mb_detect(mbid: str):
    """Identify which MusicBrainz entity kind a bare MBID belongs to, so the
    browser can route pasted IDs without the user choosing a type."""
    rid = intg._mbid(mbid)
    if not rid:
        raise HTTPException(400, "invalid MusicBrainz ID or URL")
    try:
        return intg.detect_mbid(rid)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz lookup failed: {e}")


@router.get("/api/mb/search/releases")
def mb_search_releases(q: str = Query(..., min_length=1), limit: int = 10,
                        mode: str = Query("release")):
    """Search MusicBrainz releases: mode = release | track | catno | barcode."""
    if mode not in ("release", "track", "catno", "barcode"):
        raise HTTPException(400, "mode must be release, track, catno or barcode")
    try:
        return intg.search_releases(q, limit, mode)
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz search failed: {e}")


@router.get("/api/mb/search/artists")
def mb_search_artists(q: str = Query(..., min_length=1), limit: int = 5):
    try:
        return intg.search_artists(q, limit)
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz search failed: {e}")


def _scan_album_tracks(album_dir, lyrics=False):
    """Recursively list audio files in an album folder with tags + tech info.

    `lyrics=True` adds the per-track lyrics state (see `_add_lyrics_state`) —
    the wizard's per-track steps read this payload for an album the library
    tree does not list yet (a staged import)."""
    tracks = []
    skip = _skip_names()
    for root, dirs, files in os.walk(album_dir):
        dirs[:] = [d for d in dirs if d.lower() not in skip]
        for f in sorted(files):
            if not is_audio_file(f):
                continue
            path = os.path.join(root, f)
            tags, tech = tagcache.read_track(path, None)
            duration = float(tech.get("length") or 0) or None
            tn = lib_mod._parse_num(tags.get("TRACKNUMBER"))
            dn = lib_mod._parse_num(tags.get("DISCNUMBER"))
            tracks.append({
                "path": path.replace("\\", "/"),
                "file": os.path.relpath(path, album_dir).replace("\\", "/"),
                "tracknumber": tn,
                "discnumber": dn,
                "title": tags.get("TITLE"),
                "artist": tags.get("ARTIST"),
                "duration": duration,
                "tags": tags,
                "tech": tech,
            })
    if lyrics:
        _add_lyrics_state(tracks)
    return tracks


def _add_lyrics_state(tracks):
    """Mark scanned tracks with the lyrics they actually have.

    A scan used to carry no lyrics fields, so the import wizard's per-track
    steps hardcoded "no lyrics" and every track of a staged album claimed
    lyrics it already carried. The flags come from `mlo.grader._grade_album`,
    the one place that decides what counts as lyrics (an embedded
    LYRICS/UNSYNCEDLYRICS tag, and an .lrc that holds text and is not shared
    with a same-stem sibling), called per FOLDER because a disc folder is its
    own album to the grader. A folder the grader cannot read leaves the flags
    unset rather than claiming anything.
    """
    from mlo import grader

    cfg = load_config()
    fmt = str(cfg.get("lyrics_format") or "EMBEDDED")
    graded = {}
    for folder in {os.path.dirname(t["path"]) for t in tracks}:
        try:
            res = grader._grade_album(folder, fmt, cfg)
        except Exception:
            continue
        for row in (res or {}).get("tracks") or []:
            graded[os.path.normpath(os.path.join(folder, row.get("file") or ""))] = row
    for t in tracks:
        row = graded.get(os.path.normpath(t["path"])) or {}
        t["lyrics_embedded"] = bool(row.get("lyrics_embedded"))
        t["lyrics_lrc"] = bool(row.get("lyrics_lrc"))
        # The wizard's per-track lyrics step reads the KIND the same way the
        # library payload carries it (mlo.grader stamped both from one read of
        # the two stored texts), and presence is the kind not being null — so a
        # track the wizard shows as "Plain" is "plain" in the library too.
        t["lyrics_kind"] = row.get("lyrics_kind") or None
        t["lyrics_present"] = bool(t["lyrics_kind"])


@router.post("/api/mb/match")
def mb_match(req: MatchRequest):
    """Suggest release track/disc matches for local files in an album."""
    album_dir = os.path.normpath(req.album_path)
    if not os.path.isdir(album_dir):
        raise HTTPException(404, "album not found")
    _guard_folder(album_dir, req.staged, "album")
    rid = intg._mbid(req.release_id)
    if not rid:
        raise HTTPException(400, "invalid release ID")
    try:
        release = intg.release_lookup(rid)
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz lookup failed: {e}")

    from mlo.audio import AudioFile
    local_tracks = _scan_album_tracks(album_dir)
    for lt in local_tracks:
        lt.pop("tags", None)
        lt.pop("tech", None)
    return {"release": release, "suggestions": intg.match_tracks(local_tracks, release)}


@router.post("/api/mb/assign")
@job_locks.holds(lambda req: list((req.tracks or {}).keys()), kind="tags",
                 label="Write MB links")
def mb_assign(req: AssignTagsRequest):
    """Write per-track MB/RYM link tags. tracks: {path: {TAG: value}}.

    Videos are re-emitted losslessly, so a raw container comes back as a
    same-stem MKV: `container_changed`/`output_paths` name those files."""
    from mlo.audio import AudioFile
    # honey: unbounded dict = unbounded tag writes per request; 500 is far
    # above any wizard batch, raise it if a real flow ever hits the cap.
    if len(req.tracks or {}) > 500:
        raise HTTPException(400, "too many tracks (max 500 per request)")
    # GENRE can be assigned through this route (the wizard's tag step writes
    # it here), so the canonicalization cap comes from mb_genre_count once for
    # the whole request.
    cfg = load_config()
    genre_cap = _autotag.genre_count(cfg)
    folder = _music_folder()

    def assign_one(item):
        """Write ONE file's tags -> (errors, changed, swapped path or None).

        Split out of the route body so an album's files can be written across
        lanes: each file costs a FULL container rewrite — mlo.atomic copies it
        and mutagen rewrites the copy, two passes over every byte — and a
        28-track write ran 28 of those back to back, which is the wait the
        wizard's own label sits in front of. The files share nothing (one
        path, one temp, one mutagen object each) and `genre_cap` is read-only,
        so the pool is safe by construction. Errors keep request order because
        the pool is drained in submission order.
        """
        p, tag_map = item
        errs = []
        fp = os.path.normpath(p)
        if not os.path.isfile(fp):
            return [f"{p}: not found"], 0, None
        if not _in_music_folder(fp, folder) and not _allow_staged(fp, req.staged):
            return [f"{p}: outside music folder"], 0, None
        af = AudioFile(fp)
        if af.audio is None:
            return [f"{p}: {af.error or 'unreadable'}"], 0, None
        # Advisory values: only 0/1/2 exist (0 clean, 1 clean/explicit-clean,
        # 2 explicit) and a blank means "delete the tag" — an invalid value
        # written here would only surface as a grading failure later.
        bad = [k for k, v in tag_map.items()
               if k.upper() in ("ITUNESADVISORY", "ALBUMITUNESADVISORY")
               and str(v or "").strip() not in ("", "0", "1", "2")]
        if bad:
            return [f"{p}: {', '.join(sorted(bad))} must be 0, 1, 2 or empty"], 0, None
        if getattr(af, "is_video", False):
            # Video containers: batch all tags into ONE lossless ffmpeg
            # rewrite (a per-tag rewrite remuxes the whole file each time).
            # GENRE is canonicalized like any other write, but an MKV holds
            # ONE genre string (the batch writer takes a value per key), so
            # the list goes in joined with "; " — the spelling tag_values()
            # reads back as the same names (see _genre_names).
            clean = {}
            for k, v in tag_map.items():
                if str(k).upper() == "GENRE":
                    names = _genre_names(v, genre_cap)
                    if names:
                        clean[k] = "; ".join(names)
                    continue
                if str(v or "").strip():
                    clean[k] = v
            deletes = [k for k, v in tag_map.items() if not str(v or "").strip()]
            if deletes and not clean:
                return [f"{p}: video containers cannot delete tags — overwrite instead"], 0, None
            if clean and not af.set_video_tags(clean):
                return [f"{p}: {af.error or 'tag write failed'}"], 0, None
            tagcache.invalidate_path(fp)
            if af.container_changed:
                return [], 1, (af.tag_output_path or fp).replace("\\", "/")
            return [], 1, None
        af.defer_save(True)
        for k, v in tag_map.items():
            try:
                if str(k).upper() == "GENRE":
                    # GENRE is the one tag whose value is a LIST: every writer
                    # stores one repeated field per genre, so a value that
                    # arrives as a list, or as one "A; B" string the caller
                    # joined, is split and canonicalized here (an empty one
                    # deletes the tag). `str(v)` wrote the whole thing as a
                    # single genre literally named "['shoegaze', 'rock']".
                    names = _genre_names(v, genre_cap)
                    if names:
                        if not af.set_tag("GENRE", names):
                            errs.append(f"{p} GENRE: {af.error or 'write failed'}")
                    elif not af.delete_tag("GENRE"):
                        errs.append(f"{p} GENRE: {af.error or 'delete failed'}")
                    continue
                if isinstance(v, (list, tuple)):
                    # Several values for one field: set_tag() writes each as a
                    # repeated field, where `str(v)` stored the Python repr as
                    # one value (the GENRE case above, for every other tag).
                    values = [str(x).strip() for x in v if str(x).strip()]
                    if values and not af.set_tag(k, values):
                        errs.append(f"{p} {k}: {af.error or 'write failed'}")
                    elif not values and not af.delete_tag(k):
                        errs.append(f"{p} {k}: {af.error or 'delete failed'}")
                    continue
                if v is None or str(v) == "":
                    if not af.delete_tag(k):
                        errs.append(f"{p} {k}: {af.error or 'delete failed'}")
                elif not af.set_tag(k, str(v)):
                    errs.append(f"{p} {k}: {af.error or 'write failed'}")
            except Exception as e:
                errs.append(f"{p} {k}: {e}")
        af.defer_save(False)
        tagcache.invalidate_path(fp)
        return errs, 1, None

    items = list((req.tracks or {}).items())
    # A lane per file, capped at 8: this is disk work on ONE album folder, so
    # a lane per core on a big box would just queue at the disk. Same ceiling
    # the other I/O-bound scripts take (mlo/lyrics_fetch).
    lanes = worker_count(cfg, maximum=8, items=len(items))
    if lanes > 1:
        with ThreadPoolExecutor(max_workers=lanes) as pool:
            results = list(pool.map(assign_one, items))
    else:
        results = [assign_one(it) for it in items]
    errors = []
    changed = 0
    # Video writes are full lossless rewrites, so a raw container (VOB/AVI/…)
    # comes back as a same-stem MKV: report the files that were re-emitted.
    swapped = []
    for errs, n, sw in results:
        errors.extend(errs)
        changed += n
        if sw:
            swapped.append(sw)
    # Partial success is the norm (one unreadable file must not discard the
    # rest): always 200 with the capped list, never 500 — callers show `errors`.
    return {"ok": not errors, "changed": changed, "errors": errors[:20],
            "container_changed": bool(swapped), "output_paths": swapped}


def _lyrics_record(hit, artist="", track="", album=None, duration=None):
    """A provider-chain hit in the record shape the lyrics UI already consumes.

    The album/library batch download, the lyrics manager's search list and the
    viewer all read `id` / `plainLyrics` / `syncedLyrics` / `artist` / `track`
    (the LRCLIB record shape). Keeping it means those callers get the whole
    fallback chain — LRCLIB → NetEase → Kugou → QQ Music → Kuwo → YouTube
    captions, see `mlo.lyrics_providers.SOURCES` — without changing, and
    `provider` / `provider_label` ride along for anything that wants to show
    where the lyrics came from.
    """
    if not hit:
        return None
    synced = (hit.get("synced") or "").strip()
    plain = (hit.get("plain") or "").strip()
    if not synced and not plain:
        return None
    import zlib
    key = f"{hit.get('provider')}|{hit.get('matched_artist')}|{hit.get('matched_title')}"
    return {
        # Stable positive id: the manager modal dedupes candidates by it.
        "id": zlib.crc32(key.encode("utf-8")) & 0x7FFFFFFF,
        "trackName": hit.get("matched_title") or track,
        "artistName": hit.get("matched_artist") or artist,
        "albumName": hit.get("matched_album") or album,
        "duration": hit.get("duration") or duration,
        "instrumental": bool(hit.get("instrumental")),
        "plainLyrics": plain or None,
        "syncedLyrics": synced or None,
        "provider": hit.get("provider"),
        "provider_label": hit.get("provider_label") or hit.get("provider"),
        # The UI's older field names (kept for the search list rendering).
        "artist": hit.get("matched_artist") or artist,
        "track": hit.get("matched_title") or track,
    }


@router.get("/api/lyrics/search")
async def lyrics_search(
    artist: str = Query(...), track: str = Query(...),
    album: str = Query(None), duration: int = Query(None),
):
    """Lyrics candidates from the configured provider chain.

    At most one row (the chain returns its best hit); the list shape is what
    the editor's candidate picker expects, and a miss is an empty list rather
    than a 404 so a search can just say "nothing found".
    """
    from mlo.lyrics_providers import fetch_lyrics
    try:
        hit = await asyncio.to_thread(
            fetch_lyrics, load_config(), artist, track, album, duration)
    except Exception as e:
        raise HTTPException(502, f"lyrics search failed: {e}")
    record = _lyrics_record(hit, artist, track, album, duration)
    return [record] if record else []


@router.get("/api/lyrics/get")
async def lyrics_get(
    artist: str = Query(""), track: str = Query(...),
    album: str = Query(None), duration: int = Query(None),
):
    """The best lyrics hit for a track, through the chain (with fallbacks)."""
    from mlo.lyrics_providers import fetch_lyrics
    try:
        hit = await asyncio.to_thread(
            fetch_lyrics, load_config(), artist, track, album, duration)
    except Exception as e:
        raise HTTPException(502, str(e))
    record = _lyrics_record(hit, artist, track, album, duration)
    if record is None:
        return JSONResponse({"found": False}, status_code=404)
    return {"found": True, **record}


class LyricsWriteRequest(BaseModel):
    path: str
    lrc: str = ""
    source: str = "lrclib"
    staged: bool = False  # the import wizard's not-yet-imported album


@router.post("/api/lyrics/write")
@job_locks.holds(lambda req: [req.path], kind="lyrics", label="Lyrics write")
def lyrics_write(req: LyricsWriteRequest):
    """Write .lrc sidecar atomically, canonicalized via mlo.lyrics."""
    from mlo.lyrics import _format_for_storage
    p = os.path.normpath(mbresolve.resolve_track(req.path) or req.path)
    if not os.path.isfile(p):
        raise HTTPException(404, "audio file not found")
    _guard_folder(p, req.staged, "file")
    lrc_path = os.path.splitext(p)[0] + ".lrc"
    cfg = load_config()
    try:
        final = _format_for_storage(req.lrc, cfg, optimize=True, is_for_lrc=True)
    except Exception:
        final = req.lrc
    fd, tmp = tempfile.mkstemp(prefix=".lrc_tmp_", suffix=".lrc", dir=os.path.dirname(p) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(final)
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, lrc_path)
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise HTTPException(500, str(e))
    tagcache.invalidate_path(p)
    return {"ok": True, "lrc": lrc_path.replace("\\", "/")}


@router.get("/api/rym/validate")
def rym_validate(url: str = Query(...)):
    """Is this a RateYourMusic link, and WHICH page is it?

    `valid` keeps its old meaning ("a RYM URL") for callers that only accept
    or reject a paste; `kind` is what lets the link editor put an artist page
    in the artist field instead of storing it as the album link — a song page
    written into RATEYOURMUSIC_ALBUM would look resolved forever and block the
    automatic album lookup.
    """
    kind = intg.rym_url_kind(url)
    return {"valid": kind is not None, "kind": kind}


@router.get("/api/rym/resolve")
def rym_resolve(artist: str = Query(""), album: str = Query("")):
    """Verified RateYourMusic links for an album, for the link editor.

    Same resolution the import uses (rym_links_auto gates it): each link is
    None unless RYM itself confirmed that page, and `note` says why nothing
    was found — "could not resolve" is the normal "paste the URL yourself"
    answer, so it is a 200 here, never an error.
    """
    return intg.rym_links(artist, album, cfg=load_config())


def _genre_names(value, cap):
    """The canonical genre list one caller's GENRE value holds.

    A caller may send the value as a list (`["shoegaze", "rock"]`) or as one
    string another surface joined ("Rock; Shoegaze"), and both have to land as
    repeated GENRE fields holding MusicBrainz's own names. `str(v)` stored the
    first as ONE genre literally named "['shoegaze', 'rock']" and the second as
    one named "Rock; Shoegaze" — one field, and a genre no reader recognises.
    `normalize_genres` splits it, resolves each name, derives the family and
    puts it first, capped at *cap*.
    """
    from mlo.genres import normalize_genres
    return normalize_genres(value, cap)
