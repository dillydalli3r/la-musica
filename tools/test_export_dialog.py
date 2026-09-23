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
  * the DIALOG'S DEFAULT REQUEST — copy, the shipped Album artist / Album /
    "1-01 Title" layout, "Music" subfolder, the shipped export_* defaults —
    runs end to end against a stub target through the real endpoint;
  * the folder-structure menu is the exporter's own table, a CUSTOM structure
    is previewed through the same evaluator the run uses and then written
    exactly as previewed, and a %field% the app does not know is refused with a
    sentence (in the preview and at the start of a run) instead of quietly
    shortening the tree;
  * ONE track stays ONE FILE: the audio is written on its own, never inside an
    archive;
  * many tracks write one file each — audio only, since the defaults leave the
    album's own `.m3u8`/sidecar files in the library and REPORT them as
    excluded — plus, when the form asks for them, the sidecars and playlists;
    a re-run skips everything it already wrote;
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
# (the exporter's own declaration — same tuple the endpoint filters on).
_FORM_FIELDS = set(exporter.FORM_FIELDS)
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
            "quality": "", "structure": exporter.DEFAULT_STRUCTURE}
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
# The login gate reads the config through server.auth's own import (not the
# `load_config` alias patched below), so it is switched off HERE rather than
# left to whatever auth state the machine happens to have.
mlo_main.auth_mod.requires_login = lambda request, state: False
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
    # The file selection is served RESOLVED (the run's own resolver), so the
    # form opens on the files the next export would actually write: the tracks
    # alone, until somebody saves a selection.
    assert d["copy_files"] == ["audio"], d["copy_files"]

    # ------------------------------------------- one track stays exactly one file
    res = post(dialog_body([one], DEST))
    assert res["exported"] == 1 and res["verified"] == 1, res
    written = listing(DEST)
    audio = [p for p in written if os.path.splitext(p)[1].lower() in exporter.EXPORT_AUDIO_EXTS]
    assert audio == [f"{DEST}/Music/Artist One/Album A/1-01 Track 1.flac".replace("\\", "/")], written
    # …and no archive anywhere: a single-track export is the track, unpackaged.
    assert not [p for p in written if p.lower().endswith((".zip", ".7z", ".rar", ".tar"))], written
    # The defaults write AUDIO and nothing else: no cover.jpg, no .lrc — the
    # cover travels EMBEDDED inside the file and the library's own files stay
    # in the library. What did not travel is REPORTED, never dropped silently.
    assert res["sidecars"] == 0, res
    assert f"{DEST}/Music/Artist One/Album A/cover.jpg".replace("\\", "/") not in written
    assert f"{DEST}/Music/Artist One/Album A/01 - One.lrc".replace("\\", "/") not in written
    assert AudioFile(audio[0]).embedded_pictures(), "the cover must travel embedded"
    reported = {r["name"]: r["kind"] for r in res["excluded"]}
    assert reported.get("cover.jpg") == "cover", res["excluded"]
    assert reported.get("01 - One.lrc") == "lyrics", res["excluded"]
    assert res["excluded_total"] == 2, res["excluded"]

    # ---------------------------------------------------------- the whole album
    # Two tracks are new (the first one is already on the device) — the count
    # only ever reports what this run wrote.
    res = post(dialog_body([one, two, solo], DEST))
    assert (res["exported"], res["skipped"]) == (2, 1), res
    # No album `.m3u8` by default either: a PLAYLIST export is a different
    # thing (server.playlists.export_m3u8), and this switch is opt-in too.
    assert res["playlists"] == 0, res
    written = listing(DEST)
    assert f"{DEST}/Music/Artist Two/Album B/1-01 Track 1.flac".replace("\\", "/") in written
    assert f"{DEST}/Music/Artist One/Album A/1-02 Track 2.flac".replace("\\", "/") in written
    assert not [p for p in written if p.lower().endswith(".m3u8")], written

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
    assert only == [f"{solo_dest}/Music/Artist One/Album A/1-02 Track 2.flac".replace("\\", "/")], only

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
        [f"{mp3_one}/Music/Artist One/Album A/1-01 Track 1.mp3".replace("\\", "/")]

    # ------------------------------------- sidecars and playlists are opt-in
    # OFF is the default (asserted above); ON still writes exactly what the old
    # default did — the album's cover.jpg/.lrc beside the files and one .m3u8
    # per album plus all.m3u8 — so a device that wants them can still have them.
    # …and a client that sends the OLD switch and no file selection (a browser
    # on a cached bundle, or a config from before the selection existed) still
    # gets exactly what it got: `copy_files=None` is "nothing new to say".
    opted = os.path.join(ROOT, "Device5")
    os.makedirs(opted)
    res = post(dialog_body([one], opted, playlists=True, sidecars=True,
                           copy_files=None))
    assert (res["playlists"], res["sidecars"]) == (2, 2), res
    got = listing(opted)
    assert f"{opted}/Music/Artist One/Album A/cover.jpg".replace("\\", "/") in got, got
    assert f"{opted}/Music/Artist One/Album A/01 - One.lrc".replace("\\", "/") in got, got
    assert f"{opted}/Music/Artist One/Album A/Album A.m3u8".replace("\\", "/") in got, got
    assert [p for p in got if p.lower().endswith("all.m3u8")], got

    bare = os.path.join(ROOT, "Device5b")
    os.makedirs(bare)
    res = post(dialog_body([one], bare, playlists=False, sidecars=False, embed_covers=False))
    assert (res["playlists"], res["sidecars"]) == (0, 0), res
    assert listing(bare) == [f"{bare}/Music/Artist One/Album A/1-01 Track 1.flac".replace("\\", "/")], \
        listing(bare)
    # The classic switch resolves to its own family set in the RESULT too, so a
    # client can see which files a run really wrote.
    assert res["copy_files"] == ["audio"], res["copy_files"]

    # ------------------------------------- WHAT gets copied is selectable
    # The dialog's checkbox group posts a selection of file families; only-audio
    # is what it opens on, and every family a run is asked for travels with the
    # same code path the audit is filtered by — a file copied is never reported
    # as left behind, and a file left behind is never copied.
    audio_only = os.path.join(ROOT, "Device6")
    os.makedirs(audio_only)
    res = post(dialog_body([one], audio_only, copy_files=["audio"]))
    assert res["copy_files"] == ["audio"] and res["sidecars"] == 0, res
    assert listing(audio_only) == [
        f"{audio_only}/Music/Artist One/Album A/1-01 Track 1.flac".replace("\\", "/")], listing(audio_only)
    assert {r["name"] for r in res["excluded"]} == {"01 - One.lrc", "cover.jpg"}, res["excluded"]
    assert res["excluded_counts"] == {"cover": 1, "lyrics": 1}, res["excluded_counts"]

    # …audio + the artwork: the cover travels BESIDE the file (and still
    # embedded in it), the .lrc stays in the library and is reported.
    with_art = os.path.join(ROOT, "Device7")
    os.makedirs(with_art)
    res = post(dialog_body([one], with_art, copy_files=["audio", "cover"]))
    assert res["copy_files"] == ["audio", "cover"], res["copy_files"]
    assert res["sidecars"] == 1, res
    got = listing(with_art)
    assert f"{with_art}/Music/Artist One/Album A/cover.jpg".replace("\\", "/") in got, got
    assert f"{with_art}/Music/Artist One/Album A/01 - One.lrc".replace("\\", "/") not in got, got
    assert {r["name"] for r in res["excluded"]} == {"01 - One.lrc"}, res["excluded"]
    assert AudioFile(f"{with_art}/Music/Artist One/Album A/1-01 Track 1.flac".replace("/", os.sep)) \
        .embedded_pictures(), "the cover must still travel embedded"

    # AN EMPTY SELECTION IS REFUSED with the exporter's own sentence — a run
    # that copies nothing must not write an empty folder and call it success —
    # and so is a family the exporter does not have.
    empty_dest = os.path.join(ROOT, "Device8")
    os.makedirs(empty_dest)
    for bad, expect in (([], "at least one"), (["audio", "covers"], "covers")):
        r = client.post("/api/export", json=dialog_body([one], empty_dest, copy_files=bad))
        assert r.status_code == 400 and expect in r.json()["detail"], r.text[:300]
    assert listing(empty_dest) == [], listing(empty_dest)

    # A `.lrc` belongs to ITS track: the lyrics family copies the exported
    # track's own lyric file, and the lyric file of a track OUTSIDE the
    # selection stays in the library (and is reported) — the same per-track rule
    # the audit has always used.
    lrc_dest = os.path.join(ROOT, "Device9")
    os.makedirs(lrc_dest)
    res = post(dialog_body([two], lrc_dest, copy_files=["audio", "lyrics"]))
    got = listing(lrc_dest)
    assert f"{lrc_dest}/Music/Artist One/Album A/1-02 Track 2.flac".replace("\\", "/") in got, got
    assert not [p for p in got if p.lower().endswith(".lrc")], got
    assert f"{lrc_dest}/Music/Artist One/Album A/01 - One.lrc".replace("\\", "/") not in got, got

    # The menu the dialog renders is the exporter's own table (keys, labels and
    # the one-line explanation), exactly like the structure menu.
    menu = client.get("/api/export/files").json()
    assert [f["v"] for f in menu["families"]] == list(exporter.FILE_FAMILIES), menu
    assert all(f["label"] and f["hint"] for f in menu["families"]), menu

    # ------------------------------- the structure menu, and a custom one used
    # The page renders the exporter's OWN menu (keys, labels) and the grammar a
    # custom script is written in, so the dropdown cannot offer a structure the
    # run would refuse.
    menu = client.get("/api/export/structures").json()
    assert [s["v"] for s in menu["structures"]] == list(exporter.STRUCTURES), menu
    assert all(s["label"] for s in menu["structures"]), menu
    assert "discnumber" in menu["fields"] and "num" in menu["functions"], menu

    # A structure the user typed is previewed on the sample track through the
    # same evaluator the run uses, and the run then writes what the script
    # names for the REAL tracks.
    script = "$upper(%albumartist%)/%album%/%discnumber%-%tracknumber% %title%"
    pv = client.post("/api/export/structure/preview",
                     json={"script": script, "ext": ".flac"}).json()
    assert pv["ok"] and pv["path"] == "SYSTEM OF A DOWN/Toxicity/1-4 Psycho.flac", pv
    custom_dest = os.path.join(ROOT, "DeviceCustom")
    os.makedirs(custom_dest)
    res = post(dialog_body([one, two, solo], custom_dest, structure="custom",
                           structure_script=script, embed_covers=False,
                           playlists=False, sidecars=False))
    assert res["exported"] == 3, res
    custom_written = listing(custom_dest)
    assert f"{custom_dest}/Music/ARTIST ONE/Album A/1-1 Track 1.flac".replace("\\", "/") \
        in custom_written, custom_written
    assert f"{custom_dest}/Music/ARTIST TWO/Album B/1-1 Track 1.flac".replace("\\", "/") \
        in custom_written, custom_written

    # A field the app does not know is refused with a sentence — in the preview
    # the page shows while it is typed, and at the start of a run (400, with
    # nothing written, rather than a silently shortened tree).
    bad = client.post("/api/export/structure/preview",
                      json={"script": "%nope%/%title%"}).json()
    assert not bad["ok"] and "%nope%" in bad["error"], bad
    empty = client.post("/api/export/structure/preview", json={"script": ""}).json()
    assert not empty["ok"] and "needs a script" in empty["error"], empty
    empty_dest = os.path.join(ROOT, "DeviceBad")
    os.makedirs(empty_dest)
    try:
        r = client.post("/api/export", json=dialog_body(
            [one], empty_dest, structure="custom", structure_script="%nope%/%title%"))
        assert r.status_code == 400 and "%nope%" in r.json()["detail"], r.text[:300]
        # …and a structure the app does not have at all, including the key this
        # release replaced, is refused rather than silently re-laid
        r = client.post("/api/export", json=dialog_body([one], empty_dest,
                                                        structure="artist_album"))
        assert r.status_code == 400 and "folder structure" in r.json()["detail"], r.text[:300]
    finally:
        assert listing(empty_dest) == [], listing(empty_dest)

    # ------------------------------------------- an unusable target is refused
    r = client.post("/api/export", json=dialog_body([one], MUSIC))
    assert r.status_code == 400 and "music folder" in r.json()["detail"].lower(), r.text[:200]
    r = client.post("/api/export", json=dialog_body([one], DEST, codec="nope"))
    assert r.status_code == 400, r.text[:200]
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print("ok  export dialog: shared option set == the API's run options, the default "
      "request exports end to end (audio only, extras reported), 1 track stays 1 "
      "file, album + transcode runs produce one file per track, the file selection "
      "(menu == the exporter's table, audio-only / audio+artwork, empty + unknown "
      "refused), the classic sidecar switch and playlists opt-in, the structure "
      "menu + custom structure preview/run agree, bad structures and targets 400")
