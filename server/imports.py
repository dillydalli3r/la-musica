"""The import service: what happens to an album once it is on disk.

Every import path (the wizard's upload/ingest, the downloads page, the bulk
queue, the Soulseek auto-import) ends in the same place — :func:`finish_album`
runs the configured script chain over the new album folder, and
:func:`bulk_import` is the queue that moves staging folders into the library
first. The Soulseek auto-import additionally verifies the downloaded audio
against the release it was looking for (:func:`acoustid_match`), and calls
:func:`finish_album` with ``defer_tagging`` — nothing unattended may tag, it
only stages and places the album.

Config keys this module owns:

    import_auto_scripts     master switch for the post-import chain
    import_scripts          explicit chain ids ([] = DEFAULT_CHAIN)
    import_bulk_concurrency albums processed at once by bulk_import
    import_acoustid         run the AcoustID release check on an import
    acoustid_*              key / availability of the AcoustID lookup itself

Nothing here raises at a caller: an import that half-worked still reports
every album's outcome.
"""
import os
import re
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from mlo.config import load_config
from mlo.paths import library_root, move_path

from server import script_runners
from server import tagcache

# CUEs → FLACs → videos → lyrics format → fetch lyrics → publish lyrics →
# auto tagging (mood/genre/advisory) → images → audit → DR & ReplayGain →
# AccurateRip → key & BPM → beets → release tracklist → format all → grade.
# Cheap, path-independent work first, the slow re-encodes and the
# library-wide grader last. 18 (publish to LRCLIB) sits right after the fetch
# (13) whose result it gives back. 15 (the .mlo_expected.json manifest) must
# run AFTER the tagging step (14, beets): it reads the release id off the
# album's own MUSICBRAINZ_ALBUMID tags, so a tagger that has not matched the
# release yet would leave it nothing to fetch — and grading (4) requires the
# manifest.
DEFAULT_CHAIN = [2, 3, 11, 1, 13, 18, 8, 5, 6, 7, 9, 12, 14, 15, 10, 4]

# The ids a configured chain may name, kept in step with the registry itself
# (a bound that still advertised a removed id let a saved one come back as an
# error entry at import time instead of being dropped), so adding a script to
# script_runners.RUNNERS is the only edit that widens it.
SCRIPT_ID_MIN, SCRIPT_ID_MAX = 1, max(script_runners.RUNNERS)


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #
def chain_for(cfg=None):
    """The script ids an import runs, in order.

    ``import_auto_scripts`` off is no chain at all. Otherwise an explicit
    ``import_scripts`` list replaces the built-in chain (ids outside the
    runner registry dropped, duplicates dropped, order kept); an empty one
    means DEFAULT_CHAIN.
    """
    cfg = cfg or {}
    if not cfg.get("import_auto_scripts", True):
        return []
    configured = cfg.get("import_scripts")
    if isinstance(configured, str):
        configured = re.split(r"[;,\s]+", configured)
    if configured:
        ids = []
        for raw in configured:
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                continue
            if SCRIPT_ID_MIN <= sid <= SCRIPT_ID_MAX and sid not in ids:
                ids.append(sid)
        return ids
    return list(DEFAULT_CHAIN)


def _invalidate_caches():
    """The album's tags just changed — the tag/name caches are stale.

    Same two caches ``/api/run`` invalidates after a script run; a missing
    module (a stripped backend) is not a reason to fail an import.
    """
    try:
        from server import tagcache
        tagcache.invalidate_all()
    except Exception:
        pass
    try:
        from server import mbresolve
        mbresolve.invalidate()
    except Exception:
        pass


