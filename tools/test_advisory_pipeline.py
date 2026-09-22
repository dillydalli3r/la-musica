#!/usr/bin/env python3
"""ITUNESADVISORY pipeline — the defects the audit reproduced, pinned.

What this file holds, and why each assertion is here (see
`server/integrations.resolve_advisory_route` / `server.imports.fetch_advisories`):

  * EVERY ISRC the file states is asked. `AudioFile.get_tag` joins repeated
    fields with "; ", so an ISRC tag may name several pressings of the same
    recording; taking the first alone left the file's own tag contributing ONE
    code while the route's contract says "every ISRC the track has".
  * `fetch_advisories` derives ALBUMITUNESADVISORY for the album folders it
    touched. Script 8 (the album tag's writer) does not run behind the manual
    surfaces — the wizard's advisory step, the album page's Check and the
    tag-actions item — so without this pass every track was rated and the album
    tag stayed empty, and the album failed grading on
    "Missing album tag ALBUMITUNESADVISORY".
  * a fetch refused by the write gate SAYS SO, and the two ADVISORY tags answer
    to their own writer's switch: `ITUNESADVISORY` to the FETCH's
    `advisory_auto_fetch`, `ALBUMITUNESADVISORY` to script 8's `auto_advisory`
    derivation (`mlo/config.py::_TAG_WRITE_SWITCH`). One family switch for both
    meant that turning the derivation off silently disabled the explicit
    "Fetch advisory rating" action, whose reply read exactly like "nobody
    stated anything" — a silent no-op instead of an answer.
  * titles are compared accent-folded: `_norm_compare` used to turn a letter it
    could not fold (ö, é, ü) into a SEPARATOR, so "Störagéd" read as
    "st rag d" — it matched neither "Storaged" nor anything else a provider
    spells without the diacritics, and "Motörhead" collided with "Mot rhead".
  * a track with no ISRC and no MusicBrainz id is still rated (Apple's album
    route and Apple's song search are asked), and a track NOBODY stated
    anything about is reported as such: `answers` empty, `sources` naming the
    ladder's stage — so a caller can tell "detected clean" from "nobody spoke".
  * a value the file ALREADY carries is echoed with its provenance
    (`sources` = "existing-tag") and a `status` saying it was not re-checked,
    so the readout can never show a value beside "source unknown" again;
    `force=True` is the re-rate that asks those files and writes what the
    sources state — and even then an INVENTED fallback never overwrites a
    stored rating. The endpoint that carries both (`POST
    /api/mb/advisory/fetch`) is exercised here too: a dropped `force` or
    `status` is a re-rate button that silently does nothing.
  * ONE source asked once per pressing keeps its STRONGEST answer (1 > 0 > 2):
    a clean answer for the first ISRC must not suppress an explicit answer for
    a later one of the same track.

Run:  python tools/test_advisory_pipeline.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import integrations as intg
from server import imports

# The two ISRCs of one recording (the "Streamline" case the audit measured):
# Deezer holds both, from two different pressings.
FIRST_ISRC, SECOND_ISRC = "GBAAA0000001", "USSM10213523"

# No Spotify credentials: the documented "source skipped" state.
CFG = {}


def stub_http(routes):
    """Replace integrations' two HTTP seams; returns the call log.

    Same shape as tools/test_advisory_sources.py: the log is
    (METHOD, url, params-or-data, headers) tuples, and `routes` maps a URL
    substring to a payload (or a callable taking the request's params/data).
    """
    calls = []

    def _route(url, arg):
        for key, payload in routes.items():
            if key in url:
                return payload(arg) if callable(payload) else payload
        return None

    def fake_get(url, params=None, headers=None, timeout=None, host=None):
        calls.append(("GET", url, dict(params or {}), dict(headers or {})))
        return _route(url, params or {})

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append(("POST", url, dict(data or {}), dict(headers or {})))
        return _route(url, data or {}), ""

    intg._advisory_json, intg._advisory_post = fake_get, fake_post
    return calls


def clear():
    """A fresh cache and a silent transport for the section that follows."""
    intg._ADVISORY_CACHE.clear()
    intg._SPOTIFY_TOKEN.clear()
    return stub_http({})


def gets(calls, url_part):
    return [c for c in calls if c[0] == "GET" and url_part in c[1]]


# the iTunes disk cache belongs to a real app run, and the politeness sleep is
# not what this file measures
intg._apple_cache_dir = lambda: None
intg._APPLE_MIN_INTERVAL = 0.0

# Apple answers nothing about these made-up artists, so only the Deezer legs
# can state anything — which is what makes the ISRC count observable.
NO_APPLE = {"itunes.apple.com/search": {"results": []},
            "itunes.apple.com/lookup": {"results": []}}


# --------------------------------------------------------------------------- #
# 1) The file's OWN ISRC tag: every ISRC on it is asked, not just the first
# --------------------------------------------------------------------------- #
clear()
calls = stub_http(dict(NO_APPLE, **{
    # only the SECOND pressing is held by Deezer, and it states explicit
    "api.deezer.com/track/isrc:" + SECOND_ISRC: {
        "explicit_lyrics": True, "explicit_content_lyrics": 1},
    "api.deezer.com": {"error": {"type": "DataException"}},
}))
route = intg.resolve_advisory_route(
    isrc=FIRST_ISRC + "; " + SECOND_ISRC,   # how get_tag reads a repeated field
    title="Streamline", artist="System Of A Down", cfg=CFG)
assert route["value"] == 1, route
assert route["answers"] == {"deezer-isrc": 1}, route
assert len(gets(calls, "api.deezer.com")) == 2, calls

# a list is the same statement in a caller's own words
clear()
stub_http(dict(NO_APPLE, **{
    "api.deezer.com/track/isrc:" + SECOND_ISRC: {
        "explicit_lyrics": True, "explicit_content_lyrics": 1},
    "api.deezer.com": {"error": {"type": "DataException"}},
}))
assert intg.resolve_advisory_route(isrc=[FIRST_ISRC, SECOND_ISRC],
                                   cfg=CFG)["answers"] == {"deezer-isrc": 1}

# ... and when Deezer holds BOTH pressings and they disagree, the source's
# STRONGEST answer is the one that counts: the first pressing's clean answer
# used to win by ask order (`setdefault`), so a track Deezer itself flags
# explicit was written 0
clear()
calls = stub_http(dict(NO_APPLE, **{
    "api.deezer.com/track/isrc:" + FIRST_ISRC: {
        "explicit_lyrics": False, "explicit_content_lyrics": 0},
    "api.deezer.com/track/isrc:" + SECOND_ISRC: {
        "explicit_lyrics": True, "explicit_content_lyrics": 1},
    "api.deezer.com": {"error": {"type": "DataException"}},
}))
route = intg.resolve_advisory_route(isrc=FIRST_ISRC + "; " + SECOND_ISRC,
                                    cfg=CFG)
assert route["value"] == 1 and route["source"] == "deezer-isrc", route
assert route["answers"] == {"deezer-isrc": 1}, route
assert len(gets(calls, "api.deezer.com")) == 2, calls

# --------------------------------------------------------------------------- #
# 2) A track with NO ISRC and NO MusicBrainz id: the name routes rate it
# --------------------------------------------------------------------------- #
# With no identity at all, Apple's album route (artist → editions → the track)
# and Apple's song search are the only sources left, and they are BOTH asked.
# This is a route contract, not one of the fixes: it is what makes an
# identity-less file rateable at all.
clear()
calls = stub_http({
    "itunes.apple.com/search": lambda p: (
        {"results": [{"wrapperType": "artist", "artistId": 462715,
                      "artistName": "System Of A Down"}]}
        if p.get("entity") == "musicArtist" else
        {"results": [{"trackName": "Boom!", "artistName": "System Of A Down",
                      "trackExplicitness": "explicit"}]}),
    "itunes.apple.com/lookup": lambda p: (
        {"results": [{"wrapperType": "collection", "collectionId": 99,
                      "artistName": "System Of A Down",
                      "collectionName": "Steal This Album!",
                      "collectionExplicitness": "explicit",
                      "trackCount": 16}]}
        if p.get("entity") == "album" else
        {"results": [{"wrapperType": "track", "discNumber": 1, "trackNumber": 4,
                      "trackName": "Boom!", "trackExplicitness": "explicit"}]}),
})
route = intg.resolve_advisory_route(title="Boom!", artist="System Of A Down",
                                    album="Steal This Album!", disc=1, track=4,
                                    track_count=16, cfg=CFG)
assert route["checked"] == ["apple-album", "itunes-song"], route
assert route["value"] == 1 and route["source"] == "apple-album", route
# the album route really walked artist → editions → the collection's songs
assert len(gets(calls, "itunes.apple.com/lookup")) == 2, calls

# ... and with no album name to ask about, the song search is what answers
clear()
stub_http({"itunes.apple.com/search": {"results": [
    {"trackName": "Boom!", "artistName": "System Of A Down",
     "trackExplicitness": "explicit"}]}})
route = intg.resolve_advisory_route(title="Boom!", artist="System Of A Down",
                                    cfg=CFG)
assert route["checked"] == ["itunes-song"], route
assert route["value"] == 1 and route["source"] == "itunes-song", route

# --------------------------------------------------------------------------- #
# 3) fetch_advisories writes the per-track values AND the album's derived tag
# --------------------------------------------------------------------------- #
from mlo import audio as mlo_audio


class FakeAudio:
    """Tag-reading stand-in for mlo.audio.AudioFile."""

    written = {}

    def __init__(self, path):
        self.path = path
        self.audio = object()
        self.tags = dict(FakeAudio.written.get(path) or {})

    def get_tag(self, name):
        return self.tags.get(name)

    def get_lyrics(self):
        """The embedded LYRICS tag — what `mlo.lyrics_publish.local_lyrics`
        reads before it looks for an .lrc sidecar. None (no such tag) is what
        the tracks below carry unless a case puts words on one."""
        return self.tags.get("LYRICS")

    def set_tag(self, name, value):
        self.tags[name] = value
        FakeAudio.written[self.path] = dict(self.tags)
        return True


ROOT = tempfile.mkdtemp(prefix="mlo_advisory_pipeline_")
ALBUM = os.path.join(ROOT, "Steal This Album")
os.makedirs(ALBUM)
# (title, ext, ISRC, what Deezer says about it): a clean track, an explicit
# one, a track nobody states anything about, and an MP3 — the odd file type
# out is what the per-filetype half of the write gate is measured with
TRACKS = [("Chic 'N' Stu", ".flac", "USSM10213322", 0),
          ("Boom!", ".flac", "USSM10213324", 1),
          ("Roulette", ".flac", "USSM10213333", None),
          ("Streamline", ".mp3", "USSM10213523", 0)]
FILES = []
for i, (title, ext, isrc, _answer) in enumerate(TRACKS, 1):
    path = os.path.join(ALBUM, f"1-{i:02d} - {title}{ext}")
    with open(path, "wb") as fh:
        fh.write(b"")
    FakeAudio.written[path] = {"TITLE": title, "ARTIST": "System Of A Down",
                               "ALBUM": "Steal This Album!",
                               "DISCNUMBER": "1", "TRACKNUMBER": str(i),
                               "ISRC": isrc}
    FILES.append(path)
MP3 = FILES[-1]
DELIVERABLE = FILES[:3]


def deezer_routes():
    routes = dict(NO_APPLE)
    for _title, _ext, isrc, answer in TRACKS:
        routes["api.deezer.com/track/isrc:" + isrc] = (
            {"explicit_lyrics": bool(answer),
             "explicit_content_lyrics": 1 if answer else 0}
            if answer is not None else {"error": {"type": "DataException"}})
    routes["api.deezer.com"] = {"error": {"type": "DataException"}}
    return routes


_real_audiofile = mlo_audio.AudioFile
mlo_audio.AudioFile = FakeAudio
try:
    # 0 / 1 / nobody → the strictest per-track value (1) is the album's, and
    # every track of the album carries it. The tag semantics are the grader's
    # and the registry's own words: "the strictest per-track advisory,
    # repeated on every track of the album".
    for path in FILES:
        FakeAudio.written[path].pop("ALBUMITUNESADVISORY", None)
    clear()
    stub_http(deezer_routes())
    out = imports.fetch_advisories([ALBUM], dict(CFG, advisory_auto_fetch=True))
    assert out["values"] == {FILES[0]: 0, FILES[1]: 1, FILES[2]: 0, FILES[3]: 0}, out
    assert out["albums"] == {ALBUM: 1}, out
    assert out["album_updated"] == 4, out
    for path in FILES:
        assert FakeAudio.written[path]["ALBUMITUNESADVISORY"] == "1", \
            (path, FakeAudio.written[path])

    # a track already carrying a valid value is not re-asked, but it still
    # counts toward the album's derivation: its 2 must not be lost
    FakeAudio.written[FILES[2]]["ITUNESADVISORY"] = "2"
    for path in FILES:
        FakeAudio.written[path].pop("ALBUMITUNESADVISORY", None)
    clear()
    stub_http(deezer_routes())
    out = imports.fetch_advisories([ALBUM], dict(CFG, advisory_auto_fetch=True))
    assert out["albums"] == {ALBUM: 1}, out
    for path in FILES:
        assert FakeAudio.written[path]["ALBUMITUNESADVISORY"] == "1", FakeAudio.written

    # the album tag follows the per-track values DOWN too: with every track
    # clean the album reads 0, not the previous 1
    for path in FILES:
        FakeAudio.written[path].pop("ITUNESADVISORY", None)
        FakeAudio.written[path].pop("ALBUMITUNESADVISORY", None)
    clear()
    stub_http(dict(NO_APPLE, **{"api.deezer.com": {
        "explicit_lyrics": False, "explicit_content_lyrics": 0}}))
    out = imports.fetch_advisories([ALBUM], dict(CFG, advisory_auto_fetch=True))
    assert out["albums"] == {ALBUM: 0}, out
    for path in FILES:
        assert FakeAudio.written[path]["ALBUMITUNESADVISORY"] == "0", FakeAudio.written

    # ----------------------------------------------------------------------- #
    # 4) The two ADVISORY tags answer to their OWN writer's switch
    # ----------------------------------------------------------------------- #
    # `mlo/config.py::_TAG_WRITE_SWITCH` gives ITUNESADVISORY the FETCH's switch
    # (`advisory_auto_fetch`) while ALBUMITUNESADVISORY keeps script 8's
    # derivation switch (`auto_advisory`, "Auto Album Advisory"). One family
    # switch for both meant that turning the derivation off silently disabled
    # the explicit "Fetch advisory rating" action — and the refused run replied
    # exactly like "no provider knew this track". So: the per-track values land,
    # the album tag is refused, and the refusal is COUNTED and worded.
    for path in FILES:
        FakeAudio.written[path].pop("ITUNESADVISORY", None)
        FakeAudio.written[path].pop("ALBUMITUNESADVISORY", None)
    clear()
    calls = stub_http(deezer_routes())
    out = imports.fetch_advisories(
        [ALBUM], {"advisory_auto_fetch": True, "auto_advisory": False})
    assert out["updated"] == len(FILES), out
    assert out["values"] == {p: out["values"][p] for p in FILES}, out
    # The derived album value is still REPORTED (it is what the album reads),
    # while the write is refused by its own switch and COUNTED — and worded, so
    # an album whose tracks are all rated and whose album tag is empty says why.
    assert out["albums"] == {ALBUM: 1}, out
    assert out["album_updated"] == 0 and out["album_gated"] == len(FILES), out
    assert out.get("skipped") and "auto_advisory" in out["skipped"], out
    for path in FILES:
        assert FakeAudio.written[path]["ITUNESADVISORY"] in ("0", "1"), FakeAudio.written[path]
        assert "ALBUMITUNESADVISORY" not in FakeAudio.written[path], FakeAudio.written[path]
    # ...and the route WAS asked: the derated switch no longer short-circuits it
    assert gets(calls, "api.deezer.com"), calls

    # The gate can also refuse only PART of a selection — its per-filetype half
    # is the matrix's own ADVISORY column. The files that were written are
    # still written, the ones refused are COUNTED (not silently dropped), and
    # the reply is not a "nothing happened" one.
    for path in FILES:
        FakeAudio.written[path].pop("ITUNESADVISORY", None)
        FakeAudio.written[path].pop("ALBUMITUNESADVISORY", None)
    clear()
    stub_http(deezer_routes())
    out = imports.fetch_advisories([ALBUM], {
        "advisory_auto_fetch": True,
        "audio_tag_writes": {"mp3": {"ADVISORY": False}}})
    assert out["gated"] == 1 and out.get("skipped") is None, out
    assert out["updated"] == 3, out
    assert MP3 not in out["values"], out
    assert "ITUNESADVISORY" not in FakeAudio.written[MP3], FakeAudio.written[MP3]
    for path in DELIVERABLE:
        assert FakeAudio.written[path]["ITUNESADVISORY"] == str(out["values"][path]), \
            (path, out, FakeAudio.written[path])
    # the album tag is still derived, from the tracks whose values are known
    assert out["albums"] == {ALBUM: 1}, out
    assert out["album_updated"] == 3, out
    assert "ALBUMITUNESADVISORY" not in FakeAudio.written[MP3], FakeAudio.written[MP3]

    # ----------------------------------------------------------------------- #
    # 3b) The AI is a SOURCE in the fetch itself (issue #28): asked ONCE for
    #     every track this run DECIDES, ranked against what the providers
    #     stated by the app's one rule, and reported in `answers` beside them —
    #     while `sources` keeps naming the one source that decided the value.
    # ----------------------------------------------------------------------- #
    from server import ai as ai_mod

    for path in FILES:
        FakeAudio.written[path].pop("ITUNESADVISORY", None)
        FakeAudio.written[path].pop("ALBUMITUNESADVISORY", None)
    # The escalation takes a READ of the words: the first track carries an
    # explicit line, and the tracks without one show what an answer is worth
    # when the model read nothing.
    FakeAudio.written[FILES[0]]["LYRICS"] = "i dont give a fuck"
    _real_configured, _real_chat = ai_mod.ai_configured, ai_mod.ai_chat
    ai_calls = []
    ai_mod.ai_configured = lambda c: True
    ai_mod.ai_chat = lambda *a, **k: ai_calls.append(k) or "1"
    try:
        clear()
        stub_http(deezer_routes())
        out = imports.fetch_advisories([ALBUM], dict(CFG, advisory_auto_fetch=True))

        # ONE call per track this run decided — four tracks, four calls, no
        # matter how many stages wanted the answer (the escalation of a stated
        # 0 reuses the ladder's own call instead of paying for a second one)
        assert len(ai_calls) == len(FILES), ai_calls
        # Chic 'N' Stu: Deezer stated 0 and the WORD SCAN found explicit
        # language, so the scan owns the 1 and its provenance — the AI is a
        # backup and no longer escalates a stated value (owner's rule)
        # Boom!: Deezer's own 1 survives the AI's 1, so the provider keeps it
        # Roulette: nobody stated anything — the AI's answer is the ladder's
        # Silent Streamline: no words to read, and the AI is a BACKUP — it
        # neither overrules Deezer's 0 nor is reported as a source behind it
        # (the reply's `answers` map names what spoke for the VALUE)
        assert out["sources"] == {FILES[0]: "lyrics-scan (escalated)",
                                  FILES[1]: "deezer-isrc",
                                  FILES[2]: "ai",
                                  FILES[3]: "deezer-isrc"}, out
        assert out["values"] == {FILES[0]: 1, FILES[1]: 1, FILES[2]: 1,
                                 FILES[3]: 0}, out
        assert out["answers"] == {
            FILES[0]: {"deezer-isrc": 0},
            FILES[1]: {"deezer-isrc": 1},
            FILES[2]: {"ai": 1},
            FILES[3]: {"deezer-isrc": 0}}, out

        # A file whose value this run ECHOES is asked about by NOBODY — the AI
        # included: there is no decision to inform, so nothing is paid for.
        for path in FILES:
            FakeAudio.written[path]["ITUNESADVISORY"] = "1"
        ai_calls.clear()
        clear()
        stub_http(deezer_routes())
        out = imports.fetch_advisories([ALBUM], dict(CFG, advisory_auto_fetch=True))
        assert ai_calls == [], ai_calls
        assert out["sources"] == {p: "existing-tag" for p in FILES}, out
    finally:
        ai_mod.ai_configured, ai_mod.ai_chat = _real_configured, _real_chat
        FakeAudio.written[FILES[0]].pop("LYRICS", None)

    # ----------------------------------------------------------------------- #
    # 5) Nobody spoke: the stage is in `sources`, `answers` is empty — the two
    #    outcomes a caller must be able to tell apart
    # ----------------------------------------------------------------------- #
    for path in FILES:
        FakeAudio.written[path].pop("ITUNESADVISORY", None)
        FakeAudio.written[path].pop("ALBUMITUNESADVISORY", None)
    clear()
    stub_http(NO_APPLE)
    out = imports.fetch_advisories([ALBUM], dict(CFG, advisory_auto_fetch=True))
    assert out["values"] == {p: 0 for p in FILES}, out
    assert out["answers"] == {}, out
    assert out["sources"] == {p: "fallback" for p in FILES}, out
    assert out["gated"] == 0 and out.get("skipped") is None, out
    # ... and a track a provider DID state 0 for is not the same entry: it
    # carries its provider in `answers`. `fallback` versus `deezer-isrc` is the
    # difference between "detected clean" and "nobody spoke" — visible in ONE
    # reply here, because Roulette's ISRC is one Deezer does not hold.
    for path in FILES:
        FakeAudio.written[path].pop("ITUNESADVISORY", None)
    clear()
    stub_http(deezer_routes())
    out = imports.fetch_advisories([ALBUM], dict(CFG, advisory_auto_fetch=True))
    assert out["answers"] == {FILES[0]: {"deezer-isrc": 0},
                              FILES[1]: {"deezer-isrc": 1},
                              FILES[3]: {"deezer-isrc": 0}}, out
    assert out["sources"][FILES[0]] == "deezer-isrc", out
    assert out["sources"][FILES[2]] == "fallback", out

    # ----------------------------------------------------------------------- #
    # 5b) The wizard's readout, end to end. A value already on the file is
    #     ECHOED with its provenance and marked "not re-checked", and `force`
    #     is the re-rate that asks and writes. The screenshot behind this
    #     read "0 (not explicit) · source unknown" on every row with "0
    #     value(s) written" — a 0 an earlier run's fallback had written, never
    #     re-asked, with nobody's name on it.
    # ----------------------------------------------------------------------- #
    FakeAudio.written[FILES[1]]["ITUNESADVISORY"] = "0"    # the invented 0
    clear()
    stub_http(deezer_routes())      # Deezer states explicit for this ISRC
    out = imports.fetch_advisories([ALBUM], dict(CFG, advisory_auto_fetch=True))
    assert out["values"][FILES[1]] == 0, out        # echoed, nobody asked
    assert out["sources"][FILES[1]] == "existing-tag", out
    assert out["status"][FILES[1]] == "existing", out
    assert out["updated"] == 0, out                 # nothing was written
    assert out["answers"] == {}, out
    assert set(out["sources"].values()) == {"existing-tag"}, out

    # ... the re-rate asks the sources and writes what they state
    clear()
    calls = stub_http(deezer_routes())
    out = imports.fetch_advisories([ALBUM], dict(CFG, advisory_auto_fetch=True),
                                   force=True)
    assert gets(calls, "api.deezer.com"), calls
    assert out["values"][FILES[1]] == 1, out
    assert out["sources"][FILES[1]] == "deezer-isrc", out
    assert out["status"][FILES[1]] == "written", out
    assert out["answers"][FILES[1]] == {"deezer-isrc": 1}, out
    assert out["updated"] == 1, out
    assert FakeAudio.written[FILES[1]]["ITUNESADVISORY"] == "1", FakeAudio.written
    # the album tag follows the re-rated track up (any explicit → 1)
    assert out["albums"] == {ALBUM: 1}, out
    # ... while a track the re-rate could not improve keeps what it had and
    # says so (its own 0 is still Deezer's 0, so nothing was rewritten)
    assert out["status"][FILES[0]] == "unchanged", out
    assert out["sources"][FILES[0]] == "deezer-isrc", out
finally:
    mlo_audio.AudioFile = _real_audiofile


# --------------------------------------------------------------------------- #
# 6) Titles are compared accent-folded
# --------------------------------------------------------------------------- #
# The two spellings of one title must match, and a letter that folds must not
# become a separator (which is how "Motörhead" collided with "Mot rhead").
assert intg._norm_compare("Störagéd") == "storaged", intg._norm_compare("Störagéd")
assert intg.title_matches("Störagéd", "Storaged"), "accent-folded title"
assert intg.title_matches("Nüguns", "Nuguns"), "accent-folded title"
assert intg._norm_compare("Café") == "cafe", intg._norm_compare("Café")
assert intg._norm_compare("Motörhead") != intg._norm_compare("Mot rhead"), \
    "a folded letter must not read as a word boundary"
# punctuation and the non-breaking hyphen still compare equal, and the variant
# guard still separates an instrumental from the track itself
assert intg.title_matches("Suite‐Pee", "Suite-Pee")
assert intg.title_matches("Chic ’n’ Stu", "Chic N Stu")
assert not intg.title_matches("Boom! (Instrumental)", "Boom!")

# ... and the Apple song search accepts the folded spelling end to end
clear()
stub_http({"itunes.apple.com/search": {"results": [
    {"trackName": "Storaged", "artistName": "8-Bit Arcade",
     "trackExplicitness": "notExplicit"}]}})
route = intg.resolve_advisory_route(title="Störagéd", artist="8-Bit Arcade",
                                    cfg=CFG)
assert route["value"] == 0 and route["source"] == "itunes-song", route

# --------------------------------------------------------------------------- #
# 7) The ENDPOINT the wizard's re-rate control is built on: `force` reaches the
#    fetch, and `status` comes back beside the values — so a surface can ask
#    for a re-rate and tell an echoed value from one this run wrote. The wiring
#    is the whole feature here: a dropped `force` is a button that silently
#    does nothing, and a dropped `status` is "0 value(s) written" again.
# --------------------------------------------------------------------------- #
from fastapi.testclient import TestClient  # noqa: E402  (heavy import)

from server import main as mlo_main  # noqa: E402

HTTP_MUSIC = tempfile.mkdtemp(prefix="mlo_advisory_http_")
HTTP_ALBUM = os.path.join(HTTP_MUSIC, "Artists", "An Artist", "2001 - An Album")
os.makedirs(HTTP_ALBUM)
# A real (if silent) MP3 frame sequence: the route opens these with mutagen and
# writes the tag for real, so the reply is not the only evidence.
_MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413
HTTP_FILES = []
for i in (1, 2):
    path = os.path.join(HTTP_ALBUM, f"1-{i:02d} Track {i}.mp3")
    with open(path, "wb") as fh:
        fh.write(_MP3_FRAME * 40)
    HTTP_FILES.append(path)

_real_load_config = mlo_main.load_config
mlo_main.load_config = lambda *a, **k: {"music_folder": HTTP_MUSIC,
                                        "advisory_auto_fetch": True}
_client = TestClient(mlo_main.app)     # no lifespan: no workers, no boot
try:
    # nobody states anything, so the ladder's fallback writes the 0 — the very
    # run that left the screenshot's rows with a value and no source
    clear()
    stub_http({})
    reply = _client.post("/api/mb/advisory/fetch",
                         json={"paths": HTTP_FILES}).json()
    assert reply["updated"] == 2, reply
    assert set(reply["status"].values()) == {"written"}, reply
    assert set(reply["sources"].values()) == {"fallback"}, reply
    assert reply["albums"] == {HTTP_ALBUM: 0}, reply

    # the second call asks nobody (the files carry a value now) and SAYS so
    clear()
    stub_http({})
    reply = _client.post("/api/mb/advisory/fetch",
                         json={"paths": HTTP_FILES}).json()
    assert reply["updated"] == 0, reply
    assert set(reply["status"].values()) == {"existing"}, reply
    assert set(reply["sources"].values()) == {"existing-tag"}, reply
    assert set(reply["values"].values()) == {0}, reply

    # `force` reaches `fetch_advisories` and re-asks the files it echoed
    clear()
    stub_http({})
    reply = _client.post("/api/mb/advisory/fetch",
                         json={"paths": HTTP_FILES, "force": True}).json()
    assert reply["updated"] == 0, reply
    assert set(reply["status"].values()) == {"unchanged"}, reply
    assert reply["albums"] == {HTTP_ALBUM: 0}, reply
finally:
    mlo_main.load_config = _real_load_config

print("advisory pipeline: all assertions passed")
