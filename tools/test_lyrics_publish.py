#!/usr/bin/env python3
"""Script 18 — publishing lyrics to LRCLIB (`mlo.lyrics_publish`).

Offline: `lrclib_fetch` / `lrclib_publish` are stubbed (and the HTTP seam under
`lrclib_publish` itself is stubbed for the request-shape checks), so nothing
here touches the network. The audio fixtures are real FLACs, encoded with the
vendored flac.exe; the suite skips itself when that tool is missing.

Pinned here:

  * a track LRCLIB already answers for is NEVER published — the rule the
    script cannot override except through `force_publish`;
  * the submission carries the plain text BESIDE the synced one (LRCLIB's
    validator rejects a synced-only body), timestamps and `[ar:…]` headers
    stripped;
  * what is skipped is skipped for its own reason (no lyrics, instrumental,
    no duration, missing tags) and counted apart from what failed;
  * a 409 duplicate is a SKIP — the database's own answer, not an error —
    while a refused submission is a failure;
  * `lrclib_auto_publish` off means the script never runs at all.

Run:  python tools/test_lyrics_publish.py
"""
import os
import shutil
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import lyrics_providers as lp  # noqa: E402
from mlo import lyrics_publish as pub  # noqa: E402
from mlo.audio import AudioFile  # noqa: E402
from server import script_runners  # noqa: E402

FAILS = []


