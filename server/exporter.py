"""Export-to-device: copy or transcode tracks to a target drive / folder.

Powers the Export page. The user picks tracks (from a playlist or whole
albums), a destination drive, a codec and a folder structure; the exporter
either bit-copies the files (codec ``copy`` / matching FLAC) or re-encodes
them with ffmpeg and then re-writes the full tag set (plus embedded artwork
when the source carries any) so exported files are immediately usable on an
MP3 player / DAP.

Output layout options:

* ``albumartist_album_disc`` — "<Album artist>/<Album>/D-NN Title.<ext>", the
                     shipped default, spelled as the naming script
                     "%albumartist%/%album%/%discnumber%-$num(%tracknumber%,2)
                     %title%" (mlo.naming — the grammar the library's own
                     organizer writes its paths with)
* ``album``        — "<Album>/NN - Title.<ext>"
* ``flat``         — "<Artist - NN - Title>.<ext>" in one folder
* ``mirror``       — the track's relative path inside the music folder,
                     re-extensioned (keeps the library's own organization)
* ``custom``       — the structure the user typed (``structure_script``), a
                     naming script too, so the page offers one grammar

``album`` and ``flat`` prefix a multi-disc album's file name with its disc
number ("1-01 - Intro"): two discs must never collide on "01 - Intro". The
shipped ``albumartist_album_disc`` writes the library's OWN file name instead
— "1-01 Title", disc number included for a single-disc album as well, exactly
as ``mlo.naming``'s default script does — so an exported tree reads like the
library it was copied from.

Compatibility options (all defaulted from ``export_*`` config keys, all
overridable per run — see ``EXPORT_DEFAULTS``):

* ``embed_covers`` — embed the album cover into every exported file, with
  ``embed_cover_jpeg_quality`` / ``embed_cover_resolution`` controlling the
  JPEG quality and the longest-side cap of what gets embedded (the same
  knobs script 10 uses; ``mlo.format_all`` prepares the bytes).
* ``id3v2`` / ``id3v1`` — ID3 version for MP3 exports ("2.3" is what older
  players and car stereos read) and whether to also write an ID3v1 chunk.
* ``replaygain_mode`` — ``off`` does nothing, ``tags`` measures each exported
  track with ffmpeg's EBU R128 meter and writes the ReplayGain 2.0 tags (track
  + album gain/peak) so a player that honours them plays the export at the
  library's loudness, and ``apply`` bakes the same measurement into the audio
  itself with ffmpeg's ``volume`` filter, so a player that honours nothing
  still plays level. ``tags`` rides along in the transcode pass (the
  ``ebur128`` filter is a pass-through), so it costs no extra decode; ``apply``
  has to know the gain BEFORE the encoder starts and therefore measures the
  sources once up front. An applied file carries no ReplayGain tags at all —
  a player that honoured them would apply the gain twice.
* ``eq_profile`` — apply an Equalizer APO / Peace profile (a built-in preset or
  an imported one, see ``mlo.eq``) while transcoding, after the ReplayGain
  gain: the profile's preamp, then its filters in file order. ``""`` is no EQ.
* ``clean_tags`` — write only the canonical tag set on transcodes instead of
  keeping the source's leftover frames.
* ``playlists`` — write ``.m3u8`` playlists (UTF-8, relative paths) next to
  the exported albums plus one for the whole export. OFF by default: the
  album playlists are an album-export artifact, while a real playlist export
  is written by ``server.playlists.export_m3u8``.
* ``copy_files`` — WHICH files the run writes, as the family keys of
  ``FILE_FAMILIES``: the tracks themselves, the covers and artwork, the lyrics,
  the cue sheets, the rip log, the album's own description, the checksum lists,
  the text/notes/scans, the playlists the album carries, and anything else it
  holds that this app does not classify. The shipped default is the tracks
  alone; an empty selection is REFUSED (a run that copies nothing would write
  an empty folder). ``sidecars`` is the switch this replaced and still resolves
  to the set it always copied (``LEGACY_SIDECAR_FAMILIES``), so a caller of the
  old API and a config written by it behave exactly as they did.
* Every non-audio file left behind is reported in ``excluded`` (name, album,
  family and the reason it did not travel — see ``extra_files``). A file the
  run WAS asked for is copied and not reported: the audit's filter and the copy
  pass are the same predicate (``export_tracks._travels``).
* ``manifest`` — write ``checksums.sha256`` at the export root: one
  "<sha256>  <relative path>" line per exported file, the format
  ``sha256sum -c`` reads back.
* ``verify`` — re-open every written file and prove it parses with the
  source's duration before calling the export done.
* ``prune`` — sync mode: delete audio files under the export root that this
  run did not write.
* ``workers`` — parallel transcode/copy workers (0 = automatic).
* ``target`` — ``server`` writes into ``dest``/``subfolder`` (the drive picker
  on a machine that has one), ``zip`` stages the same tree under the app's own
  data dir and returns a single archive instead: a browser client cannot hand
  the server a folder on the user's computer, and a download is the one form of
  "save this to my machine" every browser has. ``dest``/``subfolder``/``prune``
  have no meaning for a zip.

Exports are idempotent: a destination that already is this source's export
(same duration and track identity) is skipped on re-runs. A run that REWROTE
the audio (``apply``, an equalizer profile) stamps the file it wrote
(``MLO_EXPORT_PROCESSING``) and compares that stamp on the next run, so
changing the curve re-encodes rather than skipping the user's new setting.
Progress is reported through ``server.job_locks.publish`` — one frame to the
in-progress row AND the header bar — so an export reads exactly like a library
script run on both surfaces. (It used to import ``mlo.stats.progress_hook`` by
value, which the server replaces after this module is imported, so the bar
stayed empty for a whole export.)
"""
import hashlib
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile

from mlo import eq as eq_mod
from mlo import lyrics as lyrics_mod
from mlo import naming
from mlo.audio import AudioFile
from mlo.naming import sanitize_path, sanitize_segment
from mlo.paths import album_sidecar_of, app_data_dir
from mlo.stats import is_audio_file, worker_count
from mlo.subproc import tool_path
from mlo.tools import detect_all_tools
from server import job_locks

# Audio file extensions the pruner considers "an exported track" (the audio
# extensions the library itself knows, plus the containers this exporter can
# produce). Used by prune only — nothing deletes a non-audio file.
EXPORT_AUDIO_EXTS = {
    ".flac", ".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".wav", ".wv",
    ".aiff", ".aif", ".wma",
}


def safe_subfolder(name):
    """The export subfolder as ONE relative folder name under the drive root.

    The value comes from a saved default and a form field, and it is joined
    onto the destination — "../.." there would write outside the device, so
    separators are neutralised and traversal is refused instead of trusted.
    The NAME itself follows the app's one rule (mlo.naming.sanitize_segment),
    so a subfolder is spelled the same way every other folder the app writes
    is."""
    text = str(name or "").replace("..", "_")[:120]
    return sanitize_segment(text) or "Music"


# Codec table. Each codec carries the quality PRESETS the UI offers (the
# single source of truth for the Export page's quality dropdown and its size
# estimate) and the ffmpeg arguments each preset expands to:
#
#   presets: [{v, label, kbps|None, args}]  — v is what the API receives back
#   custom:  {mode: "kbps"|"q", min, max, default, enc} — a plain number from
#            the UI's "custom" field, clamped, appended to enc
#   ext:     container extension produced (absent for "copy", which keeps the
#            source's own extension)
#
# kbps is a display/estimate hint (None = unpredictable: lossless or copy).
CODECS = {
    "copy": {"label": "Copy (original codec)", "presets": []},
    "flac": {
        "label": "FLAC (lossless)", "ext": ".flac",
        "presets": [
            {"v": str(lv),
             "label": "level %d%s" % (lv, {
                 8: " (smallest, slowest)", 5: " (balanced)",
                 0: " (fastest)"}.get(lv, "")),
             "kbps": None,
             "args": ["-c:a", "flac", "-compression_level", str(lv)]}
            for lv in range(8, -1, -1)
        ],
        "default": "8",
    },
    "mp3": {
        "label": "MP3", "ext": ".mp3",
        "presets": [
            {"v": "V0", "label": "V0 (~245 kbps VBR, best)", "kbps": 245,
             "args": ["-c:a", "libmp3lame", "-q:a", "0"]},
            {"v": "V1", "label": "V1 (~225 kbps VBR)", "kbps": 225,
             "args": ["-c:a", "libmp3lame", "-q:a", "1"]},
            {"v": "V2", "label": "V2 (~175 kbps VBR)", "kbps": 175,
             "args": ["-c:a", "libmp3lame", "-q:a", "2"]},
            {"v": "V3", "label": "V3 (~155 kbps VBR)", "kbps": 155,
             "args": ["-c:a", "libmp3lame", "-q:a", "3"]},
            {"v": "V4", "label": "V4 (~135 kbps VBR)", "kbps": 135,
             "args": ["-c:a", "libmp3lame", "-q:a", "4"]},
            {"v": "V5", "label": "V5 (~115 kbps VBR)", "kbps": 115,
             "args": ["-c:a", "libmp3lame", "-q:a", "5"]},
            {"v": "320", "label": "320 kbps CBR", "kbps": 320,
             "args": ["-c:a", "libmp3lame", "-b:a", "320k"]},
            {"v": "256", "label": "256 kbps CBR", "kbps": 256,
             "args": ["-c:a", "libmp3lame", "-b:a", "256k"]},
            {"v": "192", "label": "192 kbps CBR", "kbps": 192,
             "args": ["-c:a", "libmp3lame", "-b:a", "192k"]},
            {"v": "128", "label": "128 kbps CBR", "kbps": 128,
             "args": ["-c:a", "libmp3lame", "-b:a", "128k"]},
        ],
        "default": "V2",
        "custom": {"mode": "kbps", "min": 32, "max": 320, "default": 192,
                   "enc": ["-c:a", "libmp3lame"]},
    },
    "aac": {
        "label": "AAC / M4A", "ext": ".m4a",
        "presets": [
            {"v": str(b), "label": f"{b} kbps", "kbps": b,
             "args": ["-c:a", "aac", "-b:a", f"{b}k"]}
            for b in (320, 256, 192, 128)
        ],
        "default": "256",
        "custom": {"mode": "kbps", "min": 32, "max": 512, "default": 256,
                   "enc": ["-c:a", "aac"]},
    },
    "opus": {
        "label": "Opus", "ext": ".opus",
        "presets": [
            {"v": str(b), "label": f"{b} kbps", "kbps": b,
             "args": ["-c:a", "libopus", "-b:a", f"{b}k"]}
            for b in (320, 256, 224, 192, 160, 128, 112, 96, 80, 64)
        ],
        "default": "160",
        "custom": {"mode": "kbps", "min": 16, "max": 510, "default": 160,
                   "enc": ["-c:a", "libopus"]},
    },
    "vorbis": {
        "label": "Ogg Vorbis", "ext": ".ogg",
        "presets": [
            # kbps hints are the usual measured averages of libvorbis q0-q10
            {"v": f"q{q}", "label": "q%d%s" % (q, {
                10: " (~320 kbps, best)", 0: " (~64 kbps, smallest)"}.get(q, "")),
             "kbps": {10: 320, 9: 280, 8: 256, 7: 224, 6: 192, 5: 160,
                      4: 128, 3: 112, 2: 96, 1: 80, 0: 64}[q],
             "args": ["-c:a", "libvorbis", "-q:a", str(q)]}
            for q in range(10, -1, -1)
        ],
        "default": "q6",
        "custom": {"mode": "q", "min": 0, "max": 10, "default": 6,
                   "enc": ["-c:a", "libvorbis"]},
    },
    "wav": {
        "label": "WAV (PCM, uncompressed)", "ext": ".wav",
        "presets": [
            {"v": "24", "label": "24-bit", "kbps": 2117,
             "args": ["-c:a", "pcm_s24le"]},
            {"v": "16", "label": "16-bit (CD)", "kbps": 1411,
             "args": ["-c:a", "pcm_s16le"]},
        ],
        "default": "16",
    },
    "aiff": {
        "label": "AIFF (PCM, uncompressed)", "ext": ".aiff",
        "presets": [
            {"v": "24", "label": "24-bit", "kbps": 2117,
             "args": ["-c:a", "pcm_s24be"]},
            {"v": "16", "label": "16-bit (CD)", "kbps": 1411,
             "args": ["-c:a", "pcm_s16be"]},
        ],
        "default": "16",
    },
    "alac": {
        "label": "ALAC / Apple lossless", "ext": ".m4a",
        "presets": [],
        "args": ["-c:a", "alac"],
    },
    "wavpack": {
        "label": "WavPack (lossless)", "ext": ".wv",
        "presets": [
            {"v": str(lv),
             "label": "level %d%s" % (lv, {
                 8: " (smallest, slowest)", 4: " (balanced)",
                 0: " (fastest)"}.get(lv, "")),
             "kbps": None,
             "args": ["-c:a", "wavpack", "-compression_level", str(lv)]}
            for lv in range(8, -1, -1)
        ],
        "default": "4",
    },
    "wma": {
        "label": "WMA (Windows Media)", "ext": ".wma",
        "presets": [
            {"v": str(b), "label": f"{b} kbps", "kbps": b,
             "args": ["-c:a", "wmav2", "-b:a", f"{b}k"]}
            for b in (192, 160, 128, 96)
        ],
        "default": "192",
        "custom": {"mode": "kbps", "min": 32, "max": 320, "default": 192,
                   "enc": ["-c:a", "wmav2"]},
    },
}

