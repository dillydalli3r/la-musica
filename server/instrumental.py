"""Instrumental detection (INSTRUMENTAL): every source asked, then merged.

`detect_instrumental(paths, cfg)` answers per track, and the writer
(`server.imports.fetch_instrumentals`, script 8 in `mlo.autotag`) uses it:

  lrclib      LRCLIB's own per-track `instrumental` flag, by artist + title +
              album + duration (/api/get, falling back to /api/search) — the
              hit must be the track, so `integrations.title_matches` guards
              the name and the duration must be within `_DURATION_TOLERANCE`
              seconds when both are known      (verified live: Rush "YYZ"
              true, System Of A Down "Boom!" false)
  spotify     Spotify audio-features `instrumentalness`, reached by ISRC →
              search → track id → /v1/audio-features/{id} (needs credentials;
              >= _INSTRUMENTAL_MIN is instrumental, <= _INSTRUMENTAL_MAX is
              not, in between is no answer). EVERY ISRC the file states is
              asked, in the tag's own order — `integrations._isrc_codes` is
              the advisory ladder's own reader, so both paths ask the same
              codes, and a source asked more than once keeps its STRONGEST
              answer. Spotify has deprecated this endpoint for newer apps, so
              a 403/404 is NO ANSWER plus one log line — never a crash, never
              a guess
  title       the track's own name: "... (Instrumental)", "Instrumental
              Version", a leading "Instrumental -" — an instrumental file is
              instrumental whatever any API says (and only the *instrumental*
              marker answers: a karaoke/karaoke-style name states nothing)
  lyrics      embedded LYRICS or an .lrc sidecar → NOT instrumental (the same
              evidence mlo/autotag.py already reads)

MusicBrainz is deliberately NOT a source here. Probed live on 2026-09-17:
a recording lookup carries no instrumental field at all (only id / title /
length / video / releases), and release groups have no `Instrumental`
secondary type — `secondarytype:instrumental` matches 0 release groups across
the whole database while `secondarytype:soundtrack` matches 95k. There is no
per-recording evidence to keep, so the probe was dropped rather than shipped.

Merge rule (`merge_instrumental`): any source says instrumental → 1; else any
source says not instrumental → 0; else None — and the caller leaves the file
untouched. Nothing is ever invented from missing data.

`lyrics_absent(paths, cfg)` is the ONE exception, and it is the app's own rule
rather than a source's answer: a track whose lyrics chain found nothing and
which no source calls vocal is marked INSTRUMENTAL=1 (source `LYRICS_ABSENT`),
because the alternative is a LYRICS grading failure the import parks for a
person to answer for every instrumental track in the library.
"""
import os

from server import integrations as intg

# Every key `answers` may carry.
INSTRUMENTAL_SOURCES = frozenset({"lrclib", "spotify", "title", "lyrics"})

# The source key `lyrics_absent` states its own answer under. NOT one of the
# cross-referenced sources above: none of them said this — the statement is the
# app's (a lyrics search that found nothing, over a track nothing calls vocal),
# and the evidence panel, the import log and any later reader must be able to
# tell it apart from a provider's own answer for exactly that reason.
LYRICS_ABSENT = "lyrics-none"

# A candidate hit is this track only when the duration agrees this closely:
# LRCLIB's own entries drift a few seconds between masters.
_DURATION_TOLERANCE = 5.0
# Spotify's audio-features `instrumentalness` banding: at/above this it is
# instrumental, at/below the other it is not, and in between Spotify is not
# saying either way.
_INSTRUMENTAL_MIN = 0.5
_INSTRUMENTAL_MAX = 0.2

_SPOTIFY_AF = "https://api.spotify.com/v1/audio-features/"
# One line per process when Spotify refuses audio-features (403/404 on apps
# the endpoint has been removed for), never one per track.
_spotify_warned = False


