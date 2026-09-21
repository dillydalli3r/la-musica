#!/usr/bin/env python3
"""AcoustID lookup module: every failure mode is NAMED, never a silent "no match".

No network and no fpcalc run here: the fpcalc path, `run_tool` and the HTTP
layer are stubbed, with the response shapes documented at
https://acoustid.org/webservice (`meta=recordings+releasegroups+compress`,
score 0..1, MusicBrainz ids as strings). Each case pins one way the path can
end — and that the module says WHICH way, because "no match" must only ever
mean "the service answered and knew nothing".

Run: python tools/test_acoustid.py
Exit 0 = pass, 1 = failure.
"""
import io
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import acoustid  # noqa: E402

FAILURES = []


def check(label, cond):
    if not cond:
        FAILURES.append(label)
        print(f"FAIL {label}")


# Captured response shape (meta=recordings+releasegroups+compress).
SAMPLE = {
    "status": "ok",
    "results": [
        {
            "id": "9f4e1d2c-acoustid-id",
            "score": 0.9378,
            "recordings": [{
                "id": "38035858-f990-4fbb-b3b2-f2f8b958eeba",
                "title": "Silent Shout",
                "duration": 289,
                "artists": [{"id": "artist-1", "name": "The Knife"}],
                "releasegroups": [{
                    "id": "1d3d2a5c-1111-2222-3333-444455556666",
                    "title": "Silent Shout",
                    "type": "Album",
                }],
            }],
        },
        {
            "id": "low",
            "score": 0.6102,
            "recordings": [{
                "id": "rec-low",
                "title": "Something Else",
                "artists": [{"id": "artist-2", "name": "Somebody"}],
                "releasegroups": [{"id": "rg-low", "title": "Else", "type": "Single"}],
            }],
        },
    ],
}

MIN = 0.75
RG = "1d3d2a5c-1111-2222-3333-444455556666"


def cfg(**over):
    base = {
        "acoustid_enabled": True,
        "acoustid_api_key": "gh5HwBPwmAs",
        "acoustid_min_score": MIN,
    }
    base.update(over)
    return base


# A temp album of "audio" files: fpcalc is stubbed, so their contents are moot,
# but `fingerprint` refuses a path that is not a real file.
TMP = tempfile.TemporaryDirectory(prefix="mlo-acoustid-")
ALBUM = TMP.name
PATHS = []
for i in range(1, 6):
    p = os.path.join(ALBUM, f"{i:02d}.flac")
    with open(p, "wb") as fh:
        fh.write(b"fLaC")
    PATHS.append(p)

REAL = {
    "fpcalc_path": acoustid.fpcalc_path,
    "run_tool": acoustid.run_tool,
    "urlopen": urllib.request.urlopen,
    "urllib": acoustid.urllib,
    "throttle": acoustid._throttle,
}


def restore():
    acoustid.fpcalc_path = REAL["fpcalc_path"]
    acoustid.run_tool = REAL["run_tool"]
    acoustid.urllib = REAL["urllib"]
    acoustid._throttle = REAL["throttle"]
    urllib.request.urlopen = REAL["urlopen"]


# Rate limiting would add 0.34s per stubbed call; the limit itself is asserted
# once, at the end, instead of being paid by every case.
acoustid._throttle = lambda: None

# --------------------------------------------------------------------------- #
# availability: what the config lacks is NAMED
# --------------------------------------------------------------------------- #
acoustid.fpcalc_path = lambda cfg=None: None

check("no api key -> unavailable", acoustid.available(cfg(acoustid_api_key="")) is False)
check("no api key note", acoustid.acoustid_enabled_note(cfg(acoustid_api_key="")) == "no API key")
check("no api key code", acoustid.check(cfg(acoustid_api_key=""))["code"] == acoustid.NO_API_KEY)
check("disabled note",
      acoustid.acoustid_enabled_note(cfg(acoustid_enabled=False)) == "AcoustID disabled in settings")
