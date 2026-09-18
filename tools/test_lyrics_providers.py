#!/usr/bin/env python3
"""Pin the multi-source lyrics chain.

The chain used to be LRCLIB or nothing, the settings list could not describe
what each provider is good at, and a provider that only had plain lyrics
could still win a "synced or nothing" run. Pinned here:

  * provider_order() honours cfg["lyrics_sources"], preserves its order,
    drops unknown ids (a hand-edited config can never wedge the run) and
    falls back to the built-in order,
  * fetch_lyrics() skips a plain-only hit — the chain is synced-only by
    default — and keeps walking until a synced source answers,
  * the NetEase and Kugou scoring picks the RIGHT candidate out of the real
    payloads they returned (cover versions with a matching title but another
    artist lose), and the synced LRC really is available as plain text,
  * every provider failing returns None instead of raising.

Payloads below are captured from the live APIs (see the module docstring of
mlo/lyrics_providers.py); no network is used.

Run:  python tools/test_lyrics_providers.py
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import lyrics_providers as lp


# --------------------------------------------------------------------------- #
# The catalogue and its settings metadata
# --------------------------------------------------------------------------- #
assert lp.SOURCES[:3] == ["lrclib", "netease", "qq"], lp.SOURCES
assert set(lp.SOURCE_LABELS) == set(lp.SOURCES) == set(lp.SOURCE_NOTES), lp.SOURCES
assert lp.SOURCE_LABELS["lrclib"] == "LRCLIB", lp.SOURCE_LABELS
assert lp.SOURCE_LABELS["netease"] == "NetEase", lp.SOURCE_LABELS
assert lp.SOURCE_LABELS["kugou"] == "Kugou", lp.SOURCE_LABELS
assert lp.SOURCE_LABELS["qq"] == "QQ Music", lp.SOURCE_LABELS

listed = lp.available_sources()
assert [s["id"] for s in listed] == lp.SOURCES, listed
assert all(set(s) == {"id", "kind", "label", "synced", "free", "needs",
                      "rank", "notes"} for s in listed), listed
assert [s["rank"] for s in listed] == [1, 2, 3, 4, 5, 6], listed
assert all(s["synced"] and s["free"] and s["needs"] == [] for s in listed), listed
assert all(s["notes"].strip() for s in listed), listed


# --------------------------------------------------------------------------- #
# provider_order: configured order, unknown ids dropped
# --------------------------------------------------------------------------- #
assert lp.provider_order() == lp.SOURCES
assert lp.provider_order({}) == lp.SOURCES
assert lp.provider_order({"lyrics_sources": []}) == lp.SOURCES
assert lp.provider_order({"lyrics_sources": "kuwo"}) == ["kuwo"]
# order is the user's, not the built-in one
assert lp.provider_order({"lyrics_sources": ["kuwo", "lrclib"]}) == \
    ["kuwo", "lrclib"], lp.provider_order({"lyrics_sources": ["kuwo", "lrclib"]})
# unknown ids (and a stale/duplicated one) are dropped, not fatal
assert lp.provider_order({"lyrics_sources": ["nope", "netease", "NETEASE",
                                             " megalobiz "]}) == ["netease"]
# nothing known at all -> the built-in order, never an empty run
assert lp.provider_order({"lyrics_sources": ["nope"]}) == lp.SOURCES
assert lp.provider_order({"lyrics_sources": None}) == lp.SOURCES


# --------------------------------------------------------------------------- #
# Captured payloads
# --------------------------------------------------------------------------- #
# https://music.163.com/api/search/get?s=Slowdive+Alison&type=1&limit=5  (200)
# result.songs[] -> id / name / duration (ms) / artists[].name / album.name
NETEASE_SEARCH = {"code": 200, "result": {"songCount": 5, "songs": [
    {"id": 4281481, "name": "Alison", "duration": 230295,
     "artists": [{"name": "Slowdive"}], "album": {"name": "Souvlaki"}},
    {"id": 19204218, "name": "Alison", "duration": 230736,
     "artists": [{"name": "Slowdive"}], "album": {"name": "Souvlaki"}},
    # same title, ANOTHER artist: a cover must never outrank the real track
    {"id": 1334891498, "name": "Alison(Slowdive Cover）", "duration": 181443,
     "artists": [{"name": "天鹅与花朵"}], "album": {"name": "阴天(Demos:2012-2015）"}},
    {"id": 2691031387, "name": "Alison (Slowdive cover)", "duration": 177160,
     "artists": [{"name": "Caleb Luther"}],
     "album": {"name": "Haphazard: Unreleased Songs & Demos"}},
]}}

# https://music.163.com/api/song/lyric?id=4281481&lv=1&kv=1&tv=-1  (200)
NETEASE_LRC = (
    "[00:00.00] 作词 : Halstead\n"
    "[00:01.00] 作曲 : Neil Halstead\n"
    "[00:19.92]Listen close and don't be stoned\n"
    "[00:27.45]I'll be here in the morning 'cause I'm just floating\n"
    "\n"
    "[00:39.50]Your cigarette still burns\n"
    "[00:46.58]Your messed-up world will thrill me\n"
    "[00:52.71]Alison, I'm lost\n"
)
NETEASE_LYRIC = {"code": 200, "sgc": False, "nolyric": False,
                 "lrc": {"version": 13, "lyric": NETEASE_LRC},
                 "tlyric": {"version": 2, "lyric": ""}}

# https://krcs.kugou.com/search?ver=1&man=yes&client=mobi&keyword=Slowdive+-+Alison
# candidates[] -> id / accesskey / singer / song / duration (ms)
KUGOU_SEARCH = {"status": 200, "candidates": [
    {"id": "139662286", "accesskey": "C842102B997DD2CA78DCE6CD81B3B5E4",
     "singer": "Slowdive", "song": "Alison", "duration": 230321},
    {"id": "139661659", "accesskey": "838ED8DEF2285113FCF0919EEA458839",
     "singer": "Slowdive", "song": "Alison", "duration": 231026},
]}
KUGOU_LRC = ("[id:$00000000]\n[ti:Alison]\n[ar:Slowdive]\n[al:Souvlaki]\n"
             "[00:21.55]Listen close and don't be stoned\n"
             "[00:26.99]I'll be here in the morning\n"
             "[00:33.10]Cause i'm just floating\n")
# ...the download endpoint answers base64 under "content" with fmt "lrc"
KUGOU_DOWNLOAD = {"status": 200, "fmt": "lrc", "charset": "utf8",
                  "content": base64.b64encode(KUGOU_LRC.encode("utf-8")).decode("ascii")}


class Patch:
    """Swap module attributes for a block and always restore them."""

    def __init__(self, module, **attrs):
        self.module, self.attrs = module, attrs

    def __enter__(self):
        self.saved = {k: getattr(self.module, k) for k in self.attrs}
        for k, v in self.attrs.items():
            setattr(self.module, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.module, k, v)
        return False


def fake_api(routes, calls=None):
    """Stand-in for the HTTP layer: maps a URL substring to a payload, and
    records every URL it was asked for."""
    def get_json(url, headers=None, timeout=15, retries=3):
        if calls is not None:
            calls.append(url)
        for needle, payload in routes:
            if needle in url:
                return payload
        return None
    return get_json


def dead_http(*a, **k):
    raise OSError("network is unreachable")


CFG = {}

# Both HTTP entry points stay stubbed for the whole run: no assertion here may
# depend on — or disturb — a live provider (a section narrows them further, and
# Patch puts these silent stubs back afterwards).
Patch(lp, _get_json=lambda *a, **k: None, _http=lambda *a, **k: None).__enter__()


# --------------------------------------------------------------------------- #
# NetEase: scoring on the real payload + synced LRC -> plain text
# --------------------------------------------------------------------------- #
calls = []
with Patch(lp, _get_json=fake_api([
        ("/search/get?", NETEASE_SEARCH),
        ("/song/lyric?id=4281481", NETEASE_LYRIC),
        ("/song/lyric", {"code": 200, "lrc": {"lyric": ""}})], calls)):
    hit = lp.fetch_lyrics(dict(CFG, lyrics_sources=["netease"]),
                          "Slowdive", "Alison", "Souvlaki", 230.295)

# the RIGHT song won: the cover versions with a matching title but another
# artist (and a 50s-short duration) were ranked below it.
assert [c for c in calls if "/song/lyric" in c] == \
    [c for c in calls if "id=4281481" in c], calls
assert hit["provider"] == "netease" and hit["provider_label"] == "NetEase", hit
assert hit["matched_title"] == "Alison" and hit["matched_artist"] == "Slowdive", hit
assert hit["matched_album"] == "Souvlaki", hit
assert abs(hit["duration"] - 230.295) < 0.01, hit["duration"]
assert hit["instrumental"] is False, hit

# synced keeps its timestamps ...
assert hit["synced"] == NETEASE_LRC.strip(), hit["synced"]
# ... and plain is the same text with every timestamp / metadata tag removed
plain = hit["plain"]
assert "[00:" not in plain and "[" not in plain, plain
assert plain.splitlines()[:3] == ["作词 : Halstead", "作曲 : Neil Halstead",
                                  "Listen close and don't be stoned"], plain
assert len(plain.splitlines()) == 7, plain          # the blank line is gone
assert "Alison, I'm lost" in plain, plain

# a track whose lyric request comes back empty is a miss, not an empty hit
with Patch(lp, _get_json=fake_api([("/search/get?", NETEASE_SEARCH)])):
    assert lp.fetch_lyrics({"lyrics_sources": ["netease"]}, "Slowdive", "Alison") is None
# ...and the encrypted-blob answer of /api/search/get/web is just a miss
with Patch(lp, _get_json=fake_api([("/search/get", {"code": 200, "abroad": True,
                                                    "result": "35b17489deadbeef"})])):
    assert lp.fetch_lyrics({"lyrics_sources": ["netease"]}, "Slowdive", "Alison") is None


# --------------------------------------------------------------------------- #
# Kugou: the same scoring, over its own candidate shape
# --------------------------------------------------------------------------- #
kugou_calls = []
with Patch(lp, _get_json=fake_api([
        ("krcs.kugou.com/search", KUGOU_SEARCH),
        ("lyrics.kugou.com/download", KUGOU_DOWNLOAD)], kugou_calls)):
    hit = lp.fetch_lyrics({"lyrics_sources": ["kugou"]},
                          "Slowdive", "Alison", None, 230.295)
# the first candidate (closest duration) is the one whose lyrics were fetched
assert "id=139662286" in [c for c in kugou_calls if "download" in c][0], kugou_calls
assert hit["provider"] == "kugou" and hit["matched_artist"] == "Slowdive", hit
assert hit["synced"] == KUGOU_LRC.strip(), hit["synced"]
assert hit["plain"].splitlines() == [
    "Listen close and don't be stoned", "I'll be here in the morning",
    "Cause i'm just floating"], hit["plain"]


# --------------------------------------------------------------------------- #
# A wrong-artist / wrong-duration hit scores below the floor
# --------------------------------------------------------------------------- #
assert lp._match_score(["Somebody Else"], "Alison", 230.0, "Slowdive", "Alison", 230.0) < \
    lp._MIN_SCORE, "a namesake artist must not clear the floor"
assert lp._match_score(["Slowdive"], "Alison", 177.0, "Slowdive", "Alison", 230.0) == 0.0, \
    "a 53s duration difference must kill the hit outright"
assert lp._match_score(["Slowdive"], "Alison", 230.3, "Slowdive", "Alison", 230.0) > 1.0, \
    "an exact title+artist with a matching duration must score highest"
assert lp._match_score(["Slowdive"], "Alison (Remastered)", 0, "Slowdive", "Alison", 0) > \
    lp._MIN_SCORE, "a decorated title must still match"


# --------------------------------------------------------------------------- #
# allow_plain: a plain-only hit is skipped and the chain keeps walking
# --------------------------------------------------------------------------- #
PLAIN_ONLY = "listen close and dont be stoned\ni'll be here in the morning\n"
SYNCED = "[00:19.92]listen close and don't be stoned\n"

# LRCLIB has the song with untimed text only (its /get record carries
# plainLyrics alone)
LRCLIB_PLAIN_ONLY = ("lrclib.net/api/get", {"syncedLyrics": None,
                                            "plainLyrics": PLAIN_ONLY,
                                            "instrumental": False,
                                            "duration": 230.0})
with Patch(lp, _get_json=fake_api([LRCLIB_PLAIN_ONLY])):
    # synced-only is the default: refused, and never replaced by an invention
    hit = lp.fetch_lyrics(CFG, "Slowdive", "Alison")
    assert hit is None, hit
    assert lp.fetch_lyrics(dict(CFG, lyrics_allow_plain=False),
                           "Slowdive", "Alison") is None
    # the opt-in lets it through, and the param beats the config both ways
    hit = lp.fetch_lyrics(dict(CFG, lyrics_allow_plain=True), "Slowdive", "Alison")
    assert hit["provider"] == "lrclib" and hit["synced"] is None, hit
    assert hit["plain"] == PLAIN_ONLY.strip(), hit["plain"]
    assert lp.fetch_lyrics(CFG, "Slowdive", "Alison",
                           allow_plain=True)["provider"] == "lrclib"
    assert lp.fetch_lyrics(dict(CFG, lyrics_allow_plain=True), "Slowdive", "Alison",
                           allow_plain=False) is None

# ...and the chain FALLS THROUGH to the next provider instead of stopping
with Patch(lp, _get_json=fake_api([
        LRCLIB_PLAIN_ONLY,
        ("lrclib.net/api/search", {"syncedLyrics": SYNCED, "plainLyrics": None,
                                   "instrumental": False, "duration": 230.0}),
        ("krcs.kugou.com/search", KUGOU_SEARCH),
        ("lyrics.kugou.com/download", KUGOU_DOWNLOAD)])):
    order = {"lyrics_sources": ["lrclib", "kugou"], "lyrics_allow_plain": True}
    assert lp.fetch_lyrics(order, "Slowdive", "Alison")["provider"] == "lrclib"
    fell = lp.fetch_lyrics(dict(order, lyrics_allow_plain=False),
                           "Slowdive", "Alison")
    assert fell["provider"] == "kugou" and fell["synced"] == KUGOU_LRC.strip(), fell


# --------------------------------------------------------------------------- #
# Every provider failing is None, never an exception
# --------------------------------------------------------------------------- #
with Patch(lp, _http=dead_http):
    assert lp.fetch_lyrics(CFG, "Slowdive", "Alison") is None
with Patch(lp, _get_json=lambda *a, **k: None):
    assert lp.fetch_lyrics(CFG, "Slowdive", "Alison") is None
# garbage where JSON was promised, and a provider that itself blows up
with Patch(lp, _http=lambda *a, **k: b"<html>rate limited</html>"):
    assert lp.fetch_lyrics(CFG, "Slowdive", "Alison") is None
with Patch(lp, _get_json=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))):
    assert lp.fetch_lyrics(CFG, "Slowdive", "Alison") is None
# no artist/title: nothing is even asked for
asked = []
with Patch(lp, _get_json=fake_api([], asked)):
    assert lp.fetch_lyrics(CFG, "", "Alison") is None
    assert lp.fetch_lyrics(CFG, "Slowdive", None) is None
    assert asked == [], asked


# --------------------------------------------------------------------------- #
# The shared hit shape (every provider returns all of these)
# --------------------------------------------------------------------------- #
with Patch(lp, _get_json=fake_api([LRCLIB_PLAIN_ONLY])):
    hit = lp.fetch_lyrics(dict(CFG, lyrics_allow_plain=True),
                          "Slowdive", "Alison", "Souvlaki", 230.295)
assert set(hit) == {"provider", "provider_label", "synced", "plain", "instrumental",
                    "duration", "matched_artist", "matched_title", "matched_album",
                    "score"}, hit
# the score is the match confidence the caller gates automatic writes on
assert 0.0 <= hit["score"] <= 1.2, hit["score"]
assert isinstance(hit["instrumental"], bool), hit
json.dumps(hit)  # the run report serializes a hit as-is


# --------------------------------------------------------------------------- #
# The runner asks the chain (config order) and counts the winner per source
# --------------------------------------------------------------------------- #
from mlo import lyrics_fetch  # noqa: E402

assert lyrics_fetch.lrclib_fetch is lp.lrclib_fetch, "the LRCLIB shim was dropped"
assert lyrics_fetch.fetch_lyrics is lp.fetch_lyrics, "the runner lost the chain"

stats = lyrics_fetch.run_fetch_lyrics({"music_folder": "", "targets": []})
assert stats["by_provider"] == {}, stats["by_provider"]   # nothing was fetched

print("ok")
