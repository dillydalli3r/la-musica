"""Tag-rich library payload builder for the v2 web UI.

Builds the artist -> album -> track tree from `mlo.grader` output and
enriches every track with sortable metadata (audio tech info + tags),
so the frontend can sort/filter by grade, audit, genre, year, advisory,
instrumental, MBIDs, RYM links, duration, bitrate, sample rate, etc.
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor

from mlo.stats import _find_albums, worker_count, WALK_FILES
from mlo.grader import _empty_folder_result, _find_empty_folders, _grade_album
from mlo.audio import AudioFile
from mlo.paths import (LIB_VIDEO_EXTS, load_expected_tracks, load_pending,
                       load_track_covers, _album_file, SIDECAR_COVER_EXTS)
from server import tagcache

# Tags surfaced per track for sorting/filtering on the frontend.
TRACK_TAGS = [
    "TITLE", "ARTIST", "ALBUMARTIST", "ALBUM", "DATE", "GENRE",
    "ITUNESADVISORY", "INSTRUMENTAL", "MEDIA", "SOURCE",
    "TRACKNUMBER", "DISCNUMBER",
    "MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_ALBUMARTISTID",
    "MUSICBRAINZ_ARTISTID", "MUSICBRAINZ_TRACKID",
    "MUSICBRAINZ_RELEASEGROUPID", "MUSICBRAINZ_RELEASETRACKID",
    "MUSICBRAINZ_WORKID",
    "RATEYOURMUSIC_ALBUM", "RATEYOURMUSIC_TRACK", "RATEYOURMUSIC_ARTIST",
    "ALBUMARTISTSORT", "ORIGINALDATE", "RELEASETYPE", "RELEASESTATUS",
    "RELEASECOUNTRY", "CATALOGNUMBER", "LABEL", "BARCODE", "SCRIPT",
    "TRACKTOTAL", "DISCTOTAL",
    "COMPOSER", "COPYRIGHT", "ISRC", "LYRICIST", "REMIXER",
    "DYNAMIC RANGE",
    # MOOD/ENERGY (script 8 or 16) ride along so the track page and the
    # `tag:MOOD` / `tag:ENERGY` columns show the pair without a second read.
    "MOOD", "ENERGY",
    # RATING is the user's own star rating (half-stars, `server/ratings.py`) and
    # BPM/INITIALKEY are script 12's analysis values: the library browser's
    # query engine filters on all three, so they have to reach the payload the
    # browser filters — three more cached tag reads per track, no extra decode.
    "RATING", "BPM", "INITIALKEY",
    # read on every track so _album_meta can lift the album-level value
    "ALBUM DYNAMIC RANGE",
]

# Tags read from the first track to represent album-level metadata.
ALBUM_LEVEL_TAGS = [
    "ALBUM", "ALBUMARTIST", "ARTIST", "DATE", "ORIGINALDATE", "ORIGINALYEAR",
    "ITUNESADVISORY", "ALBUMITUNESADVISORY",
    "MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_ALBUMARTISTID",
    "MUSICBRAINZ_RELEASEGROUPID",
    "RATEYOURMUSIC_ALBUM", "MEDIA", "CATALOGNUMBER", "LABEL", "BARCODE",
    "RELEASETYPE", "RELEASESTATUS", "RELEASECOUNTRY", "SCRIPT",
    "ALBUM DYNAMIC RANGE",
]

TECH_ATTRS = ("length", "bitrate", "sample_rate", "bits_per_sample", "channels")


def _read_tags(path):
    """Read TRACK_TAGS for a file path as a flat dict (None when missing)."""
    tags, _ = tagcache.read_track(path, TRACK_TAGS)
    return tags


def _parse_num(val):
    """Numeric part of a tag value: '3/12' -> 3, '1-04' -> 4, '01' -> 1."""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    if "-" in s:
        s = s.split("-")[-1]
    s = s.split("/")[0].strip()
    try:
        return int(s)
    except ValueError:
        return None


def _nums_from_filename(filename):
    """Fallback (disc, track) from a leading 'D-TT' / 'TT' filename stem."""
    import re
    stem = os.path.splitext(os.path.basename(filename))[0]
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)", stem)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^\s*(\d+)", stem)
    if m:
        return None, int(m.group(1))
    return None, None


def _video_title_from_filename(filename):
    """Display title for a music video with no TITLE tag.

    Strips the leading disc/track numbers and the trailing "[Music Video]" /
    "(MV)" style markers so "2-01 Yowamushi Mont Blanc [Music Video].mkv"
    displays as "Yowamushi Mont Blanc" instead of the raw file name. This is
    a DISPLAY fallback only — the tag itself stays unset until it is edited.
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.sub(r"^\s*\d+\s*-\s*\d+\s+", "", stem)
    stem = re.sub(r"^\s*\d+\s+", "", stem)
    stem = re.sub(r"\s*[\[\(](?:music\s*video|mv|pv|video)[\]\)]\s*$", "", stem, flags=re.I)
    stem = stem.strip(" -_")
    return stem or os.path.basename(filename)


