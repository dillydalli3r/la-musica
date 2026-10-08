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
import time
import urllib.error
import urllib.parse
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
                    **cfg_over),
                1)          # the lane's share of the thread budget (script 3)
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
# Script 3 — the LEVEL decides; the encoder's own version does not (by default).
# --------------------------------------------------------------------------- #
def check_encoder_version_does_not_reencode(tmp):
    """A library at the target level is not re-encoded for an old VERSION tag.

    Issue #74: ENCODER_VERSION named the encoder BINARY, and the skip check
    compared it — so every tool upgrade re-encoded every track (hours of CPU
    for a tag nothing reads once it matches). The shipped default is OFF since
    v4.4.0, the compare is gated on the marker being enabled, and a stored
    `true` (what the old default itself wrote into every saved config) follows
    the new default exactly once.
    """
    from mlo.config import DEFAULT_CONFIG, normalize_config

    # 1. the shipped default: the level is on, the version is off
    ok(DEFAULT_CONFIG["encoder_tags"]["flac"] ==
       {"ENCODER_PROGRAM": False, "ENCODER_QUALITY": True,
        "ENCODER_VERSION": False},
       f"the shipped FLAC markers are level-only "
       f"({DEFAULT_CONFIG['encoder_tags']['flac']})")

    # 2. the one-time move: a stored `true` (the OLD default) becomes false,
    #    and a `true` written AFTER the move is the user's own choice and stays
    stored = {"encoder_tags": {"flac": {"ENCODER_VERSION": True}}}
    moved = normalize_config(stored)
    ok(moved["encoder_tags"]["flac"]["ENCODER_VERSION"] is False,
       f"a stored pre-4.4 `true` follows the new default "
       f"({moved['encoder_tags']['flac']})")
    kept = normalize_config({**stored, "encoder_tags_version_default_moved": True})
    ok(kept["encoder_tags"]["flac"]["ENCODER_VERSION"] is True,
       "a later `true` (the user re-enabled the row) is kept")

    # 3. the script itself: quality 8 >= target 5, an ancient VERSION
    if not FLAC_EXE:
        return skip("script 3: encoder version gate (no vendored flac.exe)")
    from mlo.containers import _write_flac_tags
    lib = os.path.join(tmp, "version_gate_lib")
    album = os.path.join(lib, "A", "Album")
    os.makedirs(album)
    track = make_flac(os.path.join(album, "01 - Song.flac"), 1,
                      {"TITLE": "Song", "ARTIST": "A", "ALBUM": "Album"})
    _write_flac_tags(track, 8, "0.0.1", None)     # our level, an old encoder
    stats = flac.run_optimize_flacs(cfg(music_folder=lib, targets=[track],
                                        library_codec_quality=5))
    # The markers say it: a re-encode would have rewritten QUALITY to the
    # target (5) and — with the marker off — dropped ENCODER_VERSION entirely.
    # (The file's mtime is NOT the witness: the skip path still applies the
    # seektable state the settings ask for, which is a metadata-block edit.)
    q, v, _p, _seek = flac._flac_probe(track)
    ok(stats["modified_count"] == 0 and q == "8" and v == "0.0.1",
       f"script 3: an old ENCODER_VERSION alone does not re-encode "
       f"(modified={stats['modified_count']}, q={q}, v={v})")

    # …and the same file WITH the marker enabled is re-encoded, so the gate is
    # what changed and not the encoder
    enabled = dict(DEFAULT_CONFIG["encoder_tags"]["flac"], ENCODER_VERSION=True)
    stats = flac.run_optimize_flacs(cfg(music_folder=lib, targets=[track],
                                        library_codec_quality=5,
                                        encoder_tags={"flac": enabled}))
    q2, v2, _p2, _s2 = flac._flac_probe(track)
    ok(stats["modified_count"] == 1 and q2 == "5" and v2,
       f"script 3: with ENCODER_VERSION enabled it re-encodes again "
       f"(modified={stats['modified_count']}, q={q2}, v={v2})")


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
    # 2 was the count after the chapter double-probe was fixed; the source
    # probe is now served from `mlo.remux._ffprobe_json`'s file-stat memo,
    # which `probe_chapters(src)` above already filled, so only the OUTPUT's
    # verification probe spawns. (Was 4 before either fix.)
    ok(len(probes) == 1,
       f"script 11: 1 ffprobe spawn left — the source is memoized on its own "
       f"stat, the output is verified; was 4, measured {len(probes)}")

    # …and the memo is not a path-keyed lie: a source that CHANGED is probed
    # again. (A video rewritten in place — remuxed, retagged, re-downloaded.)
    os.utime(src, (os.path.getmtime(src) + 10, os.path.getmtime(src) + 10))
    probes.clear()
    remux.run_tool = counting_run
    try:
        remux.remux_video(src, os.path.join(tmp, "out2.mkv"), FFMPEG_EXE,
                          FFPROBE_EXE, cfg())
    finally:
        remux.run_tool = real_run
    ok(len(probes) >= 2,
       f"script 11: a changed source is probed again (source + verification), "
       f"measured {len(probes)}")


