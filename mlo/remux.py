"""Lossless video remux — script 11: any video container -> MKV.

Music libraries increasingly carry promo clips and music videos alongside
the tracks (VOB rips, MKV/AVI/WMV downloads, TS captures...). Matroska can
hold virtually every stream codec in common circulation, so this script
normalizes every video file while keeping quality fully intact:

* Pass 1 — video is copied **bit-exact**; audio that is already lossless is
  re-encoded to FLAC (compression level from ``video_flac_level``) so it
  matches the library's FLAC standard, while lossy audio (AC3/DTS/AAC…) is
  copied byte-for-byte under ``video_lossy_audio_copy`` — re-encoding it
  cannot restore a sample and only inflates the file.
* Pass 2 (caption rescue) — text captions the MKV muxer refuses verbatim
  are converted to SubRip so they are kept, never dropped.
* Pass 3 (last resort, ``video_reencode_incompatible``) — a video codec the
  muxer still refuses is re-encoded to H.264; audio keeps the same rule.
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
* **Disc structures are recognized, not walked file by file** (mlo.videodisc).
  A folder holding a ``VIDEO_TS`` (DVD-Video: ``VTS_nn_1.VOB``,
  ``VTS_nn_2.VOB``, … — the title's program stream cut at 1 GB, part 0 being
  the menu) or a ``BDMV/STREAM`` (Blu-ray: ``nnnnn.m2ts`` transport streams,
  ordered by ``PLAYLIST/nnnnn.mpls``) is one disc, and its LONGEST title is
  remuxed as ONE input: the chosen streams are written to a concat-demuxer
  list and handed to ffmpeg as a single input (``-f concat -safe 0``), so the
  streams are still COPIED, never re-encoded, and the verification, caption and
  chapter rules above apply to the disc's product exactly as to a single file.
  The streams that were consumed are removed after the verified remux, the same
  way a single source file is (``video_remove_original``).
* The disc's own streams win over a compressed derivative shipped beside them
  (a "700 MB rip" next to the ``VIDEO_TS`` folder): the derivative is left
  where it is, is never counted as the folder's feature, and the log says so.
  ``prefer_disc_streams`` (on by default) is that preference — with it off,
  disc structures are left to the ordinary per-file path and the log says why.
  A re-encode with NO disc structure beside it is never claimed as a disc: it
  takes the ordinary single-file path, with no disc treatment at all.
* When the structure does not say which title is the feature — two titles
  within 5 % of each other, a Blu-ray playlist that cannot be read or that
  plays only part of a clip, a title set whose parts are not a 1…N run, or an
  ``.iso`` (nothing here can read inside one) — the script picks NOTHING, says
  why in the log, and raises the app's own prompt for that folder
  (``server.import_autonomy``: the notification bell, GET /api/import/prompts
  and the queue's "Needs you" row), whose body names the candidates and their
  durations. Chapters are still NOT read from a disc: a DVD's live in its
  IFO's program-chain table and a Blu-ray's in the ``.mpls`` PlayListMark
  section, and neither is read here, so a disc structure's remux carries the
  chapters its streams carry (a raw rip's: none) and none are invented. What
  IS read of the playlist is its play items — which clips form which title, in
  order, and how long each runs.

Config keys: video_reencode_incompatible, video_lossy_audio_copy, video_crf,
video_preset, video_flac_level, video_remove_original, video_process_mp4,
prefer_disc_streams.
"""

import json
import os
import tempfile
import threading
import traceback

from . import videodisc
from .paths import LIB_VIDEO_DISC_NAMES
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

# Audio codecs that lose nothing when re-encoded as FLAC, plus every raw PCM
# variant. `dts` is deliberately NOT here: ffprobe reports DTS-HD MA (lossless)
# and plain DTS core (lossy) as the same codec name, and re-encoding a lossy
# core is exactly what video_lossy_audio_copy exists to stop.
LOSSLESS_AUDIO_CODECS = frozenset({
    "flac", "alac", "truehd", "mlp", "wavpack", "tta", "ape", "tak", "als",
})


