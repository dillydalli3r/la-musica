#!/usr/bin/env python3
"""AcoustID lookup module: availability, parsing, modal album match.

No network and no fpcalc required - the fpcalc path, the HTTP layer and
`lookup` are stubbed. The embedded payload mirrors the documented response of
https://api.acoustid.org/v2/lookup?meta=recordings+releasegroups+compress
(doc example: {"status":"ok","results":[{"id","score","recordings":[{"id"}]}]}),
with the `compress` shape (score 0..1 float, MusicBrainz ids as strings).

Run: python tools/test_acoustid.py
Exit 0 = pass, 1 = failure.
"""
import os
import sys

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
            "id": "9ff43b6a-4f16-427c-93c2-92307ca505e0",
            "score": 0.9378,
            "recordings": [
                {
                    "id": "38035858-f990-4fbb-b3b2-f2f8b958eeba",
                    "title": "Silent Shout",
                    "duration": 289,
                    "artists": [
                        {"id": "9d7b7c1e-1e5c-4b1e-9df0-8a4a4f0a4f11",
                         "name": "The Knife"},
                    ],
                    "releasegroups": [
                        {"id": "1d3d2a5c-1111-2222-3333-444455556666",
                         "title": "Silent Shout", "type": "Album"},
                    ],
                },
            ],
        },
        {
            "id": "0d3ddc52-6a68-4f05-b1a1-5bd5a2b0d9b8",
            "score": 0.6102,
            "recordings": [
                {
                    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "title": "Wrong Song",
                    "artists": [],
                    "releasegroups": [
                        {"id": "99999999-9999-9999-9999-999999999999",
                         "title": "Other Album", "type": "Single"},
                    ],
                },
            ],
        },
    ],
}

MIN = 0.75