def merge_instrumental(answers):
    """The ONE merge rule for INSTRUMENTAL — `{source: 0|1}` → 0|1|None.

    Any source saying instrumental wins (a vocal-free statement is the one
    that cannot be faked by a missing flag), otherwise any source saying not
    instrumental, otherwise None: no answer, nothing written.
    """
    values = [int(v) for v in (answers or {}).values() if v in (0, 1)]
    if 1 in values:
        return 1
    if 0 in values:
        return 0
    return None


def _duration_seconds(af):
    """Track length in seconds, or None (mutagen info, ffprobe tech)."""
    info = getattr(af.audio, "info", None)
    for candidate in (getattr(info, "length", None), (af.tech or {}).get("length")):
        try:
            seconds = float(candidate)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return seconds
    return None


def _same_track(hit, title, duration):
    """True when an LRCLIB hit is the track we asked about."""
    if not isinstance(hit, dict) or not intg.title_matches(
            title, hit.get("trackName")):
        return False
    if not duration:
        return True
    try:
        got = float(hit.get("duration") or 0)
    except (TypeError, ValueError):
        return True
    return not got or abs(got - duration) <= _DURATION_TOLERANCE


def _lrclib_hit(artist, title, album, duration):
    """The LRCLIB entry for this track: /get, then /search. Else None."""
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = int(duration)
    try:
        r = intg._lrclib_get("get", params, retries=2)
        if r.status_code == 200:
            hit = r.json()
            if _same_track(hit, title, duration):
                return hit
        elif r.status_code not in (400, 404):
            return None
    except Exception:
        return None
    try:
        # /api/search is the documented fallback (the album can hurt the
        # exact match); the same title + duration guard applies to its hits.
        r = intg._lrclib_get("search",
                             {"track_name": title, "artist_name": artist},
                             retries=2)
        hits = r.json() if r.status_code == 200 else []
    except Exception:
        return None
    for hit in hits if isinstance(hits, list) else []:
        if _same_track(hit, title, duration):
            return hit
    return None


def _lrclib_answer(artist, title, album, duration):
    """(0|1|None, note) from LRCLIB's `instrumental` flag."""
    if not artist or not title:
        return None, ""
    hit = _lrclib_hit(artist, title, album, duration)
    if not hit:
        return None, ""
    flag = hit.get("instrumental")
    if not isinstance(flag, bool):
        return None, ""
    return (1 if flag else 0,
            f"lrclib says instrumental={flag} for {hit.get('trackName')!r}")


def _spotify_answer(isrc, cfg):
    """(0|1|None, note) from Spotify's audio-features `instrumentalness`."""
    global _spotify_warned
    code = str(isrc or "").strip()
    token = intg._spotify_token(cfg) if code else None
    if not token:
        return None, ""
    search = intg._advisory_json(
        intg._SPOTIFY_SEARCH,
        {"q": f"isrc:{code}", "type": "track", "limit": 1},
        headers={"Authorization": f"Bearer {token}"}, host="api.spotify.com")
    tid = ""
    for item in (((search or {}).get("tracks") or {}).get("items") or []):
        if str((item.get("external_ids") or {}).get("isrc") or "").upper() \
                == code.upper():
            tid = str(item.get("id") or "")
            break
    if not tid:
        return None, ""
    data = intg._advisory_json(
        _SPOTIFY_AF + tid, headers={"Authorization": f"Bearer {token}"},
        host="api.spotify.com")
    if not isinstance(data, dict) or "instrumentalness" not in data:
        if not _spotify_warned:
            _spotify_warned = True
            print("[mlo] spotify: audio-features unavailable for this app "
                  "(403/404 is expected — the endpoint is deprecated for new "
                  "apps); instrumental detection uses the other sources")
        return None, ""
    try:
        score = float(data.get("instrumentalness"))
    except (TypeError, ValueError):
        return None, ""
    if score >= _INSTRUMENTAL_MIN:
        return 1, f"spotify instrumentalness {score:.2f}"
    if score <= _INSTRUMENTAL_MAX:
        return 0, f"spotify instrumentalness {score:.2f}"
    return None, ""