def finish_album(album_dir, cfg=None, progress=None, force=None, *,
                 defer_tagging=False):
    """Run the configured chain over ONE album folder.

    The single call every import path makes after an album is on disk.
    Returns ``{"path", "scripts", "chain", "errors"}``; ``scripts`` is one
    result per chain id (``server.script_runners`` shape) and ``errors`` a
    flat list for a caller that only wants to know what went wrong. A failing
    script is reported, never raised: the album is already imported.

    ``defer_tagging`` is the auto-import's mode: the album is staged and
    placed — RYM link stamps, the metadata step and the cover step, the three
    things an unattended import can do without deciding anything for the user
    — and nothing else. The ITUNESADVISORY and INSTRUMENTAL fetches and the
    whole script chain (auto tagging, lyrics, DR & ReplayGain, grade, format
    all) are tag-writing work that was never asked for, so an album the
    auto-import dropped in the library keeps the tags it arrived with until
    the user runs the wizard or a menu action. That is also why this is a
    keyword here and not a config key: the manual paths (``/api/import/
    finish``, the Soulseek page, the bulk queue) call without it and keep the
    full chain. ``chain``/``scripts`` come back empty — nothing was deferred
    to a later run either.

    The artist image and the artist/album descriptions are accounted for by
    the caller instead: ``soulseek_auto`` is the ONLY ``defer_tagging``
    caller (its unattended importer), and it runs
    ``api_discovery.ensure_artist_album_metadata`` right after this returns,
    logging one line per item — the step lives there rather than here so it
    can never run twice on that path.
    """
    cfg = cfg or load_config()
    path = os.path.normpath(str(album_dir))
    out = {"path": path, "scripts": [], "chain": [], "errors": []}
    chain = chain_for(cfg)
    out["chain"] = chain
    if not os.path.isdir(path):
        out["errors"].append("album folder not found")
        return out

    # RateYourMusic album + artist links: resolved and written once, only for
    # the links the album does not carry yet. Deliberately BEFORE the chain
    # check — an album imported with no scripts configured still gets its
    # links. Gated by rym_links_auto; a lookup that finds nothing is one log
    # line (the user pastes the URL in the links editor), never an error.
    try:
        rym = stamp_rym_links(path, cfg)
        if rym["note"].startswith("could not resolve"):
            print(f"[mlo] rateyourmusic: {rym['note']} — {os.path.basename(path)}")
    except Exception:
        traceback.print_exc()

    # defer_tagging keeps going to the metadata/cover steps below; its chain
    # is empty by definition and is set when it returns.
    if not chain and not defer_tagging:
        return out                      # auto scripts off / no ids configured

    # Advisory BEFORE the chain: script 8 derives ALBUMITUNESADVISORY from the
    # per-track values, so writing ITUNESADVISORY afterwards would leave the
    # album tag stale. Gated by advisory_auto_fetch; never fatal.
    if not defer_tagging and cfg.get("advisory_auto_fetch", True):
        try:
            out["advisory"] = fetch_advisories([path], cfg)
        except Exception:
            traceback.print_exc()

    # Instrumental detection sits next to it for the same reason (the chain's
    # lyrics step reads INSTRUMENTAL), and is independent of the advisory: a
    # track can be instrumental and explicit-rated. Gated by
    # instrumental_auto_fetch; never fatal.
    if not defer_tagging and cfg.get("instrumental_auto_fetch", True):
        try:
            out["instrumental"] = fetch_instrumentals([path], cfg)
        except Exception:
            traceback.print_exc()

    # Artist image / descriptions: fetched here (the metadata step) so an
    # import leaves the album graded-ready. metadata_review on stages the
    # candidates instead of writing them. Staged BEFORE the chain on purpose:
    # the record is looked up by the album's own identity (see
    # `staged_metadata`), so it survives the chain moving the album to its
    # canonical folder — which it does, via beets/organize. Never fatal.
    try:
        out["metadata"] = run_metadata_step(path, cfg)
    except Exception:
        traceback.print_exc()

    # Cover art: an album that arrived without one gets it now, found by the
    # identity the import just stamped and stored by the cover page's own
    # writer. With cover_review on the candidates are staged instead (as
    # ``covers`` on the album's entry in the same review file the metadata step
    # writes, so the two steps share one record and neither clobbers the
    # other). Gated by cover_auto_fetch; never fatal.
    try:
        out["cover"] = run_cover_step(path, cfg)
    except Exception:
        traceback.print_exc()

    if defer_tagging:
        # Stage and place, stop. The identity is stamped (RYM links) and the
        # album got its artist image/description and cover art, but every
        # tag-writing script stays unrun — see the docstring. `chain` empty so
        # a caller reads "no chain ran" rather than "these ids ran".
        out["chain"] = []
        _invalidate_caches()            # the steps above wrote tags/files
        return out

    try:
        # wait=True: an import must not skip its chain just because a UI run
        # happens to hold the library lock — it queues behind it instead.
        out["scripts"] = script_runners.run_chain(
            cfg, chain, targets=[path], force=force, progress=progress, wait=True)
    except script_runners.RunBusy as e:
        # Only reachable after the (1 h) wait timed out: report it so the
        # caller marks the album unfinished instead of "imported".
        out["errors"] = [str(e)]
        return out
    out["errors"] = [f"script {r.get('id')}: {r['error']}"
                     for r in out["scripts"] if r.get("error")]
    _invalidate_caches()
    return out

# --------------------------------------------------------------------------- #
# AcoustID release check
# --------------------------------------------------------------------------- #
def _album_dir(path):
    """A track path's album folder, or the folder itself."""
    p = os.path.normpath(str(path))
    return p if os.path.isdir(p) else os.path.dirname(p)


def _audio_files(folder):
    """Every audio file under *folder*, dot-dirs pruned.

    Uses the download-side extension set (``server.soulseek_auto``): an import
    can be a folder of raw WAVs/APEs that the chain converts afterwards, and
    ``mlo.paths``'s library sets do not cover those.
    """
    from server.soulseek_auto import _AUDIO_EXTS
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in _AUDIO_EXTS:
                out.append(os.path.join(root, f))
    return sorted(out)


_ACOUSTID_ROW = {"release_group_id": None, "release_group_title": None,
                 "release_group_type": None, "artists": [], "score": None,
                 "matched": 0, "total": 0, "recordings": []}


def fetch_advisories(paths, cfg=None):
    """Resolve and write ITUNESADVISORY for these albums / tracks.

    Each track is identified by its ISRC tag (or by the MusicBrainz recording
    ID the import just stamped, whose ISRCs MusicBrainz supplies) and rated by
    `integrations.resolve_advisory_route`, which asks EVERY applicable source
    in one pass — Deezer and Spotify by ISRC, Apple's explicit-edition album
    route, Apple's exact-title song search — and merges them with
    `integrations.merge_advisory`: 1 when any source states explicit, else 0.
    An unstated advisory is therefore written as 0 (the user's policy): the
    per-track `answers` map is what shows whether any source actually spoke.
    A file that already carries a valid 0/1/2 is left alone (the user's manual
    edit wins) and a value equal to the merged one is not rewritten.

    Returns ``{"updated": n, "values": {path: 0|1}, "sources": {path:
    provider}, "answers": {path: {source: 0|1}}}`` — `sources` is who stated
    each value, `answers` is what every source said about the track.
    """
    from mlo.audio import AudioFile
    from mlo.config import should_write_audio_tag
    from server import integrations as intg

    cfg = cfg or load_config()
    if not cfg.get("advisory_auto_fetch", True):
        return {"updated": 0, "values": {}, "sources": {}, "answers": {},
                "skipped": "advisory_auto_fetch is off"}
    targets = []
    for p in paths or []:
        p = os.path.normpath(str(p))
        if os.path.isdir(p):
            targets.extend(_audio_files(p))
        elif os.path.isfile(p):
            targets.append(p)

    # Apple's album route prefers the collection whose trackCount matches the
    # album, which is the number of audio files sitting in each folder.
    per_folder = {}
    for path in targets:
        folder = os.path.dirname(path)
        per_folder[folder] = per_folder.get(folder, 0) + 1

    updated = 0
    values = {}
    sources = {}
    answers = {}
    for path in targets:
        try:
            af = AudioFile(path)
            if af.audio is None:
                continue
            current = str(af.get_tag("ITUNESADVISORY") or "").strip()
            if current in ("0", "1", "2"):
                values[path] = int(current)
                continue
            if not should_write_audio_tag(cfg, "ITUNESADVISORY", filepath=path):
                continue
            route = intg.resolve_advisory_route(
                isrc=str(af.get_tag("ISRC") or "").split(";")[0].strip(),
                recording_mbid=str(af.get_tag("MUSICBRAINZ_TRACKID") or "").strip(),
                title=str(af.get_tag("TITLE") or ""),
                artist=str(af.get_tag("ARTIST") or af.get_tag("ALBUMARTIST") or ""),
                album=str(af.get_tag("ALBUM") or ""),
                disc=af.get_tag("DISCNUMBER"),
                track=af.get_tag("TRACKNUMBER"),
                track_count=per_folder.get(os.path.dirname(path)),
                cfg=cfg,
            )
            value = route.get("value")
            if value is None:
                continue
            if str(value) != current and af.set_tag("ITUNESADVISORY", str(value)):
                updated += 1
            values[path] = value
            if route.get("source"):
                sources[path] = route["source"]
            if route.get("answers"):
                answers[path] = route["answers"]
        except Exception:
            continue
    if updated:
        _invalidate_caches()
    return {"updated": updated, "values": values, "sources": sources,
            "answers": answers}