check("disabled code",
      acoustid.check(cfg(acoustid_enabled=False))["code"] == acoustid.DISABLED)
check("key but no fpcalc -> unavailable", acoustid.available(cfg()) is False)
check("no fpcalc note", acoustid.acoustid_enabled_note(cfg()) == "fpcalc not installed")
check("no fpcalc code", acoustid.check(cfg())["code"] == acoustid.NO_FPCALC)

acoustid.fpcalc_path = lambda cfg=None: "C:/fake/fpcalc.exe"
check("key + fpcalc -> available", acoustid.available(cfg()) is True)
check("available note empty", acoustid.acoustid_enabled_note(cfg()) == "")
check("available code ok", acoustid.check(cfg())["code"] == acoustid.OK)

# explicit config path wins, a bogus one is ignored (real FS check, so the real
# resolver goes back in)
acoustid.fpcalc_path = REAL["fpcalc_path"]
mine = os.path.abspath(__file__)
check("explicit fpcalc path honored",
      acoustid.fpcalc_path(cfg(acoustid_fpcalc_path=mine)) == mine)
check("bogus explicit path falls through",
      acoustid.fpcalc_path(cfg(acoustid_fpcalc_path="nope/none.exe")) != "nope/none.exe")

# the app's OWN dependency install is consulted (the folder mlo.tools and the
# Dependencies installer both read), not just PATH
DEPS = os.path.join(TMP.name, "deps")
os.makedirs(os.path.join(DEPS, "chromaprint v1.6.1"), exist_ok=True)
BUNDLED = os.path.join(DEPS, "chromaprint v1.6.1", "fpcalc.exe")
with open(BUNDLED, "wb") as fh:
    fh.write(b"MZ")
real_deps = acoustid.DEPS_DIR
acoustid.DEPS_DIR = DEPS
try:
    check("bundled .dependencies fpcalc is found",
          acoustid.fpcalc_path({"acoustid_fpcalc_path": ""}) == BUNDLED)
finally:
    acoustid.DEPS_DIR = real_deps

# --------------------------------------------------------------------------- #
# no key / no fpcalc: a NAMED skip, no lookup attempted
# --------------------------------------------------------------------------- #
def boom(*a, **k):
    raise AssertionError("network attempted when AcoustID cannot run")


acoustid.run_tool = boom
acoustid.urllib = type("U", (), {
    "parse": urllib.parse, "error": urllib.error,
    "request": type("R", (), {"urlopen": boom,
                              "Request": urllib.request.Request}),
})()

acoustid.fpcalc_path = lambda cfg=None: None
res = acoustid.match_release(cfg(), PATHS)
check("no fpcalc -> status skipped, not no_match",
      res["status"] == "skipped" and res["code"] == acoustid.NO_FPCALC)
check("no fpcalc reason", res["reason"] == "fpcalc not installed")
check("no fpcalc -> no match object", res["match"] is None)
check("no fpcalc -> nothing examined, nothing claimed",
      res["tracks"]["total"] == 0 and res["failures"] == [] and res["skips"] == [])

res = acoustid.match_release(cfg(acoustid_api_key=""), PATHS)
check("no key -> status skipped, not no_match",
      res["status"] == "skipped" and res["code"] == acoustid.NO_API_KEY
      and res["reason"] == "no API key")

res = acoustid.match_release(cfg(acoustid_enabled=False), PATHS)
check("disabled -> skipped with its own code",
      res["status"] == "skipped" and res["code"] == acoustid.DISABLED
      and res["reason"] == "AcoustID disabled in settings")

check("lookup without fpcalc is a named skip",
      acoustid.lookup(cfg(), PATHS[0])["code"] == acoustid.NO_FPCALC)

