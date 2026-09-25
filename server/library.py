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
from mlo.grader import (_empty_folder_result, _find_empty_folders, _grade_album,
                        printed_pct)
from mlo.audio import AudioFile
from mlo.artistdata import has_image, strip_mbid_suffix
from mlo.paths import (expected_tracks_state, LIB_VIDEO_EXTS,
                       load_expected_tracks, load_pending,
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
    # The podcast identity (mlo.naming's DERIVED type): the SERIES MusicBrainz
    # links an episode's release group to, and the episode number it states.
    # Read here so the Podcasts shelf, the series page and the Podcasts
    # preset answer a scan without asking MusicBrainz once.
    "PODCASTSERIES", "PODCASTSERIESMBID", "PODCASTEPISODE",
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
    # Album-level like every other release fact: one episode folder states one
    # series (the tag is written to every file of it, and the first readable
    # track is what an album-level value is read from).
    "PODCASTSERIES", "PODCASTSERIESMBID", "PODCASTEPISODE",
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
    # WHICH KIND the track's stored lyrics are — "synced", "plain" or null —
    # stamped by mlo.grader beside the `lyrics_embedded` / `lyrics_lrc` flags
    # it reads the same two stored texts for (`mlo.lyrics.stored_lyrics_kind`),
    # so this is the stored truth, not a second opinion about it.
    kind = tr.get("lyrics_kind") or None
    tr["lyrics_kind"] = kind
    # Presence is the kind not being null: one fact, two fields, and no reader
    # can ever see them disagree — `lyrics_present` stays because the queries,
    # the players and the grading stats read it.
    tr["lyrics_present"] = bool(kind)
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


def podcast_info(meta):
    """The podcast block an album row carries, or None.

    An episode is a release group MusicBrainz links `part of` a series of type
    Podcast; the app records that on the files (mlo.autotag writes
    PODCASTSERIES / PODCASTSERIESMBID / PODCASTEPISODE), so a scan reads it off
    the album's own tags and never asks MusicBrainz again — and a rescan, a
    moved folder or a fresh install sees the same fact.

    `series` is the name a reader sees (with MusicBrainz's disambiguation when
    it stated one, which is what keeps two same-named shows apart), and
    `episode` is MusicBrainz's own episode number or None when it states none.
    None (not an empty block) for everything that is not an episode — which is
    every music album, so no surface has to test the fields one by one.
    """
    meta = meta or {}
    series = str(meta.get("PODCASTSERIES") or "").strip()
    if not series:
        return None
    return {
        "series": series,
        "series_mbid": str(meta.get("PODCASTSERIESMBID") or "").strip() or None,
        "episode": _parse_num(meta.get("PODCASTEPISODE")),
    }


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
        "grade_pct": printed_pct(pass_count, total_checks),
        # The artist's own verdict, by the same rule the albums use (failed ==
        # 0) and requiring at least one graded check, so an artist with no
        # albums at all is not handed a pass for nothing. It is a field rather
        # than something the UI derives from grade_pct because a percentage is
        # a summary: "this artist has a failed album" is a fact, and reading it
        # off arithmetic on a rounded number is how a green dot once sat over a
        # failed album.
        "pass": bool(total_checks) and pass_count == total_checks,
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
        walk = wishes.candidate_state(w, cfg)
    except Exception:
        due, terminal, walk = 0.0, False, None
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
        # WHICH ranked candidate the search is on (spec R150-R154): the album a
        # pending row links to says where its acquisition is ("Release 2 of 3")
        # from the SAME block the queue row reads, so the tile and the queue row
        # can never disagree about it. None for a wish with one candidate.
        "walk": walk,
        # The release identity the wish was recorded with (its own
        # `release_json` column, read once by `wishes._row`): the pressing's
        # medium, countries, catalogue number and label. A FRAMEWORK album has
        # no tags to carry them yet, so `_pending_album_row` takes the facts it
        # shows from HERE — one block already in the row, no second query and
        # no MusicBrainz request for a payload the app itself wrote down.
        "release": dict(w.get("release") or {}),
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
    # The DERIVED podcast identity of this album, read from its own tags (see
    # podcast_info) — the field the Home shelf, the series page, the Podcasts
    # preset and the Artist page's Podcast bucket all read. None for anything
    # that is not an episode.
    res["podcast"] = podcast_info(res["meta"])
    _add_expected_tracks(res, album_dir)
    # Every row carries the flag, so no reader has to treat "absent" as a case
    # of its own. A folder whose audio HAS arrived is a normal album: the
    # import that filled it cleared the framework marker on its way through.
    # A folder with NO audio is never a normal album, whatever its wish says —
    # a wish wrongly marked "imported" (or a marker lost to an interrupted
    # import) left a framework row rendering as a playable album with a play
    # button over nothing, which is exactly what the owner saw on screen.
    res["pending"] = not res.get("tracks")
    res["artwork"] = _album_artwork(album_dir, light=light)
    tc = res.get("total_checks", 0)
    # Printed, so it obeys the one rule (mlo.grader.printed_pct): a Fail badge
    # can never read "100% of checks passed".
    res["grade_pct"] = printed_pct(res.get("pass_count", 0), tc)
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


def _pending_release_identity(info, wish):
    """The release identity tags a FRAMEWORK album's own record already states.

    The marker (`server.pending_albums`) names the release, its artist and its
    date; the WISH the folder was created with carries the identity block the
    add resolved (`server.wishes.RELEASE_KEYS`: medium, countries, catalogue
    number, label, status…). Between the two, a folder whose audio has not
    arrived can say which pressing it is getting — the same facts the import
    then writes into the files, so the tile does not change its mind once the
    download lands.

    A value nobody resolved is ABSENT, never guessed: an identity that states
    no country contributes no country, and the slot stays empty for a reader to
    show as empty. The tag spellings are the app's own — RELEASECOUNTRY is the
    ";"-joined list every writer stores (`mlo.tagtext._LIST_SEP`, the value
    `mlo.autotag._release_country_codes` builds) — so the tile's badge and the
    file's tag can never read differently.
    """
    out = {}
    rel = (wish or {}).get("release") if isinstance(wish, dict) else None
    if not isinstance(rel, dict):
        rel = {}
    codes = [str(c).strip() for c in (rel.get("countries") or []) if str(c).strip()]
    if not codes:
        codes = [str(rel.get("country") or "").strip()]
    countries = "; ".join(c for c in codes if c)
    media = [str(m).strip() for m in (rel.get("media") or []) if str(m).strip()]
    for tag, value in (("MEDIA", media[0] if media else ""),
                       ("RELEASECOUNTRY", countries),
                       ("CATALOGNUMBER", rel.get("catalog_number")),
                       ("LABEL", rel.get("label")),
                       ("RELEASESTATUS", rel.get("status")),
                       # The marker's own date wins over the identity's (it is
                       # the date the folder was created with, and the naming
                       # script has already named the folder after it).
                       ("DATE", (str(info.get("date") or info.get("year") or "")
                                 or rel.get("date")))):
        value = str(value or "").strip()
        if value:
            out[tag] = value
    return out


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
    # THE RELEASE'S OWN FACTS, while it is still arriving (the owner's ask:
    # an album being imported must not read as a blank cell). The pressing's
    # medium, its release countries, its catalogue number and label are facts
    # the ADD already resolved — the wish row the framework album was created
    # with carries the release identity (`server.wishes.release_identity`),
    # and the SAME values are what the import then writes into the files
    # (`server.imports._stamp_release_identity`), so the tile shows the
    # pressing it is getting and keeps showing it after the audio lands.
    #
    # Nothing here is invented: an identity nobody resolved states nothing and
    # the slots stay empty, exactly like the technical readout — no file is on
    # disk to probe, so the codec/bitrate half of the card cannot be known and
    # is left for the audio (`albumTech` reads the tracks' own tech).
    identity = _pending_release_identity(info, row.get("wish"))
    for tag, value in identity.items():
        if not meta.get(tag):
            meta[tag] = value
    if identity.get("MEDIA"):
        # The row's own medium field: the album page's readout reads it beside
        # the media tag, and the card prefers it over the meta value.
        row["media"] = identity["MEDIA"]
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
    wizard, or read off the rip's own .cue/.log) is diffed against the files
    here. `expected_tracks` carries every release track with a `missing` flag;
    `partial` is the album-level "this is not the whole release" answer the
    album page greys out on.

    The diff itself lives in mlo.paths.expected_tracks_state, because the
    grader asks the same question (a partial CD must not be graded on a full
    disc's evidence) and two readings of "is this album complete" would
    eventually disagree."""
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
    # fallback — so an untagged partial import still lines up; plus the file
    # names, which is how a track the rip's own .cue names is found even when
    # nothing about it says which position it occupies.
    on_disk = {(tr.get("discnumber") or 1, tr.get("tracknumber"))
               for tr in res.get("tracks", [])}
    names = [tr.get("file") for tr in res.get("tracks", []) if tr.get("file")]
    rows = expected_tracks_state(tracks, on_disk, names)
    res["expected_tracks"] = rows
    res["expected_release_id"] = exp.get("release_id")
    present = sum(1 for r in rows if not r["missing"])
    res["partial"] = present < len(rows)
    if res["partial"]:
        # The album page's own words for why it is not the whole release.
        res["partial_reason"] = (f"{present} of {len(rows)} tracks of the "
                                 f"album's tracklist are in this folder")


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


def _artist_display_name(artist_dir, albums_data):
    """The artist's name as a page should draw it — ``name`` stays the folder's
    own basename, which is library IDENTITY (paths, lookups), not a caption.

    The naming script builds artist folders as "Slowdive [a16371b9-…]" (and
    ``short_folder_names`` truncates the id), so Home's artist shelf was
    drawing "Radiohead [a74b1b7f-71a5-4011-9441-d0b5e4122711]" at the reader:
    the folder name is where the id lives, and the id is not part of the name.

    A tag-derived album artist wins — the same first non-empty ALBUMARTIST the
    artist page reads for its header (main.py's ``/api/artist``), so both pages
    spell the artist the way the tags do; the folder name minus the id is the
    answer for a folder whose audio carries no artist tag at all (an empty or
    framework-only folder)."""
    from_tags = next((str(a.get("album_artist") or "").strip()
                      for a in albums_data if a.get("album_artist")), "")
    return from_tags or strip_mbid_suffix(os.path.basename(artist_dir))


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


def _release_identity(row):
    """The MusicBrainz release a row is ABOUT, or "" when it states none.

    The ONE identity a framework row and the album it becomes have in common:
    both carry the release id in their album-level tags (a placeholder from its
    marker, a real album from its files). The release GROUP is the fallback,
    for a wish keyed by the group and an album whose tracks state only that.
    """
    meta = row.get("meta") or {}
    for key in ("MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_RELEASEGROUPID"):
        value = str(meta.get(key) or "").strip().lower()
        if value:
            return value
    return ""


def _drop_filled_placeholders(result):
    """ONE RELEASE IS ONE TILE — even mid-import.

    A framework album is a row of its own (a folder with a marker and no
    audio) and the album the audio landed in is another, so a release that
    exists as TWO folders was listed twice, for as long as the placeholder
    survived: the chain's own end clears it (`imports._finish_album` →
    `pending_albums.clear_if_filled`), which is minutes of a duplicate tile the
    user watches — and a placeholder whose chain never ends (a review stop, a
    crash) stayed a duplicate for good. Only the same folder was ever compared
    before, and nothing compared the two rows.

    The placeholder YIELDS to the album: a release already represented by a row
    with audio is not also a pending one. Rows are matched on the release id
    (the group id as a fallback), never on the folder name — the whole point is
    that the two folders are named differently. A placeholder whose release is
    NOT in the payload keeps its row: that is the album the user asked for and
    nothing has filled yet.
    """
    real = set()
    for ar in result:
        for row in ar["albums"]:
            if row.get("pending"):
                continue
            ident = _release_identity(row)
            if ident:
                real.add(ident)
    if not real:
        return
    for ar in result:
        kept = []
        for row in ar["albums"]:
            if row.get("pending") and _release_identity(row) in real:
                continue        # the album itself is here: not a second one
            kept.append(row)
        if len(kept) != len(ar["albums"]):
            ar["albums"] = kept
            ar["aggregate"] = _aggregate_albums(kept)


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
            try:
                artist_image = has_image(artist_dir)
            except Exception:
                # A folder this cannot read holds no picture the app can serve,
                # and a row that draws its initial is the honest rendering of
                # that — not a broken-image glyph from a URL that would 404.
                artist_image = False
            result.append({
                "path": artist_dir.replace("\\", "/"),
                "name": os.path.basename(artist_dir),
                "display_name": _artist_display_name(artist_dir, albums_data),
                "albums": albums_data,
                "aggregate": agg,
                # Whether `GET /api/artist/image` would answer for this folder:
                # a listing, not a walk, and the same question Home's shelf asks
                # (`mlo.artistdata.has_image`), so the Artists view draws an
                # artist's own picture and every other row an initial.
                "has_image": artist_image,
            })
        _drop_filled_placeholders(result)
        return {"folder": folder.replace("\\", "/"),
                "artists": [ar for ar in result if ar["albums"]]}

    return tagcache.get_library(cfg_key, _build)