def _title_answer(title, path):
    """(0|1|None, note) from the track's own name — instrumentals only."""
    stem = os.path.splitext(os.path.basename(str(path or "")))[0]
    for text in (title, stem):
        if intg.title_variant_kind(text) == "instrumental":
            return 1, f"title says instrumental ({text!r})"
    return None, ""


def _lyrics_answer(af, path):
    """(0|1|None, note) from lyrics evidence — lyrics mean vocals."""
    try:
        lyrics = af.get_lyrics()
    except Exception:
        lyrics = None
    if lyrics and str(lyrics).strip():
        return 0, "embedded lyrics"
    if os.path.exists(os.path.splitext(str(path))[0] + ".lrc"):
        return 0, "lyrics sidecar (.lrc)"
    return None, ""


def detect_instrumental(paths, cfg=None):
    """{path: {"value": 0|1|None, "answers": {source: 0|1}, "evidence": str}}.

    Every source is asked for every track (LRCLIB, Spotify when configured,
    the track's own title, lyrics evidence) and the answers are merged by
    `merge_instrumental`; `evidence` is the one-line reason each answering
    source gave. A track nobody can state anything about comes back with
    `value` None — the caller must leave the tag alone rather than invent a 0.

    Files that are unreadable, or a path that is not a track, are absent from
    the result (never an exception). The per-source answers are cached for the
    process, so a library pass asks each track once.
    """
    from mlo.audio import AudioFile

    cfg = cfg or {}
    out = {}
    for p in paths or []:
        path = os.path.normpath(str(p))
        try:
            af = AudioFile(path)
            if af.audio is None:
                continue
            tag = str(af.get_tag("TITLE") or "")
            title = tag.strip() or os.path.splitext(os.path.basename(path))[0]
            artist = str(af.get_tag("ARTIST")
                         or af.get_tag("ALBUMARTIST") or "").strip()
            album = str(af.get_tag("ALBUM") or "").strip()
            # EVERY ISRC the file states, in the tag's own order — not just
            # the first: one recording is published in several territories
            # under several codes, and `split(";")[0]` let a clean pressing's
            # code hide the instrumental one. server.integrations._isrc_codes
            # is the advisory ladder's own reader, so both paths ask the same
            # codes the same way.
            codes = intg._isrc_codes(af.get_tag("ISRC"))
            duration = _duration_seconds(af)
        except Exception:
            continue

        answers = {}
        notes = []

        def _ask(source, producer, key):
            answer = intg._advisory_cached((source, key), producer)
            if not answer:
                return
            value, note = answer
            if value not in (0, 1):
                return
            if answers.get(source) == 1:
                # A source asked more than once — one ISRC per pressing is
                # the normal case — contributes its STRONGEST answer: 1 is
                # the statement no missing flag can imitate
                # (merge_instrumental), so a later clean code never clears a
                # stated instrumental.
                return
            answers[source] = value
            if note:
                notes.append(note)

        # The track's own name first: a file that says "Instrumental" needs no
        # network, and its answer is part of the cross-reference either way.
        _ask("title", lambda: _title_answer(title, path), path.lower())
        _ask("lyrics", lambda: _lyrics_answer(af, path), path.lower())
        _ask("lrclib", lambda: _lrclib_answer(artist, title, album, duration),
             f"{artist}|{title}|{album}|{duration}".lower())
        for code in codes:
            _ask("spotify", lambda c=code: _spotify_answer(c, cfg), code.upper())

        out[path] = {
            "value": merge_instrumental(answers),
            "answers": answers,
            "evidence": "; ".join(notes) or "no source stated anything",
        }
    return out


def _lyrics_present(af, path):
    """Whether this file carries lyrics — the grader's own test (spec R45).

    Embedded text that survives stripping, or a `.lrc` sidecar holding real
    text: a 0-byte file or a timestamps-only stub is ABSENT, which is the same
    line `mlo.lyrics_fetch` draws before it fetches and `mlo.grader` before it
    grades.
    """
    try:
        if str(af.get_lyrics() or "").strip():
            return True
    except Exception:
        pass
    try:
        from mlo.lyrics import _lrc_for, has_lyrics_text

        with open(_lrc_for(path), "r", encoding="utf-8", errors="replace") as fh:
            return has_lyrics_text(fh.read())
    except Exception:
        return False


