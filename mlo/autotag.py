"""Automatic tagging: ALBUMITUNESADVISORY + INSTRUMENTAL + MOOD + GENRE.

Script 8 ("Auto Tagging") derives values that would otherwise have to be
filled in by hand:

1) ALBUMITUNESADVISORY from the per-track ITUNESADVISORY (set manually):
       0 = unrated / not explicit, 1 = explicit, 2 = edited / safe.
   Across ALL of the album's tracks (every disc in a multi-disc folder):
       any explicit track (1)      -> 1
       else any edited/safe track (2) -> 2
       else                        -> 0

2) INSTRUMENTAL from lyrics presence per track:
       has lyrics (embedded LYRICS or an .lrc sidecar) -> 0 (not instrumental)
       no lyrics -> LEFT UNTOUCHED. A track without downloaded lyric files
       may still be non-instrumental, so it is never auto-marked as
       instrumental.

3) MOOD from the track's own audio (``mlo.moods``: tempo, energy, brightness,
   dynamics → valence/arousal quadrant), refined by the track's GENRE in
   hybrid mode. Grading requires the tag, so every track gets one unless the
   file cannot be decoded or librosa is unavailable.

4) GENRE autofill when the tags carry none, through a caller-supplied
   provider hook (``set_genre_lookup``). The engine deliberately does not
   ship an HTTP client for this: the server and the import pipeline register
   their discovery/MusicBrainz chain, the CLI leaves it unset and step 4 is
   skipped.

Albums / tracks that already carry the correct values are skipped unless
the run is forced.
"""
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from .audio import AudioFile
from .config import should_write_audio_tag
from .paths import AUDIO_EXTS
from .stats import (
    new_stats, _make_pbar, _pbar_skip, _pbar_update, _collect_targets,
    _find_albums, is_audio_file, worker_count,
)
from .ui import print_header, log, c, Color


# ----------------------------------------------------------------------
# ALBUMITUNESADVISORY / INSTRUMENTAL (single-pass per album)
# ----------------------------------------------------------------------
def _album_files(album_dir):
    return sorted(
        os.path.join(album_dir, f)
        for f in os.listdir(album_dir) if is_audio_file(f))


def _derive_advisory(advisories):
    """1 if any explicit advisory, else 2 if any safe, else 0."""
    if any(v == "1" for v in advisories):
        return 1
    if any(v == "2" for v in advisories):
        return 2
    return 0


# Genre autofill provider, registered by whoever HAS a provider chain (the
# server's discovery layer, the import pipeline). The engine never imports the
# server, so an unset hook simply means "step 4 does not run".
_genre_lookup = None


def set_genre_lookup(fn):
    """Install `fn(artist, album, track_path) -> list[str]` for script 8.

    The callable must never raise; returning an empty list means "no genres
    found". Passing None removes the hook again (the CLI's default).
    """
    global _genre_lookup
    _genre_lookup = fn


