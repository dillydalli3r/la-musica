"""Script 13 — Fetch Lyrics (LRCLIB).

Downloads missing lyrics for every track from lrclib.net and writes them
per the global ``lyrics_format`` (EMBEDDED / LRC / BOTH), canonicalized
with the same formatting rules as script 1. Tracks tagged INSTRUMENTAL=1
and tracks that already carry lyrics (embedded or an .lrc sidecar) are
skipped unless the run is forced. Standard library only, so the runner
works in every install (no httpx dependency).
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .audio import AudioFile
from .lyrics import (
    _atomic_write_text, _format_for_storage, _lrc_for,
    _process_lyrics_for_audio,
)
from .paths import AUDIO_EXTS
from .stats import (
    is_audio_file, new_stats, _collect_targets, _find_albums,
    _make_pbar, _pbar_skip, _pbar_update,
)
from .ui import print_header, log, c, Color

LRCLIB_BASE = "https://lrclib.net/api"
_USER_AGENT = "MusicLibraryOptimizer/2 (la musica)"
_throttle_lock = threading.Lock()
_last_request = 0.0


def _lrclib_get(endpoint, params, timeout=15, retries=3):
    """Rate-throttled LRCLIB GET with retry on 429/5xx (they throttle IPs).
    Returns the decoded JSON body, or None for not-found / errors."""
    global _last_request
    url = f"{LRCLIB_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    for attempt in range(retries):
        with _throttle_lock:
            elapsed = time.time() - _last_request
            if elapsed < 0.4:
                time.sleep(0.4 - elapsed)
            status, body = 0, b""
            try:
                req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    status, body = r.status, r.read()
            except urllib.error.HTTPError as e:
                status = e.code
            except Exception:
                status = 0
            _last_request = time.time()
        if status in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(2.0 * (attempt + 1))
            continue
        if status == 200:
            try:
                return json.loads(body.decode("utf-8", "replace"))
            except Exception:
                return None
        return None
    return None


def lrclib_fetch(artist, track, album=None, duration=None):
    """Best LRCLIB record for a track: exact /get lookup first, then a
    /search fallback preferring synced lyrics and the closest duration.
    Returns the record dict (syncedLyrics / plainLyrics) or None."""
    params = {"artist_name": artist, "track_name": track}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = int(round(duration))
    rec = _lrclib_get("get", params)
    if isinstance(rec, dict) and (rec.get("syncedLyrics") or rec.get("plainLyrics")):
        return rec
    # The exact lookup is strict — retry as a search, without the album
    # filter first (it can hurt matches).
    for album_filter in dict.fromkeys((None, album)):
        search = {"track_name": track, "artist_name": artist}
        if album_filter:
            search["album_name"] = album_filter
        hits = _lrclib_get("search", search)
        if isinstance(hits, list) and hits:
            synced = [h for h in hits if h.get("syncedLyrics")]
            pool = synced or hits
            if duration:
                pool = sorted(pool, key=lambda h: abs(int(h.get("duration") or 0) - int(duration)))
            return pool[0]
    return None


def run_fetch_lyrics(config):
    folder = config.get("music_folder") or ""
    stats = new_stats()

    print_header("Fetch Lyrics (LRCLIB)")
    fmt = str(config.get("lyrics_format") or "EMBEDDED").upper()
    write_embedded = fmt != "LRC"  # EMBEDDED or BOTH
    write_sidecar = fmt in ("LRC", "BOTH")
    force = bool(config.get("force_lyrics", False))
    log(f"write mode: {fmt}" + ("  (forced: re-fetch existing lyrics)" if force else ""))

    if config.get("targets") is not None:
        files = sorted(_collect_targets(config["targets"], AUDIO_EXTS))
    else:
        if not os.path.isdir(folder):
            log(c(f"ERROR: folder does not exist: {folder}", Color.RED))
            return stats
        files = []
        for album_dir in _find_albums(folder):
            files.extend(sorted(
                os.path.join(album_dir, f)
                for f in os.listdir(album_dir) if is_audio_file(f)
            ))

    if not files:
        log("No audio files found.")
        return stats

    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(total=len(files), desc="Fetch lyrics")
    try:
        for path in files:
            try:
                af = AudioFile(path)
                if af.audio is None:
                    raise RuntimeError(af.error or "unreadable")

                instrumental = str(af.get_tag("INSTRUMENTAL") or "").strip() == "1"
                existing = (af.get_lyrics() or "").strip()
                has_sidecar = os.path.isfile(_lrc_for(path))
                if not force and (instrumental or existing or has_sidecar):
                    stats["skipped_count"] += 1
                    _pbar_skip(pbar, counts)
                    continue

                artist = af.get_tag("ARTIST")
                title = af.get_tag("TITLE")
                if not artist or not title:
                    stats["skipped_count"] += 1
                    _pbar_skip(pbar, counts)
                    continue

                duration = None
                try:
                    duration = af.audio.info.length
                except Exception:
                    pass
                rec = lrclib_fetch(artist, title, af.get_tag("ALBUM"), duration)
                text = ((rec or {}).get("syncedLyrics")
                        or (rec or {}).get("plainLyrics") or "").strip()
                if not text:
                    stats["skipped_count"] += 1  # not on LRCLIB
                    counts["skip"] += 1
                    _pbar_skip(pbar, counts)
                    continue

                written = False
                if write_sidecar:
                    final = _format_for_storage(text, config, optimize=True, is_for_lrc=True)
                    _atomic_write_text(_lrc_for(path), final)
                    written = True
                if write_embedded:
                    final = _format_for_storage(text, config, optimize=True)
                    if not af.set_lyrics(final):
                        raise RuntimeError(af.error or "lyrics write failed")
                    written = True
                if written:
                    # Normalize with the exact script-1 code path so grading
                    # sees the canonical form (blank lines, zero stamps, …).
                    _process_lyrics_for_audio(path, config)
                    stats["modified_count"] += 1
                    _pbar_update(pbar, counts, "ok")
            except Exception as e:
                stats["error_count"] += 1
                if len(stats["errors"]) < 25:
                    stats["errors"].append(f"{os.path.basename(path)}: {e}")
                _pbar_update(pbar, counts, "fail")
    finally:
        try:
            pbar.close()
        except Exception:
            pass

    log(c(
        f"lyrics fetched: {counts['ok']} · skipped: {counts['skip']} · failed: {counts['fail']}",
        Color.GREEN if counts["fail"] == 0 else Color.YELLOW,
    ))
    return stats
