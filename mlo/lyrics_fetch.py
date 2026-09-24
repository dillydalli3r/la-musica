"""Script 13 — Fetch Lyrics.

Downloads missing lyrics for every track from the configured provider chain
(``lyrics_sources``, built-in order in ``lyrics_providers``) and writes
them per the global ``lyrics_format`` (EMBEDDED / LRC / BOTH), canonicalized
with the same formatting rules as script 1. Tracks tagged INSTRUMENTAL=1
and tracks that already carry lyrics (embedded or an .lrc sidecar) are
skipped unless the run is forced. Standard library only, so the runner
works in every install (no httpx dependency).

A search that comes back with nothing is not the end of the track here: the
app's own rule settles it (`server.instrumental.lyrics_absent`, spec R162) —
no lyrics from any configured provider, over a track no source calls vocal,
means the track is marked INSTRUMENTAL=1 with the note that says so. That is
what keeps a whole library's instrumentals out of the import's "needs you"
prompt without inventing a single tag: the alternative was a LYRICS grading
failure for every instrumental a source had no lyrics for, which is exactly
what an unattended import must not hand to a person.

"Nothing" means nothing, though: the chain's own two fallbacks run first, and
both are the runner's business because the summary reports them.

* UNTIMED lyrics are written when no source states timestamps — the owner's
  rule, and the reason a plain hit is `kind: "plain"` with status "ok" instead
  of the "no confident match" that used to end in INSTRUMENTAL=1 on a track a
  source had the words for.
* The ALIAS pass is the chain's second walk, under the names MusicBrainz
  states for the artist, the recording and the release-group
  (`lyrics_search_aliases`, on by default): the Japanese/Latin pair in both
  directions, and any other name a source knows the track by.
"""
import os

from .audio import AudioFile
from .lyrics import (
    _atomic_write_text, _format_for_storage, _lrc_for, has_lyrics_text,
    _process_lyrics_for_audio,
)
from .lyrics_providers import (  # noqa: F401  (lrclib_fetch is a re-export shim)
    SOURCE_LABELS, fetch_lyrics, lrclib_fetch, provider_order, youtube_id_from,
)
from .paths import AUDIO_EXTS
from .stats import (
    is_audio_file, new_stats, _collect_targets, _find_albums,
    _make_pbar, _pbar_skip, _pbar_update, worker_count,
)
from .ui import print_header, log, c, Color

# Confidence an AUTOMATIC write needs. _MIN_SCORE (0.6, the search floor) lets
# a same-title answer from a different artist through when that artist is
# unknown to the provider (the score falls back to the "artist unknown"
# weight, 0.65 + 0.14 of duration credit ≈ 0.79) — a fine candidate to SHOW
# a person, a bad lyric to write unattended. 0.85 keeps an unknown artist only
# when the title is an exact match and the duration agrees, and rejects it as
# soon as the duration is off or the title is fuzzy.
_AUTO_MIN_SCORE = 0.85

# The MusicBrainz identity a lyrics search can be re-asked in another name: the
# slot a lyrics query is built from, the MusicBrainz entity its aliases come
# from, and the tag this app's own import writes that entity's MBID into (see
# mlo.autotag). The track artist's id is preferred, the album artist's is the
# fallback — a compilation or a featured credit often has only the latter.
_ALIAS_ENTITIES = (
    ("artist", "artist",
     ("MUSICBRAINZ_ARTISTID", "MUSICBRAINZ_ALBUMARTISTID")),
    ("title", "recording", ("MUSICBRAINZ_TRACKID",)),
    ("album", "release-group", ("MUSICBRAINZ_RELEASEGROUPID",)),
)


