#!/usr/bin/env python3
"""Optimization-script audit: every defect fixed in the 18 scripts, with the
measurement that proves it.

Nothing here is a prose claim. Each check counts a real cost — file opens,
container saves, decoder loads, ffprobe spawns — or reads back the artifact
the script is supposed to produce, and every label states what the run did
BEFORE the fix and what it does now. Run it with:

    python tools/test_script_optimizations.py

The fixtures are synthetic FLAC/PNG/JPEG/MP4 files in a temp folder; the
user's library is never touched.
"""
import io
import math
import os
import shutil
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import audio as mlo_audio
from mlo import autotag, audiometa, flac, grader, images, paths, remux
from mlo.config import DEFAULT_CONFIG
from mlo.deps import HAS_PIL
from mlo.ui import log

passed = 0
skipped = []


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


def skip(label):
    skipped.append(label)
    print(f"  skip: {label}")


def _dep_exe(prefix, name):
    """An executable from the repo's .dependencies toolchain, or None."""
    deps = os.path.join(ROOT, ".dependencies")
    if not os.path.isdir(deps):
        return None
    for entry in sorted(os.listdir(deps)):
        if entry.lower().startswith(prefix):
            cand = os.path.join(deps, entry, name)
            if os.path.isfile(cand):
                return cand
    return None


FLAC_EXE = _dep_exe("flac", "flac.exe")
FFMPEG_EXE = _dep_exe("ffmpeg", "ffmpeg.exe")
FFPROBE_EXE = _dep_exe("ffmpeg", "ffprobe.exe")
METAFLAC_EXE = _dep_exe("flac", "metaflac.exe")


def cfg(**over):
    """Shipped defaults plus the overrides a check needs."""
    out = dict(DEFAULT_CONFIG)
    out.update(over)
    return out


def make_flac(path, seconds=1, tags=None, genre_values=None):
    """A real FLAC (so mutagen and the scripts see a real container)."""
    wav = path + ".wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * (44100 * seconds))
    if FLAC_EXE:
        subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                       check=True, capture_output=True)
    else:
        subprocess.run([FFMPEG_EXE, "-v", "error", "-i", wav, "-c:a", "flac",
                        "-y", path], check=True, capture_output=True)
    os.remove(wav)
    from mutagen.flac import FLAC
    f = FLAC(path)
    for k, v in (tags or {}).items():
        f[k] = [v]
    if genre_values:
        f["genre"] = list(genre_values)
    f.save()
    return path


def make_image(path, size, fmt=None, mode="RGB", alpha=False):
    from PIL import Image
    if alpha:
        img = Image.new("RGBA", size, (255, 0, 0, 128))
    else:
        img = Image.new(mode, size, (200, 120, 40))
    img.save(path, format=fmt)
    return path


def count_calls(target, name):
    """(counter, restore) around target.name — the counter is a list."""
    real = getattr(target, name)
    hits = []

    def wrapper(*a, **kw):
        hits.append(a)
        return real(*a, **kw)

    setattr(target, name, wrapper)
    return hits, lambda: setattr(target, name, real)


# --------------------------------------------------------------------------- #
# Script 4 — Grade: the album-cover check read the pixels to ask for the size.
# --------------------------------------------------------------------------- #
def check_grader_cover_header_read(tmp):
    from PIL import Image
    album = os.path.join(tmp, "grade_album")
    os.makedirs(album)
    big = make_image(os.path.join(album, "cover.jpg"), (1500, 1500))
    small = os.path.join(album, "cover_small.jpg")
    make_image(small, (600, 600))

    loads = []
    real_load = Image.Image.load

    def counting_load(self, *a, **kw):
        loads.append(self.size)
        return real_load(self, *a, **kw)

    Image.Image.load = counting_load
    try:
        c = cfg(cover_enforce_size=True, cover_resize_enabled=True,
                cover_target_size=1000)
        oversize = grader._cover_image_ok(big, c)
        fits = grader._cover_image_ok(small, c)
    finally:
        Image.Image.load = real_load

    # 1500x1500 against a 1000 target is too large (the fix is a resize);
    # an undersized cover is accepted, like before.
    ok(oversize is False, "script 4: an oversize cover still fails the check")
    ok(fits is True, "script 4: an undersized cover still passes the check")
    ok(not loads,
       f"script 4: _cover_image_ok decodes 0 images to read the size "
       f"(was 1 full decode per cover, measured {len(loads)})")


# --------------------------------------------------------------------------- #
# Script 2 — Format CUEs: the sheet was read from disk twice.
# --------------------------------------------------------------------------- #
def check_cue_single_read(tmp):
    album = os.path.join(tmp, "cue_album")
    os.makedirs(album)
    cue = os.path.join(album, "album.cue")
    body = ('REM DISCID 12345678\r\n'
            'FILE "01 - Track.flac" WAVE\r\n'
            '  TRACK 01 AUDIO\r\n'
            '    INDEX 01 00:00:00\r\n')
    with open(cue, "wb") as fh:
        fh.write(body.encode("utf-8"))

    reads = []
    real_open = open

    def counting_open(file, mode="r", *a, **kw):
        if os.path.normcase(str(file)) == os.path.normcase(cue) and "r" in mode:
            reads.append(mode)
        return real_open(file, mode, *a, **kw)

    import builtins
    builtins.open = counting_open
    try:
        from mlo.cue import _process_cue_file
        path, changed, err, b_rem, b_add = _process_cue_file(
            (cue, False, False, "WAVE", False, False))
    finally:
        builtins.open = real_open

    ok(changed is True, "script 2: a CRLF sheet is still normalised")
    with open(cue, "rb") as fh:
        after = fh.read()
    ok(b"\r\n" not in after, "script 2: the sheet on disk is LF, as before")
    ok(len(reads) == 1,
       f"script 2: one read of the sheet (was 2: NUL check + text read, "
       f"measured {len(reads)})")

    # The non-UTF-8 branch is the one the single-read change touched: a
    # CP1252 sheet must still be left byte-for-byte alone.
    cp = os.path.join(album, "latin.cue")
    with open(cp, "wb") as fh:
        fh.write(b'FILE "01 - \xe9song.flac" WAVE\r\nTRACK 01 AUDIO\r\n')
    before = open(cp, "rb").read()
    _path, changed, err, _r, _a = _process_cue_file(
        (cp, False, False, "WAVE", False, False))
    ok(changed is False and "non-UTF-8" in (err or ""),
       "script 2: a non-UTF-8 sheet is still left untouched")
    ok(open(cp, "rb").read() == before,
       "script 2: the non-UTF-8 sheet is byte-for-byte unchanged")


