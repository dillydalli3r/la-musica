"""Script 13 — Fetch Lyrics.

Downloads missing lyrics for every track from the configured provider chain
(LRCLIB → NetEase → lyrics.ovh → Kugou; see ``lyrics_providers``) and writes
them per the global ``lyrics_format`` (EMBEDDED / LRC / BOTH), canonicalized
with the same formatting rules as script 1. Tracks tagged INSTRUMENTAL=1
and tracks that already carry lyrics (embedded or an .lrc sidecar) are
skipped unless the run is forced. Standard library only, so the runner
works in every install (no httpx dependency).
"""
import os

from .audio import AudioFile
from .lyrics import (
    _atomic_write_text, _format_for_storage, _lrc_for,
    _process_lyrics_for_audio,
)
from .lyrics_providers import (  # noqa: F401  (lrclib_fetch is a re-export shim)
    SOURCE_LABELS, fetch_lyrics, lrclib_fetch, provider_order,
)
from .paths import AUDIO_EXTS
from .stats import (
    is_audio_file, new_stats, _collect_targets, _find_albums,
    _make_pbar, _pbar_skip, _pbar_update,
)
from .ui import print_header, log, c, Color


def fetch_one(path, config, force=False):
    """Fetch + write lyrics for ONE track; the shared core of script 13 and
    the API's "auto-import lyrics" button.

    Returns `{path, status: "ok"|"skipped"|"failed", provider,
    provider_label, synced, wrote: {embedded, lrc}, reason, error}`. The skip
    rules (INSTRUMENTAL, existing embedded/sidecar lyrics unless *force*),
    the `lyrics_format` write mode and the canonicalization pass are the same
    ones the batch runner uses, so a lyrics run from the UI and a lyrics run
    from the Optimization page produce identical files.
    """
    result = {"path": path, "status": "skipped", "provider": None,
              "provider_label": None, "synced": False,
              "wrote": {"embedded": False, "lrc": None},
              "reason": "", "error": ""}
    fmt = str(config.get("lyrics_format") or "EMBEDDED").upper()
    write_embedded = fmt != "LRC"      # EMBEDDED or BOTH
    write_sidecar = fmt in ("LRC", "BOTH")
    try:
        af = AudioFile(path)
        if af.audio is None:
            raise RuntimeError(af.error or "unreadable")

        instrumental = str(af.get_tag("INSTRUMENTAL") or "").strip() == "1"
        existing = (af.get_lyrics() or "").strip()
        has_sidecar = os.path.isfile(_lrc_for(path))
        if not force and (instrumental or existing or has_sidecar):
            result["reason"] = "instrumental" if instrumental else "lyrics already present"
            return result

        artist = af.get_tag("ARTIST")
        title = af.get_tag("TITLE")
        if not artist or not title:
            result["reason"] = "missing ARTIST/TITLE tags"
            return result

        duration = None
        try:
            duration = af.audio.info.length
        except Exception:
            pass
        hit = fetch_lyrics(config, artist, title, af.get_tag("ALBUM"), duration)
        # A synced provider hit keeps its timestamps; a plain one does not.
        text = ((hit or {}).get("synced") or (hit or {}).get("plain") or "").strip()
        if not text:
            result["reason"] = "no provider had lyrics"
            return result
        result["provider"] = hit["provider"]
        result["provider_label"] = hit.get("provider_label") or hit["provider"]
        result["synced"] = bool((hit.get("synced") or "").strip())

        if write_sidecar:
            final = _format_for_storage(text, config, optimize=True, is_for_lrc=True)
            _atomic_write_text(_lrc_for(path), final)
            result["wrote"]["lrc"] = _lrc_for(path)
        if write_embedded:
            final = _format_for_storage(text, config, optimize=True)
            if not af.set_lyrics(final):
                raise RuntimeError(af.error or "lyrics write failed")
            result["wrote"]["embedded"] = True
        # Normalize with the exact script-1 code path so grading sees the
        # canonical form (blank lines, zero stamps, …).
        _process_lyrics_for_audio(path, config)
        result["status"] = "ok"
        return result
    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
        return result


def run_fetch_lyrics(config):
    folder = config.get("music_folder") or ""
    stats = new_stats()
    stats["by_provider"] = {}

    print_header("Fetch Lyrics")
    fmt = str(config.get("lyrics_format") or "EMBEDDED").upper()
    force = bool(config.get("force_lyrics", False))
    log("sources: " + " → ".join(
        SOURCE_LABELS[p] for p in provider_order(config)))
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
            res = fetch_one(path, config, force=force)
            if res["status"] == "ok":
                pid = res["provider"]
                stats["by_provider"][pid] = stats["by_provider"].get(pid, 0) + 1
                stats["modified_count"] += 1
                _pbar_update(pbar, counts, "ok")
            elif res["status"] == "failed":
                stats["error_count"] += 1
                if len(stats["errors"]) < 25:
                    stats["errors"].append(f"{os.path.basename(path)}: {res['error']}")
                _pbar_update(pbar, counts, "fail")
            else:
                stats["skipped_count"] += 1
                _pbar_skip(pbar, counts)
    finally:
        try:
            pbar.close()
        except Exception:
            pass

    log(c(
        f"lyrics fetched: {counts['ok']} · skipped: {counts['skip']} · failed: {counts['fail']}",
        Color.GREEN if counts["fail"] == 0 else Color.YELLOW,
    ))
    if stats["by_provider"]:
        summary = " · ".join(
            f"{SOURCE_LABELS.get(p, p)}: {n}"
            for p, n in sorted(stats["by_provider"].items(), key=lambda kv: -kv[1])
        )
        log(f"sources: {summary}")
    return stats
