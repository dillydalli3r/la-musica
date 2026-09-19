"""Export-to-device: copy or transcode tracks to a target drive / folder.

Powers the Export page. The user picks tracks (from a playlist or whole
albums), a destination drive, a codec and a folder structure; the exporter
either bit-copies the files (codec ``copy`` / matching FLAC) or re-encodes
them with ffmpeg and then re-writes the full tag set (plus embedded artwork
when the source carries any) so exported files are immediately usable on an
MP3 player / DAP.

Output layout options:

* ``artist_album`` — "<Artist>/<Album>/NN - Title.<ext>" (default)
* ``album``        — "<Album>/NN - Title.<ext>"
* ``flat``         — "<Artist - NN - Title>.<ext>" in one folder
* ``mirror``       — the track's relative path inside the music folder,
                     re-extensioned (keeps the library's own organization)

Multi-disc albums get a "D-" prefix on the file name (the library's own
"D-NN Title" convention), so two discs can never collide on "01 - Intro".

Compatibility options (all defaulted from ``export_*`` config keys, all
overridable per run — see ``EXPORT_DEFAULTS``):

* ``embed_covers`` — embed the album cover into every exported file, with
  ``embed_cover_jpeg_quality`` / ``embed_cover_resolution`` controlling the
  JPEG quality and the longest-side cap of what gets embedded (the same
  knobs script 10 uses; ``mlo.format_all`` prepares the bytes).
* ``id3v2`` / ``id3v1`` — ID3 version for MP3 exports ("2.3" is what older
  players and car stereos read) and whether to also write an ID3v1 chunk.
* ``replaygain`` — measure each exported track with ffmpeg's EBU R128 meter
  and write the ReplayGain 2.0 tags (track + album gain/peak) so a player
  that honours them plays the export at the library's loudness. The
  measurement rides along in the transcode pass (the ``ebur128`` filter is a
  pass-through), so it costs no extra decode.
* ``clean_tags`` — write only the canonical tag set on transcodes instead of
  keeping the source's leftover frames.
* ``playlists`` — write ``.m3u8`` playlists (UTF-8, relative paths) next to
  the exported albums plus one for the whole export.
* ``sidecars`` — mirror cover.*/description.txt/artist image/.lrc/.cue/.log.
* ``verify`` — re-open every written file and prove it parses with the
  source's duration before calling the export done.
* ``prune`` — sync mode: delete audio files under the export root that this
  run did not write.
* ``workers`` — parallel transcode/copy workers (0 = automatic).

Exports are idempotent: a destination that already is this source's export
(same duration and track identity) is skipped on re-runs. Progress is
reported through the shared ``mlo.stats.progress_hook`` so the UI header bar
works exactly like it does for library scripts.
"""
import math
import os
import shutil
import subprocess
import tempfile
import threading

