"""Mood classification from audio (script 8, Auto tagging) — engine side.

Deterministic, offline mood tagging: the MOOD value is derived from the
track's own audio (no model files, no training data, no network), scored
against hand-tuned thresholds kept as named module constants so they stay
tunable.

Feature recipe (Russell's valence/arousal model plus two refinements)
--------------------------------------------------------------------
AROUSAL (how much energy the track projects) is the weighted mean of

  * RMS level in dB          — a loud master reads as high energy,
  * onset rate (onsets/sec)  — event density, not just loudness,
  * tempo (BPM)              — the strongest single cue musicians agree on,
  * dynamic range            — INVERTED and lightly weighted: a whisper-to-
                               roar mix is not the "constant drive" that
                               high arousal means, so it loses energy.

VALENCE (how positive the track sounds) is the weighted mean of

  * spectral centroid        — brightness; dull/dark spectra read negative,
  * major-vs-minor key       — the only harmony cue available without a
                               trained model (0.5 when the key is unknown,
                               so a missing key never fakes positivity),
  * tempo                    — fast tracks skew positive,
  * percussiveness (HPSS)    — a percussive mix reads more celebratory than
                               a sustained/ambient one at equal loudness.

Both axes are mapped to 0..1 through fixed break points (the ``*_LO`` /
``*_HI`` constants) and then cut into layers, most specific first:

  party       high arousal + positive + percussive + >= PARTY_TEMPO BPM
  aggressive  high arousal + negative + percussive
  happy       high arousal + positive
  energetic   high arousal + not positive (instrumental/club drive)
  dreamy      below-mid arousal + bright spectrum + not negative
  dark        negative + dull spectrum (any energy)
  sad         below-mid arousal + negative
  calm        everything else (quiet, un-percussive, not negative)

``confidence`` is a margin proxy: how far the (arousal, valence) point sits
from the quadrant centre, discounted when tempo or key could not be
measured. In ``hybrid`` mood_source a genre prior only overrides the audio
verdict when that confidence is below ``AUDIO_LOW_CONF`` (the audio wins
whenever it is sure of itself).

AROUSAL is written out as the ENERGY tag too (integer 0-100) whenever MOOD
is — the same number the quadrant rules were scored from, so a player or a
grade can use the continuous value without re-deriving the label. Both are
gated per filetype by ``audio_tag_writes`` (MOOD / ENERGY).

Music videos (MKV/VOB/AVI/… — first-class library tracks, graded like any
other) are analysed as well: no librosa decoder opens a video container, so
the audio stream is extracted with ffmpeg first (see ``_load_signal``).

Performance: ONE file is decoded at a time, capped at
``ANALYSIS_MAX_SECONDS`` of audio — an album is never loaded into memory.

librosa comes from ``mlo.audiometa._ensure_librosa`` (vendored
``.dependencies/librosa vX.Y`` first, pip fallback); the module imports
without librosa, missing/undecodable audio simply returns None.
"""
import math
import os
import warnings

from .audio import AudioFile
from .audiometa import _detect_bpm, _detect_key, _ensure_librosa, _load_signal
from .config import should_write_audio_tag
from .paths import LIB_AUDIO_EXTS
from .stats import (_collect_targets, _make_pbar, _pbar_skip, _pbar_update,
                    _walk_files, new_stats, worker_count)
from .ui import Color, c, log, print_header

MOODS = ["happy", "energetic", "aggressive", "sad", "calm", "dreamy", "dark", "party"]

# --- analysis ---------------------------------------------------------
ANALYSIS_SR = 22050
ANALYSIS_MAX_SECONDS = 120.0
MIN_SECONDS = 2.0

# --- feature break points (value -> 0..1); the tunable knobs -----------
RMS_DB_LO, RMS_DB_HI = -35.0, -12.0
ONSET_RATE_LO, ONSET_RATE_HI = 0.5, 6.0
TEMPO_LO, TEMPO_HI = 70.0, 170.0
CENTROID_LO, CENTROID_HI = 500.0, 4000.0
DYN_RANGE_LO, DYN_RANGE_HI = 3.0, 20.0
# ponytail: these quadrant/axis thresholds are hand-tuned on music-cognition
# rules of thumb, no training corpus — revisit if users report mislabels.
AROUSAL_MID = 0.50
VALENCE_MID = 0.50
DREAMY_BRIGHTNESS = 0.50
DARK_CENTROID = 0.45
PARTY_TEMPO = 128.0
PARTY_AROUSAL = 0.60
PARTY_VALENCE = 0.55
PARTY_PERCUSSIVE = 0.50
AGGRESSIVE_AROUSAL = 0.60
AGGRESSIVE_VALENCE = 0.45
AGGRESSIVE_PERCUSSIVE = 0.45
DARK_VALENCE = 0.35

