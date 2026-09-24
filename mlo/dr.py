"""Dynamic range measured in-process, the dr.loudness-war.info way.

Script 7 writes DYNAMIC RANGE (per track) and ALBUM DYNAMIC RANGE (per album).
The measurement itself used to come from a downloaded copy of simple-dr-meter:
a second Python program the app fetched into .dependencies, ran through
whatever interpreter it could find and parsed a log file out of — and on an
install whose interpreter had no numpy (a Docker image, any frozen build) it
died on its first import, so every album came back skipped while the app still
showed the feature. The arithmetic is a page long, so it lives here instead:
ffmpeg decodes the track to float PCM, numpy does the block math, and nothing
outside this process is involved.

The formulas are that meter's, unchanged — it is what the DR rating of a
release is measured with, so a second opinion is a different number:

  * the track is decoded to 44.1 kHz float PCM, because the official meter
    measures at 44.1 kHz (a 96 kHz master has to come down to it or the values
    are not comparable);
  * sample blocks of ``3 * sample_rate`` samples, the last one possibly short;
  * per block and channel: ``peak = max|x|`` and
    ``rms = sqrt(2 * sum(x^2) / samples)`` (the 2x is the meter's sine-RMS
    factor);
  * fewer than two blocks is NO dynamic range — a silent or very short track,
    never a track of DR 0;
  * the second-highest block peak per channel against the loudest 20% of the
    blocks (at least one) combined by RMS:
    ``dr = -20 * log10(rms_top / second_peak)``;
  * the channels' values are averaged, and only ``0 < dr < 40`` is reported;
  * the album value is the mean of its tracks' values, rounded.

numpy is a declared dependency of the server (see server/requirements.txt); it
is imported here at module level because the block math is numpy's, and a build
without it says so as the reason instead of measuring nothing quietly.
"""
import json
import math
import os
import subprocess
from typing import NamedTuple

try:
    import numpy as np
except ImportError:  # pragma: no cover - the dependency ships with the app
    np = None

from .subproc import run_tool, run_tool_stream

# What a build without numpy reports, per album and once per run, instead of
# skipping every track in silence.
NUMPY_REASON = "dynamic range needs numpy, which this install is missing"

# The meter's block length, and the rate it measures at: both are part of the
# convention the numbers are quoted under, not tuning knobs.
BLOCK_SECONDS = 3
MEASURE_SAMPLE_RATE = 44100

# A decode reads a whole track through ffmpeg, so it gets a much longer
# deadline than the metadata calls run_tool makes: 30 minutes covers a long mix
# on a slow machine. run_tool_stream itself has no default deadline.
DECODE_TIMEOUT = 30 * 60
# ffprobe answers from the header; a hung network mount is the only way past
# this, and then the album reports the file instead of hanging on it.
PROBE_TIMEOUT = 60

# Bytes per sample of pcm_f32le.
_SAMPLE_BYTES = 4


class TrackDR(NamedTuple):
    """One track's measurement: the value, or the reason there is none.

    ``failed`` separates "the meter has no DR for this file" (silent, shorter
    than two blocks) from "this file could not be measured at all" (no audio
    stream, a decode that failed) — only the second is an error the run has to
    report.
    """
    dr: int | None
    reason: str      # "" when dr is not None
    failed: bool


def have_numpy():
    """Whether the numpy the block math needs is importable here."""
    return np is not None


def measure_track(path, ffmpeg_exe, *, block_seconds=BLOCK_SECONDS,
                  sample_rate=MEASURE_SAMPLE_RATE, channels=0):
    """The track's DR (the loudness-war value), or None.

    None means the meter has no value for the file at all — silent, shorter
    than two blocks, undecodable, or no numpy. measure_track_detailed() says
    which, which is what the caller logs.
    """
    return measure_track_detailed(path, ffmpeg_exe, block_seconds=block_seconds,
                                  sample_rate=sample_rate,
                                  channels=channels).dr