def cfg(**over):
    base = {
        "acoustid_enabled": True,
        "acoustid_api_key": "gh5HwBPwmAs",
        "acoustid_min_score": MIN,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# availability
# --------------------------------------------------------------------------- #
real_fpcalc_path = acoustid.fpcalc_path
real_lookup = acoustid.lookup
real_request = acoustid._request
real_fingerprint = acoustid.fingerprint

acoustid.fpcalc_path = lambda cfg=None: None  # no fpcalc installed

check("no api key -> unavailable", acoustid.available(cfg(acoustid_api_key="")) is False)
check("no api key note", acoustid.acoustid_enabled_note(cfg(acoustid_api_key="")) == "no API key")
check("disabled note",
      acoustid.acoustid_enabled_note(cfg(acoustid_enabled=False)) == "AcoustID disabled in settings")
check("key but no fpcalc -> unavailable", acoustid.available(cfg()) is False)
check("no fpcalc note", acoustid.acoustid_enabled_note(cfg()) == "fpcalc not installed")

acoustid.fpcalc_path = lambda cfg=None: "C:/fake/fpcalc.exe"
check("key + fpcalc -> available", acoustid.available(cfg()) is True)
check("available note empty", acoustid.acoustid_enabled_note(cfg()) == "")

# explicit config path wins over deps/PATH
seen = {}


def fake_path(cfg=None):
    seen["cfg"] = cfg
    explicit = (cfg or {}).get("acoustid_fpcalc_path")
    return explicit if explicit and os.path.isfile(explicit) else None


acoustid.fpcalc_path = fake_path
mine = os.path.abspath(__file__)
check("explicit fpcalc path honored",
      acoustid.available(cfg(acoustid_fpcalc_path=mine)) is True)
check("bogus explicit path ignored",
      acoustid.available(cfg(acoustid_fpcalc_path="nope/none.exe")) is False)

# --------------------------------------------------------------------------- #
# lookup is None-safe when fpcalc / fingerprinting is missing
# --------------------------------------------------------------------------- #
acoustid.fpcalc_path = lambda cfg=None: None


def boom(*a, **k):
    raise AssertionError("network attempted without fpcalc")


acoustid._request = boom
check("lookup [] without fpcalc", acoustid.lookup(cfg(), __file__) == [])
check("fingerprint None without fpcalc", acoustid.fingerprint(__file__, cfg()) is None)
check("fingerprint None for missing file",
      acoustid.fingerprint(os.path.join(ROOT, "does-not-exist.flac"), cfg()) is None)

# --------------------------------------------------------------------------- #
# response parser
# --------------------------------------------------------------------------- #
rows = acoustid.parse_payload(SAMPLE, min_score=MIN)
check("parser drops low score", len(rows) == 1)
row = rows[0]
check("row score", row["score"] == 0.9378)
check("row recording id", row["recording_id"] == "38035858-f990-4fbb-b3b2-f2f8b958eeba")
check("row title", row["title"] == "Silent Shout")
check("row artists", row["artists"] == ["The Knife"])
check("row rg id", row["release_group_id"] == "1d3d2a5c-1111-2222-3333-444455556666")
check("row rg title", row["release_group_title"] == "Silent Shout")
check("row rg type", row["release_group_type"] == "Album")
check("row keys", set(row) == {
    "score", "recording_id", "title", "artists",
    "release_group_id", "release_group_title", "release_group_type"})

check("parser sorted desc",
      [r["score"] for r in acoustid.parse_payload(SAMPLE, min_score=0.0)] == [0.9378, 0.6102])
check("parser null rg safe", acoustid.parse_payload({
    "status": "ok",
    "results": [{"id": "x", "score": 0.9,
                 "recordings": [{"id": "r", "title": "t"}]}],
}, min_score=0.0)[0]["release_group_id"] is None)
check("parser error status -> []", acoustid.parse_payload({"status": "error", "error": {"code": 4}}) == [])
check("parser junk -> []", acoustid.parse_payload(None) == [] and acoustid.parse_payload({}) == [])

# lookup() wires fingerprint -> request -> rows
acoustid.fpcalc_path = lambda cfg=None: "C:/fake/fpcalc.exe"
acoustid.fingerprint = lambda path, cfg=None: {"duration": 289.4, "fingerprint": "AQAB"}
acoustid._request = lambda cfg, fp: SAMPLE["results"]
check("lookup rows from sample", acoustid.lookup(cfg(), __file__)[0]["recording_id"]
      == "38035858-f990-4fbb-b3b2-f2f8b958eeba")
acoustid.fingerprint = lambda path, cfg=None: None
check("lookup [] with no fingerprint", acoustid.lookup(cfg(), __file__) == [])

# --------------------------------------------------------------------------- #
# match_release: modal release group + quorum
# --------------------------------------------------------------------------- #
acoustid.fingerprint = real_fingerprint
acoustid._request = real_request

RG_A = "aaaaaaaa-0000-0000-0000-000000000001"
RG_B = "bbbbbbbb-0000-0000-0000-000000000002"


def row(rg, rec, title, score):
    return {
        "score": score,
        "recording_id": rec,
        "title": title,
        "artists": ["The Knife"],
        "release_group_id": rg,
        "release_group_title": "Silent Shout" if rg == RG_A else "Hits",
        "release_group_type": "Album" if rg == RG_A else "Compilation",
    }


def stub_lookup(mapping):
    def inner(cfg_, path):
        return mapping.get(os.path.basename(path), [])
    return inner


paths = [f"C:/m/{i:02d}.flac" for i in range(1, 6)]
progress_calls = []

acoustid.lookup = stub_lookup({
    "01.flac": [row(RG_A, "r1", "T1", 0.95)],
    "02.flac": [row(RG_A, "r2", "T2", 0.90)],
    "03.flac": [row(RG_A, "r3", "T3", 0.88)],
    "04.flac": [row(RG_B, "r4", "T4", 0.80)],
    "05.flac": [],
})
match = acoustid.match_release(cfg(), paths,
                               progress=lambda d, t, m: progress_calls.append((d, t, m)))
check("modal release group wins", match and match["release_group_id"] == RG_A)
check("matched/total", (match["matched"], match["total"]) == (3, 5))
check("mean score", abs(match["score"] - (0.95 + 0.90 + 0.88) / 3) < 1e-9)
check("rg metadata", (match["release_group_title"], match["release_group_type"])
      == ("Silent Shout", "Album"))
check("recordings carried", [r["path"] for r in match["recordings"]]
      == paths[:3] and match["recordings"][0]["recording_id"] == "r1")
check("artists", match["artists"] == ["The Knife"])
check("progress reported", progress_calls == [(1, 5, "AcoustID lookup"),
                                              (2, 5, "AcoustID lookup"),
                                              (3, 5, "AcoustID lookup"),
                                              (4, 5, "AcoustID lookup"),
                                              (5, 5, "AcoustID lookup")])

# no real quorum: every track a different release group -> 1 match each
acoustid.lookup = stub_lookup({
    f"{i:02d}.flac": [row(f"rg-{i}", f"r{i}", f"T{i}", 0.9)] for i in range(1, 6)})
check("no quorum -> None", acoustid.match_release(cfg(), paths) is None)
check("empty inputs -> None", acoustid.match_release(cfg(), []) is None
      and acoustid.match_release(cfg(), [None, ""]) is None)

# below min score: rows exist but no track clears the threshold
acoustid.lookup = stub_lookup({
    "01.flac": [row(RG_A, "r1", "T1", 0.10)],
    "02.flac": [row(RG_A, "r2", "T2", 0.20)]})
check("below threshold -> None", acoustid.match_release(cfg(), paths[:2]) is None)

# single-track album can never match (quorum floor of 2)
acoustid.lookup = stub_lookup({"01.flac": [row(RG_A, "r1", "T1", 0.99)]})
check("single track -> None", acoustid.match_release(cfg(), paths[:1]) is None)

# no fpcalc -> None without any lookup
acoustid.lookup = stub_lookup({"01.flac": [row(RG_A, "r1", "T1", 0.99)]})
acoustid.fpcalc_path = lambda cfg=None: None
check("no fpcalc -> match None", acoustid.match_release(cfg(), paths) is None)
acoustid.fpcalc_path = lambda cfg=None: "C:/fake/fpcalc.exe"

# 12-file cap
big = [f"C:/m/{i:02d}.flac" for i in range(1, 21)]
acoustid.lookup = stub_lookup({f"{i:02d}.flac": [row(RG_A, f"r{i}", f"T{i}", 0.95)]
                               for i in range(1, 21)})
match = acoustid.match_release(cfg(), big)
check("tracks capped at 12", match and match["total"] == acoustid.MAX_TRACKS == 12
      and match["matched"] == 12)

# fpcalc parse: DURATION=/FINGERPRINT= fallback + json form
class _R:
    returncode = 0


def make_result(text):
    r = _R()
    r.stdout = text
    return r


real_run_tool = acoustid.run_tool
acoustid.fpcalc_path = lambda cfg=None: mine  # any existing file
acoustid.run_tool = lambda *a, **k: make_result('{"duration": 12.5, "fingerprint": "AQAB"}')
check("fpcalc json parsed", acoustid.fingerprint(mine, cfg()) == {"duration": 12.5, "fingerprint": "AQAB"})
acoustid.run_tool = lambda *a, **k: make_result("DURATION=12\nFINGERPRINT=AQAB\n")
check("fpcalc legacy output parsed",
      acoustid.fingerprint(mine, cfg()) == {"duration": 12.0, "fingerprint": "AQAB"})
acoustid.run_tool = lambda *a, **k: make_result("garbage")


def raise_on_run(*a, **k):
    raise OSError("boom")


acoustid.run_tool = raise_on_run
check("fpcalc failure -> None", acoustid.fingerprint(mine, cfg()) is None)
acoustid.run_tool = real_run_tool
acoustid.lookup = real_lookup
acoustid.fpcalc_path = real_fpcalc_path

if FAILURES:
    print(f"{len(FAILURES)} failure(s)")
    sys.exit(1)
print("OK test_acoustid")
sys.exit(0)
