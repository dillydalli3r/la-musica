#!/usr/bin/env python3
"""Export AUDIO processing: the ReplayGain modes, the equalizer profiles, zip.

The Export page can now change the audio itself, not just its tags or its
container, so this suite pins the parts of that which a tag cannot show:

  * the Equalizer APO / Peace parser, fed the profiles a user actually has
    (``tools/fixtures/eq``: a real Peace ``FilterCurve:`` export, a parametric
    APO text profile, a 31-band graphic profile, a straight-line curve saved
    with a BOM and CRLF, and a file with nothing to apply), including the lines
    it REFUSES to guess at — a profile that silently loses one of its bands is
    a different curve, so a band line that cannot be read refuses the whole
    import and names itself — plus APO's own aliases and spellings, each read
    as the filter its reference says it is, the ``If:``/``ElseIf:`` blocks whose
    bands are reported and left out instead of applied unconditionally, and the
    AP/IIR filters reported rather than approximated;
  * the rendered ``-af`` chain, asserted literally and then actually run
    through ffmpeg, since a chain that only looks right is worth nothing;
  * ``replaygain_mode=apply``: an album's quiet and loud track must come out
    with the album's own balance (level difference) intact while the album as a
    whole lands on the ReplayGain reference, and neither file may carry
    REPLAYGAIN_* tags (the gain is in the samples now);
  * ``eq_profile``: a bass shelf must raise the low band measurably and leave
    the mid band where it was;
  * the copy codec refusing a filtering run with the one message the UI shows;
  * the EQ endpoints end to end (list/import/delete, the encoding a hand-copied
    file is read in, the traversal refusals, the size cap);
  * the SAVED EXPORT CONFIGS end to end: the form under a name, one JSON file
    per name, the round trip that proves a saved config still names the profile
    it was saved with (the run reports the id and stamps the file it wrote), and
    the honest degradation when that profile is gone;
  * the zip target end to end.

Every number below is measured with the same ffmpeg EBU R128 meter the export
itself uses (mlo.loudness), so "it changed the audio" is a measurement.

Run:  python tools/test_export_audio.py
"""
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

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

from mlo import eq as eq_mod                                   # noqa: E402
from mlo.audio import AudioFile                                 # noqa: E402
from mlo.loudness import RG2_REFERENCE_LUFS, analyze_file, parse_ebur128  # noqa: E402
from server import exporter                                     # noqa: E402
from server import exportconfigs                                # noqa: E402
from server import main as mlo_main                             # noqa: E402

# --------------------------------------------------------------------------- #
# The two profile texts a user actually has
# --------------------------------------------------------------------------- #

# A parametric Equalizer APO profile — the shape AutoEQ publishes for a
# headphone correction: a preamp and one Filter line per band. Field order and
# the Hz/dB suffixes are exactly as those files write them.
APO_PROFILE = """\
Preamp: -6.4 dB
Filter 1: ON PK Fc 21 Hz Gain 6.2 dB Q 0.70
Filter 2: ON PK Fc 105 Hz Gain 5.5 dB Q 0.70
Filter 3: ON PK Fc 1200 Hz Gain -2.1 dB Q 1.41
Filter 4: ON PK Fc 3200 Hz Gain 3.4 dB Q 2.00
Filter 5: ON HS Fc 10000 Hz Gain -3.0 dB Q 0.70
"""

# A Peace export: Peace writes its own banner, the device and channel it was
# saved for, an Include, and a disabled band around the filter list. Only the
# filter lines can be rendered — the rest is reported, never dropped.
PEACE_EXPORT = """\
Filter Settings file

Room EQ V5.1
EqualizerAPO Configuration File
Device: Speakers (Realtek(R) Audio)
Channel: all
Preamp: -5.6 dB
Filter 1: ON PK Fc 30 Hz Gain 6.0 dB Q 0.70
Filter 2: ON PK Fc 200 Hz Gain -2.5 dB Q 1.00
Filter 3: OFF PK Fc 1000 Hz Gain 4.0 dB Q 1.00
Include: MyHeadphone.txt
Filter 4: ON LSC Fc 80 Hz Gain 3.0 dB Q 0.70
Filter 5: ON LP Fc 18000 Hz Q 0.707
Gain: 0
"""

apo = eq_mod.parse_apo(APO_PROFILE, "apo")
assert apo["preamp_db"] == -6.4, apo
assert apo["unsupported"] == [] and apo["notes"] == [], apo
assert apo["filters"] == [
    {"type": "PK", "fc": 21.0, "gain": 6.2, "q": 0.7, "on": True},
    {"type": "PK", "fc": 105.0, "gain": 5.5, "q": 0.7, "on": True},
    {"type": "PK", "fc": 1200.0, "gain": -2.1, "q": 1.41, "on": True},
    {"type": "PK", "fc": 3200.0, "gain": 3.4, "q": 2.0, "on": True},
    {"type": "HS", "fc": 10000.0, "gain": -3.0, "q": 0.7, "on": True},
], apo["filters"]

# The rendered chain, literally: the preamp first (it is what keeps the boosts
# from clipping), then each band in file order — peaking bands as `equalizer`,
# the high shelf as `treble`.
assert eq_mod.to_af(apo) == (
    "volume=-6.4dB,"
    "equalizer=f=21:t=q:w=0.7:g=6.2,"
    "equalizer=f=105:t=q:w=0.7:g=5.5,"
    "equalizer=f=1200:t=q:w=1.41:g=-2.1,"
    "equalizer=f=3200:t=q:w=2:g=3.4,"
    "treble=g=-3:f=10000:t=q:w=0.7"
), eq_mod.to_af(apo)

peace = eq_mod.parse_apo(PEACE_EXPORT, "peace")
assert peace["preamp_db"] == -5.6, peace
assert [(f["type"], f["fc"], f["gain"], f["q"], f["on"]) for f in peace["filters"]] == [
    ("PK", 30.0, 6.0, 0.7, True),
    ("PK", 200.0, -2.5, 1.0, True),
    ("PK", 1000.0, 4.0, 1.0, False),      # OFF stays in the list...
    ("LSC", 80.0, 3.0, 0.7, True),        # ...and the file's own type name does
    ("LP", 18000.0, 0.0, 0.707, True),
], peace["filters"]
# ...but nothing unrenderable does, and every such line is named.
assert peace["unsupported"] == [
    "Filter Settings file", "Room EQ V5.1", "EqualizerAPO Configuration File",
    "Device: Speakers (Realtek(R) Audio)", "Include: MyHeadphone.txt", "Gain: 0",
], peace["unsupported"]
assert any("6 line(s)" in n for n in peace["notes"]), peace["notes"]
assert eq_mod.to_af(peace) == (
    "volume=-5.6dB,"
    "equalizer=f=30:t=q:w=0.7:g=6,"
    "equalizer=f=200:t=q:w=1:g=-2.5,"
    "bass=g=3:f=80:t=q:w=0.7,"
    "lowpass=f=18000:t=q:w=0.707"
), eq_mod.to_af(peace)
# The OFF band and the unrenderable lines are absent from the chain, so an
# imported profile can never apply something the file did not ask for.
chain = eq_mod.to_af(peace)
for absent in ("f=1000", "Include", "Realtek", "Gain: 0"):
    assert absent not in chain, (absent, chain)

# A malformed BAND line is an ERROR naming its line and its problem — an
# unknown filter type, a missing frequency, a non-numeric value, a `GraphicEQ`
# group that is not one pair, an unreadable preamp. The readable half is never
# imported as a shorter curve: `import_profile` refuses the whole file.
broken = eq_mod.parse_apo(
    "Filter 1: ON PK Fc 1000 Gain 3 Q 1\n"
    "Filter 2: ON XX Fc 200 Gain 3\n"
    "Filter 3: ON PK\n"
    "Filter 4: ON PK Fc abc Gain 1\n"
    "GraphicEQ: 25 0; 40 x\n"
    "Preamp: loud dB\n")
assert len(broken["filters"]) == 1 and broken["filters"][0]["fc"] == 1000.0, broken
assert [e.split(" — ")[-1] for e in broken["errors"]] == [
    "unknown filter type 'XX' (the types are PK, LS, HS, LP, HP, BP, NO, LSC, HSC)",
    "no frequency (Fc)",
    "Fc needs a number, got 'abc'",
    "expected one 'frequency gain' pair, got '40 x'",
    "Preamp needs a number, got 'loud dB'",
], broken["errors"]
assert [e.split(":")[0] for e in broken["errors"]] == [
    "line 2", "line 3", "line 4", "line 5", "line 6"], broken["errors"]

# --- APO's own type spellings, each read as the filter it NAMES --------------
# APO's configuration reference lists these aliases in the row of the filter
# they are, so the mapping is its own and not a guess: PEQ/Modal are its
# peaking row, LPQ/HPQ its pass filters with a Q, "LS 6dB"/"LS 12dB" and
# "HSC 6 dB" shelves with their slope in dB per octave, and "BW Oct 0.5" a
# bandwidth. A low-pass stays a low-pass.
aliased = eq_mod.parse_apo(
    "Filter 1: ON PEQ Fc 100 Hz Gain 1.0 dB BW Oct 0.5\n"
    "Filter 2: ON Modal Fc 200 Hz Gain 3.0 dB Q 5.41 T60 target 100 ms\n"
    "Filter 3: ON LPQ Fc 10000 Hz Q 0.400\n"
    "Filter 4: ON HPQ Fc 20 Hz Q 0.5\n"
    "Filter 5: ON LS 6dB Fc 50 Hz Gain 7.2 dB\n"
    "Filter 6: ON LS 12dB Fc 2000 Hz Gain -5.0 dB\n"
    "Filter 7: ON HS 6dB Fc 12000 Hz Gain 10.0 dB\n"
    "Filter 8: ON HS 12dB Fc 500 Hz Gain 5.0 dB\n"
    "Filter 9: ON LSC 10.8 dB Fc 300 Hz Gain 5.0 dB\n"
    "Filter 10: ON HSC 6 dB Fc 100 Hz Gain -6.0 dB\n")