# Lossless codecs whose output size tracks the decoded size, with the factor
# each typically reaches (used by the drive-fit estimate).
_LOSSLESS_FACTORS = {"flac": 0.62, "alac": 0.70, "wavpack": 0.60}
# Assumed decoded rate when a source does not report its own sample format.
_PCM_KBPS_FALLBACK = 1411.0

# What an export writes, as ONE table: the file families this exporter can see
# beside a selection's tracks, in the order the Export page offers them.
# ``audio`` is the tracks themselves — the family an export exists for — and
# every other key is a family of NON-audio siblings. Which family a concrete
# file belongs to is decided by ONE classifier (``_extra_kind``, over the
# extension table ``_EXTRA_REASONS``, plus the cover names and the artist image
# it knows by NAME), and this table is what the menu, the run's copy pass and
# the run's own report all read: what the page offers, what a run copies and
# what a run says it left behind can never disagree. The label and the hint are
# served to the page (``file_families``), so the menu cannot offer a family a
# run would refuse either.
FILE_FAMILIES = {
    "audio": {
        "label": "The tracks themselves",
        "hint": "The audio files, as the codec and the quality above write them.",
    },
    "cover": {
        "label": "Covers and artwork",
        "hint": "The album's cover.* and the artist image, copied as files. They "
                "travel EMBEDDED in each exported file anyway (see above).",
    },
    "lyrics": {
        "label": "Lyrics (.lrc)",
        "hint": "The exported tracks' own .lrc files. The Lyrics option above is "
                "what writes lyrics FOR the exported files.",
    },
    "cue": {
        "label": "Cue sheets (.cue)",
        "hint": "The rip's track layout. Copied as it is; the run repoints its "
                "FILE lines at the files it wrote.",
    },
    "log": {
        "label": "Rip log (.log)",
        "hint": "The ripper's own log — the file the audit reads for the disc's "
                "SHA256 and the per-track CRCs.",
    },
    "accurip": {
        "label": "AccurateRip report (.accurip)",
        "hint": "The CUETools AccurateRip evidence the audit verifies — the "
                "pressing's own verdict, independent of the rip log.",
    },
    "description": {
        "label": "Album description (description.txt)",
        "hint": "The album's own description, the file this app writes and "
                "reads — numbered copies of it included.",
    },
    "checksum": {
        "label": "Checksum lists (.md5, .sfv, .ffp, .torrent)",
        "hint": "The checksum and fingerprint lists a rip ships with, and its "
                "torrent file.",
    },
    "text": {
        "label": "Notes, links and scans (.txt, .nfo, .url, .pdf)",
        "hint": "The ripper's release notes, a link file, a booklet or a scan.",
    },
    "playlist": {
        "label": "Playlists the album carries (.m3u, .m3u8, .pls, .wpl)",
        "hint": "Copied as they are — they name the LIBRARY's own files. “Write "
                ".m3u8 playlists” below writes new ones for the export instead.",
    },
    "other": {
        "label": "Anything else (a file this app does not recognise)",
        "hint": "Thumbs.db, a .bak, an image that is not the cover. A stray "
                "SUBFOLDER is reported in the run's leftovers but never walked "
                "or copied.",
    },
}

# The family set the ``sidecars`` switch stood for: the exporter mirrored
# cover.*/description.txt/artist image/.lrc/.cue/.log beside the exported audio,
# and nothing else. A caller that still sends that boolean — or a config that
# still holds ``export_sidecars`` — gets exactly this set, so the switch the
# file selection replaced keeps meaning what it always meant. `accurip` is in
# it because the legacy set's `log` covered BOTH files before they had a family
# each: dropping it would quietly stop copying .accurip for those callers.
LEGACY_SIDECAR_FAMILIES = ("audio", "cover", "lyrics", "cue", "log", "accurip",
                           "description")

# Run options and their defaults. ``export_<name>`` in the config holds the
# saved default for each; a per-run value (the Export page's form) wins.
EXPORT_DEFAULTS = {
    "embed_covers": True,
    "embed_cover_jpeg_quality": 90,
    "embed_cover_resolution": 1200,
    "id3v2": "2.3",
    "id3v1": False,
    "replaygain_mode": "off",
    "eq_profile": "",
    "clean_tags": True,
    # An export is a copy service for AUDIO. By default it writes neither the
    # album's own files (.m3u8/cover.jpg/description.txt) nor the rip's
    # evidence (.cue/.log/.accurip/.lrc): the cover travels EMBEDDED in each
    # file (embed_covers), and the rest stays in the library where the audit,
    # the grading and the rip's checksum read it. Both switches still exist for
    # a device that wants them, and both are off unless asked for. Whatever the
    # run leaves behind is reported in ``excluded`` (see extra_files).
    "playlists": False,
    "sidecars": False,
    # WHICH files a run writes, as the keys of FILE_FAMILIES above.
    # `copy_files` is the one option that does NOT resolve through ``option``:
    # ``copy_files()`` below reads it, and a selection nobody made falls back to
    # the `sidecars` switch it replaced BEFORE it falls back to this value —
    # which is what a caller that sends nothing new gets, i.e. today's
    # behaviour: the tracks alone.
    "copy_files": ["audio"],
    "manifest": False,
    "verify": True,
    "prune": False,
    "workers": 0,
    "target": "server",
    # How lyrics travel: the LYRICS tag ("embedded"), a `.lrc` beside the
    # exported file ("lrc"), or both. "" — the shipped value — follows the
    # LIBRARY's own `lyrics_format`, so an export defaults to what the app
    # keeps in the library instead of to an opinion of its own (see
    # lyrics_mode below).
    "lyrics": "",
}

# The parts of a run that are POSITIONAL: they say WHAT is exported and where
# it lands, and each has its own parameter on ``export_tracks`` below.
# Everything else a caller may send is an option with a default
# (``EXPORT_DEFAULTS``). The endpoint reads this to split a posted form into
# options and positionals, and the saved-config store reads it to know exactly
# which keys of the form a config snapshots — one tuple, so neither can know
# half the form.
FORM_FIELDS = ("paths", "dest", "subfolder", "codec", "quality",
               "structure", "structure_script")

# ReplayGain modes. "tags" is the tag-writing behaviour the export shipped with
# (a player applies it), "apply" bakes the same gain into the samples (a player
# that honours nothing still plays level).
REPLAYGAIN_MODES = ("off", "tags", "apply")

# How an export can write lyrics: keep the LYRICS tag ("embedded"), write a
# `.lrc` beside the exported file ("lrc"), or both. The tag names are the
# source's own — an exported file must not carry a lyrics tag the run decided
# to write as a file instead, or a player shows a second, stale copy.
LYRICS_MODES = ("embedded", "lrc", "both")
_LYRICS_TAGS = ("LYRICS", "UNSYNCEDLYRICS", "SYNCLYRICS")

# Where an export can go, as one table: `server` writes into dest/subfolder on
# the machine running this app, `zip` stages the same export and hands back one
# archive. Read by the endpoint that validates a request, by the run itself and
# by the saved-config store, so the three cannot disagree about what a target is.
TARGETS = ("server", "zip")

# ---- cancelling a run ---------------------------------------------------- #
# One export runs at a time (the route holds a job), and the owner's report is
# that a long export to a slow drive had no way to stop: the request blocked
# until every file was written. `POST /api/export/cancel` sets this event; the
# per-file pass checks it before it writes anything more, so a run stops at a
# file boundary — never mid-file, which would leave a half-written audio file
# on the destination.
#
# What is already written STAYS: an export is a copy service, and deleting a
# finished file because the user stopped the run would be the app destroying
# data it just produced. The run reports itself cancelled with the counts, and
# the destination is exactly the files it managed to write.
_CANCEL = threading.Event()


def request_cancel() -> bool:
    """Ask the running export to stop. True when one was in flight.

    "In flight" is read off the job registry rather than a flag of this module:
    the route that runs an export registers an `export` job for its whole call,
    so the registry already knows — and a press between two exports cannot arm
    the NEXT run. False means nothing was running, which the caller reports as
    "nothing to cancel" instead of pretending it stopped something."""
    busy = any(j.get("kind") == "export" for j in job_locks.jobs())
    if not busy:
        return False
    _CANCEL.set()
    return True


def cancel_requested() -> bool:
    """Whether the CURRENT run was asked to stop (read by the pass)."""
    return _CANCEL.is_set()

# The one thing a processing run needs that the copy codec cannot give it. The
# API hands this exact text to the UI, so there is one wording for it.
_PROCESSING_NEEDS_CODEC = (
    "'Copy (original codec)' cannot rewrite the audio — ReplayGain \"apply\" and "
    "an equalizer profile both filter the samples, which needs a real codec "
    "(FLAC, ALAC, MP3, AAC, Opus, Vorbis, WAV, AIFF, WavPack or WMA)")

# Tag recording WHAT audio processing an exported file went through. The audio
# itself is the only place that answer lives: a processed export has the same
# duration, the same tags and the same track identity as an unprocessed one, so
# without this a re-run with a different equalizer curve would be skipped as
# "already exported" and the user's new curve would never reach the device.
# Players ignore an unknown tag.
_PROCESSING_TAG = "MLO_EXPORT_PROCESSING"

# The EBU R128 measurement, as one filter chain: ebur128 is a pass-through
# analysis filter and astats rides along for a full-precision copy of the peak
# (ebur128's summary rounds it to 0.1 dBFS). peak=sample, not peak=true: rsgain
# writes sample peaks into REPLAYGAIN_*_PEAK, and an export carrying a true
# peak disagreed with the tag script 7 puts on the same audio by up to 30%.
_RG_FILTER = "ebur128=peak=sample,astats=measure_overall=Peak_level"

# The four tags ReplayGain lives in. An applied export must carry NONE of them
# (its gain is in the samples), so this is also the list a processing run drops.
_RG_TAGS = ("REPLAYGAIN_TRACK_GAIN", "REPLAYGAIN_TRACK_PEAK",
            "REPLAYGAIN_ALBUM_GAIN", "REPLAYGAIN_ALBUM_PEAK")

_DRIVETYPE = {2: "removable", 3: "fixed", 4: "network", 5: "optical"}


def codec_specs():
    """Public codec table for the UI: label, produced extension, ordered
    quality presets and the custom-value range. The ffmpeg arguments stay
    here — the page needs the choices, not the command line."""
    out = {}
    for name, spec in CODECS.items():
        out[name] = {
            "label": spec.get("label", name),
            "ext": spec.get("ext"),
            "presets": [{"v": p["v"], "label": p["label"], "kbps": p.get("kbps")}
                        for p in spec.get("presets", [])],
            "custom": ({k: v for k, v in spec["custom"].items() if k != "enc"}
                       if spec.get("custom") else None),
            "default": _default_quality(name),
        }
    return out


def list_drives():
    """Windows drive list with free space and bus type. On non-Windows
    systems a single "/" root is reported so the UI stays functional."""
    drives = []
    if os.name != "nt":
        try:
            u = shutil.disk_usage("/")
            drives.append({"letter": "/", "root": "/", "free": u.free,
                           "total": u.total, "type": "fixed"})
        except OSError:
            pass
        return drives
    import ctypes
    k32 = ctypes.windll.kernel32
    for n in range(ord("A"), ord("Z") + 1):
        letter = chr(n)
        root = f"{letter}:\\"
        dtype = k32.GetDriveTypeW(ctypes.c_wchar_p(root))
        if dtype not in _DRIVETYPE:
            continue  # DRIVE_UNKNOWN / DRIVE_NO_ROOT_DIR
        entry = {"letter": f"{letter}:", "root": root,
                 "type": _DRIVETYPE[dtype], "free": None, "total": None}
        if os.path.exists(root):
            try:
                u = shutil.disk_usage(root)
                entry["free"], entry["total"] = u.free, u.total
            except OSError:
                pass  # drive present but unreadable (empty card reader…)
            drives.append(entry)
    return drives


def option(cfg, opts, name):
    """One run option: the per-run value, else the saved ``export_<name>``
    config value, else the built-in default."""
    if opts:
        value = opts.get(name)
        if value is not None and value != "":
            return value
    value = (cfg or {}).get("export_" + name)
    return EXPORT_DEFAULTS[name] if value is None or value == "" else value


def lyrics_mode(cfg, opts):
    """This run's lyric handling: the per-run value, else the saved
    `export_lyrics`, else the LIBRARY's own `lyrics_format`.

    "" is not a fourth mode — it is "whatever the library keeps", so an export
    made by someone who never touched either setting writes lyrics the way the
    library itself does (mlo.lyrics' own format pass), and changing one setting
    cannot silently disagree with the other. An unreadable value falls back the
    same way rather than reaching the writer as an unknown mode.
    """
    value = str(option(cfg, opts, "lyrics") or "").strip().lower()
    if value in LYRICS_MODES:
        return value
    library = str((cfg or {}).get("lyrics_format") or "").strip().lower()
    return library if library in LYRICS_MODES else "embedded"


