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
    NO ANSWER plus ONE log line, never a crash;
  * the merge rule lives in ONE place, `merge_instrumental`: instrumental
    anywhere → 1, else not-instrumental anywhere → 0, else None with NOTHING
    written (absence of evidence is never recorded as a value);
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
    payload is exactly what the real seam returns for a 403/404."""
    calls = []

    def fake_json(url, params=None, headers=None, timeout=None, host=None):
        calls.append((url, dict(params or {})))
        for key, payload in (routes or {}).items():
            if key in url:
                return payload
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
finally:
    mlo_audio.AudioFile = _real_audiofile
    intg._lrclib_get, intg._advisory_json, intg._spotify_token = (
        _REAL_LRCLIB, _REAL_JSON, _REAL_TOKEN)

print("instrumental: all assertions passed")