# --------------------------------------------------------------------------- #
# Script 1 — Format lyrics: the sidecar was read a second time after cleaning.
# --------------------------------------------------------------------------- #
def check_lyrics_single_sidecar_read(tmp):
    album = os.path.join(tmp, "lyrics_album")
    os.makedirs(album)
    track = make_flac(os.path.join(album, "01 - Track.flac"),
                      tags={"TITLE": "Track", "ARTIST": "A", "ALBUM": "B",
                            "TRACKNUMBER": "1"})
    lrc = os.path.join(album, "01 - Track.lrc")
    # A non-canonical sidecar (trailing blank lines) so the cleaning pass
    # really writes it, then the post-cleaning text is what the conversion
    # step must use.
    with open(lrc, "w", encoding="utf-8") as fh:
        fh.write("[00:01.00] line\n\n\n")

    reads = []
    real_open = open

    def counting_open(file, mode="r", *a, **kw):
        if os.path.normcase(str(file)) == os.path.normcase(lrc) and "r" in mode:
            reads.append(mode)
        return real_open(file, mode, *a, **kw)

    import builtins
    builtins.open = counting_open
    try:
        from mlo.lyrics import _process_lyrics_for_audio
        status, _r, _a, info = _process_lyrics_for_audio(
            track, cfg(lyrics_format="EMBEDDED"))
    finally:
        builtins.open = real_open

    ok(status == "modified", f"script 1: the sidecar is still processed ({info})")
    ok(len(reads) == 1,
       f"script 1: one read of the .lrc sidecar (was 2, measured {len(reads)})")
    ok(not os.path.exists(lrc),
       "script 1: EMBEDDED still removes the sidecar once it is embedded")
    from mutagen.flac import FLAC
    ok("line" in (FLAC(track)["lyrics"][0] if "lyrics" in FLAC(track) else ""),
       "script 1: the lyrics really landed in the tag")


# --------------------------------------------------------------------------- #
# Script 8 — Auto tagging: release tags were written one container save each.
# --------------------------------------------------------------------------- #
def check_autotag_release_write_batch(tmp):
    album = os.path.join(tmp, "release_album")
    os.makedirs(album)
    track = make_flac(os.path.join(album, "01 - Track.flac"),
                      tags={"TITLE": "Track", "ARTIST": "A", "ALBUM": "B",
                            "TRACKNUMBER": "1", "DISCNUMBER": "1",
                            "MUSICBRAINZ_ALBUMID": "11111111-1111-1111-1111-111111111111"})

    release = {
        "id": "11111111-1111-1111-1111-111111111111",
        "release_group_id": "22222222-2222-2222-2222-222222222222",
        "album_artist_mbid": "33333333-3333-3333-3333-333333333333",
        "label": "Test Label",
        "catalog_number": "CAT-1",
        "country": "GB",
        "release_type": "album",
        "date": "1980-10-01",
        "originaldate": "1980-10-01",
        "medium": "CD",
        "status": "Official",
        "tracks": {(1, 1): {"recording_mbid": "44444444-4444-4444-4444-444444444444",
                            "artist_mbid": "33333333-3333-3333-3333-333333333333",
                            "release_track_mbid": "55555555-5555-5555-5555-555555555555",
                            "isrcs": ["GBTEST0000001"]}},
    }
    real_cached = autotag._cached_release
    autotag._cached_release = lambda mbid: release
    saves, restore = count_calls(mlo_audio.AudioFile, "_save_container")
    try:
        af = mlo_audio.AudioFile(track)
        written, note = autotag._fill_release_tags(
            [{"af": af}], cfg(music_folder=tmp), album)
    finally:
        restore()
        autotag._cached_release = real_cached

    ok(written >= 10, f"script 8: release tags are still filled ({note})")
    ok(len(saves) == 1,
       f"script 8: ONE container rewrite for the whole fill "
       f"(was {written}, measured {len(saves)})")
    from mutagen.flac import FLAC
    got = FLAC(track)
    ok(got.get("label") == ["Test Label"] and got.get("date") == ["1980-10-01"],
       "script 8: the filled tags really reached the file")
    ok(got.get("isrc") == ["GBTEST0000001"],
       "script 8: the per-track ISRC reached the file")


def check_autotag_no_reopen_after_fix(tmp):
    lib = os.path.join(tmp, "autotag_lib")
    album = os.path.join(lib, "Artist", "Album")
    os.makedirs(album)
    make_flac(os.path.join(album, "01 - Track.flac"),
              tags={"TITLE": "Track", "ARTIST": "A", "ALBUM": "B",
                    "TRACKNUMBER": "1", "ALBUMARTIST": "A"},
              genre_values=["Rock", "Pop"])

    opens = []
    real_init = mlo_audio.AudioFile.__init__

    def counting_init(self, path):
        opens.append(path)
        return real_init(self, path)

    mlo_audio.AudioFile.__init__ = counting_init
    try:
        autotag.run_auto_tagging(cfg(
            music_folder=lib,
            auto_advisory=False,
            auto_instrumental=False,
            mood_enabled=False,
            genre_autofill=True,
            mb_genre_count=1,
        ))
    finally:
        mlo_audio.AudioFile.__init__ = real_init

    from mutagen.flac import FLAC
    genres = FLAC(os.path.join(album, "01 - Track.flac")).get("genre") or []
    ok(len(genres) == 1,
       f"script 8: the GENRE cap still trims the track (now {genres})")
    ok(len(opens) == 1,
       f"script 8: the track is opened once per pass (was 2 — one re-open "
       f"per fix, measured {len(opens)} opens)")


# --------------------------------------------------------------------------- #
# Script 12 — Key & BPM: BPM and INITIALKEY rewrote the container twice.
# --------------------------------------------------------------------------- #
def check_audiometa_batch_write(tmp):
    album = os.path.join(tmp, "audiometa_album")
    os.makedirs(album)
    track = make_flac(os.path.join(album, "01 - Track.flac"),
                      tags={"TITLE": "Track", "ARTIST": "A", "ALBUM": "B",
                            "TRACKNUMBER": "1"})

    saves, restore = count_calls(mlo_audio.AudioFile, "_save_container")
    try:
        changed = audiometa._write_tags(track, "128.4", "Am", cfg())
    finally:
        restore()

    from mutagen.flac import FLAC
    got = FLAC(track)
    ok(changed is True, "script 12: the pass reports the file as changed")
    ok(got.get("bpm") == ["128.4"] and got.get("initialkey") == ["Am"],
       "script 12: both BPM and INITIALKEY are on disk")
    ok(len(saves) == 1,
       f"script 12: ONE container rewrite for both tags (was 2, "
       f"measured {len(saves)})")


