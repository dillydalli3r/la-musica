"""la musica core package.

Modules:
    paths       filesystem locations and constants
    deps        optional third-party feature detection (mutagen / Pillow / tqdm)
    config      config.json load/save and defaults
    ui          console output helpers
    stats       run statistics, byte accounting, progress shims, walkers
    report      human-readable result reports
    tools       .dependencies encoder auto-detection
    containers  FLAC/JXL/JPEG/PNG metadata tag readers and writers
    audio       unified multi-format tag abstraction (AudioFile)
    lyrics      lyrics formatting + MEDIA/SOURCE normalization
    cue         CUE sheet formatter
    flac        lossless FLAC re-encoding
    images      image optimization (JXL / lossless / reverse)
    grader      per-album compliance grading
    audit       audio integrity auditing via the AudioAuditor CLI
    cli         interactive console menu

Only FLAC is losslessly re-encoded; all other audio formats receive safe tag
operations only. Encoder marker tags (ENCODER_PROGRAM / QUALITY / VERSION) are
written to every processed artifact so re-runs can skip finished files.
"""
from .config import load_config, save_config, DEFAULT_CONFIG
from .autotag import run_auto_tagging
from .cue import run_format_cues
from .flac import run_optimize_flacs
from .grader import run_grade_library
from .images import run_process_images
from .loudness import run_calc_dr_replaygain
from .lyrics import run_format_lyrics
from .audit import run_audit_library
try:
    from .accurip import run_generate_accurip
except ImportError:
    run_generate_accurip = None
try:
    from .format_all import run_format_all
except ImportError:
    run_format_all = None

__version__ = "2.2.1"
__all__ = [
    "load_config", "save_config", "DEFAULT_CONFIG",
    "run_auto_tagging",
    "run_format_lyrics", "run_format_cues", "run_optimize_flacs",
    "run_grade_library", "run_process_images", "run_audit_library",
    "run_calc_dr_replaygain", "run_generate_accurip", "run_format_all",
]