def ok(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def find_flac():
    for root, _dirs, files in os.walk(os.path.join(ROOT, ".dependencies")):
        for f in files:
            if f.lower() == "flac.exe":
                return os.path.join(root, f)
    return shutil.which("flac")


print("== to_plain ─────────────────────────────────────────────────────")
ok(pub.to_plain("[00:01.00]<00:01.20>Kimi <00:01.60>no na\n[00:03.50]\n") == "Kimi no na",
   "timestamps and word tags are stripped, blank lines dropped")
ok(pub.to_plain("[ti:Song]\n[ar:Band]\nHello\n") == "Hello",
   "LRC metadata headers are not lyrics")

print("== the submission body ─────────────────────────────────────────")
sent = {}


def fake_request(url, headers=None, data=None, timeout=15, retries=3):
    sent.update(url=url, headers=headers or {}, data=data)
    return sent.pop("status", 201), sent.pop("body", b"")


real_request = lp._request
lp._request = fake_request
try:
    okr, msg = lp.lrclib_publish("Artist", "Song", "Album", 213,
                                 plain="Hello\nthere", synced="[00:01.00]Hello\n[00:03.00]there")
    ok(okr and "published" in msg, f"a 201 is a published submission ({msg})")
    ok("artist_name=Artist" in sent["url"] and "track_name=Song" in sent["url"]
       and "album_name=Album" in sent["url"] and "duration=213" in sent["url"],
       f"the query carries artist/track/album/duration ({sent['url'].split('?')[1]})")
    import json
    body = json.loads(sent["data"].decode("utf-8"))
    ok(body == {"plainLyrics": "Hello\nthere", "syncedLyrics": "[00:01.00]Hello\n[00:03.00]there"},
       f"both lyric forms travel in the JSON body ({body})")
    ok(sent["headers"].get("Content-Type") == "application/json"
       and sent["headers"].get("User-Agent"),
       "the request carries its type and the descriptive User-Agent LRCLIB requires")

    lp._request = lambda *a, **k: (409, b"track already exists")
    okr, msg = lp.lrclib_publish("Artist", "Song", "Album", 213, plain="Hello")
    ok(not okr and "already has this track" in msg, f"a 409 is reported as a duplicate ({msg})")

    lp._request = lambda *a, **k: (429, b"")
    okr, msg = lp.lrclib_publish("Artist", "Song", "Album", 213, plain="Hello")
    ok(not okr and "rate-limit" in msg, f"a 429 names the rate limit ({msg})")

    okr, msg = lp.lrclib_publish("Artist", "Song", "Album", 0, plain="Hello")
    ok(not okr and "duration" in msg, f"a missing duration is refused before any request ({msg})")
    okr, msg = lp.lrclib_publish("Artist", "Song", "Album", 213)
    ok(not okr and "nothing to publish" in msg, f"an empty submission is refused ({msg})")
finally:
    lp._request = real_request

print("== the per-track rule, on real files ───────────────────────────")
FLAC = find_flac()
if not FLAC:
    print("  skipped: no flac.exe (set up .dependencies or install flac to run this part)")
else:
    TMP = tempfile.mkdtemp(prefix="mlo-publish-test-")
    wav = os.path.join(TMP, "tone.wav")
    with wave.open(wav, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 44100)

    def make(name, *, lyrics="[00:01.00]Hello there", instrumental=False):
        path = os.path.join(TMP, name)
        import subprocess
        subprocess.run([FLAC, "-f", "-s", "-o", path, wav], check=True, capture_output=True)
        af = AudioFile(path)
        af.set_tag("TITLE", "Song"); af.set_tag("ARTIST", "Artist"); af.set_tag("ALBUM", "Album")
        if lyrics:
            af.set_tag("LYRICS", lyrics)
        if instrumental:
            af.set_tag("INSTRUMENTAL", "1")
        return path

    published, fetched = [], []

    def fake_fetch(artist, track, album=None, duration=None):
        fetched.append((artist, track, album, duration))
        return {"syncedLyrics": "x"} if "known" in track.lower() else None

    def fake_publish(artist, track, album, duration, plain=None, synced=None):
        published.append((artist, track, album, duration, plain, synced))
        return True, "published to LRCLIB — thank you for contributing!"

    real_fetch, real_publish = pub.lrclib_fetch, pub.lrclib_publish
    pub.lrclib_fetch, pub.lrclib_publish = fake_fetch, fake_publish
    try:
        unknown = make("01 - Unknown.flac")
        known = make("02 - Known.flac")
        af_known = AudioFile(known)
        af_known.set_tag("TITLE", "Known")
        empty = make("03 - Empty.flac", lyrics="")
        instr = make("04 - Instrumental.flac", instrumental=True)

        config = {"music_folder": TMP, "targets": [unknown, known, empty, instr],
                  "lrclib_auto_publish": True, "force_publish": False}
        stats = pub.run_publish_lyrics(config)
        ok(stats["published"] == 1 and len(published) == 1,
           f"only the track LRCLIB lacks is submitted ({stats['published']} of 4)")
        ok(published[0][1] == "Song" and published[0][3] == 1
           and published[0][4] == "Hello there"
           and published[0][5] == "[00:01.00]Hello there",
           f"the file's own text and duration go up, plain beside synced ({published[0]})")
        ok(stats["already_known"] == 1 and stats["no_lyrics"] == 1,
           f"the known track and the lyric-less one are counted apart "
           f"({stats['already_known']} known, {stats['no_lyrics']} no lyrics)")
        ok(sorted(f[1] for f in fetched) == ["Known", "Song"],
           f"only tracks WITH lyrics are looked up — the instrumental and the "
           f"lyric-less file never reach LRCLIB ({sorted(f[1] for f in fetched)})")

        # a synced text travels WITH its plain form
        published.clear()
        synced_path = make("05 - Synced.flac",
                           lyrics="[ti:Song]\n[00:01.00]Hello\n[00:03.00]there")
        af_synced = AudioFile(synced_path)
        af_synced.set_tag("TITLE", "Synced")
        pub.run_publish_lyrics({"music_folder": TMP, "targets": [synced_path],
                                "lrclib_auto_publish": True, "force_publish": False})
        ok(published and published[0][4] == "Hello\nthere"
           and published[0][5] == "[ti:Song]\n[00:01.00]Hello\n[00:03.00]there",
           f"the synced text is published with its plain form ({published[0][4:5]})")

        # LRCLIB already answers for it: never publish, unless forced
        published.clear()
        pub.run_publish_lyrics({"music_folder": TMP, "targets": [known],
                                "lrclib_auto_publish": True, "force_publish": False})
        ok(not published, "a track LRCLIB already has is never submitted")
        got = pub.publish_one(known, {"lrclib_auto_publish": True}, force=True)
        ok(got["status"] == "ok" and published, f"force_publish submits it anyway ({got['status']})")

        # a duplicate answer is a skip, not a failure
        pub.lrclib_publish = lambda *a, **k: (False, "LRCLIB already has this track")
        got = pub.publish_one(unknown, {"lrclib_auto_publish": True})
        ok(got["status"] == "skipped", f"a 409 duplicate is a skip, not a failure ({got['status']})")
        pub.lrclib_publish = fake_publish

        # the switch
        skipped = script_runners.run_script(18, {"music_folder": TMP, "targets": [unknown],
                                                 "lrclib_auto_publish": False})
        ok(skipped.get("skipped") is True and "lrclib_auto_publish" in skipped.get("reason", ""),
           f"the setting disables the script outright ({skipped.get('reason')})")
        ran = script_runners.run_script(18, {"music_folder": TMP, "targets": [unknown],
                                             "lrclib_auto_publish": True})
        ok("stats" in ran and ran["stats"]["published"] == 1,
           f"with the setting on it runs and reports ({ran.get('stats', {}).get('published')} published)")
    finally:
        pub.lrclib_fetch, pub.lrclib_publish = real_fetch, real_publish
        shutil.rmtree(TMP, ignore_errors=True)

# --------------------------------------------------------------------------- #
# The MANUAL path ("Publish to LRCLIB" in the lyric editor) enforces the same
# rule: a track LRCLIB already answers for is never submitted. This is the
# endpoint the editor's button posts to, so the rule has to hold there and not
# only inside script 18 — the editor is the path a user drives by hand.
# --------------------------------------------------------------------------- #
print()
print("== the manual publish endpoint ==")
from fastapi.testclient import TestClient  # noqa: E402  (heavy import)

from server import integrations as srv_intg  # noqa: E402
from server import main as mlo_main  # noqa: E402

_client = TestClient(mlo_main.app)          # no lifespan: no workers, no slskd
_real_get, _real_pub = srv_intg.lrclib_get, srv_intg.lrclib_publish
_submitted = []
try:
    srv_intg.lrclib_publish = lambda *a, **k: (_submitted.append(a) or (True, "published"))

    srv_intg.lrclib_get = lambda *a, **k: {"syncedLyrics": "[00:01.00]Hello"}
    r = _client.post("/api/lyrics/publish", json={
        "artist": "Artist", "track": "Song", "album": "Album", "duration": 213,
        "plain": "Hello there"})
    body = r.json()
    ok(r.status_code == 200 and body.get("ok") is False and body.get("exists") is True,
       f"a track LRCLIB already has is refused ({body})")
    ok(not _submitted, "and nothing was submitted")
    ok("already has lyrics" in (body.get("message") or ""),
       f"the refusal says why ({body.get('message')!r})")

    srv_intg.lrclib_get = lambda *a, **k: None
    r = _client.post("/api/lyrics/publish", json={
        "artist": "Artist", "track": "Unknown Song", "duration": 213, "plain": "Hello there"})
    body = r.json()
    ok(body.get("ok") is True and len(_submitted) == 1,
       f"a track LRCLIB lacks is submitted ({body})")
    ok(_submitted[0][:4] == ("Artist", "Unknown Song", "", 213),
       f"the submission carries the editor's own fields ({_submitted[0][:4]})")

    def _boom(*a, **k):
        raise RuntimeError("lrclib unreachable")
    _submitted.clear()
    srv_intg.lrclib_get = _boom
    r = _client.post("/api/lyrics/publish", json={
        "artist": "Artist", "track": "Unknown Song", "duration": 213, "plain": "Hello there"})
    ok(len(_submitted) == 1,
       "an unreachable existence check does not block the submission")
finally:
    srv_intg.lrclib_get, srv_intg.lrclib_publish = _real_get, _real_pub

print()
if FAILS:
    print("FAILED: %d check(s)" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all lyrics-publish checks passed")