# --------------------------------------------------------------------------- #
# Script 13 — the network loop ran one track at a time.
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


def check_worker_budget_semantics(tmp):
    """The one knob: 0 = the machine, ceilings cap only the automatic width.

    ``worker_limit`` is what every script's lanes come from, so its precedence
    is the setting's own contract: an explicit number is the user's size (never
    clamped), 0/unset means every core this process may use, a script's own
    ceiling may only lower THAT automatic number, and the pool never starts
    more lanes than it has items. `thread_budget`/`tool_threads` report the
    same machine so lanes × threads-per-lane stays within it (R79, R323).
    """
    from mlo import stats as stats_mod

    cores = stats_mod.usable_cores()
    ok(stats_mod.worker_count({}) == cores,
       f"0 lanes nothing: the automatic width is the machine's ({cores})")
    ok(stats_mod.worker_count({"worker_limit": 0}, maximum=4, items=100)
       == min(4, cores),
       "a script's own ceiling lowers only the automatic width")
    ok(stats_mod.worker_count({"worker_limit": 9}, maximum=4, items=100) == 9,
       "an explicit Worker threads value is the user's size, not a ceiling's")
    ok(stats_mod.worker_count({}, items=2) == min(2, cores),
       "the pool never starts more lanes than it has items")
    ok(stats_mod.worker_count({"worker_limit": "junk"}) == cores,
       "a junk value falls back to automatic instead of raising")
    budget = stats_mod.thread_budget({})
    for lanes in (1, 2, 4):
        if lanes > budget:
            continue
        share = stats_mod.tool_threads({}, lanes)
        ok(lanes * share <= budget,
           f"{lanes} lane(s) of {share} thread(s) stay within the budget of "
           f"{budget}")