# --------------------------------------------------------------------------- #
# Script 10 — Format all: art replacement rewrote the container per step.
# --------------------------------------------------------------------------- #
def check_format_all_art_single_write(tmp):
    if not HAS_PIL:
        return skip("script 10: art replacement (Pillow missing)")
    album = os.path.join(tmp, "formatall_album")
    os.makedirs(album)
    track = make_flac(os.path.join(album, "01 - Track.flac"),
                      tags={"TITLE": "Track", "ARTIST": "A", "ALBUM": "B",
                            "TRACKNUMBER": "1"})
    make_image(os.path.join(album, "cover.jpg"), (600, 600))
    # Some embedded art that is NOT the album cover, so the replace path runs.
    from mutagen.flac import FLAC, Picture
    f = FLAC(track)
    pic = Picture()
    pic.type = 3
    pic.mime = "image/png"
    pic.data = b"\x89PNG\r\n\x1a\n" + b"old" * 4
    f.add_picture(pic)
    f.save()

    from mlo.format_all import _format_audio_file
    saves, restore = count_calls(mlo_audio.AudioFile, "_save_container")
    try:
        path, tag_res, cover_res, _genres, _canon = _format_audio_file(
            track, cfg(embed_covers=True), False, {})
    finally:
        restore()

    ok(cover_res[0] is True, f"script 10: the cover is still replaced ({cover_res})")
    ok(len(saves) == 1,
       f"script 10: ONE container rewrite for the art replacement (was 2: "
       f"strip + add, measured {len(saves)})")
    got = FLAC(track)
    ok(len(got.pictures) == 1 and got.pictures[0].mime == "image/jpeg",
       "script 10: exactly the album cover is embedded afterwards")


def check_format_all_cue_single_read(tmp):
    album = os.path.join(tmp, "formatall_cue")
    os.makedirs(album)
    make_flac(os.path.join(album, "01 - Track.flac"),
              tags={"TITLE": "Track", "TRACKNUMBER": "1"})
    cue = os.path.join(album, "album.cue")
    with open(cue, "w", encoding="utf-8", newline="\n") as fh:
        fh.write('REM DISCID 12345678\nFILE "01 - Track.flac" WAVE\n'
                 '  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n')

    reads = []
    real_open = open

    def counting_open(file, mode="r", *a, **kw):
        if os.path.normcase(str(file)) == os.path.normcase(cue) and "r" in mode:
            reads.append(mode)
        return real_open(file, mode, *a, **kw)

    import builtins
    builtins.open = counting_open
    try:
        from mlo.format_all import _format_cue_file
        path, changed, err = _format_cue_file(cue, cfg(), False)
    finally:
        builtins.open = real_open

    ok(changed is True and err is None,
       "script 10: the .cue is still normalised (trailing newline dropped)")
    ok(len(reads) == 1,
       f"script 10: one read of the sheet (was 3, measured {len(reads)})")