def _album_cover_lookup(album_dir):
    """A `track filename -> cover path` function for ONE album folder.

    mlo.paths.get_track_cover answers this per track, and each answer re-reads
    the album's manifest and walks the folder's image extensions (a listdir
    per miss): a 12-track album paid that 12 times, a full scan 2000 times.
    Both the manifest and the folder listing belong to the FOLDER, so they are
    read once here. The per-track answer is the one get_track_cover gives —
    manifest entry first (and only when that image is really there), then the
    same-stem sidecar with mlo.paths.SIDECAR_COVER_EXTS order deciding.
    """
    try:
        manifest = {str(k).lower(): v
                    for k, v in load_track_covers(album_dir).items()}
    except Exception:
        manifest = {}
    listing = {}
    try:
        for name in os.listdir(album_dir):
            listing.setdefault(name.lower(), []).append(name)
    except OSError:
        listing = {}

    def cover_for(track_filename):
        name = os.path.basename(str(track_filename))
        image = manifest.get(name.lower())
        if image:
            found = _album_file(album_dir, image)
            if found:
                return found
        base = os.path.splitext(name)[0]
        for ext in SIDECAR_COVER_EXTS:
            entries = listing.get((base + ext).lower())
            if not entries:
                # Not in the listing means the file is not in the folder —
                # the isfile probe get_sidecar_cover_path makes here could
                # only ever fail.
                continue
            exact = base + ext
            for cand in ([exact] if exact in entries else entries):
                full = os.path.join(album_dir, cand)
                if os.path.isfile(full):
                    return full
        return None

    return cover_for


def _enrich_track(tr, album_dir, cover_for=None):
    """Add path, tech info, tags and per-track sidecar cover (cached reads).

    `cover_for` is the album's shared cover lookup (`_album_cover_lookup`);
    without it each track builds its own, which is what a single-track caller
    (the tag editor) still wants.
    """
    p = os.path.join(album_dir, tr["file"])
    tr["path"] = p.replace("\\", "/")
    tags, tech = tagcache.read_track(p, TRACK_TAGS)
    tr["tags"] = tags
    tr["tech"] = tech
    # Per-track cover: manifest entry ("01 - Song.jpg", possibly shared with
    # other tracks) or a same-stem sidecar next to "01 - Song.flac".
    try:
        sc = (cover_for or _album_cover_lookup(album_dir))(tr["file"])
        tr["cover_file"] = os.path.basename(sc) if sc else None
    except Exception:
        tr["cover_file"] = None
    # Numeric disc/track numbers for correct ordering ("1-10" must not
    # sort before "1-2"). Tags first, filename stem as fallback.
    tags_obj = tr.get("tags") or {}
    disc = _parse_num(tags_obj.get("DISCNUMBER"))
    num = _parse_num(tags_obj.get("TRACKNUMBER"))
    if num is None or disc is None:
        f_disc, f_num = _nums_from_filename(tr.get("file") or "")
        if num is None:
            num = f_num
        if disc is None:
            disc = f_disc
    tr["discnumber"] = disc
    tr["tracknumber"] = num
    # Music-video containers ride the same track shape; the UI uses this
    # to offer the video player / remux+tag tooling instead of the audio path.
    tr["is_video"] = p.lower().endswith(LIB_VIDEO_EXTS)
    # Untagged videos must not display their raw file name: derive a clean
    # title from the file name so the library, player bar and playlist
    # viewers show "Yowamushi Mont Blanc" instead of
    # "2-01 Yowamushi Mont Blanc [Music Video].mkv".
    if tr["is_video"] and not str((tags_obj.get("TITLE") or "")).strip():
        tr["tags"]["TITLE"] = _video_title_from_filename(tr["file"])
    # Per-track audit/grade convenience fields for sorting.
    tr["grade_pass"] = not tr.get("issues")
    tr["lyrics_present"] = bool(tr.get("lyrics_embedded") or tr.get("lyrics_lrc"))
    return tr