def check_lyrics_fetch_concurrency(tmp):
    """Script 13: N tracks overlap their provider waits (bounded lanes), and
    one provider's politeness wait never queues another HOST's request.

    The gate was ONE global ``_last_request``/``_MIN_GAP`` for the whole
    process, so a MusicBrainz or Wayback wait made every NetEase/Kugou/QQ/Kuwo
    probe queue behind it. It is keyed by the URL's hostname now.
    """
    from mlo import lyrics_fetch, lyrics_providers

    lib_seq, _album, _seq_files = _lyric_album(tmp, "fetch_lib_seq", 4)
    lib_par, _album, _par_files = _lyric_album(tmp, "fetch_lib_par", 4)
    LATENCY = 0.15

    def fake_fetch(cfg, artist, title, album_name=None, duration=None, **kw):
        time.sleep(LATENCY)
        return {"provider": "lrclib", "provider_label": "LRCLIB",
                "synced": f"[00:01.00] {title}", "plain": title, "score": 0.99}

    real = lyrics_fetch.fetch_lyrics
    lyrics_fetch.fetch_lyrics = fake_fetch

    def run(lib, worker_limit):
        # Each run gets its OWN library: script 13 fills what is missing and
        # never touches a track that already holds lyrics (force included), so
        # a second run over the same album would time a run that skips
        # everything — a timing check, not a force-semantics check.
        c = cfg(music_folder=lib, lyrics_format="EMBEDDED",
                worker_limit=worker_limit)
        t0 = time.perf_counter()
        st = lyrics_fetch.run_fetch_lyrics(c)
        return st, time.perf_counter() - t0

    try:
        seq_stats, t_seq = run(lib_seq, 1)
        par_stats, t_par = run(lib_par, 0)
    finally:
        lyrics_fetch.fetch_lyrics = real

    ok(seq_stats["modified_count"] == par_stats["modified_count"] == 4,
       f"script 13: both runs write every track ({seq_stats['modified_count']}"
       f"/{par_stats['modified_count']} of 4)")
    ok(par_stats["by_provider"] == seq_stats["by_provider"],
       "script 13: the provider tally is the same either way")
    ok(t_par < t_seq * 0.6,
       f"script 13: {len(_seq_files)} tracks x {LATENCY * 1000:.0f} ms of provider "
       f"wait take {t_par:.2f} s with lanes vs {t_seq:.2f} s one at a time "
       f"({t_seq / t_par:.1f}x)")

    # The politeness gap is PER HOST now. The REAL `_request` and its gate run
    # here against a stub transport that notes when each call actually left, so
    # what is timed is what a provider would pay: two different hosts start
    # together, the same host still waits out the full gap.
    class _Response:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Transport:
        """`urllib` stand-in: urlopen records when a request left."""

        error = urllib.error

        def __init__(self, starts):
            self.starts = starts
            self.parse = urllib.parse
            self.request = self

        def Request(self, url, data=None, headers=None):
            return url

        def urlopen(self, req, timeout=None):
            self.starts.append(time.perf_counter())
            return _Response()

    def gate_starts(*urls):
        starts = []
        real_urllib = lyrics_providers.urllib
        lyrics_providers.urllib = _Transport(starts)
        lyrics_providers._last_request.clear()
        try:
            for url in urls:
                lyrics_providers._request(url, retries=1)
            return list(starts)
        finally:
            lyrics_providers.urllib = real_urllib
            lyrics_providers._last_request.clear()

    first, second = gate_starts("https://a.example/x", "https://b.example/x")
    ok(second - first < lyrics_providers._MIN_GAP / 2,
       f"script 13: two DIFFERENT hosts start without queueing "
       f"({second - first:.3f}s apart)")
    first, second = gate_starts("https://a.example/x", "https://a.example/x")
    ok(second - first >= lyrics_providers._MIN_GAP - 0.02,
       f"script 13: the SAME host still stays {lyrics_providers._MIN_GAP}s apart "
       f"({second - first:.3f}s)")


def check_lyrics_fetch_never_replaces(tmp):
    """Script 13 FILLS: a stored text is never replaced, force included.

    This is the owner-reported damage (issue #74): `force_lyrics` on the fetch
    script re-asked every provider for the whole library and overwrote the
    words the files already held — an import's answer, a provider hit from an
    earlier run, a person's own edit. A run now asks only for what is MISSING,
    and the manual per-track route (`POST /api/lyrics/auto` with force) is the
    one place that may replace words, because a person asked for that track.
    """
    from mlo import lyrics_fetch

    lib, _album, files = _lyric_album(tmp, "fill_lib", 2)
    mine = "[00:05.00] a line only I wrote"
    af = mlo_audio.AudioFile(files[0])
    af.set_lyrics(mine)

    asked = []

    def fake_fetch(cfg, artist, title, album_name=None, duration=None, **kw):
        asked.append(title)
        return {"provider": "lrclib", "provider_label": "LRCLIB",
                "synced": "[00:01.00] provider text", "plain": "provider text",
                "score": 0.99}

    real = lyrics_fetch.fetch_lyrics
    lyrics_fetch.fetch_lyrics = fake_fetch
    try:
        stats = lyrics_fetch.run_fetch_lyrics(cfg(
            music_folder=lib, lyrics_format="EMBEDDED", force_lyrics=True))
    finally:
        lyrics_fetch.fetch_lyrics = real

    ok(mlo_audio.AudioFile(files[0]).get_lyrics() == mine,
       "script 13: the stored text survives a forced run byte for byte")
    ok(len(asked) == 1 and "Song 2" in asked[0],
       f"script 13: only the track without lyrics was searched ({asked})")
    ok(stats["modified_count"] == 1 and stats["skipped_count"] == 1,
       f"script 13: one filled, one skipped "
       f"(modified={stats['modified_count']}, skipped={stats['skipped_count']})")

    # …and an INSTRUMENTAL track is never searched at all: the file states
    # there are no words, so a hit could only be written and then deleted
    # again by the very next pass (`_process_lyrics_for_audio`).
    lib2, _album2, files2 = _lyric_album(tmp, "fill_inst_lib", 1)
    af2 = mlo_audio.AudioFile(files2[0])
    af2.set_tag("INSTRUMENTAL", "1")
    asked.clear()
    lyrics_fetch.fetch_lyrics = fake_fetch
    try:
        lyrics_fetch.run_fetch_lyrics(cfg(
            music_folder=lib2, lyrics_format="EMBEDDED", force_lyrics=True))
    finally:
        lyrics_fetch.fetch_lyrics = real
    ok(not asked and not (mlo_audio.AudioFile(files2[0]).get_lyrics() or "").strip(),
       f"script 13: an instrumental is never searched ({asked})")


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
    # ENCODER_VERSION is OFF by default since v4.4.0 (it re-encoded whole
    # libraries after every tool upgrade), so this check asks for it: the
    # marker is what proves the pass really handed the file to oxipng instead
    # of stamping it (Settings → Encoder Tags is where a user asks for it too).
    version_on = dict(DEFAULT_CONFIG["encoder_tags"]["png"], ENCODER_VERSION=True)
    images.run_process_images(cfg(
        music_folder=lib, targets=[src], reencode_images=True,
        images_convert_to_jpeg=False, images_convert_lossless_to_png=True,
        rename_to_cover=False, encoder_tags={"png": version_on}))

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


