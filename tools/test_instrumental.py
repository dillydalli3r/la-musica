#!/usr/bin/env python3
"""Instrumental detection (server.instrumental) — offline contract.

What this pins, with LRCLIB and Spotify stubbed (no network at all):

  * LRCLIB's own `instrumental` flag is the primary source, asked by artist +
    title + album + duration — a hit must be THIS track (`title_matches`) and
    the duration must agree within ~5 s when both are known;
  * the /api/search fallback is used when /api/get answers 400/404, and its
    hits go through the same guards;
  * a file whose own name says "... (Instrumental)" is instrumental whatever
    any API says, and lyrics evidence (embedded LYRICS or an .lrc sidecar)
    says not instrumental;
  * Spotify audio-features bands `instrumentalness` (>= 0.5 instrumental,
    <= 0.2 not, in between no answer) and the deprecated endpoint's 403/404 is
    NO ANSWER plus ONE log line, never a crash; EVERY ISRC the file states is
    asked (the advisory ladder's own `_isrc_codes`), so a clean first code
    cannot hide the instrumental second one, and a source asked more than once
    keeps its strongest answer;
  * the merge rule lives in ONE place, `merge_instrumental`: instrumental
    anywhere → 1, else not-instrumental anywhere → 0, else None with NOTHING
    written (absence of evidence is never recorded as a value);
  * the AI provider (spec R319) is the LAST source and is asked ONCE for a
    track with no lyrics at all that no source stated anything about: a lone
    0/1 after trimming is written the way the app writes INSTRUMENTAL and
    attributed to the AI, while prose, a hedge, a stray digit inside a
    sentence or an empty reply writes NOTHING and records the reason. A track
    carrying lyrics, one a source already answered for, `instrumental_ai_
    classify` off and an unconfigured endpoint all cost no model call, and a
    failed/timed-out call leaves the file exactly as it was, inside the import
    path (`server.imports.fetch_instrumentals`);
  * a variant is never the track, in BOTH directions;
  * fetch_instrumentals writes the merged value with its per-source evidence,
    never overwrites an existing 0/1 (the user's edit wins) and writes nothing
    when no source stated anything.

Run:  python tools/test_instrumental.py
"""
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import integrations as intg
from server import instrumental as inst


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def clear():
    intg._ADVISORY_CACHE.clear()
    intg._SPOTIFY_TOKEN.clear()
    inst._spotify_warned = False


_REAL_LRCLIB, _REAL_JSON, _REAL_TOKEN = (intg._lrclib_get, intg._advisory_json,
                                         intg._spotify_token)


def stub_lrclib(routes):
    """routes: {"get"|"search": payload | (status, payload) | callable(params)}.

    A callable is what lets one endpoint answer differently per track (the
    variant / writing cases need that); returning None means "404, no hit".
    """
    calls = []

    def fake_get(endpoint, params, timeout=15, retries=3):
        calls.append((endpoint, dict(params)))
        hit = (routes or {}).get(endpoint)
        if callable(hit):
            hit = hit(params)
        if hit is None:
            return FakeResponse({"message": "not found"}, 404)
        status, payload = hit if isinstance(hit, tuple) else (200, hit)
        return FakeResponse(payload, status)

    intg._lrclib_get = fake_get
    return calls


def stub_spotify(routes=None, token="tok"):
    """Spotify's two calls (the ISRC search and audio-features). A `None`
    payload is exactly what the real seam returns for a 403/404.

    A payload may be a callable `(url, params)` — the ISRC search is asked
    once per code and has to answer per code (the same shape `stub_lrclib`
    allows for its own per-track routing).
    """
    calls = []

    def fake_json(url, params=None, headers=None, timeout=None, host=None):
        calls.append((url, dict(params or {})))
        for key, payload in (routes or {}).items():
            if key in url:
                return payload(url, params or {}) if callable(payload) else payload
        return None

    intg._advisory_json = fake_json
    intg._spotify_token = lambda cfg, timeout=None: token
    return calls