# --------------------------------------------------------------------------- #
# fpcalc: reported, never "no match"
# --------------------------------------------------------------------------- #
# still no fpcalc here: the check has to come before the exe is stubbed, and an
# empty album is "no tracks" only once AcoustID itself could run
check("no fpcalc -> fingerprint code",
      acoustid.fingerprint(PATHS[0], cfg())["code"] == acoustid.NO_FPCALC)
acoustid.fpcalc_path = lambda cfg=None: mine  # any existing file
check("empty album -> no_tracks skip",
      acoustid.match_release(cfg(), [])["code"] == acoustid.NO_TRACKS)


class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


acoustid.run_tool = lambda *a, **k: _R(0, '{"duration": 12.5, "fingerprint": "AQAB"}')
check("fpcalc json parsed",
      acoustid.fingerprint(mine, cfg())["fingerprint"] == "AQAB")
acoustid.run_tool = lambda *a, **k: _R(0, "DURATION=12\nFINGERPRINT=AQAB\n")
fp = acoustid.fingerprint(mine, cfg())
check("fpcalc legacy output parsed",
      fp["ok"] and fp["duration"] == 12.0 and fp["fingerprint"] == "AQAB")
acoustid.run_tool = lambda *a, **k: _R(0, "garbage")
fp = acoustid.fingerprint(mine, cfg())
check("unreadable fpcalc output is an error, not a match",
      fp["ok"] is False and fp["code"] == acoustid.FPCALC_FAILED
      and "could not be read" in fp["reason"])
check("missing file is named", acoustid.fingerprint(
    os.path.join(ALBUM, "gone.flac"), cfg())["code"] == acoustid.NO_FILE)


def raise_on_run(*a, **k):
    raise OSError("fpcalc.exe is not a valid Win32 application")


acoustid.run_tool = raise_on_run
res = acoustid.match_release(cfg(), PATHS)
check("broken fpcalc -> status error, never no_match",
      res["status"] == "error" and res["code"] == acoustid.FPCALC_FAILED)
check("broken fpcalc reason names the tool",
      "could not run" in res["reason"] and "fpcalc.exe is not a valid" in res["reason"])
check("broken fpcalc -> one failure per track", len(res["failures"]) == 5)
check("broken fpcalc -> conflict list empty", res["conflicts"] == [] and res["conflict"] is False)

# a video container: no audio stream, so fpcalc fails and the track is SKIPPED
video = os.path.join(ALBUM, "concert.mp4")
with open(video, "wb") as fh:
    fh.write(b"\x00")
acoustid.run_tool = lambda *a, **k: _R(1, "", "ERROR: could not find codec parameters")
fp = acoustid.fingerprint(video, cfg())
check("video container -> not_audio skip with a reason",
      fp["code"] == acoustid.NOT_AUDIO and "concert.mp4" in fp["reason"])
res = acoustid.match_release(cfg(), [video])
check("video only album -> skipped, not error",
      res["status"] == "skipped" and res["code"] == acoustid.NOT_AUDIO
      and res["tracks"]["skipped"] == 1)
# the same verdict when the file is mislabelled: fpcalc's own words decide
acoustid.run_tool = lambda *a, **k: _R(
    2, "", "ERROR: Could not find any audio stream in the file track01.flac")
fp = acoustid.fingerprint(PATHS[0], cfg())
check("a file with no audio stream is a skip, not a tool failure",
      fp["code"] == acoustid.NOT_AUDIO and "no fingerprintable audio stream" in fp["reason"])
acoustid.run_tool = lambda *a, **k: _R(1, "", "ERROR: unknown format")
check("a real fpcalc failure stays a failure",
      acoustid.fingerprint(PATHS[0], cfg())["code"] == acoustid.FPCALC_FAILED)

# a very short track
acoustid.run_tool = lambda *a, **k: _R(0, '{"duration": 2.0, "fingerprint": "AQAB"}')
fp = acoustid.fingerprint(PATHS[0], cfg())
check("short track -> too_short skip", fp["code"] == acoustid.TOO_SHORT
      and "too short" in fp["reason"])