def fetch_instrumentals(paths, cfg=None):
    """Resolve and write INSTRUMENTAL (0/1) for these albums / tracks.

    The detection itself is ``server.instrumental.detect_instrumental``, which
    cross-references every available source (LRCLIB, Spotify audio-features,
    the file's own title marker, lyrics evidence) and merges them: any source
    saying instrumental wins, otherwise any source saying not-instrumental,
    otherwise NO answer and no write. A file that already carries 0/1 is left
    alone (the user's manual edit wins).

    Returns ``{"updated": n, "values": {path: 0|1}, "evidence": {path:
    {source: 0|1}}}``.
    """
    from mlo.audio import AudioFile
    from mlo.config import should_write_audio_tag
    from server import instrumental as inst

    cfg = cfg or load_config()
    if not cfg.get("instrumental_auto_fetch", True):
        return {"updated": 0, "values": {}, "evidence": {},
                "skipped": "instrumental_auto_fetch is off"}
    targets = []
    for p in paths or []:
        p = os.path.normpath(str(p))
        if os.path.isdir(p):
            targets.extend(_audio_files(p))
        elif os.path.isfile(p):
            targets.append(p)

    found = inst.detect_instrumental(targets, cfg)
    updated = 0
    values = {}
    evidence = {}
    for path, hit in found.items():
        try:
            value = hit.get("value")
            if hit.get("answers"):
                evidence[path] = hit["answers"]
            if value is None:
                continue
            af = AudioFile(path)
            if af.audio is None:
                continue
            current = str(af.get_tag("INSTRUMENTAL") or "").strip()
            if current in ("0", "1"):
                values[path] = int(current)
                continue
            if not should_write_audio_tag(cfg, "INSTRUMENTAL", filepath=path):
                continue
            if str(value) != current and af.set_tag("INSTRUMENTAL", str(value)):
                updated += 1
            values[path] = value
        except Exception:
            continue
    if updated:
        _invalidate_caches()
    return {"updated": updated, "values": values, "evidence": evidence}


# --------------------------------------------------------------------------- #
# Metadata step — artist image / artist description / album description
# --------------------------------------------------------------------------- #
# With metadata_review ON the candidates are STAGED here (nothing is written
# until the user applies one through POST /api/metadata/apply); with it OFF
# the best candidate is saved as part of the import.
_METADATA_REVIEW_NAME = "metadata_review.json"


def _metadata_review_path(cfg=None):
    from mlo.paths import app_data_dir
    cfg = cfg or load_config()
    d = app_data_dir(str(cfg.get("music_folder") or "") or None)
    return os.path.join(d, _METADATA_REVIEW_NAME) if d else None


def _review_key(album_dir):
    return os.path.normpath(str(album_dir)).replace("\\", "/").lower()


def _review_load(path):
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _album_mbids(album_dir):
    """(album id, release-group id) from an album's own tags, either "".

    Read from a few files only: album-level tags are uniform across the tracks
    (the same reason `_album_mbids` caps its scan), and this runs for
    every staged-entry lookup.
    """
    from mlo.audio import AudioFile

    album_id = rg = ""
    for p in _audio_files(album_dir)[:5]:
        try:
            af = AudioFile(p)
            if af.audio is None:
                continue
            album_id = album_id or str(af.get_tag("MUSICBRAINZ_ALBUMID") or "").strip().lower()
            rg = rg or str(af.get_tag("MUSICBRAINZ_RELEASEGROUPID") or "").strip().lower()
        except Exception:
            continue
        if album_id or rg:
            break
    return album_id, rg


