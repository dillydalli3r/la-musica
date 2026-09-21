#!/usr/bin/env python3
"""The per-page Export dialog's server contract.

Every page that shows a selection — artist, album, track, playlist listing,
playlist detail, favourites — exports through ONE shared dialog
(web/src/components/ExportDialog.tsx), and that dialog posts the whole option
form to POST /api/export exactly as the Export page does. This suite pins the
server half of that agreement:

  * the option set the dialog renders is exactly the set `ExportRequest`
    accepts as run options, and /api/export/defaults supplies a value for every
    one of them (a renamed or dropped field would silently turn a knob into a
    no-op);
  * the DIALOG'S DEFAULT REQUEST — copy, Artist/Album layout, "Music"
    subfolder, the shipped export_* defaults — runs end to end against a stub
    target through the real endpoint;
  * ONE track stays ONE FILE: the audio is written on its own, never inside an
    archive;
  * many tracks write one file each plus the .m3u8 playlists and sidecars the
    defaults ask for, and a re-run skips everything it already wrote;
  * the selection is what decides: exporting only the second track of an album
    writes only that file and (prune off) leaves the first one alone.

Run:  python tools/test_export_dialog.py
Exit 0 = pass, 2 = skip (no fastapi/httpx, or no ffmpeg).
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import tools as tools_mod  # noqa: E402

FFMPEG = (tools_mod.detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")
if not FFMPEG:
    print("skip: ffmpeg not installed (run a dependency install first)")
    sys.exit(2)

try:
    from fastapi.testclient import TestClient  # noqa: E402
except Exception as e:  # pragma: no cover - a missing extra is a SKIP
    print(f"skip: TestClient unavailable: {e}")
    sys.exit(2)

from mlo.audio import AudioFile          # noqa: E402
from server import exporter              # noqa: E402
from server import main as mlo_main      # noqa: E402  (heavy import)

# --------------------------------------------------------------------------- #
# What the dialog posts
# --------------------------------------------------------------------------- #

# Fields of the request that are positional parts of the call, not run options
# (the endpoint's own declaration — same tuple it filters on).
_FORM_FIELDS = set(mlo_main._EXPORT_FORM_FIELDS)
_OPTION_FIELDS = set(mlo_main.ExportRequest.model_fields) - _FORM_FIELDS

# The dialog's BLANK_FORM is the server's table plus those positional fields,
# so this equality is what makes "the button offers every option the API has".
assert _OPTION_FIELDS == set(exporter.EXPORT_DEFAULTS), (
    sorted(_OPTION_FIELDS ^ set(exporter.EXPORT_DEFAULTS)))


def dialog_body(paths, dest, **over):
    """Exactly what the dialog posts with nothing touched: the blank form
    (server.exporter.EXPORT_DEFAULTS, which /api/export/defaults serves) plus
    the page's own selection and target drive."""
    body = {"paths": paths, "dest": dest, "subfolder": "Music", "codec": "copy",
            "quality": "", "structure": "artist_album"}
    body.update(exporter.EXPORT_DEFAULTS)
    body.update(over)
    return body


ROOT = tempfile.mkdtemp(prefix="mlo_export_dialog_")
MUSIC = os.path.join(ROOT, "Library")
DEST = os.path.join(ROOT, "Device")
os.makedirs(DEST)
os.makedirs(os.path.join(MUSIC, "Artist One", "Album A"), exist_ok=True)

CFG = {"music_folder": MUSIC, "embed_cover_jpeg_quality": 85,
       "embed_cover_resolution": 400, "jpeg_progressive": True}
# The endpoints read the config through this module's own name (the pattern the
# other HTTP suites use), so a temp library needs no real install.
mlo_main.load_config = lambda: dict(CFG)
client = TestClient(mlo_main.app)   # no lifespan: no workers, no slskd boot


def make(path, seconds=1.0, freq=440, tags=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={seconds}", "-c:a", "flac", path],
                   check=True, capture_output=True)
    af = AudioFile(path)
    af.defer_save(True)
    for key, value in (tags or {}).items():
        af.set_tag(key, value)
    af.defer_save(False)
    return path


def album_tags(album, track, artist="Artist One"):
    return {"TITLE": f"Track {track}", "ARTIST": artist, "ALBUMARTIST": artist,
            "ALBUM": album, "TRACKNUMBER": track}


def listing(root):
    out = []
    for base, _dirs, files in os.walk(root):
        for f in files:
            out.append(os.path.join(base, f).replace("\\", "/"))
    return sorted(out)


def post(body):
    r = client.post("/api/export", json=body)
    assert r.status_code == 200, (r.status_code, r.text[:400])
    res = r.json()
    assert res["failed"] == 0, res["errors"]
    return res


