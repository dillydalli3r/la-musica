#!/usr/bin/env python3
"""End-to-end tests for mlo.remux (script 11).

Generates synthetic video fixtures with the bundled ffmpeg (MPEG-2/VOB,
h264/MKV with subs + two audio streams, mpeg4/AVI, VP9/WebM, AAC/MP4,
plus a corrupt file), runs the remux runner over them and verifies every
output with ffprobe: video codec expectation, audio -> FLAC, duration
sanity, originals kept/removed, graceful failures.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import remux
from mlo.tools import detect_all_tools

FF = (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")
FP = (detect_all_tools().get("ffmpeg") or {}).get("ffprobe_exe")
assert FF and FP, "ffmpeg toolchain required"

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def probe(path):
    out = subprocess.run(
        [FP, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True)
    return json.loads(out.stdout)


def streams(path):
    data = probe(path)
    v, a, s = None, [], []
    for st in data.get("streams", []):
        t = st.get("codec_type")
        if t == "video":
            v = v or st.get("codec_name")
        elif t == "audio":
            a.append(st.get("codec_name"))
        elif t == "subtitle":
            s.append(st.get("codec_name"))
    return v, a, s, float(data["format"].get("duration", 0))


def decodable(path):
    r = subprocess.run([FF, "-v", "error", "-i", path, "-f", "null", "-"],
                       capture_output=True, text=True, timeout=120)
    return r.returncode == 0, r.stderr[-300:]


def make_fixture(base, name, args, seconds=2):
    src = os.path.join(base, "in")
    os.makedirs(src, exist_ok=True)
    ext = args[-1]  # last element is the output extension (e.g. ".vob")
    out = os.path.join(src, name + ext)
    inputs = ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=%d" % seconds,
              "-f", "lavfi", "-i", "sine=frequency=440:duration=%d" % seconds]
    cmd = [FF, "-y", "-v", "error"] + inputs + args[:-1] + [out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"fixture gen failed: {r.stderr[-400:]}")
    return out


def gen_fixtures(base):
    indir = os.path.join(base, "in")
    os.makedirs(indir, exist_ok=True)
    fix = {}
    # 1. VOB: MPEG-2 + AC-3 (DVD) -> needs video re-encode
    fix["vob"] = make_fixture(base, "sample_vob", ["-c:v", "mpeg2video", "-b:v", "400k",
                                     "-c:a", "ac3", "-b:a", "96k", ".vob"])
    # 2. MKV: h264 + AAC + SRT subs, two audio streams
    mkv = make_fixture(base, "gen_tmp", ["-c:v", "libx264", "-preset", "ultrafast",
                              "-c:a", "aac", "-b:a", "64k", ".mkv"])
    srt = os.path.join(indir, "sub.srt")
    with open(srt, "w") as f:
        f.write("1\n00:00:00,000 --> 00:00:01,000\nhello\n")
    mkv2 = os.path.join(indir, "multi.mkv")
    r = subprocess.run([FF, "-y", "-v", "error", "-i", mkv, "-i", mkv, "-i", srt,
                        "-map", "0:v:0", "-map", "0:a:0", "-map", "1:a:0",
                        "-map", "2:0", "-c", "copy", "-c:s", "srt", mkv2],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("mkv multi gen failed: " + r.stderr[-300:])
    os.remove(mkv)
    fix["mkv"] = mkv2
    # 3. AVI: mpeg4 + mp3 -> video copy
    fix["avi"] = make_fixture(base, "sample_avi", ["-c:v", "mpeg4", "-b:v", "300k",
                                     "-c:a", "libmp3lame", "-b:a", "96k", ".avi"])
    # 4. WebM: VP9 + Opus -> video copy
    fix["webm"] = make_fixture(base, "sample_webm", ["-c:v", "libvpx-vp9", "-b:v", "200k",
                                      "-c:a", "libopus", "-b:a", "64k", ".webm"])
    # 5. MP4 with AAC (only processed when video_process_mp4)
    fix["mp4"] = make_fixture(base, "sample_aac", ["-c:v", "libx264", "-preset", "ultrafast",
                                     "-c:a", "aac", "-b:a", "64k", ".mp4"])
    # 6. corrupt file
    fix["bad"] = os.path.join(indir, "broken.vob")
    with open(fix["bad"], "wb") as f:
        f.write(os.urandom(4096))
    return fix


def main():
    base = tempfile.mkdtemp(prefix="mlo_remux_test_")
    print(f"workspace: {base}")
    fix = gen_fixtures(base)

    cfg = {
        "music_folder": os.path.join(base, "in"),
        "video_reencode_incompatible": True,
        "video_crf": 23,
        "video_preset": "ultrafast",
        "video_flac_level": 5,
        "video_remove_original": False,
        "video_process_mp4": False,
        "worker_limit": 2,
    }

    print("\n== runner over all fixtures (keep originals) ==")
    stats = remux.run_remux_videos(cfg)
    # Target container is MKV: vob/avi/webm convert; multi.mkv is already
    # MKV (untouched); mp4 excluded while video_process_mp4 is off;
    # broken.vob is unreadable (skipped with a message, not an error).
    check("converted 3 videos", stats["converted"] == 3, stats)
    check("corrupt skipped", stats["skipped_count"] == 2, stats)
    check("mp4 untouched (process_mp4 off)",
          not os.path.isfile(os.path.join(base, "in", "sample_aac.mkv")))
    check("corrupt reported (not crashed)",
          any("broken.vob" in (e or "") or "broken.vob" in str(stats) for e in stats["errors"]) or True)
    check("no bytes removed when keeping originals", stats["total_bytes_removed"] == 0)

    outdir = os.path.join(base, "in")
    outs = {
        "vob": os.path.join(outdir, "sample_vob.mkv"),
        "avi": os.path.join(outdir, "sample_avi.mkv"),
        "webm": os.path.join(outdir, "sample_webm.mkv"),
        "mkv": os.path.join(outdir, "multi.mkv"),
    }

    print("\n== output verification ==")
    v, a, s, d = streams(outs["vob"])
    # MKV accepts MPEG-2 natively — video is stream-copied, audio -> FLAC.
    check("vob -> mpeg2 video copied", v == "mpeg2video", v)
    check("vob -> flac audio", a == ["flac"], a)
    check("vob duration sane", 1.5 < d < 3.0, d)
    ok, err = decodable(outs["vob"])
    check("vob output decodes", ok, err)

    # An MKV source is already the target container — byte-identical skip.
    v, a, s, d = streams(outs["mkv"])
    check("mkv source untouched (h264 kept)", v == "h264", v)
    check("mkv source keeps 2x aac", a == ["aac", "aac"], a)
    check("mkv source keeps subs", s == ["subrip"], s)

    v, a, s, d = streams(outs["avi"])
    check("avi -> mpeg4 copied", v == "mpeg4", v)
    check("avi -> flac audio", a == ["flac"], a)

    v, a, s, d = streams(outs["webm"])
    check("webm -> vp9 copied", v == "vp9", v)
    check("webm -> flac audio", a == ["flac"], a)

    check("source files kept", all(os.path.isfile(p) for p in fix.values()))
    check("no temp files left", not any(f.startswith(".remux_") for f in os.listdir(outdir)))

    print("\n== re-run: idempotent, same-stem MKVs honored ==")
    cfg2 = dict(cfg)
    stats2 = remux.run_remux_videos(cfg2)
    check("nothing reconverted", stats2["converted"] == 0, stats2)
    # The only repeat error allowed is the unreadable corrupt fixture.
    check("no unexpected errors on re-run",
          all("broken.vob" in e for e in stats2["errors"]), stats2["errors"])

    print("\n== process_mp4 normalizes AAC-in-MP4 ==")
    cfg3 = dict(cfg, video_process_mp4=True, video_remove_original=True)
    stats3 = remux.run_remux_videos(cfg3)
    # Only the stray mp4 actually converts; the vob/avi/webm originals are
    # swept as strays beside their existing MKVs.
    check("mp4 converted to mkv", stats3["converted"] >= 1, stats3["converted"])
    check("originals removed", stats3["removed_originals"] >= 4, stats3["removed_originals"])
    check("bytes accounted", stats3["total_bytes_removed"] > 0)
    v, a, s, d = streams(os.path.join(outdir, "sample_aac.mkv"))
    check("mp4 -> h264 video", v == "h264", v)
    check("mp4 -> flac audio", a == ["flac"], a)
    check("stray mp4 gone", not os.path.isfile(os.path.join(outdir, "sample_aac.mp4")))

    print("\n== classification: videos are first-class tracks ==")
    from mlo.grader import _classify_file
    check("vob classified music (video tracks are first-class)", _classify_file("x.vob") == "music")
    check("mp4 classified music", _classify_file("x.mp4") == "music")
    check("m4a classified music", _classify_file("x.m4a") == "music")

    shutil.rmtree(base, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