def lyrics_absent(paths, cfg=None):
    """INSTRUMENTAL=1 for the tracks the configured lyrics chain could not fill.

    The app's OWN rule, and the one an unattended import needs: after a search
    that found nothing the track is a LYRICS grading failure (`mlo.grader`: a
    track with no INSTRUMENTAL and no lyrics fails the check), so every
    instrumental track in a fresh library raised a prompt for a person to
    answer. What the app has after that search is evidence in its own right —
    it asked every provider in `lyrics_providers`' chain and none had the
    track — and the family's automatic answer is the one it already has: mark
    it instrumental, under its own source (`LYRICS_ABSENT`), and record why.

    Three statements outrank the absence of lyrics, and each leaves the file
    untouched:

    * an INSTRUMENTAL tag already on the file — a provider's answer, the
      pipeline's own `fetch_instrumentals` step, or the user's own edit;
    * lyrics on the file (embedded or a real sidecar): lyrics ARE vocals, so a
      track whose words the chain merely did not re-fetch is not instrumental;
    * a cross-referenced source saying not-instrumental for this track
      (`detect_instrumental`, asked once, on its cached answers): LRCLIB's
      `instrumental: false` and Spotify's `instrumentalness` know better than a
      search that came back empty.

    Returns ``{"updated", "values", "evidence", "reason"}`` — the same shape
    `server.imports.fetch_instrumentals` returns, `evidence` keyed per track by
    the source that stated the value, and `reason` the one-line why per track
    (the honest note the import log prints). Nothing is ever raised: a track
    that cannot be read or written is simply not in the result.
    """
    from mlo.audio import AudioFile
    from mlo.config import should_write_audio_tag

    cfg = cfg or {}
    out = {"updated": 0, "values": {}, "evidence": {}, "reason": {}}
    if not cfg.get("instrumental_auto_fetch", True):
        # The user's switch over "does the app decide this on its own" is not
        # overruled to close a gap — the same rule `fetch_instrumentals` states.
        out["skipped"] = "instrumental_auto_fetch is off"
        return out

    written = []
    for p in paths or []:
        path = os.path.normpath(str(p))
        try:
            af = AudioFile(path)
            if af.audio is None:
                continue
            if str(af.get_tag("INSTRUMENTAL") or "").strip() in ("0", "1"):
                continue
            if _lyrics_present(af, path):
                out["reason"][path] = "the file carries lyrics — vocals"
                continue
            got = detect_instrumental([path], cfg).get(path) or {}
            value = got.get("value")
            if value == 0:
                out["reason"][path] = (
                    "a source states vocals — left alone ("
                    + str(got.get("evidence") or "") + ")")
                continue
            if value == 1:
                # A source stated it: the tag the pipeline's own step would have
                # written, with that source's own evidence.
                evidence = dict(got.get("answers") or {})
                note = str(got.get("evidence") or "")
                why = "stated by " + ", ".join(sorted(evidence))
            else:
                evidence = {LYRICS_ABSENT: 1}
                note = ("no provider in the configured lyrics chain had this "
                        "track and no source states vocals")
                why = LYRICS_ABSENT
            if not should_write_audio_tag(cfg, "INSTRUMENTAL", filepath=path):
                out["reason"][path] = ("this file type does not take the "
                                       "INSTRUMENTAL tag (write gate)")
                continue
            if af.set_tag("INSTRUMENTAL", "1"):
                out["updated"] += 1
                written.append(path)
            out["values"][path] = 1
            out["evidence"][path] = evidence
            out["reason"][path] = f"{note} ({why})"
        except Exception:
            continue
    if written:
        # The tags these files now carry are the ones the library, the grade and
        # the wizard read back; a stale cache here would show the old value
        # until something else happened to drop it.
        try:
            from server import tagcache

            for path in written:
                tagcache.invalidate_path(path)
        except Exception:
            pass
    return out
