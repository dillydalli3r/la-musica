#!/usr/bin/env python3
"""Every automatic import step, and the option a person uses instead.

An import fetches metadata, links, cover art, genres, lyrics and advisory
ratings on its own. Each of those is also a thing a user must be able to do by
hand — from the wizard's step, from the tag-actions menu, from the entity
pages — and the two paths must not drift apart: same entry point, same tags,
same sidecars. `mlo.import_policy` keeps that table (`manual_options`), one row
per thing a step decides, and this suite is what reads it.

Pinned here:

  * every family in the registry names at least one manual option, every row's
    route is a route the app really serves (the app's own OpenAPI table, method
    included), and every `auto`/`service` entry point resolves;
  * the lyrics family covers its chain — fetch (script 13) and
    transliterate/translate (17) — and each of the two options runs the same
    entry point its own script runs, so a click and an import cannot write
    different things;
  * the tag targets are the same, proven on real files rather than argued: the
    manual fetch and script 13 write the same `LYRICS`/`USLT` tag and/or the
    same `.lrc` sidecar under each `lyrics_format`; and the manual
    transliteration pass and script 17 write the same `TRANSLITERATION-<lang>`
    tag and `.romaji.lrc` sidecar;
  * a provider that fails surfaces its own message, and a path the app may not
    touch is refused with the sentence the fetch has always used.

Nothing here touches the network or the developer's library: the music folder
and the config file are redirected into a temp directory before `server.main`
is imported, the lyrics provider and the AI/LRCLIB seams are stubbed, and the
audio fixtures are real FLACs built with the bundled encoder (the lyrics tag
and its sidecar are what this suite is about, so a fake tag store would prove
nothing).

Run: python tools/test_import_corrections.py   (exit 0 pass, 1 fail)
"""
import atexit
import contextlib
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: the app resolves its paths through the music folder, so the
# scope is redirected BEFORE anything imports server.main.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-corrections-redirect-")
MF = tempfile.mkdtemp(prefix="mlo-corrections-test-")
os.environ["MLO_MUSIC_FOLDER"] = MF

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as f:
    json.dump({"music_folder": MF, "lyrics_format": "EMBEDDED"}, f)
for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")

atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True)
                         for d in (REDIRECT, MF)])
atexit.register(lambda: os.environ.pop("MLO_MUSIC_FOLDER", None))

_REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/").lower()
assert not MF.replace("\\", "/").lower().startswith(_REAL or "\0"), \
    f"temp fixture {MF} sits inside the real music folder {REAL_MUSIC_FOLDER}"

pathmod._warn_if_temp_folder = lambda mf: None

import importlib  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from mlo import import_policy, lyrics_fetch, lyrics_xlit, web_ratings  # noqa: E402
from mlo.audio import AudioFile  # noqa: E402
from mlo.scripts import SCRIPT_LABELS  # noqa: E402
from server import api_imports, api_lyrics, script_runners  # noqa: E402
import server.main as mlo_main  # noqa: E402

# The routes under test, on their own app: the wizard's Lyrics step, the tag
# actions menu and the entity pages all call these, and nothing else here
# depends on how server/main.py mounts them.
APP = FastAPI()
APP.include_router(api_lyrics.router)
CLIENT = TestClient(APP)

# The app's own route table — one entry per path with its methods, so a row
# that names a route nobody serves (or the wrong verb) fails here.
PATHS = mlo_main.app.openapi()["paths"]

FAILED = []