def staged_metadata(album_dir, cfg=None):
    """The staged review entry for an album ({} when none).

    Looked up three ways, in order: the exact path key, the album FOLDER name,
    then the album's MusicBrainz identity (release id, then release-group id).
    The identity is what makes the record survive the import chain moving the
    album — beets/organize relocates it to the canonical folder, so the path
    the import staged under can be a folder that no longer exists by the time
    the album page asks. Two albums sharing a folder name is the one ambiguous
    case; the first match wins, which is no worse than a record nobody finds.
    """
    path = _metadata_review_path(cfg)
    if not path or not os.path.isfile(path):
        return {}
    data = _review_load(path)
    hit = data.get(_review_key(album_dir)) or {}
    if hit:
        return hit
    leaf = os.path.basename(os.path.normpath(str(album_dir))).lower()
    if leaf:
        for key, entry in data.items():
            if os.path.basename(key.rstrip("/")) == leaf:
                return entry or {}
    album_id, rg_id = _album_mbids(album_dir)
    if album_id or rg_id:
        for entry in data.values():
            covers = (entry or {}).get("covers") or {}
            staged_album = str(covers.get("album_id") or "").strip().lower()
            staged_rg = str(covers.get("release_group") or "").strip().lower()
            if album_id and staged_album == album_id:
                return entry or {}
            if rg_id and staged_rg and staged_rg == rg_id:
                return entry or {}
    return {}


def stage_metadata(album_dir, entry, cfg=None):
    """Record an album's staged candidates (entry=None clears the entry)."""
    import json
    path = _metadata_review_path(cfg)
    if not path:
        return
    data = _review_load(path) if os.path.isfile(path) else {}
    key = _review_key(album_dir)
    if entry is None:
        data.pop(key, None)
    else:
        data[key] = entry
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        traceback.print_exc()


def _album_identity(album_dir):
    """(artist, album) from the folder's own tags (folder name as fallback)."""
    from mlo.audio import AudioFile
    for path in _audio_files(album_dir):
        try:
            af = AudioFile(path)
            if af.audio is None:
                continue
            artist = str(af.get_tag("ALBUMARTIST") or af.get_tag("ARTIST") or "").strip()
            album = str(af.get_tag("ALBUM") or "").strip()
            if artist or album:
                return artist, album
        except Exception:
            continue
    return "", os.path.basename(str(album_dir).rstrip("\\/"))


def apply_metadata(album_dir, cfg=None):
    """Fetch and store the best artist image / descriptions for an album.

    The import chain's metadata step and the manual apply route share this:
    it honours the per-feature switches (artist_image_enabled,
    artist_description_enabled, album_description_enabled — what the app may
    fetch on the user's behalf) and NEVER overwrites stored content. Returns
    {"artist_image", "artist_description", "album_description"} with the paths
    written (None where nothing was written). Never raises."""
    from mlo import artistdata
    from server import discovery
    from server import integrations as intg

    cfg = cfg or load_config()
    out = {"artist_image": None, "artist_description": None, "album_description": None}
    artist, album = _album_identity(album_dir)
    if not artist:
        return out
    folder = artistdata.artist_dir(cfg, artist)

    if folder and cfg.get("artist_image_enabled", True) and not artistdata.has_image(folder):
        hit = discovery.artist_image(artist, mbid=artistdata.folder_mbid(folder), cfg=cfg)
        if hit and hit.get("url"):
            try:
                data, _ctype = intg.fetch_image_bytes(hit["url"])
                out["artist_image"] = artistdata.save_image(
                    folder, data, cfg, source=hit.get("source") or "auto",
                    source_url=hit.get("url"), kind="artist",
                    label=hit.get("label"))
            except Exception:
                traceback.print_exc()

    if folder and cfg.get("artist_description_enabled", True) and not artistdata.has_description(folder):
        found = discovery.artist_description(artist, mbid=artistdata.folder_mbid(folder), cfg=cfg)
        if found and str(found.get("text") or "").strip():
            out["artist_description"] = artistdata.write_description(
                folder, found["text"], cfg=cfg, source=found.get("source"),
                source_url=found.get("source_url"), kind="artist")
            artistdata.write_provenance(folder, {
                "description_source": found.get("source"),
                "description_source_url": found.get("source_url"),
                "description_title": found.get("title")}, kind="artist", cfg=cfg)

    if (album and cfg.get("album_description_enabled", True)
            and not artistdata.has_description(album_dir)):
        found = discovery.album_description(artist, album, cfg=cfg)
        if found and str(found.get("text") or "").strip():
            out["album_description"] = artistdata.write_description(
                album_dir, found["text"], cfg=cfg, source=found.get("source"),
                source_url=found.get("source_url"), kind="album")
            artistdata.write_provenance(album_dir, {
                "description_source": found.get("source"),
                "description_source_url": found.get("source_url"),
                "description_title": found.get("title")}, kind="album", cfg=cfg)

    if any(out.values()):
        _invalidate_caches()
    return out


def run_metadata_step(album_dir, cfg=None):
    """The import chain's metadata step (metadata_auto_fetch / metadata_review).

    auto-fetch off → nothing happens. On with review ON → the candidates are
    staged for the user and nothing is written until their apply call. On with
    review off → the best candidate is saved now. Never fatal."""
    from server import discovery

    cfg = cfg or load_config()
    if not cfg.get("metadata_auto_fetch", True):
        return {"staged": False, "applied": {}}
    if cfg.get("metadata_review", False):
        artist, album = _album_identity(album_dir)
        try:
            candidates = discovery.metadata_candidates(artist, album, cfg=cfg)
        except Exception:
            traceback.print_exc()
            return {"staged": False, "applied": {}}
        stage_metadata(album_dir, {"artist": artist, "album": album,
                                   "candidates": candidates}, cfg)
        return {"staged": True, "applied": {}}
    try:
        return {"staged": False, "applied": apply_metadata(album_dir, cfg)}
    except Exception:
        traceback.print_exc()
        return {"staged": False, "applied": {}}


# Cover auto-fetch: which album this is decides who is asked. A release-group
# id is an identity — the Cover Art Archive answers about it by id, with no
# name guessing at all — while an album without one is found by its names.
COVER_FETCH_TIMEOUT = 30.0

# How many candidates a review handout carries. The finder's own search deals
# in dozens; a review is a pick-one screen, so a screenful is the whole point
# — the long tail is one click away on the cover page.
COVER_REVIEW_LIMIT = 12