from mlo.audio import AudioFile
from mlo.naming import sanitize_path
from mlo.stats import progress_hook, worker_count
from mlo.subproc import tool_path
from mlo.tools import detect_all_tools

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
    separators are dropped and traversal is neutralised instead of trusted."""
    text = str(name or "").strip().replace("/", " ").replace("\\", " ")
    text = text.replace("..", "_").strip(" .")
    return text[:120] or "Music"


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

# Run options and their defaults. ``export_<name>`` in the config holds the
# saved default for each; a per-run value (the Export page's form) wins.
EXPORT_DEFAULTS = {
    "embed_covers": True,
    "embed_cover_jpeg_quality": 90,
    "embed_cover_resolution": 1200,
    "id3v2": "2.3",
    "id3v1": False,
    "replaygain": False,
    "clean_tags": True,
    "playlists": True,
    "sidecars": True,
    "verify": True,
    "prune": False,
    "workers": 0,
}

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


def option_int(cfg, opts, name, low=0, high=None):
    """An integer run option, clamped (bad input falls back to the default)."""
    try:
        value = int(option(cfg, opts, name))
    except (TypeError, ValueError):
        value = int(EXPORT_DEFAULTS[name])
    value = max(low, value)
    return min(high, value) if high is not None else value


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


def _target_relpath(path, af, structure, music_folder, ext, disc=""):
    """Destination relative path for one track under the export root."""
    stem = os.path.splitext(os.path.basename(path))[0]
    artist = (af.get_tag("ALBUMARTIST") or af.get_tag("ARTIST") or "").strip() \
        or (os.path.basename(os.path.dirname(os.path.dirname(path))) if structure != "flat" else "")
    album = (af.get_tag("ALBUM") or "").strip() \
        or (os.path.basename(os.path.dirname(path)) if structure != "flat" else "")
    title = (af.get_tag("TITLE") or "").strip() or stem
    nn = _tracknum(af)

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
    if structure == "flat":
        base = f"{disc}{nn} - {title}"
        if artist:
            base = f"{artist} - {base}"
        return sanitize_path(f"{base}{ext}")
    if structure == "album":
        album = album or "Unknown Album"
        return os.path.join(sanitize_path(album),
                            sanitize_path(f"{disc}{nn} - {title}{ext}"))
    # artist_album (default) — fall back to folder-derived names so files
    # with missing tags still export into a sensible layout.
    artist = artist or "Unknown Artist"
    album = album or "Unknown Album"
    return os.path.join(
        sanitize_path(artist), sanitize_path(album),
        sanitize_path(f"{disc}{nn} - {title}{ext}"),
    )


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


# Text sidecars that belong to a track and travel with it.
_TRACK_SIDECAR_EXTS = (".lrc", ".cue", ".log")


def _mirror_sidecars(cfg, src_track, dst_track, seen):
    """Mirror an exported track's sidecars into the exported album folder.

    An exported copy is meant to carry what grading expects: the album cover
    (cover.*), the album description (description.txt) and the track's own
    .lrc/.cue/.log go next to the exported tracks, and the artist image
    (artist.jpg, which lives one level ABOVE the album folder in the library)
    is mirrored one level above the exported album folder. Embedded artwork
    already travels inside the audio file on the transcode path; this covers
    the `copy` path too, where the bytes are untouched.

    Returns the number of files copied."""
    from mlo.grader import COVER_NAMES
    from mlo.paths import ALBUM_SIDECAR_NAMES, library_root

    src_dir = os.path.dirname(src_track)
    dst_dir = os.path.dirname(dst_track)
    stem = os.path.splitext(os.path.basename(src_track))[0]
    copied = 0

    try:
        lowered = {e.lower(): e for e in os.listdir(src_dir)}
    except OSError:
        lowered = {}

    # Track-level sidecar ("01 - Song.lrc"), then the album-level artifacts:
    # the rip's .cue/.log are normally named after the ALBUM, not the track,
    # and grading wants them next to the exported tracks either way.
    for ext in _TRACK_SIDECAR_EXTS:
        name = stem + ext
        if _copy_once(os.path.join(src_dir, name), os.path.join(dst_dir, name), seen):
            copied += 1
    for real in sorted(lowered.values()):
        if os.path.splitext(real)[1].lower() in (".cue", ".log"):
            if _copy_once(os.path.join(src_dir, real),
                          os.path.join(dst_dir, real), seen):
                copied += 1

    for name in tuple(COVER_NAMES) + tuple(ALBUM_SIDECAR_NAMES):
        real = lowered.get(name)
        if real and _copy_once(os.path.join(src_dir, real),
                               os.path.join(dst_dir, real), seen):
            copied += 1

    # Artist image, one level above the album. Skipped when the album sits
    # directly in the library root (no artist folder to mirror).
    parent = os.path.dirname(os.path.abspath(src_dir))
    root = library_root(cfg.get("music_folder"))
    if root and os.path.normcase(parent) != os.path.normcase(os.path.abspath(root)):
        try:
            from mlo import artistdata
            image = artistdata.image_path(parent)
        except Exception:
            image = None
        if image:
            if _copy_once(image,
                          os.path.join(os.path.dirname(dst_dir), os.path.basename(image)),
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


def _write_tags(dst_path, src_af, id3v2="2.4", id3v1=False):
    """Copy the source track's full semantic tag set onto the exported
    file (mutagen handles the per-format mapping via AudioFile.set_tag).

    ``id3v2``/``id3v1`` only mean anything for ID3 containers (MP3, raw AAC)
    and are what older players need: v2.3 instead of mutagen's v2.4 default,
    plus optionally an ID3v1 chunk. Returns the number of tags written."""
    dst = _writer(dst_path, id3v2, id3v1)
    if dst.audio is None:
        return 0
    written = 0
    dst.defer_save(True)
    try:
        for k, v in (src_af.all_tags() or {}).items():
            if v is None or str(v).strip() == "":
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
            plan.append((folder, f"{sanitize_path(name) or 'album'}.m3u8", rows))
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


def export_tracks(cfg, paths, dest, subfolder="Music", codec="copy",
                  quality="", structure="artist_album", **opts):
    """Run the export; returns a stats dict for the API response.

    ``opts`` holds per-run overrides of the compatibility options (see
    ``EXPORT_DEFAULTS``); each falls back to its ``export_<name>`` config value.

    Raises ValueError when the destination is unusable in a way that would
    destroy library data (it lies inside the music folder) — the caller
    turns that into a 4xx, and a run must never start."""
    opts = {k: v for k, v in opts.items() if v is not None}
    out = {"total": len(paths), "exported": 0, "skipped": 0, "failed": 0,
           "bytes": 0, "sidecars": 0, "playlists": 0, "verified": 0,
           "pruned": 0, "pruned_files": [], "warnings": [], "errors": [],
           "error_count": 0, "estimated_bytes": None}
    if not paths:
        return out
    if not dest or not os.path.isdir(dest):
        out["errors"].append(f"Destination not found: {dest}")
        out["failed"] = out["total"]
        return out

    root = os.path.abspath(os.path.join(dest, safe_subfolder(subfolder)))
    music_folder = os.path.abspath(cfg.get("music_folder") or "")
    if music_folder and (root == music_folder
                         or root.startswith(music_folder + os.sep)):
        raise ValueError(
            "destination is inside the music folder — export to a device or a "
            "folder outside the library")
    os.makedirs(root, exist_ok=True)

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
    replaygain = bool(option(cfg, opts, "replaygain"))
    clean_tags = bool(option(cfg, opts, "clean_tags"))
    want_playlists = bool(option(cfg, opts, "playlists"))
    mirror_sidecars = bool(option(cfg, opts, "sidecars"))
    verify = bool(option(cfg, opts, "verify"))
    prune = bool(option(cfg, opts, "prune"))
    workers = option_int(cfg, opts, "workers", 0, 64) or worker_count(
        cfg, default=max(2, (os.cpu_count() or 4) // 2), maximum=8, items=len(paths))
    workers = min(workers, max(1, len(paths)))

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
        if callable(progress_hook):
            try:
                progress_hook(done, out["total"], "Exporting tracks")
            except Exception:
                pass

    def _fail(path, message):
        with state["lock"]:
            out["failed"] += 1
            out["error_count"] += 1
            if len(out["errors"]) < 25:
                out["errors"].append(f"{os.path.basename(str(path))}: {message}")

    def _one(path):
        try:
            if not os.path.isfile(path):
                raise FileNotFoundError(path)
            src_ext = os.path.splitext(path)[1].lower()
            af = AudioFile(path)
            if af.audio is None:
                raise RuntimeError(af.error or "unreadable")
            # "copy" keeps the source extension; explicit codecs use theirs,
            # except FLAC→FLAC which is a bit-exact copy, not a re-encode.
            target_ext = ext or src_ext
            if codec == "flac" and src_ext == ".flac":
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
            rel = _target_relpath(path, af, structure, music_folder, target_ext, disc)
            dst = os.path.join(root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            seconds = pre["durations"].get(path) or _duration(af)

            with state["lock"]:
                if dst in state["written"]:
                    # two tracks mapped onto the same target name — one of them
                    # would be silently lost, so fail loudly instead
                    raise RuntimeError(
                        f"target name collision with {os.path.basename(state['written'][dst])!r}")

            # Sidecars BEFORE the skip check: re-exporting an album whose
            # tracks are already there must still complete its cover / lyrics
            # / cue / log / description / artist image.
            if mirror_sidecars:
                try:
                    with state["lock"]:
                        # serialized: the shared `seen` set is what stops two
                        # threads from copying the same sidecar into the same
                        # destination at once
                        copied = _mirror_sidecars(cfg, path, dst, state["sidecars_seen"])
                        out["sidecars"] += copied
                except Exception:
                    pass  # sidecars are not worth failing an export for

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
                    # already this source's export (transcode or an earlier
                    # run of the same preset) — nothing to redo
                    _account(dst, path, af, seconds, counted="skipped")
                    return
                else:
                    # pre-existing file we cannot prove came from this source
                    # (stale preset output, or another track's file)
                    raise RuntimeError(
                        "destination already exists and was not produced from this source")

            measurement = None
            if ffmpeg_use:
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
                    cmd += ["-map", "0:a:0"] + args + [tmp]
                    if replaygain:
                        # ebur128 is a pass-through analysis filter: the same
                        # decode that feeds the encoder also yields the
                        # loudness summary, so ReplayGain costs no extra pass.
                        # Its summary is logged at info level, hence -v info
                        # (still no per-second stats: -nostats).
                        cmd += ["-map", "0:a:0", "-af", "ebur128=peak=true",
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
                if replaygain:
                    measurement = _rg_from_stderr(proc.stderr)
                _write_tags(dst, af, id3v2=id3v2, id3v1=id3v1)
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

            if replaygain and measurement is None:
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
            if replaygain and measurement and measurement.get("gain_db") is not None:
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
                if replaygain and measurement and measurement.get("lufs") is not None:
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

    # Album ReplayGain: one correction for the whole album, so its quiet and
    # loud tracks keep their relative levels. Written after every track of the
    # album exists (the measurement is only complete then).
    if replaygain and state["rg"]:
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

    if callable(progress_hook):
        try:
            progress_hook(out["total"], out["total"], "Export finished")
        except Exception:
            pass
    return out
