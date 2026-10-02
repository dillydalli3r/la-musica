"""la musica core package.

Modules:
    paths       filesystem locations and constants
    deps        optional third-party feature detection (mutagen / Pillow / tqdm)
    subproc     subprocess wrapper (no console flashes, timeouts)
    fetchdeps   external-tool installer (.dependencies GitHub builds)
    config      config.json load/save and defaults
    ui          console output helpers
    stats       run statistics, byte accounting, progress shims, walkers
    report      human-readable result reports
    tools       .dependencies encoder auto-detection
    containers  FLAC/JXL/JPEG/PNG metadata tag readers and writers
    audio       unified multi-format tag abstraction (AudioFile)
    naming      Picard-style naming-script evaluator
    lyrics      lyrics formatting + MEDIA/SOURCE normalization
    lyrics_fetch LRCLIB lyrics download (script 13)
    cue         CUE sheet formatter
    discs       multi-CD mapping, CD-N naming and per-disc log scoring
    flac        lossless FLAC re-encoding
    images      image optimization (JXL / lossless / reverse)
    remux       video remux to MKV — audio to FLAC, subtitles copied (script 11)
    loudness    Dynamic Range + ReplayGain tags (script 7)
    audiometa   key + BPM analysis via librosa (script 12)
    autotag     advisory / instrumental tagging (script 8)
    accurip     AccurateRip .accurip generation + verification (script 9)
    format_all  final canonical pass, embedded cover policy (script 10)
    grader      per-album compliance grading
    artistdata  artist artwork + descriptions stored inside the library (script 19)
    layout      read-only library-layout report, stored under .mlo/data (script 20)
    audit       audio integrity auditing via the AudioAuditor CLI
    cli         interactive console menu

Audio is kept in `library_codec` — the codec the library is converted to —
and converted by mlo.flac (script 3), which never re-encodes a lossy source
into a lossless one: that can only lose quality. An `ENCODER_QUALITY` tag —
the LEVEL the file was encoded at, per format, see `encoder_tags` — is written
to every processed file so re-runs can skip the finished ones; the
`ENCODER_PROGRAM` / `ENCODER_VERSION` markers exist per format but are OFF by
default, because comparing the encoder's own version re-encoded the library
after every tool upgrade (R329).
"""
from __future__ import annotations

import importlib

__version__ = "4.9.2"

# The engine's entry points and config helpers, re-exported LAZILY (PEP 562).
#
# Importing the whole engine here made `from mlo import __version__` — which
# `server.main` does before it can serve anything — pull in grader, images,
# flac, autotag, audit, layout, loudness and their libraries (mutagen, Pillow,
# numpy) at process start, for a server whose first paint needs none of them.
# A re-export is a NAME, not a promise to have loaded its module: each one is
# imported on first use, then memoized in the module globals exactly like an
# eager import would have been.
_LAZY = {
    "DEFAULT_CONFIG": "config",
    "load_config": "config",
    "save_config": "config",
    "run_optimize_artist_images": "artistdata",
    "run_auto_tagging": "autotag",
    "run_format_cues": "cue",
    "run_optimize_flacs": "flac",
    "run_grade_library": "grader",
    "run_process_images": "images",
    "run_optimize_layout": "layout",
    "run_calc_dr_replaygain": "loudness",
    "run_format_lyrics": "lyrics",
    "run_audit_library": "audit",
    "run_format_all": "format_all",
}

__all__ = ["__version__", *_LAZY]


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f".{module}", __name__), name)
    globals()[name] = value          # a real re-export, memoized like one
    return value


def __dir__():
    return sorted(__all__)







