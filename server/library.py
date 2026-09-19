"""Tag-rich library payload builder for the v2 web UI.

Builds the artist -> album -> track tree from `mlo.grader` output and
enriches every track with sortable metadata (audio tech info + tags),
so the frontend can sort/filter by grade, audit, genre, year, advisory,
instrumental, MBIDs, RYM links, duration, bitrate, sample rate, etc.
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor

from mlo.stats import _find_albums, worker_count
from mlo.grader import _empty_folder_result, _find_empty_folders, _grade_album
from mlo.audio import AudioFile
from mlo.paths import LIB_VIDEO_EXTS, get_track_cover, load_expected_tracks
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


def _enrich_track(tr, album_dir):
    """Add path, tech info, tags and per-track sidecar cover (cached reads)."""
    p = os.path.join(album_dir, tr["file"])
    tr["path"] = p.replace("\\", "/")
    tags, tech = tagcache.read_track(p, TRACK_TAGS)
    tr["tags"] = tags
    tr["tech"] = tech
    # Per-track cover: manifest entry ("01 - Song.jpg", possibly shared with
    # other tracks) or a same-stem sidecar next to "01 - Song.flac".
    try:
        sc = get_track_cover(album_dir, tr["file"])
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


def build_album(album_dir, cfg, light=False):
    """Grade + enrich a single album. Returns the enriched dict or None."""
    res = _grade_album(album_dir, str(cfg.get("lyrics_format", "EMBEDDED")).upper(), cfg)
    if res is None:
        return None
    if "error" in res:
        res["path"] = album_dir.replace("\\", "/")
        res["tracks"] = []
        res["artwork"] = _album_artwork(album_dir, light=True)
        return res
    res["path"] = res["path"].replace("\\", "/")
    for tr in res.get("tracks", []):
        _enrich_track(tr, album_dir)
    res["meta"] = _album_meta(album_dir, res.get("tracks", []))
    _add_expected_tracks(res, album_dir)
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

        result = []
        total = len(artists)
        for i, (artist_dir, alb_list) in enumerate(sorted(artists.items())):
            if progress:
                progress(i + 1, total, "Scanning library")
            albums_data = (build_albums_parallel(sorted(alb_list), cfg, light=True)
                           if alb_list else [])
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