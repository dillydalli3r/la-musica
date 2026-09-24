#!/usr/bin/env python3
"""Verify the Home page payload helpers: top artists, needs-attention and
Soulseek wishlist rows, and the grading strip the pages open with — including
the words the strip itself prints, rendered from the real client component.
Uses synthetic library data (no disk scan).

Run:  python tools/test_home.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import recommendations as r

artists = [
    {"path": "C:/M/A", "name": "Alpha", "aggregate": {"grade_pct": 90.0, "track_count": 20},
     "albums": [{"path": "C:/M/A/X", "cover_file": "cover.jpg"},
                {"path": "C:/M/A/Y", "cover_file": None}]},
    {"path": "C:/M/B", "name": "Beta", "aggregate": {"grade_pct": None, "track_count": 5},
     "albums": [{"path": "C:/M/B/Z"}]},
    {"path": "C:/M/C", "name": "", "albums": []},  # dropped
]
top = r._top_artists(artists, 5)
assert [a["artist"] for a in top] == ["Alpha", "Beta"], top
assert top[0]["album_count"] == 2 and top[0]["track_count"] == 20
assert top[0]["cover"] == "cover.jpg" and top[0]["cover_path"] == "C:/M/A/X"
assert top[1]["grade_pct"] is None

albums = [
    {"path": "C:/M/A/X", "total_checks": 10, "pass_count": 10, "pass": True, "grade_pct": 100.0},
    {"path": "C:/M/A/Y", "total_checks": 10, "pass_count": 4, "pass": False, "grade_pct": 40.0},
    {"path": "C:/M/A/Z", "total_checks": 0, "pass_count": 0, "pass": False, "grade_pct": None},
]
att = r._needs_attention(albums, 5)
assert [a["path"] for a in att] == ["C:/M/A/Y"], att
assert att[0]["owned"] is True and att[0]["mb_kind"] == "rg"

# --------------------------------------------------------------------------- #
# A shelf row IS the library's own album row plus the shelf's reason, not a
# reduced copy of it: the shared card (web/src/components/AlbumCard) reads the
# row's tracks, meta, cover, grade and audit, so a Home card that carried less
# would say less about an album than the Library does about the same one.
# --------------------------------------------------------------------------- #
lib_row = {
    "path": "C:/M/A/X", "cover_file": "cover.jpg", "media": "CD",
    "pass": True, "audit_summary": "REAL", "grade_pct": 100.0, "track_count": 1,
    "meta": {"ALBUM": "X", "ALBUMARTIST": "Alpha", "DATE": "1999"},
    "tracks": [{"path": "C:/M/A/X/1.flac", "file": "1.flac"}],
}
row = r._owned_row(lib_row, reason="Recently added")
assert row["reason"] == "Recently added" and row["owned"] is True, row
assert row["artist"] == "Alpha", row
assert row["meta"]["ALBUM"] == "X" and row["tracks"] == lib_row["tracks"], row
assert row["cover_file"] == "cover.jpg" and row["pass"] is True, row
assert row["audit_summary"] == "REAL" and row["grade_pct"] == 100.0, row
assert row["mbid"] is None and row["mb_kind"] == "rg", row
# …and nothing of the old reduced row: a second album shape is what let the
# two pages draw the same album differently.
assert {"album", "year", "cover"}.isdisjoint(row), sorted(row)

from server import wishes as wishes_mod

wishes_mod.list_wishes = lambda: [
    {"status": "searching", "title": "T", "artist": "Ar", "year": "1999",
     "release_mbid": "abc-123"},
    {"status": "imported", "title": "Done", "release_mbid": "zzz"},
]
wanted = r._wanted(5)
assert len(wanted) == 1, wanted
assert wanted[0]["mbid"] == "abc-123" and wanted[0]["mb_kind"] == "release"
assert wanted[0]["reason"] == "Searching Soulseek" and wanted[0]["owned"] is False
# A wish is NOT a library album: the row carries the identity a card draws and
# nothing that reads as a library fact, so no surface can claim the release was
# graded or has audio to play (the card keys that on `owned`).
assert wanted[0]["tracks"] == [], wanted[0]
assert wanted[0]["meta"] == {"ALBUM": "T", "ARTIST": "Ar", "DATE": "1999"}, wanted[0]
assert {"pass", "audit_summary", "grade_pct", "media"}.isdisjoint(wanted[0]), sorted(wanted[0])
# ------------------------------------------------------------------------- #
# The recommendation shelves are GONE: the Home payload carries library-derived
# data only, and no provider chain is consulted for it any more.
# ------------------------------------------------------------------------- #
_home = r.build_home({"music_folder": "C:/definitely-not-a-library"})
assert {"recommended", "popular", "rec_source"}.isdisjoint(_home), sorted(_home)
assert {"stats", "recent", "top_rated", "favorites", "discover", "top_artists",
        "wanted", "needs_attention", "rated"} <= set(_home), sorted(_home)
assert _home["stats"]["albums"] == 0 and _home["recent"] == [], _home["stats"]

# --------------------------------------------------------------------------- #
# The artist shelf draws the library's DISPLAY name — the naming script puts
# the MusicBrainz id in the folder name, and that is library identity, not a
# caption (the owner's shelf read "Radiohead [a74b1b7f-…]"). The picture flag
# rides along with the row so the client never asks for a URL that 404s.
# --------------------------------------------------------------------------- #
folders = [
    {"path": "C:/M/Radiohead [a74b1b7f-71a5-4011-9441-d0b5e4122711]",
     "name": "Radiohead [a74b1b7f-71a5-4011-9441-d0b5e4122711]",
     "display_name": "Radiohead",
     "albums": [{"path": "C:/M/A/X", "cover_file": "cover.jpg"}],
     "aggregate": {"track_count": 3}},
]
top = r._top_artists(folders, 6)
assert top[0]["artist"] == "Radiohead", top
assert top[0]["has_image"] is False, top  # a folder that is not there holds no picture

# --------------------------------------------------------------------------- #
# The rated shelf is the ratings store joined with the library's own album rows:
# the user's verdicts, best first, each row carrying the half-star value it was
# ranked by — so the order and the stars on the cards are one number. A value
# the store no longer holds (a cleared rating) is not a row, and a store that
# cannot be read loses the shelf rather than the page.
# --------------------------------------------------------------------------- #
from server import ratings as store

store.map_for = lambda **kw: {"C:/M/A/X": 6, "C:/M/A/Y": 9, "C:/M/A/Z": 0}
rated = r._rated(albums, user="", limit=5)
assert [x["path"] for x in rated] == ["C:/M/A/Y", "C:/M/A/X"], rated
assert [x["rating"] for x in rated] == [9, 6], rated
assert all(x["owned"] is True and x["artist"] == "" for x in rated), rated
assert r._rated(albums, user="", limit=1) == rated[:1]

store.map_for = lambda **kw: 1 / 0
assert r._rated(albums, user="", limit=5) == []
assert r._rated([], user="", limit=5) == []

# --------------------------------------------------------------------------- #
# The grading strip (server.recommendations.grade_warning — the object
# `GET /api/grades/summary` answers with and the Home payload carries as
# `grade_warning`, drawn by web/src/components/GradeWarning on both pages): the
# owner's shape for a finding, the three albums that are never one, the totals
# printed beside it, and the cap that keeps a strip from becoming a page.
# --------------------------------------------------------------------------- #
def _tr(path, **issues):
    return {"path": path, "file": path.rsplit("/", 1)[-1],
            "tags": {"TITLE": path.rsplit("/", 1)[-1]}, "issues": issues}


gw_lib = {"artists": [{"path": "C:/M/A", "name": "Alpha", "albums": [
    # ONE failing track: the finding is the TRACK, and the album is its frame.
    {"path": "C:/M/A/One", "pass": False, "pass_count": 9, "total_checks": 10,
     "grade_pct": 90.0, "meta": {"ALBUM": "One"},
     "tracks": [_tr("C:/M/A/One/1.flac", COVER="track")]},
    # TWO failing tracks: the finding is the ALBUM, with the count and the
    # union of their codes — a dozen rows of one album say less than its name.
    {"path": "C:/M/A/Two", "pass": False, "pass_count": 4, "total_checks": 10,
     "grade_pct": 40.0, "meta": {"ALBUM": "Two"},
     "tracks": [_tr("C:/M/A/Two/1.flac", COVER="track"),
                _tr("C:/M/A/Two/2.flac", AUDIT="track")]},
    # A failure recorded against the album itself names no file, so it is an
    # album row carrying the grader's own sentence.
    {"path": "C:/M/A/Three", "pass": False, "pass_count": 3, "total_checks": 4,
     "grade_pct": 75.0, "meta": {"ALBUM": "Three"}, "tracks": [],
     "issues": {"Missing MEDIA": ["album-wide"]}},
    # NOT findings: a passing album, a PENDING framework album (nothing was
    # graded because its audio has not arrived — listing it would report a wish
    # as a broken album) and an album with no checks (0 == 0 passes).
    {"path": "C:/M/A/Fine", "pass": True, "pass_count": 10, "total_checks": 10,
     "grade_pct": 100.0, "meta": {"ALBUM": "Fine"}, "tracks": []},
    {"path": "C:/M/A/Wait", "pass": False, "pending": True, "pass_count": 0,
     "total_checks": 1, "grade_pct": 0.0, "meta": {"ALBUM": "Wait"}, "tracks": []},
    {"path": "C:/M/A/Off", "pass": False, "pass_count": 0, "total_checks": 0,
     "grade_pct": None, "meta": {"ALBUM": "Off"}, "tracks": []},
]}]}
gw = r.grade_warning(gw_lib)
assert gw["ok"] is False and gw["albums_failing"] == 3, gw
assert gw["tracks_failing"] == 3, gw
# The totals are the SAME sums the Home header prints, so the strip and the
# percentage beside it cannot disagree: 26 of 35 checks.
assert (gw["pass_count"], gw["total_checks"], gw["grade_pct"]) == (26, 35, 74.3), gw
assert [(i["kind"], i["album"]) for i in gw["items"]] == \
    [("album", "Two"), ("album", "Three"), ("track", "One")], gw["items"]
two = gw["items"][0]
assert two["failing_tracks"] == 2 and two["codes"] == ["AUDIT", "COVER"], two
three = gw["items"][1]
assert three["reason"] == "Missing MEDIA" and three["failing_tracks"] == 0, three
one = gw["items"][2]
assert one["track_path"] == "C:/M/A/One/1.flac" and one["title"] == "1.flac", one
assert one["codes"] == ["COVER"] and "failing_tracks" not in one, one
# Worst first, by the same key for both kinds.
assert [i["grade_pct"] for i in gw["items"]] == [40.0, 75.0, 90.0], gw["items"]
# A library that passes says so, with nothing to list.
assert r.grade_warning({"artists": []})["ok"] is True
# The cap: 12 rows listed, the rest COUNTED — the Library's Failing filter is
# where a reader goes past a screenful.
many = {"artists": [{"path": "C:/M/A", "albums": [
    {"path": f"C:/M/A/A{n}", "pass": False, "pass_count": 0, "total_checks": 1,
     "grade_pct": float(n), "meta": {"ALBUM": f"A{n}"}, "tracks": [],
     "issues": {"album folder holds no audio": ["folder"]}} for n in range(20)]}]}
cap = r.grade_warning(many)
assert len(cap["items"]) == 12 and cap["more"] == 8, (len(cap["items"]), cap["more"])
assert cap["albums_failing"] == 20, cap["albums_failing"]
# An album a live JOB holds is not a finding either: a chain writes an album
# across its steps (the tag the next step has not reached yet is missing until
# it runs), so a strip that named a running album would be reporting the
# process. Both halves are pinned against a REAL claim in the registry — no job
# thread, just the claim — so the exclusion and the registry's containment rule
# are provably the same rule.
from server import job_locks as jl

with jl.holding(["C:/M/A/Two"], kind="beets", label="Beets tagging"):
    busy_gw = r.grade_warning(gw_lib)
assert busy_gw["albums_failing"] == 2 and busy_gw["tracks_failing"] == 1, busy_gw
assert [i["album"] for i in busy_gw["items"]] == ["Three", "One"], busy_gw["items"]
# …and the totals are STILL the whole library's, busy album included: they are
# the sums the header beside the strip prints, not the findings' own count.
assert (busy_gw["pass_count"], busy_gw["total_checks"], busy_gw["grade_pct"]) == \
    (26, 35, 74.3), busy_gw
# A claim on a FILE inside the album covers the album too — the job holding one
# of its tracks is writing that album — which is the registry's own containment
# rule, asked absolutely: this call runs inside the claim's own job context and
# must still see it.
with jl.holding(["C:/M/A/One/1.flac"], kind="lyrics", label="Lyrics"):
    inner_gw = r.grade_warning(gw_lib)
assert [i["album"] for i in inner_gw["items"]] == ["Two", "Three"], inner_gw["items"]
# The same library with no claim at all is the full three findings (`gw` above):
# the difference is the claim, not the fixture.
assert gw["ok"] is False and gw["albums_failing"] == 3, gw
# …and the Home payload carries THAT object, not a second count of the library.
assert _home["grade_warning"] == r.grade_warning({"artists": []}), _home["grade_warning"]

# --------------------------------------------------------------------------- #
# The percentage PRINTED beside a list of findings can never round the shortfall
# away. 10280 of 10281 checks is a library that FAILED a check, and at one
# decimal the old `round` turned it into `100.0`, so the strip named the album
# that failed and "100% of checks pass" in the same sentence. The rule the two
# halves now obey: exactly 100 is printed only when `pass_count` equals
# `total_checks` — by a library with no failed check at all.
# --------------------------------------------------------------------------- #
huge = {"artists": [{"path": "C:/M/A", "albums": [
    {"path": "C:/M/A/Huge", "pass": False, "pass_count": 10280,
     "total_checks": 10281, "grade_pct": 99.99, "meta": {"ALBUM": "Huge"},
     "tracks": [], "issues": {"Missing MEDIA": ["album-wide"]}}]}]}
huge_gw = r.grade_warning(huge)
assert huge_gw["ok"] is False and len(huge_gw["items"]) == 1, huge_gw
assert (huge_gw["pass_count"], huge_gw["total_checks"]) == (10280, 10281), huge_gw
assert huge_gw["grade_pct"] == 99.9, huge_gw["grade_pct"]
# …and the other side of the rule: a library whose every check passed is the
# only one that prints 100, and it is also the one with nothing to list.
whole = {"artists": [{"path": "C:/M/A", "albums": [
    {"path": "C:/M/A/Whole", "pass": True, "pass_count": 10281,
     "total_checks": 10281, "grade_pct": 100.0, "meta": {"ALBUM": "Whole"},
     "tracks": []}]}]}
whole_gw = r.grade_warning(whole)
assert whole_gw["ok"] is True and whole_gw["items"] == [], whole_gw
assert whole_gw["grade_pct"] == 100.0, whole_gw["grade_pct"]
# The one rule, over every shape built above: a finding listed means the printed
# percentage is below 100, and nothing listed means 100 — `None` when no check
# was graded at all (an install with every check switched off).
for _gw in (huge_gw, whole_gw, gw, cap, busy_gw, inner_gw,
            r.grade_warning({"artists": []})):
    if _gw["items"]:
        assert _gw["grade_pct"] is not None and _gw["grade_pct"] < 100.0, _gw
    else:
        assert _gw["ok"] is True and _gw["grade_pct"] in (100.0, None), _gw

# Two failing albums, for the plural the sentence is written for ("2 albums fall
# short of …").
two_lib = {"artists": [{"path": "C:/M/A", "albums": [
    {"path": "C:/M/A/One", "pass": False, "pass_count": 9, "total_checks": 10,
     "grade_pct": 90.0, "meta": {"ALBUM": "One"}, "tracks": [],
     "issues": {"Missing MEDIA": ["album-wide"]}},
    {"path": "C:/M/A/Two", "pass": False, "pass_count": 4, "total_checks": 10,
     "grade_pct": 40.0, "meta": {"ALBUM": "Two"}, "tracks": [],
     "issues": {"Missing cover image": ["album"]}}]}]}
two_gw = r.grade_warning(two_lib)
assert (two_gw["ok"], two_gw["albums_failing"]) == (False, 2), two_gw
# The owner's own library as the strip showed it: ONE failing track whose only
# issue is the AcoustID half pair, which the strip printed as the bare code
# `acoustid id` — naming what is missing and nothing a reader could do about it.
acoustid_lib = {"artists": [{"path": "C:/M/RATM", "name": "Rage Against the Machine",
    "albums": [{"path": "C:/M/RATM/Rage", "pass": False, "pass_count": 6,
                "total_checks": 7, "grade_pct": 85.7,
                "meta": {"ALBUM": "Rage Against the Machine",
                         "ALBUMARTIST": "Rage Against the Machine"},
                "tracks": [_tr("C:/M/RATM/Rage/05 - Fistful of Steel.flac",
                               ACOUSTID_FINGERPRINT="track")]}]}]}
acoustid_gw = r.grade_warning(acoustid_lib)
assert acoustid_gw["albums_failing"] == 1 and acoustid_gw["tracks_failing"] == 1, acoustid_gw
assert acoustid_gw["items"][0]["kind"] == "track", acoustid_gw["items"]
assert acoustid_gw["items"][0]["codes"] == ["ACOUSTID_FINGERPRINT"], acoustid_gw["items"]

# --------------------------------------------------------------------------- #
# What the strip SAYS, rendered: the sentence in front of the findings, and the
# words on a finding's own row. Those words live in the client, so the real
# component (web/src/components/GradeWarning.tsx) is rendered with the REAL
# library payloads built above — the same node + vite route the component checks
# in tools/ use, on the shipped module, and skipped (never failed) when the
# environment cannot run it.
# --------------------------------------------------------------------------- #
_STRIP_CASES = {"huge": huge_gw, "whole": whole_gw, "two": two_gw,
                "acoustid": acoustid_gw}

_STRIP_CHECK = r'''
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import path from "node:path";

const webDir = path.join(process.env.MLO_ROOT, "web");
if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[grade-strip] web/node_modules is missing");
  process.exit(2);
}
const webRequire = createRequire(path.join(webDir, "package.json"));
const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);
const React = webRequire("react");
const { renderToString } = webRequire("react-dom/server");
const { MemoryRouter } = webRequire("react-router-dom");
const { QueryClient, QueryClientProvider } = await import(pathToFileURL(
  path.join(webDir, "node_modules/@tanstack/react-query/build/modern/index.js")).href);

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  clear: () => store.clear(),
};
globalThis.window = globalThis;

const payload = JSON.parse(readFileSync(process.argv[2], "utf8"));
const failed = [];
const ok = (label, cond) => { if (!cond) failed.push(label); };

const server = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir, server: { middlewareMode: true }, appType: "custom", logLevel: "error",
});
try {
  const { default: GradeWarning } = await server.ssrLoadModule("/src/components/GradeWarning.tsx");
  // The strip with its answer already in hand (Home's own `grade_warning`, which
  // is what the pages pass as `initial`), rendered to text: React's text-node
  // markers and the markup itself are not content, so both go.
  const flat = (summary) => renderToString(
    React.createElement(QueryClientProvider, {
      client: new QueryClient({ defaultOptions: { queries: { retry: false } } }),
    },
      React.createElement(MemoryRouter, { initialEntries: ["/"] },
        React.createElement(GradeWarning, { initial: summary })))
  )
    .replace(/<!-- -->/g, "").replace(/<[^>]*>/g, "")
    .replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, "&")
    .replace(/\s+/g, " ").trim();

  const text = Object.fromEntries(Object.entries(payload).map(([k, v]) => [k, flat(v)]));

  // The owner's rule, asked of every shape: a strip that lists a finding cannot
  // contain a perfect-pass claim anywhere, and the percentage it prints beside
  // the list is below 100; a strip with nothing to list says so and no number.
  for (const [name, summary] of Object.entries(payload)) {
    if (summary.items.length) {
      ok(`${name}: the printed percentage is below 100 (${summary.grade_pct})`,
         summary.grade_pct < 100 && text[name].includes(`${summary.grade_pct}% of checks pass`));
      ok(`${name}: and nothing in the strip reads as a perfect pass`,
         !text[name].includes("All checks pass") && !/100(\.0)?% of checks pass/.test(text[name]));
    } else {
      ok(`${name}: nothing to list says All checks pass and no number`,
         text[name].includes("All checks pass") && !text[name].includes("% of checks pass"));
    }
  }
  ok("1 album falls short of the library's grading checks",
     text.huge.includes("1 album falls short of the library's grading checks"));
  ok("…and the same sentence in the plural",
     text.two.includes("2 albums fall short of the library's grading checks"));
  // The reason a reader is given for the half pair is the step that completes
  // it, not the bare code the strip used to open up ("acoustid fingerprint").
  ok("the AcoustID half pair names the step that completes it",
     text.acoustid.includes("AcoustID fingerprint missing (run Fix AcoustID pairs)")
     && !text.acoustid.includes("acoustid fingerprint"));
} finally {
  await server.close();
}

if (failed.length) {
  for (const label of failed) console.error(`[grade-strip] MISSING: ${label}`);
  process.exit(1);
}
console.log("ok  the strip's own words (4 payloads, 11 checks)");
'''

_strip_dir = tempfile.mkdtemp(prefix="mlo-grade-strip-")
try:
    _payload_path = os.path.join(_strip_dir, "grade-strip-cases.json")
    with open(_payload_path, "w", encoding="utf-8") as _f:
        json.dump(_STRIP_CASES, _f)
    _check_path = os.path.join(_strip_dir, "check_grade_strip.mjs")
    with open(_check_path, "w", encoding="utf-8") as _f:
        _f.write(_STRIP_CHECK)
    # 2 is the convention tools/check_*.mjs use for "this environment cannot run
    # me" (no node, no web/node_modules): a note, not a failure.
    _strip = subprocess.run(["node", _check_path, _payload_path], capture_output=True,
                            text=True, cwd=ROOT,
                            env=dict(os.environ, MLO_ROOT=ROOT))
    if _strip.returncode == 0:
        print("  ok   " + _strip.stdout.strip().removeprefix("ok  "))
    elif _strip.returncode == 2:
        print("  SKIP the strip's own words (no node or web/node_modules): "
              + (_strip.stderr.strip().splitlines() or [""])[0])
    else:
        raise AssertionError("the strip's own words: "
                             + (_strip.stdout + _strip.stderr).strip()[-2000:])
finally:
    shutil.rmtree(_strip_dir, ignore_errors=True)

print("ok")
