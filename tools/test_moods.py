#!/usr/bin/env python3
"""Mood classification contract (mlo.moods).

Deterministic engine-side mood scoring: the valence/arousal axes must
actually track the audio (a loud/fast/bright synthetic track must out-score
a quiet/slow/dark one on BOTH axes and land on a different label), the
genre table must map representative genres, hybrid mode must only let a
prior override a LOW-confidence audio verdict while "audio" ignores the
genre outright, and nothing may ever raise.

Synthetic signals (stdlib `wave` only, no fixtures on disk): 8 s of
150 BPM noise hits (loud/fast/bright), 8 s of soft 60 BPM low swells
(quiet/slow/dark) and a mid-level bright noise bed over an A-minor triad
(deliberately ambiguous: energy/valence near the quadrant centre, so the
verdict is low-confidence and a genre prior can flip it).

Run:  python tools/test_moods.py
"""
import array
import math
import os
import random
import shutil
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import moods

SR = 22050
SECONDS = 8


def _write_wav(path, frames):
    """Mono 16-bit PCM WAV from a float iterable."""
    data = array.array("h", (max(-32767, min(32767, int(v * 32767))) for v in frames))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


def _bright_fast():
    """150 BPM high-level noise hits over a quiet noise floor."""
    period = SR * 60 // 150
    rng = random.Random(11)
    for i in range(SR * SECONDS):
        t = (i % period) / SR
        env = math.exp(-t * 30) if t < 0.12 else 0.0
        yield 0.9 * env * rng.uniform(-1, 1) + 0.05 * rng.uniform(-1, 1)


def _dark_slow():
    """60 BPM soft low swells, mixed well below the bright track."""
    rng = random.Random(11)
    for i in range(SR * SECONDS):
        t = (i % SR) / SR
        env = (1 - math.exp(-t * 6)) * math.exp(-t * 0.9)
        yield 0.10 * env * (math.sin(2 * math.pi * 55 * i / SR) +
                            0.5 * math.sin(2 * math.pi * 82 * i / SR))


def _ambiguous():
    """Bright noise bed + A-minor triad: mid energy, mid valence."""
    rng = random.Random(13)
    prev = 0.0
    for i in range(SR * SECONDS):
        t = i / SR
        x = rng.uniform(-1, 1)
        prev = 0.55 * prev + 0.45 * x
        tone = (math.sin(2 * math.pi * 220 * t) + 0.7 * math.sin(2 * math.pi * 261.63 * t) +
                0.5 * math.sin(2 * math.pi * 329.63 * t))
        yield 0.045 * (0.45 * prev + 0.35 * x + tone)


# ----------------------------------------------------------------------
# Genre priors
# ----------------------------------------------------------------------
assert moods.MOODS == ["happy", "energetic", "aggressive", "sad", "calm",
                       "dreamy", "dark", "party"], moods.MOODS

for genre, expected in [
    ("Metal", "aggressive"),
    ("post-hardcore", "aggressive"),
    ("Ambient", "calm"),
    ("Downtempo; Ambient", "calm"),
    ("Shoegaze", "dreamy"),
    ("Dream-Pop", "dreamy"),
    ("House", "party"),
    ("EDM / Dance", "party"),
    ("Drum and Bass", "party"),
    ("Blues", "dark"),
    ("Doom Metal", "aggressive"),     # longest keyword wins ("metal" > "doom")
    ("Sadcore", "sad"),
    ("Emo", "sad"),
    ("Classical", "calm"),
    ("Rock", "energetic"),
]:
    got = moods.genre_prior(genre)
    assert got == expected, (genre, got, expected)
    assert got in moods.MOODS, (genre, got)

# No opinion: empty, missing and unknown genres.
for genre in (None, "", "   ", "Polka", "Field Recording"):
    assert moods.genre_prior(genre) is None, genre