def _album_meta(album_dir, tracks):
    """Album-level metadata read from the first readable track."""
    meta = {}
    for tr in tracks:
        tags = tr.get("tags") or {}
        if tags:
            for t in ALBUM_LEVEL_TAGS:
                meta[t] = tags.get(t)
            break
    return meta


def _aggregate_albums(albums_data):
    """Artist-level aggregates from a list of album payloads."""
    total_checks = sum(a.get("total_checks", 0) for a in albums_data)
    pass_count = sum(a.get("pass_count", 0) for a in albums_data)
    track_count = sum(a.get("track_count", 0) for a in albums_data)
    audits = {a.get("audit_summary") for a in albums_data}
    audit = "FAKE" if "FAKE" in audits else ("REAL" if audits == {"REAL"} else
                                             ("Mix" if len(audits) > 1 else
                                              (next(iter(audits)) if audits else None)))
    return {
        "album_count": len(albums_data),
        "track_count": track_count,
        "pass_count": pass_count,
        "total_checks": total_checks,
        "grade_pct": round(100.0 * pass_count / total_checks, 1) if total_checks else None,
        "audit_summary": audit,
    }


def _album_artwork(album_dir, light=False):
    """Stored description metadata for an album folder.

    `light` (the library payload, which covers every album in one response)
    only answers "does a description exist?" — cheaply, from the file's size,
    without reading the text or touching the shared provenance map. The album
    page asks for the full payload instead.
    """
    if light:
        try:
            from mlo import artistdata
            path = artistdata.description_path(album_dir)
            present = bool(os.path.isfile(path) and os.path.getsize(path) > 0)
        except Exception:
            present = False
        return {"description": bool(present), "description_text": None,
                "description_source": None, "description_url": None}
    try:
        from mlo import artistdata
        present = artistdata.has_description(album_dir)
        text = (artistdata.read_description(album_dir) or "") if present else ""
        provenance = artistdata.read_provenance(album_dir)
    except Exception:
        present, text, provenance = False, "", {}
    return {
        "description": bool(present),
        "description_text": (text.strip() or None) if present else None,
        "description_source": (provenance.get("description_source")
                               or provenance.get("source")) if present else None,
        "description_url": provenance.get("description_source_url") if present else None,
    }


def _wish_state(wish_id, cfg):
    """The wish filling a framework album, as the album page reads it.

    `status`, `attempts` and the reason a run left behind come straight from
    the queue row; `due_at`/`due_in`/`terminal` answer "when will it be tried
    again" the way the worker itself decides it (`server.wishes.due_at`), so a
    page can say "searching — attempt 2, next try in 12 min" without owning a
    second copy of the retry policy. None when there is no wish to report.

    One folder's wish — the album page's own read. A payload that lists EVERY
    pending album takes `_wish_lookup` instead, which reads the queue once.
    """
    if not wish_id:
        return None
    try:
        from server import wishes
        w = wishes.get_wish(int(wish_id))
    except Exception:
        return None
    return _wish_state_of(w, cfg)