try:
    one = make(os.path.join(MUSIC, "Artist One", "Album A", "01 - One.flac"), 1.0, 440,
               album_tags("Album A", "1"))
    two = make(os.path.join(MUSIC, "Artist One", "Album A", "02 - Two.flac"), 0.8, 660,
               album_tags("Album A", "2"))
    solo = make(os.path.join(MUSIC, "Artist Two", "Album B", "01 - Solo.flac"), 0.6, 300,
                album_tags("Album B", "1", "Artist Two"))
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=red:s=900x900", "-frames:v", "1",
                    os.path.join(MUSIC, "Artist One", "Album A", "cover.jpg")],
                   check=True, capture_output=True)
    with open(os.path.join(MUSIC, "Artist One", "Album A", "01 - One.lrc"), "w",
              encoding="utf-8") as f:
        f.write("[00:01.00] Track 1\n")

    # ------------------------------------------------------- defaults: the form
    d = client.get("/api/export/defaults").json()
    # every field the dialog's form carries is served by the endpoint (paths is
    # the page's own selection, not part of the form)
    assert set(d) == (_FORM_FIELDS - {"paths"}) | _OPTION_FIELDS, sorted(
        set(d) ^ ((_FORM_FIELDS - {"paths"}) | _OPTION_FIELDS))
    assert d["subfolder"] == "Music"
    assert d["codec"] in exporter.CODECS or d["codec"] == ""

    # ------------------------------------------- one track stays exactly one file
    res = post(dialog_body([one], DEST))
    assert res["exported"] == 1 and res["verified"] == 1, res
    written = listing(DEST)
    audio = [p for p in written if os.path.splitext(p)[1].lower() in exporter.EXPORT_AUDIO_EXTS]
    assert audio == [f"{DEST}/Music/Artist One/Album A/01 - Track 1.flac".replace("\\", "/")], written
    # …and no archive anywhere: a single-track export is the track, unpackaged.
    assert not [p for p in written if p.lower().endswith((".zip", ".7z", ".rar", ".tar"))], written
    # The defaults also carry the album's sidecars (cover + lyrics) next to it.
    assert res["sidecars"] == 2, res
    assert f"{DEST}/Music/Artist One/Album A/cover.jpg".replace("\\", "/") in written
    assert f"{DEST}/Music/Artist One/Album A/01 - One.lrc".replace("\\", "/") in written

    # ---------------------------------------------------------- the whole album
    # Two tracks are new (the first one is already on the device) — the count
    # only ever reports what this run wrote.
    res = post(dialog_body([one, two, solo], DEST))
    assert (res["exported"], res["skipped"]) == (2, 1), res
    assert res["playlists"] == 3, res           # Album A, Album B, all.m3u8
    written = listing(DEST)
    assert f"{DEST}/Music/Artist Two/Album B/01 - Track 1.flac".replace("\\", "/") in written
    assert f"{DEST}/Music/Artist One/Album A/02 - Track 2.flac".replace("\\", "/") in written
    assert f"{DEST}/Music/Artist One/Album A/Album A.m3u8".replace("\\", "/") in written
    assert f"{DEST}/Music/Artist Two/Album B/Album B.m3u8".replace("\\", "/") in written
    assert [p for p in written if p.lower().endswith("all.m3u8")], written

    # Already there: a re-run of the same selection writes nothing again.
    again = post(dialog_body([one, two, solo], DEST))
    assert (again["exported"], again["skipped"]) == (0, 3), again

    # ------------------------------- the page's selection is what gets exported
    # (only the second track: the first one from the run above is left alone —
    # prune is off in the dialog's defaults)
    solo_dest = os.path.join(ROOT, "Device2")
    os.makedirs(solo_dest)
    res = post(dialog_body([two], solo_dest))
    assert res["exported"] == 1, res
    only = [p for p in listing(solo_dest)
            if os.path.splitext(p)[1].lower() in exporter.EXPORT_AUDIO_EXTS]
    assert only == [f"{solo_dest}/Music/Artist One/Album A/02 - Track 2.flac".replace("\\", "/")], only

    # --------------------------------------------- the dialog's format options
    # mp3 V2 out of the same shared surface: one file per track, still one file
    # for the single-track case.
    mp3_dest = os.path.join(ROOT, "Device3")
    os.makedirs(mp3_dest)
    res = post(dialog_body([one, two], mp3_dest, codec="mp3", quality="V2"))
    assert res["exported"] == 2, res
    mp3s = [p for p in listing(mp3_dest) if p.endswith(".mp3")]
    assert len(mp3s) == 2, mp3s
    assert AudioFile(mp3s[0]).get_tag("TITLE"), mp3s[0]

    mp3_one = os.path.join(ROOT, "Device4")
    os.makedirs(mp3_one)
    res = post(dialog_body([one], mp3_one, codec="mp3", quality="320"))
    assert res["exported"] == 1, res
    assert [p for p in listing(mp3_one) if p.endswith(".mp3")] == \
        [f"{mp3_one}/Music/Artist One/Album A/01 - Track 1.mp3".replace("\\", "/")]

    # ------------------------------------- sidecars and playlists can be turned off
    bare = os.path.join(ROOT, "Device5")
    os.makedirs(bare)
    res = post(dialog_body([one], bare, playlists=False, sidecars=False, embed_covers=False))
    assert (res["playlists"], res["sidecars"]) == (0, 0), res
    assert listing(bare) == [f"{bare}/Music/Artist One/Album A/01 - Track 1.flac".replace("\\", "/")], \
        listing(bare)

    # ------------------------------------------- an unusable target is refused
    r = client.post("/api/export", json=dialog_body([one], MUSIC))
    assert r.status_code == 400 and "music folder" in r.json()["detail"].lower(), r.text[:200]
    r = client.post("/api/export", json=dialog_body([one], DEST, codec="nope"))
    assert r.status_code == 400, r.text[:200]
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print("ok  export dialog: shared option set == the API's run options, the default "
      "request exports end to end, 1 track stays 1 file, album + transcode runs "
      "produce one file per track, sidecars/playlists toggle off, bad targets 400")