def _family_keys(value):
    """The family keys a caller's value names, in the order it named them.

    A value is a list/tuple of keys, or a comma/semicolon-separated string: the
    form posts the list, and config.json is hand-editable, so both shapes reach
    here. Unknown keys are KEPT (``copy_files_error`` is what refuses them, with
    a sentence the caller shows) and duplicates dropped."""
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    if not isinstance(value, (list, tuple, set)):
        return []
    out = []
    for item in value:
        key = str(item or "").strip().lower()
        if key and key not in out:
            out.append(key)
    return out


def copy_files(cfg, opts):
    """THE file selection this run writes: a tuple of ``FILE_FAMILIES`` keys, in
    the table's own order.

    The per-run selection wins, then the saved ``export_copy_files``, then the
    ``sidecars`` switch this replaced — per-run OR saved (``option`` reads
    both), so a caller that still sends the boolean gets the set it asked for
    even though the exporter no longer has a switch of its own. A run nobody
    asked anything of writes the tracks alone, which is the behaviour an export
    has always had.

    A selection that is merely absent is NOT an empty one: ``[]`` is a caller
    that selected no files at all, and that is refused (``copy_files_error``)
    before anything is written. Unknown keys survive to that same refusal — this
    resolver answers "what WOULD this run write", and the answer for a family
    the exporter does not have is "nothing", which is not what the caller meant
    and must not be answered silently."""
    chosen = _family_keys((opts or {}).get("copy_files")) \
        or _family_keys((cfg or {}).get("export_copy_files"))
    if chosen:
        return tuple(key for key in FILE_FAMILIES if key in set(chosen))
    if option(cfg, opts, "sidecars"):
        return LEGACY_SIDECAR_FAMILIES
    return ("audio",)


def copy_files_error(value):
    """The sentence that refuses a file selection, or "" when it is usable.

    ``value`` is what a caller SENT (the run option, or a saved config's field).
    Refused: a key that is not one of ``FILE_FAMILIES`` — the page's menu is
    served from that table, so this is a hand-made request or a config from
    another build — and an EMPTY selection, which would create the destination
    and write an empty tree while reporting success. Both are refused BEFORE
    the destination is touched, so a refused run leaves nothing behind."""
    keys = _family_keys(value)
    unknown = [key for key in keys if key not in FILE_FAMILIES]
    if unknown:
        return ("unknown file family(ies): " + ", ".join(unknown)
                + " — the families are " + ", ".join(FILE_FAMILIES))
    if not keys:
        return ("no files selected — a run that copies nothing would write an "
                "empty folder; pick at least one of %s" % ", ".join(FILE_FAMILIES))
    return ""


def option_int(cfg, opts, name, low=0, high=None):
    """An integer run option, clamped (bad input falls back to the default)."""
    try:
        value = int(option(cfg, opts, name))
    except (TypeError, ValueError):
        value = int(EXPORT_DEFAULTS[name])
    value = max(low, value)
    return min(high, value) if high is not None else value


def _replaygain_mode(cfg, opts):
    """The run's ReplayGain mode: "off", "tags" or "apply".

    A caller that still passes the boolean this option used to be gets the mode
    it stood for (True = write the tags, False = do nothing); the stored config
    key is migrated in mlo.config, so this is only the direct-call case. An
    unrecognized value falls back to "off" rather than guessing at what a typo
    meant.
    """
    if opts and "replaygain_mode" not in opts and "replaygain" in opts:
        return "tags" if opts.get("replaygain") else "off"
    mode = str(option(cfg, opts, "replaygain_mode") or "off").strip().lower()
    return mode if mode in REPLAYGAIN_MODES else "off"


def _default_quality(codec):
    """The quality value a codec starts on (first preset / "default")."""
    spec = CODECS.get(codec) or {}
    presets = spec.get("presets") or []
    wanted = spec.get("default")
    for p in presets:
        if p["v"] == wanted:
            return wanted
    return presets[0]["v"] if presets else ""


def _codec_args(codec, quality):
    """ffmpeg output arguments for the requested codec/quality pair.

    Quality is a preset key from ``CODECS`` (V0/320/q8/…) or, for the codecs
    that offer it, a plain number — kbps for the CBR codecs, plain 0-10 for
    Vorbis — which the UI's custom field sends."""
    spec = CODECS.get(codec)
    if spec is None:
        raise ValueError(f"unknown codec: {codec}")
    if codec == "copy":
        return []
    quality = str(quality or "").strip()
    presets = spec.get("presets") or []
    chosen = next((p for p in presets if p["v"] == quality), None)
    if chosen is None:
        # Not a preset key: "<n>" kbps for the CBR codecs, plain n for
        # Vorbis q — clamped, so a typo cannot produce garbage arguments.
        custom = spec.get("custom")
        if custom:
            try:
                n = int(quality.rstrip("k").lstrip("q"))
            except (TypeError, ValueError):
                n = None
            if n is not None:
                n = max(custom["min"], min(custom["max"], n))
                flag = "-b:a" if custom["mode"] == "kbps" else "-q:a"
                suffix = "k" if custom["mode"] == "kbps" else ""
                return list(custom["enc"]) + [flag, f"{n}{suffix}"]
        chosen = next((p for p in presets if p["v"] == spec.get("default")), None)
    if chosen is not None:
        return list(chosen["args"])
    return list(spec.get("args") or [])


def _preset_kbps(codec, quality):
    """Displayed/estimated kbps for a codec+quality pair, or None when the
    output size cannot be predicted (copy, lossless)."""
    spec = CODECS.get(codec) or {}
    quality = str(quality or "").strip()
    for p in spec.get("presets") or []:
        if p["v"] == quality:
            return p.get("kbps")
    custom = spec.get("custom")
    if custom and custom["mode"] == "kbps":
        try:
            return max(custom["min"], min(custom["max"], int(quality)))
        except (TypeError, ValueError):
            return None
    return None


def _ffmpeg_for_codec(codec):
    """ffmpeg binary path for transcoding codecs; None for 'copy'."""
    if codec == "copy":
        return None
    tools = detect_all_tools()
    ff = (tools.get("ffmpeg") or {}).get("ffmpeg_exe")
    if not ff:
        raise RuntimeError("ffmpeg not found — install it from the Dependencies tab")
    return ff


def _duration(af):
    """Seconds of audio, or 0.0 when the header does not carry it."""
    try:
        return float(af.audio.info.length)
    except Exception:
        return 0.0


def _pcm_kbps(af, fallback=_PCM_KBPS_FALLBACK):
    """Decoded rate of a source (kbps) for the lossless size estimate."""
    try:
        info = af.audio.info
        rate = info.sample_rate * info.bits_per_sample * info.channels
        return rate / 1000.0 if rate else fallback
    except Exception:
        return fallback


def _disc_of(af):
    """The file's disc number as an int (DISCNUMBER tag, else 1)."""
    raw = str(af.get_tag("DISCNUMBER") or "").strip()
    num = raw.split("/")[0].strip()
    return int(num) if num.isdigit() else 1


def _tracknum(af):
    """Zero-padded track number ('3/12' -> '03'), '00' when untagged."""
    raw = str(af.get_tag("TRACKNUMBER") or "").strip()
    num = raw.split("/")[0].split("-")[-1].strip()
    return num.zfill(2) if num.isdigit() else "00"


# --------------------------------------------------------------------------- #
# Folder structures
# --------------------------------------------------------------------------- #
# A structure is a key of the Export page's dropdown. The shipped layout and
# the user's own are naming SCRIPTS (mlo.naming — the grammar the library's own
# organizer builds paths with), so a custom structure is the same kind of thing
# the default is: one evaluator, one vocabulary and one set of rules for both.
# The user's own script lives in the `export_structure_script` config key.
DEFAULT_STRUCTURE = "albumartist_album_disc"
STRUCTURE_SCRIPTS = {
    # The library's own tree, stripped of what a device has no use for (the id
    # brackets, the release-type/date parts of the album folder):
    # ALBUMARTIST, never the track's own ARTIST — a compilation is ONE artist
    # folder, not one per track — and the library's disc-numbered file name,
    # "1-01 Title". %discnumber% is written for a single-disc album too
    # (mlo.naming supplies "1" when the tag is missing, exactly as the
    # library's default script does): an export then reads like the library it
    # was copied from, and a two-disc album cannot collide on "01 - Intro".
    "albumartist_album_disc": ("%albumartist%/%album%/"
                               "%discnumber%-$num(%tracknumber%,2) %title%"),
}
# The dropdown value that means "the script the user typed".
CUSTOM_STRUCTURE = "custom"
# Every key the API accepts, in the order the Export page's dropdown shows
# them: the shipped layout, the hand-built ones (which keep the exact spelling a
# saved `export_structure` pins) and the user's own last.
STRUCTURES = tuple(STRUCTURE_SCRIPTS) + ("album", "flat", "mirror", CUSTOM_STRUCTURE)

# What the Export page's dropdown calls each one. The page renders THIS (see
# structure_menu) rather than a list of its own: a structure the exporter would
# refuse must not be on offer, and a label is the only place the shipped file
# name is spelled out.
STRUCTURE_LABELS = {
    "albumartist_album_disc": "Album artist / Album / 1-01 Title",
    "album": "Album / 01 - Title",
    "flat": "Flat — one folder",
    "mirror": "Mirror library layout",
    CUSTOM_STRUCTURE: "Custom — your own tag fields…",
}

# The %fields% a structure script may use: every variable the naming grammar
# can supply for one track. A custom structure is checked against this, and
# the Export page lists it beside the field it types into.
STRUCTURE_FIELDS = frozenset(naming.track_variables({}))

# The pretend track a structure PREVIEW is evaluated on. Same sample, same
# idea as the Settings naming-script preview (GET /api/naming/preview), so a
# user comparing the two screens sees the same shapes.
_PREVIEW_TAGS = {
    "ALBUMARTIST": "System of a Down", "ARTIST": "System of a Down",
    "ALBUM": "Toxicity", "DATE": "2001-09-04", "ORIGINALDATE": "2001-08-27",
    "RELEASETYPE": "album", "RELEASECOUNTRY": "US", "MEDIA": "CD",
    "CATALOGNUMBER": "CK 62240", "DISCNUMBER": "1", "TRACKNUMBER": "4",
    "TITLE": "Psycho", "GENRE": "Alternative Metal",
    "LABEL": "American Recordings",
}


def structure_menu():
    """The Export page's folder-structure menu and the grammar a custom one is
    written in: every key the page may send with the label to show it under,
    plus the %fields% and $functions a script may use.

    Served rather than hard-coded in the page so the dropdown, the field hint
    and the validator cannot disagree — a structure offered by the UI but
    refused by the run is exactly the drift this table prevents.
    """
    return {
        "structures": [{"v": key, "label": STRUCTURE_LABELS[key]} for key in STRUCTURES],
        "fields": sorted(STRUCTURE_FIELDS),
        "functions": list(naming.FUNCTIONS),
    }


def file_families():
    """The file-family menu the Export page renders: every key a run accepts,
    with the label and the one-line explanation to show it under.

    Served rather than hard-coded in the page — the same reason
    ``structure_menu`` is — so the checkboxes, the sentence a refused selection
    comes back with and the run's own copy pass cannot drift apart: a family
    the menu offered but the exporter did not know is exactly what this table
    prevents."""
    return {"families": [{"v": key, "label": spec["label"], "hint": spec["hint"]}
                         for key, spec in FILE_FAMILIES.items()]}


def script_for_structure(structure, typed=""):
    """The naming script *structure* evaluates, or "" for a hand-built layout.

    ``typed`` is what the user wrote in the custom field (the
    ``export_structure_script`` config value); it is used only by the
    ``custom`` structure."""
    if str(structure or "").strip() == CUSTOM_STRUCTURE:
        return str(typed or "").strip()
    return STRUCTURE_SCRIPTS.get(str(structure or "").strip(), "")


def structure_error(structure, script=""):
    """The sentence that refuses a folder structure, or "" when it is usable.

    Refused: a layout the app does not have, and a custom script that is
    empty, names a %field% or $function the grammar does not implement, or
    evaluates to nothing. Each of those would otherwise produce a broken tree
    rather than an error — an unknown field reads as an empty tag (so its
    folder silently disappears) and an empty script writes every file into the
    export root under a name with no extension.
    """
    key = str(structure or "").strip()
    if key != CUSTOM_STRUCTURE:
        if key in STRUCTURES:
            return ""
        return ("unknown folder structure %r — pick one of: %s"
                % (key, ", ".join(STRUCTURES)))
    text = script_for_structure(key, script)
    if not text:
        return ("a custom folder structure needs a script — e.g. "
                "%albumartist%/%album%/%discnumber%-$num(%tracknumber%,2) %title%")
    fields, calls = naming.script_vocabulary(text)
    unknown = sorted({f for f in fields if f not in STRUCTURE_FIELDS})
    if unknown:
        return ("the custom folder structure uses %%%s%%, which is not a field "
                "this app knows — the fields are: %s"
                % (unknown[0], ", ".join(sorted(STRUCTURE_FIELDS))))
    unknown = sorted({c for c in calls if c not in naming.FUNCTIONS})
    if unknown:
        return ("the custom folder structure calls $%s(...), which is not a "
                "function of the naming grammar — the functions are: %s"
                % (unknown[0], ", ".join(naming.FUNCTIONS)))
    if not naming.eval_script(text, naming.track_variables(_PREVIEW_TAGS)):
        return ("the custom folder structure produced nothing for a normal "
                "track — give it a field that is always there (%title%) or a "
                "$if() fallback")
    return ""