# ----------------------------------------------------------------------
# classify() on synthetic audio
# ----------------------------------------------------------------------
root = tempfile.mkdtemp(prefix="mlo_moods_")
try:
    bright = os.path.join(root, "bright_fast.wav")
    dark = os.path.join(root, "dark_slow.wav")
    amb = os.path.join(root, "amb.wav")
    _write_wav(bright, _bright_fast())
    _write_wav(dark, _dark_slow())
    _write_wav(amb, _ambiguous())

    # Unanalysable input returns None instead of raising — true with or
    # without librosa, so it is asserted before the analysis branch.
    assert moods.classify(os.path.join(root, "missing.wav")) is None
    assert moods.classify("") is None
    assert moods.classify(None) is None
    garbage = os.path.join(root, "garbage.wav")
    with open(garbage, "wb") as f:
        f.write(b"not audio at all" * 64)
    assert moods.classify(garbage) is None
    assert moods.mood_for_track(garbage, {"mood_source": "hybrid"}, "Metal") is None

    # ------------------------------------------------------------------
    # apply_mood_tags: mood_enabled + should_write_audio_tag gates, and no
    # rewrite when the tag already carries the verdict.
    # ------------------------------------------------------------------
    class FakeAudio:
        def __init__(self, current=None):
            self.tags = {} if current is None else {"MOOD": current}
            self.writes = []

        def get_tag(self, name):
            return self.tags.get(name.upper())

        def set_tag(self, name, value):
            self.tags[name.upper()] = value
            self.writes.append((name, value))
            return True

    flac = os.path.join(root, "track.flac")
    shutil.copyfile(bright, flac)   # extension only; the gate keys off .flac
    cfg = {"mood_enabled": True, "mood_source": "audio"}

    rb, rd = moods.classify(bright), moods.classify(dark)
    if rb is None or rd is None:
        # No librosa: CI installs server/requirements.txt only, while the
        # vendored .dependencies/librosa (and pip's) is a desktop install.
        # The classifier's contract in that case is "None, never an
        # exception" — assert that, assert that nothing can be tagged with no
        # verdict, and skip the analysis cases, exactly like the ffmpeg and
        # fpcalc sections in the neighbouring suites.
        print("  skip classify() / mood_source cases: librosa not installed")
        assert rb is None and rd is None, (rb, rd)
        assert moods.apply_mood_tags(FakeAudio(), flac, cfg) is False
        assert moods.apply_mood_tags(None, flac, cfg) is False
    else:
        for r, name in ((rb, "bright"), (rd, "dark")):
            assert r["mood"] in moods.MOODS, (name, r)
            assert 0.0 <= r["energy"] <= 1.0 and 0.0 <= r["valence"] <= 1.0, (name, r)
            assert 0.0 <= r["confidence"] <= 1.0, (name, r)
            assert r["tempo"] > 0, (name, r)
            assert set(r["features"]) >= {"duration", "rms_db", "dynamic_range_db",
                                          "onset_rate", "centroid_hz",
                                          "percussive_ratio", "tempo", "key"}, r
        # The axes must track the recipe, not just produce a label: the loud/
        # fast/bright track scores higher on BOTH energy and valence than the
        # quiet one.
        assert rb["energy"] > rd["energy"], (rb["energy"], rd["energy"])
        assert rb["valence"] > rd["valence"], (rb["valence"], rd["valence"])
        assert rb["mood"] != rd["mood"], (rb["mood"], rd["mood"])

        # --------------------------------------------------------------
        # mood_source: audio ignores the genre, hybrid lets a prior flip a
        # low-confidence audio verdict, provider always trusts the prior.
        # --------------------------------------------------------------
        audio_only = moods.mood_for_track(amb, {"mood_source": "audio"}, "Ambient")
        assert audio_only == moods.classify(amb), (audio_only, moods.classify(amb))
        assert audio_only["mood"] != "calm", audio_only
        assert moods.mood_for_track(amb, {"mood_source": "audio"}, None)["mood"] == audio_only["mood"]

        # The ambiguous track is deliberately near the quadrant centre, so its
        # verdict is low-confidence — the precondition of the hybrid rule.
        assert audio_only["confidence"] < moods.AUDIO_LOW_CONF, audio_only

        hybrid = moods.mood_for_track(amb, {"mood_source": "hybrid"}, "Ambient")
        assert hybrid["mood"] == "calm", hybrid
        assert hybrid["features"] == audio_only["features"], (hybrid, audio_only)
        # ...and a confident verdict is NOT overridden by a prior.
        confident = moods.mood_for_track(bright, {"mood_source": "hybrid"}, "Ambient")
        assert confident["confidence"] >= moods.AUDIO_LOW_CONF, confident
        assert confident["mood"] == rb["mood"], (confident, rb)
        # No prior -> audio verdict stays, whatever the confidence.
        assert moods.mood_for_track(amb, {"mood_source": "hybrid"}, "Polka")["mood"] == audio_only["mood"]
        assert moods.mood_for_track(amb)["mood"] == audio_only["mood"]
        # Provider mode trusts the genre outright.
        assert moods.mood_for_track(bright, {"mood_source": "provider"}, "Shoegaze")["mood"] == "dreamy"

        handle = FakeAudio()
        assert moods.apply_mood_tags(handle, flac, cfg) is True, handle.writes
        assert handle.writes == [("MOOD", rb["mood"])], handle.writes
        # Same value already present -> no write.
        assert moods.apply_mood_tags(FakeAudio(current=rb["mood"]), flac, cfg) is False
        # Different value -> rewritten.
        assert moods.apply_mood_tags(FakeAudio(current="dark"), flac, cfg) is True
        # Gates.
        assert moods.apply_mood_tags(FakeAudio(), flac, {"mood_enabled": False}) is False
        assert moods.apply_mood_tags(None, flac, cfg) is False
        assert moods.apply_mood_tags(FakeAudio(), os.path.join(root, "missing.flac"), cfg) is False
finally:
    shutil.rmtree(root, ignore_errors=True)

print("ok")