def _search_aliases(get_tag, config, artist, title, album):
    """Alternative NAMES for this track's artist, title and album, or {}.

    The whole input of the chain's second pass (``fetch_lyrics``'s *aliases*):
    ``{"artist": [...], "title": [...], "album": [...]}``, each list as
    ``server.integrations.search_aliases`` orders it — the reader's own locale
    first, then a Latin reading, then whatever else MusicBrainz states, and
    never a `search hint` or the stored name itself. This is where the file's
    MusicBrainz tags are read: `宇多田ヒカル` is `Hikaru Utada` for a source that
    never saw the Japanese name.

    An MBID is required, one per entity, from the tags this app's own import
    wrote (`MUSICBRAINZ_ARTISTID` — or the album artist's — `..._TRACKID`,
    `..._RELEASEGROUPID`). It is the only thing that makes an alias THIS
    track's: MusicBrainz searched by name alone cannot tell one `光` from
    another, and an unattended run must not spend a name search per entity on
    every track it could not fill either. The manual search box, which is one
    track and one person, still asks by name (see `server.api_lyrics`).

    Switched off entirely by `lyrics_search_aliases` (on by default) before any
    lookup happens, and a lookup that cannot answer (no tag, a host that is
    busy) simply leaves that entity out — no alias is better than a guessed
    query. Never raises and never writes.
    """
    if not bool((config or {}).get("lyrics_search_aliases", True)):
        return {}
    try:
        from server.integrations import search_aliases
    except Exception:
        return {}
    names = {"artist": artist, "title": title, "album": album}
    out = {}
    for slot, entity, id_tags in _ALIAS_ENTITIES:
        name = str(names.get(slot) or "").strip()
        if not name:
            continue
        mbid = ""
        for tag in id_tags:
            try:
                mbid = str(get_tag(tag) or "").strip()
            except Exception:
                mbid = ""
            if mbid:
                break
        if not mbid:
            continue
        try:
            found = search_aliases(entity, mbid, config, name)
        except Exception:
            continue
        found = [str(n).strip() for n in (found or []) if str(n or "").strip()]
        if found:
            out[slot] = found
    return out



def _mark_lyrics_absent(path, config, result):
    """The app's own answer to a lyrics search that found nothing (R162).

    `server.instrumental.lyrics_absent` owns the rule and the write; this is
    just the per-track hand-off, so script 13 and the "auto-import lyrics"
    route settle a vocal-less track the same way. It never fails a fetch: the
    track's own result already says the search came back empty, and marking it
    is the extra fact that stops the album from being reported as missing
    lyrics.

    The result gains ``marked_instrumental`` and ``instrumental`` (the value
    and the source that stated it) when the tag was written, and
    ``instrumental_note`` with the app's sentence either way — a track left
    alone because a source states vocals says so rather than looking ignored.
    """
    from server import instrumental as inst

    try:
        marked = inst.lyrics_absent([path], config)
    except Exception:
        return
    key = os.path.normpath(str(path))
    note = str((marked.get("reason") or {}).get(key)
               or marked.get("skipped") or "")
    if note:
        result["instrumental_note"] = note
    if not (marked.get("values") or {}).get(key):
        return
    result["marked_instrumental"] = True
    result["instrumental"] = {
        "value": 1,
        "evidence": dict((marked.get("evidence") or {}).get(key) or {}),
    }
    result["reason"] = "no lyrics found — marked INSTRUMENTAL"


