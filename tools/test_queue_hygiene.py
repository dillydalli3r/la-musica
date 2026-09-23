#!/usr/bin/env python3
"""Queue hygiene: the prompts that clear themselves, the terminal rows that can
be cleared, and the counts that can never disagree with the list.

Against the REAL registries — the import-prompt table, the wish store (its own
sqlite db), the queue view — and a throwaway music folder, with slskd and the
job registry stubbed where the network would be. No network, no slskd:

  1. a prompt for an album that is COMPLETE now (the families it named were
     supplied after the import: the wizard step it links to, a cover search, a
     lyrics fetch) is not in the payload AND not in the table any more, while a
     prompt for a family that is still missing IS returned, with what it is
     missing; a dismissed prompt stays dismissed;
  2. ONE album is ONE row: a stalled album's prompt supersedes the settled row
     that reported the same folder, and the settled row comes back (clearable)
     once the prompt is answered;
  3. terminal rows are clearable ONE BY ONE and a whole SECTION at a time, over
     the owner of each row (the wish store, the job registry): clearing removes
     exactly the rows it counted and never one that is still running, waiting or
     standing (a failed wish the worker will search again is cancelled, not
     cleared, and stays);
  4. every payload's counts equal the rows it renders, and no row appears in two
     sections.

Standalone: `python tools/test_queue_hygiene.py`, temp roots, no network.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: a throwaway music folder, its app data dir (the prompt table)
# and a config file, so nothing here can touch the developer's library.
# --------------------------------------------------------------------------- #
REDIRECT = tempfile.mkdtemp(prefix="mlo-queue-hygiene-")
MUSIC = os.path.join(REDIRECT, "music")
DATA = os.path.join(REDIRECT, "data")           # app data dir → import_prompts.json
DL = os.path.join(REDIRECT, "downloads")
for _d in (MUSIC, DATA, DL):
    os.makedirs(_d, exist_ok=True)
os.environ["MLO_MUSIC_FOLDER"] = MUSIC

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

# The five families only: this suite is about what a PROMPT says and when it
# goes, not about the rest of the grade, so the album is graded exactly on the
# families (same toggles tools/test_autonomous_import.py uses).
CFG = {
    "music_folder": MUSIC,
    "soulseek_download_dir": DL,
    "soulseek_search_concurrency": 3,
    "soulseek_download_slots": 3,
    "import_autonomy": "automatic",
    "import_review_families": [],
    "wishes_enabled": True,
    "wishes_interval_hours": 6,
    "wishes_auto_import": True,
    "wishes_max_attempts": 0,          # 0 = a failed wish keeps being retried
    "wishes_not_found_attempts": 3,
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
_STUB_CFG = os.path.join(REDIRECT, "config.json")
with open(_STUB_CFG, "w", encoding="utf-8") as f:
    json.dump(CFG, f)
cfgmod.CONFIG_FILE = _STUB_CFG
pathmod.CONFIG_FILE = _STUB_CFG
pathmod.app_data_dir = lambda *a, **k: DATA

import mlo.import_policy as import_policy  # noqa: E402
import server.api_imports as api_imports  # noqa: E402
import server.api_queue as api_queue  # noqa: E402
import server.import_autonomy as import_autonomy  # noqa: E402
import server.soulseek as slsk  # noqa: E402
import server.soulseek_auto as auto  # noqa: E402
import server.wishes as wishes  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# The wish list lives in its own sqlite file next to the real one — redirect it
# BEFORE anything opens it.
wishes.db_path = lambda: os.path.join(REDIRECT, "wishes.db")
wishes._initialized = False

app = FastAPI()
app.include_router(api_queue.router)
app.include_router(api_imports.router)
client = TestClient(app)

_passed = 0


def check(label, cond, detail=""):
    global _passed
    assert cond, f"FAILED: {label} — {detail}"
    _passed += 1
    print(f"ok  {label}")


class Patch:
    """Set attributes for the block, restore them however it ends."""

    def __init__(self, obj, **kw):
        self.obj, self.kw, self.saved = obj, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = getattr(self.obj, k, None)
            setattr(self.obj, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.obj, k, v)
        return False


def queue():
    r = client.get("/api/queue")
    assert r.status_code == 200, r.text
    return r.json()


def rows_by_id(payload=None):
    payload = payload or queue()
    return {r["id"]: (name, r) for name, rows in payload["sections"].items()
            for r in rows}


# --------------------------------------------------------------------------- #
# the albums: real FLACs (the grader reads their tags), one complete and one
# missing two families — the two sides of "is this prompt still true?"
# --------------------------------------------------------------------------- #
FLAC_EXE = None
for _entry in sorted(os.listdir(os.path.join(ROOT, ".dependencies"))):
    if _entry.lower().startswith("flac"):
        _cand = os.path.join(ROOT, ".dependencies", _entry, "flac.exe")
        if os.path.isfile(_cand):
            FLAC_EXE = _cand
            break
assert FLAC_EXE, "flac.exe not found under .dependencies"


def _png1x1():
    """A real 1x1 PNG: the grader opens the album cover when Pillow is
    installed, so the artwork has to be an image."""
    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat)
            + chunk(b"IEND", b""))


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


def make_album(name, *, cover=False, advisory=False, lyrics=True, links=True):
    """An album folder in the library, complete in everything but the two
    families *cover* / *advisory* say."""
    album = os.path.join(MUSIC, "Artists", "An Artist", name)
    os.makedirs(album)
    if cover:
        with open(os.path.join(album, "cover.png"), "wb") as fh:
            fh.write(_png1x1())
    for i in (1, 2):
        path = os.path.join(album, f"{i:02d} - Track.flac")
        make_flac(path)
        set_tags(path, {
            "TITLE": f"Track {i}", "ARTIST": "An Artist", "ALBUMARTIST": "An Artist",
            "ALBUM": name, "DATE": "2020-01-01", "TRACKNUMBER": str(i),
            "GENRE": "Shoegaze", "INSTRUMENTAL": "0",
            **({"LYRICS": "la la la la"} if lyrics else {}),
            **({"ITUNESADVISORY": "0"} if advisory else {}),
            **({"MUSICBRAINZ_ALBUMID": "11111111-1111-1111-1111-111111111111",
                "RATEYOURMUSIC_ALBUM": "https://rateyourmusic.com/release/album/x/"}
               if links else {}),
        })
    return album


def add_cover(album):
    """The thing the prompt asked for, written the way the wizard's own Covers
    step writes it."""
    with open(os.path.join(album, "cover.png"), "wb") as fh:
        fh.write(_png1x1())


def rate_tracks(album):
    """The other family: the advisory, answered on the files."""
    for f in sorted(os.listdir(album)):
        if f.lower().endswith(".flac"):
            set_tags(os.path.join(album, f), {"ITUNESADVISORY": "0"})


def fam(fid, label, state="unsourced"):
    return {"id": fid, "label": label, "step": label, "state": state,
            "fields": [label.lower()], "codes": [], "note": ""}


DONE = make_album("Done Album", cover=True, advisory=True)
GAP = make_album("Gap Album", cover=False, advisory=False)

print("== the fixtures mean what the suite says they mean ==")
done_gaps = import_policy.gaps(DONE, import_policy.effective_config(CFG))
check("an album with every family supplied has no gaps", done_gaps == {},
      json.dumps(done_gaps))
gap_gaps = import_policy.gaps(GAP, import_policy.effective_config(CFG))
check("an album missing cover art and the advisory has exactly those two",
      set(gap_gaps) == {"cover", "advisory"}, json.dumps(sorted(gap_gaps)))

# --------------------------------------------------------------------------- #
# 1. a prompt whose album is complete now is not a prompt any more
# --------------------------------------------------------------------------- #
print("\n== a prompt for an album that is already complete ==")

import_autonomy.raise_prompt(DONE, dict(CFG),
                             {"cover": fam("cover", "Cover art"),
                              "lyrics": fam("lyrics", "Lyrics")},
                             mode="automatic", reason="missing")
# The entry IS written — the store file holds it — and what drops it is the
# READ: `for_album` and `prompts` both ask `_live`, so an entry whose condition
# no longer holds is not listed (and is pruned from the store on the next walk).
_store = json.load(open(os.path.join(DATA, "import_prompts.json"), encoding="utf-8"))
check("the stale prompt was stored", import_autonomy._key(DONE) in _store,
      json.dumps(sorted(_store)))
payload = queue()
check("...and is NOT in the payload (no Needs-you row for a finished album)",
      not any(r["kind"] == "prompt" for _, r in rows_by_id(payload).values()),
      json.dumps([r["id"] for _, r in rows_by_id(payload).values()]))
check("...and is not kept in the table either — the condition it announced is gone",
      import_autonomy.for_album(DONE, dict(CFG)) == {},
      json.dumps(import_autonomy.for_album(DONE, dict(CFG))))

# --------------------------------------------------------------------------- #
# 2. a prompt for a family that IS still missing stays — with what it names
# --------------------------------------------------------------------------- #
print("\n== a prompt for a family that is still missing ==")

import_autonomy.raise_prompt(GAP, dict(CFG),
                             {"cover": fam("cover", "Cover art"),
                              "advisory": fam("advisory", "Advisory")},
                             mode="automatic", reason="missing")
payload = queue()
prompts = [r for r in payload["sections"]["completed"] if r["kind"] == "prompt"]
check("an album a finished import is short of is a FINISHED row (spec R166)",
      len(prompts) == 1, json.dumps(prompts))
check("...not a row in the section for work holding on the user",
      not [r for r in payload["sections"]["needs_attention"]
           if r["kind"] == "prompt"],
      json.dumps(payload["sections"]["needs_attention"]))
row = prompts[0] if prompts else {}
check("...naming the families it is missing, in wizard order",
      row.get("missing") == ["cover", "advisory"]
      and row.get("missing_labels") == ["Cover art", "Advisory"], json.dumps(row))
check("...carrying the whole warning (what, where to fix it, and that nothing waits)",
      (row.get("needs") or {}).get("families") == ["cover", "advisory"]
      and (row.get("needs") or {}).get("waiting") is False
      and bool((row.get("needs") or {}).get("link")), json.dumps(row.get("needs")))
check("...dismissable and never 'clearable' (its own dismiss is the way off)",
      row.get("dismissable") is True and row.get("clearable") is False,
      json.dumps(row))

# --------------------------------------------------------------------------- #
# 3. ...and it clears ITSELF the moment the families are supplied
# --------------------------------------------------------------------------- #
print("\n== the prompt clears itself once the album has what it asked for ==")

# The payload was read a moment ago and its answer is memoised: what is under
# test here is that a change to the album invalidates that memo (the album's
# own stamp), not that a window was waited out.
add_cover(GAP)
rate_tracks(GAP)
check("the fixture is complete now",
      import_policy.gaps(GAP, import_policy.effective_config(CFG)) == {})
payload = queue()
left = [r for r in payload["sections"]["completed"] if r["kind"] == "prompt"]
check("no warning row survives the fix — no import, no restart, no dismiss",
      left == [], json.dumps(left))
check("...and the entry is gone from the table",
      import_autonomy.for_album(GAP, dict(CFG)) == {})

# --------------------------------------------------------------------------- #
# 4. a dismissed prompt stays dismissed
# --------------------------------------------------------------------------- #
print("\n== dismissing a prompt ==")

GAP2 = make_album("Gap Album Two", cover=False, advisory=False)
import_autonomy.raise_prompt(GAP2, dict(CFG),
                             {"cover": fam("cover", "Cover art")},
                             mode="automatic", reason="missing")
check("the warning is listed while its family is missing",
      any(r["kind"] == "prompt" for r in
          queue()["sections"]["completed"]))
r = client.post("/api/import/prompts/dismiss", json={"path": GAP2})
check("POST /api/import/prompts/dismiss answers ok",
      r.status_code == 200 and r.json().get("ok") is True, r.text)
check("...the row is gone", not any(
    r_["kind"] == "prompt" for r_ in queue()["sections"]["completed"]))
check("...and it stays gone on the next payload (the user's own answer wins)",
      not any(r_["kind"] == "prompt" for r_ in queue()["sections"]["completed"]))

# --------------------------------------------------------------------------- #
# 5. ONE album, ONE row: a warning rides the settled row for its album
# --------------------------------------------------------------------------- #
print("\n== one album, one row ==")

GAP3 = make_album("Gap Album Three", cover=False, advisory=False)
WISH = wishes.add_wish("22222222-2222-2222-2222-222222222222", title="Gap Album Three",
                       artist="An Artist", source="soulseek")
# The wish that PUT the album there: imported, its album_path is that folder.
wishes.mark_imported(WISH["id"], GAP3)
import_autonomy.raise_prompt(GAP3, dict(CFG), {"cover": fam("cover", "Cover art")},
                             mode="automatic", reason="missing")
payload = queue()
sections_of_album = [name for name, rows in payload["sections"].items()
                     for r in rows if r.get("album_path")
                     and api_queue._album_key(r["album_path"]) == api_queue._album_key(GAP3)]
check("the album is in exactly ONE section while its warning stands",
      sections_of_album == ["completed"], json.dumps(sections_of_album))
check("...and there is exactly ONE row for it (no second 'waiting' row)",
      len([r for r in payload["sections"]["completed"]
           if r.get("album_path")
           and api_queue._album_key(r["album_path"]) == api_queue._album_key(GAP3)]) == 1,
      json.dumps([r["id"] for r in payload["sections"]["completed"]]))
_wish_row = [r for r in payload["sections"]["completed"]
             if r.get("id") == f"wish:{WISH['id']}"]
check("...the row is the release's OWN finished row, carrying the warning",
      bool(_wish_row) and (_wish_row[0].get("needs") or {}).get("families") == ["cover"]
      and _wish_row[0].get("dismissable") is True,
      json.dumps(_wish_row[0].get("needs") if _wish_row else None))
r = client.post("/api/import/prompts/dismiss", json={"path": GAP3})
check("dismissing answers ok", r.status_code == 200 and r.json().get("ok") is True, r.text)
payload = queue()
check("...the row goes on being the release's finished row, warning gone",
      any(r_["id"] == f"wish:{WISH['id']}" and r_["clearable"]
          and not r_.get("needs")
          for r_ in payload["sections"]["completed"]),
      json.dumps([r_["id"] for r_ in payload["sections"]["completed"]]))
r = client.post("/api/queue/clear", json={"id": f"wish:{WISH['id']}"})
check("...and can then be cleared, reporting the one row it removed",
      r.status_code == 200 and r.json() == {"ok": True, "cleared": 1,
                                            "ids": [f"wish:{WISH['id']}"]},
      r.text)

# --------------------------------------------------------------------------- #
# 6. terminal rows: one at a time, and a whole section
# --------------------------------------------------------------------------- #
print("\n== clearing a finished row, and a section ==")


class JobRegistry:
    """The job registry in memory: `jobs()` reads it, `forget()` drops a SETTLED
    one — the same two calls the real registry serves the queue view and the
    clear (server.soulseek_auto)."""

    def __init__(self, jobs):
        self.jobs = [dict(j) for j in jobs]

    def list(self):
        return [dict(j) for j in self.jobs if j["state"] != "idle"]

    def forget(self, job_id):
        for j in list(self.jobs):
            if j["id"] == int(job_id) and j["state"] not in ("running", "confirm"):
                self.jobs.remove(j)
                return True
        return False


def job(jid, state, stage_key, title, **over):
    d = {"id": jid, "state": state, "stage": stage_key, "stage_key": stage_key,
         "release": {"id": f"{jid:08d}-0000-0000-0000-000000000000",
                     "artist": "An Artist", "title": title},
         "label": f"An Artist — {title}", "log": [], "attempts": [],
         "result": None, "confirm": None, "search": None, "progress": None,
         "wish_id": None, "source": "soulseek", "started_at": 1.0, "ended_at": 0.0}
    d.update(over)
    return d


REG = JobRegistry([
    job(41, "running", "downloading", "Live Album", ended_at=0.0),
    job(42, "done", "completed", "Landed Album", ended_at=2.0,
        result={"album_path": os.path.join(MUSIC, "Artists", "An Artist", "Done Album"),
                "imported": True, "organized": True}),
    job(43, "error", "failed", "Broken Album", ended_at=3.0,
        result={"error": "every candidate was rejected (3 attempts)"}),
])
WANTED = wishes.add_wish("33333333-3333-3333-3333-333333333333", title="Wanted Album",
                         artist="An Artist", source="soulseek")
NOT_FOUND = wishes.add_wish("44444444-4444-4444-4444-444444444444", title="Absent Album",
                            artist="An Artist", source="soulseek")
wishes.mark_not_found(NOT_FOUND["id"], "No candidate folder contained every track", 3)
FAILED = wishes.add_wish("55555555-5555-5555-5555-555555555555", title="Retrying Album",
                         artist="An Artist", source="soulseek")
wishes.mark_failed(FAILED["id"], "every candidate was rejected", 2)

with Patch(auto, jobs=REG.list, forget=REG.forget), \
     Patch(slsk, ready_albums=lambda *a, **k: [], is_running=lambda *a, **k: False,
           web_up=lambda *a, **k: False,
           server_state=lambda *a, **k: {"isLoggedIn": False}):
    payload = queue()
    index = rows_by_id(payload)
    check("a FAILED job that gave up is clearable",
          index["job:43"][1]["clearable"] is True, json.dumps(index["job:43"][1]))
    check("a COMPLETED job is clearable",
          index["job:42"][1]["clearable"] is True, json.dumps(index["job:42"][1]))
    check("a RUNNING job is not clearable (it is cancelled, not cleared)",
          index["job:41"][1]["clearable"] is False, json.dumps(index["job:41"][1]))
    check("a wish nothing was found for is clearable",
          index[f"wish:{NOT_FOUND['id']}"][1]["clearable"] is True)
    check("a still-wanted wish is not clearable",
          index[f"wish:{WANTED['id']}"][1]["clearable"] is False)
    check("a wish that failed an ATTEMPT but is still retried is not clearable",
          index[f"wish:{FAILED['id']}"][1]["clearable"] is False)
    check("...and it can still be CANCELLED off the list (the action it lacked)",
          index[f"wish:{FAILED['id']}"][1]["cancelable"] is True)
    check("...and its row says it is searched again, not that it gave up",
          "searched again automatically" in index[f"wish:{FAILED['id']}"][1]["note"],
          json.dumps(index[f"wish:{FAILED['id']}"][1]["note"]))

    # A live row is refused BY NAME, never silently dropped.
    r = client.post("/api/queue/clear", json={"id": "job:41"})
    check("clearing a running row is refused, naming cancel",
          r.status_code == 409 and "cancel" in r.text, r.text[:200])

    # One row: exactly that row, and the count says so.
    others = set(index) - {"job:43"}
    r = client.post("/api/queue/clear", json={"id": "job:43"})
    check("clearing one finished row reports the one it removed",
          r.status_code == 200 and r.json() == {"ok": True, "cleared": 1,
                                                "ids": ["job:43"]}, r.text)
    after = set(rows_by_id())
    check("...and removes exactly that row", "job:43" not in after and others <= after,
          json.dumps(sorted(after)))

    # A section: its finished rows go, everything else stays.
    failed_job_only = {r_["id"] for r_ in queue()["sections"]["failed"] if r_["clearable"]}
    before_failed = {r_["id"] for r_ in queue()["sections"]["failed"]}
    r = client.post("/api/queue/clear", json={"scope": "failed"})
    body = r.json()
    check("clearing the FAILED section removes exactly its finished rows",
          body["cleared"] == len(failed_job_only)
          and set(body["ids"]) == failed_job_only, r.text)
    after = rows_by_id()
    check("...leaving the failed row that is still being retried",
          f"wish:{FAILED['id']}" in after
          and after[f"wish:{FAILED['id']}"][0] == "failed", json.dumps(sorted(after)))
    check("...and leaving every running or waiting row alone",
          "job:41" in after and f"wish:{WANTED['id']}" in after
          and "job:42" in after, json.dumps(sorted(after)))
    check("...so the section shrank by exactly what was cleared",
          len(queue()["sections"]["failed"]) == len(before_failed) - len(failed_job_only))

    # needs_attention holds a STANDING row (a wish nothing was found for,
    # which is terminal: only the user's retry moves it) beside a prompt — and
    # the prompt is NOT there any more: a finished import's gap is a warning on
    # its own finished row (spec R166), so the section is exactly the rows that
    # really are holding on a person.
    NL = make_album("Needs You Album", cover=False, advisory=False)
    import_autonomy.raise_prompt(NL, dict(CFG), {"cover": fam("cover", "Cover art")},
                                 mode="automatic", reason="missing")
    STANDING = wishes.add_wish("33333333-3333-3333-3333-333333333333",
                               title="Standing", artist="An Artist")
    wishes.mark_not_found(STANDING["id"], "nothing usable found", attempts=3)
    before = queue()
    here = {r_["id"] for r_ in before["sections"]["needs_attention"]}
    warn_rows = [r_ for r_ in before["sections"]["completed"] if r_["kind"] == "prompt"]
    check("Needs you holds the standing wish and NO prompt row",
          f"wish:{STANDING['id']}" in here
          and not any(r_["kind"] == "prompt"
                      for r_ in before["sections"]["needs_attention"]),
          json.dumps(sorted(here)))
    check("...and the finished import's warning is a row in COMPLETED, saying it waits for nobody",
          len(warn_rows) == 1
          and (warn_rows[0].get("needs") or {}).get("waiting") is False,
          json.dumps([r_["id"] for r_ in before["sections"]["completed"]]))
    clearable_here = {r_["id"] for r_ in before["sections"]["needs_attention"]
                      if r_["clearable"]}
    r = client.post("/api/queue/clear", json={"scope": "needs_attention"})
    check("clearing Needs you removes exactly its clearable rows",
          r.status_code == 200 and set(r.json()["ids"]) == clearable_here, r.text)
    after = rows_by_id()
    check("...and the import's warning is untouched by that (its own dismiss is the way off)",
          any(row.get("kind") == "prompt" for _, row in after.values()),
          json.dumps(sorted(after)))

    # The one state that had NO action at all: a failed wish the worker will
    # search again. Cancelling is the way off the list.
    r = client.post("/api/queue/cancel", json={"id": f"wish:{FAILED['id']}"})
    check("cancelling the failed-but-still-retried wish answers ok",
          r.status_code == 200 and r.json().get("removed") is True, r.text)
    after = rows_by_id()
    check("...and it really leaves (the row that had no way off the list)",
          f"wish:{FAILED['id']}" not in after
          and wishes.get_wish(FAILED["id"]) is None, json.dumps(sorted(after)))

    # ----------------------------------------------------------------------- #
    # 7. the counts, and the one-section invariant, on every payload
    # ----------------------------------------------------------------------- #
    print("\n== the counts answer for the rows ==")
    payload = queue()
    for name, rows in payload["sections"].items():
        check(f"counts[{name}] is the {name} rows",
              payload["counts"][name] == len(rows),
              f"{payload['counts'][name]} vs {len(rows)}")
    check("counts.total is every row",
          payload["counts"]["total"] == sum(len(v) for v in payload["sections"].values()))
    seen = {}
    for name, rows in payload["sections"].items():
        for r_ in rows:
            check_no = seen.setdefault(r_["id"], name)
            assert check_no == name, f"{r_['id']} in two sections: {check_no} and {name}"
    check("no row is in two sections", len(seen) == payload["counts"]["total"])
    albums = {}
    twice = []
    for name, rows in payload["sections"].items():
        for r_ in rows:
            key = api_queue._album_key(r_.get("album_path"))
            if not r_.get("album_path"):
                continue
            if albums.setdefault(key, name) != name:
                twice.append((r_["id"], key, albums[key], name))
    check("no album is in two sections either", not twice, json.dumps(twice))

shutil.rmtree(REDIRECT, ignore_errors=True)
print(f"\nok  {_passed} checks — the queue clears itself, and the counts can be trusted")