acoustid.run_tool = lambda *a, **k: _R(0, '{"duration": 30.0, "fingerprint": ""}')
check("empty fingerprint -> no_fingerprint skip",
      acoustid.fingerprint(PATHS[0], cfg())["code"] == acoustid.NO_FINGERPRINT)

# --------------------------------------------------------------------------- #
# HTTP: stubbed transport, every failure mode distinct
# --------------------------------------------------------------------------- #
POSTED = []


class _Resp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def urlopen(body=b"", error=None, record=True):
    def inner(req, timeout=None):
        if record:
            POSTED.append({
                "url": req.full_url,
                "form": urllib.parse.parse_qs((req.data or b"").decode()),
                "timeout": timeout,
                "ua": req.get_header("User-agent"),
            })
        if error is not None:
            raise error
        return _Resp(body() if callable(body) else body)
    return inner


def set_transport(fn):
    acoustid.urllib = type("U", (), {
        "parse": urllib.parse, "error": urllib.error,
        "request": type("R", (), {"urlopen": fn,
                                  "Request": urllib.request.Request}),
    })()


def http_error(code, body, reason="Bad Request"):
    return urllib.error.HTTPError(acoustid.API_URL, code, reason, {}, io.BytesIO(body))


GOOD_FP = {"duration": 289.4, "fingerprint": "AQAB"}
acoustid.fpcalc_path = lambda cfg=None: mine
acoustid.run_tool = lambda *a, **k: _R(0, '{"duration": 289.4, "fingerprint": "AQAB"}')

# --- success: the release identity comes back ----------------------------- #
import json  # noqa: E402

POSTED.clear()
set_transport(urlopen(json.dumps(SAMPLE).encode()))
res = acoustid.lookup(cfg(), PATHS[0])
check("lookup ok", res["ok"] is True and res["code"] == acoustid.OK)
check("lookup returns the MB recording id",
      res["rows"][0]["recording_id"] == "38035858-f990-4fbb-b3b2-f2f8b958eeba")
check("lookup returns the MB release group id",
      res["rows"][0]["release_group_id"] == RG
      and res["rows"][0]["release_group_title"] == "Silent Shout")
check("lookup drops the below-floor candidate", len(res["rows"]) == 1)
check("lookup carries the fingerprint it matched",
      res["rows"][0]["fingerprint"] == "AQAB")
sent = POSTED[0]
check("posts the documented endpoint and form",
      sent["url"] == "https://api.acoustid.org/v2/lookup"
      and sent["form"] == {"client": ["gh5HwBPwmAs"], "duration": ["289"],
                           "fingerprint": ["AQAB"],
                           "meta": ["recordings releasegroups compress"]})
check("request is bounded by a timeout (a hung lookup cannot hang the import)",
      sent["timeout"] == acoustid._HTTP_TIMEOUT and sent["timeout"] > 0)

# --- no match: the service answered, and knows nothing -------------------- #
set_transport(urlopen(b'{"status": "ok", "results": []}'))
res = acoustid.lookup(cfg(), PATHS[0])
check("empty results -> no_match (the service answered)",
      res["ok"] is False and res["code"] == acoustid.NO_MATCH
      and res["rows"] == [])
res = acoustid.match_release(cfg(), PATHS[:2])
check("album with no candidate -> status no_match",
      res["status"] == "no_match" and res["code"] == acoustid.NO_MATCH
      and res["match"] is None and res["failures"] == [])

# --- rejected request (400 + message) / route gone (404 html) ------------- #
set_transport(urlopen(error=http_error(
    400, b'{"status": "error", "error": {"code": 4, "message": "invalid API key"}}')))
res = acoustid.lookup(cfg(), PATHS[0])
check("invalid key -> lookup_failed, service message kept",
      res["code"] == acoustid.LOOKUP_FAILED and "invalid API key" in res["reason"]
      and "HTTP 400" in res["reason"])