# --------------------------------------------------------------------------- #
# The fake audio layer: tags, lyrics and a duration per file
# --------------------------------------------------------------------------- #
class FakeAudio:
    files = {}          # path -> {"tags": {...}, "lyrics": str|None}

    def __init__(self, path):
        self.path = path
        self.audio = object()
        entry = FakeAudio.files.get(path) or {}
        # the duration lives on mutagen's info object for a real file; the
        # fake carries it in `tech`, the other place `_duration_seconds` reads
        self.tech = {"length": entry.get("duration")} if entry else {}
        self.tags = dict(entry.get("tags") or {})
        self._lyrics = entry.get("lyrics")

    def get_tag(self, name):
        return self.tags.get(name)

    def set_tag(self, name, value):
        self.tags[name] = value
        FakeAudio.files.setdefault(self.path, {})["tags"] = dict(self.tags)
        return True

    def get_lyrics(self):
        return self._lyrics

    def set_lyrics(self, text):
        self._lyrics = text
        FakeAudio.files.setdefault(self.path, {})["lyrics"] = text
        return True


from mlo import audio as mlo_audio
from server import imports

_real_audiofile = mlo_audio.AudioFile
mlo_audio.AudioFile = FakeAudio

ROOT = tempfile.mkdtemp(prefix="mlo_instrumental_")


def track(name, *, title, artist="Rush", album="Moving Pictures", duration=260,
          lyrics=None, extra=None, lrc=False):
    """One fake track on disk, with the tags the detector reads."""
    path = os.path.join(ROOT, name)
    with open(path, "wb") as fh:
        fh.write(b"")
    tags = {"TITLE": title, "ARTIST": artist, "ALBUM": album}
    tags.update(extra or {})
    FakeAudio.files[path] = {"tags": tags, "lyrics": lyrics,
                             "duration": duration}
    af = FakeAudio(path)
    if lrc:
        with open(os.path.splitext(path)[0] + ".lrc", "w", encoding="utf-8") as fh:
            fh.write("[00:01.00]la la\n")
    return path


def detect(path, cfg=None):
    return inst.detect_instrumental([path], cfg or {})[path]


