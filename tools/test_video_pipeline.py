#!/usr/bin/env python3
"""Video-pipeline gate: the caption codecs the library's video files carry.

Pins the one claim that would otherwise rot silently: bitmap captions
(VobSub/PGS/DVB) are never listed or extractable — a <track> pointing at one
would load as empty WebVTT — while a text track beside one keeps its own
ffmpeg stream index.

(The YouTube downloader — its yt-dlp format policy, the binary probe, the
search scoring and the vendored-tool layout — was removed with yt-dlp on
2026-10-17; the remux and container rules are pinned by the script-11 suite.)

No network, no ffmpeg, no media files: the external touch points are stubbed.
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAIL = 0


def check(label, ok, detail=""):
    global FAIL
    if not ok:
        FAIL += 1
    print(f"{'ok  ' if ok else 'FAIL'} {label}{'' if ok else ' — ' + str(detail)}")


def _flag(cmd, name):
    try:
        return cmd[cmd.index(name) + 1]
    except ValueError:
        return None


def main():
    from mlo import fetchdeps, tools
    from server import subtitles

    print("subtitle codecs")
    check("text codecs convertible",
          all(subtitles.is_text_subtitle(c)
              for c in ("subrip", "ass", "ssa", "mov_text", "webvtt", "text")))
    check("bitmap captions not convertible",
          not any(subtitles.is_text_subtitle(c) for c in
                  ("dvd_subtitle", "dvb_subtitle", "hdmv_pgs_subtitle", "xsub",
                   "dvb_teletext")))
    check("unknown codec treated as text (ffmpeg decides)", subtitles.is_text_subtitle(None))

    # A stream list with a bitmap caption in front of a text track: the bitmap
    # entry must vanish while the text track keeps its ffmpeg stream index.
    real_info, real_probe = subtitles._stream_info, subtitles._ffprobe_exe
    subtitles._stream_info = lambda path, probe: ("h264", ["aac"],
                                                  ["dvd_subtitle", "subrip"], 2.0)
    subtitles._ffprobe_exe = lambda: "ffprobe-stub"
    subtitles._cache.clear()
    try:
        listed = subtitles.list_subtitles("video.mkv")["muxed"]
        check("bitmap caption is not advertised",
              [e["codec"] for e in listed] == ["subrip"], listed)
        check("text track keeps its ffmpeg stream index", [e["n"] for e in listed] == [1], listed)
        try:
            subtitles.vtt_for("video.mkv", muxed_n=0)
            check("bitmap stream refused by the extractor", False, "served")
        except ValueError:
            check("bitmap stream refused by the extractor", True)
    finally:
        subtitles._stream_info, subtitles._ffprobe_exe = real_info, real_probe
        subtitles._cache.clear()

    print(f"\n{'PASS' if not FAIL else 'FAIL'} — {FAIL} problem(s)")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