def preview_structure(script, ext=""):
    """``{ok, path, error}`` for a user-typed folder structure.

    The path the sample track would land in (with the run's own extension on
    the end), or the sentence that refuses it — the same validation and the
    same evaluator the run itself uses, so the Export page can never preview a
    structure that would be refused at the start of a run.
    """
    problem = structure_error(CUSTOM_STRUCTURE, script)
    if problem:
        return {"ok": False, "path": "", "error": problem}
    path = naming.eval_script(str(script).strip(), naming.track_variables(_PREVIEW_TAGS))
    return {"ok": True, "path": path + str(ext or ""), "error": ""}


def _structure_variables(path, af):
    """The naming variables for one export source.

    The file's own tags (mlo.naming reads them by their semantic names), with
    the export's fallbacks filled in for the ones the grammar reads: an export
    is a copy service pointed at files the user picked, so an untagged file has
    to land somewhere sensible — the artist and album folders it already sits
    in stand in for the missing tags and its own file name for a missing
    title, the way the hand-built layouts have always done it.
    """
    tags = dict(af.all_tags() or {})
    folder = os.path.dirname(path)
    if not str(tags.get("ALBUMARTIST") or "").strip() \
            and not str(tags.get("ARTIST") or "").strip():
        tags["ALBUMARTIST"] = os.path.basename(os.path.dirname(folder)) or "Unknown Artist"
    if not str(tags.get("ALBUM") or "").strip():
        tags["ALBUM"] = os.path.basename(folder) or "Unknown Album"
    if not str(tags.get("TITLE") or "").strip():
        tags["TITLE"] = os.path.splitext(os.path.basename(path))[0]
    return naming.track_variables(tags)


def _scripted_relpath(path, af, script, ext):
    """One source's destination under a naming-script structure, or "" when
    the script produced nothing for it (the run reports that file)."""
    rel = naming.eval_script(script, _structure_variables(path, af))
    return rel.replace("/", os.sep) + ext if rel else ""


def _target_relpath(path, af, structure, music_folder, ext, disc="", script=""):
    """Destination relative path for one track under the export root."""
    if structure == "mirror" and music_folder:
        try:
            rel = os.path.relpath(path, music_folder)
        except ValueError:
            rel = None
        if rel and not rel.startswith(".."):
            # a mirror keeps the library's own names: the disc prefix is
            # already part of them, so `disc` stays unused here. The relative
            # path is normalized to "/" BEFORE sanitize_path — on Windows a
            # "\"-separated path is one segment to it, and every separator
            # became "_" (the whole tree flattened into a single file name).
            rel = (os.path.splitext(rel)[0] + ext).replace("\\", "/")
            return sanitize_path(rel).replace("/", os.sep)
    if structure in ("flat", "album"):
        # The hand-built layouts, unchanged since they shipped: a saved
        # `export_structure` pins their exact spelling, so they keep the
        # folder-derived fallbacks for an untagged file and the " - " file name
        # the library's own script does not use.
        stem = os.path.splitext(os.path.basename(path))[0]
        artist = (af.get_tag("ALBUMARTIST") or af.get_tag("ARTIST") or "").strip() \
            or (os.path.basename(os.path.dirname(os.path.dirname(path)))
                if structure != "flat" else "")
        album = (af.get_tag("ALBUM") or "").strip() \
            or (os.path.basename(os.path.dirname(path)) if structure != "flat" else "")
        title = (af.get_tag("TITLE") or "").strip() or stem
        nn = _tracknum(af)
        # sanitize_segment, not sanitize_path: everything joined here is ONE
        # name. A tag value carrying a "/" ("AC/DC") has to become "AC_DC" —
        # through sanitize_path it would have read as a folder boundary and
        # written a second directory level nobody asked for.
        if structure == "flat":
            base = f"{disc}{nn} - {title}"
            if artist:
                base = f"{artist} - {base}"
            return sanitize_segment(f"{base}{ext}")
        return os.path.join(sanitize_segment(album or "Unknown Album") or "Unknown Album",
                            sanitize_segment(f"{disc}{nn} - {title}{ext}"))
    # albumartist_album_disc (the shipped default) and every custom structure
    # are naming scripts: the tree is the grammar's business. The disc number
    # is the script's own %discnumber% (always written, "1" when untagged), so
    # the `disc` prefix the layouts above take is unused here.
    return _scripted_relpath(path, af, script or STRUCTURE_SCRIPTS[DEFAULT_STRUCTURE], ext)


def _preflight(paths):
    """Read every source's header once, before anything is written.

    Two things need the whole selection at once and neither is safe to redo
    per track: which albums are multi-disc (their file names need a disc
    prefix or two discs collide on "01 - Intro"), and the durations/sample
    formats behind the drive-fit estimate and the post-export duration check.

    Returns ``{"durations": {path: s}, "pcm_kbps": {path: kbps},
    "disc_albums": {album dir: bool}}``. Unreadable sources are simply absent
    from the tables — the per-track pass reports them.
    """
    durations, pcm, discs = {}, {}, {}
    for path in paths:
        try:
            af = AudioFile(path)
            if af.audio is None:
                continue
            durations[path] = _duration(af)
            pcm[path] = _pcm_kbps(af)
            album_dir = os.path.dirname(path)
            discs.setdefault(album_dir, set()).add(_disc_of(af))
        except Exception:
            continue
    disc_albums = {d: bool(v) for d, v in discs.items()}
    for album_dir, numbers in discs.items():
        # A disc prefix is needed when the album really spans discs: either
        # its files disagree, or the only disc number present is not the
        # first (a "disc 2" that lost its sibling still must not read "01").
        disc_albums[album_dir] = len(numbers) > 1 or max(numbers) > 1
    return {"durations": durations, "pcm_kbps": pcm, "disc_albums": disc_albums}


def _estimate_bytes(codec, quality, paths, pre):
    """Rough size of the finished export, or None when unpredictable."""
    if codec == "copy":
        total = 0
        for p in paths:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        return total or None
    durations = pre["durations"]
    if not any(durations.get(p) for p in paths):
        return None
    kbps = _preset_kbps(codec, quality)
    factor = _LOSSLESS_FACTORS.get(codec)
    total = 0.0
    for p in paths:
        seconds = durations.get(p) or 0.0
        rate = kbps or 0
        if not rate and factor:
            rate = (pre["pcm_kbps"].get(p) or _PCM_KBPS_FALLBACK) * factor
        if not rate:
            return None
        total += seconds * rate * 1000.0 / 8.0
    return int(total) or None


def _copy_once(src, dst, seen):
    """Copy src -> dst unless it was already handled (idempotent).

    A destination that already has the source's size is left alone, so
    re-exporting the same album neither duplicates work nor rewrites files."""
    if not src or not os.path.isfile(src):
        return False
    key = (os.path.normcase(os.path.abspath(src)), os.path.normcase(os.path.abspath(dst)))
    if key in seen:
        return False
    seen.add(key)
    try:
        if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src):
            return False
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        return True
    except OSError:
        return False


def _copy_siblings(cfg, src_track, dst_track, seen, selected):
    """Copy the album files this run was asked for beside the exported track.

    ``selected(folder, name, family, is_dir)`` is the RUN's own rule for "this
    run writes this sibling" — the very predicate the audit is filtered by (see
    ``export_tracks._travels``), so a file the run copies is never reported as
    left behind and a file it leaves is never copied. Which family a sibling
    belongs to comes from the ONE classifier the audit uses (``_extra_kind``),
    so the menu's promise and the run's behaviour are the same vocabulary.

    A directory is never walked (the same promise ``extra_files`` makes).
    Audio is what the run writes itself, never a sibling. Returns the number of
    files copied."""
    from mlo.grader import COVER_NAMES
    from mlo.artistdata import ARTIST_IMAGE_EXTS
    from mlo.paths import library_root

    src_dir = os.path.dirname(src_track)
    dst_dir = os.path.dirname(dst_track)
    covers = {n.lower() for n in COVER_NAMES}
    image_exts = tuple(e.lower() for e in ARTIST_IMAGE_EXTS)
    copied = 0

    try:
        entries = sorted(os.listdir(src_dir))
    except OSError:
        entries = []
    for name in entries:
        source = os.path.join(src_dir, name)
        if is_audio_file(name):
            continue
        family, _why = _extra_kind(name, covers, image_exts)
        if not selected(src_dir, name, family, os.path.isdir(source)):
            continue
        if _copy_once(source, os.path.join(dst_dir, name), seen):
            copied += 1

    # The artist image is the one file that belongs to the album's PARENT (the
    # library keeps it one level above the album folder), so it is mirrored one
    # level above the exported album folder instead — and only when the run was
    # asked for the artwork. Skipped when the album sits directly in the library
    # root: there is no artist folder to mirror.
    parent = os.path.dirname(os.path.abspath(src_dir))
    root = library_root(cfg.get("music_folder"))
    if root and os.path.normcase(parent) != os.path.normcase(os.path.abspath(root)):
        try:
            from mlo import artistdata
            image = artistdata.image_path(parent)
        except Exception:
            image = None
        if image:
            # The predicate is asked about the FILE that would be copied, never
            # about a family on its own: one rule decides what travels, for
            # every file a run can reach.
            name = os.path.basename(image)
            if selected(parent, name, "cover", False):
                if _copy_once(image,
                              os.path.join(os.path.dirname(dst_dir), name),
                              seen):
                    copied += 1
    return copied


def _writer(path, id3v2="2.4", id3v1=False):
    """An AudioFile opened with the export's ID3 write options already set.

    Every write on the exported file has to carry them, not just the tag pass:
    the art and ReplayGain passes open their own handle, and one of them saving
    with mutagen's v2.4 default would undo the v2.3 the user asked for (same
    for an ID3v1 chunk)."""
    af = AudioFile(path)
    af.id3_version = 3 if str(id3v2) == "2.3" else 4
    af.id3v1 = bool(id3v1)
    return af


def _write_tags(dst_path, src_af, id3v2="2.4", id3v1=False, drop=()):
    """Copy the source track's full semantic tag set onto the exported
    file (mutagen handles the per-format mapping via AudioFile.set_tag).

    ``id3v2``/``id3v1`` only mean anything for ID3 containers (MP3, raw AAC)
    and are what older players need: v2.3 instead of mutagen's v2.4 default,
    plus optionally an ID3v1 chunk.

    ``drop`` names tags the export must NOT inherit. The ReplayGain set is the
    case that matters: an exported file whose gain is already in its samples
    must not also carry the tags, or a player that honours them applies the
    same correction a second time. Returns the number of tags written."""
    dst = _writer(dst_path, id3v2, id3v1)
    if dst.audio is None:
        return 0
    written = 0
    dst.defer_save(True)
    try:
        for k, v in (src_af.all_tags() or {}).items():
            if v is None or str(v).strip() == "":
                continue
            if k in drop:
                continue
            try:
                if dst.set_tag(k, str(v)):
                    written += 1
            except Exception:
                pass  # individual exotic tags must not abort the export
    finally:
        dst.defer_save(False)
    return written


def _prepare_cover(src_af, src_path, quality, resolution, progressive=True):
    """(data, mime) of the cover to embed, re-encoded at the requested JPEG
    quality / resolution cap, or None when there is nothing to embed.

    The album's on-disk cover wins (cover.jpg / png / jxl next to the track —
    the copy the user curated); a file carrying its own art but sitting in a
    folder without one falls back to its embedded picture, prepared through
    the same byte-level helper script 10 uses, so both paths apply identical
    quality/resolution rules."""
    from mlo.format_all import _prepare_embedded_cover, prepare_cover_bytes

    cfg = {"embed_cover_jpeg_quality": quality,
           "embed_cover_resolution": resolution,
           "jpeg_progressive": progressive}
    prepared = _prepare_embedded_cover(os.path.dirname(src_path), cfg)
    if prepared:
        return prepared
    pics = src_af.embedded_pictures()
    if pics:
        return prepare_cover_bytes(pics[0][1], pics[0][0], cfg)
    return None


def _embed_cover(src_af, src_path, dst_path, quality, resolution, progressive=True,
                 id3v2="2.4", id3v1=False):
    """Embed the album cover into an exported file (best effort).

    Only containers that can carry art get one — video containers and raw
    ADTS .aac cannot, and WAV/AIFF have no standard picture chunk. A file
    that already carries exactly this art is left untouched."""
    try:
        prepared = _prepare_cover(src_af, src_path, quality, resolution, progressive)
        if not prepared:
            return False
        data, mime = prepared
        dst_af = _writer(dst_path, id3v2, id3v1)
        if dst_af.audio is None or dst_af.kind in ("video", "aac", "wav"):
            return False
        pics = dst_af.embedded_pictures()
        if len(pics) == 1 and pics[0][0] == mime and pics[0][1] == data:
            return False  # exactly this art is already embedded
        if pics and not dst_af.remove_embedded_pictures():
            return False
        return bool(dst_af.add_embedded_picture(data, mime))
    except Exception:
        return False  # artwork is cosmetic — never fail the export for it