def _wish_lookup(cfg):
    """``wish_id -> that wish's state``, reading the queue ONCE per payload.

    Every album-shaped payload (library tree, album page, Home shelves, the
    query's album rows — they all read this same row) carries the pending
    album's wish through this one map, so a page listing every pending album
    never asks the queue per row: `server.wishes.list_wishes` is one query, and
    it is not even run until a row actually has a wish to report.
    """
    rows = None

    def state(wish_id):
        nonlocal rows
        if not wish_id:
            return None
        if rows is None:
            try:
                from server import wishes
                rows = {w.get("id"): w for w in (wishes.list_wishes() or [])}
            except Exception:
                rows = {}
        return _wish_state_of(rows.get(int(wish_id)), cfg)

    return state


def _wish_state_of(w, cfg):
    """The state block for ONE queue row (None when there is none)."""
    if not w:
        return None
    try:
        from server import wishes
        due = wishes.due_at(w, cfg)
        terminal = wishes.is_terminal(w, cfg)
    except Exception:
        due, terminal = 0.0, False
    import math
    import time as _time
    due_in = None if (terminal or not math.isfinite(due)) else max(0, int(due - _time.time()))
    return {
        "id": w.get("id"),
        "status": str(w.get("status") or ""),
        "attempts": int(w.get("attempts") or 0),
        "retry_at": float(w.get("retry_at") or 0.0),
        "last_search": float(w.get("last_search") or 0.0),
        "due_at": None if (terminal or not math.isfinite(due)) else float(due),
        "due_in": due_in,
        "terminal": bool(terminal),
        "reason": str(w.get("last_error") or w.get("note") or ""),
        "note": str(w.get("note") or ""),
        "source": str(w.get("source") or ""),
        "queries": list(w.get("queries") or []),
    }


def pending_album_payload(folder, cfg, light=False):
    """The ALBUM PAGE's payload for a framework album, or None when *folder*
    is not one (`server.pending_albums`: the folder "Add to library" created
    before any audio exists).

    The same row the library tree lists (`_pending_album_row`) — the release's
    own tracklist, every entry missing, the placeholder cover, the marker's
    identity — enriched the way `build_album` enriches a real album: the
    artwork the add pre-fetched (the description's text unless `light`) and the
    wish that is filling the folder. A folder the app created on purpose is
    never a 404 on a page that was linked to it.
    """
    if not load_pending(folder):
        return None
    from mlo.paths import library_root
    root = library_root(str(cfg.get("music_folder") or ""))
    # One folder: read that one wish (`_wish_state`), not the whole queue.
    row = _pending_album_row(folder, root,
                             lambda wid: _wish_state(wid, cfg))
    row["artwork"] = _album_artwork(folder, light=light)
    return row


def build_album(album_dir, cfg, light=False):
    """Grade + enrich a single album. Returns the enriched dict or None."""
    res = _grade_album(album_dir, str(cfg.get("lyrics_format", "EMBEDDED")).upper(), cfg)
    if res is None:
        # No audio to grade — but a FRAMEWORK album is one the app itself put
        # in the library before its audio arrived, so its page renders what
        # exists (identity, pre-fetched artwork, the release tracklist, the
        # wish filling it) instead of "not found". Any other audio-less folder
        # keeps answering None.
        return pending_album_payload(album_dir, cfg, light=light)
    if "error" in res:
        res["path"] = album_dir.replace("\\", "/")
        res["tracks"] = []
        res["artwork"] = _album_artwork(album_dir, light=True)
        return res
    res["path"] = res["path"].replace("\\", "/")
    # One folder listing + one manifest read for the album's tracks.
    cover_for = _album_cover_lookup(album_dir)
    for tr in res.get("tracks", []):
        _enrich_track(tr, album_dir, cover_for)
    res["meta"] = _album_meta(album_dir, res.get("tracks", []))
    _add_expected_tracks(res, album_dir)
    # Every row carries the flag, so no reader has to treat "absent" as a case
    # of its own. A folder whose audio HAS arrived is a normal album: the
    # import that filled it cleared the framework marker on its way through.
    res["pending"] = False
    res["artwork"] = _album_artwork(album_dir, light=light)
    tc = res.get("total_checks", 0)
    res["grade_pct"] = round(100.0 * res.get("pass_count", 0) / tc, 1) if tc else None
    # Same rule as the grader's own PASS — failed == 0 (run_grade_library) — so
    # an album whose checks are all switched off is a PASS here too instead of
    # disagreeing with the Grade script. grade_pct stays null for it: there is
    # nothing to show a percentage of.
    res["pass"] = res.get("pass_count", 0) == tc
    return res