def check_format_all_cue_repair_once_per_album(tmp):
    """The FILE-reference repair runs once per folder, not once per sheet."""
    album = os.path.join(tmp, "formatall_two_cue")
    os.makedirs(album)
    make_flac(os.path.join(album, "01 - Track.flac"),
              tags={"TITLE": "Track", "TRACKNUMBER": "1"})
    for name in ("cd1.cue", "cd2.cue"):
        with open(os.path.join(album, name), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write('FILE "01 - Wrong Name.flac" WAVE\nTRACK 01 AUDIO\n'
                     '  INDEX 01 00:00:00\n')

    calls = []
    real_fix = None
    try:
        from mlo import discs
        real_fix = discs._fix_cue_filenames_locked

        def counting_fix(album_dir, log_fn=None, config=None):
            calls.append(album_dir)
            return real_fix(album_dir, log_fn, config)

        discs._fix_cue_filenames_locked = counting_fix
        from mlo.format_all import run_format_all
        run_format_all(cfg(music_folder=album, targets=[album]))
    finally:
        if real_fix is not None:
            from mlo import discs
            discs._fix_cue_filenames_locked = real_fix

    ok(len(calls) == 1,
       f"script 10: the folder's cue repair runs once for 2 sheets (was 2, "
       f"measured {len(calls)})")
    with open(os.path.join(album, "cd1.cue"), encoding="utf-8") as fh:
        body = fh.read()
    ok("01 - Track.flac" in body,
       "script 10: the stale FILE reference was still repointed")


# --------------------------------------------------------------------------- #
# Script 3 — Optimize FLACs: a converted file ignored add_seektables.
# --------------------------------------------------------------------------- #
def check_flac_convert_seektable(tmp):
    if not (FFMPEG_EXE and FFPROBE_EXE and METAFLAC_EXE):
        return skip("script 3: conversion seektable (ffmpeg/flac missing)")
    src = os.path.join(tmp, "convert_source.wav")
    with wave.open(src, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 44100)

    def convert(cfg_over):
        for f in os.listdir(tmp):
            if f.endswith(".flac"):
                os.remove(os.path.join(tmp, f))
        args = (FFMPEG_EXE, FFPROBE_EXE, METAFLAC_EXE, src, 8, "", {},
                cfg(library_codec="flac", lossless_remove_original=False,
                    **cfg_over))
        name, good, msg, _r, _a = flac._convert_lossless_source(args)
        assert good, f"conversion failed: {msg}"
        return os.path.join(tmp, "convert_source.flac")

    with_seek = convert({"add_seektables": True})
    ok(flac._flac_has_seektable(with_seek) is True,
       "script 3: add_seektables=True now reaches a CONVERTED flac (the "
       "next run no longer has to add it)")
    without = convert({"add_seektables": False})
    ok(flac._flac_has_seektable(without) is False,
       "script 3: add_seektables=False still produces no seektable")


# --------------------------------------------------------------------------- #
# Script 5 — Process images: PNG alpha check decoded; progressive ignored.
# --------------------------------------------------------------------------- #
def check_images_alpha_probe(tmp):
    if not HAS_PIL:
        return skip("script 5: PNG alpha probe (Pillow missing)")
    from PIL import Image
    png = make_image(os.path.join(tmp, "alpha.png"), (800, 800), fmt="PNG",
                     alpha=True)
    plain = make_image(os.path.join(tmp, "plain.png"), (800, 800), fmt="PNG")

    loads = []
    real_load = Image.Image.load

    def counting_load(self, *a, **kw):
        loads.append(self.size)
        return real_load(self, *a, **kw)

    Image.Image.load = counting_load
    try:
        has = images._png_has_alpha(png)
        none = images._png_has_alpha(plain)
    finally:
        Image.Image.load = real_load

    ok(has is True and none is False,
       "script 5: the alpha probe still answers RGBA vs RGB correctly")
    ok(not loads,
       f"script 5: the alpha probe decodes 0 images (was 1 full decode per "
       f"PNG, measured {len(loads)})")


def check_images_progressive_honoured(tmp):
    if not HAS_PIL:
        return skip("script 5: progressive conversion (Pillow missing)")
    src = make_image(os.path.join(tmp, "cover.png"), (600, 600), fmt="PNG")
    out = os.path.join(tmp, "cover_prog.jpg")
    ok(images._prepare_image_streamlined(src, out, cfg(jpeg_progressive=True),
                                         remove_alpha=False),
       "script 5: the PNG -> JPEG conversion still runs")
    with open(out, "rb") as fh:
        data = fh.read()
    ok(_jpeg_is_progressive(data) is True,
       "script 5: jpeg_progressive=True now produces a PROGRESSIVE jpeg on "
       "the convert path (was baseline)")

    out2 = os.path.join(tmp, "cover_base.jpg")
    images._prepare_image_streamlined(src, out2, cfg(jpeg_progressive=False),
                                      remove_alpha=False)
    with open(out2, "rb") as fh:
        data2 = fh.read()
    ok(_jpeg_is_progressive(data2) is False,
       "script 5: jpeg_progressive=False still produces a baseline jpeg")


def _jpeg_is_progressive(data):
    """True when the JPEG's SOF marker is the progressive one (SOF2)."""
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            return marker == 0xC2
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        i += 2 + length
    return None


# --------------------------------------------------------------------------- #
# Script 11 — Remux videos: the same file was probed twice for chapters.
# --------------------------------------------------------------------------- #
def check_remux_single_probe(tmp):
    if not (FFMPEG_EXE and FFPROBE_EXE):
        return skip("script 11: probe count (ffmpeg missing)")
    src = os.path.join(tmp, "chaptered.mp4")
    # h264/aac with two titled chapters: the case that made the code probe the
    # source a second time for its chapter list.
    meta = os.path.join(tmp, "meta.txt")
    with open(meta, "w", encoding="utf-8") as fh:
        fh.write(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=700\n"
                 "title=First\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=700\n"
                 "END=1400\ntitle=Second\n")
    subprocess.run([FFMPEG_EXE, "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=160x120:rate=10:duration=1.4",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1.4",
                    "-i", meta, "-map_metadata", "2", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", "-shortest", "-y",
                    src], check=True, capture_output=True)
    ok(len(remux.probe_chapters(src, FFPROBE_EXE)) == 2,
       "script 11: the fixture really carries 2 chapters")

    probes = []
    real_run = remux.run_tool

    def counting_run(args, *a, **kw):
        if args and str(args[0]) == FFPROBE_EXE:
            probes.append(args)
        return real_run(args, *a, **kw)

    dest = os.path.join(tmp, "out.mkv")
    remux.run_tool = counting_run
    try:
        good, msg = remux.remux_video(src, dest, FFMPEG_EXE, FFPROBE_EXE,
                                      cfg())
    finally:
        remux.run_tool = real_run
    ok(good, f"script 11: the remux still succeeds ({msg})")
    ok(dest and os.path.exists(dest), "script 11: the MKV really exists")
    ok(len(probes) == 2,
       f"script 11: 2 ffprobe spawns per remux (source + verification); was 4, "
       f"measured {len(probes)}")


# --------------------------------------------------------------------------- #
# Script 13 / 18 — the network loops ran one track at a time.
# --------------------------------------------------------------------------- #
def _lyric_album(tmp, name, count):
    from mlo.paths import library_root
    lib = os.path.join(tmp, name)
    album = os.path.join(library_root(lib), "A", "Album")
    os.makedirs(album)
    out = []
    for i in range(count):
        out.append(make_flac(
            os.path.join(album, f"{i + 1:02d} - Song.flac"), 1,
            {"TITLE": f"Song {i + 1}", "ARTIST": "A", "ALBUM": "Album",
             "TRACKNUMBER": str(i + 1)}))
    return lib, album, out


def check_lyrics_fetch_concurrency(tmp):
    """Script 13: N tracks overlap their provider waits (bounded lanes)."""
    import time
    from mlo import lyrics_fetch

    lib, _album, files = _lyric_album(tmp, "fetch_lib", 4)
    LATENCY = 0.15

    def fake_fetch(cfg, artist, title, album_name=None, duration=None, **kw):
        time.sleep(LATENCY)
        return {"provider": "lrclib", "provider_label": "LRCLIB",
                "synced": f"[00:01.00] {title}", "plain": title, "score": 0.99}

    real = lyrics_fetch.fetch_lyrics
    lyrics_fetch.fetch_lyrics = fake_fetch

    def run(worker_limit):
        c = cfg(music_folder=lib, lyrics_format="EMBEDDED",
                force_lyrics=True, worker_limit=worker_limit)
        t0 = time.perf_counter()
        st = lyrics_fetch.run_fetch_lyrics(c)
        return st, time.perf_counter() - t0

    try:
        seq_stats, t_seq = run(1)
        par_stats, t_par = run(0)
    finally:
        lyrics_fetch.fetch_lyrics = real

    ok(seq_stats["modified_count"] == par_stats["modified_count"] == len(files),
       f"script 13: both runs write every track ({seq_stats['modified_count']}"
       f"/{par_stats['modified_count']} of {len(files)})")
    ok(par_stats["by_provider"] == seq_stats["by_provider"],
       "script 13: the provider tally is the same either way")
    ok(t_par < t_seq * 0.6,
       f"script 13: {len(files)} tracks x {LATENCY * 1000:.0f} ms of provider "
       f"wait take {t_par:.2f} s with lanes vs {t_seq:.2f} s one at a time "
       f"({t_seq / t_par:.1f}x)")


def check_publish_concurrency(tmp):
    """Script 18: the same bounded-lane treatment for the LRCLIB loop."""
    import time
    from mlo import lyrics_publish

    lib, _album, files = _lyric_album(tmp, "publish_lib", 4)
    for p in files:
        af = mlo_audio.AudioFile(p)
        af.set_lyrics("[00:01.00] line one\n[00:02.00] line two")
    LATENCY = 0.15

    real_fetch = lyrics_publish.lrclib_fetch
    real_publish = lyrics_publish.lrclib_publish

    def fake_fetch(artist, title, album=None, duration=None, **kw):
        time.sleep(LATENCY)
        return None                      # LRCLIB does not have it yet

    def fake_publish(artist, title, album, duration, plain=None, synced=None):
        time.sleep(LATENCY)
        return True, "published"

    lyrics_publish.lrclib_fetch = fake_fetch
    lyrics_publish.lrclib_publish = fake_publish

    def run(worker_limit):
        c = cfg(music_folder=lib, force_publish=True,
                worker_limit=worker_limit)
        t0 = time.perf_counter()
        st = lyrics_publish.run_publish_lyrics(c)
        return st, time.perf_counter() - t0

    try:
        seq_stats, t_seq = run(1)
        par_stats, t_par = run(0)
    finally:
        lyrics_publish.lrclib_fetch = real_fetch
        lyrics_publish.lrclib_publish = real_publish

    ok(seq_stats["published"] == par_stats["published"] == len(files),
       f"script 18: both runs publish every track "
       f"({seq_stats['published']}/{par_stats['published']} of {len(files)})")
    ok(t_par < t_seq * 0.6,
       f"script 18: {len(files)} tracks x 2 LRCLIB calls take {t_par:.2f} s "
       f"with lanes vs {t_seq:.2f} s one at a time ({t_seq / t_par:.1f}x)")


# --------------------------------------------------------------------------- #
# The atomic sidecar writers asked for a directory fsync Windows cannot do.
# --------------------------------------------------------------------------- #
def check_images_converted_png_optimized(tmp):
    """A converted PNG is handed to oxipng, not stamped as if it were."""
    if not HAS_PIL:
        return skip("script 5: converted PNG (Pillow missing)")
    from mlo.tools import detect_all_tools
    ox = detect_all_tools().get("oxipng") or {}
    if not ox.get("oxipng_exe"):
        return skip("script 5: converted PNG (oxipng not installed)")
    lib = os.path.join(tmp, "png_lib")
    os.makedirs(lib)
    src = make_image(os.path.join(lib, "01 - Art.bmp"), (600, 600), fmt="BMP")
    images.run_process_images(cfg(
        music_folder=lib, targets=[src], reencode_images=True,
        images_convert_to_jpeg=False, images_convert_lossless_to_png=True,
        rename_to_cover=False))

    out = os.path.splitext(src)[0] + ".png"
    ok(os.path.exists(out), "script 5: the BMP was converted to PNG")
    from mlo.containers import _read_png_text
    tags = _read_png_text(out)
    ok(tags.get("ENCODER_VERSION") == ox.get("version"),
       f"script 5: the converted PNG carries the oxipng identity "
       f"(was 'pillow', now {tags.get('ENCODER_VERSION')!r})")
    # The identity above is what the in-place pass reads: with it, a second
    # run must not re-optimise the file it just wrote.
    first = os.path.getsize(out)
    stats = images.run_process_images(cfg(
        music_folder=lib, targets=[out], reencode_images=True,
        images_convert_to_jpeg=False, images_convert_lossless_to_png=True,
        rename_to_cover=False))
    ok(os.path.getsize(out) == first and stats["modified_count"] == 0,
       "script 5: a second run skips the optimized PNG (idempotent)")


def check_images_artist_art_kept(tmp):
    """Script 5 must never rename artist artwork to cover.*."""
    if not HAS_PIL:
        return skip("script 5: artist artwork (Pillow missing)")
    lib = os.path.join(tmp, "img_lib")
    artist = os.path.join(lib, "Artists", "Some Artist")
    album = os.path.join(artist, "2001 - Album")
    solo = os.path.join(lib, "Artists", "Other Artist", "1999 - Solo Album")
    os.makedirs(album)
    os.makedirs(solo)
    artist_png = make_image(os.path.join(artist, "artist.png"), (900, 900),
                            fmt="PNG")
    album_cover = make_image(os.path.join(album, "cover.jpg"), (600, 600))
    # The album that must still get its rename: a scan named front.jpg.
    front = make_image(os.path.join(solo, "front.jpg"), (600, 600))
    make_flac(os.path.join(solo, "01 - Track.flac"),
              tags={"TITLE": "Track", "TRACKNUMBER": "1"})

    images.run_process_images(cfg(music_folder=lib, targets=None,
                                  reencode_images=False))

    ok(os.path.exists(artist_png),
       "script 5: Artists/<Artist>/artist.png is still there after the run")
    ok(not os.path.exists(os.path.join(artist, "cover.png")),
       "script 5: no cover.* was created in the artist folder")
    ok(os.path.exists(album_cover),
       "script 5: the album's own cover.jpg was left where it was")
    ok(os.path.exists(os.path.join(solo, "cover.jpg")) and not os.path.exists(front),
       "script 5: an album folder's front.jpg is still renamed to cover.jpg")

    from mlo.artistdata import has_image
    ok(has_image(artist) is True,
       "script 5: the app still finds the artist image afterwards")

    # Same fixture, pre-fix behaviour: with the guard neutralised the artist
    # image IS renamed away — that is the defect this check exists for.
    before = os.path.join(tmp, "img_lib_before")
    artist_b = os.path.join(before, "Artists", "Some Artist")
    os.makedirs(os.path.join(artist_b, "2001 - Album"))
    artist_b_png = make_image(os.path.join(artist_b, "artist.png"), (900, 900),
                              fmt="PNG")
    make_image(os.path.join(artist_b, "2001 - Album", "cover.jpg"), (600, 600))
    real_guard = images._artist_image_guard
    images._artist_image_guard = lambda mf: (lambda p: False)
    try:
        images.run_process_images(cfg(music_folder=before, targets=None,
                                      reencode_images=False))
    finally:
        images._artist_image_guard = real_guard
    ok(not os.path.exists(artist_b_png)
       and os.path.exists(os.path.join(artist_b, "cover.png")),
       "script 5: without the guard the artist image is renamed to cover.png "
       "(the pre-fix defect, reproduced)")


def check_fsync_dir(tmp):
    keeps = os.path.join(tmp, "fsync")
    os.makedirs(keeps)
    calls = []
    real_open = os.open

    def counting_open(path, flags, *a, **kw):
        calls.append(path)
        return real_open(path, flags, *a, **kw)

    os.open = counting_open
    try:
        synced = paths.fsync_dir(keeps)
    finally:
        os.open = real_open

    if os.name == "nt":
        ok(synced is False and not calls,
           f"the directory fsync makes 0 doomed os.open calls on Windows "
           f"(was 1 AttributeError per written sidecar, measured {len(calls)})")
    else:
        ok(synced is True and len(calls) == 1,
           "the directory fsync still opens + fsyncs the folder on POSIX")


def check_artist_image_lanes(tmp):
    """Script 19: one artist image at a time, or a pool of them.

    Every artist folder carries its own image file, written atomically, so the
    folders share nothing — and the work is a decode plus an encode, which is
    what Pillow releases the GIL inside. The pass walked artist after artist on
    the runner thread. The fake stands in for the re-encode so this measures the
    LANES and not Pillow.
    """
    import time
    from mlo import artistdata

    root = os.path.join(tmp, "artist_lanes_lib", "Artists")
    for i in range(6):
        folder = os.path.join(root, f"Artist {i:02d}")
        os.makedirs(folder)
        make_image(os.path.join(folder, "artist.png"), (64, 64), fmt="PNG")
    LATENCY = 0.12
    seen = []

    def fake_optimize(folder, config):
        seen.append(folder)
        time.sleep(LATENCY)
        return {"path": os.path.join(folder, "artist.png"), "before": (64, 64),
                "after": (64, 64), "changed": True, "reason": "re-fitted",
                "error": ""}

    real = artistdata.optimize_artist_image
    artistdata.optimize_artist_image = fake_optimize

    def run(worker_limit):
        c = cfg(music_folder=os.path.join(tmp, "artist_lanes_lib"),
                worker_limit=worker_limit)
        t0 = time.perf_counter()
        st = artistdata.run_optimize_artist_images(c)
        return st, time.perf_counter() - t0

    try:
        seq_stats, t_seq = run(1)
        par_stats, t_par = run(0)
    finally:
        artistdata.optimize_artist_image = real

    ok(len(seen) == 12, f"script 19: both runs visit every artist ({len(seen)})")
    ok(seq_stats["modified_count"] == par_stats["modified_count"] == 6,
       f"script 19: every image is re-fitted either way "
       f"({seq_stats['modified_count']}/{par_stats['modified_count']} of 6)")
    ok(seq_stats["error_count"] == par_stats["error_count"] == 0,
       "script 19: and neither run reports a failure")
    ok(t_par < t_seq * 0.6,
       f"script 19: 6 images x {LATENCY * 1000:.0f} ms take {t_par:.2f} s with "
       f"lanes vs {t_seq:.2f} s one at a time ({t_seq / t_par:.1f}x)")


def check_accurip_album_lanes(tmp):
    """Script 9: one CD album at a time, or a pool of them.

    An album's AccurateRip pass owns its folder, its cue and its CD-{n} files,
    and its slow half is one decode + CUETools verification per track. The fake
    stands in for that half (`_generate_via_cuetools`) so this measures the
    LANES, not ffmpeg — the same trick script 19's check uses for the
    re-encode.
    """
    import time

    from mlo import accurip
    from mlo.accurip import resolve_arcue_exe

    if not FFMPEG_EXE or not resolve_arcue_exe():
        return skip("script 9: album lanes (no ffmpeg/CUETools to detect)")

    lib = os.path.join(tmp, "accurip_lanes_lib")
    for i in range(4):
        album = os.path.join(lib, "Artists", "Artist", f"Album {i:02d}")
        os.makedirs(album)
        for t in (1, 2):
            make_flac(os.path.join(album, f"{t:02d} - Song.flac"), 1,
                      {"TITLE": f"Song {t}", "ARTIST": "Artist",
                       "ALBUMARTIST": "Artist", "ALBUM": f"Album {i:02d}",
                       "TRACKNUMBER": str(t), "MEDIA": "CD"})

    LATENCY = 0.15
    # What CUETools returns for a synthetic pressing: a log that says the disc
    # is not in the database, which is what the parser and the writer expect.
    fake_log = ("[CUETools log; Date: 1/1/2020 1:00:00 AM; Version: 2.2.6]\n"
                "[AccurateRip ID: 000001c2-000004b1-06000402] disk not present "
                "in database.\n")
    seen = []

    def fake_generate(ffmpeg_exe, arcue_exe, album_dir, disc_num, track_paths,
                      cue_path, config, transport=None):
        seen.append(album_dir)
        time.sleep(LATENCY)
        return fake_log

    real = accurip._generate_via_cuetools
    accurip._generate_via_cuetools = fake_generate

    def run(worker_limit):
        c = cfg(music_folder=lib, worker_limit=worker_limit, force_accurip=True)
        t0 = time.perf_counter()
        st = accurip.run_generate_accurip(c)
        return st, time.perf_counter() - t0

    try:
        seq_stats, t_seq = run(1)
        par_stats, t_par = run(0)
    finally:
        accurip._generate_via_cuetools = real

    ok(len(seen) == 8,
       f"script 9: both runs verify every CD album ({len(seen)} of 8)")
    ok(seq_stats["modified_count"] == par_stats["modified_count"] == 4,
       f"script 9: every album gets its .accurip either way "
       f"({seq_stats['modified_count']}/{par_stats['modified_count']} of 4)")
    ok(seq_stats["error_count"] == par_stats["error_count"] == 0,
       "script 9: and neither run reports a failure")
    ok(t_par < t_seq * 0.6,
       f"script 9: 4 albums x {LATENCY * 1000:.0f} ms of verification take "
       f"{t_par:.2f} s with lanes vs {t_seq:.2f} s one at a time "
       f"({t_seq / t_par:.1f}x)")


def check_layout_scan_lanes(tmp):
    """Script 20: one artist folder at a time, or a pool of them.

    What a whole-library scan spends its time on is the tag read behind the
    `wrong_case` comparison — one container per album, through the server's tag
    cache; everything else is a directory listing. The fake stands in for that
    read and hands back the folder's own names in the wrong case, so every
    artist and album also produces a row: the pooled walk has to report the same
    rows, in the same order, with the same counters as the serial one.
    """
    import time

    from mlo import layout
    from mlo.stats import new_stats

    lib = os.path.join(tmp, "layout_lanes_lib")
    # Six artist folders, one of them holding TWO albums: the artist's own row
    # must be reported once, not once per album of that artist.
    for i in range(6):
        for a in range(2 if i == 0 else 1):
            album = os.path.join(lib, "Artists", f"Artist {i:02d}",
                                 f"Album {i:02d}{'' if a == 0 else 'b'}")
            os.makedirs(album)
            make_flac(os.path.join(album, "01 - Song.flac"), 1,
                      {"TITLE": "Song", "ARTIST": f"Artist {i:02d}",
                       "ALBUMARTIST": f"Artist {i:02d}",
                       "ALBUM": f"Album {i:02d}", "TRACKNUMBER": "1"})

    LATENCY = 0.12
    seen = []

    def fake_tags(path):
        seen.append(path)
        time.sleep(LATENCY)
        album_dir = os.path.dirname(path)
        artist_dir = os.path.dirname(album_dir)
        return {"ALBUMARTIST": os.path.basename(artist_dir).lower(),
                "ARTIST": os.path.basename(artist_dir).lower(),
                "ALBUM": os.path.basename(album_dir).lower(),
                "TITLE": "Song", "TRACKNUMBER": "1"}

    real = layout._track_tags
    layout._track_tags = fake_tags

    def run(worker_limit):
        c = cfg(music_folder=lib, worker_limit=worker_limit,
                naming_script="%albumartist%/%album%/%tracknumber% %title%")
        st = new_stats()
        t0 = time.perf_counter()
        out = layout.scan_library(c, st)
        return out, st, time.perf_counter() - t0

    try:
        seq_out, seq_stats, t_seq = run(1)
        par_out, par_stats, t_par = run(0)
    finally:
        layout._track_tags = real

    seq_rows = [i["path"] for i in seq_out["issues"]]
    par_rows = [i["path"] for i in par_out["issues"]]
    ok(par_rows == seq_rows and seq_rows,
       f"script 20: the pooled walk reports the same rows in the same order as "
       f"the serial one ({len(seq_rows)} rows, same={par_rows == seq_rows})")
    ok(len(seq_rows) == 13,
       f"script 20: six artist rows + seven album rows, the shared artist "
       f"reported once ({seq_rows})")
    ok((par_out["albums"], par_out["artists"], par_out["audio_files"])
       == (seq_out["albums"], seq_out["artists"], seq_out["audio_files"])
       == (7, 6, 7),
       f"script 20: and the same report counts "
       f"({par_out['albums']} albums, {par_out['artists']} artists, "
       f"{par_out['audio_files']} audio files)")
    ok((par_stats["total_scanned"], par_stats["unchanged_count"],
        par_stats["skipped_count"], par_stats["error_count"])
       == (seq_stats["total_scanned"], seq_stats["unchanged_count"],
           seq_stats["skipped_count"], seq_stats["error_count"]),
       f"script 20: the lane merge adds up to the serial run's stats "
       f"({par_stats['total_scanned']} scanned, "
       f"{par_stats['skipped_count']} skipped)")
    ok(len(seen) == 14,
       f"script 20: one tag read per album per run, both times "
       f"({len(seen)} of 14)")
    ok(t_par < t_seq * 0.6,
       f"script 20: 7 albums x {LATENCY * 1000:.0f} ms of tag reads take "
       f"{t_par:.2f} s with lanes vs {t_seq:.2f} s one artist at a time "
       f"({t_seq / t_par:.1f}x)")


# --------------------------------------------------------------------------- #
# Script 1 — Format lyrics: the album pass reused nothing, so every container
# was opened twice (once per file pass, once for MEDIA/SOURCE), and the albums
# were found with a second walk of the library the file pass had already made.
# --------------------------------------------------------------------------- #
def check_lyrics_one_open_per_track(tmp):
    """An album whose tags are already correct is read ONCE per track."""
    from mlo import lyrics as mlo_lyrics

    album = os.path.join(tmp, "lyrics_media")
    os.makedirs(album)
    for n in (1, 2):
        make_flac(os.path.join(album, f"0{n} - Track.flac"), 1,
                  {"TITLE": f"Track {n}", "ARTIST": "A", "ALBUM": "B",
                   "TRACKNUMBER": str(n), "MEDIA": "Digital Media",
                   "SOURCE": "Web"})

    opens, restore = count_calls(mlo_lyrics, "AudioFile")
    try:
        stats = mlo_lyrics.run_format_lyrics(
            cfg(music_folder=album, targets=[album]))
    finally:
        restore()

    ok(len(opens) == 2,
       f"script 1: one open per track for a 2-track album that needs no write "
       f"(was 2 per track: the lyrics pass and then the MEDIA/SOURCE pass, "
       f"measured {len(opens)})")
    ok(stats["modified_count"] == 0 and stats["error_count"] == 0,
       f"script 1: and the album is reported unused ({stats['modified_count']} "
       f"modified, {stats['error_count']} failed)")


def check_lyrics_album_pass_still_reads(tmp):
    """A MEDIA/SOURCE pass over a folder whose files the file pass did NOT open
    (a target that is one track of an album) still reads them: the answer comes
    from the sibling, so the reuse must not become a blind spot."""
    from mlo import lyrics as mlo_lyrics

    album = os.path.join(tmp, "lyrics_media_partial")
    os.makedirs(album)
    one = make_flac(os.path.join(album, "01 - Track.flac"), 1,
                    {"TITLE": "One", "ARTIST": "A", "ALBUM": "B",
                     "TRACKNUMBER": "1", "MEDIA": "Digital Media"})
    make_flac(os.path.join(album, "02 - Track.flac"), 1,
              {"TITLE": "Two", "ARTIST": "A", "ALBUM": "B",
               "TRACKNUMBER": "2", "MEDIA": "Digital Media", "SOURCE": "Web"})

    mlo_lyrics.run_format_lyrics(
        cfg(music_folder=album, targets=[one], fill_empty_source=True,
            digital_media_source_value="CD"))

    from mutagen.flac import FLAC
    got = (FLAC(one).get("SOURCE") or [""])[0]
    ok(got == "Web",
       f"script 1: the SOURCE the album pass filled in is the sibling's, not "
       f"the fallback (got {got!r})")


# --------------------------------------------------------------------------- #
# Script 10 — Format all: the prepared album cover was cached in a plain dict
# that every pool thread read and wrote.
# --------------------------------------------------------------------------- #
def check_format_all_cover_prepared_once(tmp):
    """The album's cover is read and prepared once, not once per track."""
    if not HAS_PIL:
        return skip("script 10: cover cache (Pillow missing)")
    import time

    from mlo import format_all

    album = os.path.join(tmp, "formatall_cover_cache")
    os.makedirs(album)
    for n in range(1, 5):
        make_flac(os.path.join(album, f"0{n} - Track.flac"), 1,
                  {"TITLE": f"Track {n}", "ARTIST": "A", "ALBUM": "B",
                   "TRACKNUMBER": str(n)})
    make_image(os.path.join(album, "cover.jpg"), (600, 600))

    real_prepare = format_all._prepare_embedded_cover
    prepared = []

    def slow_prepare(album_dir, config):
        """The real work, with a window for the sibling tracks to race in."""
        prepared.append(album_dir)
        time.sleep(0.05)
        return real_prepare(album_dir, config)

    format_all._prepare_embedded_cover = slow_prepare
    try:
        format_all.run_format_all(
            cfg(music_folder=album, targets=[album], embed_covers=True))
    finally:
        format_all._prepare_embedded_cover = real_prepare

    ok(len(prepared) == 1,
       f"script 10: the album cover is prepared ONCE for 4 tracks (the cache "
       f"was shared unguarded, so each track could read it for itself; "
       f"measured {len(prepared)}: {prepared})")


def _tone_flac(path, seconds=3, freq=220.0):
    """A real, non-silent FLAC: the audit's detectors decide on audio, and a
    silent fixture gets no verdict to skip on."""
    import struct
    wav = path + ".wav"
    rate = 44100
    frames = bytearray()
    for i in range(int(rate * seconds)):
        v = 0.4 * math.sin(2 * math.pi * freq * i / rate)
        v += 0.2 * math.sin(2 * math.pi * freq * 3 * i / rate + 0.5)
        s = int(max(-1.0, min(1.0, v)) * 32767)
        frames += struct.pack("<hh", s, s)
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    if FLAC_EXE:
        subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                       check=True, capture_output=True)
    else:
        subprocess.run([FFMPEG_EXE, "-v", "error", "-i", wav, "-c:a", "flac",
                        "-y", path], check=True, capture_output=True)
    os.remove(wav)
    from mutagen.flac import FLAC as _FLAC
    f = _FLAC(path)
    f["TITLE"] = ["Track"]
    f["ARTIST"] = ["A"]
    f["ALBUM"] = ["B"]
    f["TRACKNUMBER"] = ["1"]
    f.save()
    return path


