#!/usr/bin/env python3
"""Create a small synthetic library for end-to-end testing.

Builds <temp>/TestLib/<Artist>/<Album>/ with real FLAC files (encoded
with the bundled flac.exe from generated WAVs), tags via mutagen, one
malformed-lyrics track, covers, CUE + LOG files, plus one .mp3.

Run:  python tools/make_test_library.py [--clean]
      --clean first removes `mlo_testlib_*` trees left under the OS temp dir
      by earlier runs; the library this run builds is never removed (pass
      its path to the suite you want to run against it).
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
import math

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLAC_EXE = None
try:
    for entry in os.listdir(os.path.join(ROOT, ".dependencies")):
        if entry.lower().startswith("flac"):
            cand = os.path.join(ROOT, ".dependencies", entry, "flac.exe")
            if os.path.isfile(cand):
                FLAC_EXE = cand
                break
except FileNotFoundError:
    pass
if FLAC_EXE is None:
    FLAC_EXE = shutil.which("flac") or None

# Pure-Python fallback: when no flac.exe toolchain is present, encode
# minimal valid MP3 frames instead so the API/UI can be smoke-tested.
PURE_PYTHON = FLAC_EXE is None
if PURE_PYTHON:
    print("[make_test_library] flac not found — "
          "using pure-Python MP3 fallback (no real FLACs)")

BAD_LYRICS = (
    "[00:00.00][00:45.53]Stretching, filing[00:46.86]Against her skin\n"
    "[00:48.16]Blessed are those\n"
    "[00:49.45]Who are not kin\r\n"
    "[00:50.67]In sin we breathe\n"
    "\n\n\n"
    "[00:53.21]Duct tape her legs\n"
    "[02:53.18]\n"
)

GOOD_LYRICS = (
    "[00:01.00]First line\n"
    "[00:05.50]Second line\n"
    "[00:10.00]Third line"
)


def make_wav(path, seconds=2, freq=440):
    rate = 44100
    frames = int(rate * seconds)
    with wave.open(path, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        chunks = []
        for i in range(frames):
            v = int(12000 * math.sin(2 * math.pi * freq * i / rate))
            chunks.append(struct.pack("<hh", v, v))
        w.writeframes(b"".join(chunks))


def flac_encode(wav, out):
    if PURE_PYTHON:
        write_mp3(out, seconds=2)
        return
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", out, wav],
                   check=True, capture_output=True)


def write_mp3(path, seconds=2):
    """Craft a valid MPEG-1 Layer III silence stream (32kbps mono)."""
    frame = bytes.fromhex("ff fb 10 00") + b"\x00" * 100
    n = int(seconds * 38.28) + 1
    with open(path, "wb") as f:
        for _ in range(n):
            f.write(frame)


def tag_mp3(path, tags):
    from mutagen.mp3 import MP3
    from mutagen.id3 import TIT2, TPE1, TALB, TPE2, TDRC, TRCK, TXXX, USLT, TCON
    a = MP3(path)
    if a.tags is None:
        a.add_tags()
    frame_map = {
        "TITLE": TIT2, "ARTIST": TPE1, "ALBUM": TALB, "ALBUMARTIST": TPE2,
        "DATE": TDRC, "TRACKNUMBER": TRCK, "GENRE": TCON,
    }
    for k, v in tags.items():
        if k in frame_map:
            a.tags.add(frame_map[k](encoding=3, text=str(v)))
        elif k == "LYRICS":
            a.tags.add(USLT(encoding=3, lang="eng", desc="", text=str(v)))
        else:
            a.tags.add(TXXX(encoding=3, desc=k, text=str(v)))
    a.save()


def tag(path, tags):
    from mutagen.flac import FLAC
    a = FLAC(path)
    for k, v in tags.items():
        a[k] = v
    a.save()


def build(base):
    albums = [
        ("Artist Alpha", "2020 - First Album", [
            ("01 - Intro.flac", {"GENRE": "Rock",
                                 "ITUNESADVISORY": "0",
                                 "INSTRUMENTAL": "0",
                                 "REPLAYGAIN_TRACK_GAIN": "-3.00 dB",
                                 "REPLAYGAIN_TRACK_PEAK": "0.5",
                                 "REPLAYGAIN_ALBUM_GAIN": "-3.00 dB",
                                 "REPLAYGAIN_ALBUM_PEAK": "0.5",
                                 "DYNAMIC RANGE": "8",
                                 "MEDIA": "Digital Media",
                                 "SOURCE": "Digital",
                                 "ALBUMITUNESADVISORY": "0",
                                 "ALBUM DYNAMIC RANGE": "8",
                                 "LYRICS": BAD_LYRICS},
             None),
            ("02 - Second.flac", None, None),
            ("03 - Bonus.mp3", None, None),
        ]),
        ("Artist Beta", "2021 - Vinyl Rip", [
            ("01 - Side A.flac", {"GENRE": "Jazz",
                                  "ITUNESADVISORY": "1",
                                  "INSTRUMENTAL": "0",
                                  "REPLAYGAIN_TRACK_GAIN": "-1.00 dB",
                                  "REPLAYGAIN_TRACK_PEAK": "0.9",
                                  "REPLAYGAIN_ALBUM_GAIN": "-1.00 dB",
                                  "REPLAYGAIN_ALBUM_PEAK": "0.9",
                                  "DYNAMIC RANGE": "12",
                                  "MEDIA": "CD",
                                  "SOURCE": "",
                                  "ALBUMITUNESADVISORY": "1",
                                  "ALBUM DYNAMIC RANGE": "12",
                                  "LYRICS": GOOD_LYRICS},
             None),
        ]),
        ("Artist Gamma", "2022 - Singles", [
            ("01 - Hit.flac", {"GENRE": "Pop",
                               "ITUNESADVISORY": "0",
                               "INSTRUMENTAL": "1",
                               "REPLAYGAIN_TRACK_GAIN": "-2.00 dB",
                               "REPLAYGAIN_TRACK_PEAK": "0.7",
                               "REPLAYGAIN_ALBUM_GAIN": "-2.00 dB",
                               "REPLAYGAIN_ALBUM_PEAK": "0.7",
                               "DYNAMIC RANGE": "9",
                               "MEDIA": "Digital Media",
                               "SOURCE": "Web",
                               "ALBUMITUNESADVISORY": "0",
                               "ALBUM DYNAMIC RANGE": "9"},
             None),
        ]),
    ]

    tmp = tempfile.mkdtemp(prefix="mlo_testlib_")
    lib = os.path.join(tmp, base)
    wav = os.path.join(tmp, "tone.wav")

    for artist, album, tracks in albums:
        adir = os.path.join(lib, artist, album)
        os.makedirs(adir)
        make_wav(wav)
        for i, (fname, tags, _x) in enumerate(tracks):
            was_flac = fname.endswith(".flac")
            if PURE_PYTHON and was_flac:
                fname = fname[:-5] + ".mp3"
            fpath = os.path.join(adir, fname)
            if was_flac:
                flac_encode(wav, fpath)
                base_tags = {
                    "ARTIST": artist, "ALBUMARTIST": artist,
                    "ALBUM": album.split(" - ", 1)[1],
                    "TITLE": os.path.splitext(fname)[0],
                    "TRACKNUMBER": f"{i+1:02d}",
                    "DATE": album.split(" - ")[0],
                }
                if PURE_PYTHON:
                    tag_mp3(fpath, base_tags)
                    if tags:
                        tag_mp3(fpath, tags)
                else:
                    tag(fpath, base_tags)
                    if tags:
                        tag(fpath, tags)
            else:
                # a small mp3 (silence, 32kbps) via mutagen-friendly path
                from mutagen.mp3 import MP3
                with open(fpath, "wb") as f:
                    f.write(b"")   # placeholder; MP3 created below
                make_mp3(fpath, artist, album, fname)

        # album extras
        with open(os.path.join(adir, "cover.jpg"), "wb") as f:
            from PIL import Image
            img = Image.new("RGB", (100, 100), (120, 60, 200))
            img.save(f, "JPEG")
        if "Vinyl" in album or "Singles" not in album:
            with open(os.path.join(adir, f"{album}.cue"), "w") as f:
                f.write('FILE "01 - Side A.flac" WAVE\n'
                        '  TRACK 01 AUDIO\n'
                        '    INDEX 01 00:00:00\n')
            with open(os.path.join(adir, "rip.log"), "w") as f:
                f.write("Exact Audio Copy log\nScore 100\n")

    # an untidy stray file
    with open(os.path.join(lib, "Artist Alpha", "2020 - First Album",
                           "playlist.m3u"), "w") as f:
        f.write("01 - Intro.flac\n")

    print(lib)
    return lib


def make_mp3(path, artist, album, fname):
    """Write a tiny tagged MP3: no bundled mp3 encoder, so craft a valid
    MPEG-1 Layer III silence frame stream."""
    # No bundled mp3 encoder: craft minimal valid MP3 (MPEG1 Layer3
    # 32kbps mono silence frames).
    write_mp3(path, seconds=1)
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import TIT2, TPE1, TALB, TPE2, TDRC, TRCK
        a = MP3(path)
        if a.tags is None:
            a.add_tags()
        a.tags.add(TIT2(encoding=3, text=os.path.splitext(fname)[0]))
        a.tags.add(TPE1(encoding=3, text=artist))
        a.tags.add(TPE2(encoding=3, text=artist))
        a.tags.add(TALB(encoding=3, text=album.split(" - ", 1)[1]))
        a.tags.add(TDRC(encoding=3, text=album.split(" - ")[0]))
        a.tags.add(TRCK(encoding=3, text="03"))
        a.save()
    except Exception as e:
        print("mp3 tag skip:", e)


def clean_stale():
    """Remove `mlo_testlib_*` trees left in the OS temp dir by earlier runs.

    Opt-in via `--clean`. Only other runs' trees are touched — whatever this
    run builds is created afterwards and always left in place for the caller.
    Returns the number of trees removed.
    """
    tmp = tempfile.gettempdir()
    try:
        names = os.listdir(tmp)
    except OSError:
        return 0
    removed = 0
    for name in names:
        if not name.startswith("mlo_testlib_"):
            continue
        path = os.path.join(tmp, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


if __name__ == "__main__":
    if "--clean" in sys.argv[1:]:
        print(f"[make_test_library] removed {clean_stale()} stale temp tree(s)")
    build("TestLib")
