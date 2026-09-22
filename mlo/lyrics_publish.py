"""Script 18 — Publish Lyrics (LRCLIB).

Gives back what this library has and the community database does not: for
every track that carries lyrics (embedded LYRICS tag or an .lrc sidecar) the
script asks LRCLIB whether it already knows that recording — artist, title,
album and duration, the same exact-then-search lookup the fetch chain uses —
and, when it does not, submits this library's own text (POST /api/publish).

Why it exists: LRCLIB is the app's first lyrics provider, and a library that
was tagged by hand (or from a provider LRCLIB does not have) is exactly the
material the database is missing. Publishing is outward-facing and public, so
it is gated by ``lrclib_auto_publish`` (default on, off means the script
skips) and by a per-track rule it can never override: a track LRCLIB already
answers for is never touched. ``force_publish`` re-submits anyway, for the
case where this library's text is the better one.

Skips, in order: unreadable file, INSTRUMENTAL=1, no lyric text, missing
ARTIST/TITLE, no duration (LRCLIB requires one), and "LRCLIB already has it"
(it answers 409 on a duplicate, which is reported as a skip too). Only
stdlib — the provider client in ``lyrics_providers`` is urllib-based.
"""
import os
import re

from .audio import AudioFile
from .lyrics import TIMESTAMP_RE, WORD_TS_RE, _lrc_for, has_lyrics_text
from .lyrics_providers import lrclib_fetch, lrclib_publish
from .paths import AUDIO_EXTS
from .stats import (
    is_audio_file, new_stats, _collect_targets, _find_albums,
    _make_pbar, _pbar_skip, _pbar_update, worker_count,
)
from .ui import print_header, log, c, Color

# LRC metadata headers ([ar:Artist], [offset:+500]) are not lyrics: they are
# dropped from the plain text, exactly like the formatter drops them.
_META_LINE_RE = re.compile(r"^\[[a-zA-Z]+:.*\]$")


def to_plain(text):
    """Timestamps stripped, one lyric line per line — LRCLIB wants the plain
    text beside a synced submission (a synced-only body is rejected by their
    validator)."""
    out = []
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        body = WORD_TS_RE.sub("", TIMESTAMP_RE.sub("", line)).strip()
        if body and not _META_LINE_RE.match(body):
            out.append(body)
    return "\n".join(out)


def local_lyrics(path, af=None):
    """The track's own lyrics text: embedded LYRICS first, then the .lrc
    sidecar — the same resolution order the player and grading use."""
    af = af or AudioFile(path)
    text = (af.get_lyrics() or "").strip()
    if has_lyrics_text(text):
        return text
    try:
        with open(_lrc_for(path), "r", encoding="utf-8", errors="replace") as fh:
            sidecar = fh.read().strip()
    except OSError:
        return ""
    return sidecar if has_lyrics_text(sidecar) else ""


def publish_one(path, config, force=False):
    """Publish ONE track's lyrics if LRCLIB does not have them yet.

    Returns ``{path, status: "ok"|"skipped"|"failed", reason, message,
    synced}`` — never raises, so one bad file cannot stop a library run."""
    result = {"path": path, "status": "skipped", "reason": "", "message": "",
              "synced": False}
    try:
        af = AudioFile(path)
        if af.audio is None:
            raise RuntimeError(af.error or "unreadable")
        if str(af.get_tag("INSTRUMENTAL") or "").strip() == "1":
            result["reason"] = "instrumental"
            return result

        text = local_lyrics(path, af)
        if not text:
            result["reason"] = "no lyrics stored"
            return result

        artist = str(af.get_tag("ARTIST") or af.get_tag("ALBUMARTIST") or "").strip()
        title = str(af.get_tag("TITLE") or "").strip()
        album = str(af.get_tag("ALBUM") or "").strip()
        if not artist or not title:
            result["reason"] = "missing ARTIST/TITLE tags"
            return result
        try:
            duration = int(round(float(af.audio.info.length)))
        except Exception:
            duration = 0
        if duration <= 0:
            result["reason"] = "no track duration"
            return result

        if not force and lrclib_fetch(artist, title, album or None, duration):
            # LRCLIB answers for this recording: publishing would either be
            # rejected as a duplicate or, worse, overwrite a better text.
            result["reason"] = "LRCLIB already has it"
            return result

        synced = bool(TIMESTAMP_RE.search(text))
        result["synced"] = synced
        ok, message = lrclib_publish(
            artist, title, album, duration,
            plain=to_plain(text) if synced else text,
            synced=text if synced else None)
        result["message"] = message
        if not ok:
            # A duplicate is the database's own answer, not a failure: the
            # text is there, which is all this script wanted.
            result["status"] = "skipped" if "already has this track" in message else "failed"
            result["reason"] = message
            return result
        result["status"] = "ok"
        return result
    except Exception as e:
        result["status"] = "failed"
        result["reason"] = str(e)
        return result