# --------------------------------------------------------------------------- #
# Script 6 — a re-audit of unchanged files decodes nothing.
# --------------------------------------------------------------------------- #
def check_audit_rerun_decodes_nothing(tmp):
    """The integrity test decodes only the files this run will audit."""
    from mlo import audit as mlo_audit
    from mlo.audio import AudioFile
    if not _dep_exe("audioauditor", "AudioAuditorCLI.exe"):
        skip("no .dependencies AudioAuditor: the audit cannot write a verdict")
        return
    if not (FLAC_EXE or FFMPEG_EXE):
        skip("no flac/ffmpeg: the integrity test has nothing to decode with")
        return

    album = os.path.join(tmp, "audit_rerun")
    os.makedirs(album)
    tracks = [_tone_flac(os.path.join(album, f"0{n} - Track.flac"), 3,
                         freq=220.0 + 40 * n) for n in (1, 2)]
    run_cfg = cfg(music_folder=album, targets=[album], worker_limit=1)

    verified, restore_v = count_calls(mlo_audit, "verify_integrity")
    batches, restore_b = count_calls(mlo_audit, "_audit_batch")
    try:
        mlo_audit.run_audit_library(dict(run_cfg))
        first_verified, first_batches = len(verified), len(batches)
        verdicts = [str(AudioFile(p).get_tag("AUDIT") or "") for p in tracks]
        verified.clear()
        batches.clear()
        mlo_audit.run_audit_library(dict(run_cfg))
        second_verified, second_batches = len(verified), len(batches)
        verdicts_again = [str(AudioFile(p).get_tag("AUDIT") or "")
                          for p in tracks]
        # A TAG write moves every file's stamp and none of its audio — which
        # is what the rest of a script chain does to every file it touches.
        for p in tracks:
            af = AudioFile(p)
            af.defer_save(True)
            af.set_tag("COMMENT", "written after the audit")
            af.defer_save(False)
        verified.clear()
        batches.clear()
        mlo_audit.run_audit_library(dict(run_cfg))
        third_verified, third_batches = len(verified), len(batches)
        # Re-encoded audio is NOT the audio the verdict was written for.
        _tone_flac(tracks[0], 3, freq=500.0)
        verified.clear()
        mlo_audit.run_audit_library(dict(run_cfg))
        fourth_verified = len(verified)
    finally:
        restore_v()
        restore_b()

    if not any(verdicts):
        skip("AudioAuditor gave the fixture no verdict: nothing to skip on")
        return
    ok(first_verified == 2 and first_batches == 1,
       f"script 6: the first run verifies and audits both tracks "
       f"({first_verified} integrity decodes, {first_batches} spectral batch)")
    ok(second_verified == 0 and second_batches == 0,
       f"script 6: a second run over the same bytes decodes NOTHING — the "
       f"verdict and the integrity answer are already recorded for them (was "
       f"2 integrity decodes + 1 spectral batch per run, measured "
       f"{second_verified} + {second_batches})")
    ok(verdicts_again == verdicts,
       f"script 6: and the AUDIT verdicts are unchanged ({verdicts} vs "
       f"{verdicts_again})")
    ok(third_verified == 0 and third_batches == 0,
       f"script 6: a TAG write does not invalidate that — the verdict is about "
       f"the audio, and the record keeps its identity (measured "
       f"{third_verified} decodes, {third_batches} batches)")
    ok(fourth_verified == 1,
       f"script 6: re-encoded audio is audited again, and only it (measured "
       f"{fourth_verified})")