def _album_cover_present(album_dir):
    """Whether the album already has cover art, so nothing is fetched.

    `COVER_NAMES` is the grader's own set: "has a cover" here means exactly
    what grading means by one. `tagcache.cover_bytes` is the reader the UI
    serves art from and also knows the formats it can hand out; either answer
    means there is nothing to do.
    """
    from mlo.grader import COVER_NAMES

    try:
        if {n.lower() for n in os.listdir(album_dir)} & COVER_NAMES:
            return True
    except OSError:
        pass
    try:
        return tagcache.cover_bytes(album_dir)[0] is not None
    except Exception:
        return False


def run_cover_step(album_dir, cfg=None):
    """The import chain's cover step (cover_auto_fetch / cover_review).

    An album that arrived without cover art gets one, found the way the cover
    page finds them and stored by the same writer an upload goes through — so
    the file that lands is already at the library's own size and encoding.
    The release-group id the import stamped asks the Cover Art Archive by
    identity; an album without one is searched by artist/album name (COV, then
    the Cover Art Archive / Deezer / iTunes fallbacks).

    With `cover_review` on (the default) the candidates are STAGED — under
    ``entry["covers"]`` in the same review file the metadata step uses, other
    keys of the album's entry kept — and NOTHING is written; the user picks one
    and the UI writes it through the existing ``POST /api/cover/fromurl``. With
    it off the best hit is applied here, exactly as before.

    Never fatal, and never silent about a cover it could not get: the result
    is ``{"fetched", "applied", "source", "note", "staged", "candidates"}`` —
    the same shape `run_metadata_step` hands back to `finish_album`, note being
    the line a caller can show ("cover fetched from <source>", "" when there
    was nothing to do or nothing was wanted), and ``staged``/``candidates``
    how many options the user was handed instead of a written file.
    """
    cfg = cfg or load_config()
    out = {"fetched": False, "applied": {}, "source": None, "note": "",
           "staged": False, "candidates": 0}
    if not cfg.get("cover_auto_fetch", True):
        return out
    try:
        if _album_cover_present(album_dir):
            return out
        artist, album = _album_identity(album_dir)
        if not artist and not album:
            out["note"] = "no artist/album tags to search by"
            return out
        # Review wants a choice, not one answer: ask the finder for a screenful
        # of them. Not reviewing keeps the old shape — one hit, written here.
        review = bool(cfg.get("cover_review", True))
        album_id, rg = _album_mbids(album_dir)
        from server import integrations as intg
        if rg:
            rows, provider = intg._cover_fallback(
                artist, album, COVER_REVIEW_LIMIT if review else 1, cfg, rg,
                COVER_FETCH_TIMEOUT)
        else:
            found = intg.cover_search(
                artist, album, limit=COVER_REVIEW_LIMIT if review else 2,
                cfg=cfg, timeout=COVER_FETCH_TIMEOUT)
            rows, provider = found.get("results") or [], found.get("provider")
        if review:
            # Only rows the apply route can write at all: /api/cover/fromurl
            # takes a URL, and a row without one is a dead option on the pick
            # screen.
            rows = [r for r in rows if r.get("big")][:COVER_REVIEW_LIMIT]
            if not rows:
                out["note"] = "no cover found"
                return out
            # The metadata step may have staged this very album already: read
            # its entry first so the candidates it put there survive.
            entry = dict(staged_metadata(album_dir, cfg))
            entry["covers"] = {
                "artist": artist,
                "album": album,
                # The release id too: with it (or the release group) the entry
                # is found again after the import chain relocates the album, so
                # the review record is never orphaned by a rename.
                "album_id": album_id,
                "release_group": rg,
                "provider": provider,
                "staged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "results": rows,
            }
            stage_metadata(album_dir, entry, cfg)
            out["source"] = provider
            out["staged"] = True
            out["candidates"] = len(rows)
            out["note"] = f"{len(rows)} cover candidates to pick from"
            return out
        url = next((r.get("big") for r in rows if r.get("big")), "")
        if not url:
            out["note"] = "no cover found"
            return out
        # main.py imports this module, so the cover writer is reached back into
        # lazily — exactly how server.api_discovery reaches `_in_music_folder`.
        from server.main import _cover_url_bytes, _write_cover_bytes, _sniff_image_ext

        data, ctype = _cover_url_bytes(url, artist, album, rg)
        if not data:
            out["note"] = "the cover image came back empty"
            return out
        res = _write_cover_bytes(album_dir, "cover", _sniff_image_ext(data, ctype), data)
        out["source"] = provider
        out["applied"] = {"cover": res.get("path")}
        out["fetched"] = True
        out["note"] = f"cover fetched from {provider or 'the image url'}"
    except Exception as e:
        traceback.print_exc()
        out["note"] = f"cover step failed: {e}"
    return out