def check_images_oxipng_thread_cap(tmp):
    """oxipng runs as one lane of the pool, not on every core.

    Oxipng's documented default is every logical CPU, so a 2-lane image pass
    ran 2 x cores threads while cjxl and flac already had their own
    --num_threads/-threads share. The oxipng argv must now carry this lane's
    share of the thread budget, and a 0/unknown share must leave the flag off
    so the tool keeps its own default.
    """
    if not HAS_PIL:
        return skip("script 5: oxipng thread cap (Pillow missing)")
    from mlo import stats as stats_mod
    from mlo.tools import detect_all_tools
    ox = detect_all_tools().get("oxipng") or {}
    if not ox.get("oxipng_exe"):
        return skip("script 5: oxipng thread cap (oxipng not installed)")

    lib = os.path.join(tmp, "oxipng_threads_lib")
    os.makedirs(lib)
    files = [make_image(os.path.join(lib, f"{i:02d} - Art.png"), (400, 400),
                        fmt="PNG") for i in (1, 2)]

    real_run = images.run_tool
    seen = []

    def counting_run(args, *a, **kw):
        if str(args[0]).lower().endswith("oxipng.exe"):
            seen.append([str(x) for x in args])
        return real_run(args, *a, **kw)

    images.run_tool = counting_run
    try:
        # 2 files under Worker threads = 8: the pool holds 2 lanes, so each
        # lane's share of the 8-thread budget is 4 — the value the flag carries.
        images.run_process_images(cfg(
            music_folder=lib, targets=list(files), reencode_images=True,
            images_convert_to_jpeg=False, images_convert_lossless_to_png=False,
            rename_to_cover=False, worker_limit=8))
    finally:
        images.run_tool = real_run

    share = stats_mod.tool_threads(cfg(worker_limit=8), 2)
    ok(len(seen) == len(files),
       f"script 5: every PNG still went through oxipng "
       f"({len(seen)} of {len(files)})")
    ok(all("--threads" in argv and argv[argv.index("--threads") + 1] == str(share)
           for argv in seen),
       f"script 5: oxipng argv carries the lane share (--threads {share}), "
       f"not the default every-core claim (measured {seen[:1]})")

    # A 0/unknown share (an older caller that passed none) must add no flag.
    seen.clear()
    images.run_tool = counting_run
    try:
        images._process_png_in_place((
            ox["oxipng_exe"], ox.get("version"), files[0], True, False,
            False, 6, {}, None, 0))
    finally:
        images.run_tool = real_run
    ok(len(seen) == 1 and "--threads" not in seen[0],
       f"script 5: a 0/unknown share adds no thread flag (measured {seen[:1]})")


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


