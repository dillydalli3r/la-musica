#!/usr/bin/env python3
"""Verification of the merged mlo/lyrics.py (remote v1.0.10 base + the
v1.1.0 pending-stamp / safety behaviors) and of the ported
AudioFile.set_any_tag / delete_any_tag in mlo/audio.py.

Run:  python tools/test_lyrics_merge.py
"""
import math
import os
import random
import struct
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo.audio import AudioFile
from mlo.lyrics import format_lyrics_text, _process_lyrics_for_audio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLAC_EXE = None
_deps = os.path.join(ROOT, ".dependencies")
if os.path.isdir(_deps):
    for entry in os.listdir(_deps):
        if entry.lower().startswith("flac"):
            cand = os.path.join(_deps, entry, "flac.exe")
            if os.path.isfile(cand):
                FLAC_EXE = cand
                break

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1


# ----------------------------------------------------------------------
# format_lyrics_text unit cases
# ----------------------------------------------------------------------
CASES = [
    # Start marker dropped; merged stamps split; mid-line stamp split.
    ("[00:00.00][00:45.53]Stretching, filing[00:46.86]Against her skin",
     "[00:45.53]Stretching, filing\n[00:46.86]Against her skin"),
    # Timestamp-only line lends its stamp to the next untimed line.
    ("[00:12.34]\nnext", "[00:12.34]next"),
    # ... but a blank line in between kills the pending stamp.
    ("[00:12.34]\n\nnext", "next"),
    # Stacked repeat markers -> one line per stamp.
    ("[00:20][01:20][02:20]Chorus",
     "[00:20.00]Chorus\n[01:20.00]Chorus\n[02:20.00]Chorus"),
    # Rounding carries into the seconds field at precision 2.
    ("[00:05.999]x", "[00:06.00]x"),
    # Plain text untouched.
    ("just a verse\n\nanother", "just a verse\n\nanother"),
    # Metadata lines dropped with strip_metadata=True.
    ("[ar:Artist]\n[ti:Title]\n[00:01.00]x", "[00:01.00]x"),
    # Trailing timestamp-only line dropped at EOF.
    ("[00:01.00]a\n[00:02.00]", "[00:01.00]a"),
    # Standalone zero stamp labels real text and is kept.
    ("[00:00.00]first line", "[00:00.00]first line"),
    # Zero-only line holds as a pending stamp for untimed text.
    ("[00:00.00]\nIntro text", "[00:00.00]Intro text"),
    # Pending stamps die when the next line carries its own timestamps.
    ("[00:12.34]\n[00:30.00]timed", "[00:30.00]timed"),
    # Several pending stamps each label the next untimed line.
    ("[00:10.00][00:20.00]\ntext",
     "[00:10.00]text\n[00:20.00]text"),
    # Space directly after a timestamp removed; trailing spaces trimmed.
    ("[00:05.50]  hello", "[00:05.50]hello"),
    ("[00:01.00]one two [00:02.00]three",
     "[00:01.00]one two\n[00:02.00]three"),
    # CRLF + blank collapse + end trimming.
    ("\n\n[00:01.00]x\r\n\r\n\r\n[00:02.00]y\r\n",
     "[00:01.00]x\n\n[00:02.00]y"),
    # 1-digit fields normalized.
    ("[0:5.500]x", "[00:05.50]x"),
    # Empty / whitespace-only input.
    ("", ""),
    ("   \n  \n", ""),
    # Full malformed sample (v1.1.0 report case).
    ("[00:00.00][00:45.53]Stretching, filing[00:46.86]Against her skin\n"
     "[00:48.16]Blessed are those\r\n"
     "\n\n\n"
     "[00:53.21]Duct tape her legs\n"
     "[02:53.18]\n",
     "[00:45.53]Stretching, filing\n"
     "[00:46.86]Against her skin\n"
     "[00:48.16]Blessed are those\n"
     "\n"
     "[00:53.21]Duct tape her legs"),
]

