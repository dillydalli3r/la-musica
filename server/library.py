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
from mlo.grader import _grade_album
from mlo.audio import AudioFile
from mlo.paths import LIB_VIDEO_EXTS, get_sidecar_cover_path
from server import tagcache

# Tags surfaced per track for sorting/filtering on the frontend.
TRACK_TAGS = [
    "TITLE", "ARTIST", "ALBUMARTIST", "ALBUM", "DATE", "GENRE",
    "ITUNESADVISORY", "INSTRUMENTAL", "MEDIA", "SOURCE",
    "TRACKNUMBER", "DISCNUMBER",
    "MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_ALBUMARTISTID",
    "MUSICBRAINZ_ARTISTID", "MUSICBRAINZ_TRACKID",
    "MUSICBRAINZ_RELEASEGROUPID",
    "RATEYOURMUSIC_ALBUM", "RATEYOURMUSIC_TRACK", "RATEYOURMUSIC_ARTIST",
    "ALBUMARTISTSORT", "ORIGINALDATE", "RELEASETYPE", "RELEASECOUNTRY",
    "CATALOGNUMBER", "LABEL", "TRACKTOTAL", "DISCTOTAL",
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
    "RATEYOURMUSIC_ALBUM", "MEDIA", "CATALOGNUMBER", "LABEL",
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
    # Per-track sidecar cover: "01 - Song.jpg" next to "01 - Song.flac"
    try:
        sc = get_sidecar_cover_path(album_dir, tr["file"])
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


def build_album(album_dir, cfg):
    """Grade + enrich a single album. Returns the enriched dict or None."""
    res = _grade_album(album_dir, str(cfg.get("lyrics_format", "EMBEDDED")).upper(), cfg)
    if res is None:
        return None
    if "error" in res:
        res["path"] = album_dir.replace("\\", "/")
        res["tracks"] = []
        return res
    res["path"] = res["path"].replace("\\", "/")
    for tr in res.get("tracks", []):
        _enrich_track(tr, album_dir)
    res["meta"] = _album_meta(album_dir, res.get("tracks", []))
    tc = res.get("total_checks", 0)
    res["grade_pct"] = round(100.0 * res.get("pass_count", 0) / tc, 1) if tc else None
    res["pass"] = res.get("pass_count", 0) == tc and tc > 0
    return res


def build_albums_parallel(album_dirs, cfg):
    """Grade a list of album dirs concurrently, preserving order.

    Grading is mostly file I/O + image decode (Pillow releases the GIL),
    so a small thread pool cuts full-library scan time roughly by the
    worker count. Errors become {"path": ..., "error": ...} placeholders
    so one bad folder never hides the rest of the library.
    """
    workers = worker_count(cfg, default=min(8, os.cpu_count() or 1),
                           items=len(album_dirs))
    if workers <= 1 or len(album_dirs) <= 1:
        results = []
        for alb in album_dirs:
            try:
                a = build_album(alb, cfg)
                results.append(a if a is not None else
                               {"path": alb.replace("\\", "/"), "error": "no audio files", "tracks": []})
            except Exception as e:
                results.append({"path": alb.replace("\\", "/"), "error": str(e), "tracks": []})
        return results

    results = [None] * len(album_dirs)

    def _one(i, alb):
        try:
            a = build_album(alb, cfg)
            results[i] = a if a is not None else {
                "path": alb.replace("\\", "/"), "error": "no audio files", "tracks": []}
        except Exception as e:
            results[i] = {"path": alb.replace("\\", "/"), "error": str(e), "tracks": []}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, alb in enumerate(album_dirs):
            pool.submit(_one, i, alb)
    return results


def build_library(cfg, progress=None):
    """Full library tree with artist/album/track aggregates (TTL-cached)."""
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        return {"folder": folder, "artists": [], "error": "music_folder not set or not found"}

    cfg_key = (
        folder,
        cfg.get("lyrics_format"),
        cfg.get("worker_limit"),
        cfg.get("short_folder_names"),
    )

    def _build():
        albums = _find_albums(folder)
        artists = {}
        for alb in albums:
            artists.setdefault(os.path.dirname(alb), []).append(alb)

        result = []
        total = len(artists)
        for i, (artist_dir, alb_list) in enumerate(sorted(artists.items())):
            if progress:
                progress(i + 1, total, "Scanning library")
            albums_data = build_albums_parallel(sorted(alb_list), cfg)
            agg = _aggregate_albums(albums_data)
            result.append({
                "path": artist_dir.replace("\\", "/"),
                "name": os.path.basename(artist_dir),
                "albums": albums_data,
                "aggregate": agg,
            })
        return {"folder": folder.replace("\\", "/"), "artists": result}

    return tagcache.get_library(cfg_key, _build)