def acoustid_match(paths, cfg=None, progress=None, apply=False):
    """Which release group the audio in these albums really is (AcoustID).

    Album folders or track paths; the tracks of each folder are fingerprinted
    and voted on as one album (see ``mlo.acoustid.match_release``). Returns
    ``{"available", "note", "albums": [{"path", "release_group_id",
    "release_group_title", "release_group_type", "artists", "score",
    "matched", "total", "recordings", "tagged"}]}`` — ``available`` False with
    a human-readable ``note`` when no key/fpcalc is configured, and never an
    exception.

    With *apply* the accepted match is also written into the files
    (`ACOUSTID_ID` + `ACOUSTID_FINGERPRINT`, the tags Picard writes and the
    opt-in grading check reads) and ``tagged`` reports how many went in — the
    wizard passes apply=True when the user accepts the match, which is the
    only moment "this is really that release" is a statement the app can act
    on. Fingerprints come from the lookup, so applying costs no extra fpcalc.
    """
    cfg = cfg or load_config()
    try:
        from mlo import acoustid
    except ImportError as e:                      # pragma: no cover - stripped backend
        return {"available": False, "note": f"acoustid unavailable: {e}", "albums": []}

    albums = []
    for p in paths or []:
        album = _album_dir(p)
        if album and album not in albums:
            albums.append(album)

    if not acoustid.available(cfg):
        return {"available": False,
                "note": acoustid.acoustid_enabled_note(cfg) or "AcoustID unavailable",
                "albums": []}

    rows = []
    for album in albums:
        row = {"path": album, "tagged": 0, **_ACOUSTID_ROW}
        tracks = []
        try:
            tracks = _audio_files(album)[:acoustid.MAX_TRACKS]
            match = acoustid.match_release(cfg, tracks, progress=progress) if tracks else None
        except Exception:
            traceback.print_exc()
            match = None
        if match:
            row.update({k: match.get(k) for k in _ACOUSTID_ROW})
            row["path"] = album
            if apply:
                tagged = 0
                for rec in match.get("recordings") or []:
                    if acoustid.write_tags(rec.get("path"), rec.get("recording_id"),
                                           rec.get("fingerprint"), cfg):
                        tagged += 1
                row["tagged"] = tagged
                if tagged:
                    tagcache.invalidate_all()
        else:
            row["total"] = len(tracks)
        rows.append(row)
    return {"available": True, "note": "", "albums": rows}


def release_group_mismatch(match_row, release_group_id):
    """Warning text when an AcoustID result is another release group, else "".

    Used by the Soulseek auto-import as a *verification* of an already
    accepted download: a different pressing is worth telling the user about,
    never worth throwing the album away over.
    """
    want = str(release_group_id or "").strip()
    got = str((match_row or {}).get("release_group_id") or "").strip()
    if not want or not got or got == want:
        return ""
    title = (match_row or {}).get("release_group_title") or "?"
    return (f"AcoustID matched release group {got} ({title}, "
            f"{(match_row or {}).get('matched')}/{(match_row or {}).get('total')} "
            f"tracks) but the release being imported is {want}")


# --------------------------------------------------------------------------- #
# Identity tags for a release-driven import
# --------------------------------------------------------------------------- #
def _release_for_stamping(release):
    """A release dict with the keys the Soulseek stamper reads.

    Callers hand over what they have (``release_mbid``/``title``/``artists``
    from the wizard or the discovery providers); the stamper wants
    ``id``/``media``/``artists``.
    """
    rel = dict(release or {})
    if not rel.get("id"):
        rel["id"] = rel.get("release_mbid") or ""
    return rel


def _stamp_release(album_dir, release, cfg):
    """Write the release identity into the album's tags.

    Returns ``(written, failed)`` — a file whose tags cannot be written is
    counted rather than silently skipped: a release-driven import that stamped
    nothing would otherwise surface much later as bare grading failures with
    nothing pointing at the stamp step.

    The MusicBrainz ids (+ per-track recording ids) come from
    ``server.soulseek_auto._stamp_mb_tags`` — the same stamper the Soulseek
    import uses, so a bulk import and an auto-import tag identically. On top:
    GENRE from the full per-track genre chain (RateYourMusic → ListenBrainz →
    MusicBrainz → iTunes → Wikidata → Last.fm → Discogs → Deezer, see
    ``integrations.genre_chain``), already merged per track and capped at
    ``mb_genre_count``. ITUNESADVISORY is not written here —
    ``finish_album`` resolves it from the ISRCs (``fetch_advisories``) before
    the chain runs, which is the only path that can state a real rating
    instead of echoing one.

    Returns the number of files written; a tag failure never fails an import.
    """
    from mlo.audio import AudioFile
    from server import integrations as intg
    from server import soulseek_auto

    rel = _release_for_stamping(release)
    written = 0
    try:
        soulseek_auto._stamp_mb_tags(album_dir, rel)
    except Exception:
        traceback.print_exc()
    files = _audio_files(album_dir)
    genres = {}
    if rel.get("release_group_id") or rel.get("id"):
        try:
            # The FULL per-track chain (RateYourMusic → ListenBrainz →
            # MusicBrainz → iTunes → Wikidata → Last.fm → Discogs → Deezer),
            # merged per track and capped: an import therefore writes the same
            # genres an Auto-tagging run would, from the release it has just
            # identified, and a per-track source (recording tags, Apple's
            # primaryGenreName) lands on the track it belongs to.
            chain = intg.genre_chain(
                artist=next((a.get("name") for a in rel.get("artists") or []
                             if a.get("name")), ""),
                album=rel.get("title") or "",
                release=rel, limit=cfg.get("mb_genre_count"), cfg=cfg,
                files=files)
            genres = chain.get("per_track") or {}
        except Exception:
            traceback.print_exc()

    failed = 0
    for path in files:
        try:
            af = AudioFile(path)
            if af.audio is None:
                failed += 1
                continue
            tags = {}
            disc, pos = soulseek_auto._parse_trackno(path)
            names = genres.get((disc, pos)) or []
            if names and not str(af.get_tag("GENRE") or "").strip():
                tags["GENRE"] = "; ".join(names)
            for key, value in tags.items():
                af.set_tag(key, value)
            if tags:
                written += 1
        except Exception:
            failed += 1
            continue
    return written, failed


