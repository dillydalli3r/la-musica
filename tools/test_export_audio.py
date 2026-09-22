#!/usr/bin/env python3
"""Export AUDIO processing: the ReplayGain modes, the equalizer profiles, zip.

The Export page can now change the audio itself, not just its tags or its
container, so this suite pins the parts of that which a tag cannot show:

  * the Equalizer APO / Peace parser, fed a real parametric profile and a real
    Peace export (both in this file), including the lines it REFUSES to guess
    at — a profile that silently loses its Include half is not the curve the
    user saved;
  * the rendered ``-af`` chain, asserted literally and then actually run
    through ffmpeg, since a chain that only looks right is worth nothing;
  * ``replaygain_mode=apply``: an album's quiet and loud track must come out
    with the album's own balance (level difference) intact while the album as a
    whole lands on the ReplayGain reference, and neither file may carry
    REPLAYGAIN_* tags (the gain is in the samples now);
  * ``eq_profile``: a bass shelf must raise the low band measurably and leave
    the mid band where it was;
  * the copy codec refusing a filtering run with the one message the UI shows;
  * the EQ endpoints and the zip target end to end.

Every number below is measured with the same ffmpeg EBU R128 meter the export
itself uses (mlo.loudness), so "it changed the audio" is a measurement.

Run:  python tools/test_export_audio.py
"""
import hashlib
import io
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
assert peace["notes"] and "6 line(s)" in peace["notes"][0], peace["notes"]
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

# A malformed filter line is reported, not half-rendered: an unknown type, a
# missing frequency and a non-numeric value all land in `unsupported`.
broken = eq_mod.parse_apo(
    "Filter 1: ON PK Fc 1000 Gain 3 Q 1\n"
    "Filter 2: ON XX Fc 200 Gain 3\n"
    "Filter 3: ON PK\n"
    "Filter 4: ON PK Fc abc Gain 1\n")
assert len(broken["filters"]) == 1 and len(broken["unsupported"]) == 3, broken
assert eq_mod.to_af(broken) == "equalizer=f=1000:t=q:w=1:g=3", eq_mod.to_af(broken)

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

print("ok  export audio: Equalizer APO/Peace parsing (incl. the refused lines), the "
      "literal -af chain and its real ffmpeg run, album gain applied to the samples "
      "with REPLAYGAIN_* stripped, track gain for a partial selection, tags mode "
      "unchanged, a measurable bass shelf with the mids left alone, a missing "
      "profile failing its tracks, copy refusing to filter, the EQ endpoints "
      "(list/import/delete/traversal/oversize) and a zip export that replaces "
      "the previous archive")