set_transport(urlopen(error=http_error(404, b"<html>not found</html>", "Not Found")))
res = acoustid.lookup(cfg(), PATHS[0])
check("404 -> lookup_failed by status, not by pasted HTML",
      res["code"] == acoustid.LOOKUP_FAILED and "HTTP 404" in res["reason"]
      and "<html>" not in res["reason"])
set_transport(urlopen(error=urllib.error.URLError("getaddrinfo failed")))
check("offline -> lookup_failed with the OS reason",
      "getaddrinfo failed" in acoustid.lookup(cfg(), PATHS[0])["reason"])

# --- timeout: reported, and the album stops asking ------------------------ #
set_transport(urlopen(error=TimeoutError("timed out"), record=True))
POSTED.clear()
res = acoustid.lookup(cfg(), PATHS[0])
check("timeout -> lookup_failed with 'timed out'",
      res["code"] == acoustid.LOOKUP_FAILED and "timed out" in res["reason"]
      and str(acoustid._HTTP_TIMEOUT) in res["reason"])
POSTED.clear()
res = acoustid.match_release(cfg(), PATHS + PATHS)  # 10 tracks
check("timeout aborts the album instead of hanging twelve lookups",
      len(POSTED) == 1 and res["status"] == "error"
      and res["code"] == acoustid.LOOKUP_FAILED)
check("timeout -> one named failure, not ten",
      len(res["failures"]) == 1 and "timed out" in res["failures"][0]["reason"])

# --- malformed bodies ----------------------------------------------------- #
set_transport(urlopen(b"<html>gateway</html>"))
check("non-JSON body -> bad_response",
      acoustid.lookup(cfg(), PATHS[0])["code"] == acoustid.BAD_RESPONSE)
set_transport(urlopen(b'{"status": "error", "error": {"code": 5, '
                      b'"message": "invalid fingerprint"}}'))
res = acoustid.lookup(cfg(), PATHS[0])
check("200 with status=error -> bad_response + message",
      res["code"] == acoustid.BAD_RESPONSE and "invalid fingerprint" in res["reason"])
set_transport(urlopen(b'{"status": "ok", "results": "nope"}'))
check("results not a list -> bad_response",
      acoustid.lookup(cfg(), PATHS[0])["code"] == acoustid.BAD_RESPONSE)
set_transport(urlopen(b'[1, 2, 3]'))
check("JSON array body -> bad_response",
      acoustid.lookup(cfg(), PATHS[0])["code"] == acoustid.BAD_RESPONSE)
set_transport(urlopen(b'{"status": "ok", "results": [{"score": "junk"}]}'))
check("unscorable result is no_match, not a crash",
      acoustid.lookup(cfg(), PATHS[0])["code"] == acoustid.NO_MATCH)

# --------------------------------------------------------------------------- #
# verify_key: the key checked on its own, with no audio and no fpcalc
# --------------------------------------------------------------------------- #
POSTED.clear()
acoustid.fpcalc_path = lambda cfg=None: None      # deliberately absent
set_transport(urlopen(json.dumps({"status": "ok", "results": []}).encode()))
res = acoustid.verify_key(cfg())
check("verify_key needs no fpcalc", res["ok"] is True and res["code"] == acoustid.OK
      and res["results"] == 0)
check("verify_key posts the probe fingerprint, not an audio lookup",
      POSTED[-1]["form"]["fingerprint"] == [acoustid.PROBE_FINGERPRINT]
      and POSTED[-1]["form"]["duration"] == [str(acoustid.PROBE_DURATION)]
      and len(acoustid.PROBE_FINGERPRINT) == 126
      and acoustid.PROBE_FINGERPRINT.startswith("AQAA"))
set_transport(urlopen(error=http_error(
    400, b'{"status": "error", "error": {"code": 4, "message": "invalid API key"}}')))