def _rg_from_stderr(text):
    """ReplayGain of one transcode pass from ffmpeg's ebur128 summary."""
    from mlo.loudness import RG2_REFERENCE_LUFS, parse_ebur128
    measured = parse_ebur128(text or "")
    if not measured:
        return None
    return {"gain_db": RG2_REFERENCE_LUFS - measured["lufs"],
            "peak": measured["peak"], "lufs": measured["lufs"]}


def _write_rg_tags(path, gain_db=None, peak=None, album_gain_db=None,
                   album_peak_db=None, id3v2="2.4", id3v1=False):
    """Write whichever ReplayGain fields are known onto an exported file."""
    try:
        af = _writer(path, id3v2, id3v1)
        if af.audio is None:
            return False
        af.defer_save(True)
        for key, value in (("REPLAYGAIN_TRACK_GAIN",
                            None if gain_db is None else f"{gain_db:.2f} dB"),
                           ("REPLAYGAIN_TRACK_PEAK",
                            None if peak is None else f"{peak:.6f}"),
                           ("REPLAYGAIN_ALBUM_GAIN",
                            None if album_gain_db is None else f"{album_gain_db:.2f} dB"),
                           ("REPLAYGAIN_ALBUM_PEAK",
                            None if album_peak_db is None else f"{album_peak_db:.6f}")):
            if value is None:
                continue
            try:
                af.set_tag(key, value)
            except Exception:
                pass
        af.defer_save(False)
        return True
    except Exception:
        return False


def _album_gain(measurements):
    """Album ReplayGain from its tracks' measurements: the energy average of
    the per-track loudness (what an album gain means — one correction for the
    whole album, so quiet and loud tracks keep their relative levels), with
    the album peak as the loudest track."""
    from mlo.loudness import RG2_REFERENCE_LUFS
    lufs = [m["lufs"] for m in measurements if m.get("lufs") is not None]
    peaks = [m["peak"] for m in measurements if m.get("peak") is not None]
    if not lufs:
        return None, None
    energy = sum(10.0 ** ((v + 0.691) / 10.0) for v in lufs) / len(lufs)
    album_lufs = -0.691 + 10.0 * math.log10(energy)
    return RG2_REFERENCE_LUFS - album_lufs, (max(peaks) if peaks else None)


def _measure_cmd(ffmpeg, path):
    """ffmpeg arguments that DECODE *path* and report its loudness.

    The same filter pair the transcode pass appends, so a gain measured here is
    the gain those tags would carry — and the same one mlo.loudness measures a
    library file with. ebur128 logs its summary at info level, hence no
    ``-v error``.
    """
    return [ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-i", tool_path(path),
            "-map", "0:a:0", "-af", _RG_FILTER, "-f", "null", "-"]


def _measure_sources(ffmpeg, paths, workers, job=None):
    """EBU R128 measurement of every source, ahead of an ``apply`` run.

    The tags mode can measure inside the encode (the ebur128 filter is a
    pass-through), but ``volume`` has to be given a number before the encode
    starts, so the one decode an apply run needs happens here and the encode
    then reuses the numbers. Returns ``{path: measurement}``; a track that
    could not be measured is absent, and the run fails it rather than guessing
    a gain.
    """
    done = {"n": 0}
    lock = threading.Lock()

    def _one(path):
        measurement = None
        try:
            proc = subprocess.run(
                _measure_cmd(ffmpeg, path), capture_output=True, text=True,
                creationflags=0x08000000 if os.name == "nt" else 0)
            if proc.returncode == 0:
                measurement = _rg_from_stderr(proc.stderr)
        except Exception:
            measurement = None  # unreadable source: the encode will report it
        with lock:
            done["n"] += 1
            job_locks.publish(done["n"], len(paths), "Measuring loudness", job=job)
        return measurement

    if workers > 1:
        import concurrent.futures as _futures
        with _futures.ThreadPoolExecutor(max_workers=workers) as pool:
            return dict(zip(paths, pool.map(_one, paths)))
    return {path: _one(path) for path in paths}


def _whole_album(folder, selected):
    """True when the selection holds every audio file in *folder*.

    That is the case the ALBUM gain is for: one correction across the album
    leaves its internal balance (the quiet ballad, the loud opener) exactly as
    it was mastered. A selection that holds part of an album has no album
    balance to preserve, so those tracks get their own gain.
    """
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    files = {os.path.normcase(os.path.normpath(os.path.join(folder, name)))
             for name in names
             if os.path.splitext(name)[1].lower() in EXPORT_AUDIO_EXTS}
    return bool(files) and files <= selected


def _apply_gains(paths, measured):
    """({path: gain_db}, tracks that may clip) for an ``apply`` run.

    Every number comes from the ONE measurement pass the run already did — no
    second decode. The album gain is used when the selection covers the whole
    album (album folder), the track's own gain otherwise, and the peak from the
    same measurement says which of them will push the export over full scale,
    so the run can report that instead of shipping a clipped file silently.
    """
    selected = {os.path.normcase(os.path.abspath(p)) for p in paths}
    groups = {}
    for path in paths:
        groups.setdefault(os.path.dirname(os.path.abspath(path)), []).append(path)
    gains, clipping = {}, 0
    for folder, members in groups.items():
        album_gain = None
        if _whole_album(folder, selected):
            album_gain, _peak = _album_gain(
                [measured[path] for path in members if measured.get(path)])
        for path in members:
            measurement = measured.get(path) or {}
            gain = album_gain if album_gain is not None else measurement.get("gain_db")
            gains[path] = gain
            peak = measurement.get("peak")
            if gain is not None and peak:
                if gain > -20.0 * math.log10(peak):
                    clipping += 1
    return gains, clipping


def _af_chain(gain_db, eq_filters):
    """The ``-af`` chain for one track: the ReplayGain gain first (it sets the
    level everything after it works on), then the equalizer's preamp and its
    filters in file order. Empty when the run has nothing to apply."""
    parts = []
    if gain_db is not None:
        # Two decimals, the same precision the REPLAYGAIN_*_GAIN tags carry.
        parts.append(f"volume={gain_db:.2f}dB")
    parts.extend(eq_filters)
    return parts


def _processing_signature(mode, eq_id):
    """"replaygain=apply eq=bass_shelf", or "" when nothing rewrote the audio."""
    parts = []
    if mode == "apply":
        parts.append("replaygain=apply")
    if eq_id:
        parts.append(f"eq={eq_id}")
    return " ".join(parts)


def _processing_key(af):
    """The name *af*'s container can actually hold the stamp under.

    A free-form name is a Vorbis comment, ID3 needs the TXXX spelling and MP4 a
    freeform atom; any other spelling is refused by the writer (and on MP4 a
    wrong one wrote a tag block nothing could read). A container with no tag set
    this app can write (WAV) gets no stamp at all — a later run then re-encodes,
    which is the right answer when the file cannot record what was done to it.
    """
    kind = getattr(af, "kind", "") or ""
    if kind in ("flac", "ogg", "opus"):
        return _PROCESSING_TAG
    if kind == "mp4":
        return "----:com.apple.iTunes:" + _PROCESSING_TAG
    return "TXXX:" + _PROCESSING_TAG


def _processing_of(path):
    """The processing an exported file records, "" when it records none.

    A custom tag comes back as its RAW container key (a lower-case Vorbis
    comment, a TXXX frame, a freeform MP4 atom) rather than as a semantic name
    get_tag knows, so the lookup is by suffix."""
    try:
        af = AudioFile(path)
        for key, value in (af.all_tags() or {}).items():
            if str(key).upper().endswith(_PROCESSING_TAG):
                return str(value or "").strip()
    except Exception:
        pass
    return ""


def _write_processing_tag(path, signature, id3v2="2.4", id3v1=False):
    """Record (or clear) what audio processing wrote this file. Never raises:
    the stamp only decides whether a future run can skip the file, and a
    failure here must not fail an export that is otherwise written."""
    try:
        af = _writer(path, id3v2, id3v1)
        if af.audio is None:
            return False
        key = _processing_key(af)
        if not signature:
            if not _processing_of(path):
                return False  # nothing to clear: do not rewrite the container
            af.delete_tag(key)
            return True
        af.set_tag(key, signature)
        return True
    except Exception:
        return False


def _tag_identity(af, key):
    """One tag's comparable value. Track numbers are the interesting case: the
    same recording reads "1/0" from an MP4 atom and "1" from an ID3 frame, so
    the leading integer is what a re-run compares (and what a player shows)."""
    raw = str(af.get_tag(key) or "").strip()
    if key == "TRACKNUMBER":
        num = raw.split("/")[0].split("-")[-1].strip()
        return num.lstrip("0")
    return raw.casefold()


def _target_kbps(args):
    """The bitrate a ``-b:a 192k`` argument pair asks for, or None (VBR)."""
    for i, arg in enumerate(args[:-1]):
        if arg == "-b:a" and args[i + 1].endswith("k"):
            try:
                return int(args[i + 1][:-1])
            except ValueError:
                return None
    return None


def _bitrate_matches(args, dst_af):
    """True when an existing destination's bitrate can be the requested one.

    CBR presets only: a user who switches 128 kbps to 320 kbps on a folder that
    already holds the 128 kbps export must be told rather than silently
    skipped. VBR and lossless have no single number to compare, so the tag
    identity is all there is to go on."""
    want = _target_kbps(args)
    if not want:
        return True
    try:
        have = float(dst_af.audio.info.bitrate) / 1000.0
    except Exception:
        return True
    return abs(have - want) <= max(16, want * 0.2)


def _same_export(src_af, dst, src_seconds, args=()):
    """True when ``dst`` already IS this source's export.

    A re-run must be idempotent for transcodes too, where the byte sizes can
    never match: the destination is re-opened and compared on the identity a
    player shows — duration, the track tags and (for a CBR preset) the
    bitrate — so a stale file from another source, or one written by a
    different preset, is still reported instead of silently accepted."""
    try:
        if not os.path.isfile(dst) or os.path.getsize(dst) <= 0:
            return False
        dst_af = AudioFile(dst)
        if dst_af.audio is None:
            return False
        seconds = _duration(dst_af)
        if src_seconds and seconds and abs(seconds - src_seconds) > 1.0:
            return False
        if not _bitrate_matches(args, dst_af):
            return False
        for key in ("TITLE", "ALBUM", "TRACKNUMBER"):
            want = _tag_identity(src_af, key)
            # an untagged source field cannot identify anything: skip it
            if want and _tag_identity(dst_af, key) != want:
                return False
        return True
    except Exception:
        return False


def _verify(dst, src_seconds):
    """Re-open a written file and prove it parses — the check that catches a
    half-written transcode. Returns an error string, or None when it is good."""
    try:
        af = AudioFile(dst)
    except Exception as e:
        return f"verify: {e}"
    if af.audio is None:
        return f"verify: {af.error or 'unreadable'}"
    seconds = _duration(af)
    if not seconds:
        return "verify: no audio duration"
    if src_seconds and abs(seconds - src_seconds) > max(1.0, src_seconds * 0.02):
        return f"verify: {seconds:.1f}s of audio, expected {src_seconds:.1f}s"
    return None


# ------------------------------------------------- what an export leaves behind
# An album folder holds more than audio: a rip's verification evidence
# (.log/.accurip/.md5), the ripper's own sidecars (.cue/.txt/.nfo), the app's
# album playlists and the cover image. An export carries what the user asked it
# to carry (``copy_files`` / FILE_FAMILIES above) and nothing else, so every
# file it did not write stays in the library where the audit, the grading and
# the rip's own checksum read it. What an export must NEVER do is drop one of
# them silently: the run reports every non-audio file it found, by album, with
# the family it belongs to and the reason it did not travel.
#
# ONE table: extension -> (family, why it stays). The families it names are the
# keys of FILE_FAMILIES above — the menu, the copy pass and this report are one
# vocabulary, so a run cannot copy a family it does not offer and cannot report
# as "left behind" a file it just wrote.
_EXTRA_REASONS = {
    ".log": ("log", "the rip log — the audit verifies its checksum"),
    ".accurip": ("accurip", "the AccurateRip report the audit verifies"),
    ".cue": ("cue", "the rip's track layout, which stays with the audio it describes"),
    ".md5": ("checksum", "a checksum list for the rip"),
    ".sfv": ("checksum", "a checksum list for the rip"),
    ".ffp": ("checksum", "a FLAC fingerprint list for the rip"),
    ".torrent": ("checksum", "a torrent file describing the release"),
    ".txt": ("text", "a text sidecar (an album description) written beside the audio"),
    ".nfo": ("text", "the ripper's release notes"),
    ".url": ("text", "a link file written beside the audio"),
    ".pdf": ("text", "a booklet/scan written beside the audio"),
    ".lrc": ("lyrics", "lyrics written beside the track"),
    ".m3u": ("playlist", "playlists come from a playlist export, not from an album export"),
    ".m3u8": ("playlist", "playlists come from a playlist export, not from an album export"),
    ".pls": ("playlist", "playlists come from a playlist export, not from an album export"),
    ".wpl": ("playlist", "playlists come from a playlist export, not from an album export"),
}


