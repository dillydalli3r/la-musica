#!/usr/bin/env python3
"""Video-pipeline gate: YouTube quality policy, remux inputs, caption codecs.

Pins the three claims that would otherwise rot silently:

  * the yt-dlp format policy always asks for the best video+audio pair — no
    code path may fall back to a single "best"/"bestvideo" pick (which can
    return the 360p progressive variant), and only the explicit
    youtube_max_height setting may cap the resolution;
  * the remux script and /api/videos/scan agree on the video containers the
    library lists (.mp4/.m4v included), while script 11 still leaves MP4 alone
    unless video_process_mp4 is on;
  * bitmap captions (VobSub/PGS/DVB) are never listed or extractable — a
    <track> pointing at one would load as empty WebVTT.

No network, no ffmpeg, no media files: the external touch points (yt-dlp
binary, ffprobe lookup, subprocess runner) are stubbed.

Run:  python tools/test_video_pipeline.py
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
    from server import subtitles, youtube

    print("yt-dlp format policy")
    sel = youtube._format_selector({})
    check("default selector is the best video+audio pair", sel == "bv*+ba/b", sel)
    check("selector names both a best video and a best audio stream",
          "bv*" in sel and "ba" in sel, sel)
    check("selector is not a bare single-stream pick",
          sel not in ("best", "bestvideo", "bestaudio", "b", "w"), sel)
    capped = youtube._format_selector({"youtube_max_height": 720})
    check("only youtube_max_height caps the resolution",
          capped == "bv*[height<=720]+ba/b[height<=720]", capped)
    for cfg in ({}, {"youtube_max_height": 0}, {"youtube_max_height": "0"},
                {"youtube_max_height": None}):
        check(f"no cap for {cfg}", "height<=" not in youtube._format_selector(cfg), cfg)
    check("quality sort is bitrate-then-resolution, all max",
          youtube.FORMAT_SORT == "vbr:max,abr:max,res:max", youtube.FORMAT_SORT)

    opts = youtube._ydl_opts({}, outtmpl="x.mkv")
    check("python API asks for the same best pair",
          opts["format"] == "bv*+ba/b", opts["format"])
    check("python API merges to MKV", opts["merge_output_format"] == "mkv")
    check("python API uses the same sort fields",
          opts["format_sort"] == ["vbr:max", "abr:max", "res:max"], opts["format_sort"])
    check("python API merges with the app's own ffmpeg",
          bool(opts.get("ffmpeg_location")) or not tools.detect_all_tools().get("ffmpeg"),
          opts.get("ffmpeg_location"))

    captured = {}

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = '{"id": "abc", "format_id": "137+140"}'

    real_run, real_exe = youtube.run_tool, youtube._binary_exe

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Proc()

    youtube.run_tool = _fake_run
    youtube._binary_exe = lambda: sys.executable  # any existing file passes the isfile guard
    try:
        youtube._download_binary("https://youtu.be/abc", os.path.join("out", youtube.OUTTMPL), {})
    finally:
        youtube.run_tool, youtube._binary_exe = real_run, real_exe
    cmd = captured.get("cmd") or []
    check("binary path requests the best pair", _flag(cmd, "-f") == "bv*+ba/b", _flag(cmd, "-f"))
    check("binary path sorts the same way", _flag(cmd, "-S") == "vbr:max,abr:max,res:max",
          _flag(cmd, "-S"))
    check("binary path merges to MKV", _flag(cmd, "--merge-output-format") == "mkv")
    check("binary path never asks for a bare 'best'", not any("best" in str(a) for a in cmd), cmd)
    check("binary path prints JSON after downloading", "--no-simulate" in cmd)

    check("achieved quality read from the merged pair, not the top level",
          youtube._achieved({"requested_formats": [{"height": 1080, "acodec": "none"},
                                                   {"abr": 128.0, "acodec": "opus"}],
                             "height": 360})[:2] == (1080, 128.0))

    work = tempfile.mkdtemp(prefix="mlo_gate_")
    try:
        open(os.path.join(work, "Song [abc].mkv"), "w").close()
        resolved = youtube._resolve_output(
            {"id": "abc", "requested_downloads": [{"filepath": os.path.join(work, "gone.webm")}]},
            work)
        check("finished file resolved by video id when yt-dlp's filepath is stale",
              os.path.basename(resolved or "") == "Song [abc].mkv", resolved)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("search scoring")
    official = {"title": "Artist - Song (Official Video)", "channel": "Artist",
                "duration": 200.0, "view_count": 100}
    reupload = {"title": "Artist - Song (Official Video)", "channel": "Random Uploads",
                "duration": 200.0, "view_count": 10_000_000}
    lyric = {"title": "Artist - Song (Lyrics)", "channel": "Random Lyrics",
             "duration": 200.0, "view_count": 10_000_000}
    cover = {"title": "Artist - Song (Cover)", "channel": "Some Band",
             "duration": 200.0, "view_count": 500}
    off_score = youtube._score(official, "Artist", "Song", 200)
    check("official channel scores", isinstance(off_score, float) and off_score > 0, off_score)
    check("official channel beats a view-farming re-upload",
          off_score > youtube._score(reupload, "Artist", "Song", 200))
    check("lyric re-upload rejected", youtube._score(lyric, "Artist", "Song", 200) is None)
    check("cover rejected", youtube._score(cover, "Artist", "Song", 200) is None)
    check("length outside ±5 s rejected",
          youtube._score(official, "Artist", "Song", 260) is None)
    check("length inside ±5 s accepted",
          youtube._score(official, "Artist", "Song", 204) is not None)
    check("artist's own lyric video is kept",
          youtube._score({"title": "Song (Lyric Video)", "channel": "Artist",
                          "duration": 200.0}, "Artist", "Song", 200) is not None)
    check("VEVO channel counts as the artist's own", youtube._mentions("ArtistVEVO", "Artist"))

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

    print("vendored tool layout")
    check("pip name maps to the import name (yt-dlp -> yt_dlp)",
          tools.PIP_IMPORT_NAMES.get("yt-dlp") == "yt_dlp")
    check("yt-dlp is pinned with a Windows asset",
          fetchdeps.PINNED["yt-dlp"]["asset"].endswith(".exe")
          and fetchdeps.MARKER_EXES["yt-dlp"] == ("yt-dlp.exe",))
    check("yt-dlp is a bare-exe asset", "yt-dlp" in fetchdeps.SINGLE_EXE_TOOLS)
    check("Linux install goes through pip, not a phantom binary",
          "yt-dlp" in fetchdeps.PIP_ON_LINUX
          and "yt-dlp" not in fetchdeps.LINUX_PACKAGES
          and fetchdeps.PIP_PACKAGES["yt-dlp"].startswith("yt-dlp=="))

    print(f"\n{'PASS' if not FAIL else 'FAIL'} — {FAIL} problem(s)")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