res = acoustid.verify_key(cfg())
check("verify_key returns the service's refusal verbatim",
      res["ok"] is False and res["code"] == acoustid.LOOKUP_FAILED
      and "invalid API key" in res["reason"] and "HTTP 400" in res["reason"])
POSTED.clear()
check("verify_key without a key does not ask at all",
      acoustid.verify_key(cfg(acoustid_api_key=""))["code"] == acoustid.NO_API_KEY
      and acoustid.verify_key(cfg(acoustid_enabled=False))["code"] == acoustid.DISABLED
      and POSTED == [])
set_transport(urlopen(b'{"status": "ok", "results": []}'))
check("a caller's own fingerprint can be checked instead",
      acoustid.verify_key(cfg(), fingerprint="AQAB", duration=12)["ok"] is True
      and POSTED[-1]["form"]["fingerprint"] == ["AQAB"]
      and POSTED[-1]["form"]["duration"] == ["12"])
acoustid.fpcalc_path = lambda cfg=None: mine      # back for the album cases

# --------------------------------------------------------------------------- #
# the parser itself (pure: [] on junk, the reason lives in _request)
# --------------------------------------------------------------------------- #
rows = acoustid.parse_payload(SAMPLE, min_score=MIN)
check("parser drops low score", len(rows) == 1)
row = rows[0]
check("row ids/title/artists", (row["recording_id"],
                                row["release_group_id"],
                                row["release_group_title"],
                                row["release_group_type"],
                                row["artists"]) ==
      ("38035858-f990-4fbb-b3b2-f2f8b958eeba", RG, "Silent Shout", "Album",
       ["The Knife"]))
check("row keys", set(row) == {
    "score", "recording_id", "title", "artists",
    "release_group_id", "release_group_title", "release_group_type"})
check("parser sorted desc",
      [r["score"] for r in acoustid.parse_payload(SAMPLE, min_score=0.0)]
      == [0.9378, 0.6102])
check("parser null rg safe", acoustid.parse_payload({
    "status": "ok", "results": [{"score": 0.9, "recordings": [{"id": "r"}]}],
}, min_score=0.0)[0]["release_group_id"] is None)
check("parser error status -> []",
      acoustid.parse_payload({"status": "error", "error": {"code": 4}}) == [])
check("parser junk -> []",
      acoustid.parse_payload(None) == [] and acoustid.parse_payload({}) == [])

# --------------------------------------------------------------------------- #
# album matching: modal release group + quorum
# --------------------------------------------------------------------------- #
RG_A = "aaaaaaaa-0000-0000-0000-000000000001"
RG_B = "bbbbbbbb-0000-0000-0000-000000000002"


def cand_row(rg, rec, title, score, artists=("The Knife",)):
    return {"score": score, "recording_id": rec, "title": title,
            "artists": list(artists), "release_group_id": rg,
            "release_group_title": title, "release_group_type": "Album",
            "fingerprint": "AQAB"}


def stub_lookup(mapping):
    def inner(cfg_, path):
        got = mapping.get(os.path.basename(path), [])
        if isinstance(got, dict):
            return got
        if not got:
            return acoustid._result(False, acoustid.NO_MATCH,
                                    "nothing above 0.75", rows=[])
        return acoustid._result(True, acoustid.OK, "", rows=got, fingerprint="AQAB",
                                duration=289.0)
    return inner


real_lookup = acoustid.lookup
acoustid.lookup = stub_lookup({
    "01.flac": [cand_row(RG_A, "r1", "Silent Shout", 0.95)],
    "02.flac": [cand_row(RG_A, "r2", "Silent Shout", 0.90)],
    "03.flac": [cand_row(RG_A, "r3", "Silent Shout", 0.88)],
    "04.flac": [cand_row(RG_B, "r4", "Other", 0.99)],
    "05.flac": [],
})
progress_calls = []
res = acoustid.match_release(cfg(), PATHS,
                             progress=lambda d, t, m: progress_calls.append((d, t, m)))