assert aliased["errors"] == [] and aliased["unsupported"] == [], aliased
assert [(f["type"], f["fc"], f["gain"]) for f in aliased["filters"]] == [
    ("PK", 100.0, 1.0),      # PEQ is APO's "Parametric EQ" row...
    ("PK", 200.0, 3.0),      # ...and Modal is the same row
    ("LP", 10000.0, 0.0),    # LPQ is the LP row with a Q
    ("HP", 20.0, 0.0),
    ("LS", 50.0, 7.2),       # "LS 6dB"/"LS 12dB": the shelf, slope and all
    ("LS", 2000.0, -5.0),
    ("HS", 12000.0, 10.0),
    ("HS", 500.0, 5.0),
    ("LSC", 300.0, 5.0),     # "LSC 10.8 dB": APO's custom-slope shelf
    ("HSC", 100.0, -6.0),
], aliased["filters"]
# "BW Oct 0.5" is the same bandwidth as "BW 0.5": APO's own relation, and the
# unit word between the key and its value carries nothing else.
bw_oct = 0.5
bw_plain = eq_mod.parse_apo("Filter 1: ON PK Fc 100 Hz Gain 1.0 dB BW 0.5")
assert aliased["filters"][0]["bw"] == 0.5, aliased["filters"][0]
assert abs(aliased["filters"][0]["q"]
           - math.sqrt(2 ** bw_oct) / (2 ** bw_oct - 1)) < 1e-9, aliased["filters"][0]
assert aliased["filters"][0]["q"] == bw_plain["filters"][0]["q"], aliased["filters"][0]
# What a shelf cannot carry (its own slope) and what Modal cannot (its T60
# decay) is stated rather than implied, and the file's own Q is kept.
alias_notes = " ".join(aliased["notes"])
assert "Modal" in alias_notes and "T60" in alias_notes, aliased["notes"]
assert "LS 6dB" in alias_notes and "slope" in alias_notes, aliased["notes"]
assert aliased["filters"][1]["q"] == 5.41 and aliased["filters"][1]["t60"] == 100.0, \
    aliased["filters"][1]
# The chain renders each alias as the filter it NAMES — never as a lookalike.
alias_chain = eq_mod.to_af(aliased)
for rendered in ("lowpass=f=10000:t=q:w=0.4", "highpass=f=20:t=q:w=0.5",
                 "bass=g=7.2:f=50:t=q:w=0.7", "treble=g=10:f=12000:t=q:w=0.7"):
    assert rendered in alias_chain, (rendered, alias_chain)

# --- AP and IIR: understood, reported, never rendered ------------------------
# An all-pass is phase-only (its magnitude is flat, so leaving it out leaves the
# file's own curve intact) and an IIR filter IS the file's own coefficients.
# Neither has a filter on either path, so both are reported by name instead of
# guessed at; an OFF one has nothing left to report.
unrenderable = eq_mod.parse_apo(
    "Filter 1: ON AP Fc 900 Hz Q 0.707\n"
    "Filter 2: ON IIR Order 2 Coefficients 0.0380602 0.0761205 0.0380602 "
    "1.2706 -1.84776 0.729402\n"
    "Filter 3: OFF AP Fc 400 Hz Q 1\n"
    "Filter 4: ON PK Fc 1000 Hz Gain 2 dB Q 1\n")
assert unrenderable["errors"] == [], unrenderable["errors"]
assert [f["fc"] for f in unrenderable["filters"]] == [1000.0], unrenderable["filters"]
assert len(unrenderable["unsupported"]) == 2, unrenderable["unsupported"]
assert "all-pass" in unrenderable["unsupported"][0], unrenderable["unsupported"]
assert "coefficients" in unrenderable["unsupported"][1], unrenderable["unsupported"]
assert eq_mod.to_af(unrenderable) == "equalizer=f=1000:t=q:w=1:g=2", \
    eq_mod.to_af(unrenderable)

# --- If:/ElseIf: — a condition this app cannot evaluate is NOT applied -------
# APO evaluates the block against its own variables (sample rate, channel
# count, device name, user variables). Applying the bands inside it
# unconditionally would change the sound of every config that uses one, so they
# are left out, the block is named, and the count of skipped lines is in the
# notes.
conditional = eq_mod.parse_apo(
    "Preamp: -2 dB\n"
    "Filter 1: ON PK Fc 100 Hz Gain 3 dB Q 1\n"
    "If: inputChannelCount == 2\n"
    "Filter 2: ON PK Fc 200 Hz Gain 6 dB Q 1\n"
    "ElseIf: sampleRate > 44100\n"
    "Filter 3: ON PK Fc 300 Hz Gain -6 dB Q 1\n"
    "Else:\n"
    "Filter 4: ON PK Fc 400 Hz Gain 6 dB Q 1\n"
    "EndIf:\n"
    "Filter 5: ON PK Fc 500 Hz Gain 2 dB Q 1\n")
assert conditional["errors"] == [], conditional["errors"]
assert [f["fc"] for f in conditional["filters"]] == [100.0, 500.0], conditional["filters"]
assert any(u.startswith("line 3:") and "conditional block" in u
           for u in conditional["unsupported"]), conditional["unsupported"]
assert any("3 filter line(s)" in n for n in conditional["notes"]), conditional["notes"]
assert "f=200" not in eq_mod.to_af(conditional), eq_mod.to_af(conditional)

# A nested block is reported once per If:, and each EndIf: ends exactly one.
nested = eq_mod.parse_apo(
    "If: sampleRate == 48000\n"
    "If: outputChannelCount == 2\n"
    "Filter 1: ON PK Fc 700 Hz Gain 3 dB Q 1\n"
    "EndIf:\n"
    "EndIf:\n")
assert nested["filters"] == [] and nested["empty"] is True, nested
assert sum("conditional block" in u for u in nested["unsupported"]) == 2, \
    nested["unsupported"]

# --- one profile, one curve: the renderers' bounds are the SAME --------------
# The player clamps a band's Fc/gain/Q and the profile's preamp — the WebAudio
# node and the editor's own boxes are bounded — and the ffmpeg chain clamps to
# exactly these numbers (web/src/lib/eqNodes.ts: EQ_FC_MIN, EQ_FC_MAX,
# EQ_GAIN_LIMIT, EQ_PREAMP_LIMIT and its 0.1…30 Q). A file that asks for more
# therefore sounds the same in the app and in an export, its own values are
# kept in the profile, and the difference is stated.
hot = eq_mod.parse_apo(
    "Preamp: -40 dB\n"
    "Filter 1: ON PK Fc 5 Hz Gain 30 dB Q 500\n"
    "Filter 2: ON PK Fc 40000 Hz Gain 12 dB Q 0.01\n")
assert (eq_mod.FC_MIN_HZ, eq_mod.FC_MAX_HZ, eq_mod.GAIN_LIMIT_DB,
        eq_mod.Q_MIN, eq_mod.Q_MAX, eq_mod.PREAMP_LIMIT_DB) == \
    (20.0, 20000.0, 20.0, 0.1, 30.0, 24.0)
assert [(f["fc"], f["gain"], f["q"]) for f in hot["filters"]] == [
    (5.0, 30.0, 500.0), (40000.0, 12.0, 0.01)], hot["filters"]
assert eq_mod.to_af(hot) == ("volume=-24dB,"
                             "equalizer=f=20:t=q:w=30:g=20,"
                             "equalizer=f=20000:t=q:w=0.1:g=12"), eq_mod.to_af(hot)
assert any("clamped" in n for n in hot["notes"]), hot["notes"]

# A GraphicEQ band list (what AutoEQ publishes) becomes peaking filters with
# Q 1.41 — AutoEQ's own conversion — and says so in the notes.
graphic = eq_mod.parse_apo("GraphicEQ: 20 -3.0; 50 -2.0; 100 0.0; 5000 -1.5")
assert [f["fc"] for f in graphic["filters"]] == [20.0, 50.0, 100.0, 5000.0], graphic
assert {f["q"] for f in graphic["filters"]} == {1.41}, graphic
assert graphic["notes"] and "Q 1.41" in graphic["notes"][0], graphic["notes"]
assert eq_mod.to_af(graphic) == (
    "equalizer=f=20:t=q:w=1.41:g=-3,"
    "equalizer=f=50:t=q:w=1.41:g=-2,"
    "equalizer=f=5000:t=q:w=1.41:g=-1.5"
), eq_mod.to_af(graphic)

# Every built-in preset has to be a chain ffmpeg accepts, or the Export page is
# offering a curve that cannot be applied.
PRESET_IDS = {p["id"] for p in eq_mod.PRESETS}
assert PRESET_IDS == {"flat", "bass_shelf", "presence", "night"}, PRESET_IDS
assert eq_mod.to_af(eq_mod.find(None, "flat")) == ""
assert eq_mod.to_af(eq_mod.find(None, "bass_shelf")) == "volume=-4dB,bass=g=6:f=105:t=q:w=0.7", \
    eq_mod.to_af(eq_mod.find(None, "bass_shelf"))
