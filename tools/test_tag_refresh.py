#!/usr/bin/env python3
"""MAINTAIN → Tags: the MusicBrainz pull stamps and the refresh queue.

Synthetic library only (no disk scan, no MusicBrainz): the stamp arithmetic,
the album-granular rollup, the queue's skip/claim behaviour and the writer that
puts MUSICBRAINZ_UPDATED on a file.

Run:  python tools/test_tag_refresh.py
"""
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="mlo-tags-")
for _name in ("Old", "Fresh", "Hole", "Hand"):
    os.makedirs(os.path.join(TMP, "Alpha", _name), exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import tag_refresh
from server import tag_refresh as tr

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else '  ' + str(detail)}")
    if not ok:
        failures += 1


def track(path, pulled=""):
    tags = {"TITLE": os.path.basename(path)}
    if pulled:
        tags["MUSICBRAINZ_UPDATED"] = pulled
    return {"path": path, "file": os.path.basename(path), "tags": tags}


def album(path, name, pulled=(), release="rel-1"):
    return {
        "path": path,
        "meta": {"ALBUM": name, "MUSICBRAINZ_ALBUMID": release},
        "tracks": [track(f"{path}/{i + 1}.flac", p) for i, p in enumerate(pulled)],
    }


LIB = {
    "artists": [
        {"name": "Alpha", "display_name": "Alpha", "albums": [
            # Oldest wins: 2020-01-01 makes the album due at any sane age.
            album(os.path.join(TMP, "Alpha", "Old").replace("\\", "/"), "Old", ["2020-01-01T00:00:00Z",
                                            "2026-01-01T00:00:00Z"]),
            # Every track fresh: not due.
            album(os.path.join(TMP, "Alpha", "Fresh").replace("\\", "/"), "Fresh", [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())] * 2),
            # One track never pulled: due however fresh the rest are.
            album(os.path.join(TMP, "Alpha", "Hole").replace("\\", "/"), "Hole", ["2026-01-01T00:00:00Z", ""]),
            # No release id: counted as unlinked, never queued.
            album(os.path.join(TMP, "Alpha", "Hand").replace("\\", "/"), "Hand", ["2020-01-01T00:00:00Z"], release=""),
        ]},
    ]
}

from server import library as lib_mod  # noqa: E402
lib_mod.build_library = lambda cfg, progress=None: LIB

CFG = {"music_folder": "C:/M", "tag_refresh_days": 7}

# --------------------------------------------------------------------------- #
# Stamp arithmetic
# --------------------------------------------------------------------------- #
oldest, newest, missing = tr._stamp_times([
    track("a", "2024-01-02T00:00:00Z"), track("b", "2023-12-31T10:00:00Z"),
    track("c", ""), track("d", "not-a-date")])
check("the album's age is its OLDEST parseable stamp", oldest == "2023-12-31T10:00:00Z", oldest)
check("newest is the latest stamp", newest == "2024-01-02T00:00:00Z", newest)
check("a missing or hand-edited stamp counts as 'never pulled', not as an old date",
      missing == 2, missing)
check("no stamps at all answers (None, None, n)", tr._stamp_times([track("x")]) == (None, None, 1))

# --------------------------------------------------------------------------- #
# Status: album granularity, due rules, artist rollup, unlinked albums
# --------------------------------------------------------------------------- #
status = tr.status(CFG, force=True)
rows = {r["album"]: r for r in status["albums"]}
check("every linked album is a row; a folder with no release id is not",
      set(rows) == {"Old", "Fresh", "Hole"}, sorted(rows))
check("the unlinked album is COUNTED, not silently dropped",
      status["counts"]["unlinked"] == 1, status["counts"])
check("an album is due when its OLDEST stamp is past the age", rows["Old"]["due"])
check("an all-fresh album is not due", not rows["Fresh"]["due"])
check("one never-pulled track makes the whole album due", rows["Hole"]["due"])
check("Hole carries its track counts", rows["Hole"]["tracks"] == 2 and rows["Hole"]["missing"] == 1,
      rows["Hole"])
check("the artist rollup counts due albums", status["artists"][0]["due"] == 2, status["artists"])
check("counts total the linked albums only", status["counts"]["albums"] == 3,
      status["counts"])
check("the status memo answers the same object within its TTL",
      tr.status(CFG) is status)

detail = tr.album_detail(CFG, os.path.join(TMP, "Alpha", "Hole").replace("\\", "/"))
check("album detail lists every track's own stamp",
      [t["pulled"] for t in detail["tracks"]] == ["2026-01-01T00:00:00Z", ""], detail)
check("album detail of a folder the library does not hold is None",
      tr.album_detail(CFG, os.path.join(TMP, "Nope")) is None)