match = res["match"]
check("modal release group wins", res["status"] == "matched" and match
      and match["release_group_id"] == RG_A)
check("matched/total", (match["matched"], match["total"]) == (3, 5))
check("mean score", abs(match["score"] - (0.95 + 0.90 + 0.88) / 3) < 1e-9)
check("rg metadata", (match["release_group_title"], match["release_group_type"],
                      match["artists"]) == ("Silent Shout", "Album", ["The Knife"]))
check("recordings carried", [r["path"] for r in match["recordings"]] == PATHS[:3]
      and match["recordings"][0]["recording_id"] == "r1")
check("progress reported", progress_calls == [(i, 5, "AcoustID lookup") for i in range(1, 6)])
check("no conflicts without an expectation",
      res["conflict"] is False and res["conflicts"] == [])

# a track whose own lookup failed is carried, not hidden
acoustid.lookup = stub_lookup({
    "01.flac": [cand_row(RG_A, "r1", "Silent Shout", 0.95)],
    "02.flac": [cand_row(RG_A, "r2", "Silent Shout", 0.90)],
    "03.flac": acoustid._result(False, acoustid.NO_MATCH, "nothing above 0.75", rows=[]),
    "04.flac": acoustid._result(False, acoustid.FPCALC_FAILED, "fpcalc failed on 04.flac"),
    "05.flac": [cand_row(RG_A, "r5", "Silent Shout", 0.91)],
})
res = acoustid.match_release(cfg(), PATHS)
check("a matched album still lists its failures",
      res["status"] == "matched" and res["tracks"]["fingerprinted"] == 3
      and len(res["failures"]) == 1 and res["failures"][0]["code"] == acoustid.FPCALC_FAILED
      and "1 of 5" in res["reason"])

# no quorum: every track a different release group -> 1 match each, and NO
# failures, so the honest answer is "no match"
acoustid.lookup = stub_lookup({
    f"{i:02d}.flac": [cand_row(f"rg-{i}", f"r{i}", f"T{i}", 0.9)] for i in range(1, 6)})
res = acoustid.match_release(cfg(), PATHS)
check("no quorum -> no_match", res["status"] == "no_match" and res["match"] is None)
check("below threshold -> no_match",
      acoustid.match_release(cfg(), PATHS[:2])["status"] == "no_match")
acoustid.lookup = stub_lookup({
    "01.flac": [cand_row(RG_A, "r1", "T1", 0.10)],
    "02.flac": [cand_row(RG_A, "r2", "T2", 0.20)]})
check("rows below min_score -> no_match",
      acoustid.match_release(cfg(), PATHS[:2])["status"] == "no_match")
acoustid.lookup = stub_lookup({"01.flac": [cand_row(RG_A, "r1", "T1", 0.99)]})
check("single track can never decide an album",
      acoustid.match_release(cfg(), PATHS[:1])["status"] == "no_match")

# 12-file cap
big = [os.path.join(ALBUM, f"b{i:02d}.flac") for i in range(1, 21)]
for p in big:
    with open(p, "wb") as fh:
        fh.write(b"fLaC")
acoustid.lookup = stub_lookup({os.path.basename(p): [cand_row(RG_A, "r", "T", 0.95)]
                               for p in big})
res = acoustid.match_release(cfg(), big)
check("tracks capped at 12", res["match"]["total"] == acoustid.MAX_TRACKS == 12
      and res["match"]["matched"] == 12)