def _is_lossless_audio(codec):
    """Whether an ffprobe codec name is a lossless one."""
    name = str(codec or "").lower()
    return name.startswith("pcm_") or name in LOSSLESS_AUDIO_CODECS


def _input_args(src, concat=False):
    """ffmpeg's ``-i`` half of a command line, input flags included.

    *concat* reads *src* as a concat-demuxer list file (mlo.videodisc writes
    one for a disc structure's chosen streams) instead of as media: several
    files then reach ffmpeg as ONE input, in the list's order, every stream
    copied. ``-safe 0`` is what allows the absolute paths a library has.
    """
    # Regenerate input PTS — DVD-VR VOBs often carry pcm_dvd packets with
    # unknown timestamps that abort the mux otherwise. Input flags must
    # precede -i.
    flags = ["-fflags", "+genpts"]
    if concat:
        flags += ["-f", "concat", "-safe", "0"]
    return flags + ["-i", src]


def _ffprobe_json(ffprobe_exe, path, timeout=60, concat=False):
    """Probe a media file (format, streams and chapters), parsed JSON or None.

    *concat* probes a concat list file (see :func:`_input_args`) — the same
    probe ffprobe makes of the media, of the whole joined program.
    """
    try:
        proc = run_tool(
            [ffprobe_exe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", "-show_chapters"]
            + (["-f", "concat", "-safe", "0"] if concat else [])
            + [path],
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


def _streams_from(data):
    """(video_codec, [audio_codecs], [subtitle_codecs], duration) from a
    probe payload, or None."""
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


def _stream_info(path, ffprobe_exe):
    """(video_codec, [audio_codecs], [subtitle_codecs], duration) or None."""
    return _streams_from(_ffprobe_json(ffprobe_exe, path))


def _chapters_from(data):
    """[{title, start, end}] from a probe payload, [] when it has none."""
    out = []
    for n, ch in enumerate((data or {}).get("chapters") or [], start=1):
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
    return _chapters_from(_ffprobe_json(ffprobe_exe, path))


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


def _ffmpeg_args(mode, cfg, acodecs=None):
    """Encoder arguments for a remux pass. Mode "2" = copied video + FLAC
    audio + copied captions; "2s" = copied video + FLAC audio + captions
    converted to SRT (rescue for text caption codecs the MKV muxer refuses);
    "3" = h264 video + FLAC audio. Captions are NEVER dropped: every pass
    maps all subtitle streams.

    *acodecs* is the source's audio codec list, in stream order. Lossless
    ones become FLAC; a lossy one (AC3/DTS/AAC…) is copied byte-for-byte
    when ``video_lossy_audio_copy`` is on, because re-encoding lossy audio
    cannot restore a sample and only inflates the file.
    """
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
    # This is the DEFAULT for every audio stream; the per-stream -c:a:N
    # arguments below override it for the lossy sources that must not be
    # decoded and re-encoded.
    cmd += ["-c:a", "flac", "-strict", "-2", "-compression_level", str(flac_level)]
    if cfg.get("video_lossy_audio_copy", True):
        for i, codec in enumerate(acodecs or ()):
            if not _is_lossless_audio(codec):
                cmd += [f"-c:a:{i}", "copy"]
    # Captions: copied verbatim; the "2s" rescue pass re-encodes text
    # captions to SubRip (content preserved) when the container refuses
    # the source codec. Bitmap captions (DVD/PGS/DVB) can only be copied.
    cmd += ["-c:s", "copy" if mode != "2s" else "srt"]
    return cmd


def remux_video(src, dest, ffmpeg_exe, ffprobe_exe, cfg, concat=False):
    """Remux one video file to MKV. Returns (ok, message).

    ``dest`` must not exist (callers pass a temp path); on success the
    caller os.replace()s it into place after verification. Video is copied
    bit-exact; audio streams that are lossless already are re-encoded to
    FLAC, and lossy ones (AC3/DTS/AAC…) are copied as they are while
    ``video_lossy_audio_copy`` is on — re-encoding lossy audio cannot
    restore a sample and only inflates the file. When the muxer refuses the
    video codec a config-gated H.264 pass follows. Subtitle streams are
    always mapped and copied — the output is verified to carry the same
    caption streams as the source.

    *concat* makes *src* a concat-demuxer list file (mlo.videodisc writes one
    for a disc structure's chosen streams): the files it names reach ffmpeg as
    ONE input, in order, with every stream still copied — a disc title is
    remuxed by this same machinery, never by a second ffmpeg call beside it.
    """
    # Forward slashes: with backslash paths ffmpeg's VOB/VOB-VR demuxer can
    # expose phantom audio substreams (unknown codec parameters) that kill
    # the mux. Windows ffmpeg accepts forward slashes everywhere.
    src = str(src).replace("\\", "/")
    dest = str(dest).replace("\\", "/")
    # ONE probe of the source: -show_chapters is already in the payload, so
    # asking ffprobe for the chapter list separately re-probed the same file.
    src_probe = _ffprobe_json(ffprobe_exe, src, concat=concat)
    info = _streams_from(src_probe)
    if info is None:
        return False, "unreadable by ffprobe"
    vcodec, acodecs, scodecs, duration = info
    if vcodec is None and not acodecs:
        return False, "no audio or video streams"
    if vcodec is None:
        return False, "no video stream"

    # H.264 fallback only ever runs when the user kept the safety valve on.
    allow_reencode = bool(cfg.get("video_reencode_incompatible", True))

    # What the pass did with the audio, spelled out in the result message so a
    # scan of the log shows whether a lossy source was copied or re-encoded.
    lossy_copied = sorted({str(c) for c in acodecs
                           if cfg.get("video_lossy_audio_copy", True)
                           and not _is_lossless_audio(c)})
    audio_label = (f"audio -> FLAC, {'/'.join(lossy_copied)} copied"
                   if lossy_copied else "audio -> FLAC")

    # Regenerate input PTS — DVD-VR VOBs often carry pcm_dvd packets with
    # unknown timestamps that abort the mux otherwise. Input flags must
    # precede -i. Only video/audio/subtitle streams are mapped — data
    # streams (DVD navigation packets) can't be carried by any muxer.
    # Subtitles are mapped unconditionally: captions are never removed.
    stream_maps = ["-map", "0:v", "-map", "0:a?", "-map", "0:s?"]

    last_err = ""
    src_chapters = _chapters_from(src_probe)
    for mode in ("2", "2s", "3") if allow_reencode else ("2", "2s"):
        cmd = ([ffmpeg_exe, "-y", "-v", "error", "-nostdin"]
               + _input_args(src, concat) + stream_maps)
        cmd += _ffmpeg_args(mode, cfg, acodecs)
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
        out_probe = _ffprobe_json(ffprobe_exe, dest)
        out_info = _streams_from(out_probe)
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
            out_chapters = _chapters_from(out_probe)
            if len(out_chapters) < len(src_chapters):
                last_err = (f"chapters dropped ({len(src_chapters)} -> "
                            f"{len(out_chapters)})")
                continue
        if duration and odur and abs(duration - odur) > max(1.0, 0.005 * duration):
            last_err = f"duration changed ({duration:.2f}s -> {odur:.2f}s)"
            continue
        if mode == "2":
            return True, f"video copied, {audio_label} ({' + '.join(acodecs) if acodecs else '?'})"
        if mode == "2s":
            return True, f"video copied, {audio_label}, captions -> SRT"
        return True, (f"{vcodec} video re-encoded to h264 "
                      f"(crf {cfg.get('video_crf', 18)}), {audio_label}")
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


def _duration_probe(ffprobe_exe):
    """``path -> seconds | None`` — the probe mlo.videodisc asks its caller for."""
    def probe(path):
        info = _stream_info(path, ffprobe_exe)
        return info[3] if info else None
    return probe


def _measures(path, seconds, ffprobe_exe):
    """Whether *path* probes within 0.5 % / 1 s of *seconds*."""
    info = _stream_info(path, ffprobe_exe)
    d = info[3] if info else None
    return bool(d) and abs(d - seconds) <= max(1.0, 0.005 * seconds)


def _disc_owner(path):
    """The folder whose disc structure *path* belongs to, or None.

    A file belongs to a structure when it sits inside one (``VIDEO_TS/``,
    ``BDMV/``, ``BDMV/STREAM/``) or is a loose title-set part
    (``VTS_nn_m.VOB``) of the folder that holds the set. What is returned is
    the folder mlo.videodisc recognizes — the structure's parent, which is
    where the remuxed MKV goes.
    """
    d = os.path.dirname(path)
    up = os.path.basename(d).upper()
    if up in LIB_VIDEO_DISC_NAMES:
        return os.path.dirname(d)
    if up == "STREAM" and os.path.basename(os.path.dirname(d)).upper() == "BDMV":
        return os.path.dirname(os.path.dirname(d))
    if videodisc.VOB_RE.match(os.path.basename(path)):
        return d
    return None


def _split_discs(files, probe, explicit=()):
    """``(discs, derivatives, plain)`` for one run's file list.

    *discs* is ``{folder: (Disc, (its files))}`` — ONE entry per structure,
    however many of its files the walk found, because a structure is remuxed
    ONCE, as one title.
    *derivatives* is the video files that sit BESIDE a
    structure (the "700 MB rip" a release ships next to its ``VIDEO_TS``
    folder): the disc's own streams are the feature, so those files are left
    where they are and never remuxed as if they were it. *plain* is everything
    else — the ordinary single-file path, which is also where a file the user
    NAMED (an explicit target) goes: asking for one file by hand is a request
    the disc preference does not overrule.

    honey: leaving the derivative untouched (rather than remuxing it too) is
    the owner's rule — the disc's own streams win, so a re-encode beside them
    is dead weight, not a second title. It is gated by `prefer_disc_streams`
    in the caller; with that off this function is not called at all.
    """
    found, derivatives, plain = {}, [], []
    seen = {}
    for f in files:
        if f.lower().endswith(videodisc.ISO_EXT):
            # A disc image is a structure this app cannot read inside (and a
            # Blu-ray one is usually encrypted): recognized so the app can say
            # it must be mounted first, never so it can be remuxed.
            disc = videodisc.recognize(f)
            if disc is not None:
                found.setdefault(os.path.normcase(f), [disc, []])[1].append(f)
                continue
        owner = _disc_owner(f)
        if owner is not None:
            key = os.path.normcase(owner)
            if key not in seen:
                seen[key] = videodisc.recognize(owner, probe)
            if seen[key] is not None:
                found.setdefault(key, [seen[key], []])[1].append(f)
                continue
        # Not inside a structure: a derivative when its own folder holds one.
        d = os.path.dirname(f)
        key = os.path.normcase(d)
        if key not in seen:
            seen[key] = videodisc.recognize(d, probe)
        if seen[key] is not None and os.path.normcase(f) not in explicit:
            derivatives.append(f)
        else:
            plain.append(f)
    return {k: (v[0], tuple(v[1])) for k, v in found.items()}, derivatives, plain


def _remove_streams(streams, stats):
    """Remove the streams a verified disc remux consumed. Returns the count
    that could NOT be removed — the same failure the single-file path reports."""
    failed = 0
    for s in streams:
        try:
            before = os.path.getsize(s)
            with _DEST_LOCK:
                os.remove(s)
        except OSError:
            failed += 1
            continue
        stats["removed_originals"] += 1
        stats["total_bytes_removed"] += before
    return failed


def _ask_about_disc(disc, reason, config):
    """Raise the app's own prompt for a folder whose feature cannot be picked.

    The prompt is server state (``server.import_autonomy``): one entry in
    ``import_prompts.json``, the ``import_needs_data`` event the notification
    bell shows, ``GET /api/import/prompts`` and the queue's "Needs you" row —
    its body naming the candidates and their durations. Imported lazily (mlo
    must not import server at module level) and never fatal: a remux that
    cannot write a prompt still reports its own log line.
    """
    try:
        from server import import_autonomy

        import_autonomy.raise_video_prompt(
            disc.folder, config, candidates=videodisc.candidates(disc),
            reason=reason)
    except Exception:
        traceback.print_exc()


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
    copy_lossy = bool(config.get("video_lossy_audio_copy", True))
    prefer_disc = bool(config.get("prefer_disc_streams", True))
    log(
        f"ffmpeg: {ffmpeg}\n"
        f"streams: video copied · audio -> FLAC (lossless, level {config.get('video_flac_level', 8)})"
        f"{' · lossy sources (AC3/DTS/AAC…) copied as they are' if copy_lossy else ' · every stream re-encoded'} · "
        f"captions always kept · chapters kept · h264 fallback {'on' if reenc else 'off'} · "
        f"originals: {'removed after verified remux' if remove_original else 'kept'} · "
        f"disc structures (VIDEO_TS/BDMV): "
        f"{'the disc\'s own streams win over a compressed derivative' if prefer_disc else 'left to the per-file path (prefer_disc_streams off)'}"
    )

    folder = os.path.abspath(config["music_folder"] or os.getcwd())
    targets = config.get("targets")
    # An explicit target list IS the user's request ("remux THESE files", the
    # album page's Remux button), so it may name MP4/M4V even when a
    # library-wide pass is gated off by video_process_mp4 — that switch only
    # scopes what an unattended script 11 walk picks up by itself.
    exts = VIDEO_EXTS if targets else remux_input_exts(config)
    # A disc image is not a video container: .iso is walked only while disc
    # recognition is on, and only so the app can say it must be mounted first.
    scan_exts = tuple(exts) + ((videodisc.ISO_EXT,) if prefer_disc else ())
    if targets:
        files = _collect_targets(targets, scan_exts)
    else:
        files = sorted(_walk_files(folder, scan_exts))

    # Deduplicate case-insensitively (Windows) — selections can double-count.
    seen = {}
    for f in files:
        seen.setdefault(os.path.normcase(f), f)
    files = sorted(seen.values())

    if not files:
        log("No video files found.")
        return stats
    log(f"{len(files)} video file(s) found")

    probe = _duration_probe(ffprobe)
    if prefer_disc:
        named = {os.path.normcase(t) for t in (targets or []) if os.path.isfile(t)}
        disc_jobs, derivatives, files = _split_discs(files, probe, named)
    else:
        # The preference is the ONLY thing that gives a disc structure its own
        # treatment: with it off, its files are ordinary video files again (and
        # a compressed derivative beside one is treated like any other).
        disc_jobs, derivatives = {}, []
        log(c("prefer_disc_streams is off — disc structures (VIDEO_TS/BDMV) are "
              "left to the ordinary per-file path, so nothing here prefers the "
              "disc's own streams over a derivative", Color.YELLOW))
    for f in derivatives:
        log(f"  = {os.path.basename(f)}: beside a disc structure — left as it is "
            f"(the disc's own streams are the feature)")
    if disc_jobs:
        log(f"{len(disc_jobs)} disc structure(s) recognized")

    total = len(files) + sum(len(fs) for _, fs in disc_jobs.values())
    if not total:
        log("Nothing to remux (every video file belongs to a disc structure's "
            "compressed derivative, or was left in place).")
        return stats

    workers = worker_count(config, default=min(4, os.cpu_count() or 1),
                           items=total)
    pbar = _make_pbar(total=total, desc="Remuxing videos")

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

    def _plain_job(path):
        """A single file's job, in the disc job's own result shape.

        Both kinds run in one pool, so both return the same tuple —
        ``consumed`` is how many of the run's files the job accounts for (the
        progress bar counts files) and ``refused`` is only ever a disc's.
        """
        return ("file",) + _job(path) + (1, None)

    def _disc_job(disc, disc_files):
        """One disc structure: pick its main feature, remux it as ONE input.

        Returns ``(kind, path, dest, msg, added, skipped, consumed, refused)``
        — ``consumed`` is how many of the run's files this job accounts for
        (the progress bar counts files), and ``refused`` is ``(disc, reason)``
        when mlo.videodisc would not choose, so the caller logs the reason and
        raises the app's prompt for the folder.

        The remux itself is ``remux_video`` over a concat-demuxer list, so the
        verification, caption and chapter rules are the single-file ones; the
        streams the remux consumed are removed only after it verified.
        """
        first = sorted(disc_files)[0]
        count = len(disc_files)
        title, reason = videodisc.pick(disc, probe)
        if title is None:
            return "disc", first, None, reason, 0, False, count, (disc, reason)
        existing = videodisc.output_stem(disc) + ".mkv"
        if os.path.isfile(existing):
            if remove_original and title.duration and _measures(existing, title.duration, ffprobe):
                failed = _remove_streams(title.streams, stats)
                if not failed:
                    return ("disc", first, None, "already remuxed — stray original removed",
                            0, True, count, None)
                return ("disc", first, None,
                        f"already remuxed (the disc's MKV exists); {failed} stream(s) "
                        f"could not be removed", 0, False, count, None)
            return ("disc", first, None, "already remuxed (the disc's MKV exists)",
                    0, True, count, None)
        list_path = None
        fd, tmp = tempfile.mkstemp(
            prefix=".remux_", suffix=".mkv", dir=os.path.dirname(first) or ".")
        os.close(fd)
        try:
            # The list lives in the system temp dir: it names the streams by
            # absolute path, so nothing in the library has to hold it.
            fd, list_path = tempfile.mkstemp(prefix="mlo_disc_", suffix=".ffconcat")
            os.close(fd)
            videodisc.write_concat_list(title.streams, list_path)
            ok, msg = remux_video(list_path, tmp, ffmpeg, ffprobe, config, concat=True)
            if not ok:
                return ("disc", first, None,
                        f"disc title {title.key} ({title.parts} part(s)): {msg}",
                        0, False, count, None)
            with _DEST_LOCK:
                dest = _unique_dest(videodisc.output_stem(disc))
                os.replace(tmp, dest)
            try:
                added = os.path.getsize(dest)
            except OSError:
                added = 0
        finally:
            for p in (list_path, tmp):
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
        label = f"{msg} [disc title {title.key}, {title.parts} part(s)]"
        if remove_original:
            failed = _remove_streams(title.streams, stats)
            if failed:
                return ("disc", first, dest,
                        f"{label}; {failed} stream(s) could not be removed",
                        added, False, count, None)
        return "disc", first, dest, label, added, False, count, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_plain_job, f) for f in files]
        for disc, disc_files in disc_jobs.values():
            futures.append(pool.submit(_disc_job, disc, disc_files))
        for fut in as_completed(futures):
            try:
                kind, path, dest, msg, added, skipped, consumed, refused = fut.result()
            except Exception as e:
                stats["error_count"] += 1
                stats["errors"].append(str(e))
                pbar.update(1)
                continue
            name = os.path.basename(path)
            if refused:
                # Nothing was picked, so nothing was touched: the app asks
                # instead of guessing (the notification bell, the prompts API
                # and the queue's "Needs you" row).
                asked, reason = refused
                stats["skipped_count"] += 1
                log(c(f"  ? {name}: no main feature — {reason}", Color.YELLOW))
                _ask_about_disc(asked, reason, config)
            elif skipped:
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
                if kind == "disc":
                    # The consumed streams were removed by the disc job itself
                    # (only after the remux verified), so this is not the
                    # single-file removal below.
                    log(f"  + {name} -> {os.path.basename(dest)} ({msg})")
                elif remove_original:
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
            pbar.update(consumed)

    pbar.close()
    log(
        f"Done: {stats['converted']} converted · {stats['skipped_count']} skipped · "
        f"{stats['error_count']} errors"
    )
    return stats
