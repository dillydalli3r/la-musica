"""Export-to-device: copy or transcode tracks to a target drive / folder.

Powers the Export page. The user picks tracks (from a playlist or whole
albums), a destination drive, a codec and a folder structure; the exporter
either bit-copies the files (codec ``copy`` / matching FLAC) or re-encodes
them with ffmpeg and then re-writes the full tag set (plus embedded artwork
when the source carries any) so exported files are immediately usable on an
MP3 player / DAP.

Output layout options:

* ``artist_album`` — "<Artist>/<Album>/NN - Title.<ext>" (default)
* ``flat``         — "<Artist - NN - Title>.<ext>" in one folder
* ``mirror``       — the track's relative path inside the music folder,
                     re-extensioned (keeps the library's own organization)

Exports are idempotent: a destination file with the same size as the last
write is skipped on re-runs. Progress is reported through the shared
``mlo.stats.progress_hook`` so the UI header bar works exactly like it does
for library scripts.
"""
import os
import shutil
import subprocess
import tempfile

from mlo.audio import AudioFile
from mlo.naming import sanitize_path
from mlo.stats import progress_hook
from mlo.tools import detect_all_tools

# Codec table: ffmpeg audio arguments and the container extension each
# codec produces. "copy" keeps the source container (and its bytes).
# Bitrate entries accept the preset keys below OR any plain number of
# kbps ("213" -> -b:a 213k), so the UI can offer fully custom rates.
CODECS = {
    "copy": {"label": "Copy (original codec)"},
    "flac": {
        "label": "FLAC (lossless)", "ext": ".flac",
        "args": ["-c:a", "flac", "-compression_level", "8"],
    },
    "mp3": {
        "label": "MP3", "ext": ".mp3",
        # Quality selects the -q:a VBR mode or an exact bitrate below.
        "vbr": {"V0": "0", "V1": "1", "V2": "2", "V3": "3", "V4": "4", "V5": "5"},
        "cbr": {"320": "320k", "256": "256k", "192": "192k", "128": "128k"},
    },
    "aac": {
        "label": "AAC / M4A", "ext": ".m4a",
        "bitrates": {"320": "320k", "256": "256k", "192": "192k", "128": "128k"},
    },
    "opus": {
        "label": "Opus", "ext": ".opus",
        "bitrates": {"320": "320k", "256": "256k", "224": "224k", "192": "192k",
                     "160": "160k", "128": "128k", "112": "112k", "96": "96k",
                     "80": "80k", "64": "64k"},
    },
    "vorbis": {
        "label": "Ogg Vorbis", "ext": ".ogg",
        "q": {"q10": "10", "q9": "9", "q8": "8", "q7": "7", "q6": "6", "q5": "5",
              "q4": "4", "q3": "3", "q2": "2", "q1": "1", "q0": "0"},
    },
    "wav": {
        "label": "WAV (PCM, uncompressed)", "ext": ".wav",
        "bits": {"24": "pcm_s24le", "16": "pcm_s16le"},
    },
}

_DRIVETYPE = {2: "removable", 3: "fixed", 4: "network", 5: "optical"}


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


def _ffmpeg_for_codec(codec):
    """ffmpeg binary path for transcoding codecs; None for 'copy'."""
    if codec == "copy":
        return None
    tools = detect_all_tools()
    ff = (tools.get("ffmpeg") or {}).get("ffmpeg_exe")
    if not ff:
        raise RuntimeError("ffmpeg not found — install it from the Dependencies tab")
    return ff