# arousal / valence weights (each group sums to 1.0)
W_AROUSAL_RMS, W_AROUSAL_ONSET, W_AROUSAL_TEMPO, W_AROUSAL_DYN = 0.40, 0.25, 0.25, 0.10
W_VALENCE_BRIGHT, W_VALENCE_KEY, W_VALENCE_TEMPO, W_VALENCE_PERC = 0.35, 0.30, 0.20, 0.15
VALENCE_MAJOR, VALENCE_MINOR, VALENCE_UNKNOWN_KEY = 1.00, 0.15, 0.50

# --- confidence -------------------------------------------------------
CONF_BASE = 0.35
CONF_SLOPE = 0.55
CONF_NO_KEY = 0.90
CONF_NO_TEMPO = 0.85
CONF_MIN, CONF_MAX = 0.05, 0.95
# A genre prior only overrides audio below this confidence (hybrid mode).
AUDIO_LOW_CONF = 0.45

# --- genre priors -----------------------------------------------------
# Ordered table: a match scores the LENGTH of the matched keyword, so
# "dream pop" (dreamy) beats the bare "pop" (happy) inside it and
# "drum and bass" is found whole. Duplicate/substring matches only ever
# reinforce one mood, so the first mood in table order wins ties.
GENRE_MOODS = (
    ("aggressive", ("metal", "punk", "hardcore", "thrash", "industrial",
                    "crossover", "grindcore", "screamo", "metalcore")),
    ("dreamy", ("dream pop", "shoegaze", "ethereal", "dreampop", "chillwave",
                "dreamgaze", "space rock")),
    ("party", ("dance", "house", "edm", "techno", "trance", "club",
               "electro", "disco", "dubstep", "drum and bass", "rave",
               "garage", "reggaeton")),
    ("calm", ("ambient", "classical", "acoustic", "folk", "piano", "drone",
              "new age", "downtempo", "lo fi", "lounge", "jazz", "bossa")),
    ("energetic", ("rock", "electronic", "synthpop", "alternative", "indie",
                   "ska", "big beat", "breakbeat")),
    ("happy", ("pop", "funk", "motown", "soul", "reggae", "disco pop",
               "sunshine")),
    ("dark", ("doom", "dark ambient", "darkwave", "gothic", "goth",
              "blues", "trip hop", "witch house", "sludge")),
    ("sad", ("sadcore", "slowcore", "emo", "ballad", "singer songwriter",
             "melancholy", "melancholic", "breakup", "elegy")),
)


def genre_prior(genre):
    """Mood prior for a genre string, or None when nothing matches.

    The genre may be multi-valued (MusicBrainz style ``;`` / ``/`` / ``,``
    lists) and hyphenated; matching is substring based on a lowercased,
    hyphen-free copy so "Dream-Pop" and "dream pop" agree. The longest
    matched keyword wins; None means "no opinion" and therefore counts as
    a LOW-confidence prior in hybrid mode.
    """
    if not genre:
        return None
    text = str(genre).lower().replace("-", " ")
    best_mood, best_score = None, 0
    for mood, keywords in GENRE_MOODS:
        score = sum(len(kw) for kw in keywords if kw in text)
        if score > best_score:
            best_mood, best_score = mood, score
    return best_mood


# ----------------------------------------------------------------------
# Features
# ----------------------------------------------------------------------
def _norm(value, lo, hi):
    """Map a feature onto 0..1 through its break points (clamped)."""
    if lo == hi:
        return 0.0
    return max(0.0, min(1.0, (float(value) - lo) / (hi - lo)))