def stamp_rym_links(album_dir, cfg=None):
    """Resolve and write the album's / artist's RateYourMusic links.

    One RATEYOURMUSIC_ALBUM and one RATEYOURMUSIC_ARTIST on every track — the
    state the grader and the links editor read. A link already present on ANY
    track of the album is left alone and NOT looked up: the user's own link
    wins, and nothing here ever overwrites one. Only a link RYM itself
    confirmed (``server.integrations.rym_links``) is written, so a failed
    lookup writes nothing at all and reports "could not resolve" in its note
    instead of failing the import.

    Gated by `rym_links_auto` (config.py, default True). Returns
    ``{"album", "artist", "note", "written"}``; never raises.
    """
    from mlo.audio import AudioFile
    from server import integrations as intg

    cfg = cfg or load_config()
    out = {"album": None, "artist": None, "note": "", "written": 0}
    files = []
    for path in _audio_files(album_dir):
        try:
            af = AudioFile(path)
        except Exception:
            continue
        if af.audio is not None:
            files.append(af)
    if not files:
        return out

    def _tag(af, name):
        try:
            return str(af.get_tag(name) or "").strip()
        except Exception:
            return ""

    have_album = any(_tag(af, "RATEYOURMUSIC_ALBUM") for af in files)
    have_artist = any(_tag(af, "RATEYOURMUSIC_ARTIST") for af in files)
    if have_album and have_artist:
        return out                      # nothing missing: no lookup at all

    artist, album = _album_identity(album_dir)
    links = intg.rym_links(artist, album, cfg=cfg)
    out["note"] = links.get("note") or ""
    if not have_album:
        out["album"] = links.get("album")
    if not have_artist:
        out["artist"] = links.get("artist")
    if not out["album"] and not out["artist"]:
        return out

    for af in files:
        tags = {}
        if out["album"] and not _tag(af, "RATEYOURMUSIC_ALBUM"):
            tags["RATEYOURMUSIC_ALBUM"] = out["album"]
        if out["artist"] and not _tag(af, "RATEYOURMUSIC_ARTIST"):
            tags["RATEYOURMUSIC_ARTIST"] = out["artist"]
        if not tags:
            continue
        try:
            for key, value in tags.items():
                af.set_tag(key, value)
            out["written"] += 1
        except Exception:
            continue
    if out["written"]:
        _invalidate_caches()
    return out


# --------------------------------------------------------------------------- #
# Bulk queue
# --------------------------------------------------------------------------- #
_job_lock = threading.Lock()
_job = {"id": None, "kind": None, "status": "idle", "started": None,
        "finished": None, "total": 0, "done": 0, "label": "", "items": [],
        "error": None}


def job_state():
    """The bulk job the UI polls: same shape as ``soulseek_auto.job_state()``."""
    with _job_lock:
        return dict(_job, items=[dict(x) for x in _job["items"]])


def _job_update(**fields):
    with _job_lock:
        _job.update(fields)


def _job_note(done, label, item):
    """Live progress for a running job; a no-op for a direct bulk_import call."""
    with _job_lock:
        if _job["status"] != "running":
            return
        _job["done"] = done
        _job["label"] = label
        # the per-script stats of a whole library are noise in a poll payload
        _job["items"].append({k: v for k, v in item.items() if k != "scripts"})


def _stats_hook(done, total, desc):
    """Mirror progress into mlo.stats.progress_hook — the WS relay's source."""
    from mlo import stats as stats_mod
    hook = getattr(stats_mod, "progress_hook", None)
    if callable(hook):
        try:
            hook(done, total, desc)
        except Exception:
            pass


def _inside(path, folder):
    """Whether *path* is *folder* or below it (boundary-aware).

    Same test server.main's music-folder guard applies: ``C:\\Music2`` is not
    inside ``C:\\Music``, and paths on different drives never are.
    """
    try:
        ap = os.path.abspath(os.path.normpath(path))
        af = os.path.abspath(os.path.normpath(folder))
    except (OSError, ValueError, TypeError):
        return False
    if ap == af:
        return True
    if os.path.normcase(os.path.splitdrive(ap)[0]) != os.path.normcase(os.path.splitdrive(af)[0]):
        return False
    try:
        return os.path.normcase(os.path.commonpath([ap, af])) == os.path.normcase(af)
    except ValueError:
        return False


def _unique_dir(parent, name):
    """``<parent>/<name>``, deduplicated with " (2)", " (3)"…"""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name)).strip().rstrip(".") or "Import"
    dest = os.path.join(parent, safe)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(parent, f"{safe} ({n})")
        n += 1
    return dest