for src, want in CASES:
    got = format_lyrics_text(src, lrc_extended_enabled=False, lrc_add_zero_timestamp=False)
    ok(got == want, f"case {src!r}: got {got!r}, want {want!r}")
    ok(format_lyrics_text(got, lrc_extended_enabled=False, lrc_add_zero_timestamp=False) == got, f"case {src!r}: not idempotent")

# precision=3 keeps 3 fraction digits (1-digit input padded out).
ok(format_lyrics_text("[00:05.9]x", precision=3, lrc_extended_enabled=False) == "[00:05.900]x",
   "precision 3 pads fraction")
ok(format_lyrics_text("[00:05.999]x", precision=3, lrc_extended_enabled=False) == "[00:05.999]x",
   "precision 3 keeps ms")
ok(format_lyrics_text("[00:05.9999]x", precision=3, lrc_extended_enabled=False) == "[00:05.999]x",
   "precision 3 truncates extra digits")  # int('9999'[:3]) -> 999
ok(format_lyrics_text("[00:00.000][00:45.530]text", precision=3, lrc_extended_enabled=False)
   == "[00:45.530]text",
   "precision 3 start marker dropped")

# strip_metadata=False keeps metadata lines.
ok(format_lyrics_text("[ar:Artist]\n[00:01.00]x", strip_metadata=False)
   == "[ar:Artist]\n[00:01.00]x", "strip_metadata False keeps meta")
# collapse_blank_lines=False keeps repeated blank lines (ends trimmed).
ok(format_lyrics_text("a\n\n\nb\n\n", collapse_blank_lines=False)
   == "a\n\n\nb", "collapse False keeps inner blanks")

# lrc_add_zero_timestamp: first lyric line must start with [00:00.00] when enabled
ok(format_lyrics_text("[00:05.00]Hello", lrc_add_zero_timestamp=True, lrc_extended_enabled=False)
   == "[00:00.00]Hello\n[00:05.00]Hello", "zero: add to timed first line")
ok(format_lyrics_text("[00:00.00]Hello", lrc_add_zero_timestamp=True)
   == "[00:00.00]Hello", "zero: already present -> no duplicate")
ok(format_lyrics_text("Hello world", lrc_add_zero_timestamp=True)
   == "[00:00.00]Hello world", "zero: untimed first line becomes timed")
ok(format_lyrics_text("[00:05.00]Hello", lrc_add_zero_timestamp=False, lrc_extended_enabled=False)
   == "[00:05.00]Hello", "zero: disabled -> no add")
# idempotent with zero
once = format_lyrics_text("[00:05.00]Hello", lrc_add_zero_timestamp=True, lrc_extended_enabled=False)
twice = format_lyrics_text(once, lrc_add_zero_timestamp=True, lrc_extended_enabled=False)
ok(twice == once, "zero: idempotent")
# precision 3 variant
ok(format_lyrics_text("[00:05.00]Hello", lrc_add_zero_timestamp=True, precision=3, lrc_extended_enabled=False)
   == "[00:00.000]Hello\n[00:05.000]Hello", "zero: precision 3")

# ----------------------------------------------------------------------
# Fuzz idempotency: f(f(x)) == f(x) across the config knobs
# ----------------------------------------------------------------------
rng = random.Random(20260821)

TS_BITS = [
    "[00:00.00]", "[00:12.34]", "[0:5.9]", "[00:05.999]", "[1:2]",
    "[10:20.30]", "[00:59.99]", "[99:59.999]", "[00:00]", "[000:00.0]",
]
TEXT_BITS = [
    "la la la", "Chorus", "next", "[Verse 1]", "[ar:Someone]", "[ti:X]",
    "against her skin", "  spaced  out  ", "日本語歌詞", "accénted",
    "tab\there", "-----", "...", "]", "[", "[]", "[00:", "x" * 40,
]