def _features(y, sr):
    """Raw feature dict for one mono signal. Raises on librosa failure."""
    import numpy as np
    import librosa

    duration = float(y.size) / sr
    rms = np.asarray(librosa.feature.rms(y=y)[0], dtype=float)
    rms_mean = float(rms.mean()) if rms.size else 0.0
    if rms.size >= 4:
        p95, p05 = np.percentile(rms, [95, 5])
        dyn_db = 20.0 * math.log10(max(float(p95), 1e-9) / max(float(p05), 1e-9))
    else:
        dyn_db = 0.0

    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    try:
        onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr)
        onset_rate = float(len(onsets)) / max(duration, 1e-6)
    except Exception:
        onset_rate = 0.0

    centroid_hz = float(np.asarray(librosa.feature.spectral_centroid(y=y, sr=sr)).mean())
    try:
        _, y_perc = librosa.effects.hpss(y)
        total = float(np.sum(y * y))
        percussive = float(np.sum(y_perc * y_perc)) / total if total > 0 else 0.0
    except Exception:
        percussive = 0.0

    tempo = _detect_bpm(y, sr)
    key = _detect_key(y, sr)
    return {
        "duration": round(duration, 3),
        "rms_db": round(20.0 * math.log10(max(rms_mean, 1e-9)), 2),
        "dynamic_range_db": round(dyn_db, 2),
        "onset_rate": round(onset_rate, 3),
        "centroid_hz": round(centroid_hz, 1),
        "percussive_ratio": round(percussive, 3),
        "tempo": float(tempo) if tempo else None,
        "key": (f"minor" if key[1] else "major") if key else None,
    }


def _score(features):
    """(arousal, valence, confidence) from a feature dict."""
    tempo = features.get("tempo") or 120.0
    tempo_s = _norm(tempo, TEMPO_LO, TEMPO_HI)
    rms_s = _norm(features.get("rms_db", RMS_DB_LO), RMS_DB_LO, RMS_DB_HI)
    onset_s = _norm(features.get("onset_rate", 0.0), ONSET_RATE_LO, ONSET_RATE_HI)
    dyn_s = _norm(features.get("dynamic_range_db", 0.0), DYN_RANGE_LO, DYN_RANGE_HI)
    bright_s = _norm(features.get("centroid_hz", 0.0), CENTROID_LO, CENTROID_HI)
    perc_s = max(0.0, min(1.0, float(features.get("percussive_ratio") or 0.0)))

    key = features.get("key")
    if key == "major":
        key_s = VALENCE_MAJOR
    elif key == "minor":
        key_s = VALENCE_MINOR
    else:
        key_s = VALENCE_UNKNOWN_KEY

    arousal = (W_AROUSAL_RMS * rms_s + W_AROUSAL_ONSET * onset_s +
               W_AROUSAL_TEMPO * tempo_s + W_AROUSAL_DYN * (1.0 - dyn_s))
    valence = (W_VALENCE_BRIGHT * bright_s + W_VALENCE_KEY * key_s +
               W_VALENCE_TEMPO * tempo_s + W_VALENCE_PERC * perc_s)

    margin = max(abs(arousal - AROUSAL_MID), abs(valence - VALENCE_MID))
    conf = min(CONF_MAX, CONF_BASE + CONF_SLOPE * min(1.0, margin * 2.0))
    if not key:
        conf *= CONF_NO_KEY
    if not features.get("tempo"):
        conf *= CONF_NO_TEMPO
    return arousal, valence, max(CONF_MIN, conf), bright_s, perc_s, tempo


def _verdict(features):
    """Mood label for a feature dict (see the module docstring rules)."""
    a, v, conf, bright, perc, tempo = _score(features)
    if a >= PARTY_AROUSAL and v >= PARTY_VALENCE and perc >= PARTY_PERCUSSIVE and tempo >= PARTY_TEMPO:
        mood = "party"
    elif a >= AGGRESSIVE_AROUSAL and v < AGGRESSIVE_VALENCE and perc >= AGGRESSIVE_PERCUSSIVE:
        mood = "aggressive"
    elif a >= AROUSAL_MID and v >= VALENCE_MID:
        mood = "happy"
    elif a >= AROUSAL_MID:
        mood = "energetic"
    elif bright >= DREAMY_BRIGHTNESS and v >= DARK_VALENCE:
        mood = "dreamy"
    elif v < DARK_VALENCE and bright < DARK_CENTROID:
        mood = "dark"
    elif v < VALENCE_MID:
        mood = "sad"
    else:
        mood = "calm"
    return {
        "mood": mood,
        "energy": round(a, 3),
        "valence": round(v, 3),
        "tempo": round(tempo, 1),
        "confidence": round(conf, 3),
        "features": features,
    }