def _bulk_one(item, cfg):
    """Move one item into the library (when it is not there yet) and finish it.

    Idempotent by construction: a path that already sits inside the library is
    never moved (a second move would duplicate the album as "Album (2)"), it
    just re-runs the chain.
    """
    src = os.path.normpath(str(item.get("path") or ""))
    row = {"path": src, "status": "failed", "album_path": None,
           "error": None, "scripts": []}
    if not src or not os.path.exists(src):
        row["error"] = "path not found"
        return row

    folder = str(cfg.get("music_folder") or "").strip()
    if not folder or not os.path.isdir(folder):
        row["error"] = "music_folder not set or not found"
        return row

    root = library_root(folder)
    # Containment is not enough: the music folder itself, the library root and
    # the app's own .mlo state dir all live inside the music folder, so a
    # caller could queue the whole library (or config/playlists/trash) as if it
    # were an album — and a staging item would then be *moved* into a
    # subdirectory of itself. Album folders BELOW <music>/Artists are of course
    # legitimate, so only the two roots match exactly while the state dir
    # excludes its subtree. Checked before the "has audio?" test so the reason
    # is the real one, not "no audio files".
    def _same(a, b):
        try:
            return os.path.normcase(os.path.abspath(os.path.normpath(a))) == \
                   os.path.normcase(os.path.abspath(os.path.normpath(b)))
        except (OSError, ValueError, TypeError):
            return False

    for guard in (folder, root):
        if guard and _same(src, guard):
            row["error"] = "refusing to import the library root itself"
            return row
    try:
        from mlo.paths import app_data_dir
        # Scoped to THIS cfg's music folder: a bulk run must never treat the
        # config/playlists/trash of some other library as an album.
        state = app_data_dir(folder)
        if state and _inside(src, state):
            row["error"] = "refusing to import a library or app-state folder"
            return row
    except Exception:
        pass

    if os.path.isdir(src) and not _audio_files(src):
        # an empty/stray folder is not an album: importing it would publish a
        # library folder the import did not produce
        row["status"] = "skipped"
        row["error"] = "no audio files"
        return row

    move = item.get("move")
    if move is None:
        # staging paths are moved in; a folder that already IS in the library
        # (a re-run, an album imported by another path) is left where it is
        move = not _inside(src, root)
    if move and _inside(src, root):
        move = False
    album = src
    if move:
        dest = _unique_dir(root, os.path.basename(src.rstrip("\\/")))
        if not move_path(src, dest, log=lambda m: None):
            row["error"] = ("could not move into the library — a file inside "
                            "is still in use (stop playback and retry)")
            return row
        album = dest

    release = item.get("release")
    stamp_error = None
    if release:
        try:
            written, failed = _stamp_release(album, release, cfg)
            if failed:
                stamp_error = (f"release tags written to {written} file(s); "
                               f"{failed} file(s) could not be tagged")
        except Exception as e:
            traceback.print_exc()
            stamp_error = f"release stamping failed: {e}"

    try:
        finished = finish_album(album, cfg)
    except Exception as e:                      # finish_album promises not to raise
        traceback.print_exc()
        finished = {"path": album, "scripts": [], "chain": [], "errors": [str(e)]}

    row["album_path"] = finished["path"].replace("\\", "/")
    row["scripts"] = finished["scripts"]
    row["error"] = "; ".join(filter(None, [stamp_error, *finished["errors"]])) or None
    # "Imported" means the album is in the library. It is only honest to say
    # so when the chain actually ran (or when nothing is configured to run):
    # an album that landed with none of its scripts executed is unfinished,
    # and reporting it as imported is how a silent gap becomes a mystery
    # grading failure later.
    if finished["errors"] and not finished["scripts"]:
        row["status"] = "failed"
        row["error"] = f"imported, but the script chain did not run: {row['error']}"
    else:
        row["status"] = "imported"
    return row


def bulk_import(items, cfg=None, progress=None):
    """Import a queue of albums (staging folders or library folders).

    *items*: ``{"path", "release": <optional MB release dict>, "move": bool}``
    — ``move`` defaults to True for a path outside the library and False for
    one already inside it. Each album is moved into
    ``<music folder>/Artists``, stamped with the release identity when one is
    supplied, then finished with the configured chain. ``import_bulk_concurrency``
    albums run at once (each on its own copy of the config).

    Progress goes to *progress(done, total, label, item_result)* and to
    ``mlo.stats.progress_hook`` (the websocket relay); a running job started by
    :func:`start_bulk` is updated too. Returns ``{"total", "ok", "failed",
    "skipped", "items": [...]}`` with the item rows in input order.

    A row's ``status``: ``imported`` — the album is in the library (a failing
    *script* rides along in ``error``, it does not un-import the album);
    ``skipped`` — nothing to import (no audio files); ``failed`` — it never
    got into the library, and ``error`` says why.
    """
    cfg = cfg or load_config()
    items = [dict(it) for it in (items or [])]
    total = len(items)
    rows = [None] * total
    try:
        concurrency = max(1, int(cfg.get("import_bulk_concurrency") or 1))
    except (TypeError, ValueError):
        concurrency = 1

    def label_for(index):
        name = os.path.basename(str(items[index].get("path") or "").rstrip("\\/"))
        return f"Importing {name}" if name else f"Importing {index + 1}/{total}"

    if total:
        with ThreadPoolExecutor(max_workers=concurrency,
                                thread_name_prefix="mlo-import") as pool:
            futures = {pool.submit(_bulk_one, it, dict(cfg)): i
                       for i, it in enumerate(items)}
            done = 0
            for future in as_completed(futures):
                index = futures[future]
                try:
                    row = future.result()
                except Exception as e:          # _bulk_one reports, never raises
                    traceback.print_exc()
                    row = {"path": str(items[index].get("path") or ""),
                           "status": "failed", "album_path": None,
                           "error": str(e), "scripts": []}
                rows[index] = row
                done += 1
                label = label_for(index)
                _job_note(done, label, row)
                _stats_hook(done, total, label)
                if progress is not None:
                    try:
                        progress(done, total, label, row)
                    except Exception:
                        traceback.print_exc()

    rows = [r for r in rows if r is not None]
    return {
        "total": total,
        "ok": sum(1 for r in rows if r["status"] == "imported"),
        "failed": sum(1 for r in rows if r["status"] == "failed"),
        "skipped": sum(1 for r in rows if r["status"] == "skipped"),
        "items": rows,
    }


def start_bulk(items, cfg=None):
    """Start :func:`bulk_import` on a daemon thread; returns straight away.

    One bulk job at a time: a second start is refused with
    ``{"ok": False, "error": "bulk import already running"}``. Poll
    :func:`job_state` for progress.
    """
    with _job_lock:
        if _job["status"] == "running":
            return {"ok": False, "error": "bulk import already running"}
        _job.update({"id": uuid.uuid4().hex[:12], "kind": "bulk",
                     "status": "running", "started": time.time(),
                     "finished": None, "total": len(items or []), "done": 0,
                     "label": "", "items": [], "error": None})
        job = dict(_job, items=[])

    def run():
        try:
            result = bulk_import(items, cfg or load_config())
            _job_update(status="done", finished=time.time(),
                        done=result["total"], error=None)
        except Exception as e:
            traceback.print_exc()
            _job_update(status="failed", finished=time.time(), error=str(e))

    threading.Thread(target=run, name="mlo-bulk-import", daemon=True).start()
    return {"ok": True, "job": job}