def junk_line():
    parts = []
    for _ in range(rng.randint(0, 4)):
        if rng.random() < 0.45:
            parts.append(rng.choice(TS_BITS))
            if rng.random() < 0.4:
                parts.append(" " * rng.randint(1, 3))
        else:
            parts.append(rng.choice(TEXT_BITS))
    return "".join(parts)


def junk_input():
    n = rng.randint(0, 8)
    seps = ["\n", "\n", "\n", "\r\n", "\r", "\n\n", "\n\n\n"]
    return rng.choice(seps).join(junk_line() for _ in range(n))


FUZZ_CFGS = [
    {},
    {"precision": 3},
    {"strip_metadata": False},
    {"collapse_blank_lines": False},
]

fuzz_inputs = [junk_input() for _ in range(220)]
for i, x in enumerate(fuzz_inputs):
    for cfg in FUZZ_CFGS:
        once = format_lyrics_text(x, **cfg)
        twice = format_lyrics_text(once, **cfg)
        ok(twice == once,
           f"fuzz #{i} cfg={cfg}: input {x!r} -> {once!r} then {twice!r}")

# ----------------------------------------------------------------------
# Real-file fixtures
# ----------------------------------------------------------------------


def make_wav(path, seconds=1, freq=440):
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


def make_flac(path):
    assert FLAC_EXE, "flac.exe not found under .dependencies"
    wav = path + ".wav"
    make_wav(wav)
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                   check=True, capture_output=True)
    os.remove(wav)


def make_mp3(path):
    # Minimal valid MPEG-1 Layer III 32kbps silence frames.
    frame = bytes.fromhex("ff fb 10 00") + b"\x00" * 100
    with open(path, "wb") as f:
        for _ in range(60):
            f.write(frame)
    from mutagen.mp3 import MP3
    a = MP3(path)
    if a.tags is None:
        a.add_tags()
    a.save()


tmp = tempfile.mkdtemp(prefix="mlo_merge_test_")

# ----------------------------------------------------------------------
# End-to-end: malformed tag + sidecar, format EMBEDDED, happy path
# ----------------------------------------------------------------------
flac1 = os.path.join(tmp, "track.flac")
make_flac(flac1)
from mutagen.flac import FLAC as MutFLAC

BAD_LYRICS = (
    "[00:00.00][00:45.53]Stretching, filing[00:46.86]Against her skin\n"
    "[00:48.16]Blessed are those\r\n"
    "\n\n\n"
    "[00:53.21]Duct tape her legs\n"
    "[02:53.18]\n"
)
LRC_CONTENT = "[00:01.00]First line\n[00:05.50]Second line\n[00:10.00]Third line"

