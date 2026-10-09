#!/usr/bin/env python3
"""Locked files are not playable — the READ side of server/job_locks.

The registry already refuses a WRITE that collides with a running job (see
tools/test_job_locks.py). Nothing refused a READ of the same file: a listener
(or the offline-download queue) could stream a file a script was rewriting,
which on Windows is also what makes the script's temp-then-replace fail — the
open stream holds the file the replace is aiming at. These checks pin the other
direction:

  * a held folder is published with the job's kind/label/start time, its
    match key and the very sentence a refusal carries, so a page cannot word
    the state differently from the 409;
  * the key is the registry's own normalize(), forward-slashed, and the
    containment rule is the registry's — a track's key sits under its folder's
    key at a separator boundary, a sibling folder's does not;
  * GET /api/stream is refused 409 (with the job named) for a track inside a
    held folder, in BOTH modes — plain playback and `?download=1` — and for a
    file another job holds directly, whatever that job's kind is;
  * a sibling folder, an unrelated album and the same file after release all
    stream exactly as before: 200 with the file's bytes, 206 for a Range;
  * the video variant (/api/videos/stream) and the "download the original"
    route refuse the same way, and the bulk offline download reports the held
    file per entry instead of shipping a torn copy of it;
  * a stream already in flight keeps its handle when a job claims the file —
    the running track is never cut, only told about;
  * GET /api/jobs/locks lists exactly the held paths (the source the client
    reads is the source the server enforces) and no job leaves one behind.

No network, no ffmpeg, no real audio: the paths are plain files in a temp
library, and the only thing that reads them is the file responses themselves.
"""
import json
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{('  — ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILED.append(name)


from server import job_locks as jl  # noqa: E402

music = tempfile.mkdtemp(prefix="mlo-playlocks-")
album = os.path.join(music, "Artists", "Artist", "Album")
track = os.path.join(album, "01 - Song.flac")
second = os.path.join(album, "02 - Song.flac")
video = os.path.join(album, "clip.mp4")
sibling = os.path.join(music, "Artists", "Artist", "Album 2")
sib_track = os.path.join(sibling, "01 - Other.flac")
other_album = os.path.join(music, "Artists", "Other", "Live")
other_track = os.path.join(other_album, "01 - Live.flac")
other_video = os.path.join(other_album, "live.mp4")
big_track = os.path.join(other_album, "03 - Long.flac")

BYTES = {}
for path, size in ((track, 64), (second, 64), (video, 64), (sib_track, 48),
                   (other_track, 32), (other_video, 32), (big_track, 1 << 18)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Deterministic, and different per file so a served body can be told from
    # its neighbour's: only the path identity matters to the guards under test.
    data = bytes((i * 7 + len(path)) % 256 for i in range(size))
    with open(path, "wb") as fh:
        fh.write(data)
    BYTES[path] = data

print("== what the registry publishes about a held path ==")

with jl.holding([album], kind="scripts", label="Optimize FLACs"):
    row = jl.jobs()[0]
    check("the held folder is published with the job's kind and label",
          row["kind"] == "scripts" and row["label"] == "Optimize FLACs", str(row))
    check("...its fix, which is what the route refuses with",
          row["locked"] == [{
              "path": album,
              "key": jl.wire_key(album),
              "why": jl.refusal(album, row),
          }], str(row.get("locked")))
    why = row["locked"][0]["why"]
    check("the sentence names the job holding the file",
          "in use by Optimize FLACs" in why and row["job"] in why, why)
    check("...and says what to do about it",
          "wait for it to finish" in why and "retry" in why, why)
    key = row["locked"][0]["key"]
    check("the key is the registry's own identity, forward-slashed",
          key == jl.normalize(album).replace(os.sep, "/") and "\\" not in key, key)
    check("the platform's case-folding rule travels with it",
          jl.FOLD_CASE is (os.name == "nt"), f"{jl.FOLD_CASE} {os.name}")
    # The client's whole matcher: same path, or under it at a separator
    # boundary — the registry's own containment rule, on the same keys.
    check("a track's key sits under its held folder's key",
          jl.wire_key(track).startswith(key + "/"), f"{jl.wire_key(track)} vs {key}")
    check("a sibling folder's track does not",
          not jl.wire_key(sib_track).startswith(key + "/"))
    check("nor does an unrelated album's",
          not jl.wire_key(other_track).startswith(key + "/"))

check("nothing is left held", jl.jobs() == [], str(jl.jobs()))

print("== over HTTP: the stream route is the guard ==")

mlo_main = None
try:
    from fastapi.testclient import TestClient  # noqa: E402  (heavy import)

    from server import api_jobs, main as mlo_main  # noqa: E402
except Exception as e:                                   # pragma: no cover
    print(f"  SKIP  TestClient unavailable ({e})")

if mlo_main is not None:
    _real_config = mlo_main.load_config
    mlo_main.load_config = lambda: {**_real_config(), "music_folder": music,
                                    "first_run_done": True}
    client = TestClient(mlo_main.app)        # no lifespan: no workers, no slskd

    def with_foreign_job(held, body, kind="scripts", label="Optimize FLACs"):
        """Run *body* while a job on ANOTHER THREAD holds *held*.

        Another thread, not this one: the in-process test client copies the
        CALLING context into the request task, so a job held here would be
        inherited by the request (driving the app in-process is the only place
        that happens — a real client arrives with a context of its own) and the
        request would join it instead of being refused."""
        holding_now, released = threading.Event(), threading.Event()

        def holder():
            with jl.holding(held, kind=kind, label=label):
                holding_now.set()
                released.wait(30)

        thread = threading.Thread(target=holder)
        thread.start()
        holding_now.wait(10)
        try:
            return body()
        finally:
            released.set()
            thread.join(10)

    def get(path, **params):
        return client.get("/api/stream", params={"path": path, **params})

    def refuse_while_held(name, held, request, *, names="Optimize FLACs",
                          kind="scripts", label="Optimize FLACs"):
        r = with_foreign_job(held, request, kind=kind, label=label)
        check(f"{name} is refused 409 while the path is held",
              r.status_code == 409, f"{r.status_code} {r.text[:200]}")
        if r.status_code == 409:
            sentence = r.json().get("detail", "")
            check(f"{name}: the refusal names the job holding it",
                  names in sentence, sentence)
        check(f"{name} answers with a refusal, not a media body",
              r.headers.get("content-type", "").startswith("application/json"),
              f"{r.headers.get('content-type')} {r.text[:120]}")
        return r

    probe = get(other_track)
    if probe.status_code in (401, 428):
        print(f"  SKIP  the auth gate is on in this checkout ({probe.status_code})")
    else:
        # ---- nothing about normal playback changes --------------------------
        r = get(other_track)
        check("an unlocked file streams as before",
              r.status_code == 200 and r.content == BYTES[other_track],
              f"{r.status_code} {len(r.content)} bytes")
        check("...advertising ranges",
              r.headers.get("accept-ranges") == "bytes", str(dict(r.headers)))
        r = client.get("/api/stream", params={"path": other_track},
                       headers={"Range": "bytes=0-9"})
        check("...and answering a Range with 206 and those bytes",
              r.status_code == 206 and r.content == BYTES[other_track][:10],
              f"{r.status_code} {r.content[:16]!r}")

        # ---- a held folder covers its tracks, in both stream modes ----------
        refuse_while_held("playing a track in a held folder", [album], lambda: get(track))
        refuse_while_held("the offline download of the same track", [album],
                          lambda: get(track, download=1))
        refuse_while_held("a video in a held folder", [album],
                          lambda: client.get("/api/videos/stream", params={"path": video}))
        refuse_while_held("downloading the original of a held track", [album],
                          lambda: client.get("/api/track/download", params={"path": track}))

        # ---- and only what is held ------------------------------------------
        r = with_foreign_job([album], lambda: get(sib_track))
        check("a sibling folder's track still plays while its neighbour is held",
              r.status_code == 200 and r.content == BYTES[sib_track],
              f"{r.status_code} {r.text[:120]}")
        r = with_foreign_job([album], lambda: get(other_track))
        check("so does an unrelated album's", r.status_code == 200, f"{r.status_code}")
        r = with_foreign_job([album],
                             lambda: client.get("/api/videos/stream",
                                                params={"path": other_video}))
        check("and an unrelated video", r.status_code == 200, f"{r.status_code}")

        # A single FILE held blocks that file and nothing around it: the rule
        # is the registry's, not "the folder is busy so the album is busy".
        r = with_foreign_job([second], lambda: get(second))
        check("a held track is refused", r.status_code == 409, f"{r.status_code}")
        r = with_foreign_job([second], lambda: get(track))
        check("...while the track next to it streams",
              r.status_code == 200 and r.content == BYTES[track], f"{r.status_code}")

        # ---- any kind of job holds a path, not just a script run ------------
        refuse_while_held("a track held by a TAG WRITE", [album], lambda: get(track),
                          names="Bulk tag write", kind="tags", label="Bulk tag write")
        refuse_while_held("a folder held by an organize", [album], lambda: get(track),
                          names="Organize", kind="organize", label="Organize")

        # ---- the offline-download batch reports it per file -----------------
        r = with_foreign_job([album], lambda: client.post("/api/media/bulk",
                                                          json={"paths": [track, sib_track]}))
        head, _, body = r.content.partition(b"\n")
        files = {f["path"]: f for f in json.loads(head)["files"]}
        check("the bulk download reports the held file",
              r.status_code == 200
              and "in use by Optimize FLACs" in (files[track]["error"] or ""),
              f"{r.status_code} {files.get(track)}")
        check("...promising no bytes for it (no torn copy to cache)",
              files[track]["size"] == 0, str(files.get(track)))
        check("...and still shipping the rest of the batch",
              len(body) == files[sib_track]["size"] == len(BYTES[sib_track]),
              f"{len(body)} vs {files.get(sib_track)}")

        # ---- the client's list is the server's rule -------------------------
        def locks(body):
            r = client.get("/api/jobs/locks")
            return r.json()

        payload = with_foreign_job([album], lambda: locks(None))
        rows = payload.get("jobs") or []
        check("the locks route lists exactly the held paths",
              [entry["path"] for row in rows for entry in row["locked"]] == [album],
              str(payload))
        check("...with the sentence the stream route refuses with",
              rows and rows[0]["locked"][0]["why"] == jl.refusal(album, rows[0]),
              str(payload))
        check("...and the case-folding rule a client matches with",
              isinstance(payload.get("fold_case"), bool), str(payload.get("fold_case")))
        track_key = jl.wire_key(track)
        held_key = rows[0]["locked"][0]["key"] if rows else ""
        check("...so a page can mark this track locked from the same data",
              track_key.startswith(held_key + "/"), f"{track_key} vs {held_key}")
        check("and lists nothing once the job is gone",
              locks(None)["jobs"] == [], str(locks(None)))

        # ---- releasing the job gives the file back --------------------------
        r = get(track)
        check("the same request streams once the job is gone",
              r.status_code == 200 and r.content == BYTES[track],
              f"{r.status_code} {r.text[:120]}")

        # ---- a stream already in flight keeps its handle --------------------
        with client.stream("GET", "/api/stream", params={"path": big_track}) as resp:
            check("a long stream starts", resp.status_code == 200, str(resp.status_code))
            chunks = resp.iter_bytes(4096)
            first = next(chunks)
            with_foreign_job([other_album], lambda: None)
            rest = b"".join(chunks)
        check("a job claiming the file does NOT cut the stream already playing",
              first + rest == BYTES[big_track],
              f"{len(first) + len(rest)} of {len(BYTES[big_track])}")
        check("the file streams again once the job that claimed it is gone",
              get(big_track).status_code == 200)

        check("no job is left behind", jl.jobs() == [], str(jl.jobs()))
        check("no job is left in this thread's context", jl.current() is None, str(jl.current()))

    mlo_main.load_config = _real_config

shutil.rmtree(music, ignore_errors=True)
print(f"\n{len(FAILED)} failure(s)")
sys.exit(1 if FAILED else 0)