def ok(cond, label, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{f' — {extra}' if extra else ''}")
        FAILED.append(label)


def eq(got, want, label):
    ok(got == want, label, f"got {got!r}, want {want!r}")


@contextlib.contextmanager
def patched(obj, **kw):
    """Set attributes for the block and restore them however it ends."""
    saved = {k: getattr(obj, k) for k in kw}
    for k, v in kw.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


def call(fn):
    """Set the routable config for one request and call a route with it."""
    CFG["music_folder"] = MF
    with patched(api_lyrics, load_config=lambda: dict(CFG)):
        return fn()


def resolve(entry):
    """`module:attr` from the registry, or None when it does not exist."""
    mod, _, attr = str(entry).partition(":")
    try:
        return getattr(importlib.import_module(mod), attr)
    except (ImportError, AttributeError):
        return None


# --------------------------------------------------------------------------- #
# audio fixtures: a real FLAC per case, built with the bundled encoder
# --------------------------------------------------------------------------- #
FLAC_EXE = ""
for _cand in sorted(glob.glob(os.path.join(ROOT, ".dependencies", "*", "flac.exe"))):
    FLAC_EXE = _cand
    break

CFG = {
    "music_folder": MF,
    "lyrics_format": "EMBEDDED",
    "lyrics_allow_plain": True,
    "lyrics_sources": ["lrclib"],
    "lyrics_xlit_enabled": True,
    "lyrics_translate_enabled": False,
    "lyrics_xlit_sidecars": True,
}


def make_flac(name, artist="System of a Down", title="Suite-Pee",
              album="System of a Down", lyrics=None, seconds=1.2):
    """One real FLAC track with the tags the lyrics chain reads."""
    assert FLAC_EXE, "flac.exe not found under .dependencies"
    path = os.path.join(MF, "Album", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wav = path + ".wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * int(44100 * seconds))
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                   check=True, capture_output=True)
    os.remove(wav)
    from mutagen.flac import FLAC
    f = FLAC(path)
    f["ARTIST"] = [artist]
    f["TITLE"] = [title]
    f["ALBUM"] = [album]
    if lyrics:
        f["LYRICS"] = [lyrics]
    f.save()
    return path


def tags_of(path):
    """What the file carries right now: the named tags this suite is about
    (read through the app's own reader) plus every raw key, so a before/after
    readout cannot miss a tag the reader does not name."""
    af = AudioFile(path)
    return {
        "LYRICS": af.get_lyrics() or "",
        "TRANSLITERATION": af.get_lyrics_transform("TRANSLITERATION") or "",
        "raw_keys": sorted(str(k) for k in (af.all_tags() or {})),
    }


def lrc_of(path):
    side = os.path.splitext(path)[0] + ".lrc"
    if not os.path.isfile(side):
        return None
    with open(side, "rb") as fh:
        return fh.read()


LYRIC_TEXT = "[00:01.00]Why did you leave us?\n[00:05.00]Turn around"


def stub_provider(text=LYRIC_TEXT):
    """The lyrics provider seam: one LRCLIB answer, whatever is asked.

    `fetch_one` calls `fetch_lyrics` imported into `mlo.lyrics_fetch`, so
    patching it there intercepts BOTH the manual route and script 13's runner
    (which is the point: neither path gets its own provider code).
    """
    def fetch(config, artist, title, album=None, duration=None, **kw):
        return {"provider": "lrclib", "provider_label": "LRCLIB",
                "synced": text, "plain": text.replace("[00:01.00]", "").replace("[00:05.00]", "")}
    return fetch


# --------------------------------------------------------------------------- #
# 1) the registry: every family has a manual option, and it is real
# --------------------------------------------------------------------------- #
print("== every family names a manual counterpart ==")
SURFACES = {"wizard", "tag-actions", "album-page", "artist-page", "track-page", "links-editor"}
seen_ids = set()

for fam in import_policy.FAMILIES:
    rows = import_policy.manual_options(fam["id"])
    ok(bool(rows), f"family '{fam['id']}' has a manual option")
    for row in rows:
        missing = [k for k in import_policy.MANUAL_KEYS if k not in row]
        ok(not missing, f"{row.get('id')} carries every field", f"missing {missing}")
        ok(row["id"] not in seen_ids, f"{row['id']} is unique in the table")
        seen_ids.add(row["id"])
        ok(bool(row["surface"]) and set(row["surface"]) <= SURFACES,
           f"{row['id']} says where a user finds it", str(row["surface"]))
        ok(isinstance(row["tags"], tuple) and isinstance(row["files"], tuple),
           f"{row['id']} declares its tags and sidecars as tuples")
        served = PATHS.get(row["route"])
        ok(served is not None, f"{row['id']} names a route the app serves",
           row["route"])
        if served:
            ok(row["method"].lower() in served,
               f"{row['id']} uses {row['method']} {row['route']}",
               "served methods: " + ",".join(sorted(served)))
        ok(resolve(row["auto"]) is not None,
           f"{row['id']} names an automatic path that exists", row["auto"])
        ok(resolve(row["service"]) is not None,
           f"{row['id']} names a manual entry point that exists", row["service"])

others = list(import_policy.OTHER_STEPS)
ok(all(row.get("family") is None for row in others) and len(others) >= 4,
   f"the steps no family owns are listed too ({len(others)})")
for row in others:
    ok(PATHS.get(row["route"]) is not None,
       f"{row['id']} names a route the app serves", row["route"])

flat = import_policy.manual_options()
ok(len(flat) == len(seen_ids) + len(others),
   f"table lists every family row plus the other steps ({len(flat)})")


# --------------------------------------------------------------------------- #
# 2) the lyrics family covers its whole chain, and the halves share code paths
# --------------------------------------------------------------------------- #
print("== the lyrics family is the whole chain ==")
lyric_rows = {row["id"]: row for row in import_policy.manual_options("lyrics")}
eq(sorted(lyric_rows), ["lyrics-fetch", "lyrics-xlit"],
   "the lyrics family offers fetch and transliterate")
for row in lyric_rows.values():
    eq(row["service"], row["auto"],
       f"{row['id']} runs the pipeline's own entry point")

family = import_policy.family("lyrics")
eq(sorted(family["codes"]), ["LYRICS", "XLIT_MISSING"],
   "the family's gap codes name both local halves")
eq(tuple(family["chain"]), (13, 17),
   "a lyrics review drops the fetch and the transliteration pass")
# Every family's own chain drops with it: the lyrics pair AND the web-rating
# script (24), which is the Ratings family's own chain id.
eq(sorted(import_policy.dropped_chain_ids({"import_autonomy": "review"})), [13, 17, 24],
   "review mode drops exactly those scripts")
eq([SCRIPT_LABELS[13], SCRIPT_LABELS[17]],
   ["Fetch lyrics", "Lyrics transliterate (AI)"],
   "the two scripts are the ones the options name")

# The scripts the chain runs ARE the entry points the table claims — the same
# object, so "the manual option calls what the import calls" is not a promise
# about two implementations.
ok(script_runners.RUNNERS[13][1] is lyrics_fetch.run_fetch_lyrics,
   "script 13 is mlo.lyrics_fetch.run_fetch_lyrics")
ok(script_runners.RUNNERS[17][1] is lyrics_xlit.run_lyrics_xlit,
   "script 17 is mlo.lyrics_xlit.run_lyrics_xlit")
ok(lyrics_fetch.run_fetch_lyrics.__globals__["fetch_one"] is lyrics_fetch.fetch_one,
   "script 13 books every track through fetch_one")
# The fetch route holds `fetch_one` itself (the module-level import every
# `from mlo.lyrics_fetch import fetch_one` shares), so a click and script 13
# are not two implementations that agree: they are one function.
ok(api_lyrics.fetch_one is lyrics_fetch.fetch_one,
   "the lyrics route holds the very function script 13 calls")

# --------------------------------------------------------------------------- #
# 2b) the Ratings family: right after Genres, owning script 24, and its route
#     runs the chain's own entry point for the ONE album it is asked about
# --------------------------------------------------------------------------- #
print("== the Ratings family sits right after Genres and owns script 24 ==")
_fam_ids = [f["id"] for f in import_policy.FAMILIES]
eq(_fam_ids[_fam_ids.index("ratings") - 1], "genres",
   "the ratings family sits right after genres in the wizard's step order")
_ratings = import_policy.family("ratings")
eq(tuple(_ratings["chain"]), (24,),
   "a ratings review drops script 24 from the chain")
eq(_ratings["off"], {"web_ratings_enabled": False},
   "the family's own switch is what 'do not gather this' means")
eq(sorted(_ratings["codes"]), ["WEBRATING_MISSING"],
   "the grader's code for a missing public score is the family's")
eq(sorted(import_policy.dropped_chain_ids({"import_autonomy": "review"})), [13, 17, 24],
   "review mode drops the lyrics pair AND the web-rating script")
_row = import_policy.manual_option("ratings-fetch")
eq((_row["route"], _row["method"]), ("/api/import/ratings", "POST"),
   "the manual option names the ratings route and verb")
eq(_row["auto"], "mlo.web_ratings:run_web_ratings",
   "the automatic counterpart is script 24's own runner")
eq(_row["service"], "server.api_imports:import_ratings",
   "the option calls the ratings route's handler")
eq(sorted(_row["tags"]),
   ["ALBUMWEBRATING", "ALBUMWEBRATING_SOURCE", "WEBRATING", "WEBRATING_SOURCE"],
   "the step owns both value pairs and their provenance tags")
ok(script_runners.RUNNERS[24][1] is web_ratings.run_web_ratings,
   "script 24 is mlo.web_ratings.run_web_ratings")

print("== the ratings route runs the chain's entry point for ONE album ==")
_rating_dir = os.path.join(MF, "Album")
_rating_track = make_flac("01 - rated.flac")
_rating_calls = []


def record_run_web_ratings(config, fetch=None):
    """The ONE callable the route must reach, recording its scope.

    It writes WEBRATING / WEBRATING_SOURCE the way the real writer does, so
    the rows the route reads BACK off the file are exercised, not argued."""
    _rating_calls.append({"targets": list(config.get("targets") or []),
                          "enabled": bool(config.get("web_ratings_enabled", True)),
                          "force": bool(config.get("force_web_ratings", False)),
                          "sources": config.get("web_ratings_sources")})
    from mutagen.flac import FLAC as _FLAC
    for name in sorted(os.listdir(_rating_dir)):
        if not name.lower().endswith(".flac"):
            continue
        f = _FLAC(os.path.join(_rating_dir, name))
        f["WEBRATING"] = ["89"]
        f["WEBRATING_SOURCE"] = ["MusicBrainz"]
        f.save()
    return {"modified_count": 1, "skipped_count": 0, "error_count": 0, "errors": [],
            "by_source": {"musicbrainz": 1}, "levels": {"track": 1},
            "album_level_artist": 0}


_RATING_CFG = {"music_folder": MF, "web_ratings_enabled": True,
               "web_ratings_sources": ["musicbrainz"], "manual_import_enabled": True}

with patched(web_ratings, run_web_ratings=record_run_web_ratings), \
        patched(cfgmod, load_config=lambda: dict(_RATING_CFG)):
    _res = api_imports.import_ratings(api_imports.RatingsRequest(path=_rating_dir))
eq(_rating_calls, [{"targets": [_rating_dir], "enabled": True, "force": False,
                    "sources": ["musicbrainz"]}],
   "the route scopes the run to THIS one album, with the configured sources")
eq(_res["enabled"], True, "the reply reports the switch the run read")
_rows = {os.path.basename(r["file"]): r for r in _res["rows"]}
eq(_rows["01 - rated.flac"]["value"], "89",
   "the reply reads the value back off the file")
eq((_rows["01 - rated.flac"]["level"], _rows["01 - rated.flac"]["source"]),
   ("track", "MusicBrainz"),
   "…and says which tag pair carries it and who wrote it")
eq(_res["counts"]["written"], 1, "the run's own counts ride along")

print("== the ratings rows read in (disc, track) order ==")
# The rows the step draws keep (disc, track) order — never the directory
# listing's own filename sort, which put disc 2 first on some rips (the
# report this fixes). A file whose NAME sorts first but whose TAGS say disc 2
# must come back second.
from mutagen.flac import FLAC as _OFLAC  # noqa: E402

_ord_dir = os.path.join(MF, "Album", "Order Test")
make_flac("Order Test/aa - second disc.flac")
make_flac("Order Test/zz - first disc.flac")
_second = _OFLAC(os.path.join(_ord_dir, "aa - second disc.flac"))
_second["DISCNUMBER"], _second["TRACKNUMBER"] = ["2"], ["1"]
_second.save()
_first = _OFLAC(os.path.join(_ord_dir, "zz - first disc.flac"))
_first["DISCNUMBER"], _first["TRACKNUMBER"] = ["1"], ["9"]
_first.save()
eq([os.path.basename(r["file"]) for r in api_imports._rating_rows(_ord_dir)],
   ["zz - first disc.flac", "aa - second disc.flac"],
   "disc 1 is listed before disc 2 whatever the file names sort like")
# With no disc tags at all, the "D-TT" file name is the fallback the wizard's
# own lists use — and the numbers are compared as NUMBERS ("2-9" before
# "2-10", where a plain string sort reads "2-10" first).
_n_dir = os.path.join(MF, "Album", "Order Names")
make_flac("Order Names/2-10 - ten.flac")
make_flac("Order Names/2-9 - nine.flac")
eq([os.path.basename(r["file"]) for r in api_imports._rating_rows(_n_dir)],
   ["2-9 - nine.flac", "2-10 - ten.flac"],
   "no tags: the D-TT names decide, and 9 comes before 10")

# The switch off is REPORTED, not answered as an empty run — and the route
# still asks the chain's entry point (which itself refuses; see its docstring).
_rating_calls.clear()
with patched(web_ratings, run_web_ratings=record_run_web_ratings), \
        patched(cfgmod, load_config=lambda: {**_RATING_CFG, "web_ratings_enabled": False}):
    _off = api_imports.import_ratings(api_imports.RatingsRequest(path=_rating_dir))
eq(_off["enabled"], False, "the reply says the switch is off")
eq(_rating_calls, [{"targets": [_rating_dir], "enabled": False, "force": False,
                    "sources": ["musicbrainz"]}],
   "…and the run was handed the off config rather than skipped silently")

# The one-shot Force (the "24 · Web ratings re-fetch" control) is honoured: the
# run is handed `force_web_ratings`, the key script 24 and the Force menu read,
# which is what lets a re-fetch replace a value instead of filling only.
_rating_calls.clear()
with patched(web_ratings, run_web_ratings=record_run_web_ratings), \
        patched(cfgmod, load_config=lambda: dict(_RATING_CFG)):
    api_imports.import_ratings(
        api_imports.RatingsRequest(path=_rating_dir, force=True))
eq([c["force"] for c in _rating_calls], [True],
   "a forced press hands script 24 `force_web_ratings`")

print("== a rated album is no ratings gap, and the prompt lands on Ratings ==")
# Only the ratings family is graded here (the other families' checks are off),
# so the assertions are about THIS family and nothing else.
_GAP_CFG = {
    "music_folder": MF, "web_ratings_enabled": True, "grade_check_web_rating": True,
    "grade_check_mb_links": False, "grade_check_source": False,
    "grade_check_cover": False, "grade_check_genre": False,
    "grade_check_genre_count": False, "grade_check_genre_order": False,
    "grade_check_genre_vocab": False, "grade_check_lyrics": False,
    "grade_check_xlit_transliteration": False, "grade_check_xlit_translation": False,
    "grade_check_missing_tags": False, "import_autonomy": "automatic",
    "import_review_families": [],
}
_g_rated = import_policy.gaps(_rating_dir, _GAP_CFG)
ok("ratings" not in _g_rated,
   f"a track carrying WEBRATING is no ratings gap ({sorted(_g_rated)})")
# The value IS what the family is about: take it off the file and the family
# appears — the check cannot be a constant.
from mutagen.flac import FLAC as _RFLAC  # noqa: E402
_rated_flac = _RFLAC(_rating_track)
for _key in ("WEBRATING", "WEBRATING_SOURCE"):
    if _key in _rated_flac:
        del _rated_flac[_key]
_rated_flac.save()
_g_bare = import_policy.gaps(_rating_dir, _GAP_CFG)
ok("ratings" in _g_bare, f"a track with no WEBRATING is a ratings gap ({sorted(_g_bare)})")
eq(_g_bare["ratings"]["step"], "Ratings", "the gap names the Ratings step")
_link = import_policy.wizard_link(_rating_dir, ("ratings",))
ok(_link.endswith("&step=Ratings&missing=ratings"),
   f"the family's prompt link lands on the Ratings step ({_link})")

# --------------------------------------------------------------------------- #
# 3) the manual routes go through those same entry points
# --------------------------------------------------------------------------- #
print("== the routes call the pipeline's entry points, not a copy ==")
manual_flac = make_flac("01 - manual.flac")
auto_flac = make_flac("01 - auto.flac")


# One recorder per entry point, keeping what the CALLER handed it: the point is
# that the manual route and the automatic runner reach the SAME object with the
# same arguments, not that two implementations agree today. `replace` is the
# manual-only override (a run never replaces stored words — R#): the chain must
# not pass it, the route passes its own `force` through it.
def record_fetch(calls):
    def fetch_one(path, config, replace=False, mark_absent=True):
        calls.append({"path": os.path.basename(path), "replace": replace,
                      "mark_absent": mark_absent})
        return {"path": path, "status": "skipped", "provider": None,
                "provider_label": None, "synced": False,
                "wrote": {"embedded": False, "lrc": None},
                "reason": "recorded", "error": ""}
    return fetch_one


def record_xlit(calls):
    def run_lyrics_xlit(config):
        calls.append({"targets": [os.path.basename(p) for p in config.get("targets") or []],
                      "force": bool(config.get("force_xlit"))})
        return {"modified_count": 0, "unchanged_count": len(config.get("targets") or []),
                "error_count": 0, "errors": []}
    return run_lyrics_xlit


fetch_calls, xlit_calls = [], []

fetch_recorder = record_fetch(fetch_calls)
# Both bindings of the one function: the route holds its own reference, script
# 13's runner reads the module's. Patching the module alone would prove nothing
# about the route — and vice versa.
with patched(lyrics_fetch, fetch_one=fetch_recorder), patched(api_lyrics, fetch_one=fetch_recorder):
    lyrics_fetch.run_fetch_lyrics({**CFG, "targets": [auto_flac]})
    chain = list(fetch_calls)
    fetch_calls.clear()
    route = call(lambda: CLIENT.post("/api/lyrics/auto", json={"paths": [manual_flac]}))
    ok(route.status_code == 200, "POST /api/lyrics/auto answers", str(route.status_code))
    eq(fetch_calls, [{"path": os.path.basename(manual_flac), "replace": False,
                      "mark_absent": False}],
       "the route hands fetch_one the track the caller named, with the "
       "no-lyrics answer DEFERRED to the batch's one pass")
    eq([c["replace"] for c in fetch_calls], [c["replace"] for c in chain],
       "the route and script 13 fetch through one entry point (the route's "
       "`force` IS `replace`; a run never replaces)")
    eq([c["mark_absent"] for c in fetch_calls], [False],
       "…and both DEFER the no-lyrics answer (the batch settles it once, R413)")
    print(f"        cross-check: manual={fetch_calls} chain={chain}")

with patched(lyrics_xlit, run_lyrics_xlit=record_xlit(xlit_calls)):
    lyrics_xlit.run_lyrics_xlit({**CFG, "targets": [auto_flac]})
    chain = list(xlit_calls)
    xlit_calls.clear()
    route = call(lambda: CLIENT.post("/api/lyrics/xlit", json={"paths": [manual_flac]}))
    ok(route.status_code == 200, "POST /api/lyrics/xlit answers", str(route.status_code))
    eq(xlit_calls, [{"targets": [os.path.basename(manual_flac)], "force": False}],
       "the transliteration route calls script 17's runner with the selection")
    eq([c["force"] for c in xlit_calls], [c["force"] for c in chain],
       "the route and script 17 run one implementation")
    print(f"        cross-check: manual={xlit_calls} chain={chain}")


# --------------------------------------------------------------------------- #
# 4) the tag targets, on real files: manual == automatic, per format
# --------------------------------------------------------------------------- #
print("== the manual option writes what the automatic one writes ==")
for fmt in ("EMBEDDED", "LRC", "BOTH"):
    a = make_flac(f"02 - {fmt} auto.flac")
    m = make_flac(f"02 - {fmt} manual.flac")
    before = tags_of(m)
    CFG["lyrics_format"] = fmt
    with patched(lyrics_fetch, fetch_lyrics=stub_provider()):
        auto_res = lyrics_fetch.run_fetch_lyrics({**CFG, "targets": [a]})
        resp = call(lambda: CLIENT.post("/api/lyrics/auto", json={"paths": [m]})).json()
    ok(auto_res.get("modified_count") == 1 and resp.get("ok") == 1,
       f"{fmt}: both paths wrote lyrics", f"{auto_res} / {resp}")
    eq(tags_of(a)["LYRICS"], tags_of(m)["LYRICS"],
       f"{fmt}: the LYRICS tag is byte-identical on both paths")
    eq(lrc_of(a), lrc_of(m), f"{fmt}: the .lrc sidecar is identical on both paths")
    if fmt == "EMBEDDED":
        write_embedded, write_sidecar = True, False
    elif fmt == "LRC":
        write_embedded, write_sidecar = False, True
    else:
        write_embedded, write_sidecar = True, True
    ok(bool(tags_of(m)["LYRICS"]) == write_embedded and (lrc_of(m) is not None) == write_sidecar,
       f"{fmt}: writes the tag/sidecar the format asks for")
    ok(not before["LYRICS"], f"{fmt}: the fixture started without lyrics")
    if fmt == "EMBEDDED":
        print(f"        before: LYRICS={before['LYRICS']!r}")
        print(f"        after : LYRICS={tags_of(m)['LYRICS']!r}")

# The transliteration pass owns two storage places and the lyrics format
# decides which: the language-suffixed tag alone with EMBEDDED lyrics, the
# .romaji.lrc sidecar (and no tag) with LRC — the same split script 17 makes.
def xlit_pair(fmt, name):
    """Run the pass by hand and through the chain on two identical tracks."""
    CFG["lyrics_format"] = fmt
    auto_file = make_flac(f"03 - {fmt} {name} auto.flac", lyrics=LYRIC_TEXT)
    manual_file = make_flac(f"03 - {fmt} {name} manual.flac", lyrics=LYRIC_TEXT)
    cfg = {**CFG, "lyrics_xlit_enabled": True, "lyrics_translate_enabled": False,
           "lyrics_xlit_sidecars": True}
    with patched(lyrics_xlit,
                 ai_ready=lambda c: True,
                 xlit_needs=lambda t, c, declared="": {"transliteration": True, "translation": False, "langs": []},
                 _apply=lambda c, t, mode, lang="": (f"[romaji] {t.splitlines()[0]}", True)):
        auto_res = lyrics_xlit.run_lyrics_xlit({**cfg, "targets": [auto_file]})
        resp = call(lambda: CLIENT.post("/api/lyrics/xlit",
                                        json={"paths": [manual_file]})).json()
    return auto_file, manual_file, auto_res, resp


for fmt, expect_tag, expect_sidecar in (("EMBEDDED", True, False), ("LRC", False, True)):
    a, m, auto_res, resp = xlit_pair(fmt, "xlit")
    before = {"TRANSLITERATION": "", "raw_keys": []}
    ok(auto_res.get("transliterated") == 1 and resp.get("ok") == 1,
       f"{fmt}: both paths wrote a transliteration", f"{auto_res} / {resp}")
    eq(tags_of(a)["TRANSLITERATION"], tags_of(m)["TRANSLITERATION"],
       f"{fmt}: the transliteration text is identical on both paths")
    suffix = [k for k in tags_of(m)["raw_keys"] if str(k).upper().startswith("TRANSLITERATION-")]
    ok(bool(suffix) == expect_tag,
       f"{fmt}: writes the language-suffixed tag the grader reads", str(tags_of(m)["raw_keys"]))
    romaji = os.path.splitext(m)[0] + ".romaji.lrc"
    ok(os.path.isfile(romaji) == expect_sidecar,
       f"{fmt}: writes the .romaji.lrc sidecar only where the format asks")
    if expect_sidecar:
        eq(open(os.path.splitext(a)[0] + ".romaji.lrc", "rb").read(),
           open(romaji, "rb").read(),
           f"{fmt}: the sidecar is byte-identical on both paths")
    if expect_tag:
        shown = suffix[0] if suffix else " ".join(tags_of(m)["raw_keys"])
        print(f"        after : {shown}={tags_of(m)['TRANSLITERATION']!r}")
    else:
        print(f"        after : {os.path.basename(romaji)}={open(romaji, encoding='utf-8').read()!r}")


# --------------------------------------------------------------------------- #
# 5) failures and refusals say what happened
# --------------------------------------------------------------------------- #
print("== a failure is reported, never swallowed ==")


def exploding(*a, **kw):
    raise RuntimeError("LRCLIB said 500")


with patched(lyrics_fetch, fetch_lyrics=exploding):
    resp = call(lambda: CLIENT.post("/api/lyrics/auto",
                                    json={"paths": [make_flac("05 - boom.flac")]})).json()
failed = resp["results"][0]
eq(failed["status"], "failed", "a provider that raises is a failed track")
ok("LRCLIB said 500" in failed["error"],
   "the provider's own message reaches the reply", failed["error"])
print(f"        reported: {failed['error']}")

with patched(lyrics_xlit,
             ai_ready=lambda cfg: True,
             xlit_needs=lambda text, cfg, declared="": {"transliteration": True, "translation": False, "langs": []},
             _apply=lambda cfg, text, mode, lang="": (_ for _ in ()).throw(RuntimeError("no AI key"))):
    resp = call(lambda: CLIENT.post("/api/lyrics/xlit",
                                    json={"paths": [make_flac("05 - xlit.flac", lyrics=LYRIC_TEXT)]})).json()
ok(resp["failed"] >= 1 and "no AI key" in resp["errors"][0],
   "the transliteration pass reports its own per-file error", str(resp["errors"]))

resp = call(lambda: CLIENT.post("/api/lyrics/auto",
                                json={"paths": [os.path.join(tempfile.gettempdir(), "outside.flac")]})).json()
ok(resp["failed"] == 1 and resp["results"][0]["error"] == "path outside music folder",
   "a path outside the library is refused with the fetch's own sentence")
with patched(api_lyrics, load_config=lambda: {**CFG, "lyrics_xlit_enabled": False,
                                              "lyrics_translate_enabled": False}):
    resp = CLIENT.post("/api/lyrics/xlit", json={"paths": [m]}).json()
ok("off in Settings" in resp["note"],
   "a pass that wrote nothing says why (its switches are off)", resp["note"])

# --------------------------------------------------------------------------- #
# the verdict
# --------------------------------------------------------------------------- #
print()
if FAILED:
    print(f"FAILED {len(FAILED)} check(s):")
    for label in FAILED:
        print(f"  - {label}")
    sys.exit(1)
print("All import-corrections checks passed.")