for row in eq_mod.preset_rows():
    assert row["label"], row
    cmd = [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
           "sine=frequency=440:duration=0.3"]
    rendered = eq_mod.to_af(row)
    if rendered:
        cmd += ["-af", rendered]
    proc = subprocess.run(cmd + ["-f", "null", "-"], capture_output=True, text=True)
    assert proc.returncode == 0, (row["id"], rendered, proc.stderr[-300:])

# --------------------------------------------------------------------------- #
# The FILES a user actually has, byte for byte (tools/fixtures/eq): a real
# Peace `FilterCurve:` export, a parametric APO text profile, a Peace graphic
# profile, a straight-line curve saved with a BOM and CRLF, and a file whose
# every line is one this app has no equivalent for.
# --------------------------------------------------------------------------- #

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "eq")


def fixture_bytes(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


def fixture_profile(name):
    """The fixture as the app reads a profile file: bytes decoded per encoding."""
    return eq_mod.parse_apo(eq_mod.decode_profile(fixture_bytes(name)), name)


# --- Peace's FilterCurve: ONE line, fN/vN paired by index ---------------------
curve_raw = fixture_bytes("earbuds_filtercurve.txt")
assert b"FilterCurve:" in curve_raw and not curve_raw.startswith(b"\xef\xbb\xbf")
assert not curve_raw.endswith(b"\n"), "the real file has no trailing newline"
curve = fixture_profile("earbuds_filtercurve.txt")
assert curve["errors"] == [] and curve["unsupported"] == [], curve
# Every point became a band — 50 of them, and nothing was assumed about the
# ladder: the frequencies are the file's own, in order.
assert len(curve["filters"]) == 50, len(curve["filters"])
freqs = [f["fc"] for f in curve["filters"]]
assert freqs == sorted(freqs) and freqs[0] == 10.0 and freqs[-1] == 18903.4, freqs
assert curve["filters"][0]["gain"] == 0.03, curve["filters"][0]
assert all(f["type"] == "PK" and f["on"] for f in curve["filters"]), curve["filters"]
# The band widths follow the ladder's own spacing: a 50-point curve is far
# denser than a 10-band octave list, so Q 1.41 would smear every point into its
# neighbours. A 1/3-octave ladder comes out at the textbook Q 4.32.
assert 5.0 < curve["filters"][25]["q"] < 8.0, curve["filters"][25]
# What the mapping does NOT reproduce is stated, not implied.
curve_notes = " ".join(curve["notes"])
assert "B-spline" in curve_notes, curve["notes"]
assert "8191" in curve_notes and "convolution" in curve_notes, curve["notes"]
assert "50 point(s)" in curve_notes, curve["notes"]
assert eq_mod.to_af(curve).count("equalizer=") == 50, eq_mod.to_af(curve)

# A 31-point curve with STRAIGHT lines, saved the way Notepad saves it: UTF-8
# with a BOM and CRLF. (The count is the file's: nothing may assume 50.)
#
# The fixture on disk is LF, and must stay that way: a committed TEXT file
# cannot promise its line endings, because git normalises them on checkout — a
# CRLF fixture is LF on a Linux runner — so asserting CRLF on the fixture would
# be testing git's checkout policy rather than this parser. The CRLF bytes are
# therefore BUILT here, written to a scratch path and read back the way a real
# file is, which holds on any checkout.
linear_lf = fixture_bytes("filtercurve_linear_31.txt")
assert linear_lf.startswith(b"\xef\xbb\xbf") and b"\r\n" not in linear_lf, linear_lf[:20]
scratch_curve = os.path.join(tempfile.mkdtemp(prefix="mlo_eq_curve_"), "linear_crlf.txt")
with open(scratch_curve, "wb") as f:
    f.write(linear_lf.replace(b"\n", b"\r\n"))
with open(scratch_curve, "rb") as f:
    linear_raw = f.read()
assert linear_raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in linear_raw, linear_raw[:20]
linear = eq_mod.parse_apo(eq_mod.decode_profile(linear_raw), "linear")
assert linear["errors"] == [] and len(linear["filters"]) == 31, linear
assert linear["filters"][0]["fc"] == 20.0 and linear["filters"][-1]["fc"] == 20000.0, linear
# The band width comes from the FILE's own spacing: for this 31-point ladder
# that is ~1/3 octave, which lands on the textbook Q 4.32 (and not on the
# octave-wide 1.41 a 10-band list gets).
mid = linear["filters"][15]
lo, hi = linear["filters"][14]["fc"], linear["filters"][16]["fc"]
bw = math.log2(hi / lo) / 2
assert abs(mid["q"] - math.sqrt(2 ** bw) / (2 ** bw - 1)) < 1e-9, mid
assert 4.2 < mid["q"] < 4.4, mid
linear_notes = " ".join(linear["notes"])
assert "InterpolateLin=1" in linear_notes and "straight lines" in linear_notes, linear["notes"]
assert "4096" in linear_notes, linear["notes"]

# A malformed curve fails NAMING the attribute — never as a shorter curve.
for body, needle in (
    ('FilterCurve: f0="20" v1="1"', "v1 has no f1"),
    ('FilterCurve: f0="20" f1="30" v0="1"', "f1 has no v1"),
    ('FilterCurve: f0="20" v0="loud"', "is not a number"),
    ('FilterCurve: f0="20" v0="1" nonsense', 'expected name="value"'),
    ('FilterCurve: FilterLength="8191"', "no points"),
):
    bad = eq_mod.parse_apo(body, "bad")
    assert not bad["filters"], (body, bad)
    assert len(bad["errors"]) == 1 and needle in bad["errors"][0], (body, bad["errors"])
    assert bad["errors"][0].startswith("line 1: FilterCurve —"), bad["errors"]

# --- the APO text shape, as Peace writes it ----------------------------------
parametric = fixture_profile("peace_parametric.txt")
assert parametric["errors"] == [], parametric["errors"]
assert parametric["preamp_db"] == -6.5, parametric["preamp_db"]
# Eight bands, in the file's order: the OFF slot and the `ON None` slot are not
# bands, and a disabled band stays in the list marked off.
assert [(f["type"], f["on"]) for f in parametric["filters"]] == [
    ("PK", True), ("PK", False), ("LSC", True), ("HS", True), ("LP", True),
    ("PK", True), ("NO", True), ("HP", False)], parametric["filters"]
# BW 1.2 → Q ≈ 1.17 by APO's own relation, not the type's default 1.41.
assert 1.1 < parametric["filters"][5]["q"] < 1.25, parametric["filters"][5]
param_notes = " ".join(parametric["notes"])
assert "LSC/HSC" in param_notes and "BW" in param_notes, parametric["notes"]
assert parametric["unsupported"] == [
    "Filter Settings file", "Room EQ V5.1", "EqualizerAPO Configuration File",
    "Device: Speakers (Realtek(R) Audio)", "Include: MyHeadphone.txt", "Gain: 0",
], parametric["unsupported"]
assert "MyHeadphone" not in eq_mod.to_af(parametric), eq_mod.to_af(parametric)

# --- a Peace graphic profile: 31 bands on its own ladder ---------------------
graphic31 = fixture_profile("peace_graphic_31.txt")
assert graphic31["errors"] == [] and len(graphic31["filters"]) == 31, graphic31
assert [f["fc"] for f in graphic31["filters"]][:3] == [20.0, 25.0, 31.5], graphic31
assert {f["q"] for f in graphic31["filters"]} == {1.41}, graphic31
# (The one band the file puts at 0 dB is carried but not rendered: a peaking
# filter at 0 dB is transparent, and rendering it would cost samples for
# nothing. That is why the chain is one shorter than the band list.)
assert eq_mod.to_af(graphic31).count("equalizer=") == 30, eq_mod.to_af(graphic31)

# --- a file with nothing this app can apply: EMPTY, and said so --------------
empty = fixture_profile("unknown_only.txt")
assert empty["filters"] == [] and empty["errors"] == [], empty
assert empty["empty"] is True, empty
assert empty["unsupported"] == [
    "Filter Settings file", "Device: Speakers (Realtek(R) Audio)",
    "Include: MyHeadphone.txt"], empty["unsupported"]
assert "holds no filters" in " ".join(empty["notes"]), empty["notes"]
assert eq_mod.to_af(empty) == "", eq_mod.to_af(empty)

# --- the same file read as UTF-16 (a Windows tool's other save) --------------
utf16 = fixture_bytes("peace_parametric.txt").decode("utf-8").encode("utf-16")
utf16_parsed = eq_mod.parse_apo(eq_mod.decode_profile(utf16), "utf16")
assert utf16_parsed["preamp_db"] == -6.5 and utf16_parsed["errors"] == [], utf16_parsed
assert utf16_parsed["filters"] == parametric["filters"], utf16_parsed["filters"]

# And the chains above really are chains ffmpeg accepts (one 50-band curve —
# the deepest of them — is enough to prove the shape; the presets above cover
# the rest).
deep = eq_mod.to_af(curve)
proc = subprocess.run(
    [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
     "-af", deep, "-f", "null", "-"], capture_output=True, text=True)
assert proc.returncode == 0, (deep[:200], proc.stderr[-400:])

# --------------------------------------------------------------------------- #
# A small library: one album, one quiet and one loud track, both pink noise so
# the loudness and the per-band measurements below mean something.
# --------------------------------------------------------------------------- #

ROOT = tempfile.mkdtemp(prefix="mlo_export_audio_")
MUSIC = os.path.join(ROOT, "Library")
ALBUM = os.path.join(MUSIC, "Artist One", "Album A")
os.makedirs(ALBUM, exist_ok=True)


def noise(path, level_db, seconds=4.0):
    """A pink-noise FLAC at *level_db* below the generated noise's own level."""
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
         f"anoisesrc=color=pink:amplitude=1.0:duration={seconds}:sample_rate=44100",
         "-af", f"volume={level_db}dB", "-c:a", "flac", path],
        check=True, capture_output=True)
    af = AudioFile(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    af.defer_save(True)
    for key, value in (("TITLE", stem.split()[-1]),
                       ("ARTIST", "Artist One"), ("ALBUMARTIST", "Artist One"),
                       ("ALBUM", "Album A"), ("TRACKNUMBER", stem[:2]),
                       # A library file that has been through script 7 carries
                       # ReplayGain tags; the source has them here so that
                       # "apply strips them" is a real assertion about the
                       # export and not about a file that never had any.
                       ("REPLAYGAIN_TRACK_GAIN", "-3.00 dB"),
                       ("REPLAYGAIN_TRACK_PEAK", "0.500000"),
                       ("REPLAYGAIN_ALBUM_GAIN", "-3.00 dB"),
                       ("REPLAYGAIN_ALBUM_PEAK", "0.500000")):
        af.set_tag(key, value)
    af.defer_save(False)
    return path


def lufs(path):
    """Integrated loudness of *path* — measured again, never read from a tag."""
    return analyze_file(path, force=True)["lufs"]


def band_lufs(path, low_hz, high_hz):
    """Integrated loudness of ONE band of *path*.

    A 2-pole high/low-pass pair around the band, then the same ebur128 meter
    the export measures with: that is what turns "the bass rose and the mids
    did not" into two numbers instead of a listening impression."""
    proc = subprocess.run(
        [FFMPEG, "-v", "info", "-nostats", "-nostdin", "-i", path,
         "-map", "0:a:0",
         "-af", f"highpass=f={low_hz}:t=q:w=0.707,lowpass=f={high_hz}:t=q:w=0.707,"
                "ebur128=peak=sample",
         "-f", "null", "-"], capture_output=True, text=True)
    measured = parse_ebur128(proc.stderr or "")
    assert measured, (path, proc.stderr[-300:])
    return measured["lufs"]


def energy_lufs(values):
    """The energy average of several loudness values — the album loudness an
    album gain is derived from (same formula as exporter._album_gain)."""
    import math
    energy = sum(10.0 ** ((v + 0.691) / 10.0) for v in values) / len(values)
    return -0.691 + 10.0 * math.log10(energy)


try:
    # Both tracks sit below the ReplayGain reference but 14 dB apart, so the
    # album gain has real work to do and its result cannot be confused with
    # track gain (which would put both on -18 LUFS).
    quiet = noise(os.path.join(ALBUM, "01 Quiet.flac"), -24.0)
    loud = noise(os.path.join(ALBUM, "02 Loud.flac"), -10.0)
    CFG = {"music_folder": MUSIC, "embed_cover_jpeg_quality": 85,
           "embed_cover_resolution": 400, "jpeg_progressive": True}
    # The login gate reads the config through server.auth's own import (not the
    # `load_config` alias patched above), so it is switched off HERE rather than
    # left to whatever auth state the machine happens to have.
    mlo_main.auth_mod.requires_login = lambda request, state: False
    BASE = dict(embed_covers=False, playlists=False, sidecars=False,
                verify=True, workers=1)

    src_quiet, src_loud = lufs(quiet), lufs(loud)
    # The fixture has to BE an uneven album, or the assertions below prove
    # nothing: one track far below the reference, one well above it.
    assert abs(energy_lufs([src_quiet, src_loud]) - RG2_REFERENCE_LUFS) > 3.0, \
        (src_quiet, src_loud)
    assert (src_loud - src_quiet) > 6.0, (src_quiet, src_loud)

    # ---------------------------------------------------- apply (album gain)
    DEST_APPLY = os.path.join(ROOT, "DestApply")
    os.makedirs(DEST_APPLY)
    applied = exporter.export_tracks(CFG, [quiet, loud], DEST_APPLY, codec="flac",
                                     replaygain_mode="apply", **BASE)
    assert applied["failed"] == 0, applied["errors"]
    assert applied["exported"] == 2 and applied["processed"] == 2, applied
    assert applied["replaygain_mode"] == "apply" and applied["eq_profile"] == "", applied
    assert any("stripped" in w for w in applied["warnings"]), applied["warnings"]

    out_dir = os.path.join(DEST_APPLY, "Music", "Artist One", "Album A")
    out_quiet = os.path.join(out_dir, "1-01 Quiet.flac")
    out_loud = os.path.join(out_dir, "1-02 Loud.flac")
    assert os.path.isfile(out_quiet) and os.path.isfile(out_loud), os.listdir(out_dir)
    # FLAC→FLAC is normally a bit-exact copy; a filtering run has to re-encode,
    # which is what makes the gain audible at all.
    assert os.path.getsize(out_quiet) != os.path.getsize(quiet), "apply must rewrite"

    app_quiet, app_loud = lufs(out_quiet), lufs(out_loud)
    print(f"    album gain: sources {src_quiet:.2f} / {src_loud:.2f} LUFS "
          f"-> exports {app_quiet:.2f} / {app_loud:.2f} LUFS")
    # (1) The album as a whole now sits on the ReplayGain reference...
    assert abs(energy_lufs([app_quiet, app_loud]) - RG2_REFERENCE_LUFS) < 0.5, \
        (app_quiet, app_loud)
    # (2) ...while the album's own balance is untouched: the two tracks keep the
    #     level difference they were mastered with.
    assert abs((app_loud - app_quiet) - (src_loud - src_quiet)) < 0.5, \
        (app_loud - app_quiet, src_loud - src_quiet)
    # (3) Track gain would have pulled BOTH onto the reference, collapsing that
    #     difference — so this is the assertion that pins ALBUM gain.
    assert (app_loud - app_quiet) > 6.0, (app_quiet, app_loud)

    # A track whose peak sits far above its own average level (a quiet passage
    # with one loud hit) cannot take its ReplayGain gain without clipping, and
    # the run has to SAY so rather than ship a clipped file quietly. Its own
    # album folder, so the gain applied is the track's own.
    PEAKY = os.path.join(MUSIC, "Artist Two", "Album P")
    os.makedirs(PEAKY, exist_ok=True)
    peaky = os.path.join(PEAKY, "01 Peak.flac")
    subprocess.run(
        [FFMPEG, "-y", "-v", "error",
         "-f", "lavfi", "-i",
         "anoisesrc=color=pink:amplitude=0.03:duration=3:sample_rate=44100",
         "-f", "lavfi", "-i", "sine=frequency=1000:duration=0.1",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1",
         "-c:a", "flac", peaky], check=True, capture_output=True)
    peak_analysis = analyze_file(peaky, force=True)
    DEST_PEAK = os.path.join(ROOT, "DestPeak")
    os.makedirs(DEST_PEAK)
    clipped = exporter.export_tracks(CFG, [peaky], DEST_PEAK, codec="flac",
                                     replaygain_mode="apply", **BASE)
    assert clipped["failed"] == 0 and clipped["processed"] == 1, clipped
    print(f"    clipping: {peak_analysis['lufs']:.2f} LUFS with peak "
          f"{peak_analysis['peak']:.2f} -> gain {peak_analysis['gain_db']:+.2f} dB")
    assert any("clip" in w for w in clipped["warnings"]), clipped["warnings"]

    # The gain is in the samples, so the tags must be gone: a player that
    # honoured them would correct the same audio twice. The SOURCES carry all
    # four, so this is the export dropping them, not a file that never had any.
    for path in (out_quiet, out_loud):
        af = AudioFile(path)
        assert af.get_tag("TITLE") == os.path.basename(path)[5:-5], af.all_tags()
        for key in exporter._RG_TAGS:
            assert af.get_tag(key) is None, (key, path, af.all_tags())

    # A single track exported on its own has no album balance to keep, so it
    # gets its own gain — this is the whole-album rule, from the other side.
    DEST_ONE = os.path.join(ROOT, "DestOne")
    os.makedirs(DEST_ONE)
    solo = exporter.export_tracks(CFG, [loud], DEST_ONE, codec="flac",
                                  replaygain_mode="apply", **BASE)
    assert solo["failed"] == 0 and solo["processed"] == 1, solo
    solo_lufs = lufs(os.path.join(DEST_ONE, "Music", "Artist One", "Album A",
                                  "1-02 Loud.flac"))
    print(f"    track gain: the same track alone lands on {solo_lufs:.2f} LUFS")
    assert abs(solo_lufs - RG2_REFERENCE_LUFS) < 0.7, solo_lufs
    assert abs(solo_lufs - app_loud) > 0.5, (solo_lufs, app_loud)

    # ---------------------------------------------------- tags mode is intact
    DEST_TAGS = os.path.join(ROOT, "DestTags")
    os.makedirs(DEST_TAGS)
    tagged = exporter.export_tracks(CFG, [quiet, loud], DEST_TAGS, codec="flac",
                                    replaygain_mode="tags", **BASE)
    assert tagged["failed"] == 0, tagged["errors"]
    assert tagged["processed"] == 0 and tagged["replaygain_mode"] == "tags", tagged
    tagged_af = AudioFile(os.path.join(DEST_TAGS, "Music", "Artist One", "Album A",
                                       "1-01 Quiet.flac"))
    for key in exporter._RG_TAGS:
        assert tagged_af.get_tag(key), (key, tagged_af.all_tags())
    # Writing tags is not rewriting audio: the export is the source's bytes (the
    # audio part of them) and it is not stamped as processed.
    assert exporter._processing_of(os.path.join(
        DEST_TAGS, "Music", "Artist One", "Album A", "1-01 Quiet.flac")) == ""
    assert not tagged["warnings"], tagged["warnings"]

    # ---------------------------------------------------- equalizer profile
    # An imported shelf with NO preamp, so the measurement below isolates the
    # filter from the level: with a preamp the whole curve moves and "did the
    # bass rise relative to the rest" stops being a single number.
    eq_mod.import_profile(MUSIC, "test_bass",
                          "Filter 1: ON LSC Fc 105 Hz Gain 6.0 dB Q 0.70\n")
    DEST_NOEQ = os.path.join(ROOT, "DestNoEq")
    DEST_EQ = os.path.join(ROOT, "DestEq")
    for folder in (DEST_NOEQ, DEST_EQ):
        os.makedirs(folder)
    plain = exporter.export_tracks(CFG, [loud], DEST_NOEQ, codec="flac",
                                   manifest=True, **BASE)
    assert plain["failed"] == 0 and plain["eq_applied"] == 0, plain
    equalised = exporter.export_tracks(CFG, [loud], DEST_EQ, codec="flac",
                                       eq_profile="test_bass", **BASE)
    assert equalised["failed"] == 0, equalised["errors"]
    assert equalised["eq_applied"] == 1 and equalised["processed"] == 1, equalised
    assert equalised["eq_profile"] == "test_bass", equalised

    ref = os.path.join(DEST_NOEQ, "Music", "Artist One", "Album A", "1-02 Loud.flac")
    eqd = os.path.join(DEST_EQ, "Music", "Artist One", "Album A", "1-02 Loud.flac")
    assert os.path.getsize(ref) == os.path.getsize(loud), "no EQ stays a bit copy"
    low_ref, low_eq = band_lufs(ref, 40, 150), band_lufs(eqd, 40, 150)
    mid_ref, mid_eq = band_lufs(ref, 1000, 3000), band_lufs(eqd, 1000, 3000)
    print(f"    EQ low band 40-150 Hz:   {low_ref:.2f} -> {low_eq:.2f} LUFS "
          f"({low_eq - low_ref:+.2f} dB)")
    print(f"    EQ mid band 1-3 kHz:     {mid_ref:.2f} -> {mid_eq:.2f} LUFS "
          f"({mid_eq - mid_ref:+.2f} dB)")
    # A +6 dB low shelf at 105 Hz has to raise a 40-150 Hz band by several dB,
    # while a 1-3 kHz band is far above its corner and must not move.
    assert (low_eq - low_ref) > 3.0, (low_ref, low_eq)
    assert abs(mid_eq - mid_ref) < 1.0, (mid_ref, mid_eq)

    # The manifest (asked for on the run above) lists exactly what was written,
    # with a digest that matches the bytes — that is what makes it checkable
    # with `sha256sum -c` after a copy to a card.
    manifest_path = os.path.join(DEST_NOEQ, "Music", "checksums.sha256")
    lines = open(manifest_path, encoding="utf-8").read().splitlines()
    assert len(lines) == 1, lines
    digest, rel = lines[0].split("  ", 1)
    assert rel == "Artist One/Album A/1-02 Loud.flac", rel
    with open(ref, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == digest, lines

    # Re-running the SAME curve is a skip, and changing it re-encodes: the file
    # records what was applied to it (a tag), because its duration, its tags and
    # its track identity are identical either way — without that record the
    # second run would report "already exported" and the new curve would never
    # reach the device.
    same = exporter.export_tracks(CFG, [loud], DEST_EQ, codec="flac",
                                  eq_profile="test_bass", **BASE)
    assert (same["exported"], same["skipped"], same["processed"]) == (0, 1, 0), same
    assert exporter._processing_of(eqd) == "eq=test_bass", \
        AudioFile(eqd).all_tags()
    changed = exporter.export_tracks(CFG, [loud], DEST_EQ, codec="flac",
                                     eq_profile="night", **BASE)
    assert (changed["exported"], changed["skipped"]) == (1, 0), changed
    assert exporter._processing_of(eqd) == "eq=night", exporter._processing_of(eqd)
    assert band_lufs(eqd, 40, 150) < low_ref - 3.0, "the new curve must be audible"

    # The stamp has to survive on the formats a DAP actually reads: an MP3
    # stores it as a TXXX frame (a Vorbis-comment name is refused there), so the
    # same run twice is a skip and the curve is not silently lost either way.
    DEST_MP3 = os.path.join(ROOT, "DestMp3")
    os.makedirs(DEST_MP3)
    first_mp3 = exporter.export_tracks(CFG, [loud], DEST_MP3, codec="mp3",
                                       eq_profile="test_bass", embed_covers=False,
                                       playlists=False, sidecars=False, verify=True)
    assert first_mp3["exported"] == 1 and first_mp3["eq_applied"] == 1, first_mp3
    mp3_out = os.path.join(DEST_MP3, "Music", "Artist One", "Album A",
                           "1-02 Loud.mp3")
    assert exporter._processing_of(mp3_out) == "eq=test_bass", \
        AudioFile(mp3_out).all_tags()
    again_mp3 = exporter.export_tracks(CFG, [loud], DEST_MP3, codec="mp3",
                                       eq_profile="test_bass", embed_covers=False,
                                       playlists=False, sidecars=False, verify=True)
    assert (again_mp3["exported"], again_mp3["skipped"]) == (0, 1), again_mp3

    # A profile id that no longer exists fails every track that asked for it
    # rather than exporting without the curve (and says which).
    DEST_MISS = os.path.join(ROOT, "DestMissing")
    os.makedirs(DEST_MISS)
    missing = exporter.export_tracks(CFG, [quiet, loud], DEST_MISS, codec="flac",
                                     eq_profile="gone_profile", **BASE)
    assert missing["failed"] == 2 and missing["exported"] == 0, missing
    assert "gone_profile" in missing["errors"][0], missing["errors"]
    # The export root is created up front, but a run whose every track failed
    # must not have written a single file into it.
    assert not os.listdir(os.path.join(DEST_MISS, "Music")), "nothing may be written"

    # ---------------------------------------------------- copy cannot process
    DEST_COPY = os.path.join(ROOT, "DestCopy")
    os.makedirs(DEST_COPY)
    for opts in ({"replaygain_mode": "apply"}, {"eq_profile": "test_bass"}):
        try:
            exporter.export_tracks(CFG, [loud], DEST_COPY, codec="copy", **opts)
        except ValueError as e:
            assert str(e) == exporter._PROCESSING_NEEDS_CODEC, str(e)
        else:
            raise AssertionError(f"copy + {opts} must be refused")
    assert "real codec" in exporter._PROCESSING_NEEDS_CODEC
    assert not os.path.exists(os.path.join(DEST_COPY, "Music")), \
        "a refused run must not create the folder"

    # ---------------------------------------------------- the HTTP surface
    # The endpoints read the config through this module's own name (the pattern
    # the other HTTP suites use), so a temp library needs no real install.
    mlo_main.load_config = lambda: dict(CFG)
    client = TestClient(mlo_main.app)   # no lifespan: no workers, no slskd boot

    catalog = client.get("/api/export/eq")
    assert catalog.status_code == 200, catalog.text[:300]
    data = catalog.json()
    assert {"presets", "profiles", "note"} <= set(data), sorted(data)
    assert PRESET_IDS <= {p["id"] for p in data["presets"]}, data["presets"]
    assert all({"id", "label", "preamp_db", "filters"} <= set(p) for p in data["presets"])
    assert "Filter" in data["note"], data["note"]
    # The profile this suite imported a moment ago, with its chain, is listed.
    listed = {p["id"]: p for p in data["profiles"]}
    assert set(listed) == {"test_bass"}, listed
    assert listed["test_bass"]["filters"][0]["fc"] == 105.0, listed["test_bass"]
    assert listed["test_bass"]["imported_at"], listed["test_bass"]

    # Import a real Peace export under a name with spaces and punctuation.
    imp = client.post("/api/export/eq/import",
                      json={"name": "Car stereo (bass!)", "text": PEACE_EXPORT})
    assert imp.status_code == 200, imp.text[:300]
    row = imp.json()
    assert row["id"] == "Car_stereo_bass" and row["preamp_db"] == -5.6, row
    assert row["unsupported"] and row["imported_at"], row
    assert os.path.isfile(os.path.join(MUSIC, ".mlo", "data", "eq",
                                       "Car_stereo_bass.txt"))
    # Re-importing the same thing is the same profile, not a second copy.
    again = client.post("/api/export/eq/import",
                        json={"name": "Car stereo (bass!)", "text": PEACE_EXPORT})
    assert again.json()["id"] == row["id"], again.json()
    assert again.json()["imported_at"] == row["imported_at"], again.json()
    assert {p["id"] for p in client.get("/api/export/eq").json()["profiles"]} == \
        {"test_bass", "Car_stereo_bass"}

    # A name that could name a file, and a body past the cap, are refused.
    bad_name = client.post("/api/export/eq/import",
                           json={"name": "../../evil", "text": "Preamp: -1 dB"})
    assert bad_name.status_code == 400, bad_name.text[:200]
    assert "invalid profile name" in bad_name.json()["detail"], bad_name.json()
    oversize = client.post("/api/export/eq/import",
                           json={"name": "huge", "text": "x" * (64 * 1024 + 1)})
    assert oversize.status_code == 400, oversize.text[:200]
    assert "KiB" in oversize.json()["detail"], oversize.json()
    assert not os.path.exists(os.path.join(MUSIC, ".mlo", "data", "eq", "huge.txt"))
    preset_name = client.post("/api/export/eq/import",
                              json={"name": "night", "text": "Preamp: -1 dB"})
    assert preset_name.status_code == 400, preset_name.text[:200]

    # An id is never a path, and the endpoint hands the id straight to the
    # module: the proof is that no way of spelling a traversal can reach a file
    # beside the profile folder — tested both over HTTP and in the module.
    canary = os.path.join(MUSIC, ".mlo", "data", "canary.txt")
    with open(canary, "w", encoding="utf-8") as f:
        f.write("must survive\n")
    client.delete("/api/export/eq/..%2F..%2Fcanary")
    client.delete("/api/export/eq/../canary")
    for bad in ("../../canary", "..\\..\\canary", "..", "", "/etc/passwd"):
        try:
            eq_mod.delete_profile(MUSIC, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"deleting {bad!r} must be refused")
    assert os.path.isfile(canary), "a traversal deleted a file outside the eq folder"
    assert eq_mod.find(MUSIC, "../../canary") is None
    gone = client.delete("/api/export/eq/Car_stereo_bass")
    assert gone.status_code == 200 and gone.json()["ok"] is True, gone.text[:200]
    assert not os.path.exists(os.path.join(MUSIC, ".mlo", "data", "eq",
                                           "Car_stereo_bass.txt"))
    assert client.delete("/api/export/eq/Car_stereo_bass").status_code == 404

    # The copy refusal is the SAME message over HTTP (it is what the UI shows).
    refused = client.post("/api/export", json={
        "paths": [loud], "dest": DEST_COPY, "codec": "copy",
        "replaygain_mode": "apply"})
    assert refused.status_code == 400, refused.text[:300]
    assert refused.json()["detail"] == exporter._PROCESSING_NEEDS_CODEC, refused.json()

    # ---------------------------------------------------- the files a user has
    # Every shape in tools/fixtures/eq imports, and each lands in the one list
    # the export menu's equalizer control renders.
    imported = {}
    for name, fixture_name in (("Peace parametric", "peace_parametric.txt"),
                               ("Peace graphic", "peace_graphic_31.txt"),
                               ("Earbuds curve", "earbuds_filtercurve.txt"),
                               ("Nothing here", "unknown_only.txt")):
        with open(os.path.join(FIXTURES, fixture_name), "rb") as f:
            r = client.post("/api/export/eq/import",
                            json={"name": name, "text": eq_mod.decode_profile(f.read())})
        assert r.status_code == 200, (fixture_name, r.text[:300])
        imported[fixture_name] = r.json()
    assert len(imported["peace_parametric.txt"]["filters"]) == 8, imported["peace_parametric.txt"]
    assert len(imported["peace_graphic_31.txt"]["filters"]) == 31, imported["peace_graphic_31.txt"]
    assert len(imported["earbuds_filtercurve.txt"]["filters"]) == 50, imported["earbuds_filtercurve.txt"]
    # The all-unknown file is importable AND visibly empty: an explicit answer,
    # not a flat curve standing in for one that was never read.
    assert imported["unknown_only.txt"]["empty"] is True, imported["unknown_only.txt"]
    assert imported["unknown_only.txt"]["filters"] == [], imported["unknown_only.txt"]
    listing = {p["id"]: p for p in client.get("/api/export/eq").json()["profiles"]}
    for row in imported.values():
        assert row["id"] in listing, (row["id"], sorted(listing))

    # A profile someone dropped into the folder themselves is read per its own
    # ENCODING: UTF-16 read as UTF-8 would be a profile with no filters at all,
    # which is the one outcome worse than an error.
    eq_folder = os.path.join(MUSIC, ".mlo", "data", "eq")
    with open(os.path.join(FIXTURES, "peace_graphic_31.txt"), "rb") as f:
        graphic_text = eq_mod.decode_profile(f.read())
    with open(os.path.join(eq_folder, "hand_copied.txt"), "wb") as f:
        f.write(graphic_text.encode("utf-16"))
    listing = {p["id"]: p for p in client.get("/api/export/eq").json()["profiles"]}
    assert len(listing["hand_copied"]["filters"]) == 31, listing["hand_copied"]
    assert listing["hand_copied"]["empty"] is False, listing["hand_copied"]

    # A hand-copied file with ONE unreadable band is listed as broken, and a run
    # that names it refuses every track with that line in the message — never a
    # chain built from the bands that did parse.
    with open(os.path.join(eq_folder, "half_broken.txt"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write("Preamp: -3 dB\n"
                "Filter 1: ON PK Fc 100 Hz Gain 2.0 dB Q 1.0\n"
                "Filter 2: ON PK Fc abc Gain 1.0 dB Q 1.0\n")
    listing = {p["id"]: p for p in client.get("/api/export/eq").json()["profiles"]}
    assert listing["half_broken"]["errors"], listing["half_broken"]
    DEST_BROKEN = os.path.join(ROOT, "DestBroken")
    os.makedirs(DEST_BROKEN)
    broken_run = client.post("/api/export", json={
        "paths": [loud], "dest": DEST_BROKEN, "codec": "flac",
        "structure": exporter.DEFAULT_STRUCTURE, "eq_profile": "half_broken",
        "embed_covers": False, "playlists": False, "sidecars": False})
    assert broken_run.status_code == 200, broken_run.text[:300]
    broken_body = broken_run.json()
    assert broken_body["failed"] == 1 and broken_body["eq_applied"] == 0, broken_body
    assert "Fc needs a number" in broken_body["errors"][0], broken_body["errors"]
    # The refusal is ONE sentence on every path: this module owns it
    # (`apply_refusal`), the export fails the track with the same line named,
    # the player installs nothing through it (PlayerBar), and the editor's
    # banner shows the same words. Nothing may build a chain from the bands that
    # happened to parse, either.
    stored_broken = eq_mod.find(MUSIC, "half_broken")
    refusal = eq_mod.apply_refusal(stored_broken)
    assert refusal == "this profile cannot be applied: " + stored_broken["errors"][0], \
        refusal
    assert stored_broken["errors"][0] in broken_body["errors"][0], broken_body["errors"]
    assert "cannot be applied" in broken_body["errors"][0], broken_body["errors"]
    assert eq_mod.apply_refusal(eq_mod.find(MUSIC, "test_bass")) == "", \
        eq_mod.find(MUSIC, "test_bass")
    try:
        eq_mod.chain(stored_broken)
    except ValueError as e:
        assert str(e) == refusal, str(e)
    else:
        raise AssertionError("a profile with errors must not build a chain")
    os.remove(os.path.join(eq_folder, "hand_copied.txt"))
    os.remove(os.path.join(eq_folder, "half_broken.txt"))

    # A malformed profile is refused over HTTP with the line (or the attribute)
    # named, and nothing is stored under the name that asked for it.
    for i, (text, needle) in enumerate((
            ("Filter 1: ON PK Fc abc Gain 3\n", "line 1"),
            ("GraphicEQ: 25 0; 40 x\n", "expected one 'frequency gain' pair"),
            ('FilterCurve: f0="20" v1="1"\n', "v1 has no f1"),
            ('FilterCurve: f0="20" v0="loud"\n', "is not a number"),
            ("Preamp: loud dB\n", "Preamp needs a number"))):
        malformed = client.post("/api/export/eq/import",
                                json={"name": f"bad line {i}", "text": text})
        assert malformed.status_code == 400, malformed.text[:200]
        detail = malformed.json()["detail"]
        assert "profile not imported" in detail and needle in detail, detail
        assert not os.path.exists(os.path.join(eq_folder, f"bad_line_{i}.txt"))
        assert f"bad_line_{i}" not in {
            p["id"] for p in client.get("/api/export/eq").json()["profiles"]}

    # ---------------------------------------------------- saved export configs
    # The Export page's whole form under a name — including the equalizer
    # profile BY ID — and the round trip that proves the identity: the config is
    # saved, loaded back, posted to `/api/export` unchanged, and the run reports
    # and stamps that profile on the file it wrote.
    profile_id = imported["earbuds_filtercurve.txt"]["id"]
    DEST_CFG = os.path.join(ROOT, "DestConfig")
    os.makedirs(DEST_CFG)
    form = dict(exporter.EXPORT_DEFAULTS)
    form.update({"source_kind": "library", "dest": DEST_CFG, "subfolder": "Music",
                 "codec": "flac", "quality": "8", "structure": exporter.DEFAULT_STRUCTURE,
                 "structure_script": "", "id3v2": "2.3", "embed_covers": False,
                 "eq_profile": profile_id})
    saved = client.post("/api/export/configs",
                        json={"name": "Earbuds curve (FLAC)", "config": form})
    assert saved.status_code == 200, saved.text[:300]
    row = saved.json()
    assert row["id"] == "Earbuds_curve_FLAC" and row["name"] == "Earbuds curve (FLAC)", row
    assert row["replaced"] is False, row
    assert row["eq_profile"] == profile_id and row["eq_missing"] is False, row
    assert row["eq_problem"] == "", row

    # One JSON file per name, holding the form under `config` — and NOT the
    # selection: which album was ticked is data, not configuration.
    stored_path = os.path.join(MUSIC, ".mlo", "data", "export_configs",
                               "Earbuds_curve_FLAC.json")
    assert os.path.isfile(stored_path), stored_path
    with open(stored_path, encoding="utf-8") as f:
        stored = json.load(f)
    assert stored["name"] == "Earbuds curve (FLAC)", stored
    assert stored["config"] == row["config"], stored
    assert stored["config"]["eq_profile"] == profile_id, stored["config"]
    # …and the file selection, which IS part of the form (unlike `paths`): the
    # config a user saved keeps the families that run wrote.
    assert stored["config"]["copy_files"] == ["audio"], stored["config"]
    assert "paths" not in stored["config"], sorted(stored["config"])

    # Load it back — a page that had been cleared gets the whole form — and run
    # exactly that body, with only the selection added.
    loaded = client.get("/api/export/configs/Earbuds_curve_FLAC")
    assert loaded.status_code == 200, loaded.text[:200]
    body = loaded.json()
    assert body["config"] == stored["config"], body
    assert body["config"]["source_kind"] == "library", body["config"]
    run = client.post("/api/export", json={**body["config"], "paths": [loud]})
    assert run.status_code == 200, run.text[:400]
    result = run.json()
    assert result["failed"] == 0 and result["exported"] == 1, result
    assert result["eq_profile"] == profile_id, result
    assert result["eq_applied"] == 1 and result["processed"] == 1, result
    out_flac = os.path.join(DEST_CFG, "Music", "Artist One", "Album A", "1-02 Loud.flac")
    assert exporter._processing_of(out_flac) == f"eq={profile_id}", \
        AudioFile(out_flac).all_tags()

    # The profile goes away (deleted — renaming it has the same effect, since an
    # id is what a config names): loading the config SAYS so and keeps the id,
    # rather than resolving to another profile or to none.
    assert client.delete(f"/api/export/eq/{profile_id}").status_code == 200
    gone = client.get("/api/export/configs/Earbuds_curve_FLAC").json()
    assert gone["eq_missing"] is True, gone
    assert "is gone" in gone["eq_problem"] and profile_id in gone["eq_problem"], gone
    assert gone["config"]["eq_profile"] == profile_id, gone["config"]
    listed_configs = {c["id"]: c for c in client.get("/api/export/configs").json()["configs"]}
    assert listed_configs["Earbuds_curve_FLAC"]["eq_missing"] is True, listed_configs
    gone_run = client.post("/api/export", json={**gone["config"], "paths": [loud]})
    assert gone_run.json()["failed"] == 1 and gone_run.json()["eq_applied"] == 0, gone_run.json()
    assert profile_id in gone_run.json()["errors"][0], gone_run.json()["errors"]
    # The same curve under a NEW name does not satisfy the old id: identity is
    # the id the config names, not a profile that looks like it.
    with open(os.path.join(FIXTURES, "earbuds_filtercurve.txt"), "rb") as f:
        eq_mod.import_profile(MUSIC, "Earbuds renamed", eq_mod.decode_profile(f.read()))
    renamed = client.get("/api/export/configs/Earbuds_curve_FLAC").json()
    assert renamed["eq_missing"] is True, renamed
    assert renamed["config"]["eq_profile"] == profile_id, renamed["config"]

    # Saving again under a name REPLACES that config, and says which happened.
    again = client.post("/api/export/configs",
                        json={"name": "Earbuds curve (FLAC)", "config": form})
    assert again.status_code == 200 and again.json()["replaced"] is True, again.text[:200]
    assert len({c["id"] for c in client.get("/api/export/configs").json()["configs"]}) == 1

    # A config the RUN would refuse cannot be saved: the form is validated
    # against the exporter's own tables, with the run's own sentences.
    for patch, needle in (
            ({"codec": "mp3x"}, "unknown codec"),
            ({"target": "carrier-pigeon"}, "unknown export target"),
            ({"structure": "artist_album_2003"}, "unknown folder structure"),
            ({"replaygain_mode": "loud"}, "unknown ReplayGain mode"),
            ({"workers": "4"}, "must be a whole number"),
            ({"embed_covers": "yes"}, "must be true or false"),
            ({"eq_profile": "../../../etc/passwd"}, "invalid equalizer profile id"),
            # The file selection is the exporter's own enum: a family it does
            # not have, and a selection that names no files at all, are refused
            # here with the sentence a run would give (a saved config must not
            # be a way to store an export the run would refuse).
            ({"copy_files": ["audio", "covers"]}, "unknown file famil"),
            ({"copy_files": []}, "at least one"),
            ({"copy_files": "audio"}, "must be a list"),
            ({"paths": [loud]}, "unknown config field")):
        bad_config = client.post("/api/export/configs",
                                 json={"name": "refuse me", "config": dict(form, **patch)})
        assert bad_config.status_code == 400, (patch, bad_config.text[:200])
        assert needle in bad_config.json()["detail"], (patch, bad_config.json())
    assert client.get("/api/export/configs/refuse_me").status_code == 404
    # A custom folder structure goes through the run's own validator, so a
    # script an export would refuse is not saveable either.
    bad_script = client.post("/api/export/configs", json={
        "name": "bad script",
        "config": dict(form, structure="custom", structure_script="%nope%/x")})
    assert bad_script.status_code == 400, bad_script.text[:200]
    assert "not a field" in bad_script.json()["detail"], bad_script.json()

    # Deleting, and the 404s either side of it.
    assert client.get("/api/export/configs/nope").status_code == 404
    assert client.delete("/api/export/configs/nope").status_code == 404
    # An id is never a path: a traversal spelling is either routed away before
    # the handler (Starlette normalizes the dots out of the path, so the
    # request becomes 404 Not Found / 405 Method Not Allowed) or refused by the
    # handler itself — never a 200, and never a file touched out of the folder.
    # The store's own refusal is pinned below, where the id does reach it.
    canary_cfg = os.path.join(MUSIC, ".mlo", "data", "canary.json")
    with open(canary_cfg, "w", encoding="utf-8") as f:
        f.write('{"not": "a saved config"}\n')
    for spelling in ("..%2F..%2Fcanary.json", "../canary.json", "..%2Fcanary",
                     "%2E%2E%2Fcanary.json"):
        assert client.delete(f"/api/export/configs/{spelling}").status_code in (400, 404, 405)
        assert client.get(f"/api/export/configs/{spelling}").status_code in (400, 404)
    assert client.get("/api/export/configs/%00canary").status_code == 400
    assert client.get("/api/export/configs/%00canary").json()["detail"].startswith(
        "invalid config id")
    for bad in ("../../canary", "..\\..\\canary", "..", "", "/etc/passwd"):
        try:
            exportconfigs.load(MUSIC, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"loading {bad!r} must be refused")
        try:
            exportconfigs.delete(MUSIC, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"deleting {bad!r} must be refused")
    assert os.path.isfile(canary_cfg), "a traversal deleted a file outside the folder"
    assert client.delete("/api/export/configs/Earbuds_curve_FLAC").json()["ok"] is True
    assert client.get("/api/export/configs/Earbuds_curve_FLAC").status_code == 404
    assert not os.path.exists(stored_path), "the deleted config's file is gone"
    os.remove(canary_cfg)

    # ---------------------------------------------------- lyrics in an export
    # A source album whose tracks keep their lyrics in the two places a library
    # does: one in the LYRICS tag, one in a `.lrc` beside it. Every mode has to
    # produce the lyrics in the form it names — for BOTH tracks, whichever way
    # the source happened to store them.
    LYRIC_DIR = os.path.join(MUSIC, "Artist One", "Album Lyrics")
    os.makedirs(LYRIC_DIR, exist_ok=True)
    LRC_TEXT = "[00:01.00]First line\n[00:05.50]Second line\n"

    def lyric_track(path, title, number, lyrics=None):
        subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1.5", "-c:a", "flac", path],
                       check=True, capture_output=True)
        af = AudioFile(path)
        af.defer_save(True)
        for key, value in (("ARTIST", "Artist One"), ("ALBUMARTIST", "Artist One"),
                           ("ALBUM", "Album Lyrics"), ("TITLE", title),
                           ("TRACKNUMBER", number), ("DISCNUMBER", "1")):
            af.set_tag(key, value)
        if lyrics:
            af.set_lyrics(lyrics)
        af.defer_save(False)
        return path

    tagged_track = lyric_track(os.path.join(LYRIC_DIR, "1-01 Tagged.flac"), "Tagged",
                               "01", lyrics="[00:02.00]Tagged line\n")
    sidecar_track = lyric_track(os.path.join(LYRIC_DIR, "1-02 Sidecar.flac"), "Sidecar", "02")
    with open(os.path.join(LYRIC_DIR, "1-02 Sidecar.lrc"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(LRC_TEXT)

    lyric_dests = {}
    for mode in ("embedded", "lrc", "both"):
        dest = os.path.join(ROOT, f"DestLyrics{mode}")
        os.makedirs(dest)
        lyric_dests[mode] = dest
        res = client.post("/api/export", json={
            "paths": [tagged_track, sidecar_track], "dest": dest, "codec": "flac",
            "structure": exporter.DEFAULT_STRUCTURE, "embed_covers": False,
            "playlists": False, "sidecars": False, "lyrics": mode})
        assert res.status_code == 200, res.text[:300]
        run = res.json()
        assert run["failed"] == 0, run["errors"]
        assert run["lyrics_mode"] == mode, run
        folder = os.path.join(dest, "Music", "Artist One", "Album Lyrics")
        one, two = ("1-01 Tagged", "1-02 Sidecar")
        assert os.path.isfile(os.path.join(folder, one + ".flac")), run
        assert os.path.isfile(os.path.join(folder, two + ".flac")), run
        embedded = {stem: (AudioFile(os.path.join(folder, stem + ".flac")).get_lyrics() or "")
                    for stem in (one, two)}
        files = {stem: os.path.isfile(os.path.join(folder, stem + ".lrc"))
                 for stem in (one, two)}
        rows = [f for f in run["excluded"] if f["kind"] == "lyrics"]
        if mode == "embedded":
            # The tag carries the lyrics for both tracks — the one whose lyrics
            # were in a FILE gets them embedded, so no mode loses them.
            assert "Tagged line" in embedded[one], embedded
            assert "First line" in embedded[two], embedded
            assert files == {one: False, two: False}, files
            assert run["lyrics_files"] == 0, run
            # ...and the source sidecar is reported as an extra, by name.
            assert {f["name"] for f in rows} == {"1-02 Sidecar.lrc"}, run["excluded"]
            assert run["excluded_counts"].get("lyrics") == 1, run["excluded_counts"]
        elif mode == "lrc":
            assert embedded == {one: "", two: ""}, embedded
            assert files == {one: True, two: True}, files
            assert run["lyrics_files"] == 2, run
            assert rows == [], run["excluded"]
            for stem, expected in ((one, "Tagged line"), (two, "First line")):
                with open(os.path.join(folder, stem + ".lrc"), encoding="utf-8") as f:
                    assert expected in f.read(), (stem, expected)
        else:
            assert "Tagged line" in embedded[one] and "First line" in embedded[two], embedded
            assert files == {one: True, two: True}, files
            assert run["lyrics_files"] == 2, run
            assert rows == [], run["excluded"]
            with open(os.path.join(folder, two + ".lrc"), encoding="utf-8") as f:
                assert "First line" in f.read()
        # The sidecar's own name is the exported track's own name (no second
        # naming rule), and it sits in the exported album folder.
        assert sorted(os.listdir(folder)) == sorted(
            [one + ".flac", two + ".flac"]
            + ([one + ".lrc"] if files[one] else [])
            + ([two + ".lrc"] if files[two] else [])), os.listdir(folder)
    print(f"    lyrics: modes wrote "
          f"embedded={os.listdir(os.path.join(lyric_dests['embedded'], 'Music', 'Artist One', 'Album Lyrics'))}, "
          f"lrc={os.listdir(os.path.join(lyric_dests['lrc'], 'Music', 'Artist One', 'Album Lyrics'))}")

    # The half of the audit the modes turn around: a `.lrc` whose track is NOT
    # in the selection stays in the library and is still reported.
    PARTIAL_DIR = os.path.join(ROOT, "DestLyricsPartial")
    os.makedirs(PARTIAL_DIR, exist_ok=True)
    partial = client.post("/api/export", json={
        "paths": [tagged_track], "dest": PARTIAL_DIR,
        "codec": "flac", "structure": exporter.DEFAULT_STRUCTURE,
        "embed_covers": False, "playlists": False, "sidecars": False, "lyrics": "lrc"})
    assert partial.status_code == 200, partial.text[:200]
    partial_run = partial.json()
    assert {f["name"] for f in partial_run["excluded"] if f["kind"] == "lyrics"} == \
        {"1-02 Sidecar.lrc"}, partial_run["excluded"]

    # The option is part of a saved config, and it survives the round trip.
    os.makedirs(os.path.join(ROOT, "DestLyricsLoaded"), exist_ok=True)
    lyrics_cfg = client.post("/api/export/configs", json={
        "name": "Lyrics to files",
        "config": dict(form, dest=os.path.join(ROOT, "DestLyricsLoaded"),
                       codec="flac", structure=exporter.DEFAULT_STRUCTURE,
                       embed_covers=False, lyrics="lrc",
                       eq_profile="")})   # this run is about lyrics, not the EQ
    assert lyrics_cfg.status_code == 200, lyrics_cfg.text[:200]
    assert lyrics_cfg.json()["config"]["lyrics"] == "lrc", lyrics_cfg.json()
    back = client.get(f"/api/export/configs/{lyrics_cfg.json()['id']}")
    assert back.json()["config"]["lyrics"] == "lrc", back.json()
    loaded_lyrics = client.post("/api/export",
                                json={**back.json()["config"], "paths": [tagged_track]})
    assert loaded_lyrics.status_code == 200, loaded_lyrics.text[:200]
    loaded_run = loaded_lyrics.json()
    assert loaded_run["lyrics_mode"] == "lrc" and loaded_run["lyrics_files"] == 1, loaded_run
    assert os.path.isfile(os.path.join(ROOT, "DestLyricsLoaded", "Music", "Artist One",
                                       "Album Lyrics", "1-01 Tagged.lrc")), loaded_run
    assert client.delete(f"/api/export/configs/{lyrics_cfg.json()['id']}").json()["ok"] is True

    # ---------------------------------------------------- zip target
    zip_body = {"paths": [quiet, loud], "dest": "", "target": "zip",
                "codec": "flac", "quality": "", "structure": exporter.DEFAULT_STRUCTURE,
                "manifest": True, "playlists": True, "sidecars": False,
                "embed_covers": False, "verify": True, "workers": 1}
    run = client.post("/api/export", json=zip_body)
    assert run.status_code == 200, run.text[:400]
    made = run.json()
    assert made["failed"] == 0, made["errors"]
    assert made["exported"] == 2, made
    first = made["zip"]
    assert first["url"] == f"/api/export/zip/{first['id']}", first
    assert first["name"] == "la-musica-export-2-tracks.zip", first
    first_path = exporter.zip_path(CFG, first["id"])
    assert first_path and os.path.getsize(first_path) == first["bytes"], first

    download = client.get(first["url"])
    assert download.status_code == 200, download.status_code
    assert download.headers["content-type"] == "application/zip", download.headers
    assert first["name"] in download.headers["content-disposition"], download.headers
    with zipfile.ZipFile(io.BytesIO(download.content)) as zf:
        names = sorted(zf.namelist())
        # The archive opens as the folder structure the run asked for — the
        # selection, its playlist and the checksum manifest, nothing else.
        assert names == ["Artist One/Album A/1-01 Quiet.flac",
                         "Artist One/Album A/1-02 Loud.flac",
                         "Artist One/Album A/Album A.m3u8",
                         "all.m3u8", "checksums.sha256"], names
        manifest = zf.read("checksums.sha256").decode("utf-8").splitlines()
        assert len(manifest) == len(names) - 1, manifest
        for line in manifest:
            digest, rel = line.split("  ", 1)
            assert rel in names, (rel, names)
            assert hashlib.sha256(zf.read(rel)).hexdigest() == digest, rel
        # The audio in the archive is the real, playable export.
        assert zf.read("Artist One/Album A/1-01 Quiet.flac")[:4] == b"fLaC"
    print(f"    zip: {first['name']} ({first['bytes']} bytes) members {names}")

    # A new export replaces the kept one, and the old id stops answering.
    time.sleep(1.1)   # the id is a timestamp; a second run must be a new one
    second = client.post("/api/export", json=dict(zip_body, paths=[quiet])).json()
    assert second["zip"]["id"] != first["id"], second["zip"]
    assert second["zip"]["name"] == "la-musica-export-1-tracks.zip", second["zip"]
    assert client.get(first["url"]).status_code == 404
    assert not os.path.exists(first_path), "the replaced archive is deleted"
    staged = os.path.join(MUSIC, ".mlo", "data", "export_zip")
    kept = [os.path.join(base, name)
            for base, _dirs, files in os.walk(staged) for name in files
            if name.endswith(".zip")]
    assert len(kept) == 1, kept
    assert os.path.basename(kept[0]) == second["zip"]["name"], kept

    # Deleting the archive early, and the 404 that follows.
    assert client.delete(second["zip"]["url"]).json()["ok"] is True
    assert client.get(second["zip"]["url"]).status_code == 404
    assert client.delete(second["zip"]["url"]).status_code == 404
    # An id is never a path here either.
    assert client.get("/api/export/zip/..%2F..%2Fconfig.json").status_code in (400, 404)
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print("ok  export audio: lyrics embedded / .lrc / both per setting (tag vs file vs both, "
      "the audit reporting a travelling source .lrc as output and a non-selected one as an "
      "extra, and the option surviving a config round trip); Equalizer APO/Peace parsing of the real fixture files "
      "(parametric, graphic, FilterCurve, empty, BOM/CRLF, UTF-16) with the malformed "
      "cases refused by name and APO's own type spellings (PEQ/Modal/LPQ/HPQ, "
      "'LS 6dB'/'LS 12dB', 'LSC 10.8 dB', 'BW Oct 0.5') read as the filter they name, "
      "AP/IIR reported and not applied, If:/ElseIf: blocks reported and their bands "
      "left out, the render bounds clamped the same way the player clamps them, "
      "the literal -af chain and its real ffmpeg run, album gain "
      "applied to the samples with REPLAYGAIN_* stripped, track gain for a partial "
      "selection, tags mode unchanged, a measurable bass shelf with the mids left alone, "
      "a missing or unreadable profile refused by every path with the one sentence, copy refusing to filter, the "
      "EQ endpoints (list/import/delete/traversal/oversize), saved export configs "
      "(save/load/delete, validation, and the equalizer profile surviving the round trip "
      "by identity or reporting itself gone) and a zip export that replaces the "
      "previous archive")