def _empty_album_row(folder, root):
    """Library row for a folder with no audio track anywhere beneath it.

    The Grade script's EMPTY_FOLDER row (mlo.grader._empty_folder_result) in
    the shape this payload uses. The album tree is built from audio files, so
    without it such a folder — and the failure it reports — would only ever be
    visible in the run's output.
    """
    row = _empty_folder_result(folder, root)
    row["path"] = row["path"].replace("\\", "/")
    # grade_pct / pass exactly as build_album derives them (nothing passed out
    # of one failed check), and a null audit: the artist rollup folds album
    # audits together, and "" would read as a verdict of its own.
    row["grade_pct"] = 0.0
    row["pass"] = False
    row["audit_summary"] = None
    return row


def framework_dirs(dir_scan):
    """The FRAMEWORK folders in a library walk's own directory scan.

    `_walk_files` records what each directory held (`WALK_FILES` = files but no
    audio), which is exactly the shape a framework album has: the release
    manifest, the placeholder cover and the pending marker, and no audio at
    all. Only a marker makes it one — an audio-less folder WITHOUT one is the
    empty-folder case (`_find_empty_folders`), not a placeholder.
    """
    return [d for d, held in dir_scan.items()
            if held == WALK_FILES and load_pending(d)]


def pending_album_dirs(artist_dir, dir_scan=None):
    """The FRAMEWORK albums directly under *artist_dir*, sorted.

    The artist page finds its albums by walking for AUDIO (`_find_albums`), so
    a folder whose audio has not arrived yet would never be listed there — and
    an album the user asked for that the artist's own page hides is the one
    gap this closes. Same marker read as the library tree (`framework_dirs`):
    pass the walk's directory scan when the caller has one, else this reads the
    subtree itself (one walk, the same `_find_albums` the caller needs anyway).
    """
    if dir_scan is None:
        dir_scan = {}
        _find_albums(artist_dir, dir_scan)
    low = os.path.normpath(artist_dir).lower()
    return sorted(d for d in framework_dirs(dir_scan)
                  if os.path.dirname(d).lower() == low)