# ----------------------------------------------------------------------
# Script 8 runner
# ----------------------------------------------------------------------
def run_auto_tagging(config):
    folder = config["music_folder"]
    stats = new_stats()

    print_header("Auto Tagging")
    log(f"music folder: {folder}")
    if config.get("auto_advisory", True):
        log("  ALBUMITUNESADVISORY: from per-track ITUNESADVISORY "
            "(any explicit -> 1, else any safe -> 2, else 0)")
    if config.get("auto_zero_advisory_for_instrumental", False):
        log("  ITUNESADVISORY: zeroed on instrumentals (auto_zero_advisory_for_instrumental)")
    if config.get("auto_instrumental", True):
        log("  INSTRUMENTAL: 0 when lyrics present (no-lyrics tracks left "
            "untouched)")
    if config.get("mood_enabled", True):
        log("  MOOD: from the track's audio" + (
            " (refined by GENRE)" if config.get("mood_source", "hybrid") == "hybrid"
            else f" (source: {config.get('mood_source', 'hybrid')})"))
    if config.get("genre_autofill", True):
        log("  GENRE: filled from the provider chain when missing"
            if _genre_lookup else
            "  GENRE: autofill skipped (no provider chain in this runner)")

    force = config.get("force_auto_tag", False)
    do_advisory = config.get("auto_advisory", True)
    do_instrumental = config.get("auto_instrumental", True)
    do_mood = config.get("mood_enabled", True)
    do_genre = config.get("genre_autofill", True) and _genre_lookup is not None
    if do_mood:
        from . import moods  # local: keeps librosa discovery out of import time
    # Advisory zero-fill is OFF by default: a missing ITUNESADVISORY means
    # "unrated" and stays missing — only an explicit setting turns the
    # instrumental zero-fill back on.
    do_zero_advisory_for_instrumental = config.get("auto_zero_advisory_for_instrumental", False)

    if config.get("targets") is not None:
        target_files = _collect_targets(config["targets"], AUDIO_EXTS)
        album_dirs = sorted({os.path.dirname(f) for f in target_files})
    else:
        if not os.path.isdir(folder):
            log(c(f"ERROR: folder does not exist: {folder}", Color.RED))
            return stats
        album_dirs = _find_albums(folder)

    if not album_dirs:
        log("No albums found.")
        return stats

    def process_album(album):
        files = _album_files(album)
        if not files:
            return album, 0, None, None, []

        # Single pass: load every file once and cache the values needed,
        # instead of re-parsing each file for advisory + instrumental.
        info = []
        for path in files:
            try:
                af = AudioFile(path)
                lyr = af.get_lyrics()
                info.append({
                    "af": af,
                    "advisory": str(af.get_tag("ITUNESADVISORY") or "").strip(),
                    "album_advisory": str(
                        af.get_tag("ALBUMITUNESADVISORY") or "").strip(),
                    "instrumental": str(af.get_tag("INSTRUMENTAL") or "").strip(),
                    "has_lyrics": bool(lyr and str(lyr).strip()) or
                        os.path.exists(os.path.splitext(path)[0] + ".lrc"),
                })
            except Exception:
                continue
        if not info:
            return album, 0, None, None, []

        modified = 0
        notes = []
        advisory_value = None

        # Formatting: ensure GENRE has no leading/trailing spaces and
        # ITUNESADVISORY is exactly 0/1/2 without spaces. This is the
        # optimization step for those tags.
        for d in info:
            # GENRE (standard tag, not gated by per-type ADVISORY — but still trim)
            try:
                raw_genre = d["af"].get_tag("GENRE")
                if raw_genre is not None:
                    stripped = str(raw_genre).strip()
                    if str(raw_genre) != stripped:
                        if d["af"].set_tag("GENRE", stripped):
                            modified += 1
                            d["af"] = AudioFile(d["af"].path)  # refresh
            except Exception:
                pass
            # ITUNESADVISORY: trim spaces; keep 0/1/2 only (grading will flag others)
            # Gated by per-filetype ADVISORY
            try:
                raw_adv = d["af"].get_tag("ITUNESADVISORY")
                if raw_adv is not None:
                    stripped = str(raw_adv).strip()
                    if str(raw_adv) != stripped:
                        if not should_write_audio_tag(config, "ITUNESADVISORY", filepath=d["af"].path):
                            continue
                        # Only write trimmed if the trimmed value is valid 0/1/2 or empty
                        # If it's invalid like " 3 ", we still trim to "3" so grading can flag the value, not the spaces
                        if d["af"].set_tag("ITUNESADVISORY", stripped):
                            modified += 1
                            d["advisory"] = stripped
            except Exception:
                pass

        # 1) Fix INSTRUMENTAL from lyrics presence first (correct order)
        instrumental_modified = 0
        if do_instrumental:
            for d in info:
                if d["has_lyrics"] and d["instrumental"] != "0":
                    if not should_write_audio_tag(config, "INSTRUMENTAL", filepath=d["af"].path):
                        continue
                    if d["af"].set_tag("INSTRUMENTAL", "0"):
                        modified += 1
                        instrumental_modified += 1
                        d["instrumental"] = "0"
                        d["af"] = AudioFile(d["af"].path)  # refresh
            if instrumental_modified:
                notes.append("instrumental")

        # 2) Derive ALBUMITUNESADVISORY from current per-track advisories (respect per-type gate)
        advisory_modified = 0
        if do_advisory:
            # Only include advisories for tracks where we can write album advisory, or where advisory is enabled
            advisories_for_derive = [d["advisory"] for d in info]
            advisory_value = _derive_advisory(advisories_for_derive)
            # Check if write needed (filter to writable files)
            need_write = []
            for d in info:
                if d["album_advisory"] != str(advisory_value) and should_write_audio_tag(config, "ALBUMITUNESADVISORY", filepath=d["af"].path):
                    need_write.append(d)
            # When force is True, rewrite even if already correct (count as modified for stats)
            write_list = need_write if not force else [d for d in info if should_write_audio_tag(config, "ALBUMITUNESADVISORY", filepath=d["af"].path)]
            if write_list:
                for d in write_list:
                    if d["af"].set_tag("ALBUMITUNESADVISORY", str(advisory_value)):
                        modified += 1
                        advisory_modified += 1
                        d["album_advisory"] = str(advisory_value)
                if advisory_modified:
                    notes.append(f"advisory={advisory_value}")

        # 3) Auto-zero ITUNESADVISORY for instrumental tracks (must run AFTER instrumental fix + advisory derive)
        zero_modified = 0
        if do_zero_advisory_for_instrumental:
            for d in info:
                is_instrumental = (d["instrumental"] == "1")
                # Only zero if instrumental and advisory is explicit/safe or empty; leave invalid "3" untouched
                if is_instrumental and d["advisory"] in ("1", "2", ""):
                    if not should_write_audio_tag(config, "ITUNESADVISORY", filepath=d["af"].path):
                        continue
                    if d["af"].set_tag("ITUNESADVISORY", "0"):
                        modified += 1
                        zero_modified += 1
                        d["advisory"] = "0"
            if zero_modified:
                notes.append("zero advisory for instrumental")
                # Re-derive album advisory if we zeroed any track (album may need to go from 1/2 -> 0)
                if do_advisory:
                    new_val = _derive_advisory(d["advisory"] for d in info)
                    if new_val != advisory_value:
                        for d in info:
                            if d["album_advisory"] != str(new_val) and should_write_audio_tag(config, "ALBUMITUNESADVISORY", filepath=d["af"].path):
                                if d["af"].set_tag("ALBUMITUNESADVISORY", str(new_val)):
                                    modified += 1
                        advisory_value = new_val
                        # Update note if advisory already appended
                        # Replace last advisory note if present
                        for i, n in enumerate(notes):
                            if n.startswith("advisory="):
                                notes[i] = f"advisory={new_val}"
                                break

        return album, modified, notes, advisory_value, info

    def process_album_full(album):
        """`process_album` plus the mood/genre stages.

        The mood classifier and the genre hook both need the tags the first
        pass already read, so they reuse its parsed handles — a track is
        never opened twice (the librosa decode is the expensive part).
        """
        _, modified, notes, advisory_value, info = process_album(album)
        notes = list(notes or [])

        mood_modified = 0
        genre_modified = 0
        for d in info:
            af = d["af"]
            path = af.path
            if do_genre and not str(af.get_tag("GENRE") or "").strip():
                artist = af.get_tag("ALBUMARTIST") or af.get_tag("ARTIST") or ""
                album_tag = af.get_tag("ALBUM") or ""
                try:
                    names = _genre_lookup(artist, album_tag, path) or []
                except Exception:
                    names = []
                if names and should_write_audio_tag(config, "GENRE", filepath=path):
                    if af.set_tag("GENRE", "; ".join(names)):
                        genre_modified += 1
                        af = d["af"] = AudioFile(path)  # refresh for the mood prior
            if do_mood:
                try:
                    # The decode is the expensive part (librosa, seconds per
                    # track), so an already-correct tag short-circuits exactly
                    # like the GENRE branch above: re-running Auto tagging, or
                    # importing the same album again, must not re-analyse the
                    # library for nothing.
                    if not force and str(af.get_tag("MOOD") or "").strip():
                        continue
                    genre = af.get_tag("GENRE") or ""
                    if moods.apply_mood_tags(af, path, config, genre=genre):
                        mood_modified += 1
                except Exception:
                    continue

        modified = (modified or 0) + mood_modified + genre_modified
        if mood_modified:
            notes.append("mood")
        if genre_modified:
            notes.append("genre")
        return album, modified, notes, advisory_value, info

    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(len(album_dirs), "AutoTag", unit="album")
    workers = worker_count(config, default=8, maximum=8, items=len(album_dirs))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(process_album_full, a): a for a in sorted(album_dirs)}
        for fut in as_completed(futures):
            album = futures[fut]
            try:
                _album, modified, notes, advisory_value, _info = fut.result()
            except Exception as e:
                stats["total_scanned"] += 1
                stats["error_count"] += 1
                stats["errors"].append((os.path.basename(album), str(e)))
                _pbar_update(pbar, counts, kind="fail")
                continue
            if modified:
                stats["total_scanned"] += 1
                stats["modified_count"] += 1
                log(f"  {os.path.basename(_album)} ({', '.join(notes)})")
                _pbar_update(pbar, counts, kind="ok")
            else:
                stats["skipped_count"] += 1
                _pbar_skip(pbar, counts)

    if pbar:
        pbar.close()
    stats["is_grader"] = False
    return stats
