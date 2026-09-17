"""Lossless video remux — script 11: any video container -> MKV.

Music libraries increasingly carry promo clips and music videos alongside
the tracks (VOB rips, MKV/AVI/WMV downloads, TS captures...). Matroska can
hold virtually every stream codec in common circulation, so this script
normalizes every video file while keeping quality fully intact:

* Pass 1 — video is copied **bit-exact**; every audio stream is re-encoded
  to FLAC (lossless from the decoded source, compression level from
  ``video_flac_level``) so the audio matches the library's FLAC standard.
* Pass 2 (caption rescue) — text captions the MKV muxer refuses verbatim
  are converted to SubRip so they are kept, never dropped.
* Pass 3 (last resort, ``video_reencode_incompatible``) — a video codec the
  muxer still refuses is re-encoded to H.264; audio stays FLAC.
* Captions are NEVER removed: every subtitle stream is mapped in every
  pass, and the output is verified to carry the same subtitle stream count
  as the source (a remux that would drop captions fails instead).
* Chapters are preserved: ffmpeg copies input 0's chapters (-map_chapters 0)
  and the output is verified to still carry them (probe_chapters, ffprobe
  -show_chapters). Per-chapter titles survive where the container carries them
  — a raw .vob/.m2ts rip has its chapter marks in the disc's IFO/playlist, not
  in the stream, so such rips report none and none are invented.
* Output is verified with ffprobe (video present, audio and subtitle stream
  counts match, chapters kept, duration within 0.5%) before anything replaces
  the source. The original file is deleted after a verified remux
  (``video_remove_original``, on by default) — a stray VOB whose same-stem
  MKV already exists from an earlier run is verified by duration match and
  then removed too.

* Inputs are every video container the library knows (VIDEO_EXTS, the same
  set /api/videos/scan lists) — including .mp4/.m4v, which are only rewritten
  when ``video_process_mp4`` is on because they already play and tag natively.

Config keys: video_reencode_incompatible, video_crf, video_preset,
video_flac_level, video_remove_original, video_process_mp4.
"""

import json
import os
import tempfile
import threading

from .paths import DEPS_DIR
from .stats import (
    _collect_targets,
    _make_pbar,
    _walk_files,
    new_stats,
    worker_count,
)
from .subproc import run_tool
from .tools import detect_all_tools
from .ui import Color, c, log, print_header

# Every container this script (and /api/videos/scan, which lists the same set)
# accepts as a video. .mp4/.m4v are listed here because the library treats them
# as music videos (mlo.paths.LIB_VIDEO_EXTS) — but they already play and tag
# natively, so run_remux_videos() only rewrites them when video_process_mp4 is
# on (REMUX_GATED_EXTS).
VIDEO_EXTS = (
    ".vob", ".mpg", ".mpeg", ".m2v", ".mp4", ".m4v", ".vro", ".mod", ".tod",
    ".ts", ".m2ts", ".mts", ".m2t",
    ".mkv", ".avi", ".divx", ".wmv", ".asf", ".mov", ".flv", ".f4v",
    ".webm", ".ogv", ".3gp", ".3g2", ".rm", ".rmvb", ".dv", ".amv", ".nsv",
    ".evo", ".ogm", ".tp", ".trp", ".mxf", ".gxf",
)

# Containers the remux script leaves alone unless video_process_mp4 is on.
REMUX_GATED_EXTS = (".mp4", ".m4v")


def remux_input_exts(config):
    """Extensions script 11 walks: everything except the containers already
    playable/taggable as-is, unless the user opted into processing MP4."""
    if config.get("video_process_mp4", False):
        return VIDEO_EXTS
    return tuple(e for e in VIDEO_EXTS if e not in REMUX_GATED_EXTS)

X264_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)