def fetch_one(path, config, force=False):
    """Fetch + write lyrics for ONE track; the shared core of script 13 and
    the API's "auto-import lyrics" button.

    Returns `{path, status: "ok"|"skipped"|"failed", provider,
    provider_label, kind: "synced"|"plain", synced, alias_pass, wrote:
    {embedded, lrc}, reason, error}`, plus `marked_instrumental` /
    `instrumental` / `instrumental_note` on a track whose search found nothing
    (`_mark_lyrics_absent`). *kind* is what was WRITTEN — a plain fallback is
    status "ok" with `kind: "plain"`, never "no lyrics found", because a source
    had the words and only the timestamps were missing. *alias_pass* is present
    (and names the entity and the name that found the track) when the chain's
    second pass is what answered. The skip
    rules (INSTRUMENTAL, existing embedded/sidecar lyrics unless *force*),
    the `lyrics_format` write mode and the canonicalization pass are the same
    ones the batch runner uses, so a lyrics run from the UI and a lyrics run
    from the Optimization page produce identical files.
    """
    result = {"path": path, "status": "skipped", "provider": None,
              "provider_label": None, "kind": None, "synced": False,
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
        # A sidecar only blocks the fetch when it really holds lyrics: a
        # 0-byte file or a metadata/timestamp-only stub counts as ABSENT, so
        # the fetch proceeds and overwrites it.
        try:
            with open(_lrc_for(path), "r", encoding="utf-8", errors="replace") as _f:
                has_sidecar = has_lyrics_text(_f.read())
        except OSError:
            has_sidecar = False
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
        # A YouTube id is the ONLY thing the captions provider can work with;
        # it never searches, so nothing is guessed from the tags (the file name
        # carries "[<id>]" — the template the video download itself writes).
        youtube_id = youtube_id_from(af.get_tag("YOUTUBEID"),
                                     af.get_tag("YOUTUBE_URL"),
                                     os.path.basename(path))
        # AUTOMATIC writes only take lyrics we are confident about. The search
        # floor (0.6) is deliberately loose — it is what a person browsing
        # candidates wants — but nothing is written here that a person would
        # have rejected: a hit must clear _AUTO_MIN_SCORE, which a same-title
        # different-artist answer cannot (that is the usual false positive:
        # the lyrics of a namesake cover). The manual search endpoints keep
        # the loose floor and never write on their own.
        # `aliases` is the chain's SECOND pass: the names MusicBrainz states
        # for this artist/recording/release-group, tried only when the stored
        # names found nothing (and only while lyrics_search_aliases is on). It
        # is passed as a CALLABLE so an answered track never pays for the
        # MusicBrainz lookups the names come from.
        album = af.get_tag("ALBUM")
        hit = fetch_lyrics(
            config, artist, title, album, duration, youtube_id=youtube_id,
            min_score=_AUTO_MIN_SCORE,
            aliases=lambda: _search_aliases(af.get_tag, config, artist, title,
                                            album))
        if hit is None:
            # Either no provider had it, or every answer was a weak match —
            # both mean "nothing safe to write", and both are retried by the
            # next run (nothing is written, so nothing is remembered as done).
            # What they also mean is that this track has no lyrics anywhere the
            # app can find them, which is the family's OWN automatic answer
            # (`_mark_lyrics_absent`): mark it instrumental instead of leaving
            # the album in the "needs you" queue for a person to answer.
            result["reason"] = "no confident match"
            _mark_lyrics_absent(path, config, result)
            return result
        # A synced provider hit keeps its timestamps; a plain one does not.
        text = ((hit or {}).get("synced") or (hit or {}).get("plain") or "").strip()
        if not text:
            result["reason"] = "no provider had lyrics"
            _mark_lyrics_absent(path, config, result)
            return result
        result["provider"] = hit["provider"]
        result["provider_label"] = hit.get("provider_label") or hit["provider"]
        # The KIND is what was written: timestamps, or the untimed fallback.
        result["synced"] = bool((hit.get("synced") or "").strip())
        result["kind"] = "synced" if result["synced"] else "plain"
        if hit.get("alias_pass"):
            # The second pass is what answered — say so, and name the name.
            result["alias_pass"] = dict(hit["alias_pass"])
        if not result["synced"]:
            # Honest, not apologetic: a source had the words, no source had
            # the timing. `lyrics_allow_plain` is the install's own switch over
            # STORING untimed lyrics (the grader and the import's settle pass
            # read it) — the chain does not invent timestamps either way.
            result["reason"] = "plain lyrics — no source had the timestamps"

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
    # What was actually WRITTEN: timestamps, or the untimed fallback. The owner
    # asked for the split to be visible — "2 fetched" hides that one of them
    # may have no timing at all, which is a weaker answer, not a failure.
    stats["by_kind"] = {"synced": 0, "plain": 0}
    stats["by_provider_kind"] = {}      # pid -> {"synced": n, "plain": n}
    # Tracks answered by the chain's SECOND pass (the alias names) — counted
    # apart, because "which name found it" is the whole point of that pass.
    stats["alias_count"] = 0
    # Tracks this run's empty searches settled as instrumental (R162) — booked
    # apart from the provider counts, since no provider answered for them.
    stats["instrumental_count"] = 0

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

    def _finish(path, res):
        """Book one track's result — the runner thread owns every counter, so
        the workers below never touch shared state."""
        if res.get("marked_instrumental"):
            # The family's own automatic answer (`server.instrumental
            # .lyrics_absent`), counted on its own: the track is booked as
            # skipped below — nothing was fetched — and "skipped: 12" must not
            # read as "twelve tracks nobody looked at" when the app wrote the
            # tag that settles them.
            stats["instrumental_count"] += 1
        if res["status"] == "ok":
            pid = res["provider"]
            kind = "synced" if res.get("synced") else "plain"
            stats["by_provider"][pid] = stats["by_provider"].get(pid, 0) + 1
            stats["by_kind"][kind] = stats["by_kind"].get(kind, 0) + 1
            row = stats["by_provider_kind"].setdefault(pid, {"synced": 0, "plain": 0})
            row[kind] += 1
            if res.get("alias_pass"):
                stats["alias_count"] += 1
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

    # Bounded parallelism: each track is its own provider search plus its own
    # tag/sidecar write, and the provider layer throttles request STARTS
    # globally while leaving the request itself outside the lock (see
    # mlo.lyrics_providers._request), so N lanes overlap the waiting with the
    # network instead of paying it once per track. The writes are per file, so
    # nothing is shared but the counters kept on this thread.
    workers = worker_count(config, default=4, maximum=8, items=len(files))
    try:
        if len(files) == 1 or workers == 1:
            for path in files:
                _finish(path, fetch_one(path, config, force=force))
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(fetch_one, p, config, force): p
                           for p in files}
                for fut in as_completed(futures):
                    path = futures[fut]
                    try:
                        res = fut.result()
                    except Exception as e:      # a worker must never kill the run
                        res = {"status": "failed", "error": str(e)}
                    _finish(path, res)
    finally:
        try:
            pbar.close()
        except Exception:
            pass

    # The kind split rides on the same line as the counters: "2 fetched" says
    # nothing about whether either of them has timestamps.
    kinds = stats["by_kind"]
    log(c(
        f"lyrics fetched: {counts['ok']} · skipped: {counts['skip']} · failed: {counts['fail']}"
        + (f" · synced: {kinds['synced']} · plain: {kinds['plain']}"
           if counts["ok"] else ""),
        Color.GREEN if counts["fail"] == 0 else Color.YELLOW,
    ))
    if stats["by_provider"]:
        def _kinds(pid):
            """How that provider's answers split — the kind is not implied."""
            row = stats["by_provider_kind"].get(pid) or {}
            parts = [f"{row[k]} {k}" for k in ("synced", "plain") if row.get(k)]
            if not parts:
                return ""
            if len(parts) == 1:
                return f" {parts[0].split(' ', 1)[1]}"
            return " (" + " · ".join(parts) + ")"

        summary = " · ".join(
            f"{SOURCE_LABELS.get(p, p)}: {n}{_kinds(p)}"
            for p, n in sorted(stats["by_provider"].items(), key=lambda kv: -kv[1])
        )
        log(f"sources: {summary}")
    if stats["alias_count"]:
        # The second pass answered: the stored names found nothing, the names
        # MusicBrainz states for them did.
        log(f"alias pass hit: {stats['alias_count']} track(s)")
    if stats["instrumental_count"]:
        # The decision, out loud: these tracks are why the album is not
        # reported as missing lyrics (see the module docstring).
        log(f"marked instrumental (no lyrics found by any provider): "
            f"{stats['instrumental_count']} track(s)")
    return stats