# ----------------------------------------------------------------------
# Public classification
# ----------------------------------------------------------------------
def classify(path, cfg=None):
    """Mood verdict for one audio file, or None when it cannot be analysed.

    Decodes at most ANALYSIS_MAX_SECONDS of mono audio; librosa missing,
    an unreadable/garbage file or a too-short track all return None. Never
    raises, so a scanning caller can simply move on to the next track.
    """
    if not path or not os.path.isfile(path):
        return None
    if _ensure_librosa() is None:
        return None
    try:
        with warnings.catch_warnings():
            # librosa warns loudly on decode fallbacks and on degenerate
            # chroma (near-silent/synthetic audio); both are handled here.
            warnings.simplefilter("ignore")
            y, sr = _load_signal(path, ANALYSIS_SR, ANALYSIS_MAX_SECONDS)
            if y is None or y.size < sr * MIN_SECONDS:
                return None
            return _verdict(_features(y, sr))
    except Exception:
        return None


def mood_for_track(path, cfg=None, genre=None):
    """classify() refined by the genre prior according to mood_source.

    ``audio`` ignores the genre outright. ``provider`` trusts a matching
    genre prior over the audio. ``hybrid`` (the default) keeps the audio
    verdict unless it is low-confidence and a prior exists — audio has the
    last word whenever it is sure of itself.
    """
    result = classify(path, cfg)
    if result is None:
        return None
    source = str((cfg or {}).get("mood_source") or "hybrid").lower()
    if source == "audio":
        return result
    prior = genre_prior(genre)
    if prior is None:
        return result
    if source == "provider":
        result["mood"] = prior
        result["confidence"] = round(result["confidence"] * CONF_NO_KEY, 3)
        return result
    if result["confidence"] < AUDIO_LOW_CONF:
        result["mood"] = prior
    return result


def apply_mood_tags(audio, path, cfg, genre=None):
    """Classify *path* and write MOOD through an AudioFile-like handle.

    Follows the script-8 tag pattern (see mlo/autotag.py): gated by
    ``cfg["mood_enabled"]`` and
    ``mlo.config.should_write_audio_tag(cfg, "MOOD", path)``, and the tag
    is only written when it differs from the value already present.
    ENERGY (the arousal the verdict was scored from, as an integer 0-100)
    rides along, gated by ``audio_tag_writes['ENERGY']``. Returns True when
    a tag was actually written.
    """
    if audio is None or not (cfg or {}).get("mood_enabled", True):
        return False
    try:
        result = mood_for_track(path, cfg, genre)
        if not result:
            return False
        pending = {}
        if (should_write_audio_tag(cfg, "MOOD", filepath=path)
                and str(audio.get_tag("MOOD") or "").strip().lower()
                != result["mood"]):
            pending["MOOD"] = result["mood"]
        if should_write_audio_tag(cfg, "ENERGY", filepath=path):
            energy = str(int(round(float(result["energy"]) * 100)))
            if str(audio.get_tag("ENERGY") or "").strip() != energy:
                pending["ENERGY"] = energy
        if not pending:
            return False
        if getattr(audio, "is_video", False):
            # A video container is rewritten whole on every tag write (see
            # mlo.audio), so both tags go in ONE ffmpeg pass.
            return bool(audio.set_video_tags(pending))
        # Both tags go to disk in ONE container rewrite: without the
        # deferral each set_tag() re-saved the file for itself, and a handle
        # that cannot defer (a test double) is written per tag as before.
        defer = hasattr(audio, "defer_save")
        if defer:
            audio.defer_save(True)
        wrote = False
        for name, value in pending.items():
            wrote = bool(audio.set_tag(name, value)) or wrote
        if defer:
            # A failed flush means nothing landed; never report a write.
            wrote = bool(audio.flush()) and wrote
            audio.defer_save(False)
        return wrote
    except Exception:
        return False


