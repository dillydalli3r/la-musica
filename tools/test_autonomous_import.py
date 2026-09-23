#!/usr/bin/env python3
"""Autonomous import contract (mlo.import_policy + server.import_autonomy).

Run:  python tools/test_autonomous_import.py

Every source is stubbed (no network, no script chain): what is under test is
the POLICY, not the fetchers.

  * automatic (the default): an album whose sources answer every family ends
    with no prompt at all;
  * automatic: an album no source can finish runs the whole chain, lands in
    the library, and produces exactly ONE prompt naming the two families it is
    missing — cover art and advisory;
  * automatic: a family listed in `import_review_families` is never decided for
    the user, while the rest of the import stays automatic;
  * review: the same album stops before the first family that needs a
    decision (cover), and no tag-writing chain runs;
  * the prompt's wizard link carries the album, the step and the missing
    families.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import import_policy
from mlo.config import DEFAULT_CONFIG, DEFAULT_RUN_ALL_ORDER, normalize_config
from server import events, import_autonomy, imports, integrations, script_runners

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLAC_EXE = None
_deps = os.path.join(ROOT, ".dependencies")
if os.path.isdir(_deps):
    for entry in sorted(os.listdir(_deps)):
        if entry.lower().startswith("flac"):
            cand = os.path.join(_deps, entry, "flac.exe")
            if os.path.isfile(cand):
                FLAC_EXE = cand
                break
assert FLAC_EXE, "flac.exe not found under .dependencies"

TMP = tempfile.mkdtemp(prefix="mlo_autonomous_import_")
MF = os.path.join(TMP, "music")
LIB = os.path.join(MF, "Artists")
os.makedirs(LIB)

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


# A 1x1 PNG built here rather than pasted: the grader opens the album cover
# when Pillow is installed, so the test's artwork has to be a real image.
def _png1x1():
    import struct
    import zlib

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat)
            + chunk(b"IEND", b""))


PNG_1PX = _png1x1()


def make_flac(path):
    wav = path + ".wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 4410)
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                   check=True, capture_output=True)
    os.remove(wav)


def set_tags(path, tags):
    from mutagen.flac import FLAC
    f = FLAC(path)
    for k, v in tags.items():
        f[k] = [v]
    f.save()


def make_album(name, *, links, cover, advisory, cover_now=False, advisory_now=False,
               genre=True):
    """An album folder in the library with everything but cover/advisory set.

    `links` is what the album ARRIVED with; `cover`/`advisory` are what the
    sources will answer (recorded in ANSWERS, answered by the stubs below).
    `*_now` writes that family into the album up front instead, which is what
    an album that needs no decision at all looks like.
    """
    album = os.path.join(LIB, name)
    os.makedirs(album)
    if cover_now:
        with open(os.path.join(album, "cover.png"), "wb") as fh:
            fh.write(PNG_1PX)
    for i in (1, 2):
        path = os.path.join(album, f"{i:02d} - Track.flac")
        make_flac(path)
        tags = {
            "TITLE": f"Track {i}",
            "ARTIST": "Test Artist",
            "ALBUMARTIST": "Test Artist",
            "ALBUM": name,
            "DATE": "2020-01-01",
            "TRACKNUMBER": str(i),
            "INSTRUMENTAL": "0",
            "LYRICS": "la la la la",
        }
        if genre:
            tags["GENRE"] = "Shoegaze"
        if advisory_now:
            tags["ITUNESADVISORY"] = "0"
        if links:
            # What a MusicBrainz-driven import arrives with: the release id and
            # the links the release stamping already resolved.
            tags["MUSICBRAINZ_ALBUMID"] = "11111111-1111-1111-1111-111111111111"
            tags["RATEYOURMUSIC_ALBUM"] = "https://rateyourmusic.com/release/album/x/"
        set_tags(path, tags)
    ANSWERS[name] = {"cover": cover, "advisory": advisory, "links": links}
    return album


ANSWERS = {}

# --------------------------------------------------------------------------- #
# The stubbed sources
# --------------------------------------------------------------------------- #
_chain_calls = []
_step_calls = []
_real = {n: getattr(imports, n) for n in
         ("stamp_rym_links", "fetch_advisories", "fetch_instrumentals",
          "run_metadata_step", "run_cover_step", "_stamp_release")}
_real_chain = script_runners.run_chain


def stub_stamp(album_dir, cfg):
    name = os.path.basename(album_dir)
    _step_calls.append(("links", name, dict(cfg)))
    if (ANSWERS.get(name) or {}).get("links"):
        for f in sorted(os.listdir(album_dir)):
            if f.lower().endswith(".flac"):
                set_tags(os.path.join(album_dir, f),
                         {"RATEYOURMUSIC_ALBUM": "https://rateyourmusic.com/release/album/x/"})
        return {"album": "https://rateyourmusic.com/release/album/x/", "artist": None, "note": ""}
    return {"album": None, "artist": None, "note": "no RYM page found"}


def stub_advisory(paths, cfg):
    name = os.path.basename(str(paths[0]))
    _step_calls.append(("advisory", name, dict(cfg)))
    if not cfg.get("advisory_auto_fetch", True):
        return {"updated": 0, "values": {}, "sources": {}, "answers": {},
                "skipped": "advisory_auto_fetch is off"}
    if not (ANSWERS.get(name) or {}).get("advisory"):
        # No provider states one, the AI cannot tell and the ladder is set to
        # write nothing: the real shape of "nothing was decided".
        return {"updated": 0, "values": {}, "sources": {}, "answers": {}}
    values = {}
    for base, _dirs, files in os.walk(str(paths[0])):
        for f in sorted(files):
            if f.lower().endswith(".flac"):
                p = os.path.join(base, f)
                set_tags(p, {"ITUNESADVISORY": "0"})
                values[p] = 0
    return {"updated": len(values), "values": values, "sources": {}, "answers": {}}


def stub_instrumentals(paths, cfg):
    return {"updated": 0, "values": {}, "evidence": {}}


def stub_metadata(album_dir, cfg):
    _step_calls.append(("metadata", os.path.basename(album_dir), dict(cfg)))
    return {"staged": False, "applied": {}}


def stub_cover(album_dir, cfg):
    name = os.path.basename(album_dir)
    _step_calls.append(("cover", name, dict(cfg)))
    if not cfg.get("cover_auto_fetch", True):
        return {"fetched": False, "applied": {}, "source": None, "note": "",
                "staged": False, "candidates": 0}
    if not (ANSWERS.get(name) or {}).get("cover"):
        return {"fetched": False, "applied": {}, "source": None, "note": "no cover found",
                "staged": False, "candidates": 0}
    if cfg.get("cover_review", False):
        # A cover the user asked to pick: the candidates are staged, nothing is
        # written — exactly what the real step does with cover_review on.
        return {"fetched": False, "applied": {}, "source": "stub", "staged": True,
                "candidates": 2, "note": "2 cover candidates to pick from"}
    with open(os.path.join(album_dir, "cover.png"), "wb") as fh:
        fh.write(PNG_1PX)
    return {"fetched": True, "applied": {"cover": os.path.join(album_dir, "cover.png")},
            "source": "stub", "note": "cover fetched from stub", "staged": False,
            "candidates": 0}


_resolve_calls = []
_real_resolve = integrations.resolve_release


def stub_resolve(mbid):
    """`integrations.resolve_release` — the release a stamped MBID names.

    The genres step resolves the release itself when its caller did not hand
    one over (the wizard and the bulk paths do not carry one), and the real call
    is a MusicBrainz lookup; the test answers it here and records the id, so a
    case can tell the two routes apart.
    """
    _resolve_calls.append(mbid)
    return ({"id": mbid, "title": "Test Album",
             "release_group_id": "22222222-2222-2222-2222-222222222222",
             "artists": [{"name": "Test Artist"}]}, mbid)


def stub_genres(album_dir, release, cfg):
    """The genres family's own action (`imports._stamp_release`).

    The ONE step that FETCHES a genre — recorded like the others so a test can
    say whether the import asked for genres at all. It writes GENRE, which is
    what the real step does with the chain's answer (and why the family is no
    longer missing afterwards).
    """
    name = os.path.basename(album_dir)
    _step_calls.append(("genres", name, dict(cfg)))
    if not cfg.get("genre_autofill", True):
        return (0, 0)
    written = 0
    for f in sorted(os.listdir(album_dir)):
        if f.lower().endswith(".flac"):
            set_tags(os.path.join(album_dir, f), {"GENRE": "Shoegaze"})
            written += 1
    return (written, 0)


def stub_chain(cfg, ids, targets=None, force=None, progress=None, wait=True,
               timeout=None, final=None):
    _chain_calls.append({"name": os.path.basename((targets or [""])[0]),
                         "chain": list(ids), "targets": list(targets or []),
                         "cfg": dict(cfg)})
    return []


for _name, _fn in (("stamp_rym_links", stub_stamp), ("fetch_advisories", stub_advisory),
                   ("fetch_instrumentals", stub_instrumentals),
                   ("run_metadata_step", stub_metadata), ("run_cover_step", stub_cover),
                   ("_stamp_release", stub_genres)):
    setattr(imports, _name, _fn)
integrations.resolve_release = stub_resolve
script_runners.run_chain = stub_chain


def prompts_for(album):
    """The prompts `prompts(CFG)` holds for ONE album (paths normalized as the
    store normalizes them), so a count is about this album and not about every
    case that ran before it."""
    key = str(album).replace("\\", "/").lower()
    return [p for p in import_autonomy.prompts(CFG)
            if str(p.get("album") or "").replace("\\", "/").lower() == key]


def chain_of(name):
    calls = [c for c in _chain_calls if c["name"] == name]
    return calls[-1]["chain"] if calls else None


def step_cfgs(kind, name):
    return [c[2] for c in _step_calls if c[0] == kind and c[1] == name]


# Checks unrelated to a family are off: this suite is about what an import
# decides, so the album is graded exactly on the five families.
CFG = {
    "music_folder": MF,
    "import_scripts": [4],
    "import_autonomy": "automatic",
    "import_review_families": [],
    "rym_links_auto": True,
    "cover_auto_fetch": True,
    "cover_review": False,
    "advisory_auto_fetch": True,
    "advisory_fallback": "none",
    "instrumental_auto_fetch": False,
    "metadata_auto_fetch": False,
    "grade_check_cover": True,
    "grade_check_missing_tags": True,
    "grade_check_genre": True,
    "grade_check_genre_count": True,
    "grade_check_genre_order": True,
    "grade_check_genre_vocab": True,
    "grade_check_mb_links": True,
    "grade_check_rym_links": True,
    "grade_check_lyrics": True,
    "grade_check_mood": False,
    "grade_check_energy": False,
    "grade_check_key_bpm": False,
    "grade_check_instrumental": False,
    "grade_check_naming": False,
    "grade_check_filename_case": False,
    "grade_check_media": False,
    "grade_check_source": False,
    "grade_check_encoder": False,
    "grade_check_expected_tracks": False,
    "grade_check_audit": False,
    "grade_check_replaygain": False,
    "grade_check_sidecar_cover": False,
    "grade_check_lyrics_format": False,
    "grade_check_lyrics_zero": False,
    "grade_check_lyrics_spaces": False,
    "grade_check_lyrics_blank_lines": False,
    "grade_check_lyrics_lang_tags": False,
    "grade_check_xlit_transliteration": False,
    "grade_check_xlit_translation": False,
    "grade_check_unreadable": True,
}

print("== the policy's defaults ==")
ok(DEFAULT_CONFIG["import_autonomy"] == "automatic",
   "automatic is the shipped default (nothing to configure)")
ok(DEFAULT_CONFIG["import_review_families"] == [],
   "no family is held back by default")
ok(import_policy.mode({}) == "automatic", "a config without the key is automatic")
ok(normalize_config({"import_autonomy": "nonsense"})["import_autonomy"] == "automatic",
   "an unknown mode falls back to automatic")
ok(normalize_config({"import_review_families": "cover, bogus, cover"})["import_review_families"]
   == ["cover"], "the family list keeps known names, once, in wizard order")
ok(import_policy.review_families({"import_review_families": ["lyrics"]}) == ("lyrics",),
   "a listed family is under review while the others stay automatic")
ok(DEFAULT_CONFIG["cover_review"] is False,
   "the cover pick is written by default (review is the opt-in, not the norm)")
ok(DEFAULT_CONFIG["genre_autofill"] is True,
   "the genres family decides for itself by default")

print("== automatic: everything the sources can answer ==")
full = make_album("Full Album", links=True, cover=True, advisory=True)
res = imports.finish_album(full, CFG)
ok(res["autonomy"]["missing"] == {}, f"a complete album has no gaps ({res['autonomy']['missing']})")
ok(res["autonomy"]["prompt"] is None, "and no prompt is raised for it")
ok(import_autonomy.prompts(CFG) == [], "the prompt list stays empty")
ok(chain_of("Full Album") == [4], "the configured chain ran end to end")
ok([c for c in _step_calls if c[0] == "cover" and c[1] == "Full Album"],
   "the cover step was asked (the source answered)")
ok(os.path.isfile(os.path.join(full, "cover.png")), "the cover it found is on disk")

print("== automatic: the genres family fetches its own answer ==")
# The one step that FETCHES a genre: without it an unattended import landed
# with no genre at all and graded GENRE_MISSING however many sources it was
# allowed to ask (script 8 trims and caps what is already there, it never
# looks one up).
bare = make_album("Bare Genre Album", links=True, cover=True, advisory=True, genre=False)
_step_calls.clear()
res = imports.finish_album(bare, CFG)
ok([c for c in _step_calls if c[0] == "genres" and c[1] == "Bare Genre Album"],
   "an import with no genre runs the genres step itself")
ok(_resolve_calls and _resolve_calls[-1] == "11111111-1111-1111-1111-111111111111",
   f"…resolving the release from the identity the album carries ({_resolve_calls[-1:]})")
ok("genres" not in res["autonomy"]["missing"],
   f"…and the family is not left missing ({res['autonomy']['missing']})")

print("== automatic: a Genres family under review is left alone ==")
held = dict(CFG, import_review_families=["genres"])
bare2 = make_album("Held Genre Album", links=True, cover=True, advisory=True, genre=False)
_step_calls.clear()
res = imports.finish_album(bare2, held)
ok(not [c for c in _step_calls if c[0] == "genres"],
   "the import does not fetch genres for a family the user kept")
ok("genres" in res["autonomy"]["missing"],
   f"…and the gap names it, so the prompt can ({res['autonomy']['missing']})")
held_entry = import_autonomy.for_album(bare2, held)
ok(held_entry and [f["id"] for f in held_entry["families"]] == ["genres"],
   "a held family is announced, not hidden")
ok(held_entry and held_entry["families"][0]["state"] == "decision",
   "and it is announced as awaiting a decision")

print("== automatic: what no source could supply ==")
gap = make_album("Gap Album", links=True, cover=False, advisory=False)
res = imports.finish_album(gap, CFG)
ok(chain_of("Gap Album") == [4], "the whole chain still runs for a half-findable album")
ok(os.path.isdir(gap) and os.path.isdir(LIB), "the album stayed in the library")
missing = res["autonomy"]["missing"]
ok(sorted(missing) == ["advisory", "cover"], f"exactly the two families are missing ({sorted(missing)})")
ok(missing["cover"]["state"] == "unsourced" and missing["advisory"]["state"] == "unsourced",
   "both are reported as unsourced (every source was asked)")
ok(res["autonomy"]["stopped"] is None, "automatic mode never stops")
entry = import_autonomy.for_album(gap, CFG)
ok(bool(entry), "the prompt was stored for the wizard to list")
ok([f["id"] for f in entry["families"]] == ["cover", "advisory"],
   f"the prompt names both families, in step order ({[f['id'] for f in entry['families']]})")
body = import_autonomy.body(entry)
ok("Cover art" in body and "Advisory" in body, f"the notification names both ({body})")
ok("cover art" in body.lower(), "and the field that is missing, not just the family")
prompts = prompts_for(gap)
ok(len(prompts) == 1, f"exactly one prompt for the album ({len(prompts)})")
raised = [e for e in events.recent()
          if e["event"] == "import_needs_data" and e["data"]["album_path"] == gap.replace("\\", "/")]
ok(len(raised) == 1, f"exactly one notification, about this album ({len(raised)})")
ok(raised[0]["data"]["families"] == ["cover", "advisory"], "carrying both families")

print("== the prompt's link ==")
from urllib.parse import quote

link = entry["link"]
ok(link.startswith("/import?album="), f"the link is the import wizard ({link})")
ok(quote(entry["album"], safe="") in link, "it carries the album path")
ok("step=Covers" in link, "and the first step that needs a decision")
ok("missing=cover,advisory" in link, "and every missing family, in step order")

print("== a second import of the same album ==")
imports.finish_album(gap, CFG)
ok(len(prompts_for(gap)) == 1, "still one prompt per album, not one per run")

print("== automatic, one family held back by hand ==")
held = make_album("Held Cover Album", links=True, cover=True, advisory=True)
held_cfg = dict(CFG, cover_review=False, import_review_families=["cover"])
res = imports.finish_album(held, held_cfg)
ok(not os.path.isfile(os.path.join(held, "cover.png")),
   "a family the user kept is never decided for them")
ok(all(c.get("cover_review") for c in step_cfgs("cover", "Held Cover Album")),
   "the step is told to stage its candidates instead")
ok(step_cfgs("advisory", "Held Cover Album") and
   step_cfgs("advisory", "Held Cover Album")[-1]["advisory_auto_fetch"] is True,
   "the families NOT held back stay automatic")
held_missing = res["autonomy"]["missing"]
ok(list(held_missing) == ["cover"], f"only the held family is reported ({list(held_missing)})")
ok(held_missing["cover"]["state"] == "decision", "and it is reported as awaiting a decision")
ok("2 cover candidates" in import_autonomy.body(import_autonomy.for_album(held, held_cfg)),
   "with the step's own note saying what is waiting")

print("== review: stop before the first decision ==")
rev = make_album("Review Album", links=True, cover=False, advisory=False)
rev_cfg = dict(CFG, import_autonomy="review")
_calls_before = len(_chain_calls), len(_step_calls)
res = imports.finish_album(rev, rev_cfg)
ok(res["autonomy"]["stopped"] == "cover", f"it stops at the first missing family ({res['autonomy']['stopped']})")
ok(res["autonomy"]["mode"] == "review", "the mode is reported")
ok(res["chain"] == [] and res["scripts"] == [], "no tag-writing chain runs past the stop")
ok(len(_chain_calls) == _calls_before[0] and len(_step_calls) == _calls_before[1],
   "no family step runs past the stop either")
rev_entry = import_autonomy.for_album(rev, rev_cfg)
ok([f["id"] for f in rev_entry["families"]][0] == "cover",
   f"the prompt leads with the stopped family ({[f['id'] for f in rev_entry['families']]})")
ok([f["id"] for f in rev_entry["families"]] == ["cover", "advisory"],
   "and still names the other family nothing has decided")
ok(rev_entry["families"][1]["state"] == "decision", "the advisory is the user's to answer")
ok(rev_entry["reason"] == "stopped", "and says the import stopped rather than ran dry")
ok("step=Covers" in rev_entry["link"], "the link still lands on that step")

print("== review: an album with nothing left to decide ==")
done = make_album("Complete Album", links=True, cover=True, advisory=True,
                  cover_now=True, advisory_now=True)
res = imports.finish_album(done, rev_cfg)
ok(res["autonomy"]["stopped"] is None, "nothing stops an album that needs no decision")
ok(res["autonomy"]["missing"] == {} and res["autonomy"]["prompt"] is None, "and no prompt is raised")
ok(chain_of("Complete Album") == imports.chain_for(rev_cfg),
   "the chain runs as usual, minus what a reviewed family would decide")
# ... and the chain itself is the Run All order minus the scripts it declares
# library-wide (`imports.LIBRARY_WIDE_SCRIPTS`), so an album imported here gets
# every script the Optimize menu runs — 16/17/19 used to run only from there.
ok(imports.DEFAULT_CHAIN == [sid for sid in DEFAULT_RUN_ALL_ORDER
                             if sid not in imports.LIBRARY_WIDE_SCRIPTS],
   "an import with nothing configured runs the Run All order, minus the "
   "library-wide scripts it declares")
ok(13 not in (chain_of("Complete Album") or []),
   "reviewing lyrics takes the lyrics script out of the chain")

print("== a WAITING album is not swept; a WARNING is not a hold (R161/R166) ==")
# Three albums in one library. One is WAITING on a person — a review import
# whose chain has not run — and a library-wide run must leave it alone. One has
# a family no source could supply: the import ran to its END, the album is in
# the library, and that is a WARNING, so a sweep touches it like any other
# album. One has nothing standing at all.
waiting = make_album("Waiting Album", links=True, cover=False, advisory=True)
warned = make_album("Warned Album", links=True, cover=False, advisory=True)
swept = make_album("Swept Album", links=True, cover=True, advisory=True,
                   cover_now=True, advisory_now=True)
import_autonomy.raise_prompt(
    waiting, CFG,
    {"cover": {"id": "cover", "label": "Cover art", "step": "Covers",
               "state": "decision", "fields": ["cover art"],
               "codes": ["COVER"], "note": ""}},
    reason="stopped")
import_autonomy.raise_prompt(
    warned, CFG,
    {"cover": {"id": "cover", "label": "Cover art", "step": "Covers",
               "state": "unsourced", "fields": ["cover art"],
               "codes": ["COVER"], "note": ""}})
ok(import_autonomy.parked_entry(waiting, CFG),
   "a review import that has not run its chain is parked")
ok(not import_autonomy.parked_entry(warned, CFG),
   "an album whose import FINISHED is not parked, whatever it is short of")
ok(bool(import_autonomy.for_album(warned, CFG)),
   "…its warning still stands for the wizard to act on")
ok(not import_autonomy.for_album(swept, CFG), "the third one has nothing standing")
ok("cover" in import_autonomy.warning(import_autonomy.for_album(warned, CFG))["families"],
   "the warning names the family, in the shape every surface carries")
ok(import_autonomy.warning(import_autonomy.for_album(warned, CFG))["waiting"] is False,
   "…and says it is a warning, not a wait")
ok(import_autonomy.warning(import_autonomy.for_album(waiting, CFG))["waiting"] is True,
   "while the review import's own entry says it IS waiting")
ok("needs extra data" in import_autonomy.title(import_autonomy.for_album(warned, CFG)),
   "the notification for a finished import says what the album needs, not that a "
   "decision is pending")
ok(import_autonomy.title(import_autonomy.for_album(waiting, CFG)).startswith(
       "Import needs a decision"),
   "…and a stopped one keeps the wording that really is a decision")

kept, dropped, note = import_autonomy.chain_scope(None, CFG)
_dropped = {os.path.normcase(d) for d in dropped}
ok(os.path.normcase(waiting) in _dropped,
   f"a sweep drops the WAITING album ({dropped})")
ok(os.path.normcase(warned) not in _dropped,
   "…and does NOT drop the album an import finished short of a family")
ok(os.path.normcase(swept) not in _dropped, "…nor the one with nothing standing")
ok(os.path.normcase(warned) in {os.path.normcase(k) for k in kept}
   and os.path.normcase(swept) in {os.path.normcase(k) for k in kept},
   "…while still sweeping the rest of the library")
ok("waiting on you" in note and os.path.basename(waiting) in note,
   f"…saying so ({note})")
ok(import_autonomy.chain_scope([waiting], CFG) == ([waiting], [], ""),
   "a run that names its album is not filtered, whoever started it")
ok(import_autonomy.chain_scope(
    None, {"music_folder": tempfile.mkdtemp(prefix="mlo_nopark_")}) == (None, [], ""),
   "and nothing is filtered while nothing is waiting")

# The same rule through the REAL seam every chain passes: the run's own
# targets are what the script is handed.
_seen = []
_suite_chain = script_runners.run_chain


def _sweep_runner(cfg):
    _seen.append([os.path.normcase(t) for t in (cfg.get("targets") or [])])
    return {"modified_count": 0}


script_runners.RUNNERS[98] = ("Sweep script", _sweep_runner)
script_runners.run_chain = _real_chain
try:
    import contextlib
    import io

    _log = io.StringIO()
    with contextlib.redirect_stdout(_log):
        script_runners.run_chain(dict(CFG), [98], targets=None)
    ok(_seen and os.path.normcase(waiting) not in _seen[-1],
       f"a library-wide run never reaches the WAITING album ({_seen[-1:]})")
    ok(os.path.normcase(swept) in _seen[-1], "…and does reach the album that is not")
    ok(os.path.normcase(warned) in _seen[-1],
       "…and the one a finished import warned about (a warning is not a lock)")
    ok("waiting on you" in _log.getvalue(),
       f"…with the skip said out loud ({_log.getvalue().strip()[-160:]})")
    script_runners.run_chain(dict(CFG), [98], targets=[waiting])
    ok(_seen[-1] == [os.path.normcase(waiting)],
       f"the person's own press on it still runs ({_seen[-1:]})")
finally:
    script_runners.RUNNERS.pop(98, None)
    script_runners.run_chain = _suite_chain

for _name, _fn in _real.items():
    setattr(imports, _name, _fn)
integrations.resolve_release = _real_resolve
script_runners.run_chain = _real_chain
shutil.rmtree(TMP, ignore_errors=True)
# --------------------------------------------------------------------------- #
# ONE IMPORT PER ALBUM: a FOREIGN claim means the work is already being done.
# --------------------------------------------------------------------------- #
# A second autonomous caller used to QUEUE behind the claim and then run the
# whole pipeline again — the six pre-chain lookups and all 21 scripts, over an
# album the first caller had just finished ("it's doing the scripts again").
# It now answers "already importing" and runs nothing. A caller inside its OWN
# claim — the download job, which holds the album from its first byte and then
# imports it — is not a duplicate and still runs.
from server import job_locks  # noqa: E402
import threading as _threading  # noqa: E402

held = os.path.join(TMP, "held-album")
os.makedirs(held, exist_ok=True)
_foreign = {}
_release = _threading.Event()
_acquired = _threading.Event()


def _hold_in_another_job():
    # A claim lives in the CONTEXT that made it (`job_locks.current`), so the
    # other import has to be a context of its own — exactly what a second
    # background job is.
    with job_locks.holding([held], kind="import", label="Import Held"):
        _foreign["job"] = job_locks.current()
        _acquired.set()
        _release.wait(30)


_holder = _threading.Thread(target=_hold_in_another_job, daemon=True)
_holder.start()
_acquired.wait(30)
try:
    _mine = job_locks.new_job()
    with job_locks.holding([os.path.join(TMP, "other-album")], kind="import",
                           label="Import Other", job=_mine):
        out = imports.finish_album(held, dict(CFG), wait=True)
finally:
    _release.set()
    _holder.join(30)
ok(out.get("already_importing") is True and not out.get("scripts"),
   "an album a FOREIGN import holds is not imported a second time")
ok("Import Held" in str(out.get("note") or ""),
   "…and the answer names the holder")

print(f"autonomous import: all {passed} assertions passed")