try:
    # ----------------------------------------------------------------- #
    # 1) LRCLIB — the primary source, both ways
    # ----------------------------------------------------------------- #
    clear()
    calls = stub_lrclib({"get": {"instrumental": True, "trackName": "YYZ",
                                 "duration": 260}})
    p = track("01 - YYZ.flac", title="YYZ")
    hit = detect(p)
    assert hit["value"] == 1 and hit["answers"] == {"lrclib": 1}, hit
    assert calls == [("get", {"artist_name": "Rush", "track_name": "YYZ",
                              "album_name": "Moving Pictures",
                              "duration": 260})], calls
    assert "lrclib" in hit["evidence"], hit

    clear()
    stub_lrclib({"get": {"instrumental": False, "trackName": "Boom!",
                         "duration": 135}})
    p = track("02 - Boom!.flac", title="Boom!", artist="System Of A Down",
              album="Steal This Album!", duration=135)
    hit = detect(p)
    assert hit["value"] == 0 and hit["answers"] == {"lrclib": 0}, hit

    # a hit nobody flagged states nothing
    clear()
    stub_lrclib({"get": {"trackName": "Boom!", "duration": 135}})
    hit = detect(p)
    assert hit["value"] is None and hit["answers"] == {}, hit

    # ----------------------------------------------------------------- #
    # 2) The guards: the hit must be THIS track, at THIS length
    # ----------------------------------------------------------------- #
    yyz = track("01b - YYZ.flac", title="YYZ")   # its own 260 s copy
    # another master more than 5 s away is a different recording
    clear()
    stub_lrclib({"get": {"instrumental": True, "trackName": "YYZ",
                         "duration": 271}})
    hit = detect(yyz)
    assert hit["value"] is None and hit["answers"] == {}, hit
    clear()
    stub_lrclib({"get": {"instrumental": True, "trackName": "YYZ",
                         "duration": 263}})          # 3 s apart: accepted
    assert detect(yyz)["answers"] == {"lrclib": 1}

    # a VARIANT hit is never the track — no instrumental value from an
    # "(Instrumental)" hit, and the fallback skips it for the real one
    clear()
    calls = stub_lrclib({"get": (400, {"message": "no match"}),
                         "search": [
                             {"instrumental": True,
                              "trackName": "YYZ (Instrumental)",
                              "duration": 260},
                             {"instrumental": True, "trackName": "YYZ",
                              "duration": 259}]})
    hit = detect(yyz)
    assert hit["answers"] == {"lrclib": 1}, hit
    assert [c[0] for c in calls] == ["get", "search"], calls

    # ... and the reverse direction: a file that IS the variant never takes
    # the original's answer
    clear()
    p_var = track("03 - Boom! (Instrumental).flac", title="Boom! (Instrumental)",
                  artist="System Of A Down", album="Steal This Album!",
                  duration=135)
    stub_lrclib({"get": {"instrumental": False, "trackName": "Boom!",
                         "duration": 135}})
    assert detect(p_var)["answers"] == {"title": 1}

    # the guard is its own check, not just "the names differ": these two
    # normalize to the SAME key, so only the variant kind separates them
    assert intg._norm_compare("Boom! (Instrumental)") == \
        intg._norm_compare("Boom! Instrumental")
    clear()
    stub_lrclib({"get": (400, {"message": "no"}),
                 "search": [
                     {"instrumental": True, "trackName": "Boom! Instrumental",
                      "duration": 135}]})
    assert detect(p_var)["answers"] == {"title": 1}

    # ----------------------------------------------------------------- #
    # 3) The file's own name and its lyrics evidence
    # ----------------------------------------------------------------- #
    clear()
    stub_lrclib({})
    p = track("04 - Instrumental.flac", title="Instrumental Version")
    hit = detect(p)
    assert hit["value"] == 1 and hit["answers"] == {"title": 1}, hit

    clear()
    stub_lrclib({})
    p = track("05 - Song.flac", title="Song", lyrics="la la la\n")
    hit = detect(p)
    assert hit["value"] == 0 and hit["answers"] == {"lyrics": 0}, hit

    # an .lrc sidecar is the same evidence
    clear()
    stub_lrclib({})
    assert detect(track("06 - Sidecar.flac", title="Sidecar",
                        lrc=True))["answers"] == {"lyrics": 0}

    # A file that carries lyrics is never asked of the NETWORK: `_lyrics_answer`
    # states the 0 locally, so LRCLIB and Spotify would spend their requests
    # (and LRCLIB's 0.4 s spacing) on a question the album already answered —
    # the single biggest cost of Auto tagging on a library that had already
    # been lyric-fetched.
    clear()
    no_lrclib = stub_lrclib({"get": {"instrumental": True, "trackName": "Sung"}})
    no_spotify = stub_spotify({"v1/search": {"tracks": {"items": [
        {"id": "sp-x", "external_ids": {"isrc": "USRC17607839"}}]}}})
    sung = track("05b - Sung.flac", title="Sung", duration=200,
                 lyrics="la la la\n", extra={"ISRC": "USRC17607839"})
    hit = detect(sung, {"spotify_client_id": "cid", "spotify_client_secret": "sec"})
    assert hit["value"] == 0 and hit["answers"] == {"lyrics": 0}, hit
    assert no_lrclib == [] and no_spotify == [], (no_lrclib, no_spotify)

    # ----------------------------------------------------------------- #
    # 4) Spotify audio-features — banding, and the deprecated endpoint
    # ----------------------------------------------------------------- #
    ISRC_CFG = {"spotify_client_id": "cid", "spotify_client_secret": "sec"}
    SEARCH_HIT = {"tracks": {"items": [
        {"id": "sp-1", "external_ids": {"isrc": "USRC17607839"}}]}}

    def sp(score, name):
        clear()
        p = track(name, title=name, extra={"ISRC": "USRC17607839"})
        stub_lrclib({})
        calls = stub_spotify({"v1/search": SEARCH_HIT,
                              "audio-features": {"instrumentalness": score}})
        return calls, detect(p, ISRC_CFG)

    calls, hit = sp(0.71, "07a")
    assert hit["value"] == 1 and hit["answers"] == {"spotify": 1}, hit
    assert "api.spotify.com/v1/audio-features/sp-1" in calls[1][0], calls
    assert sp(0.11, "07b")[1]["answers"] == {"spotify": 0}
    # in between (0.2 < x < 0.5) Spotify is not saying either way
    hit = sp(0.35, "07c")[1]
    assert hit["value"] is None and hit["answers"] == {}, hit

    # EVERY ISRC the file states is asked, not just the first: one recording
    # is published in several territories under several codes, and the old
    # `.split(";")[0]` let a clean first code hide the instrumental second
    # one. `integrations._isrc_codes` is the advisory ladder's own reader, so
    # the two paths ask the same codes in the same (tag) order, and the
    # strongest answer wins over the later clean code.
    clear()
    p = track("07d - TwoPressings.flac", title="TwoPressings",
              extra={"ISRC": "USRC17607839; GBAYE0601498"})
    stub_lrclib({})

    def _search(_url, params):
        code = str(params.get("q") or "").split(":", 1)[-1]
        return {"tracks": {"items": [{"id": "sp-" + code.lower(),
                                      "external_ids": {"isrc": code}}]}}

    calls = stub_spotify({
        "v1/search": _search,
        "audio-features": lambda url, _params: (
            {"instrumentalness": 0.9} if url.endswith("sp-gbaye0601498")
            else {"instrumentalness": 0.05}),
    })
    hit = detect(p, ISRC_CFG)
    assert [c[1]["q"] for c in calls if "search" in c[0]] == \
        ["isrc:USRC17607839", "isrc:GBAYE0601498"], calls
    assert hit["value"] == 1 and hit["answers"] == {"spotify": 1}, hit
    # the same two codes, in the other order: the answer does not depend on
    # which pressing answered first
    clear()
    p2 = track("07e - TwoPressings2.flac", title="TwoPressings2",
               extra={"ISRC": "GBAYE0601498; USRC17607839"})
    stub_lrclib({})
    stub_spotify({"v1/search": _search,
                  "audio-features": lambda url, _params: (
                      {"instrumentalness": 0.9}
                      if url.endswith("sp-gbaye0601498")
                      else {"instrumentalness": 0.05})})
    hit = detect(p2, ISRC_CFG)
    assert hit["value"] == 1 and hit["answers"] == {"spotify": 1}, hit

    # A file with NO ISRC is never asked at all (unchanged).
    clear()
    p3 = track("07f - NoIsrc.flac", title="NoIsrc")
    stub_lrclib({})
    calls = stub_spotify({"v1/search": SEARCH_HIT})
    assert detect(p3, ISRC_CFG)["value"] is None
    assert calls == [], calls

    # the deprecated endpoint (403/404) is NO answer, one log line, no crash
    clear()
    p = track("08 - Gone.flac", title="Gone", extra={"ISRC": "USRC17607839"})
    stub_lrclib({})
    stub_spotify({"v1/search": SEARCH_HIT, "audio-features": None})
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        assert detect(p, ISRC_CFG)["value"] is None
        assert detect(p, ISRC_CFG)["value"] is None      # not once per track
    lines = [ln for ln in log.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1 and "spotify" in lines[0], lines

    # no credentials → Spotify is never asked at all
    clear()
    p = track("09 - NoCreds.flac", title="NoCreds",
              extra={"ISRC": "USRC17607839"})
    stub_lrclib({})
    calls = stub_spotify({"v1/search": SEARCH_HIT}, token=None)
    assert detect(p)["value"] is None
    assert calls == [], calls

    # ----------------------------------------------------------------- #
    # 5) Merge: instrumental anywhere wins, and the rule lives in one place
    # ----------------------------------------------------------------- #
    assert inst.merge_instrumental({}) is None
    assert inst.merge_instrumental({"a": 0}) == 0
    assert inst.merge_instrumental({"a": 0, "b": 1}) == 1
    assert inst.merge_instrumental({"a": 1, "b": 0}) == 1

    clear()
    p = track("10 - Conflict (Instrumental).flac",
              title="Conflict (Instrumental)", duration=100)
    stub_lrclib({"get": (400, {"message": "no"}),
                 "search": [
                     {"instrumental": False,
                      "trackName": "Conflict (Instrumental)",
                      "duration": 100}]})
    hit = detect(p)
    assert hit["value"] == 1, hit
    assert hit["answers"] == {"title": 1, "lrclib": 0}, hit

    # ----------------------------------------------------------------- #
    # 6) Writing: merged value + evidence, the user's value wins, and nothing
    #    at all when no source stated anything
    # ----------------------------------------------------------------- #
    ROOT = tempfile.mkdtemp(prefix="mlo_instrumental_write_")   # fresh folder
    clear()
    unknown = track("11 - Unknown.flac", title="Unknown", duration=200)
    instrumental = track("12 - YYZ2.flac", title="YYZ2", duration=200)
    manual = track("13 - Manual.flac", title="Manual", duration=200,
                   extra={"INSTRUMENTAL": "1"})
    answers = {"YYZ2": {"instrumental": True, "trackName": "YYZ2",
                        "duration": 200},
               "Manual": {"instrumental": False, "trackName": "Manual",
                          "duration": 200}}
    stub_lrclib({"get": lambda params: answers.get(params.get("track_name"))})
    out = imports.fetch_instrumentals([ROOT], {"instrumental_auto_fetch": True})
    assert out["updated"] == 1, out
    assert out["values"] == {instrumental: 1, manual: 1}, out
    assert out["evidence"] == {instrumental: {"lrclib": 1},
                               manual: {"lrclib": 0}}, out

    def written(path):
        return FakeAudio.files[path]["tags"].get("INSTRUMENTAL")

    assert written(instrumental) == "1", FakeAudio.files
    assert written(manual) == "1", FakeAudio.files      # the user's edit wins
    assert written(unknown) is None, FakeAudio.files    # nobody stated anything

    # instrumental_auto_fetch off → nothing at all
    assert imports.fetch_instrumentals(
        [ROOT], {"instrumental_auto_fetch": False})["updated"] == 0

    # ----------------------------------------------------------------- #
    # 7) The lyrics-absent rule (R162): a lyrics search that found nothing
    #    settles the track, unless something better already states it — and
    #    the fetch that DID find lyrics never reaches it
    # ----------------------------------------------------------------- #
    from mlo import lyrics_fetch

    ROOT = tempfile.mkdtemp(prefix="mlo_instrumental_lyrics_")
    clear()

    # The double mirrors `mlo.lyrics_providers.fetch_lyrics`, which
    # `lyrics_fetch` re-exports: a parameter the real one grows has to appear
    # here too (`aliases` is the alias second pass), or every caller that
    # passes it dies inside this fake with a TypeError instead of testing.
    def empty_chain(config, artist, title, album, duration,
                    youtube_id=None, min_score=None, aliases=None):
        return None

    real_fetch_lyrics = lyrics_fetch.fetch_lyrics
    real_fetch_audio = lyrics_fetch.AudioFile
    lyrics_fetch.fetch_lyrics = empty_chain
    lyrics_fetch.AudioFile = FakeAudio
    CFG = {"music_folder": ROOT, "instrumental_auto_fetch": True,
           "lyrics_format": "EMBEDDED", "lyrics_sources": ["lrclib"]}
    try:
        # nothing anywhere: the app's own answer, with its own source
        silent = track("20 - Silent.flac", title="Silent", duration=200)
        res = lyrics_fetch.fetch_one(silent, CFG)
        assert res["status"] == "skipped" and res["marked_instrumental"] is True, res
        assert res["reason"] == "no lyrics found — marked INSTRUMENTAL", res
        assert res["instrumental"] == {"value": 1,
                                       "evidence": {inst.LYRICS_ABSENT: 1}}, res
        assert written(silent) == "1", FakeAudio.files[silent]
        assert "no provider" in res["instrumental_note"], res

        # the same rule through the writer alone, and the switch it honours
        assert inst.lyrics_absent([silent], CFG)["updated"] == 0   # already 1
        off = inst.lyrics_absent([silent], {"instrumental_auto_fetch": False})
        assert off["updated"] == 0 and off["values"] == {} and off["skipped"], off

        # a source that says the track HAS vocals wins over the empty search
        clear()
        vocals = track("21 - Vocals.flac", title="Vocals", duration=200)
        stub_lrclib({"get": {"instrumental": False, "trackName": "Vocals",
                             "duration": 200}})
        res = lyrics_fetch.fetch_one(vocals, CFG)
        assert not res.get("marked_instrumental"), res
        assert written(vocals) is None, FakeAudio.files[vocals]
        assert "states vocals" in res.get("instrumental_note", ""), res

        # a source that says INSTRUMENTAL states the value itself
        clear()
        stated = track("22 - Stated.flac", title="Stated", duration=200)
        stub_lrclib({"get": {"instrumental": True, "trackName": "Stated",
                             "duration": 200}})
        res = lyrics_fetch.fetch_one(stated, CFG)
        assert res["instrumental"]["evidence"] == {"lrclib": 1}, res
        assert written(stated) == "1", FakeAudio.files[stated]

        # lyrics the fetch DID find change nothing: no mark, and the tag stays
        # empty on the file
        clear()
        found = track("23 - Found.flac", title="Found", duration=200)
        stub_lrclib({})
        lyrics_fetch.fetch_lyrics = lambda *a, **k: {
            "provider": "lrclib", "provider_label": "LRCLIB", "synced": "",
            "plain": "[00:01.00]words\n"}
        try:
            import mlo.lyrics as mlo_lyrics

            real_write = mlo_lyrics._atomic_write_text
            mlo_lyrics._atomic_write_text = lambda *a, **k: None
            real_proc = lyrics_fetch._process_lyrics_for_audio
            lyrics_fetch._process_lyrics_for_audio = lambda *a, **k: None
            try:
                res = lyrics_fetch.fetch_one(found, CFG)
            finally:
                mlo_lyrics._atomic_write_text = real_write
                lyrics_fetch._process_lyrics_for_audio = real_proc
        finally:
            lyrics_fetch.fetch_lyrics = empty_chain
        assert res["status"] == "ok" and not res.get("marked_instrumental"), res
        assert written(found) is None, FakeAudio.files[found]

        # a track that already carries lyrics is skipped before the search and
        # is never marked (its words ARE the evidence it has vocals)
        clear()
        sung = track("24 - Sung.flac", title="Sung", duration=200,
                     lyrics="[00:01.00]words")
        res = lyrics_fetch.fetch_one(sung, CFG)
        assert res["reason"] == "lyrics already present", res
        assert written(sung) is None, FakeAudio.files[sung]
    finally:
        lyrics_fetch.fetch_lyrics = real_fetch_lyrics
        lyrics_fetch.AudioFile = real_fetch_audio

    # ----------------------------------------------------------------- #
    # 8) The AI (spec R319): the LAST source, asked ONLY for a lyric-less,
    #    evidence-less track, and parsed strictly as a lone 0/1
    # ----------------------------------------------------------------- #
    from server import ai as ai_mod

    _real_ai_chat, _real_ai_configured = ai_mod.ai_chat, ai_mod.ai_configured
    AI_CFG = dict(CFG, ai_base_url="http://127.0.0.1:9/v1",
                  ai_model="test-model", instrumental_ai_classify=True)
    try:
        def stub_ai(reply):
            """A configured endpoint whose answer is `reply` (or raises it)."""
            calls = []

            def fake_chat(cfg, system, user, timeout=90.0):
                calls.append((system, user, timeout))
                if isinstance(reply, Exception):
                    raise reply
                return reply

            ai_mod.ai_configured = lambda c: True
            ai_mod.ai_chat = fake_chat
            return calls

        # (a) a lyric-less track nobody states anything about: the model's lone
        #     "1" is written the way the app writes INSTRUMENTAL, attributed to
        #     the AI rather than to LRCLIB, and the prompt it was asked with
        #     demands the single digit and names the track
        clear()
        stub_lrclib({})
        calls = stub_ai(" 1\n")          # a lone 1 after TRIM is the answer
        ai_track = track("30 - AiTrack.flac", title="AiTrack", duration=200)
        out = imports.fetch_instrumentals([ai_track], AI_CFG)
        assert out["updated"] == 1 and out["values"] == {ai_track: 1}, out
        assert out["evidence"] == {ai_track: {"ai": 1}}, out
        assert written(ai_track) == "1", FakeAudio.files[ai_track]
        assert len(calls) == 1, calls
        system, user, timeout = calls[0]
        assert "ONE digit" in system and "NOTHING else" in system, system
        assert "instrumental" in system and "0 if it has vocals" in system, system
        assert "Title: AiTrack" in user and "Artist: Rush" in user, user
        assert "Album: Moving Pictures" in user, user
        assert timeout > 0, calls

        # (b) a lone "0" is the negative answer, written the same way
        clear()
        stub_lrclib({})
        stub_ai("0")
        vocals_ai = track("31 - AiVocals.flac", title="AiVocals", duration=200)
        out = imports.fetch_instrumentals([vocals_ai], AI_CFG)
        assert out["values"] == {vocals_ai: 0}, out
        assert out["evidence"] == {vocals_ai: {"ai": 0}}, out
        assert written(vocals_ai) == "0", FakeAudio.files[vocals_ai]

        # (c) anything that is not a LONE 0 or 1 is no answer: nothing may be
        #     written out of prose, a hedge, a stray digit inside a sentence or
        #     an empty reply, and the reason says the model replied without one
        for reply in ("This track is instrumental, I think.", "maybe",
                      "1 (instrumental)", "0, although it is hard to say", "",
                      "yes", "10"):
            clear()
            stub_lrclib({})
            stub_ai(reply)
            bad = track("32 - Bad.flac", title="Bad", duration=200)
            hit = detect(bad, AI_CFG)
            assert hit["value"] is None and hit["answers"] == {}, (reply, hit)
            assert "not a lone 0 or 1" in (hit["ai"] or ""), (reply, hit)
            assert "no source stated anything" in hit["evidence"], (reply, hit)
            out = imports.fetch_instrumentals([bad], AI_CFG)
            assert out["updated"] == 0 and out["values"] == {}, (reply, out)
            assert written(bad) is None, (reply, FakeAudio.files[bad])

        # (d) a track that HAS lyrics never costs a call — its words are the
        #     evidence it has vocals, and the model is never asked to
        #     second-guess them (an embedded LYRICS tag and a real .lrc
        #     sidecar both count)
        clear()
        stub_lrclib({})
        calls = stub_ai("1")
        sung_ai = track("33 - SungAi.flac", title="SungAi", duration=200,
                        lyrics="[00:01.00]words")
        side_ai = track("34 - SideAi.flac", title="SideAi", duration=200, lrc=True)
        for with_words in (sung_ai, side_ai):
            hit = detect(with_words, AI_CFG)
            assert hit["value"] == 0, hit
            assert hit["answers"] == {"lyrics": 0}, hit
            assert hit["ai"] is None, hit
        assert calls == [], "a track with lyrics never reaches the model"

        # (e) a source that STATED a value ends the question: the AI answers
        #     silence, it is not a second opinion on somebody else's answer
        clear()
        stub_lrclib({"get": {"instrumental": False, "trackName": "StatedAi",
                             "duration": 200}})
        calls = stub_ai("1")
        stated_ai = track("35 - StatedAi.flac", title="StatedAi", duration=200)
        hit = detect(stated_ai, AI_CFG)
        assert hit["value"] == 0 and hit["answers"] == {"lrclib": 0}, hit
        assert calls == [], "a stated answer is never second-guessed"

        # (f) `instrumental_ai_classify` off: the model is never spoken to,
        #     through the detector and through the import's own writer
        clear()
        stub_lrclib({})
        calls = stub_ai("1")
        off_cfg = dict(AI_CFG, instrumental_ai_classify=False)
        off_track = track("36 - AiOff.flac", title="AiOff", duration=200)
        hit = detect(off_track, off_cfg)
        assert hit["value"] is None and hit["ai"] is None, hit
        assert imports.fetch_instrumentals([off_track], off_cfg)["updated"] == 0
        assert written(off_track) is None, FakeAudio.files[off_track]
        assert calls == [], "the switch off: the AI is never asked"

        # (g) a call that FAILS (timeout, bad key, dead endpoint) leaves the
        #     track exactly as it was, says so, and never breaks the import
        clear()
        stub_lrclib({})
        stub_ai(TimeoutError("no answer"))
        dead = track("37 - AiTimeout.flac", title="AiTimeout", duration=200)
        hit = detect(dead, AI_CFG)
        assert hit["value"] is None and hit["answers"] == {}, hit
        assert "ai call failed" in (hit["ai"] or ""), hit
        out = imports.fetch_instrumentals([dead], AI_CFG)
        assert out["updated"] == 0 and written(dead) is None, out

        # (h) with no endpoint configured the question is never asked at all —
        #     an app without an AI behaves exactly as it did before this source
        clear()
        stub_lrclib({})
        calls = stub_ai("1")
        ai_mod.ai_configured = _real_ai_configured
        plain = track("38 - NoEndpoint.flac", title="NoEndpoint", duration=200)
        hit = detect(plain)
        assert hit["value"] is None and hit["ai"] is None, hit
        assert calls == [], "an unconfigured endpoint is never called"

        # (i) one call per track, no more: two lyric-less tracks cost two
        clear()
        stub_lrclib({})
        calls = stub_ai("1")
        pair = [track("39 - Pair A.flac", title="Pair A", duration=200),
                track("40 - Pair B.flac", title="Pair B", duration=200)]
        out = imports.fetch_instrumentals(pair, AI_CFG)
        assert out["updated"] == 2 and len(calls) == 2, (out, calls)
        assert {p: FakeAudio.files[p]["tags"].get("INSTRUMENTAL")
                for p in pair} == {pair[0]: "1", pair[1]: "1"}, FakeAudio.files
    finally:
        ai_mod.ai_chat, ai_mod.ai_configured = _real_ai_chat, _real_ai_configured
finally:
    mlo_audio.AudioFile = _real_audiofile
    intg._lrclib_get, intg._advisory_json, intg._spotify_token = (
        _REAL_LRCLIB, _REAL_JSON, _REAL_TOKEN)

print("instrumental: all assertions passed")