def _extra_kind(name, covers, image_exts):
    """(family, why) for ONE non-audio sibling. The family is a key of
    FILE_FAMILIES ("other" for a file this app does not classify at all), and
    nothing is guessed from a file's CONTENTS — only its name decides, so the
    same file always lands in the same family and the run's copy pass, the menu
    and this audit can never disagree about it."""
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if low in covers:
        # A cover is a NAME (cover.jpg/.jpeg/.png/.jxl), not an extension: the
        # app reads it as the album's artwork whatever else sits beside it.
        return ("cover",
                "the album cover — it travels INSIDE the file (embed_covers)")
    if ext in image_exts and os.path.splitext(low)[0] == "artist":
        return ("cover",
                "the artist image — it belongs to the artist folder, not the album")
    if album_sidecar_of(low):
        # The app's OWN album note — and a numbered copy ("description (2).txt")
        # is that same file, not a stray (mlo.paths.album_sidecar_of).
        return ("description",
                "the album's description — the file this app writes and reads")
    known = _EXTRA_REASONS.get(ext)
    if known:
        return known
    return "other", "not audio and not a file this app writes"


def extra_files(cfg, paths, skip=None):
    """Every non-audio file beside the selected tracks, grouped by album, with
    the reason it is not part of the export.

    Returns ``{"files": [{album, name, kind, reason, dir}], "counts": {kind: n},
    "total": n, "albums": {album: n}}``, where a row's ``kind`` is the file
    FAMILY it belongs to (a key of ``FILE_FAMILIES``) and ``reason`` is why it
    did not travel. Directories are reported too — a stray subfolder is exactly
    the kind of thing a library carries that nobody anticipated — named with a
    trailing "/" and never walked.

    ``skip(folder, name, family, is_dir)`` says a sibling is NOT an extra for
    the run that asked, because that run WROTE it: a file it was asked to copy
    (the run's own predicate, ``export_tracks._travels``, is passed here), and a
    `.lrc` whose track is in the selection when the run writes lyrics as a file
    — output, not something left behind. The `.lrc` of a track OUTSIDE the
    selection stays in the library and is still reported.
    """
    from mlo.grader import COVER_NAMES
    from mlo.artistdata import ARTIST_IMAGE_EXTS
    from mlo.stats import is_audio_file

    covers = {n.lower() for n in COVER_NAMES}
    image_exts = tuple(e.lower() for e in ARTIST_IMAGE_EXTS)
    root = os.path.abspath(cfg.get("music_folder") or "")
    rows, counts, albums = [], {}, {}
    for folder in sorted({os.path.dirname(p) for p in paths if p}):
        album = folder
        if root and os.path.normcase(folder).startswith(os.path.normcase(root) + os.sep):
            album = os.path.relpath(folder, root).replace(os.sep, "/")
        albums.setdefault(album, 0)
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in entries:
            if is_audio_file(name):
                continue                       # audio is what travels
            family, reason = _extra_kind(name, covers, image_exts)
            is_dir = os.path.isdir(os.path.join(folder, name))
            if skip and skip(folder, name, family, is_dir):
                continue
            rows.append({"album": album, "name": name + ("/" if is_dir else ""),
                         "kind": family, "reason": reason, "dir": is_dir})
            counts[family] = counts.get(family, 0) + 1
            albums[album] += 1
    return {"files": rows, "counts": counts, "total": len(rows), "albums": albums}


def _extra_summary(extra):
    """One sentence naming what the run left behind, for the run log and the
    result. Empty when the selection carried nothing but audio."""
    if not extra["total"]:
        return ""
    counts = ", ".join(f"{n} {kind}" for kind, n in sorted(extra["counts"].items()))
    return (f"{extra['total']} non-audio file(s) not exported ({counts}) — "
            f"see 'excluded' for each one and why")


def _write_playlists(root, groups, exported):
    """Write ``.m3u8`` playlists for a DAP (UTF-8, relative paths, EXTINF).

    ``groups`` maps an album folder (under the export root, "" for the root
    itself) to the tracks written for it; each group gets its own playlist
    named after the folder, and the whole export also gets ``all.m3u8`` at
    the root. Returns the number of playlists written.
    """
    written = 0
    plan = []
    for folder, rows in groups.items():
        if not rows:
            continue
        name = os.path.basename(folder) if folder else "all"
        if folder:
            plan.append((folder, f"{sanitize_segment(name) or 'album'}.m3u8", rows))
    if exported:
        plan.append(("", "all.m3u8", [r for rows in groups.values() for r in rows]))
    for folder, name, rows in plan:
        target = os.path.join(root, folder, name)
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="\n") as f:
                f.write("#EXTM3U\n")
                for dst, title, artist, seconds in sorted(rows, key=lambda r: r[0]):
                    rel = os.path.relpath(dst, os.path.dirname(target))
                    info = f"{int(round(seconds)) if seconds else -1}," \
                           f"{artist or 'Unknown Artist'} - {title or ''}"
                    f.write(f"#EXTINF:{info}\n{rel.replace(os.sep, '/')}\n")
            written += 1
        except OSError:
            pass  # a playlist is a convenience, never a reason to fail an export
    return written


def _prune(root, keep):
    """Delete audio files under the export root that this run did not write.

    Sync mode: the export root is meant to be a mirror of the selection, so a
    track that left the selection leaves the device too. Only files this
    exporter could have produced are considered (see EXPORT_AUDIO_EXTS) —
    anything else on the drive is left alone — and emptied folders are
    removed. Returns ``(count, [names])``.
    """
    removed, names = 0, []
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.splitext(name)[1].lower() not in EXPORT_AUDIO_EXTS:
                continue
            if os.path.normcase(os.path.abspath(path)) in keep:
                continue
            try:
                os.remove(path)
                removed += 1
                if len(names) < 25:
                    names.append(name)
            except OSError:
                pass
        if dirpath != root:
            try:
                os.rmdir(dirpath)  # only succeeds while it is empty
            except OSError:
                pass
    return removed, names


def _track_playlist_row(dst, af, seconds):
    return (dst,
            str(af.get_tag("TITLE") or os.path.splitext(os.path.basename(dst))[0]),
            str(af.get_tag("ARTIST") or af.get_tag("ALBUMARTIST") or ""),
            seconds)