def _pending_album_row(folder, root, wish_state=None):
    """Library row for a FRAMEWORK album (see ``server.pending_albums``): the
    folder "Add to library" created before any audio arrived.

    The folder is in the library the second it is asked for, so it is listed —
    with the two things that must never lie: ``track_count`` is 0 (there is no
    file on disk to play) and ``pending`` says why the album is here and empty.
    The track list the album page renders comes from the release manifest the
    framework album was created with, so the page greys out exactly the tracks
    the search is still filling, and the placeholder cover is the album's cover
    until the import writes a real one.

    *wish_state* is `_wish_lookup(cfg)` when the payload lists more than one
    album (library tree, Home, a query — all of them read this same row): the
    queue is then read ONCE for the whole payload. A caller with a single
    folder to answer for (the album page) leaves it out and reads that one
    wish itself, in `pending_album_payload`.
    """
    row = _empty_folder_result(folder, root)
    info = load_pending(folder) or {}
    row["path"] = row["path"].replace("\\", "/")
    # Nothing was graded — the empty-folder row's single failed check would
    # report a deliberately audio-less placeholder as a broken album.
    row["total_checks"] = 0
    row["pass_count"] = 0
    row["issues"] = {}
    row["grade_pct"] = None
    row["pass"] = False
    row["audit_summary"] = None
    row["pending"] = True
    row["pending_reason"] = str(info.get("waiting_for") or "")
    wid = info.get("wish_id")
    row["wish_id"] = wid
    # The wish filling this folder, in the payload's own row: a reader states
    # "searching — attempt 2, next try in 12 min" / "nothing is searching for
    # it right now" from HERE rather than asking the queue again per row.
    row["wish"] = wish_state(wid) if wish_state else None
    cover = info.get("cover") or {}
    row["cover_file"] = str(cover.get("file") or "")
    row["cover_ok"] = bool(row["cover_file"])
    row["cover_detail"] = ("release-group cover (placeholder)"
                           if row["cover_file"] else "")
    row["album_artist"] = str(info.get("artist") or "")
    meta = {t: None for t in ALBUM_LEVEL_TAGS}
    # The links and the page content the ADD pre-fetched (`server.imports
    # .prefetch_album`): they are on the marker because a framework album has
    # no tags to carry them yet, and the album's page shows them from the
    # moment the album is added.
    links = info.get("links") or {}
    meta.update({
        "ALBUM": info.get("title") or None,
        "ALBUMARTIST": info.get("artist") or None,
        "ARTIST": info.get("artist") or None,
        "DATE": info.get("date") or info.get("year") or None,
        "MUSICBRAINZ_ALBUMID": info.get("release_id") or None,
        "MUSICBRAINZ_RELEASEGROUPID": info.get("release_group_id") or None,
        "RELEASETYPE": info.get("release_type") or None,
        "RATEYOURMUSIC_ALBUM": links.get("album") or None,
        "RATEYOURMUSIC_ARTIST": links.get("artist") or None,
    })
    row["meta"] = meta
    for key, val in (("ALBUM", info.get("title")), ("ALBUMARTIST", info.get("artist")),
                     ("ARTIST", info.get("artist")), ("DATE", info.get("year")),
                     ("RATEYOURMUSIC_ALBUM", links.get("album")),
                     ("RATEYOURMUSIC_ARTIST", links.get("artist"))):
        if key in row["album_values"]:
            row["album_values"][key] = str(val or "").strip()
    # The release's own tracklist, with nothing on disk matching it: every
    # entry is missing, which is exactly what the album page should show.
    _add_expected_tracks(row, folder)
    row["artwork"] = _album_artwork(folder, light=True)
    # What the ADD already fetched for this folder (see `server.imports
    # .prefetch_album`): the artist image and the descriptions are listed with
    # the paths they were written to, so a reader can see the page content
    # exists before a byte of audio does.
    row["prefetched"] = info.get("prefetched") or None
    return row


def _add_expected_tracks(res, album_dir):
    """Attach the recorded release tracklist, flagged present/missing.

    A partially imported album has no on-disk trace of the tracks that never
    arrived, so the release's own running order (written by the import
    wizard) is diffed against the files here. `expected_tracks` carries every
    release track with a `missing` flag; `partial` is the album-level "this
    is not the whole release" answer the album page greys out on."""
    res.setdefault("expected_tracks", [])
    res.setdefault("partial", False)
    try:
        exp = load_expected_tracks(album_dir)
    except Exception:
        return
    tracks = exp.get("tracks") or []
    if not tracks:
        return
    # (disc, track) as the library derived them — tags first, file name
    # fallback — so an untagged partial import still lines up.
    on_disk = {(tr.get("discnumber") or 1, tr.get("tracknumber"))
               for tr in res.get("tracks", [])}
    rows = [{**e, "missing": (e["disc"], e["position"]) not in on_disk}
            for e in tracks]
    res["expected_tracks"] = rows
    res["expected_release_id"] = exp.get("release_id")
    res["partial"] = any(r["missing"] for r in rows)