def measure_track_detailed(path, ffmpeg_exe, *, block_seconds=BLOCK_SECONDS,
                           sample_rate=MEASURE_SAMPLE_RATE, channels=0):
    """measure_track(), plus why there is no value when there is none.

    *channels* is the channel count the caller already knows (an open handle's
    own stream info — see :func:`handle_channels`); 0 asks for it to be probed
    with ffprobe, which is one process spawn per track. The batch pass holds
    the handle already, so it passes the number and pays for the probe only
    when the container states none.
    """
    if np is None:
        return TrackDR(None, NUMPY_REASON, True)

    channels = int(channels or 0)
    if channels < 1:
        channels, why = probe_channels(path, ffmpeg_exe)
        if not channels:
            return TrackDR(None, why, True)

    samples_per_block = max(1, int(block_seconds * sample_rate))
    block_bytes = samples_per_block * _SAMPLE_BYTES * channels
    argv = _decode_argv(path, ffmpeg_exe, sample_rate)

    peaks = []
    rms_values = []
    pending = b""
    try:
        for chunk in run_tool_stream(argv, timeout=DECODE_TIMEOUT):
            pending += chunk
            while len(pending) >= block_bytes:
                block, pending = pending[:block_bytes], pending[block_bytes:]
                peaks.append(_block_peak(block, channels))
                rms_values.append(_block_rms(block, channels))
        if pending:
            # The last block of a track is nearly always short, and the meter
            # counts it: only a missing block is missing.
            usable = len(pending) - len(pending) % (_SAMPLE_BYTES * channels)
            if usable:
                peaks.append(_block_peak(pending[:usable], channels))
                rms_values.append(_block_rms(pending[:usable], channels))
    except subprocess.TimeoutExpired:
        return TrackDR(None, f"ffmpeg did not finish within "
                             f"{DECODE_TIMEOUT // 60} minutes", True)
    except subprocess.CalledProcessError as e:
        return TrackDR(None, f"ffmpeg could not decode it: {_tail(e.stderr)}",
                       True)
    except OSError as e:
        return TrackDR(None, f"ffmpeg could not be started: {e}", True)

    value, why = value_from_blocks(np.asarray(peaks, dtype="<f4"),
                                   np.asarray(rms_values, dtype="<f4"))
    # The file WAS measured; a value that came back empty is a silent or very
    # short track, which is a skip and not a failure (see probe/decode above).
    return TrackDR(value, why, False)


def value_from_blocks(peaks, rms):
    """(dr, reason) from the per-block (block, channel) float32 metrics.

    The metrics are stored as float32 exactly as the meter stores them, and the
    steps below are its steps in its order: partition the peaks at the second
    highest, take the loudest 20% of the blocks, combine those by RMS, compare.
    """
    block_count = peaks.shape[0]
    if block_count < 2:
        return None, "silent, or shorter than two 3-second blocks - no DR"

    second_peak = np.partition(peaks, block_count - 2,
                               axis=0)[block_count - 2, :]

    # The loudest 20% of the blocks, at least one — the meter's percentile.
    rms_count = max(1, int(math.floor(block_count * 0.2)))
    loudest = np.partition(rms, block_count - rms_count,
                           axis=0)[block_count - rms_count:, :]
    rms_top = np.sqrt(np.sum(loudest ** 2, axis=0) / rms_count)

    # errstate: a silent track divides by its own zero peak and logs 0/0. The
    # nan that comes out is exactly what "no measurable signal" means below, so
    # the arithmetic is unchanged and the run does not print a numpy warning
    # for every silent track it meets.
    with np.errstate(divide="ignore", invalid="ignore"):
        dr = float(np.mean(-decibel(rms_top / second_peak), axis=0))
    if not math.isfinite(dr):
        return None, "no measurable signal (silent track)"
    if 0 < dr < 40:
        return int(round(dr)), ""
    return None, f"measured DR {dr:.1f} is outside the meter's 0-40 range"