f = MutFLAC(flac1)
f["LYRICS"] = BAD_LYRICS
f.save()
lrc1 = os.path.splitext(flac1)[0] + ".lrc"
with open(lrc1, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(LRC_CONTENT + "\r\n")

cfg = {"lyrics_format": "EMBEDDED", "optimize_lrc": True,
       "optimize_embedded_lyrics": True}
status, _b_rem, _b_add, info = _process_lyrics_for_audio(flac1, cfg)
ok(status == "modified", f"e2e status {status} ({info})")
ok(not os.path.exists(lrc1), "e2e: no LRC sidecar remains")
after = AudioFile(flac1)
ok(after.get_lyrics() == LRC_CONTENT,
   f"e2e: tag cleaned to canonical LRC text, got {after.get_lyrics()!r}")
ok(format_lyrics_text(after.get_lyrics()) == after.get_lyrics(),
   "e2e: embedded tag is canonical")
ok(after.get_tag("INSTRUMENTAL") is None,
   "e2e: INSTRUMENTAL untouched when writes succeed")
# Re-run must be a no-op.
status2, _r, _a, info2 = _process_lyrics_for_audio(flac1, cfg)
ok(status2 == "unchanged", f"e2e re-run status {status2} ({info2})")

# INSTRUMENTAL auto-flip reflects persisted state after a reload.
flac2 = os.path.join(tmp, "inst.flac")
make_flac(flac2)
f = MutFLAC(flac2)
f["LYRICS"] = "[00:12.34]\nnext"
f["INSTRUMENTAL"] = "1"
f.save()
status3, _r, _a, info3 = _process_lyrics_for_audio(flac2, cfg)
ok(status3 == "modified", f"inst flip status {status3} ({info3})")
ok(AudioFile(flac2).get_tag("INSTRUMENTAL") == "0",
   "inst flip: INSTRUMENTAL cleared with lyrics persisted")
ok(AudioFile(flac2).get_lyrics() == "[00:12.34]next",
   "inst flip: pending-stamp attachment through the pipeline")

# ----------------------------------------------------------------------
# Forced failure: failed embed must keep the sidecar, return fail
# ----------------------------------------------------------------------
flac3 = os.path.join(tmp, "failcase.flac")
make_flac(flac3)
lrc3 = os.path.splitext(flac3)[0] + ".lrc"
with open(lrc3, "w", encoding="utf-8", newline="\n") as fh:
    fh.write("[00:01.00]Only copy\n")

orig_set_lyrics = AudioFile.set_lyrics
try:
    AudioFile.set_lyrics = lambda self, text: False
    status4, _r, _a, info4 = _process_lyrics_for_audio(flac3, cfg)
finally:
    AudioFile.set_lyrics = orig_set_lyrics
ok(status4 == "fail", f"forced failure status {status4} ({info4})")
ok(os.path.exists(lrc3), "forced failure: sidecar file still present")
with open(lrc3, "r", encoding="utf-8") as fh:
    ok(fh.read() == "[00:01.00]Only copy",
       "forced failure: sidecar content intact")

# ----------------------------------------------------------------------
# AudioFile generic tag editor round-trips
# ----------------------------------------------------------------------
flac4 = os.path.join(tmp, "tags.flac")
make_flac(flac4)
f = MutFLAC(flac4)
f["TITLE"] = "Song"
f["ARTIST"] = "Someone"
f.save()

def tag_value(af, name):
    """Case-insensitive lookup over all_tags() (mutagen lowercases
    vorbis comment keys on save, so the stored case may differ)."""
    for k, v in af.all_tags().items():
        if k.upper() == name.upper():
            return v
    return None


af = AudioFile(flac4)
ok(af.all_tags().get("TITLE") == "Song", "flac all_tags reads TITLE")
ok(af.set_any_tag("MOOD", "happy"), "flac set_any_tag raw key")
af2 = AudioFile(flac4)
ok(tag_value(af2, "MOOD") == "happy",
   f"flac all_tags after set: {af2.all_tags()!r}")
ok(af2.get_tag("TITLE") == "Song",
   "flac: cached reads unaffected / still correct")
ok(af2.set_any_tag("MOOD", "calm"), "flac set_any_tag overwrite")
ok(tag_value(AudioFile(flac4), "MOOD") == "calm", "flac overwrite visible")
ok(af2.delete_any_tag("mood"), "flac delete_any_tag (case-insensitive)")
ok(tag_value(AudioFile(flac4), "MOOD") is None,
   "flac tag gone after delete")

mp3 = os.path.join(tmp, "tags.mp3")
make_mp3(mp3)
am = AudioFile(mp3)
# The raw-key probes use a key the app has no mapping for: MOOD is a
# first-class tag now (TAG_MAP), so writing it as a raw TXXX frame would be
# canonicalized back to "MOOD" on read — which is correct behaviour, but not
# what this test is about.
RAW = "TXXX:MLOTEST"
ok(am.set_any_tag(RAW, "happy"), "mp3 set_any_tag TXXX")
am2 = AudioFile(mp3)
ok(am2.all_tags().get(RAW) == "happy",
   f"mp3 all_tags after TXXX set: {am2.all_tags()!r}")
ok(am2.set_any_tag("TIT2", "New Title"), "mp3 set_any_tag plain frame")
am3 = AudioFile(mp3)
ok(am3.all_tags().get("TITLE") == "New Title",
   f"mp3 TIT2 canonicalized: {am3.all_tags()!r}")
ok(am3.set_any_tag("TZZZ", "custom"), "mp3 set_any_tag unknown frame id")
ok(am3.delete_any_tag(RAW), "mp3 delete_any_tag TXXX")
after_mp3 = AudioFile(mp3).all_tags()
ok(RAW not in after_mp3, "mp3 TXXX gone after delete")
ok(after_mp3.get("TITLE") == "New Title", "mp3 other frames untouched")

# ----------------------------------------------------------------------
# The lyric OFFSET (the player's control): only the sync moves, every
# timestamp moves together, and the write lands where the lyrics already live.
# ----------------------------------------------------------------------
from mlo.lyrics import shift_lyrics_timestamps, shift_stored_lyrics  # noqa: E402

ok(shift_lyrics_timestamps("plain line\n[00:10.00]sung", 500)
   == "plain line\n[00:10.50]sung",
   "offset: an untimed line is untouched, the timed one moves")
ok(shift_lyrics_timestamps("[00:00.20]x", -5000) == "[00:00.00]x",
   "offset: a stamp clamps at zero instead of going negative")
ok(shift_lyrics_timestamps("[01:59.999]x", 1) == "[02:00.000]x",
   "offset: its own precision is kept and the carry is right")
ok(shift_lyrics_timestamps("[00:10.05]<00:10.50>a <00:11.00>b", 500)
   == "[00:10.55]<00:11.00>a <00:11.50>b",
   "offset: Enhanced word stamps move with their own line")
ok(shift_lyrics_timestamps("[ti:Song]\n[offset:+200]\n[00:05]x", 1000)
   == "[ti:Song]\n[offset:+200]\n[00:06]x",
   "offset: LRC metadata headers are not timestamps")
ok(shift_lyrics_timestamps("anything", 0) == "anything",
   "offset: 0 is the identity, byte for byte")

flac_off = os.path.join(tmp, "offset.flac")
make_flac(flac_off)
af_off = AudioFile(flac_off)
af_off.set_lyrics("[00:02.00]one\nplain line\n[00:07.50]two")
off_text, off_targets = shift_stored_lyrics(flac_off, 250)
ok(off_targets == ["embedded"], f"offset: the tag it found is the tag it wrote {off_targets}")
ok(AudioFile(flac_off).get_lyrics() == "[00:02.25]one\nplain line\n[00:07.75]two",
   "offset: written to the tag, untimed line intact")

# A sidecar beside the track is a place too: the shift follows the text, so a
# track whose lyrics live in both gets both moved (and none is invented).
sidecar_off = os.path.splitext(flac_off)[0] + ".lrc"
with open(sidecar_off, "w", encoding="utf-8") as f:
    f.write("[00:03.00]sidecar\n")
off_text2, off_targets2 = shift_stored_lyrics(flac_off, -500)
ok(sorted(off_targets2) == ["embedded", "lrc"],
   f"offset: both stores shift when both hold lyrics {off_targets2}")
with open(sidecar_off, encoding="utf-8") as f:
    ok(f.read().startswith("[00:02.50]sidecar"), "offset: the sidecar moved with it")
ok(AudioFile(flac_off).get_lyrics().startswith("[00:01.75]one"),
   "offset: the tag moved again, from its own current value")

flac_bare = os.path.join(tmp, "bare.flac")
make_flac(flac_bare)
ok(shift_stored_lyrics(flac_bare, 100) == (None, []),
   "offset: a track with no lyrics is left alone, not given any")

print(f"ALL {passed} MERGE TESTS PASSED")