def _codec_args(codec, quality):
    """ffmpeg output arguments for the requested codec/quality pair.

    Quality presets come from the tables above; a plain number of kbps
    ("213") is accepted as a custom bitrate for the CBR codecs, and a
    plain 0-10 for Vorbis quality — the UI's custom fields rely on it."""
    spec = CODECS.get(codec)
    if spec is None:
        raise ValueError(f"unknown codec: {codec}")
    if codec == "flac":
        try:
            level = max(0, min(8, int(quality)))
        except (TypeError, ValueError):
            level = 8
        return ["-c:a", "flac", "-compression_level", str(level)]
    if codec == "mp3":
        if quality in spec["vbr"]:
            return ["-c:a", "libmp3lame", "-q:a", spec["vbr"][quality]]
        if quality in spec["cbr"]:
            return ["-c:a", "libmp3lame", "-b:a", spec["cbr"][quality]]
    elif codec == "aac":
        if quality in spec["bitrates"]:
            return ["-c:a", "aac", "-b:a", spec["bitrates"][quality]]
    elif codec == "opus":
        if quality in spec["bitrates"]:
            return ["-c:a", "libopus", "-b:a", spec["bitrates"][quality]]
    elif codec == "vorbis":
        if quality in spec["q"]:
            return ["-c:a", "libvorbis", "-q:a", spec["q"][quality]]
    elif codec == "wav":
        return ["-c:a", spec["bits"].get(quality, "pcm_s16le")]
    # Custom numeric quality: "<n>" kbps for the CBR codecs, plain n for
    # Vorbis q — clamped to a sane range so typos can't produce garbage.
    try:
        n = int(str(quality).strip().rstrip("k").lstrip("q"))
    except (TypeError, ValueError):
        n = None
    if n is not None:
        if codec == "mp3":
            return ["-c:a", "libmp3lame", "-b:a", f"{max(32, min(320, n))}k"]
        if codec == "aac":
            return ["-c:a", "aac", "-b:a", f"{max(32, min(512, n))}k"]
        if codec == "opus":
            return ["-c:a", "libopus", "-b:a", f"{max(16, min(510, n))}k"]
        if codec == "vorbis":
            return ["-c:a", "libvorbis", "-q:a", str(max(0, min(10, n)))]
    # Per-codec defaults when the quality is unusable.
    return {
        "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
        "aac": ["-c:a", "aac", "-b:a", "256k"],
        "opus": ["-c:a", "libopus", "-b:a", "160k"],
        "vorbis": ["-c:a", "libvorbis", "-q:a", "6"],
        "wav": ["-c:a", "pcm_s16le"],
        "flac": ["-c:a", "flac", "-compression_level", "8"],
        "copy": [],
    }.get(codec, [])


def _tracknum(af):
    """Zero-padded track number ('3/12' -> '03'), '00' when untagged."""
    raw = str(af.get_tag("TRACKNUMBER") or "").strip()
    num = raw.split("/")[0].split("-")[-1].strip()
    return num.zfill(2) if num.isdigit() else "00"


def _target_relpath(path, af, structure, music_folder, ext):
    """Destination relative path for one track under the export root."""
    stem = os.path.splitext(os.path.basename(path))[0]
    artist = (af.get_tag("ALBUMARTIST") or af.get_tag("ARTIST") or "").strip() \
        or (os.path.basename(os.path.dirname(os.path.dirname(path))) if structure != "flat" else "")
    album = (af.get_tag("ALBUM") or "").strip() \
        or (os.path.basename(os.path.dirname(path)) if structure != "flat" else "")
    title = (af.get_tag("TITLE") or "").strip() or stem
    nn = _tracknum(af)

    if structure == "flat":
        base = f"{nn} - {title}"
        if artist:
            base = f"{artist} - {base}"
        return sanitize_path(f"{base}{ext}")
    if structure == "mirror" and music_folder:
        try:
            rel = os.path.relpath(path, music_folder)
        except ValueError:
            rel = None
        if rel and not rel.startswith(".."):
            return sanitize_path(os.path.splitext(rel)[0] + ext).replace("/", os.sep)
    # artist_album (default) — fall back to folder-derived names so files
    # with missing tags still export into a sensible layout.
    artist = artist or "Unknown Artist"
    album = album or "Unknown Album"
    return os.path.join(
        sanitize_path(artist), sanitize_path(album), sanitize_path(f"{nn} - {title}{ext}"),
    )