def album_dr(values):
    """The album's DR: the mean of its tracks' values, rounded — or None.

    A track with no DR contributes nothing: counting it as 0 would drag the
    album's rating down for a bonus track that is silent or three seconds long.
    An album where every track came back empty has no album DR, not DR 0.
    """
    if np is None:
        return None
    valid = [int(v) for v in values if v is not None and v == v]
    if not valid:
        return None
    return int(np.round(np.mean(valid)))


def handle_channels(af):
    """The channel count an already-open handle states, else 0.

    The DR pass opens every track anyway (to read and write its tags), and
    mutagen's stream info is the app's OWN answer for "channels" — the tech
    panel reads the same field through `server.tagcache`, and both parse the
    container header. So the pass asks the handle it holds instead of spawning
    one ffprobe per track (one process per file, on a library-wide run, for a
    number the file had already handed us). A handle that states nothing — an
    unopened container, a video container whose tech came from ffprobe, a
    format mutagen has no stream info for — returns 0 and the caller probes,
    exactly as before.
    """
    try:
        if getattr(af, "is_video", False):
            return 0
        info = getattr(getattr(af, "audio", None), "info", None)
        channels = int(getattr(info, "channels", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0
    return channels if channels > 0 else 0


def probe_channels(path, ffmpeg_exe):
    """(channel count, why) for the file's first audio stream.

    The decode resamples but never remixes, so the channel count of the file is
    the layout of the PCM stream; ffprobe is asked the same question about the
    same stream that the meter asks (its audio_io.read_audio_file_metadata).
    """
    try:
        proc = run_tool(
            [_ffprobe_for(ffmpeg_exe), "-v", "error", "-print_format", "json",
             "-select_streams", "a:0", "-show_entries", "stream=channels",
             path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=PROBE_TIMEOUT,
        )
    except Exception as e:
        return 0, f"could not be probed: {e}"
    if proc.returncode != 0:
        return 0, f"has no readable audio stream ({_tail(proc.stderr)})"
    try:
        streams = (json.loads(proc.stdout or "{}") or {}).get("streams") or []
        channels = int(streams[0]["channels"]) if streams else 0
    except (ValueError, KeyError, TypeError):
        channels = 0
    if channels < 1:
        return 0, "has no audio stream"
    return channels, ""


def _ffprobe_for(ffmpeg_exe):
    """ffprobe beside ffmpeg (the installer ships the pair), else PATH."""
    folder = os.path.dirname(str(ffmpeg_exe))
    for name in ("ffprobe.exe", "ffprobe"):
        candidate = os.path.join(folder, name)
        if os.path.isfile(candidate):
            return candidate
    return "ffprobe"


def _decode_argv(path, ffmpeg_exe, sample_rate):
    """The meter's own decode: first audio stream, float32, resampled, stdout."""
    return [ffmpeg_exe, "-loglevel", "fatal", "-nostdin", "-i", path,
            "-map", "0:a:0", "-c:a", "pcm_f32le", "-ar", str(sample_rate),
            "-f", "f32le", "-"]


def _block_peak(block, channels):
    """The per-channel peak of one interleaved float32 block."""
    return np.max(np.abs(_deinterleave(block, channels)), axis=1)


def _block_rms(block, channels):
    """The per-channel RMS of one block, at the meter's 2x sine factor."""
    samples = _deinterleave(block, channels)
    sum_sqr = np.sum(samples ** 2, axis=1)
    return np.sqrt(2.0 * sum_sqr / samples.shape[1])


def _deinterleave(block, channels):
    """A (channels, samples) view of interleaved little-endian float32 PCM.

    order='F' is what de-interleaves: the file holds L R L R…, and filling the
    first axis fastest puts every L in row 0 and every R in row 1 — the same
    reshape the meter does.
    """
    samples = np.frombuffer(block, dtype="<f4")
    return samples.reshape(channels, -1, order="F")


def decibel(value):
    """20 * log10, the meter's own helper."""
    return np.log10(value) * 20


def _tail(text, limit=160):
    """The last non-empty line of a tool's output, for one log line."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line:
            return line[:limit]
    return "no output"