def build_albums_parallel(album_dirs, cfg, light=False):
    """Grade a list of album dirs concurrently, preserving order.

    Grading is mostly file I/O + image decode (Pillow releases the GIL),
    so a small thread pool cuts full-library scan time roughly by the
    worker count. Errors become {"path": ..., "error": ...} placeholders
    so one bad folder never hides the rest of the library. `light` is passed
    through to `build_album` (the library payload only needs to know whether
    a description exists, not its text).
    """
    workers = worker_count(cfg, default=min(8, os.cpu_count() or 1),
                           items=len(album_dirs))
    if workers <= 1 or len(album_dirs) <= 1:
        results = []
        for alb in album_dirs:
            try:
                a = build_album(alb, cfg, light=light)
                results.append(a if a is not None else
                               {"path": alb.replace("\\", "/"), "error": "no audio files", "tracks": []})
            except Exception as e:
                results.append({"path": alb.replace("\\", "/"), "error": str(e), "tracks": []})
        return results

    results = [None] * len(album_dirs)

    def _one(i, alb):
        try:
            a = build_album(alb, cfg, light=light)
            results[i] = a if a is not None else {
                "path": alb.replace("\\", "/"), "error": "no audio files", "tracks": []}
        except Exception as e:
            results[i] = {"path": alb.replace("\\", "/"), "error": str(e), "tracks": []}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, alb in enumerate(album_dirs):
            pool.submit(_one, i, alb)
    return results


def library_cache_key(cfg):
    """The tagcache key for a library payload.

    Exposed so callers that need the same payload (the discovery routes
    matching owned albums, "more like this" exclusions) read the SAME cache
    entry instead of building the tree a second time under their own key.
    """
    return (
        cfg.get("music_folder") or "",
        cfg.get("lyrics_format"),
        cfg.get("worker_limit"),
        cfg.get("short_folder_names"),
    )


def build_library(cfg, progress=None):
    """Full library tree with artist/album/track aggregates (TTL-cached)."""
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        return {"folder": folder, "artists": [], "error": "music_folder not set or not found"}

    cfg_key = library_cache_key(cfg)

    def _build():
        # One walk feeds the album list and the empty-folder sweep below (the
        # same directory scan mlo.grader's run fills).
        dir_scan = {}
        albums = _find_albums(folder, dir_scan)
        artists = {}
        for alb in albums:
            artists.setdefault(os.path.dirname(alb), []).append(alb)
        # A folder with no audio track anywhere beneath it never becomes an
        # album, so the tree would never show the EMPTY_FOLDER failures the
        # Grade script reports for it. The run's own row is added under the
        # folder it sits in, so the library and the run name the same problems.
        empty_rows = {}
        if cfg.get("grade_check_empty_folders", True):
            for d in _find_empty_folders(folder, dir_scan):
                artists.setdefault(os.path.dirname(d), [])
                empty_rows.setdefault(os.path.dirname(d), []).append(d)

        # Framework albums: folders holding the release manifest, the
        # placeholder cover and a pending marker but no audio at all, so the
        # album walk above never sees them. They ARE albums the user asked for,
        # so they are listed — read from the SAME directory scan the walk
        # produced (directories holding files but no audio) and only where the
        # marker is: an audio-less folder WITHOUT one is the empty-folder case
        # below, not a placeholder.
        pending_rows = {}
        for d in framework_dirs(dir_scan):
            parent = os.path.dirname(d)
            artists.setdefault(parent, [])
            pending_rows.setdefault(parent, []).append(d)

        # One queue read for the whole payload, so the pending rows' wish
        # state costs no lookup per album (`_wish_lookup`).
        wish_state = _wish_lookup(cfg)

        result = []
        total = len(artists)
        for i, (artist_dir, alb_list) in enumerate(sorted(artists.items())):
            if progress:
                progress(i + 1, total, "Scanning library")
            albums_data = (build_albums_parallel(sorted(alb_list), cfg, light=True)
                           if alb_list else [])
            albums_data.extend(_pending_album_row(d, folder, wish_state)
                               for d in pending_rows.get(artist_dir, []))
            albums_data.extend(_empty_album_row(d, folder)
                               for d in empty_rows.get(artist_dir, []))
            albums_data.sort(key=lambda a: str(a.get("path", "")).lower())
            agg = _aggregate_albums(albums_data)
            result.append({
                "path": artist_dir.replace("\\", "/"),
                "name": os.path.basename(artist_dir),
                "albums": albums_data,
                "aggregate": agg,
            })
        return {"folder": folder.replace("\\", "/"), "artists": result}

    return tagcache.get_library(cfg_key, _build)