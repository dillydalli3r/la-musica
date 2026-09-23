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
  * the lyrics family covers its WHOLE chain — fetch (script 13),
    transliterate/translate (17) and publish to LRCLIB (18) — and each of the
    three options runs the same entry point its own script runs, so a click and
    an import cannot write different things;
  * the tag targets are the same, proven on real files rather than argued: the
    manual fetch and script 13 write the same `LYRICS`/`USLT` tag and/or the
    same `.lrc` sidecar under each `lyrics_format`; the manual transliteration
    pass and script 17 write the same `TRANSLITERATION-<lang>` tag and
    `.romaji.lrc` sidecar; and the publish writes NO local tag at all — it is
    the chain's only outward step;
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

from mlo import import_policy, lyrics_fetch, lyrics_publish, lyrics_xlit  # noqa: E402
from mlo.audio import AudioFile  # noqa: E402
from mlo.cli import SCRIPT_LABELS  # noqa: E402
from server import api_lyrics, script_runners  # noqa: E402
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
    """One real FLAC track with the tags the lyrics chain reads.

    A second long, well past the whole-second duration LRCLIB insists on — a
    0.1 s fixture has `duration 0` and publishing skips it as "no track
    duration", which is true but not what these cases are about."""
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
eq(sorted(lyric_rows), ["lyrics-fetch", "lyrics-publish", "lyrics-xlit"],
   "the lyrics family offers fetch, transliterate and publish")
for row in lyric_rows.values():
    eq(row["service"], row["auto"],
       f"{row['id']} runs the pipeline's own entry point")

family = import_policy.family("lyrics")
eq(sorted(family["codes"]), ["LYRICS", "XLIT_MISSING"],
   "the family's gap codes name both local halves")
eq(tuple(family["chain"]), (13, 17),
   "a lyrics review drops the fetch and the transliteration pass")
eq(sorted(import_policy.dropped_chain_ids({"import_autonomy": "review"})), [13, 17],
   "review mode drops exactly those scripts")
eq([SCRIPT_LABELS[13], SCRIPT_LABELS[17], SCRIPT_LABELS[18]],
   ["Fetch lyrics", "Lyrics transliterate (AI)", "Publish lyrics (LRCLIB)"],
   "the three scripts are the ones the options name")

# The scripts the chain runs ARE the entry points the table claims — the same
# object, so "the manual option calls what the import calls" is not a promise
# about two implementations.
ok(script_runners.RUNNERS[13][1] is lyrics_fetch.run_fetch_lyrics,
   "script 13 is mlo.lyrics_fetch.run_fetch_lyrics")
ok(script_runners.RUNNERS[17][1] is lyrics_xlit.run_lyrics_xlit,
   "script 17 is mlo.lyrics_xlit.run_lyrics_xlit")
ok(script_runners.RUNNERS[18][1] is lyrics_publish.run_publish_lyrics,
   "script 18 is mlo.lyrics_publish.run_publish_lyrics")
ok(lyrics_fetch.run_fetch_lyrics.__globals__["fetch_one"] is lyrics_fetch.fetch_one,
   "script 13 books every track through fetch_one")
ok(lyrics_publish.run_publish_lyrics.__globals__["publish_one"] is lyrics_publish.publish_one,
   "script 18 books every track through publish_one")
# The fetch route holds `fetch_one` itself (the module-level import every
# `from mlo.lyrics_fetch import fetch_one` shares), so a click and script 13
# are not two implementations that agree: they are one function.
ok(api_lyrics.fetch_one is lyrics_fetch.fetch_one,
   "the lyrics route holds the very function script 13 calls")

# --------------------------------------------------------------------------- #
# 3) the manual routes go through those same entry points
# --------------------------------------------------------------------------- #
print("== the routes call the pipeline's entry points, not a copy ==")
manual_flac = make_flac("01 - manual.flac")
auto_flac = make_flac("01 - auto.flac")


# One recorder per entry point, keeping what the CALLER handed it: the point is
# that the manual route and the automatic runner reach the SAME object with the
# same arguments, not that two implementations agree today.
def record_fetch(calls):
    def fetch_one(path, config, force=False):
        calls.append({"path": os.path.basename(path), "force": force})
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


def record_publish(calls):
    def publish_one(path, config, force=False):
        calls.append({"path": os.path.basename(path), "force": force})
        return {"path": path, "status": "skipped", "reason": "recorded",
                "message": "", "synced": False}
    return publish_one


fetch_calls, xlit_calls, publish_calls = [], [], []

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
    eq(fetch_calls, [{"path": os.path.basename(manual_flac), "force": False}],
       "the route hands fetch_one the track the caller named")
    eq([c["force"] for c in fetch_calls], [c["force"] for c in chain],
       "the route and script 13 fetch through one entry point, force included")
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

with patched(lyrics_publish, publish_one=record_publish(publish_calls)):
    lyrics_publish.run_publish_lyrics({**CFG, "targets": [auto_flac]})
    chain = list(publish_calls)
    publish_calls.clear()
    route = call(lambda: CLIENT.post("/api/lyrics/publish-batch",
                                     json={"paths": [manual_flac]}))
    ok(route.status_code == 200, "POST /api/lyrics/publish-batch answers", str(route.status_code))
    eq(publish_calls, [{"path": os.path.basename(manual_flac), "force": False}],
       "the publish route calls script 18's per-track core")
    eq([c["force"] for c in publish_calls], [c["force"] for c in chain],
       "the route and script 18 publish through one implementation")
    print(f"        cross-check: manual={publish_calls} chain={chain}")


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

a = make_flac("04 - publish auto.flac", lyrics=LYRIC_TEXT)
m = make_flac("04 - publish manual.flac", lyrics=LYRIC_TEXT)
before = tags_of(m)
sent = []


def fake_publish(artist, title, album, duration, plain=None, synced=None):
    sent.append({"artist": artist, "title": title, "album": album,
                 "duration": duration, "plain": plain, "synced": synced})
    return True, "Published"


with patched(lyrics_publish,
             lrclib_fetch=lambda *a_, **kw: None,     # LRCLIB does not have it
             lrclib_publish=fake_publish):
    auto_res = lyrics_publish.run_publish_lyrics({**CFG, "targets": [a]})
    auto_sent = list(sent)
    sent.clear()
    resp = call(lambda: CLIENT.post("/api/lyrics/publish-batch",
                                    json={"paths": [m]})).json()
    manual_sent = list(sent)
ok(auto_res.get("published") == 1 and resp.get("ok") == 1,
   "both paths submitted the track", f"{auto_res} / {resp}")
eq(len(manual_sent), 1, "the route submitted exactly one track")
eq(manual_sent, auto_sent, "the submit payload is the same on both paths")
if manual_sent:
    eq(manual_sent[0]["synced"], LYRIC_TEXT, "publishing sends the stored lyrics unchanged")
    eq(manual_sent[0]["title"], "Suite-Pee", "the submission is keyed by the file's own tags")
eq(tags_of(m), before, "publishing writes NO local tag (the one outward step)")


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

with patched(lyrics_publish,
             lrclib_fetch=lambda *a_, **kw: None,
             lrclib_publish=lambda *a_, **kw: (False, "LRCLIB already has this track")):
    resp = call(lambda: CLIENT.post("/api/lyrics/publish-batch",
                                    json={"paths": [make_flac("05 - dup.flac", lyrics=LYRIC_TEXT)]})).json()
got = resp["results"][0]
eq(got["status"], "skipped", "a duplicate is the database's answer, not a failure")
ok("already has this track" in got["reason"],
   "LRCLIB's own words are reported", got["reason"])

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