def check_accurip_album_lanes(tmp):
    """Script 9: one CD album at a time, or a pool of them.

    An album's AccurateRip pass owns its folder, its cue and its CD-{n} files,
    and its slow half is one decode + CUETools verification per track. The fake
    stands in for that half (`_generate_via_cuetools`) so this measures the
    LANES, not ffmpeg.
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


# --------------------------------------------------------------------------- #
# Script 23 — Optimize tags: the strip is ONE write per file it really cleans,
# and NO write at all for the files it finds nothing on.
# --------------------------------------------------------------------------- #
def check_tag_hygiene_writes_only_what_it_changes(tmp):
    """The scoped strip: one container save for the dirty file, zero for the
    clean ones.

    A "clean the tags" pass that rewrote every file it looked at would cost a
    whole-library container save (and with flac_no_padding, a re-encode) for a
    library that had nothing wrong — and the details menu offers this on any
    album. The counter is on the CONTAINER write (`AudioFile._save_container`,
    the single path to `mutagen.save`), not on the calls into it.
    """
    from mlo import audio as audio_mod
    from mlo.taghygiene import run_tag_hygiene

    album = os.path.join(tmp, "taghygiene_album")
    os.makedirs(album, exist_ok=True)
    dirty = make_flac(os.path.join(album, "01 - Dirty.flac"), 1,
                      {"TITLE": "Lost Umbrella", "ARTIST": "Radiohead",
                       "ALBUM": "The Album", "ARTISTALIAS": "Radiohead",
                       "COMMENT": "ripped by some tool"})
    clean = make_flac(os.path.join(album, "02 - Clean.flac"), 1,
                      {"TITLE": "Song", "ARTIST": "Radiohead",
                       "ALBUM": "The Album"})

    saves = []
    real_save = audio_mod.AudioFile._save_container

    def counting_save(self, *a, **kw):
        saves.append(self.path)
        return real_save(self, *a, **kw)

    audio_mod.AudioFile._save_container = counting_save
    try:
        stats = run_tag_hygiene(cfg(music_folder=album, targets=[album],
                                    strip_unknown_tags=True))
    finally:
        audio_mod.AudioFile._save_container = real_save

    ok(len(saves) == 1 and saves[0] == dirty,
       f"script 23: ONE container write for 2 files — the file that had excess "
       f"(was: a write per file opened; measured {len(saves)}: {saves})")
    ok(stats["modified_count"] == 1 and stats["tags_removed"] == 2
       and stats["skipped_count"] == 1,
       f"script 23: and it reports what it removed "
       f"({stats['modified_count']} cleaned, {stats['tags_removed']} tags, "
       f"{stats['skipped_count']} already clean)")
    from mlo.audio import AudioFile
    ok(AudioFile(clean).get_tag("ARTIST") == "Radiohead"
       and AudioFile(dirty).get_tag("ARTISTALIAS") is None,
       "script 23: the clean file kept its tags and the dirty one lost its "
       "unneeded alias")


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


def check_ffprobe_asked_only_when_needed(tmp):
    """No pass spawns ffprobe for an answer it already holds.

    Counted by SPAWN COUNT, not by time (a timing assertion is flaky):
      * script 7's meter took the channel count from the container the pass
        ALREADY had open, instead of one `ffprobe` process per track — a
        process spawn per file, per run, for a number the header states;
      * a converted file's duration came from the container this pipeline had
        just written and was about to open for its tags anyway, so one
        conversion spawns ffprobe once (the source probe), not twice.
    """
    from mlo import dr as mlo_dr
    from mlo import flac as mlo_flac
    from mlo import loudness
    if not (FFMPEG_EXE and FFPROBE_EXE):
        skip("no ffmpeg/ffprobe: the DR meter and the conversion cannot run")
        return

    # --- script 7: the DR pass asks the handle it is holding --------------
    album = os.path.join(tmp, "dr_handle_album")
    os.makedirs(album)
    files = [_tone_flac(os.path.join(album, f"0{n} - Track.flac"), 7)
             for n in (1, 2)]
    opened = mlo_audio.AudioFile(files[0])
    from_handle = (mlo_dr.handle_channels(opened)
                   if hasattr(mlo_dr, "handle_channels") else 0)
    ok(from_handle == 2,
       f"script 7: the open handle states the channel count "
       f"(measured {from_handle})")

    probes, restore_probe = count_calls(mlo_dr, "probe_channels")
    try:
        modified, failures = loudness._dr_album(
            album, FFMPEG_EXE, False, write_tags=True,
            config=cfg(music_folder=album))
    finally:
        restore_probe()

    ok(not probes,
       f"script 7: the DR pass spawns NO ffprobe per track — the handle it "
       f"already holds answers (measured {len(probes)} probes for "
       f"{len(files)} tracks)")
    ok(not failures and modified == len(files),
       f"script 7: every track is still measured and written (modified="
       f"{modified}, failures={failures})")
    written = str(mlo_audio.AudioFile(files[0]).get_tag("DYNAMIC RANGE") or "").strip()
    ok(written and written == str(mlo_dr.measure_track(files[0], FFMPEG_EXE)),
       f"script 7: the tag holds the meter's own number ({written})")

    # --- script 3 / the import: one ffprobe per converted file ------------
    src = os.path.join(tmp, "convert_once.wav")
    with wave.open(src, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x01\x00\x02" * (44100 * 2))

    spawns = []
    real_run = mlo_flac.run_tool

    def counting_run(args, *a, **kw):
        spawns.append(os.path.basename(str(args[0])).lower())
        return real_run(args, *a, **kw)

    mlo_flac.run_tool = counting_run
    try:
        name, converted, info, _rem, _add = mlo_flac._convert_lossless_source((
            FFMPEG_EXE, FFPROBE_EXE, METAFLAC_EXE, src, 5, "1.5.0", {},
            cfg(music_folder=tmp, lossless_remove_original=False),
            1))         # the lane's share of the thread budget (script 3)
    finally:
        mlo_flac.run_tool = real_run

    out = os.path.splitext(src)[0] + ".flac"
    ok(converted and os.path.exists(out) and os.path.exists(src),
       f"script 3: the WAV still converts and the original is kept ({info})")
    ok(sum(1 for s in spawns if s.startswith("ffprobe")) == 1,
       f"script 3: ONE ffprobe per converted file — the source probe; the "
       f"output's duration came from the container we were about to open "
       f"anyway (measured {spawns.count('ffprobe.exe') + spawns.count('ffprobe')}"
       f" ffprobe spawns: {spawns})")
    tags = {str(k).lower(): v
            for k, v in (mlo_audio.AudioFile(out).all_tags() or {}).items()}
    ok(str(tags.get("encoder_program") or "").strip() == "FLAC reference encoder",
       f"script 3: the converted file still carries this pipeline's ENCODER "
       f"identity (measured {tags.get('encoder_program')!r})")


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
        ("script 3  Optimize FLACs (level only)", check_encoder_version_does_not_reencode),
        ("script 5  Process images (alpha probe)", check_images_alpha_probe),
        ("script 5  Process images (progressive)", check_images_progressive_honoured),
        ("script 5  Process images (converted PNG)", check_images_converted_png_optimized),
        ("script 5  Process images (oxipng lanes)", check_images_oxipng_thread_cap),
        ("script 11 Remux videos", check_remux_single_probe),
        ("script 13 Fetch lyrics (lanes)", check_lyrics_fetch_concurrency),
        ("script 13 Fetch lyrics (fills only)", check_lyrics_fetch_never_replaces),
        ("script 9  AccurateRip (lanes)", check_accurip_album_lanes),
        ("script 20 Scan library layout (lanes)", check_layout_scan_lanes),
        ("script 1  Format lyrics (one open)", check_lyrics_one_open_per_track),
        ("script 1  Format lyrics (album pass reads)", check_lyrics_album_pass_still_reads),
        ("script 10 Format all (cover cache)", check_format_all_cover_prepared_once),
        ("script 23 Optimize tags (writes)", check_tag_hygiene_writes_only_what_it_changes),
        ("script 7/3 ffprobe spawns", check_ffprobe_asked_only_when_needed),
        ("all       atomic sidecar writes", check_fsync_dir),
        ("all       the worker budget knob", check_worker_budget_semantics),
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