def _write_manifest(root):
    """Write ``checksums.sha256`` at the export root.

    One ``<sha256>  <relative path>`` line per file under the root, sorted —
    the format ``sha256sum -c`` reads back, so a user who copies the export to
    a card (or unzips it) can prove the bytes survived the trip. That is the
    one thing a tag cannot tell them. Returns the number of lines written, 0
    when the manifest itself could not be written.
    """
    rows = []
    try:
        for folder, _dirs, names in os.walk(root):
            for name in names:
                path = os.path.join(folder, name)
                if name == "checksums.sha256":
                    continue
                digest = hashlib.sha256()
                try:
                    with open(path, "rb") as f:
                        for chunk in iter(lambda: f.read(1024 * 1024), b""):
                            digest.update(chunk)
                except OSError:
                    continue
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                rows.append(f"{digest.hexdigest()}  {rel}")
    except OSError:
        return 0
    try:
        with open(os.path.join(root, "checksums.sha256"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write("\n".join(sorted(rows)) + ("\n" if rows else ""))
    except OSError:
        return 0
    return len(rows)


def _zip_root(cfg):
    """The staging folder of a zip export, as ``(id, folder)``.

    A browser cannot hand the server a folder on the user's own computer, so
    for a browser client the archive IS the destination: the export is staged
    under the app's own data dir and zipped there. Every earlier staging folder
    is removed first — the app keeps ONE built archive at a time, and the next
    export replacing it is what makes the id (and the download URL) a single
    well-known thing instead of a growing pile of exports nobody cleans up.
    """
    base = os.path.join(app_data_dir(cfg.get("music_folder")), "export_zip")
    try:
        for name in os.listdir(base):
            victim = os.path.join(base, name)
            if os.path.isdir(victim):
                shutil.rmtree(victim, ignore_errors=True)
            else:
                try:
                    os.remove(victim)
                except OSError:
                    pass
    except OSError:
        pass  # nothing built yet
    run_id = time.strftime("%Y%m%d-%H%M%S")
    root = os.path.join(base, run_id, "stage")
    os.makedirs(root, exist_ok=True)
    return run_id, root


def _build_zip(run_id, root, exported):
    """Zip the staged tree and drop it, returning the response's ``zip`` dict.

    Members are the export's own relative paths, in sorted order, so the
    archive opens as the tree the folder structure option describes — no extra
    wrapper folder to click through. The archive keeps the human name so the
    browser's download gets that name, and it lives beside the staging folder
    rather than inside it (a zip cannot contain itself).
    """
    name = f"la-musica-export-{int(exported)}-tracks.zip"
    folder = os.path.dirname(root)
    target = os.path.join(folder, name)
    members = []
    for base, dirs, names in os.walk(root):
        dirs.sort()
        for member in sorted(names):
            path = os.path.join(base, member)
            members.append((path, os.path.relpath(path, root).replace(os.sep, "/")))
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, rel in members:
                zf.write(path, rel)
    except OSError:
        return None
    shutil.rmtree(root, ignore_errors=True)
    return {"id": run_id, "name": name, "bytes": os.path.getsize(target),
            "files": len(members)}


def _zip_folder(cfg, run_id):
    """The staging folder of *run_id*, or None when the id is unusable.

    The id comes from a URL, so it is validated before it is joined onto the
    data dir: anything that could name a path outside it answers None (the
    route turns that into a 404) instead of reading or deleting an arbitrary
    file.
    """
    text = str(run_id or "")
    if not text or any(sep in text for sep in ("/", "\\", "..", "\x00")):
        return None
    return os.path.join(app_data_dir((cfg or {}).get("music_folder")),
                        "export_zip", text)


def zip_path(cfg, run_id):
    """The built archive for *run_id*, or None when there is not one."""
    folder = _zip_folder(cfg, run_id)
    if not folder:
        return None
    try:
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith(".zip"):
                return os.path.join(folder, name)
    except OSError:
        return None
    return None


def drop_zip(cfg, run_id):
    """Delete the whole staging folder of *run_id*. False when there was none."""
    folder = _zip_folder(cfg, run_id)
    if not folder or not os.path.isdir(folder):
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return True


def export_tracks(cfg, paths, dest, subfolder="Music", codec="copy",
                  quality="", structure="", structure_script="", **opts):
    """Run the export; returns a stats dict for the API response.

    ``structure`` is one of ``STRUCTURES`` (empty = the shipped one) and
    ``structure_script`` the naming script a ``custom`` structure evaluates.

    ``opts`` holds per-run overrides of the compatibility options (see
    ``EXPORT_DEFAULTS``); each falls back to its ``export_<name>`` config value.

    Raises ValueError when the destination is unusable in a way that would
    destroy library data (it lies inside the music folder), when the codec is
    unknown, and when the folder structure cannot name a path — the caller
    turns that into a 4xx, and a run must never start."""
    opts = {k: v for k, v in opts.items() if v is not None}
    structure = str(structure or "").strip() or DEFAULT_STRUCTURE
    problem = structure_error(structure, structure_script)
    if problem:
        # Refused before the destination is created: a structure that cannot
        # name a path would otherwise write into the root, or write nothing at
        # all, and leave a tree behind that nobody asked for.
        raise ValueError(problem)
    script = script_for_structure(structure, structure_script)
    # The job this export belongs to (the route holds one, kind="export"), read
    # HERE: the ticks below come from pool threads, which are not inside it.
    # job_locks.publish then writes the in-progress row and the header bar from
    # the same frame.
    job = job_locks.current()
    # A flag left armed by a cancelled run must not kill THIS one: the next
    # export starts clean, whatever the last press was for.
    _CANCEL.clear()
    out = {"total": len(paths), "exported": 0, "skipped": 0, "failed": 0,
           "bytes": 0, "sidecars": 0, "playlists": 0, "verified": 0,
           "pruned": 0, "pruned_files": [], "warnings": [], "errors": [],
           "cancelled": False,
           "error_count": 0, "estimated_bytes": None,
           "processed": 0, "eq_applied": 0, "zip": None,
           "lyrics_mode": "", "lyrics_files": 0,
           # Every non-audio file beside the selection, and why it did not
           # travel: an export writes what it was asked for (see FILE_FAMILIES)
           # and a file it leaves behind is reported rather than dropped in
           # silence.
           "excluded": [], "excluded_counts": {}, "excluded_total": 0,
           "excluded_note": ""}
    if not paths:
        return out

    # WHICH files this run writes (see FILE_FAMILIES). Resolved first, and
    # refused here, because the audit below, the copy pass and every write
    # depend on it — and a selection that names no files must fail BEFORE the
    # destination is touched, not leave an empty tree behind.
    families = copy_files(cfg, opts)
    given = (opts or {}).get("copy_files")
    if given is not None:
        problem = copy_files_error(given)
        if problem:
            raise ValueError(problem)
    # What this run was asked to write, as the response carries it — the same
    # shape the lyrics mode is reported in, so a client can see the selection
    # the run RESOLVED (a saved default and the switch it replaced included)
    # rather than the one it hoped it sent.
    out["copy_files"] = list(families)

    # Read BEFORE the audit below, which asks what this run writes itself.
    lyrics = lyrics_mode(cfg, opts)
    # The two halves of the choice: the tag rides along with every other tag on
    # a rewrite, and is DROPPED when the run writes the lyrics as a file. The
    # `.lrc` leg is written with the tracks, from the SOURCE text — so "both"
    # cannot leave the tag and the file disagreeing.
    lyrics_tag = lyrics in ("embedded", "both")
    lyrics_lrc = lyrics in ("lrc", "both")

    # What the run itself writes as a `.lrc`, so the audit below does not
    # report a travelling lyric file as "left behind" (see extra_files' skip).
    travelling = {}
    for _p in paths:
        travelling.setdefault(os.path.normcase(os.path.dirname(_p)), set()).add(
            os.path.splitext(os.path.basename(_p))[0].lower())

    def _lyrics_written(folder, name, family):
        if family != "lyrics" or not lyrics_lrc:
            return False
        return (os.path.splitext(name)[0].lower()
                in travelling.get(os.path.normcase(folder), ()))

    def _travels(folder, name, family, is_dir=False):
        """Whether THIS run writes this sibling.

        The ONE rule the file selection is enforced by, for every caller: the
        audit's filter below asks it (a file the run writes is not "left
        behind"), the copy pass asks it before it copies anything
        (``_copy_siblings``), and nothing else decides. A directory is never
        written (a stray subfolder is reported, never walked), audio is what
        the run writes itself rather than a sibling, and a `.lrc` belongs to
        ONE track — it travels with that track (the same per-track rule the
        audit uses), never with a track outside the selection."""
        if is_dir or is_audio_file(name) or family not in families:
            return False
        if family == "lyrics":
            return (os.path.splitext(name)[0].lower()
                    in travelling.get(os.path.normcase(folder), ()))
        return True

    extra = extra_files(
        cfg, paths,
        skip=lambda folder, name, family, is_dir: (
            _travels(folder, name, family, is_dir)
            or _lyrics_written(folder, name, family)))
    out["excluded"] = extra["files"]
    out["excluded_counts"] = extra["counts"]
    out["excluded_total"] = extra["total"]
    out["excluded_note"] = _extra_summary(extra)

    target = str(option(cfg, opts, "target") or "server").strip().lower()
    if target not in TARGETS:
        raise ValueError(f"unknown export target: {target}")
    zip_target = target == "zip"
    if not zip_target and (not dest or not os.path.isdir(dest)):
        out["errors"].append(f"Destination not found: {dest}")
        out["failed"] = out["total"]
        return out

    music_folder = os.path.abspath(cfg.get("music_folder") or "")

    spec = CODECS.get(codec)
    if spec is None:
        raise ValueError(f"unknown codec: {codec}")
    quality = str(quality or "").strip() or _default_quality(codec)
    ext = spec.get("ext")
    args = _codec_args(codec, quality)
    ffmpeg = _ffmpeg_for_codec(codec)
    ffmpeg_missing = ffmpeg is None and codec != "copy"

    embed_covers = bool(option(cfg, opts, "embed_covers"))
    embed_quality = option_int(cfg, opts, "embed_cover_jpeg_quality", 1, 100)
    embed_resolution = option_int(cfg, opts, "embed_cover_resolution", 0, 8000)
    progressive = bool(cfg.get("jpeg_progressive", True))
    id3v2 = str(option(cfg, opts, "id3v2"))
    id3v1 = bool(option(cfg, opts, "id3v1"))
    rg_mode = _replaygain_mode(cfg, opts)
    rg_tags = rg_mode == "tags"
    rg_apply = rg_mode == "apply"
    eq_id = str(option(cfg, opts, "eq_profile") or "").strip()
    clean_tags = bool(option(cfg, opts, "clean_tags"))
    want_playlists = bool(option(cfg, opts, "playlists"))
    # Whether any NON-audio family was asked for at all: a run that copies
    # the tracks alone never lists the album folder beside them.
    want_siblings = any(key != "audio" for key in families)
    want_manifest = bool(option(cfg, opts, "manifest"))
    verify = bool(option(cfg, opts, "verify"))
    prune = bool(option(cfg, opts, "prune")) and not zip_target
    workers = option_int(cfg, opts, "workers", 0, 64) or worker_count(
        cfg, default=max(2, (os.cpu_count() or 4) // 2), maximum=8, items=len(paths))
    workers = min(workers, max(1, len(paths)))

    # The equalizer profile is resolved once: the chain is the same for every
    # track, and a profile that cannot be found fails each track that asked for
    # it rather than exporting the file without the curve the user chose. The
    # app data dir is resolved from the CONFIGURED music folder (the same way
    # the zip staging folder is), not from the absolute path above — an install
    # with no folder yet keeps reading the legacy repo-local one.
    eq_profile = eq_mod.find(cfg.get("music_folder") or "", eq_id) if eq_id else None
    if eq_id and eq_profile is None:
        eq_filters = []
        eq_error = (f"equalizer profile {eq_id!r} not found — pick another "
                    "profile or clear the equalizer option")
    elif eq_profile and eq_profile.get("errors"):
        # A profile file on disk with a band line this app cannot read: applying
        # the rest would put a curve on the device that the profile never asked
        # for, which for audio is worse than refusing. The line is named — in the
        # SAME sentence the import and the player refuse it with, so one profile
        # is never described three ways.
        eq_filters = []
        eq_error = (f"equalizer profile {eq_id!r} — "
                    f"{eq_mod.apply_refusal(eq_profile)}")
    else:
        eq_filters = eq_mod.chain(eq_profile) if eq_profile else []
        eq_error = None
    processing = _processing_signature(rg_mode, eq_id)
    filtered_run = rg_apply or bool(eq_filters)

    if prune and "audio" not in families:
        # Sync mode makes the destination match the tracks a run writes, so a
        # selection that writes no tracks would DELETE the destination's audio
        # instead of mirroring anything. A contradictory request is refused
        # with the reason rather than obeyed.
        raise ValueError(
            "sync mode mirrors the tracks an export writes, but this file "
            "selection copies no tracks — include \"The tracks themselves\", "
            "or turn sync off")
    if codec == "copy" and filtered_run:
        # A copied file IS the source's bytes: there is no decode to filter.
        # Refused before anything is written, so the run fails with one message
        # naming what has to change instead of every track failing on its own.
        raise ValueError(_PROCESSING_NEEDS_CODEC)

    out["replaygain_mode"] = rg_mode
    out["lyrics_mode"] = lyrics
    out["eq_profile"] = eq_id
    if zip_target and bool(option(cfg, opts, "prune")):
        out["warnings"].append(
            "prune applies to a device the export mirrors; a zip holds exactly "
            "this run's selection, so it was ignored")

    zip_id = ""
    if zip_target:
        # dest/subfolder name a place on the SERVER, which a browser client has
        # no way to choose for the user; the archive below is the destination
        # instead, so the library guard does not apply to it.
        zip_id, root = _zip_root(cfg)
    else:
        root = os.path.abspath(os.path.join(dest, safe_subfolder(subfolder)))
        if music_folder and (root == music_folder
                             or root.startswith(music_folder + os.sep)):
            raise ValueError(
                "destination is inside the music folder — export to a device or a "
                "folder outside the library")
        os.makedirs(root, exist_ok=True)

    if script:
        # The script must name a path for a REAL track, not only for the sample
        # the preview uses: "%catalognumber%" produces a path for the sample and
        # nothing at all for a library that has no catalog numbers. Checked
        # once, up front, so a run refuses with a sentence instead of writing
        # the whole selection into the export root.
        try:
            probe = AudioFile(paths[0])
        except Exception:
            probe = None
        if probe is not None and probe.audio is not None and not _scripted_relpath(
                paths[0], probe, script, ""):
            raise ValueError(
                f"the folder structure produced no path for "
                f"{os.path.basename(paths[0])!r} — give it a field that is always "
                "there (%title%) or a $if() fallback")

    pre = _preflight(paths)
    out["estimated_bytes"] = _estimate_bytes(codec, quality, paths, pre)
    need = out["estimated_bytes"]
    if need:
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            free = None
        if free is not None and need > free:
            out["errors"].append(
                f"needs about {need / 1024 ** 3:.2f} GB, the destination has "
                f"{free / 1024 ** 3:.2f} GB free")
            out["failed"] = out["total"]
            return out

    apply_gains, clipping = (None, 0)
    if rg_apply:
        apply_gains, clipping = _apply_gains(
            paths, _measure_sources(ffmpeg, paths, workers, job))
        if clipping:
            out["warnings"].append(
                "%d track(s) will clip after the applied ReplayGain gain — the "
                "album's own balance is kept, so the level is not reduced per "
                "track" % clipping)
        out["warnings"].append(
            "ReplayGain was applied to the audio (album gain for a whole album, "
            "track gain otherwise) and REPLAYGAIN_* tags were stripped — a "
            "player that applied them on top would correct the audio twice")

    state = {
        "lock": threading.Lock(),
        "written": {},          # dst -> source path (collision guard)
        "written_set": set(),   # normcased dst, for prune
        "sidecars_seen": set(),
        "progress": 0,
        "groups": {},           # album folder -> playlist rows
        "rg": {},               # album folder -> [measurement]
    }

    def _folder_key(dst):
        """The playlist/RG bucket a written file belongs to: its album folder
        relative to the export root, "" for a file that landed in the root."""
        folder = os.path.dirname(dst)
        if folder == root:
            return ""
        return os.path.relpath(folder, root).replace(os.sep, "/")

    def _account(dst, path, af, seconds, counted=None):
        """Record one finished file: collision map, counters and its playlist
        row. Serialized — every worker reaches this, and the playlist order
        must not depend on which thread finished first."""
        with state["lock"]:
            state["written"][dst] = path
            state["written_set"].add(os.path.normcase(os.path.abspath(dst)))
            if counted:
                out[counted] += 1
            state["groups"].setdefault(_folder_key(dst), []).append(
                _track_playlist_row(dst, af, seconds))

    def _tick():
        with state["lock"]:
            state["progress"] += 1
            done = state["progress"]
        job_locks.publish(done, out["total"], "Exporting tracks", job=job)

    def _fail(path, message):
        with state["lock"]:
            out["failed"] += 1
            out["error_count"] += 1
            if len(out["errors"]) < 25:
                out["errors"].append(f"{os.path.basename(str(path))}: {message}")

    def _one(path):
        try:
            if cancel_requested():
                # Cancelled: nothing more is written. Checked BEFORE the file is
                # opened, so a stop never leaves a half-written audio file —
                # the price is that up to `workers` files already in flight
                # still finish.
                return
            if eq_error:
                # The run asked for an equalizer profile that is not there any
                # more (deleted, or a saved default from another library).
                # Exporting without it would hand the user a different curve
                # than the one they picked, so the track fails and says which.
                raise RuntimeError(eq_error)
            if not os.path.isfile(path):
                raise FileNotFoundError(path)
            src_ext = os.path.splitext(path)[1].lower()
            af = AudioFile(path)
            if af.audio is None:
                raise RuntimeError(af.error or "unreadable")
            # "copy" keeps the source extension; explicit codecs use theirs,
            # except FLAC→FLAC which is a bit-exact copy, not a re-encode — and
            # that shortcut is exactly what a filtering run cannot use: a copied
            # stream has no samples to filter, so such a run re-encodes instead
            # (still lossless, and the gain/EQ it was asked for is applied).
            target_ext = ext or src_ext
            if codec == "flac" and src_ext == ".flac" and not filtered_run:
                target_ext, ffmpeg_use = ".flac", False
            elif codec == "copy":
                ffmpeg_use = False
            else:
                ffmpeg_use = True
                if ffmpeg_missing:
                    raise RuntimeError("ffmpeg not found — install it from the Dependencies tab")
            album_dir = os.path.dirname(path)
            disc = ""
            if pre["disc_albums"].get(album_dir):
                disc = f"{_disc_of(af)}-"
            rel = _target_relpath(path, af, structure, music_folder, target_ext, disc, script)
            if not rel:
                # A script that names nothing for THIS file (a tag it needs is
                # empty and it has no fallback): the file fails and says why,
                # rather than landing in the export root under a bare ".mp3".
                raise RuntimeError(
                    "the folder structure produced no path for this file — add "
                    f"a $if() fallback to {script!r}")
            dst = os.path.join(root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            seconds = pre["durations"].get(path) or _duration(af)

            with state["lock"]:
                if dst in state["written"]:
                    # two tracks mapped onto the same target name — one of them
                    # would be silently lost, so fail loudly instead
                    raise RuntimeError(
                        f"target name collision with {os.path.basename(state['written'][dst])!r}")

            src_lyrics = None
            src_lyrics_tag = ""
            if lyrics_lrc or lyrics_tag:
                src_lyrics = lyrics_mod.read_lyrics(path)
                _tags = af.all_tags() or {}
                src_lyrics_tag = next(
                    (str(_tags.get(k) or "").strip() for k in _LYRICS_TAGS
                     if str(_tags.get(k) or "").strip()), "")
            if lyrics_lrc and src_lyrics:
                # The exported file's OWN name carries its lyrics beside it
                # (mlo.lyrics' one name rule: the track's own name), written
                # from the SOURCE text and canonicalised by the same formatter
                # the library's own format pass uses — so a "both" run's file
                # and tag cannot disagree. A lyric sidecar is never worth
                # failing an export for.
                try:
                    if lyrics_mod.write_lyrics_sidecar(
                            dst, lyrics_mod.format_lyrics_text(src_lyrics, cfg=cfg)):
                        with state["lock"]:
                            out["lyrics_files"] += 1
                except Exception:
                    pass

            # The files the run was ASKED for go in BEFORE the skip check:
            # re-exporting an album whose tracks are already there must still
            # complete its cover / lyrics / cue / log / description / artist
            # image. `_travels` is the run's own selection rule, asked per file,
            # so a family nobody ticked cannot be written by this path.
            if want_siblings:
                try:
                    with state["lock"]:
                        # serialized: the shared `seen` set is what stops two
                        # threads from copying the same file into the same
                        # destination at once
                        copied = _copy_siblings(cfg, path, dst,
                                                state["sidecars_seen"], _travels)
                        out["sidecars"] += copied
                except Exception:
                    pass  # a sidecar is not worth failing an export for

            if "audio" not in families:
                # The selection asked for the files BESIDE the tracks, not for
                # the tracks: the path this track would have landed at is
                # already known, and its own travelling siblings are in place.
                # Everything below writes an audio file.
                return

            rewritten = False
            if os.path.exists(dst):
                size = os.path.getsize(dst)
                if size <= 0:
                    os.remove(dst)
                elif not ffmpeg_use and size == os.path.getsize(path):
                    # byte-for-byte copy of this source: only the cover pass
                    # can still have work to do here
                    rewritten = embed_covers and _embed_cover(
                        af, path, dst, embed_quality, embed_resolution,
                        progressive, id3v2, id3v1)
                    if not rewritten:
                        _account(dst, path, af, seconds, counted="skipped")
                        return
                elif _same_export(af, dst, seconds, args):
                    if not processing or _processing_of(dst) == processing:
                        # already this source's export (a transcode, or an
                        # earlier run of the same processing) — nothing to redo
                        _account(dst, path, af, seconds, counted="skipped")
                        return
                    # This IS this source's export, but written by a DIFFERENT
                    # processing run: the user changed the ReplayGain mode or
                    # the equalizer curve. The audio depends on that choice, so
                    # the file is encoded again rather than skipped — skipping
                    # would leave the old curve on the device and report
                    # success for work that never happened.
                else:
                    # pre-existing file we cannot prove came from this source
                    # (stale preset output, or another track's file)
                    raise RuntimeError(
                        "destination already exists and was not produced from this source")

            measurement = None
            if ffmpeg_use:
                gain = None
                if rg_apply:
                    gain = (apply_gains or {}).get(path)
                    if gain is None:
                        # Nothing measured means no number to apply, and
                        # exporting the track unlevelled would be the opposite
                        # of what the run was asked for.
                        raise RuntimeError(
                            "could not measure this track's loudness — "
                            "ReplayGain 'apply' needs it to rewrite the audio")
                fd, tmp = tempfile.mkstemp(suffix=target_ext,
                                           dir=os.path.dirname(dst))
                os.close(fd)
                try:
                    # tool_path: a picker/source path past MAX_PATH is
                    # unreadable to ffmpeg itself (see mlo.subproc)
                    cmd = [ffmpeg, "-y", "-nostdin", "-hide_banner", "-nostats",
                           "-i", tool_path(path)]
                    if clean_tags:
                        # the source's leftover frames would otherwise ride
                        # along beside the canonical set written below
                        cmd += ["-map_metadata", "-1"]
                    filters = _af_chain(gain, eq_filters)
                    cmd += ["-map", "0:a:0"]
                    if filters:
                        # one chain, in the order the run promises: the
                        # ReplayGain gain first (it sets the level the rest of
                        # the chain shapes), then the equalizer's preamp and
                        # its filters. Both ride in this SAME encode — nothing
                        # is transcoded twice to apply them.
                        cmd += ["-af", ",".join(filters)]
                    cmd += args + [tmp]
                    if rg_tags:
                        # ebur128 is a pass-through analysis filter: the same
                        # decode that feeds the encoder also yields the
                        # loudness summary, so the tags cost no extra pass.
                        # Its summary is logged at info level, hence -v info
                        # (still no per-second stats: -nostats). The filter
                        # pair itself is _RG_FILTER, shared with the up-front
                        # measurement an 'apply' run does.
                        cmd += ["-map", "0:a:0", "-af", _RG_FILTER,
                                "-f", "null", "-"]
                    else:
                        cmd += ["-v", "error"]
                    # CREATE_NO_WINDOW: the app runs windowed and owns no
                    # console, so every console child (ffmpeg here) would
                    # otherwise allocate one and flash a window per file.
                    proc = subprocess.run(
                        cmd, capture_output=True, text=True,
                        creationflags=0x08000000 if os.name == "nt" else 0)
                    if proc.returncode != 0 or os.path.getsize(tmp) == 0:
                        raise RuntimeError(f"ffmpeg: {(proc.stderr or '').strip()[-200:]}")
                    shutil.move(tmp, dst)
                finally:
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                if rg_tags:
                    measurement = _rg_from_stderr(proc.stderr)
                # Any run that REWROTE the audio drops the source's ReplayGain
                # tags: after "apply" the gain is in the samples (a player that
                # also honoured the tag would correct the file twice), and after
                # an equalizer the level has moved, so the source's numbers no
                # longer describe this file. The tags mode writes fresh ones
                # right below, from the measurement of THIS encode.
                _write_tags(dst, af, id3v2=id3v2, id3v1=id3v1,
                            drop=(_RG_TAGS if filtered_run else ())
                            + (() if lyrics_tag else _LYRICS_TAGS))
                _write_processing_tag(dst, processing, id3v2=id3v2, id3v1=id3v1)
                if embed_covers:
                    _embed_cover(af, path, dst, embed_quality, embed_resolution,
                                 progressive, id3v2, id3v1)
            else:
                shutil.copy2(path, dst)
                if embed_covers:
                    # a copied file keeps its bytes; adding/changing art is a
                    # tag write, and only the cover pass forces one
                    _embed_cover(af, path, dst, embed_quality, embed_resolution,
                                 progressive, id3v2, id3v1)
                if not lyrics_tag and any(
                        str((af.all_tags() or {}).get(k) or "").strip()
                        for k in _LYRICS_TAGS):
                    # A byte copy carries the source's tags with it, so a
                    # lyrics tag this option writes as a FILE has to be
                    # REMOVED — a tag write can only add, and merely leaving it
                    # out of the inherited set would keep the copy's own copy
                    # of the lyrics beside the file. Only when the source has
                    # one, and never worth failing an export for.
                    try:
                        dst_af = _writer(dst, id3v2, id3v1)
                        if dst_af.audio is not None:
                            dst_af.defer_save(True)
                            for _k in _LYRICS_TAGS:
                                dst_af.delete_tag(_k)
                            dst_af.defer_save(False)
                    except Exception:
                        pass

            if lyrics_tag and src_lyrics and not src_lyrics_tag:
                # The source kept its lyrics in an `.lrc` rather than in a tag,
                # so nothing rode along with its other tags and the chosen mode
                # would have dropped them. Embed the text the sidecar leg read
                # — one text, one formatter, and only when the source had no
                # lyrics tag of its own to inherit.
                try:
                    dst_af = _writer(dst, id3v2, id3v1)
                    if dst_af.audio is not None:
                        dst_af.defer_save(True)
                        dst_af.set_lyrics(
                            lyrics_mod.format_lyrics_text(src_lyrics, cfg=cfg))
                        dst_af.defer_save(False)
                except Exception:
                    pass  # a lyric conversion never fails an export

            if rg_tags and measurement is None:
                # copy/FLAC-to-FLAC path, or a transcode whose summary was
                # missing: measure the source (mlo.loudness caches nothing,
                # but it returns the source's own tags when they are complete)
                try:
                    from mlo.loudness import analyze_file
                    analysis = analyze_file(path)
                except Exception:
                    analysis = None
                if analysis:
                    measurement = {"gain_db": analysis.get("gain_db"),
                                   "peak": analysis.get("peak"),
                                   "lufs": analysis.get("lufs")}
                    if measurement["lufs"] is None and measurement["gain_db"] is not None:
                        from mlo.loudness import RG2_REFERENCE_LUFS
                        measurement["lufs"] = RG2_REFERENCE_LUFS - measurement["gain_db"]
            if rg_tags and measurement and measurement.get("gain_db") is not None:
                _write_rg_tags(dst, gain_db=measurement["gain_db"],
                               peak=measurement.get("peak"),
                               id3v2=id3v2, id3v1=id3v1)

            if verify:
                problem = _verify(dst, seconds)
                if problem:
                    raise RuntimeError(problem)

            _account(dst, path, af, seconds, counted="exported")
            with state["lock"]:
                out["bytes"] += os.path.getsize(dst)
                if verify:
                    out["verified"] += 1
                if processing:
                    # What THIS run rewrote, as opposed to what the device
                    # already held: a skip is reported as a skip.
                    out["processed"] += 1
                    if eq_filters:
                        out["eq_applied"] += 1
                if rg_tags and measurement and measurement.get("lufs") is not None:
                    state["rg"].setdefault(_folder_key(dst), []).append(measurement)
        except Exception as e:
            _fail(path, e)
        finally:
            _tick()

    if workers > 1:
        import concurrent.futures as _futures
        with _futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_one, paths))
    else:
        for path in paths:
            _one(path)

    if cancel_requested():
        # Stopped at a file boundary. Everything written stays (an export is a
        # copy service — deleting finished files because the user stopped the
        # run would destroy what they may still want), and the finishing passes
        # are SKIPPED: a manifest, a playlist, a ReplayGain album pass and a
        # prune all describe a COMPLETE export, and `prune` in particular would
        # delete the audio a previous run put beside these files.
        out["cancelled"] = True
        job_locks.publish(out["total"], out["total"],
                          f"Export cancelled — {out['exported']} of "
                          f"{out['total']} file(s) written", job=job)
        return out

    # Album ReplayGain: one correction for the whole album, so its quiet and
    # loud tracks keep their relative levels. Written after every track of the
    # album exists (the measurement is only complete then). Only the tag mode
    # writes anything here — an applied run already has the album gain in its
    # samples.
    if rg_tags and state["rg"]:
        for folder, measurements in state["rg"].items():
            rows = state["groups"].get(folder, [])
            if len(measurements) != len(rows):
                # Only part of this album was (re-)measured — a half-exported
                # album must not have its album gain recomputed from a subset.
                continue
            gain, peak = _album_gain(measurements)
            if gain is None:
                continue
            for dst, _t, _a, _s in rows:
                _write_rg_tags(dst, album_gain_db=gain, album_peak_db=peak,
                               id3v2=id3v2, id3v1=id3v1)

    if want_playlists and state["groups"]:
        out["playlists"] = _write_playlists(root, state["groups"], out["exported"])

    if prune:
        removed, names = _prune(root, state["written_set"])
        out["pruned"], out["pruned_files"] = removed, names

    # A container with no tag set this app can write: WAV/AIFF exports keep
    # the audio and lose the identity, which a user picking them for a player
    # that shows artist/title needs to know.
    if codec in ("wav", "aiff") and out["exported"]:
        out["warnings"].append(
            "%s carries no tag set this app can write — the export keeps the "
            "audio only (use FLAC, ALAC or MP3 for a player that shows "
            "artist/title)" % CODECS[codec]["label"].split(" (")[0])

    # Lossy-to-lossy warning: re-encoding a lossy source into another lossy
    # codec cannot add quality back, and the user may have meant to copy.
    if codec not in ("copy", "flac", "alac", "wavpack", "wav", "aiff"):
        lossy_sources = {"mp3", "aac", "m4a", "ogg", "opus", "wma"}
        seen = set()
        for path in paths:
            ext = os.path.splitext(path)[1].lower().lstrip(".")
            if ext in lossy_sources and ext not in seen:
                seen.add(ext)
        if seen:
            out["warnings"].append(
                "re-encoding lossy sources (%s) to %s cannot restore quality — "
                "'Copy (original codec)' keeps the original bytes"
                % (", ".join(sorted(seen)), CODECS[codec]["label"]))

    # Cue sheets were mirrored BEFORE their folder's audio existed, and
    # fix_cue_filenames reads the folder to match against — so the repoint has
    # to happen here, once every track is written. Without it an album exported
    # as MP3 shipped a .cue naming the library's .flac files.
    for folder in {os.path.dirname(d) for d in state["written"]}:
        try:
            from mlo.discs import fix_cue_filenames
            fix_cue_filenames(folder, config=cfg)
        except Exception:
            pass

    # Both of these describe the FINISHED export, so they come last: the
    # manifest hashes the files as they will be shipped (a .cue that
    # fix_cue_filenames just repointed is not the one that was copied), and the
    # archive holds that same tree.
    if want_manifest and state["written"]:
        lines = _write_manifest(root)
        if not lines:
            out["warnings"].append(
                "could not write the checksum manifest at the export root")
    if zip_target:
        out["zip"] = _build_zip(zip_id, root, out["exported"])
        if out["zip"] is None:
            out["warnings"].append(
                "could not build the export archive — the export is staged in "
                "the app's data folder")

    # The run log gets the extras by name, not only the counts: a user reading
    # the finished run has to see WHAT stayed in the library and WHY.
    if out["excluded_note"]:
        job_locks.publish(out["total"], out["total"],
                          f"Export finished · {out['excluded_note']}", job=job)
    job_locks.publish(out["total"], out["total"], "Export finished", job=job)
    return out