def _ffprobe_json(ffprobe_exe, path, timeout=60):
    """Probe a media file (format, streams and chapters), parsed JSON or None."""
    try:
        proc = run_tool(
            [ffprobe_exe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", "-show_chapters", path],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None


def _stream_info(path, ffprobe_exe):
    """(video_codec, [audio_codecs], [subtitle_codecs], duration) or None."""
    data = _ffprobe_json(ffprobe_exe, path)
    if not data:
        return None
    video = None
    audio, subs = [], []
    for st in data.get("streams") or []:
        sttype = st.get("codec_type")
        if sttype == "video":
            # cover-art attached pictures (mjpeg/png) are not the program
            disp = st.get("disposition") or {}
            if st.get("codec_name") in ("mjpeg", "png") and disp.get("attached_pic"):
                continue
            if video is None:
                video = st.get("codec_name")
        elif sttype == "audio":
            audio.append(st.get("codec_name"))
        elif sttype == "subtitle":
            subs.append(st.get("codec_name"))
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return video, audio, subs, duration


def probe_chapters(path, ffprobe_exe=None):
    """[{title, start, end}] for a video's chapters, [] when it has none.

    DVD-Video/Blu-ray chapter marks live in the disc's IFO/playlist, not in
    the streams of a raw .vob/.m2ts, so rips without chapter atoms report
    nothing here. Containers that do carry them (MKV/MP4/some TS) keep their
    per-chapter titles through the remux — ffmpeg copies input 0's chapters
    (-map_chapters 0). Untitled chapters are named "Chapter N" so the discs
    that put the *song* in each chapter are addressable by the matching UI.
    """
    if ffprobe_exe is None:
        ffprobe_exe = (detect_all_tools().get("ffmpeg") or {}).get("ffprobe_exe")
    if not ffprobe_exe:
        return []
    data = _ffprobe_json(ffprobe_exe, path)
    if not data:
        return []
    out = []
    for n, ch in enumerate(data.get("chapters") or [], start=1):
        def _secs(key):
            try:
                return float(ch.get(key))
            except (TypeError, ValueError):
                return None
        out.append({
            "title": (ch.get("tags") or {}).get("title") or f"Chapter {n}",
            "start": _secs("start_time"),
            "end": _secs("end_time"),
        })
    return out


def _unique_dest(src, out_ext=".mkv"):
    """Destination path next to the source: same stem + out_ext, never
    overwriting an existing file ('name (2).mkv')."""
    stem = os.path.splitext(src)[0]
    dest = stem + out_ext
    if os.path.normcase(dest) == os.path.normcase(src):
        stem += " (video)"
        dest = stem + out_ext
    n = 2
    while os.path.exists(dest):
        dest = f"{stem} ({n}){out_ext}"
        n += 1
    return dest


# Serializes dest-name allocation + final replace so two threads converting
# same-stem sources (video.vob + video.mkv) can't collide.
_DEST_LOCK = threading.Lock()


def _ffmpeg_args(mode, cfg):
    """Encoder arguments for a remux pass. Mode "2" = copied video + FLAC
    audio + copied captions; "2s" = copied video + FLAC audio + captions
    converted to SRT (rescue for text caption codecs the MKV muxer refuses);
    "3" = h264 video + FLAC audio. Captions are NEVER dropped: every pass
    maps all subtitle streams."""
    try:
        crf = max(0, min(51, int(cfg.get("video_crf", 18))))
    except (TypeError, ValueError):
        crf = 18
    preset = str(cfg.get("video_preset", "medium") or "medium").lower()
    if preset not in X264_PRESETS:
        preset = "medium"
    try:
        flac_level = max(0, min(8, int(cfg.get("video_flac_level", 8))))
    except (TypeError, ValueError):
        flac_level = 8

    cmd = []
    cmd += ["-c:v", "copy" if mode != "3" else "libx264"]
    if mode == "3":
        cmd += ["-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
    # FLAC keeps every decoded audio stream bit-perfect (any channel
    # layout / bit depth) — lossless, at the configured compression level.
    cmd += ["-c:a", "flac", "-strict", "-2", "-compression_level", str(flac_level)]
    # Captions: copied verbatim; the "2s" rescue pass re-encodes text
    # captions to SubRip (content preserved) when the container refuses
    # the source codec. Bitmap captions (DVD/PGS/DVB) can only be copied.
    cmd += ["-c:s", "copy" if mode != "2s" else "srt"]
    return cmd


def remux_video(src, dest, ffmpeg_exe, ffprobe_exe, cfg):
    """Remux one video file to MKV. Returns (ok, message).

    ``dest`` must not exist (callers pass a temp path); on success the
    caller os.replace()s it into place after verification. Video is copied
    bit-exact and every audio stream is re-encoded to FLAC (lossless);
    when the muxer refuses the video codec a config-gated H.264 pass
    follows. Subtitle streams are always mapped and copied — the output is
    verified to carry the same caption streams as the source.
    """
    # Forward slashes: with backslash paths ffmpeg's VOB/VOB-VR demuxer can
    # expose phantom audio substreams (unknown codec parameters) that kill
    # the mux. Windows ffmpeg accepts forward slashes everywhere.
    src = str(src).replace("\\", "/")
    dest = str(dest).replace("\\", "/")
    info = _stream_info(src, ffprobe_exe)
    if info is None:
        return False, "unreadable by ffprobe"
    vcodec, acodecs, scodecs, duration = info
    if vcodec is None and not acodecs:
        return False, "no audio or video streams"
    if vcodec is None:
        return False, "no video stream"

    # H.264 fallback only ever runs when the user kept the safety valve on.
    allow_reencode = bool(cfg.get("video_reencode_incompatible", True))

    # Regenerate input PTS — DVD-VR VOBs often carry pcm_dvd packets with
    # unknown timestamps that abort the mux otherwise. Input flags must
    # precede -i. Only video/audio/subtitle streams are mapped — data
    # streams (DVD navigation packets) can't be carried by any muxer.
    # Subtitles are mapped unconditionally: captions are never removed.
    input_flags = ["-fflags", "+genpts"]
    stream_maps = ["-map", "0:v", "-map", "0:a?", "-map", "0:s?"]

    last_err = ""
    src_chapters = probe_chapters(src, ffprobe_exe)
    for mode in ("2", "2s", "3") if allow_reencode else ("2", "2s"):
        cmd = ([ffmpeg_exe, "-y", "-v", "error", "-nostdin"]
               + input_flags + ["-i", src] + stream_maps)
        cmd += _ffmpeg_args(mode, cfg)
        # Chapters are copied from the source explicitly (ffmpeg's default,
        # spelled out so a future option change can't silently drop them).
        cmd += ["-map_chapters", "0", "-f", "matroska", dest]
        try:
            proc = run_tool(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60 * 120)
        except Exception as e:
            last_err = f"ffmpeg failed: {e}"
            continue
        if proc.returncode != 0:
            last_err = "; ".join((proc.stderr or "").strip().splitlines()[-3:]) \
                or f"ffmpeg exit {proc.returncode}"
            continue

        # Verification: the output must have video, the same number of audio
        # streams, the SAME subtitle (caption) streams — a remux that would
        # drop captions fails rather than losing them — and (when known) a
        # duration within 0.5% / 1 s of the source.
        out_info = _stream_info(dest, ffprobe_exe)
        if out_info is None:
            last_err = "output failed verification (ffprobe)"
            continue
        ov, oa, osubs, odur = out_info
        if ov is None:
            last_err = "output has no video stream"
            continue
        if len(oa) != len(acodecs):
            last_err = f"audio stream count changed ({len(acodecs)} -> {len(oa)})"
            continue
        if len(osubs) != len(scodecs):
            last_err = f"subtitle streams dropped ({len(scodecs)} -> {len(osubs)}) — refusing to lose captions"
            continue
        if src_chapters:
            out_chapters = probe_chapters(dest, ffprobe_exe)
            if len(out_chapters) < len(src_chapters):
                last_err = (f"chapters dropped ({len(src_chapters)} -> "
                            f"{len(out_chapters)})")
                continue
        if duration and odur and abs(duration - odur) > max(1.0, 0.005 * duration):
            last_err = f"duration changed ({duration:.2f}s -> {odur:.2f}s)"
            continue
        if mode == "2":
            return True, f"video copied, audio -> FLAC ({' + '.join(acodecs) if acodecs else '?'})"
        if mode == "2s":
            return True, "video copied, audio -> FLAC, captions -> SRT"
        return True, (f"{vcodec} video re-encoded to h264 "
                      f"(crf {cfg.get('video_crf', 18)}), audio -> FLAC")
    return False, last_err or "remux failed"


def _same_program(src, mkv, ffprobe_exe):
    """Best-effort check that *mkv* looks like the remux product of *src*:
    both probe, both carry video, and their durations match closely. Used
    before deleting a stray original whose same-stem MKV already exists."""
    a = _stream_info(src, ffprobe_exe)
    b = _stream_info(mkv, ffprobe_exe)
    if not a or not b:
        return False
    if a[0] is None or b[0] is None:
        return False
    da, db = a[3], b[3]
    if not da or not db:
        return False
    return abs(da - db) <= max(1.0, 0.005 * da)


def run_remux_videos(config):
    """Script 11 — convert every video file in the target set to MKV."""
    stats = new_stats()
    stats["converted"] = 0
    stats["removed_originals"] = 0

    tools = detect_all_tools()
    ffmpeg = (tools.get("ffmpeg") or {}).get("ffmpeg_exe")
    ffprobe = (tools.get("ffmpeg") or {}).get("ffprobe_exe")
    if not ffmpeg or not ffprobe or not os.path.isfile(ffmpeg):
        log(c("ERROR: ffmpeg/ffprobe not available — install Dependencies first.", Color.RED))
        stats["errors"].append("ffmpeg/ffprobe not available")
        return stats

    print_header("Video Remux (MKV)")
    reenc = bool(config.get("video_reencode_incompatible", True))
    remove_original = bool(config.get("video_remove_original", True))
    log(
        f"ffmpeg: {ffmpeg}\n"
        f"streams: video copied · audio -> FLAC (lossless, level {config.get('video_flac_level', 8)}) · "
        f"captions always kept · chapters kept · h264 fallback {'on' if reenc else 'off'} · "
        f"originals: {'removed after verified remux' if remove_original else 'kept'}"
    )

    folder = os.path.abspath(config["music_folder"] or os.getcwd())
    targets = config.get("targets")
    # An explicit target list IS the user's request ("remux THESE files", the
    # album page's Remux button), so it may name MP4/M4V even when a
    # library-wide pass is gated off by video_process_mp4 — that switch only
    # scopes what an unattended script 11 walk picks up by itself.
    exts = VIDEO_EXTS if targets else remux_input_exts(config)
    if targets:
        files = _collect_targets(targets, exts)
    else:
        files = sorted(_walk_files(folder, exts))

    # Deduplicate case-insensitively (Windows) — selections can double-count.
    seen = {}
    for f in files:
        seen.setdefault(os.path.normcase(f), f)
    files = sorted(seen.values())

    if not files:
        log("No video files found.")
        return stats
    log(f"{len(files)} video file(s) found")

    workers = worker_count(config, default=min(4, os.cpu_count() or 1),
                           items=len(files))
    pbar = _make_pbar(total=len(files), desc="Remuxing videos")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _job(path):
        """(path, dest, message, bytes_added, skipped).

        ``skipped`` is explicit: "already remuxed … remove failed" is a real
        failure that happens to start with the same words as the skip case,
        so it must never be classified by its message text.
        """
        ext = os.path.splitext(path)[1].lower()
        if ext == ".mkv":
            # Already the target container — nothing to normalize.
            return path, None, "already MKV", 0, True
        if os.path.exists(os.path.splitext(path)[0] + ".mkv"):
            # A verified remux from an earlier run is already in place —
            # keep re-runs idempotent instead of piling up "(2).mkv" copies.
            # The stray original (e.g. a leftover VOB) is removed once the
            # existing MKV is duration-verified as its remux product.
            mkv = os.path.splitext(path)[0] + ".mkv"
            if remove_original and _same_program(path, mkv, ffprobe):
                try:
                    before = os.path.getsize(path)
                    with _DEST_LOCK:
                        os.remove(path)
                    stats["removed_originals"] += 1
                    stats["total_bytes_removed"] += before
                    return path, None, "already remuxed — stray original removed", 0, True
                except OSError as e:
                    return path, None, (
                        f"already remuxed (same-stem MKV exists); remove failed: {e}"
                    ), 0, False
            return path, None, "already remuxed (same-stem MKV exists)", 0, True
        # Unique temp file in the same directory (same volume => the final
        # os.replace is atomic). mkstemp guarantees no two jobs share one.
        fd, tmp = tempfile.mkstemp(
            prefix=".remux_", suffix=".mkv", dir=os.path.dirname(path) or ".")
        os.close(fd)
        try:
            ok, msg = remux_video(path, tmp, ffmpeg, ffprobe, config)
            if not ok:
                return path, None, msg, 0, False
            with _DEST_LOCK:
                dest = _unique_dest(path)
                os.replace(tmp, dest)
            try:
                added = os.path.getsize(dest)
            except OSError:
                added = 0
            return path, dest, msg, added, False
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_job, f) for f in files]
        for fut in as_completed(futures):
            try:
                path, dest, msg, added, skipped = fut.result()
            except Exception as e:
                stats["error_count"] += 1
                stats["errors"].append(str(e))
                pbar.update(1)
                continue
            name = os.path.basename(path)
            if skipped:
                stats["skipped_count"] += 1
                if msg == "already remuxed — stray original removed":
                    log(f"  - {name}: stray original removed (same-stem MKV verified)")
                else:
                    log(c(f"  = {name}: {msg}", Color.YELLOW))
            elif dest is None:
                # A remux that produced nothing — including the "already
                # remuxed … remove failed" case, which is a failure, not a skip.
                stats["error_count"] += 1
                stats["errors"].append(f"{name}: {msg}")
                log(c(f"  ! {name}: {msg}", Color.YELLOW))
            else:
                stats["converted"] += 1
                stats["modified_count"] += 1
                stats["total_bytes_added"] += added
                if remove_original:
                    try:
                        before = os.path.getsize(path)
                        with _DEST_LOCK:
                            os.remove(path)
                        stats["removed_originals"] += 1
                        stats["total_bytes_removed"] += before
                        log(f"  + {name} -> {os.path.basename(dest)} ({msg}; original removed)")
                    except OSError as e:
                        stats["error_count"] += 1
                        stats["errors"].append(f"{name}: remove failed: {e}")
                        log(c(f"  ! {name}: remuxed but the original could not be removed: {e}",
                              Color.YELLOW))
                else:
                    log(f"  + {name} -> {os.path.basename(dest)} ({msg})")
            pbar.update(1)

    pbar.close()
    log(
        f"Done: {stats['converted']} converted · {stats['skipped_count']} skipped · "
        f"{stats['error_count']} errors"
    )
    return stats