def _copy_artwork(src_af, dst_path):
    """Best-effort embedded artwork copy after a transcode. Handles the
    common pairs: FLAC pictures, MP3 APIC and MP4 covr on either side."""
    try:
        pics = []
        kind = src_af.kind
        if kind == "flac":
            pics = [(p.mime_type, p.data) for p in getattr(src_af.audio, "pictures", [])]
        elif kind == "mp3":
            for f in src_af.audio.tags.getall("APIC") or []:
                pics.append((f.mime, f.data))
        elif kind == "mp4":
            from mutagen.mp4 import MP4Cover
            # MP4Tags is dict-like (no getall); covr holds MP4Cover objects
            # (a bytes subclass) — the MIME lives in .imageformat.
            covr = (getattr(src_af.audio, "tags", None) or {}).get("covr") or []
            for f in covr:
                mime = "image/png" if getattr(f, "imageformat", None) == MP4Cover.FORMAT_PNG \
                    else "image/jpeg"
                pics.append((mime, bytes(f)))
        if not pics:
            return
        mime, data = pics[0]
        dst_ext = os.path.splitext(dst_path)[1].lower()
        if dst_ext == ".flac":
            from mutagen.flac import Picture, FLAC
            pic = Picture()
            pic.type, pic.mime, pic.data = 3, mime, data
            f = FLAC(dst_path)
            f.clear_pictures()
            f.add_picture(pic)
            f.save()
        elif dst_ext == ".mp3":
            from mutagen.id3 import APIC, ID3
            tags = ID3(dst_path)
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, data=data))
            tags.save()
        elif dst_ext == ".m4a":
            from mutagen.mp4 import MP4, MP4Cover
            f = MP4(dst_path)
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            f["covr"] = [MP4Cover(data, imageformat=fmt)]
            f.save()
    except Exception:
        pass  # artwork is cosmetic — never fail the export for it


def _write_tags(dst_path, src_af):
    """Copy the source track's full semantic tag set onto the exported
    file (mutagen handles the per-format mapping via AudioFile.set_tag)."""
    dst = AudioFile(dst_path)
    if dst.audio is None:
        return
    dst.defer_save(True)
    try:
        for k, v in (src_af.all_tags() or {}).items():
            if v is None or str(v).strip() == "":
                continue
            try:
                dst.set_tag(k, str(v))
            except Exception:
                pass  # individual exotic tags must not abort the export
    finally:
        dst.defer_save(False)


def export_tracks(cfg, paths, dest, subfolder="Music", codec="copy",
                  quality="", structure="artist_album"):
    """Run the export; returns a stats dict for the API response."""
    out = {"total": len(paths), "exported": 0, "skipped": 0, "failed": 0,
           "bytes": 0, "errors": []}
    if not paths:
        return out
    if not dest or not os.path.isdir(dest):
        out["errors"].append(f"Destination not found: {dest}")
        out["failed"] = out["total"]
        return out

    root = os.path.abspath(os.path.join(dest, subfolder.strip() or ""))
    os.makedirs(root, exist_ok=True)
    music_folder = os.path.abspath(cfg.get("music_folder") or "")
    ext = CODECS.get(codec, {}).get("ext")
    ffmpeg = _ffmpeg_for_codec(codec)
    args = _codec_args(codec, quality)
    written = {}  # dst -> source path, catches collisions within one run

    for i, path in enumerate(paths):
        if callable(progress_hook):
            try:
                progress_hook(i, out["total"], "Exporting tracks")
            except Exception:
                pass
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
            rel = _target_relpath(path, af, structure, music_folder, target_ext)
            dst = os.path.join(root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)

            if dst in written:
                # two tracks mapped onto the same target name — one of them
                # would be silently lost, so fail loudly instead
                raise RuntimeError(
                    f"target name collision with {os.path.basename(written[dst])!r}")
            if os.path.exists(dst):
                size = os.path.getsize(dst)
                if size <= 0:
                    os.remove(dst)
                elif not ffmpeg_use and size == os.path.getsize(path):
                    out["skipped"] += 1  # byte-for-byte copy of this source
                    continue
                else:
                    # pre-existing file we cannot prove came from this source
                    # (stale preset output, or another track's file)
                    raise RuntimeError(
                        "destination already exists and was not produced from this source")

            if ffmpeg_use:
                fd, tmp = tempfile.mkstemp(suffix=target_ext,
                                           dir=os.path.dirname(dst))
                os.close(fd)
                try:
                    cmd = [ffmpeg, "-y", "-v", "error", "-nostdin", "-i", path]
                    cmd += args + [tmp]
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
                _write_tags(dst, af)
                _copy_artwork(af, dst)
            else:
                shutil.copy2(path, dst)
            written[dst] = path
            out["exported"] += 1
            out["bytes"] += os.path.getsize(dst)
        except Exception as e:
            out["failed"] += 1
            if len(out["errors"]) < 25:
                out["errors"].append(f"{os.path.basename(str(path))}: {e}")

    if callable(progress_hook):
        try:
            progress_hook(out["total"], out["total"], "Export finished")
        except Exception:
            pass
    return out