# --------------------------------------------------------------------------- #
# The queue: due-only selection, one job, per-album results, claims held
# --------------------------------------------------------------------------- #
from server import script_runners, imports  # noqa: E402

claims = []
real_claim = script_runners.claim_paths


class FakeClaim:
    def __init__(self, paths, **kw):
        claims.append((tuple(paths), kw.get("kind"), kw.get("wait")))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


script_runners.claim_paths = lambda paths, **kw: FakeClaim(paths, **kw)
refreshed = []


def fake_refresh(path, release=None, cfg=None):
    refreshed.append(path)
    if path.endswith("Hole"):
        return {"skipped": True, "reason": "no link", "release": None,
                "identity": None, "tracks": 0}
    return {"skipped": False, "reason": "", "release": "rel-1", "tracks": 2,
            "identity": {"written": 2, "skipped": 0, "failed": 0}}


real_refresh = imports.refresh_release_tags
imports.refresh_release_tags = fake_refresh

out = tr.refresh(due_only=True, cfg=CFG, source="test")
check("a due-only refresh queues exactly the due albums", out["started"], out)
job = out["job"]
deadline = time.time() + 10
while time.time() < deadline:
    job = tr.job_state()
    if job and job["status"] != "running":
        break
    time.sleep(0.02)
check("the job finishes", job and job["status"] == "done", job)
check("both due albums were refreshed", job["refreshed"] == 1, job)
check("a skipped album carries its reason", job["skipped"] and job["skipped"][0]["reason"] == "no link",
      job)
check("every album was worked under the library's own claim",
      all(c[1] == "tags" and c[2] is True for c in claims), claims)
check("the due list never included the fresh or the unlinked album",
      os.path.join(TMP, "Alpha", "Fresh").replace("\\", "/") not in refreshed and os.path.join(TMP, "Alpha", "Hand").replace("\\", "/") not in refreshed,
      refreshed)

# A second press while one runs answers the running job instead of doubling it.
tr._job = {"id": "x", "status": "running"}
second = tr.refresh(paths=[os.path.join(TMP, "Alpha", "Old").replace("\\", "/")], cfg=CFG)
check("a second press never starts a second queue", second["started"] is False,
      second)
tr._job = job

# Nothing due means NO job, with the sentence an empty spinner cannot give.
tr._job = None
real_status = tr.status
tr.status = lambda cfg, now=None, force=False: {"counts": {"due": 0}, "albums": []}
none_due = tr.refresh(due_only=True, cfg=CFG, source="test")
tr.status = real_status
check("nothing due starts no job and says why", none_due["job"] is None and none_due["reason"],
      none_due)

# --------------------------------------------------------------------------- #
# The scheduler is OFF by default and never starts a job when off
# --------------------------------------------------------------------------- #
off = tr.tick()
check("the scheduler does nothing while tag_refresh_enabled is off",
      off["started"] is False and "off" in off["reason"], off)
check("the shipped config has it off", tr._load_cfg().get("tag_refresh_enabled") is False)

# --------------------------------------------------------------------------- #
# The refresh writer: an album with no MusicBrainz link is skipped, not
# invented.
# --------------------------------------------------------------------------- #
imports.refresh_release_tags = real_refresh
real_album_mbids = imports._album_mbids
imports._album_mbids = lambda path: ("", "")
missing = imports.refresh_release_tags(os.path.join(TMP, "Alpha", "Hand").replace("\\", "/"))
check("refresh of an unlinked album skips with its reason",
      missing["skipped"] and "no MusicBrainz release" in missing["reason"], missing)
imports._album_mbids = real_album_mbids

# --------------------------------------------------------------------------- #
# write_mb_tags stamps MUSICBRAINZ_UPDATED on every pull
# --------------------------------------------------------------------------- #
from mlo import autotag  # noqa: E402


class FakeFile:
    def __init__(self):
        self.path = "C:/M/x.flac"
        self.tags = {}
        self.error = ""

    def get_tag(self, key):
        return self.tags.get(key)

    def set_tag(self, key, value):
        self.tags[key] = value
        return True


f = FakeFile()
written, refused = autotag.write_mb_tags(f, [("TITLE", "One"), ("LABEL", "L")])
check("MB values are written", f.tags.get("TITLE") == "One", f.tags)
stamp = str(f.tags.get("MUSICBRAINZ_UPDATED") or "")
check("the pull stamp lands in the same write", len(stamp) == 20 and stamp.endswith("Z"), stamp)
check("the writer reports no refusal", refused == [], refused)

f2 = FakeFile()
autotag.write_mb_tags(f2, [])
check("a pull with no values still records WHEN it happened",
      str(f2.tags.get("MUSICBRAINZ_UPDATED") or "").endswith("Z"), f2.tags)

print(f"\ntag refresh: {'all assertions passed' if not failures else f'{failures} failure(s)'}")
sys.exit(1 if failures else 0)