# ----------------------------------------------------------------------
# Script 16 — Mood & Energy detection
# ----------------------------------------------------------------------
def _track_paths(config):
    """Every track this run covers: the run's targets, else the whole library.

    LIB_AUDIO_EXTS (not the audio-only AUDIO_EXTS script 12 walks): a music
    video is a first-class library track and carries MOOD/ENERGY too —
    apply_mood_tags writes both through the video tag writer.
    """
    config = config or {}
    folder = str(config.get("music_folder") or "")
    if config.get("targets") is not None:
        return sorted(_collect_targets(config["targets"], LIB_AUDIO_EXTS))
    if not os.path.isdir(folder):
        return []
    return sorted(_walk_files(folder, LIB_AUDIO_EXTS))


def needs_mood(path, config, force=False):
    """True when *path* still needs a verdict (or the run forces one).

    The same short-circuit script 8 uses: the librosa decode is the expensive
    part, so an already-tagged track is left alone — except one that carries
    MOOD but predates ENERGY, which is analysed once more to backfill it
    (only while ENERGY writes are still on for its filetype).
    """
    if force:
        return True
    try:
        af = AudioFile(path)
        if not str(af.get_tag("MOOD") or "").strip():
            return True
        if should_write_audio_tag(config, "ENERGY", filepath=path):
            return not str(af.get_tag("ENERGY") or "").strip()
        return False
    except Exception:
        return True


def _apply_one(path, config):
    """(changed, error) for one track — one handle, one classification."""
    try:
        af = AudioFile(path)
        genre = str(af.get_tag("GENRE") or "").strip()
        return bool(apply_mood_tags(af, path, config, genre=genre)), None
    except Exception as e:          # unreadable container: report, keep going
        return False, str(e)


def run_detect_mood_energy(config):
    """Script 16 — MOOD + ENERGY for every track, from its own audio.

    Script 8 derives both while it is already open for the advisory /
    instrumental / genre pass; this is the same classifier on its own, for a
    library that only wants the mood work (a genre rewritten since, a changed
    mood_source, a backfill after ENERGY was added). Same gates, same tags,
    same outcome as script 8's stage — the difference is only the scope.
    """
    config = config or {}
    stats = new_stats()
    if not config.get("mood_enabled", True):
        print_header("Mood & Energy (skipped - disabled in settings)")
        return stats

    print_header("Mood & Energy Detection")
    log(f"music folder: {config.get('music_folder') or ''}")

    version = _ensure_librosa()
    if not version:
        log(c("ERROR: librosa is not installed. Use Dependencies to install it.",
              Color.RED))
        stats["error_count"] += 1
        stats["errors"].append(("librosa", "not installed"))
        return stats

    force = bool(config.get("force_mood", False))
    log(f"librosa v{version} · source={config.get('mood_source') or 'hybrid'}")

    paths = [p for p in _track_paths(config) if needs_mood(p, config, force)]
    if not paths:
        log("Nothing to analyse (every track already carries MOOD/ENERGY).")
        return stats

    workers = worker_count(config, default=4, maximum=8, items=len(paths))
    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(len(paths), "Mood & Energy", unit="file")

    def _finish(path, changed, err):
        stats["total_scanned"] += 1
        if err is not None:
            stats["error_count"] += 1
            stats["errors"].append((os.path.basename(path), err))
            _pbar_update(pbar, counts, kind="fail")
        elif changed:
            stats["modified_count"] += 1
            _pbar_update(pbar, counts, kind="ok")
        else:
            # classify() returns nothing for an undecodable or too-short
            # track, and nothing when the tags already say the same thing —
            # both are a skip, not a failure.
            stats["skipped_count"] += 1
            _pbar_skip(pbar, counts)

    try:
        if len(paths) == 1 or workers == 1:
            for path in paths:
                _finish(path, *_apply_one(path, config))
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(_apply_one, p, config): p for p in paths}
                for fut in as_completed(futures):
                    _finish(futures[fut], *fut.result())
    finally:
        if pbar:
            pbar.close()

    log(c(f"mood & energy: {stats['modified_count']} written · "
          f"{stats['skipped_count']} skipped · "
          f"{stats['error_count']} failed",
          Color.GREEN if not stats["error_count"] else Color.YELLOW))
    return stats
