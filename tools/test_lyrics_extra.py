#!/usr/bin/env python3
"""The extra synced lyrics providers (QQ Music, Kuwo, YouTube captions) and the
one gate every hit passes.

Offline: both HTTP entry points (`_get_json` for the JSON APIs, `_http` for
Kuwo's single-quoted bodies) and the yt-dlp call are stubbed, so nothing here
touches the network. The variant marker list is the shipped one out of
`server.integrations` — the guard has to work against the real thing.

Pinned here:

  * every provider in the catalogue is synced, free and needs no config key,
  * an untimed answer is rejected outright — `_accept`, the single gate — unless
    `lyrics_allow_plain` is explicitly on (off by default in config),
  * QQ's and Kuwo's real payload shapes: the right candidate wins, a namesake
    or a karaoke row loses, a translation is never written over the original,
  * a provider that raises, an empty answer and a dead host are all just misses,
  * YouTube needs a KNOWN video id (never a search), turns captions into LRC,
    and is a skip — not a failure — when yt-dlp is missing.

Run:  python tools/test_lyrics_extra.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import config as mlo_config  # noqa: E402
from mlo import lyrics_providers as lp  # noqa: E402


# --------------------------------------------------------------------------- #
# Captured payloads (live shapes, trimmed)
# --------------------------------------------------------------------------- #
# c.y.qq.com/soso/fcgi-bin/client_search_cp?w=Radiohead+Creep&new_json=1 → real
QQ_SEARCH = {"code": 0, "data": {"song": {"curnum": 5, "curpage": 1, "list": [
    {"mid": "000ybVWz3FP6fu", "name": "Creep", "interval": 238,
     "singer": [{"name": "Radiohead"}], "album": {"name": "Creep (Explicit)"}},
    {"mid": "003cafuF1ZVf1p", "name": "Creep", "interval": 260,
     "singer": [{"name": "Radiohead"}], "album": {"name": "Guardians Vol. 3"}},
    {"mid": "000zdigp4fe3Ti", "name": "Creep", "interval": 174,
     "singer": [{"name": "GAMPER & DADONI"}], "album": {"name": "Creep"}},
]}}}

QQ_LRC = ("[ti:Creep]\n[ar:Radiohead]\n[al:Pablo Honey (Collector's Edition)]\n"
          "[by:]\n[offset:0]\n"
          "[00:00.00]Creep (Explicit) - Radiohead\n"
          "[00:05.06]Lyrics by：Thom Yorke\n"
          "[00:20.24]When you were here before\n"
          "[00:25.04]Couldn't look you in the eye\n"
          "[00:30.36]You're just like an angel\n")
QQ_LYRIC = {"retcode": 0, "code": 0, "subcode": 0, "lyric": QQ_LRC,
            "trans": "[00:20.24]以前你在这里的时候\n[00:25.04]我无法直视你的双眼\n"}

# search.kuwo.cn/r.s?all=Radiohead+Creep...&ver=kwplayer_ar_9.2.2.1 → real
# (single-quoted JSON, HTML-escaped values, DURATION in seconds)
KUWO_SEARCH = (
    "{'ARTISTPIC':'','HIT':'1','abslist':["
    "{'MUSICRID':'MUSIC_16996995','SONGNAME':'Creep&nbsp;(Explicit)',"
    "'ARTIST':'Radiohead','DURATION':'238','ALBUM':'Creep&nbsp;(Explicit)'},"
    "{'MUSICRID':'MUSIC_274477991','SONGNAME':'Creep&nbsp;(Acoustic)',"
    "'ARTIST':'Radiohead','DURATION':'260','ALBUM':'Guardians&nbsp;Vol.&nbsp;3'},"
    "{'MUSICRID':'MUSIC_1250107','SONGNAME':'Creep&nbsp;(Explicit)',"
    "'ARTIST':'Radiohead','DURATION':'238',"
    "'ALBUM':'Pablo&nbsp;Honey&nbsp;(Explicit)'},"
    "{'MUSICRID':'MUSIC_260471467','SONGNAME':'Creep&nbsp;(cover:&nbsp;Radiohead)',"
    "'ARTIST':'康健音乐','DURATION':'240','ALBUM':''}"
    "],'SHOW':'4'}"
)

# m.kuwo.cn/newh5/singles/songinfoandlrc?musicId=1250107 → real cue rows: the
# translation repeats its line's timestamp, and Kuwo pads cues with "   "
KUWO_LRC = {"data": {"lrclist": [
    {"time": "0.0", "lineLyric": "Creep (Explicit) - Radiohead"},
    {"time": "6.74", "lineLyric": "   "},
    {"time": "6.74", "lineLyric": "Lyrics by：Thom Yorke"},
    {"time": "20.24", "lineLyric": "   "},
    {"time": "20.24", "lineLyric": "When you were here before"},
    {"time": "25.04", "lineLyric": "以前你在这里的时候"},
    {"time": "25.04", "lineLyric": "Couldn't look you in the eye"},
    {"time": "30.36", "lineLyric": "你就像一个天使"},
    {"time": "30.36", "lineLyric": "You're just like an angel"},
]}}

PLAIN = ("Creep (Explicit) - Radiohead\nLyrics by：Thom Yorke\n"
         "When you were here before\nCouldn't look you in the eye\n"
         "You're just like an angel")

CFG = {}


class Patch:
    """Swap module attributes for a block and always restore them."""

    def __init__(self, obj, **attrs):
        self.obj, self.attrs, self.old = obj, attrs, {}

    def __enter__(self):
        self.old = {k: getattr(self.obj, k) for k in self.attrs}
        for k, v in self.attrs.items():
            setattr(self.obj, k, v)
        return self.obj

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(self.obj, k, v)
        return False


def fake_api(routes, calls=None):
    """Stand-in for `_get_json`: URL substring -> payload, recording calls."""
    def get_json(url, headers=None, **kw):
        if calls is not None:
            calls.append(url)
        for needle, payload in routes:
            if needle in url:
                return payload
        return None
    return get_json


def fake_http(routes, calls=None):
    """Stand-in for `_http`: URL substring -> raw body bytes."""
    def http(url, headers=None, data=None, **kw):
        if calls is not None:
            calls.append(url)
        for needle, payload in routes:
            if needle in url:
                return payload.encode("utf-8") if isinstance(payload, str) else payload
        return None
    return http


def dead_http(*a, **k):
    raise OSError("network is unreachable")


def collect_log():
    """Capture `mlo.ui.log` (the one-line skip notices) into a list."""
    lines = []

    def log(message, *a, **k):
        lines.append(str(message))

    return lines, log


# Nothing in this file may touch the network: both HTTP entry points are
# stubbed for the whole run, and every section below narrows them further
# (Patch restores these silent stubs when a section ends).
_OFFLINE = Patch(lp, _get_json=lambda *a, **k: None, _http=lambda *a, **k: None)
_OFFLINE.__enter__()


# --------------------------------------------------------------------------- #
# The catalogue is synced-only, free, and describes itself
# --------------------------------------------------------------------------- #
assert lp.SOURCES == ["lrclib", "netease", "qq", "kuwo", "kugou", "youtube"], lp.SOURCES
assert set(lp.SOURCE_LABELS) == set(lp.SOURCE_NOTES) == set(lp._PROVIDERS), lp.SOURCES
# the plain-only providers are gone, not dormant
for gone in ("lyricsovh", "genius", "musixmatch"):
    assert gone not in lp._PROVIDERS and gone not in lp.SOURCES, gone
    assert gone not in lp.SOURCE_LABELS and gone not in lp.SOURCE_NOTES, gone
assert not [k for k in mlo_config.DEFAULT_CONFIG
            if k in ("genius_token", "musixmatch_api_key")], "a dead key survived"

listed = lp.available_sources()
assert [s["id"] for s in listed] == lp.SOURCES, listed
for entry in listed:
    assert set(entry) == {"id", "kind", "label", "synced", "free", "needs",
                          "rank", "notes"}, entry
    assert entry["kind"] == "lyrics", entry
    # every source answers with timestamps, is free, and needs no key
    assert entry["synced"] is True and entry["free"] is True, entry
    assert entry["needs"] == [], entry
    assert "synced" in entry["notes"].lower(), entry
    assert entry["label"].strip() and entry["notes"].strip(), entry
assert "translations" in lp.SOURCE_NOTES["netease"], lp.SOURCE_NOTES["netease"]
assert "captions" in lp.SOURCE_NOTES["youtube"], lp.SOURCE_NOTES["youtube"]

# the config the providers read agrees with that
assert mlo_config.DEFAULT_CONFIG["lyrics_allow_plain"] is False, \
    mlo_config.DEFAULT_CONFIG["lyrics_allow_plain"]
assert mlo_config.DEFAULT_CONFIG["lyrics_youtube_captions"] is True, \
    mlo_config.DEFAULT_CONFIG["lyrics_youtube_captions"]


# --------------------------------------------------------------------------- #
# `_accept`: the one gate — synced, or nothing
# --------------------------------------------------------------------------- #
assert lp._accept({"synced": "[00:01.00]x"}, False) is True
assert lp._accept({"synced": "[00:01.00]x", "plain": "x"}, False) is True
assert lp._accept({"plain": "x", "synced": None}, False) is False
assert lp._accept({"plain": "x", "synced": None}, True) is True
assert lp._accept({"synced": "  ", "plain": None}, True) is False
assert lp._accept(None, True) is False
assert lp._accept({}, True) is False
# no config at all means the strict default, and the parameter wins both ways
with Patch(lp, _get_json=fake_api([("lrclib.net/api/get", {
        "syncedLyrics": None, "plainLyrics": "untimed text\n",
        "instrumental": False, "duration": 238.0})])):
    assert lp.fetch_lyrics(CFG, "Radiohead", "Creep") is None
    assert lp.fetch_lyrics(CFG, "Radiohead", "Creep", allow_plain=True)["plain"] == \
        "untimed text"
    assert lp.fetch_lyrics({"lyrics_allow_plain": True}, "Radiohead", "Creep",
                           allow_plain=False) is None


# --------------------------------------------------------------------------- #
# QQ Music: real ranking payload, synced LRC, translations unused
# --------------------------------------------------------------------------- #
calls = []
with Patch(lp, _get_json=fake_api([("client_search_cp", QQ_SEARCH),
                                   ("fcg_query_lyric_new.fcg", QQ_LYRIC)], calls)):
    hit = lp.fetch_lyrics(dict(CFG, lyrics_sources=["qq"]),
                          "Radiohead", "Creep", "Pablo Honey", 238.0)

assert hit["provider"] == "qq" and hit["provider_label"] == "QQ Music", hit
assert hit["matched_title"] == "Creep" and hit["matched_artist"] == "Radiohead", hit
assert hit["matched_album"] == "Creep (Explicit)", hit
assert abs(hit["duration"] - 238.0) < 0.01, hit
assert hit["synced"] == QQ_LRC.strip(), hit["synced"]
assert hit["instrumental"] is False, hit
# the lyric request went to the RIGHT song, and no other one was asked for
asked = [c for c in calls if "lyric" in c]
assert len(asked) == 1 and "songmid=000ybVWz3FP6fu" in asked[0], asked
# the translation is not part of the answer, in either view
assert "以前你在这里的时候" not in hit["plain"], hit["plain"]
assert hit["plain"].splitlines() == PLAIN.splitlines(), hit["plain"]
assert "[ti:" not in hit["plain"] and "[00:" not in hit["plain"], hit["plain"]

# an answered search with no lyric text is a miss, never an empty hit
with Patch(lp, _get_json=fake_api([("client_search_cp", QQ_SEARCH),
                                   ("lyric", {"retcode": 0, "lyric": ""})])):
    assert lp.fetch_lyrics(dict(CFG, lyrics_sources=["qq"]),
                           "Radiohead", "Creep") is None
# a namesake artist never clears the floor, so no lyric request is spent on it
QQ_NAMESAKE = {"code": 0, "data": {"song": {"list": [
    {"mid": "x", "name": "Creep", "interval": 238,
     "singer": [{"name": "GAMPER & DADONI"}], "album": {"name": "C"}}]}}}
qq_calls = []
with Patch(lp, _get_json=fake_api([("client_search_cp", QQ_NAMESAKE),
                                   ("lyric", QQ_LYRIC)], qq_calls)):
    assert lp.fetch_lyrics(dict(CFG, lyrics_sources=["qq"]),
                           "Radiohead", "Creep", None, 238.0) is None
assert not [c for c in qq_calls if "lyric" in c], qq_calls


# --------------------------------------------------------------------------- #
# Kuwo: single-quoted search JSON, originals win their timestamp
# --------------------------------------------------------------------------- #
assert lp._kuwo_text("Pablo&nbsp;Honey&nbsp;(Explicit)") == "Pablo Honey (Explicit)"
# a translation repeats its line's timestamp: one LRC line per timestamp, the
# original — never the translation, and the whitespace-only pads are dropped
assert lp._kuwo_lrc(KUWO_LRC["data"]["lrclist"]).splitlines() == [
    "[00:00.00]Creep (Explicit) - Radiohead",
    "[00:06.74]Lyrics by：Thom Yorke",
    "[00:20.24]When you were here before",
    "[00:25.04]Couldn't look you in the eye",
    "[00:30.36]You're just like an angel",
], lp._kuwo_lrc(KUWO_LRC["data"]["lrclist"])
assert lp._kuwo_lrc([]) == "" and lp._kuwo_lrc(None) == ""
assert lp._kuwo_lrc([{"time": "x", "lineLyric": "junk"}, None]) == ""

kuwo_calls = []
with Patch(lp, _http=fake_http([("search.kuwo.cn", KUWO_SEARCH),
                                ("songinfoandlrc?musicId=1250107", json.dumps(KUWO_LRC))],
                               kuwo_calls)):
    hit = lp.fetch_lyrics(dict(CFG, lyrics_sources=["kuwo"]),
                          "Radiohead", "Creep", "Pablo Honey", 238.0)
assert hit["provider"] == "kuwo" and hit["provider_label"] == "Kuwo", hit
# the provider's own title is reported as-is (it is what Kuwo calls the track)
assert hit["matched_artist"] == "Radiohead", hit
assert hit["matched_title"] == "Creep (Explicit)", hit
assert hit["matched_album"] == "Pablo Honey (Explicit)", hit
assert abs(hit["duration"] - 238.0) < 0.01, hit
assert "以前你在这里的时候" not in hit["synced"], hit["synced"]
# a real apostrophe in the body is not mangled by the single-quote repair
assert "Couldn't look you in the eye" in hit["synced"], hit["synced"]
# the ids without lyrics were tried first (they rank level with the good one),
# and the cover row was never asked for at all
assert "musicId=1250107" in [c for c in kuwo_calls if "songinfoandlrc" in c][-1], kuwo_calls
assert not [c for c in kuwo_calls if "260471467" in c], kuwo_calls

# an id the lyric module refuses (音乐查询失败) is a miss, and the chain walks on
with Patch(lp, _http=fake_http([("search.kuwo.cn", KUWO_SEARCH),
                                ("songinfoandlrc", '{"data":null,"msg":"音乐查询失败"}')])):
    assert lp.fetch_lyrics(dict(CFG, lyrics_sources=["kuwo"]),
                           "Radiohead", "Creep") is None
# garbage where JSON was promised is a miss too, never an exception
with Patch(lp, _http=lambda *a, **k: b"<html>blocked</html>"):
    assert lp.fetch_lyrics(dict(CFG, lyrics_sources=["kuwo"]),
                           "Radiohead", "Creep") is None


# --------------------------------------------------------------------------- #
# A variant is another recording: rejected, both directions
# --------------------------------------------------------------------------- #
assert lp._variant_guard("Creep", "Creep") is True
assert lp._variant_guard("Creep", "Creep (Instrumental)") is False
assert lp._variant_guard("Creep (Karaoke Version)", "Creep") is False
assert lp._variant_guard("Creep (Karaoke Version)", "Creep (Karaoke Version)") is True
assert lp._variant_guard("Creep", "Creep (Remastered 2011)") is True
assert lp._variant_guard("Creep", "Creep (In the Style of Radiohead)") is False

QQ_KARAOKE_ONLY = {"code": 0, "data": {"song": {"list": [
    {"mid": "k", "name": "Creep (Karaoke Version)", "interval": 238,
     "singer": [{"name": "Radiohead"}], "album": {"name": "K"}}]}}}
with Patch(lp, _get_json=fake_api([("client_search_cp", QQ_KARAOKE_ONLY),
                                   ("lyric", QQ_LYRIC)])):
    assert lp.fetch_lyrics(dict(CFG, lyrics_sources=["qq"]),
                           "Radiohead", "Creep") is None
with Patch(lp, _get_json=fake_api([("client_search_cp", QQ_SEARCH),
                                   ("lyric", QQ_LYRIC)])):
    assert lp.fetch_lyrics(dict(CFG, lyrics_sources=["qq"]),
                           "Radiohead", "Creep (Karaoke Version)") is None


# --------------------------------------------------------------------------- #
# The configured order decides, and a dead provider never stops the chain
# --------------------------------------------------------------------------- #
with Patch(lp, _http=fake_http([("search.kuwo.cn", KUWO_SEARCH),
                                ("songinfoandlrc", json.dumps(KUWO_LRC))])):
    first = lp.fetch_lyrics({"lyrics_sources": ["kuwo", "qq"]},
                            "Radiohead", "Creep")
assert first["provider"] == "kuwo", first

with Patch(lp, _get_json=fake_api([("client_search_cp", QQ_SEARCH),
                                   ("lyric", QQ_LYRIC)])):
    first = lp.fetch_lyrics({"lyrics_sources": ["kuwo", "qq"]},
                            "Radiohead", "Creep")
assert first["provider"] == "qq", first                 # kuwo could not run


def boom(*a, **k):
    raise RuntimeError("provider exploded")


for broken in ("qq", "kuwo", "youtube"):
    providers = dict(lp._PROVIDERS, **{broken: boom})
    with Patch(lp, _PROVIDERS=providers,
               _get_json=fake_api([("client_search_cp", QQ_SEARCH),
                                   ("lyric", QQ_LYRIC)]),
               _http=fake_http([("search.kuwo.cn", KUWO_SEARCH),
                                ("songinfoandlrc?musicId=1250107",
                                 json.dumps(KUWO_LRC))])):
        hit = lp.fetch_lyrics({"lyrics_sources": [broken, "qq", "kuwo"]},
                              "Radiohead", "Creep")
    assert hit and hit["provider"] != broken, (broken, hit)
    assert hit["synced"], hit


# --------------------------------------------------------------------------- #
# YouTube captions: a known id only, converted to LRC
# --------------------------------------------------------------------------- #
assert lp.youtube_id_from("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
assert lp.youtube_id_from("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s") == "dQw4w9WgXcQ"
assert lp.youtube_id_from("https://youtu.be/dQw4w9WgXcQ?si=x") == "dQw4w9WgXcQ"
assert lp.youtube_id_from("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
assert lp.youtube_id_from("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
assert lp.youtube_id_from("", None) is None
assert lp.youtube_id_from("https://bandcamp.com/track/creep") is None
assert lp.youtube_id_from("too-short") is None
assert lp.youtube_id_from("https://youtube.com/", "dQw4w9WgXcQ") == "dQw4w9WgXcQ"
# the app's own video download template leaves the id in the file name
assert lp.youtube_id_from("Creep [dQw4w9WgXcQ].mkv") == "dQw4w9WgXcQ"

# a caption track becomes the same LRC shape the other synced providers answer
VTT = (
    "WEBVTT\nKind: captions\nLanguage: en\n\n"
    "00:00:19.920 --> 00:00:23.150 align:start position:0%\n"
    "When you were here before\n\n"
    "00:00:23.150 --> 00:00:26.640 align:start position:0%\n"
    "Couldn't look you in the eye\n\n"
    "00:00:26.640 --> 00:00:29.120\n"
    "You're just like an angel\n"
)
assert lp._vtt_to_lrc(VTT) == (
    "[00:19.92]When you were here before\n"
    "[00:23.15]Couldn't look you in the eye\n"
    "[00:26.64]You're just like an angel"
), lp._vtt_to_lrc(VTT)
# automatic captions: inline word tags dropped, the rolling repeat kept once,
# SRT comma timestamps accepted, and the header before the first cue ignored
SRT = (
    "NOTE this is not a cue\n\n"
    "1\n"
    "00:00:19,920 --> 00:00:23,150\n"
    "When you were here before\n\n"
    "2\n"
    "00:00:23,150 --> 00:00:26,640\n"
    "When you were here before\nCouldn't look you in the eye\n\n"
    "3\n"
    "00:00:26,640 --> 00:00:29,120\n"
    "<00:00:26.640><c>You're</c><00:00:27.100><c>just</c> like an angel\n"
)
assert lp._vtt_to_lrc(SRT) == (
    "[00:19.92]When you were here before\n"
    "[00:23.15]When you were here before Couldn't look you in the eye\n"
    "[00:26.64]You're just like an angel"
), lp._vtt_to_lrc(SRT)
assert lp._vtt_to_lrc("WEBVTT\n\n") == ""
assert lp._vtt_to_lrc("") == ""


def stub_captions(files):
    """Stand-in for `_caption_files`: writes fixtures, as yt-dlp would."""
    def write(exe, video_id, tmpdir, flags):
        made = []
        for name, body in files:
            path = os.path.join(tmpdir, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            made.append(path)
        return made
    return write


with Patch(lp, _ytdlp_exe=lambda: "yt-dlp",
           _caption_files=stub_captions([("dQw4w9WgXcQ.en.vtt", VTT)])):
    hit = lp.fetch_lyrics(dict(CFG, lyrics_sources=["youtube"]),
                          "Radiohead", "Creep", "Pablo Honey", 238.0,
                          youtube_id="dQw4w9WgXcQ")
assert hit["provider"] == "youtube", hit
assert hit["provider_label"] == "YouTube captions", hit
assert hit["synced"] == lp._vtt_to_lrc(VTT), hit["synced"]
assert hit["plain"] == ("When you were here before\n"
                        "Couldn't look you in the eye\n"
                        "You're just like an angel"), hit["plain"]
assert hit["matched_title"] == "Creep" and hit["instrumental"] is False, hit


def never(*a, **k):
    raise AssertionError("a captions fetch happened without a video id")


# without an id there is nothing to ask for — never a YouTube search
with Patch(lp, _ytdlp_exe=lambda: "yt-dlp", _caption_files=never):
    assert lp.fetch_lyrics(dict(CFG, lyrics_sources=["youtube"]),
                           "Radiohead", "Creep") is None
    denied = dict(CFG, lyrics_sources=["youtube"], lyrics_youtube_captions=False)
    assert lp.fetch_lyrics(denied, "Radiohead", "Creep",
                           youtube_id="dQw4w9WgXcQ") is None

# yt-dlp is optional: missing means one log line, and the chain keeps working
lp._LOGGED.clear()
lines, log = collect_log()
with Patch(lp, _ytdlp_exe=lambda: None, _caption_files=never,
           _get_json=fake_api([("client_search_cp", QQ_SEARCH),
                               ("lyric", QQ_LYRIC)])):
    with Patch(sys.modules["mlo.ui"], log=log):
        hit = lp.fetch_lyrics(dict(CFG, lyrics_sources=["youtube", "qq"]),
                              "Radiohead", "Creep", youtube_id="dQw4w9WgXcQ")
        assert lp.fetch_lyrics(dict(CFG, lyrics_sources=["youtube"]),
                               "Radiohead", "Creep",
                               youtube_id="dQw4w9WgXcQ") is None
assert hit and hit["provider"] == "qq", hit
assert len(lines) == 1 and "yt-dlp" in lines[0], lines


# --------------------------------------------------------------------------- #
# probe_source: one cheap lookup, honest status, never an exception
# --------------------------------------------------------------------------- #
ok = lp.probe_source("qq", CFG)
assert set(ok) == {"id", "kind", "status", "detail", "ms"}, ok
assert ok["id"] == "qq" and ok["kind"] == "lyrics", ok
assert isinstance(ok["ms"], int) and ok["ms"] >= 0, ok

with Patch(lp, _get_json=fake_api([("client_search_cp", QQ_SEARCH),
                                   ("lyric", QQ_LYRIC)])):
    probe = lp.probe_source("qq", CFG)
assert probe["status"] == "ok" and "lines" in probe["detail"], probe

with Patch(lp, _get_json=fake_api([("client_search_cp", QQ_SEARCH)])):
    probe = lp.probe_source("qq", CFG)
assert probe["status"] == "fail" and probe["detail"], probe

with Patch(lp, _get_json=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))):
    probe = lp.probe_source("qq", CFG)
assert probe["status"] == "fail" and "raised" in probe["detail"], probe

# the sample is the same one for every provider, so two runs compare
assert lp.PROBE_SAMPLE == ("Radiohead", "Creep", "Pablo Honey", 238.0), lp.PROBE_SAMPLE
# youtube can never be probed with the sample: it needs a track's own video id
probe = lp.probe_source("youtube", CFG)
assert probe["status"] == "skipped" and probe["id"] == "youtube", probe
with Patch(lp, _ytdlp_exe=lambda: None):
    probe = lp.probe_source("youtube", CFG)
assert probe["status"] == "skipped" and "yt-dlp" in probe["detail"], probe
assert lp.probe_source("megalobiz", CFG)["status"] == "skipped"

# probe_source asks the provider with the sample and reports what came back —
# it never passes a video id, and never writes anything
seen = {}


def spy(artist, title, album=None, duration=None, cfg=None, youtube_id=None):
    seen.update(artist=artist, title=title, album=album, duration=duration,
                youtube_id=youtube_id)
    return {"synced": "[00:01.00]x\n[00:02.00]y", "plain": "x"}


with Patch(lp, _PROVIDERS=dict(lp._PROVIDERS, qq=spy)):
    probe = lp.probe_source("qq", CFG)
assert seen == {"artist": "Radiohead", "title": "Creep",
                "album": "Pablo Honey", "duration": 238.0,
                "youtube_id": None}, seen
assert probe["status"] == "ok" and probe["detail"] == "synced lyrics, 2 lines", probe

print("ok")