def check_audit_one_tag_read_per_file(tmp):
    """Script 6 asks both of its questions of one container parse."""
    from mlo import audit as mlo_audit
    import server.tagcache as tagcache
    if not (FLAC_EXE or FFMPEG_EXE):
        skip("no flac/ffmpeg: the audit cannot run")
        return

    album = os.path.join(tmp, "audit_reads")
    os.makedirs(album)
    for n in (1, 2):
        _tone_flac(os.path.join(album, f"0{n} - Track.flac"), 2)
    tagcache.invalidate_all()

    reads, restore_r = count_calls(tagcache, "read_track")
    opens, restore_o = count_calls(mlo_audit, "AudioFile")
    try:
        mlo_audit.run_audit_library(
            cfg(music_folder=album, targets=[album], worker_limit=1))
    finally:
        restore_r()
        restore_o()
    tagcache.invalidate_all()

    ok(len(reads) == 2,
       f"script 6: ONE tag read per file answers both MEDIA and AUDIT (was "
       f"two separate container parses per file plus the write, measured "
       f"{len(reads)} reads for 2 files)")
    ok(len(opens) <= 2,
       f"script 6: and the audit itself opens a container only to WRITE "
       f"(measured {len(opens)} opens for 2 files)")


def main():
    print("Script optimization audit (measurements, not claims)")
    tmp = tempfile.mkdtemp(prefix="mlo_script_opt_")
    checks = [
        ("script 4  Grade", check_grader_cover_header_read),
        ("script 2  Format CUEs", check_cue_single_read),
        ("script 1  Format lyrics", check_lyrics_single_sidecar_read),
        ("script 8  Auto tagging (release tags)", check_autotag_release_write_batch),
        ("script 8  Auto tagging (no re-open)", check_autotag_no_reopen_after_fix),
        ("script 12 Key & BPM", check_audiometa_batch_write),
        ("script 6  Audit (re-run decodes nothing)", check_audit_rerun_decodes_nothing),
        ("script 6  Audit (one read per file)", check_audit_one_tag_read_per_file),
        ("script 10 Format all (art)", check_format_all_art_single_write),
        ("script 10 Format all (.cue reads)", check_format_all_cue_single_read),
        ("script 10 Format all (cue repair)", check_format_all_cue_repair_once_per_album),
        ("script 3  Optimize FLACs", check_flac_convert_seektable),
        ("script 5  Process images (alpha probe)", check_images_alpha_probe),
        ("script 5  Process images (progressive)", check_images_progressive_honoured),
        ("script 5  Process images (artist art)", check_images_artist_art_kept),
        ("script 5  Process images (converted PNG)", check_images_converted_png_optimized),
        ("script 11 Remux videos", check_remux_single_probe),
        ("script 13 Fetch lyrics (lanes)", check_lyrics_fetch_concurrency),
        ("script 19 Artist images (lanes)", check_artist_image_lanes),
        ("script 18 Publish lyrics (lanes)", check_publish_concurrency),
        ("script 9  AccurateRip (lanes)", check_accurip_album_lanes),
        ("script 20 Scan library layout (lanes)", check_layout_scan_lanes),
        ("script 1  Format lyrics (one open)", check_lyrics_one_open_per_track),
        ("script 1  Format lyrics (album pass reads)", check_lyrics_album_pass_still_reads),
        ("script 10 Format all (cover cache)", check_format_all_cover_prepared_once),
        ("all       atomic sidecar writes", check_fsync_dir),
    ]
    bad = 0
    for label, fn in checks:
        print(f"\n{label}")
        try:
            fn(tmp)
        except AssertionError as e:
            bad += 1
            print(f"  FAIL {e}")
        except Exception as e:                     # a broken fixture is not a pass
            bad += 1
            import traceback
            print(f"  ERROR {type(e).__name__}: {e}")
            traceback.print_exc()
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{passed} check(s) passed, {bad} failed"
          + (f", {len(skipped)} skipped" if skipped else ""))
    if skipped:
        for s in skipped:
            print(f"  skipped: {s}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