# --------------------------------------------------------------------------- #
# cross-check: the fingerprint is verified against the TAGS, never trusted
# --------------------------------------------------------------------------- #
acoustid.lookup = stub_lookup({
    "01.flac": [cand_row(RG_A, "r1", "Silent Shout", 0.95)],
    "02.flac": [cand_row(RG_A, "r2", "Silent Shout", 0.90)],
    "03.flac": [cand_row(RG_A, "r3", "Silent Shout", 0.91, artists=("The Knife",))],
})
res = acoustid.match_release(cfg(), PATHS[:3], expect={"release_group_id": RG_A})
check("agreement -> no conflict", res["conflicts"] == [] and res["conflict"] is False
      and res["status"] == "matched")

res = acoustid.match_release(cfg(), PATHS[:3],
                             expect={"release_group_id": RG_B, "release_group_title": "Other"})
check("a disagreeing release-group id is a reported conflict",
      res["status"] == "matched" and res["conflict"] is True
      and [c["kind"] for c in res["conflicts"]] == ["release_group"])
check("conflict names both ids",
      RG_A in res["conflicts"][0]["reason"] and RG_B in res["conflicts"][0]["reason"])
check("the match is NOT overwritten by the tags",
      res["match"]["release_group_id"] == RG_A
      and res["conflicts"][0]["fingerprint"] == RG_A
      and res["conflicts"][0]["tags"] == RG_B)
check("code marks the conflict", res["code"] == acoustid.CONFLICT)

# no ids to compare -> the normalized title decides
acoustid.lookup = stub_lookup({
    "01.flac": [cand_row("rg-none", "r1", "Silent Shout", 0.95)],
    "02.flac": [cand_row("rg-none", "r2", "Silent Shout", 0.90)],
})
res = acoustid.match_release(cfg(), PATHS[:2], expect={"release_group_title": "Different"})
check("title-only disagreement is reported",
      [c["kind"] for c in res["conflicts"]] == ["title"])
check("case/punctuation of a title is not a disagreement",
      acoustid.match_release(cfg(), PATHS[:2],
                             expect={"release_group_title": "silent-shout!"})["conflicts"] == [])

# artists: a disjoint pair means the audio is by somebody else
check("artist disagreement is reported",
      [c["kind"] for c in acoustid.cross_check(
          {"release_group_id": RG_A, "release_group_title": "T",
           "artists": ["The Knife"]},
          {"release_group_id": RG_A, "artists": [{"name": "Somebody Else"}]})]
      == ["artist"])
check("artist case/punctuation is not a disagreement",
      acoustid.cross_check({"release_group_id": RG_A, "artists": ["The Knife"]},
                           {"release_group_id": RG_A, "artists": ["the-knife"]}) == [])
check("an edition suffix is not a different release",
      acoustid.cross_check({"release_group_title": "Silent Shout"},
                           {"release_group_title": "Silent Shout (Deluxe Edition)"}) == [])
check("a guest credit is not a different artist",
      acoustid.cross_check({"artists": ["The Knife"]},
                           {"artists": ["The Knife feat. Someone"]}) == [])
check("one-sided evidence is not a disagreement",
      acoustid.cross_check({"release_group_id": RG_A, "artists": ["The Knife"]},
                           {"title": "Silent Shout"}) == []
      and acoustid.cross_check(match, None) == [])
check("a release dict's title is only compared when the ids cannot be",
      acoustid.cross_check({"release_group_id": RG_A, "release_group_title": "Silent Shout"},
                           {"release_group_id": RG_A, "title": "Totally Elsewhere"}) == [])

acoustid.lookup = real_lookup
restore()

# --------------------------------------------------------------------------- #
# the throttle is real (one 0.34s sample, not one per stubbed call)
# --------------------------------------------------------------------------- #
start = time.monotonic()
acoustid._last_call = time.monotonic()
acoustid._throttle()
elapsed = time.monotonic() - start
check("throttle keeps the module under 3 req/s", elapsed >= acoustid._MIN_INTERVAL - 0.02)

TMP.cleanup()

if FAILURES:
    print(f"{len(FAILURES)} failure(s)")
    sys.exit(1)
print("OK test_acoustid")
sys.exit(0)