def run_publish_lyrics(config):
    """Script 18 entry point: publish this library's lyrics to LRCLIB."""
    folder = config.get("music_folder") or ""
    stats = new_stats()
    stats["published"] = 0
    stats["already_known"] = 0
    stats["no_lyrics"] = 0
    stats["rejected"] = 0

    print_header("Publish Lyrics (LRCLIB)")
    force = bool(config.get("force_publish", False))
    log("LRCLIB only receives tracks it does not already have"
        + ("  (forced: re-submit existing)" if force else ""))

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
    pbar = _make_pbar(total=len(files), desc="Publish lyrics")

    def _finish(path, got):
        """Book one track's result on the runner thread (workers share no
        state but the throttle inside the provider layer)."""
        status = got["status"]
        # Every examined track lands in scanned, published included: the ok
        # branch returned before this line, so a run that published 5 of 31
        # reported 26 scanned beside its "published 5" — numbers on one report
        # that could not both be about the same 31 tracks (README R10a).
        stats["total_scanned"] += 1
        if status == "ok":
            stats["published"] += 1
            stats["modified_count"] += 1
            _pbar_update(pbar, counts, "ok")
            return
        stats["unchanged_count"] += 1
        if status == "failed":
            stats["rejected"] += 1
            stats["error_count"] += 1
            if len(stats["errors"]) < 25:
                stats["errors"].append(
                    f"{os.path.basename(path)}: {got['reason']}")
            _pbar_update(pbar, counts, "fail")
            return
        if got["reason"] == "LRCLIB already has it":
            stats["already_known"] += 1
        elif got["reason"] == "no lyrics stored":
            stats["no_lyrics"] += 1
        _pbar_skip(pbar, counts)

    # Bounded parallelism: every track is its own existence check plus its own
    # submission, and the provider layer throttles request starts globally
    # while the request itself runs outside the lock (mlo.lyrics_providers.
    # _request), so lanes overlap the network wait instead of paying it once
    # per track.
    workers = worker_count(config, default=4, maximum=8, items=len(files))
    try:
        if len(files) == 1 or workers == 1:
            for path in files:
                _finish(path, publish_one(path, config, force=force))
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(publish_one, p, config, force): p
                           for p in files}
                for fut in as_completed(futures):
                    path = futures[fut]
                    try:
                        got = fut.result()
                    except Exception as e:      # a worker must never kill the run
                        got = {"status": "failed", "reason": str(e)}
                    _finish(path, got)
    finally:
        try:
            pbar.close()
        except Exception:
            pass

    log(c(
        f"published {stats['published']}"
        f" · already on LRCLIB {stats['already_known']}"
        f" · no lyrics {stats['no_lyrics']}"
        f" · refused {stats['rejected']}"
        f" · unchanged {stats['unchanged_count']}",
        Color.GREEN if stats["error_count"] == 0 else Color.YELLOW,
    ))
    return stats
