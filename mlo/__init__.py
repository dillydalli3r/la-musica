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
into a lossless one: that can only lose quality. Encoder marker tags
(ENCODER_PROGRAM / QUALITY / VERSION) are written to every processed FLAC so
re-runs can skip finished files.
"""
from .config import load_config, save_config, DEFAULT_CONFIG
from .artistdata import run_optimize_artist_images
from .autotag import run_auto_tagging
from .cue import run_format_cues
from .flac import run_optimize_flacs
from .grader import run_grade_library
from .images import run_process_images
from .layout import run_optimize_layout
from .loudness import run_calc_dr_replaygain
from .lyrics import run_format_lyrics
from .audit import run_audit_library
try:
    from .format_all import run_format_all
except ImportError:
    run_format_all = None

__version__ = "3.21.1"
__all__ = [
    "load_config", "save_config", "DEFAULT_CONFIG",
    "run_auto_tagging",
    "run_format_lyrics", "run_format_cues", "run_optimize_flacs",
    "run_grade_library", "run_process_images", "run_audit_library",
    "run_calc_dr_replaygain", "run_format_all",
    "run_optimize_artist_images", "run_optimize_layout",
]







