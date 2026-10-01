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
(it answers 409 on a duplicate, which is reported as a skip too). A record
LRCLIB holds WITHOUT timings is not a skip when this track carries the synced
text: that submission is an upgrade, kept beside the old revision
(``lrclib_would_add``), so the community's plain-only entries get their timed
copy from this library.

A track whose MusicBrainz ids give it another name (`宇多田ヒカル` / `Hikaru
Utada`) is published under its own name pair AND each alias pair (see
``lyrics_search_aliases``, on by default). Each name is its own record with its
own answer, so a duplicate under one name still leaves the others free to
publish; with no MBIDs there is exactly one pair. Only stdlib — the provider
client in ``lyrics_providers`` is urllib-based.
"""
import os
import re

from .audio import AudioFile
from .lyrics import TIMESTAMP_RE, WORD_TS_RE, _lrc_for, has_lyrics_text
from .lyrics_fetch import _search_aliases
from .lyrics_providers import (
    PublishTokenPool, _alias_queries, lrclib_fetch, lrclib_publish,
    lrclib_would_add,
)
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


def publish_one(path, config, force=False, solver=None):
    """Publish ONE track's lyrics if LRCLIB does not have them yet.

    Returns ``{path, status: "ok"|"skipped"|"failed", reason, message,
    synced, names}`` — never raises, so one bad file cannot stop a library
    run. *names* lists every name pair tried, each with its own status: the
    file's own ``(artist, title, album)`` first, then each alias pair the
    track's MusicBrainz ids provide. The status is ``ok`` when ANY pair
    published, ``skipped`` when every pair was already there, else ``failed``.

    *solver* is the run's shared `PublishTokenPool`: LRCLIB's challenge per
    submission is ~15 s of sha256, and the run's lanes are threads, so the
    solve has to leave this process to have any width at all. A one-off
    caller leaves it None and solves in its own thread.
    """
    result = {"path": path, "status": "skipped", "reason": "", "message": "",
              "synced": False, "names": []}
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

        # The alias names are a SECOND set of submissions, not a retry: the CJK
        # title LRCLIB lacks may sit under its Latin reading, and each name is
        # its own record with its own answer. No MBIDs (or the switch off) makes
        # this {} — one pair, exactly as before, with no extra request.
        try:
            aliases = _search_aliases(af.get_tag, config, artist, title, album)
        except Exception:
            aliases = {}

        pairs, seen = [(artist, title, album)], {(artist, title, album)}
        for a, t, al, _entity, _query in _alias_queries(
                artist, title, album, aliases):
            if (a, t, al) not in seen:
                seen.add((a, t, al))
                pairs.append((a, t, al))

        synced = bool(TIMESTAMP_RE.search(text))
        result["synced"] = synced
        plain = to_plain(text) if synced else text
        synced_body = text if synced else None

        names = []
        for a, t, al in pairs:
            entry = {"artist": a, "title": t, "album": al, "status": "skipped"}
            # Independent per name: a duplicate or a refusal under one name
            # never stops the others from publishing.
            known = None if force else lrclib_fetch(a, t, al or None, duration)
            if known is not None and not lrclib_would_add(known, synced_body):
                entry["reason"] = "LRCLIB already has it"
                names.append(entry)
                continue
            if known is not None:
                # The name is there as a plain-only record and this track
                # holds the synced text: the submission is the UPGRADE
                # (`lrclib_would_add`), and the report says so.
                entry["upgrade"] = "synced over plain"
            ok, message = lrclib_publish(a, t, al, duration,
                                         plain=plain, synced=synced_body,
                                         solver=solver)
            if ok:
                entry["status"] = "ok"
            elif "already has this track" in message:
                # The database's own words, not a paraphrase: the reply that
                # reaches the user is the sentence LRCLIB wrote (`server.
                # api_lyrics` reads this same phrase to call the row a skip).
                entry["reason"] = message
            else:
                entry["status"] = "failed"
                entry["reason"] = message
            names.append(entry)

        result["names"] = names
        published = [n for n in names if n["status"] == "ok"]
        if published:
            result["status"] = "ok"
            result["message"] = "published to LRCLIB as " + "; ".join(
                f"{n['artist']} — {n['title']}" for n in published)
            return result
        refused = [n for n in names if n["status"] == "failed"]
        if refused:
            result["status"] = "failed"
            result["reason"] = refused[0]["reason"]
            return result
        # Every name pair was already there. The reason is the answer the name
        # actually got — LRCLIB's own sentence when it refused the duplicate,
        # this app's phrase when its own existence check found it first — so a
        # single-name track reports exactly what the old code did.
        result["reason"] = next(
            (n.get("reason") for n in names if n.get("reason")),
            "LRCLIB already has it")
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
    stats["published_names"] = 0
    stats["already_known"] = 0
    stats["no_lyrics"] = 0
    stats["rejected"] = 0

    print_header("Publish Lyrics (LRCLIB)")
    force = bool(config.get("force_publish", False))
    log("LRCLIB only receives what it does not already have — a plain-only "
        "entry still gets this library's synced text"
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
            # Every name pair that actually went up, so a localised submission
            # beside the original shows in the report (>= published).
            stats["published_names"] += sum(
                1 for n in got.get("names") or () if n.get("status") == "ok")
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
    workers = worker_count(config, maximum=8, items=len(files))
    # The proof-of-work, on cores of its own: every submission's token is ~15 s
    # of sha256 (see `PublishTokenPool`), the lanes below are threads, and
    # threads do not scale a GIL-held hash loop — so an album paid that 15 s per
    # track, one after another, which is what "stuck on Publish Lyrics for five
    # minutes" measured. The pool starts on the FIRST token a run needs (a run
    # that publishes nothing pays nothing) and is closed in the `finally`.
    solver = PublishTokenPool(workers, announce=log)
    try:
        if len(files) == 1 or workers == 1:
            for path in files:
                _finish(path, publish_one(path, config, force=force, solver=solver))
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(publish_one, p, config, force, solver): p
                           for p in files}
                for fut in as_completed(futures):
                    path = futures[fut]
                    try:
                        got = fut.result()
                    except Exception as e:      # a worker must never kill the run
                        got = {"status": "failed", "reason": str(e)}
                    _finish(path, got)
    finally:
        solver.close()
        try:
            pbar.close()
        except Exception:
            pass

    names_note = (f" · {stats['published_names']} name pairs"
                  if stats["published_names"] > 0 else "")
    log(c(
        f"published {stats['published']}"
        f" · already on LRCLIB {stats['already_known']}"
        f" · no lyrics {stats['no_lyrics']}"
        f" · refused {stats['rejected']}"
        f" · unchanged {stats['unchanged_count']}"
        + names_note,
        Color.GREEN if stats["error_count"] == 0 else Color.YELLOW,
    ))
    return stats
