#!/usr/bin/env python3
"""Disc rips: recognizing DVD-Video / Blu-ray structures, and the main feature.

Script 11 used to walk a rip file by file, which for a disc is exactly wrong:
VTS_01_1.VOB, VTS_01_2.VOB … are ONE title cut at 1 GB, and a BDMV's m2ts files
are ordered by a playlist, not by their names. This suite pins the whole
contract of mlo.videodisc and its two consumers:

  * RECOGNITION — a folder holding VIDEO_TS (DVD-Video) or BDMV/STREAM
    (Blu-ray), a folder of loose VTS_nn_m.VOB sets, and an .iso, each named
    for what it is;
  * THE PICK — the longest title wins, the part-0 menu VOB is never a title,
    and the pick REFUSES (with a reason) on two titles within a few percent, a
    Blu-ray playlist that cannot be read or that does not play whole clips, and
    an .iso;
  * THE REMUX — the chosen streams go to ffmpeg as ONE input through the
    concat demuxer (streams copied, never re-encoded), through mlo.remux's own
    remux_video, and the consumed streams are removed only after it verified;
  * THE ASK — a refused pick raises the app's own prompt for the folder
    (server.import_autonomy), whose body names the candidates and their
    durations, and which is re-derived from the structure;
  * THE DERIVATIVE RULE — a compressed re-encode shipped beside a disc
    structure is left alone (the disc's own streams are the feature), the
    preference is prefer_disc_streams, and a re-encode with NO structure is
    never claimed as a disc;
  * THE LAYOUT — a rip's own folder is the shape a disc comes in (not a stray
    subfolder), while an empty folder merely NAMED VIDEO_TS still is;
  * A SINGLE .vob/.m2ts takes the ordinary single-file path, unchanged.

No network, no ffmpeg, no real media: the toolchain is faked (the fixtures are
empty files whose codecs and durations are declared), so the concat list and
the exact argv can be asserted.

Run:  python tools/test_video_discs.py
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAIL = 0

PTS = 45000


def check(label, ok, detail=""):
    global FAIL
    if not ok:
        FAIL += 1
    print(f"{'ok  ' if ok else 'FAIL'} {label}{'' if ok else ' — ' + str(detail)}")


# --------------------------------------------------------------------------- #
# The fake toolchain
# --------------------------------------------------------------------------- #
class Result:
    def __init__(self, code, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


class FakeFF:
    """The ffmpeg/ffprobe pair this suite runs against: no binary, no media.

    ffprobe answers from a table of what each fixture file IS (its codecs and
    its duration) and, for a concat list, from the list's own parts — summed,
    so what is probed of the joined program is the title's own duration. ffmpeg
    writes a placeholder where the remux asked for its output and remembers
    what it was asked to mux, so the verification after it sees a product of
    the same program. Every argv, every list file's parts and every (source,
    output) pair are recorded, which is what lets a test assert WHICH streams
    were joined, in what order, and that nothing was re-encoded.
    """

    def __init__(self):
        self.media = {}        # normcase path -> (video, [audio], [subs], seconds)
        self.calls = []        # every argv
        self.lists = {}        # list path -> [part paths]
        self.muxed = []        # (source, output) per ffmpeg call
        self.out = None        # the last output's (video, audio, subs, seconds)
        self.lists_seen = {}   # list path -> parts, read when it was used

    # -- the fixtures declare what they are --------------------------------
    def declare(self, path, seconds, video="mpeg2video", audio=("ac3",), subs=()):
        self.media[os.path.normcase(os.path.normpath(path))] = (
            video, list(audio), list(subs), float(seconds))
        return path

    def _info(self, target):
        key = os.path.normcase(os.path.normpath(str(target)))
        if key in self.media:
            return self.media[key]
        if str(target).lower().endswith(".ffconcat"):
            parts = []
            try:
                with open(target, "r", encoding="utf-8") as fh:
                    for line in fh:
                        if line.startswith("file "):
                            parts.append(line.split("'")[1])
            except OSError:
                return None
            self.lists_seen[str(target)] = list(parts)
            infos = [self._info(p) for p in parts]
            if not parts or any(i is None for i in infos):
                return None
            return (infos[0][0], infos[0][1], infos[0][2],
                    sum(i[3] for i in infos))
        return self.out

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if "ffprobe" in os.path.basename(str(cmd[0])).lower():
            return self._probe(cmd)
        return self._ffmpeg(cmd)

    def _probe(self, cmd):
        info = self._info(cmd[-1])
        if info is None:
            return Result(1, "", "fake ffprobe: unknown input")
        v, a, s, d = info
        streams = ([{"codec_type": "video", "codec_name": v}] if v else [])
        streams += [{"codec_type": "audio", "codec_name": c} for c in a]
        streams += [{"codec_type": "subtitle", "codec_name": c} for c in s]
        return Result(0, json.dumps({"format": {"duration": str(d)},
                                     "streams": streams, "chapters": []}), "")

    def _ffmpeg(self, cmd):
        if "-i" not in cmd:
            return Result(1, "", "fake ffmpeg: no input")
        src, dest = cmd[cmd.index("-i") + 1], cmd[-1]
        info = self._info(src)
        if info is None:
            return Result(1, "", "fake ffmpeg: unknown input")
        with open(dest, "wb") as fh:
            fh.write(b"fake-mkv")
        self.out = info
        self.muxed.append((src, dest))
        return Result(0, "", "")

    # -- what the assertions ask -------------------------------------------
    def reset(self):
        """Forget every recorded call, so a block's assertions are about IT."""
        self.calls.clear()
        self.lists.clear()
        self.muxed.clear()
        self.lists_seen.clear()
        self.out = None

    def concat_calls(self):
        """The ffmpeg calls that read a concat list: (list path, argv)."""
        return [(src, cmd) for src, cmd in self.muxed_and_args()
                if str(src).lower().endswith(".ffconcat")]

    def muxed_and_args(self):
        out = []
        for cmd in self.calls:
            if "ffprobe" in os.path.basename(str(cmd[0])).lower():
                continue
            if "-i" in cmd:
                out.append((cmd[cmd.index("-i") + 1], cmd))
        return out


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #
def touch(path, data=b""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def write_mpls(path, items, corrupt=False):
    """A BD-ROM playlist: the layout mlo.videodisc.read_playlist reads.

    *items* is ``[(clip, in_seconds, out_seconds)]``. The offsets are the ones
    documented in mlo.videodisc's module docstring (``MPLS`` at 0, the PlayList
    start at 8, the play item count at ``start + 6``, the clip id at
    ``item + 2`` and IN/OUT time at ``item + 14``/``+ 18``).
    """
    body = b""
    for clip, tin, tout in items:
        clip_id = clip.encode("ascii").ljust(5, b"0")[:5]
        if corrupt:
            clip_id = b"AB" + clip_id[2:]
        item = (clip_id + b"M2TS" + b"\x00"      # reserved
                + b"\x10"                        # connection condition 1, low nibble
                + b"\x00"                        # ref_to_STC_id
                + int(tin * PTS).to_bytes(4, "big")
                + int(tout * PTS).to_bytes(4, "big")
                + b"\x00" * 8 + b"\x00" + b"\x00")
        body += len(item).to_bytes(2, "big") + item
    section = (2 + 2 + 2 + len(body)).to_bytes(4, "big") + b"\x00\x00" \
        + len(items).to_bytes(2, "big") + b"\x00\x00" + body
    header = (b"MPLS" + b"0200"
              + (40).to_bytes(4, "big")
              + (40 + len(section)).to_bytes(4, "big")
              + (0).to_bytes(4, "big") + b"\x00" * 20)
    return touch(path, header + section)


def dvd_set(folder, set_no, parts, seconds, menu=True):
    """``VTS_nn_1..N.VOB`` of one title set, plus the set's menu VOB."""
    for n in range(1, parts + 1):
        touch(os.path.join(folder, f"VTS_{set_no:02d}_{n}.VOB"))
    if menu:
        touch(os.path.join(folder, f"VTS_{set_no:02d}_0.VOB"))     # the menu
    return [os.path.join(folder, f"VTS_{set_no:02d}_{n}.VOB")
            for n in range(1, parts + 1)]


def fake_toolchain(tmp):
    """A stand-in ffmpeg/ffprobe on disk (never executed) and the fake runner."""
    exe = touch(os.path.join(tmp, "bin", "ffmpeg.exe"), b"stub")
    probe = touch(os.path.join(tmp, "bin", "ffprobe.exe"), b"stub")
    return exe, probe


class Patched:
    """Point mlo.remux and mlo.tools at the fake toolchain for one block."""

    def __init__(self, fake, exe, probe):
        self.fake, self.exe, self.probe = fake, exe, probe
        self.saved = []

    def __enter__(self):
        from mlo import remux, tools

        for mod, name, value in (
                (remux, "run_tool", self.fake),
                (remux, "detect_all_tools",
                 lambda: {"ffmpeg": {"ffmpeg_exe": self.exe, "ffprobe_exe": self.probe}}),
                (tools, "detect_all_tools",
                 lambda: {"ffmpeg": {"ffmpeg_exe": self.exe, "ffprobe_exe": self.probe}})):
            self.saved.append((mod, name, getattr(mod, name)))
            setattr(mod, name, value)
        return self

    def __exit__(self, *exc):
        for mod, name, value in reversed(self.saved):
            setattr(mod, name, value)
        return False


def cfg_for(root, **over):
    cfg = {"music_folder": root, "video_reencode_incompatible": False,
           "video_remove_original": True, "video_lossy_audio_copy": True,
           "video_process_mp4": False, "worker_limit": 1,
           "prefer_disc_streams": True}
    cfg.update(over)
    return cfg


def captured_logs(fn, *a, **kw):
    """Run *fn* with ``log`` captured: (result, [lines])."""
    from mlo import remux

    lines = []
    real = remux.log
    remux.log = lambda msg: lines.append(str(msg))
    try:
        return fn(*a, **kw), lines
    finally:
        remux.log = real


def main():
    base = tempfile.mkdtemp(prefix="mlo_disc_test_")
    exe, probe = fake_toolchain(base)
    fake = FakeFF()

    from mlo import layout, paths, remux, videodisc
    from server import events as events_mod
    from server import import_autonomy

    # ================= 1. DVD-Video: the 3-part title wins =================
    print("\n== DVD-Video: a 3-part title beside a shorter extra ==")
    dvd = os.path.join(base, "dvd", "An Album")
    ts = os.path.join(dvd, "VIDEO_TS")
    touch(os.path.join(ts, "VIDEO_TS.IFO"))
    touch(os.path.join(ts, "VTS_01_0.IFO"))
    parts = dvd_set(ts, 1, 3, 100)
    extra = dvd_set(ts, 2, 1, 60, menu=False)
    for p in parts:
        fake.declare(p, 100)
    fake.declare(os.path.join(ts, "VTS_01_0.VOB"), 20)   # the title set's menu
    fake.declare(extra[0], 60)

    disc = videodisc.recognize(dvd)
    check("a folder holding VIDEO_TS is a DVD structure",
          bool(disc) and disc.kind == "dvd" and disc.folder == dvd, disc)
    check("its title sets are grouped by part, in order",
          bool(disc) and [t.key for t in disc.titles] == ["VTS_01", "VTS_02"],
          disc and [t.key for t in disc.titles])
    check("VTS_01 is the three parts, in order",
          bool(disc) and disc.titles[0].streams == tuple(parts),
          disc and disc.titles[0].streams)
    check("the part-0 MENU vob is not part of the title",
          bool(disc) and all("_0.VOB" not in s for t in disc.titles for s in t.streams))

    with Patched(fake, exe, probe):
        fake.reset()
        probe_fn = remux._duration_probe(probe)
        disc = videodisc.recognize(dvd, probe_fn)
        check("the parts' durations are summed (3 x 100 s)",
              bool(disc) and disc.titles[0].duration == 300,
              disc and disc.titles[0].duration)
        check("the shorter extra measures 60 s",
              bool(disc) and disc.titles[1].duration == 60,
              disc and disc.titles[1].duration)
        title, reason = videodisc.pick(disc, probe_fn)
        check("the LONGEST title is the pick", bool(title) and title.key == "VTS_01",
              (title, reason))
        stats, lines = captured_logs(remux.run_remux_videos, cfg_for(dvd))

    check("one disc structure was converted", stats["converted"] == 1, stats)
    out_mkv = os.path.join(dvd, "An Album.mkv")
    check("the title lands as ONE mkv beside the structure", os.path.isfile(out_mkv))
    concat = fake.concat_calls()
    check("the remux used the concat demuxer exactly once", len(concat) == 1, concat)
    used_list = concat[0][0] if concat else ""
    check("...naming the three parts IN PLAYBACK ORDER",
          fake.lists_seen.get(used_list)
          == [p.replace("\\", "/") for p in parts], fake.lists_seen)
    check("...as ONE input with -safe 0",
          bool(concat) and "-f" in concat[0][1] and "concat" in concat[0][1]
          and "-safe" in concat[0][1])
    check("...with the video stream COPIED, never re-encoded",
          bool(concat) and "copy" in concat[0][1]
          and "libx264" not in concat[0][1], concat[0][1] if concat else None)
    check("...through mlo.remux's own machinery (its chapter/stream maps)",
          bool(concat) and "-map_chapters" in concat[0][1]
          and "0:v" in concat[0][1] and "0:s?" in concat[0][1])
    check("the consumed streams are removed after the verified remux",
          not any(os.path.exists(p) for p in parts), parts)
    check("...and the other title set is left alone", os.path.isfile(extra[0]))
    check("...as is the structure's IFO", os.path.isfile(os.path.join(ts, "VIDEO_TS.IFO")))
    check("the progress totals the structure's own files",
          any("5 video file(s) found" in ln for ln in lines), lines[:3])

    print("\n== ...and a re-run is idempotent ==")
    with Patched(fake, exe, probe):
        stats2 = remux.run_remux_videos(cfg_for(dvd))
    check("nothing reconverted (the disc's MKV is already there)",
          stats2["converted"] == 0, stats2)
    check("no second copy piled up",
          not os.path.isfile(os.path.join(dvd, "An Album (2).mkv")))

    # ================= 2. two titles within 5 % -> refuse + ask ============
    print("\n== two similar-length titles: the pick refuses and the app asks ==")
    amb = os.path.join(base, "amb", "Twin Discs")
    ats = os.path.join(amb, "VIDEO_TS")
    a1 = dvd_set(ats, 1, 3, 100)          # 300 s
    a2 = dvd_set(ats, 2, 3, 98, menu=False)   # 294 s
    for p in a1 + a2:
        fake.declare(p, 100 if p in a1 else 98)
    with Patched(fake, exe, probe):
        probe_fn = remux._duration_probe(probe)
        disc_a = videodisc.recognize(amb, probe_fn)
        title_a, reason_a = videodisc.pick(disc_a, probe_fn)
        check("neither title is picked", title_a is None, title_a)
        check("...and the reason says they are within 5 %",
              "within 5%" in reason_a, reason_a)
        check("...naming BOTH durations",
              "5:00" in reason_a and "4:54" in reason_a, reason_a)
        events_mod._events.clear()
        stats_a, lines_a = captured_logs(remux.run_remux_videos, cfg_for(amb))

    check("nothing was converted", stats_a["converted"] == 0, stats_a)
    check("nothing was skipped silently — it was asked about",
          stats_a["skipped_count"] == 1, stats_a)
    check("not one stream was touched", all(os.path.isfile(p) for p in a1 + a2))
    check("the log names the reason",
          any("no main feature" in ln and "within 5%" in ln for ln in lines_a), lines_a)

    prompts = import_autonomy.prompts(cfg_for(amb))
    mine = [p for p in prompts if p.get("kind") == "video"]
    check("the prompt is listed by GET /api/import/prompts", len(mine) == 1, prompts)
    body = import_autonomy.body(mine[0]) if mine else ""
    check("its body carries the candidates AND their durations",
          "Main feature" in body and "5:00" in body and "4:54" in body, body)
    check("...and the headline says what is being asked",
          bool(mine) and import_autonomy.title(mine[0]).startswith("Which title is the main feature"),
          mine and import_autonomy.title(mine[0]))
    frames = [e for e in events_mod.recent(0, limit=10 ** 6)
              if e.get("event") == "import_needs_data"]
    check("the notification bell got exactly one frame", len(frames) == 1, frames)
    check("...carrying a link into the album and the family row",
          bool(frames) and str(frames[0]["data"]["link"]).startswith("/import?album=")
          and frames[0]["data"]["families"] == [import_autonomy.VIDEO_FAMILY],
          frames and frames[0].get("data"))
    check("the queue row's own words come from the same body",
          bool(frames) and frames[0]["body"] == body, frames and frames[0].get("body"))
    from server.api_queue import _prompt_rows
    row = _prompt_rows(mine)[0] if mine else {}
    check("...and the queue's row is a Needs-you row carrying it",
          row.get("stage") == "needs_attention" and row.get("kind") == "prompt"
          and row.get("reason") == body and row.get("missing_labels") == ["Main feature"],
          row)
    check("...with the same link as the wizard action",
          bool(mine) and row.get("action_link") == mine[0]["link"]
          and row.get("wizard_link") == mine[0]["link"], row.get("action_link"))

    with Patched(fake, exe, probe):
        events_mod._events.clear()
        remux.run_remux_videos(cfg_for(amb))
    check("re-running the same refusal does not repeat the notification",
          not [e for e in events_mod.recent(0, limit=10 ** 6)
               if e.get("event") == "import_needs_data"])
    check("the prompt is still listed while the structure is",
          len([p for p in import_autonomy.prompts(cfg_for(amb))
               if p.get("kind") == "video"]) == 1)

    print("\n== ...and withdrawn once the ambiguity is gone ==")
    for p in a2:
        os.remove(p)
    with Patched(fake, exe, probe):
        probe_fn = remux._duration_probe(probe)
        check("with only one title left the prompt is withdrawn",
              [p for p in import_autonomy.prompts(cfg_for(amb))
               if p.get("kind") == "video"] == [],
              import_autonomy.for_album(amb, cfg_for(amb)))
        check("...and the pick now answers",
              videodisc.pick(videodisc.recognize(amb, probe_fn), probe_fn)[0] is not None)
    check("a dismissed prompt is gone too",
          import_autonomy.clear(amb, cfg_for(amb)) is False)   # already withdrawn

    # ================= 3. Blu-ray: the playlist decides ====================
    print("\n== Blu-ray: the playlist orders the clips ==")
    bd = os.path.join(base, "bd", "A Concert")
    stream_dir = os.path.join(bd, "BDMV", "STREAM")
    pl_dir = os.path.join(bd, "BDMV", "PLAYLIST")
    touch(os.path.join(bd, "BDMV", "index.bdmv"), b"INDX0200")
    touch(os.path.join(bd, "BDMV", "CLIPINF", "00000.clpi"))
    clip_main = touch(os.path.join(stream_dir, "00000.m2ts"))
    clip_short = touch(os.path.join(stream_dir, "00001.m2ts"))
    fake.declare(clip_main, 600)
    fake.declare(clip_short, 100)
    write_mpls(os.path.join(pl_dir, "00000.mpls"), [("00000", 0, 600)])
    write_mpls(os.path.join(pl_dir, "00001.mpls"), [("00001", 0, 100)])

    bdisc = videodisc.recognize(bd)
    check("a folder holding BDMV/STREAM is a Blu-ray structure",
          bool(bdisc) and bdisc.kind == "bluray", bdisc)
    check("its titles are the playlists, named after them",
          bool(bdisc) and sorted(t.key for t in bdisc.titles) == ["00000.mpls", "00001.mpls"],
          bdisc and [t.key for t in bdisc.titles])
    check("the durations come from the play items (45 kHz PTS ticks)",
          bool(bdisc) and {t.key: t.duration for t in bdisc.titles}
          == {"00000.mpls": 600.0, "00001.mpls": 100.0},
          bdisc and {t.key: t.duration for t in bdisc.titles})
    with Patched(fake, exe, probe):
        btitle, breason = videodisc.pick(bdisc, remux._duration_probe(probe))
        check("the playlist with the longest title wins",
              bool(btitle) and btitle.key == "00000.mpls", (btitle, breason))
        check("...and its clip is the stream that will be remuxed",
              bool(btitle) and btitle.streams == (clip_main,))
        stats_b, _ = captured_logs(remux.run_remux_videos, cfg_for(bd))
    check("the Blu-ray title converted to one MKV",
          stats_b["converted"] == 1 and os.path.isfile(os.path.join(bd, "A Concert.mkv")),
          stats_b)
    check("its consumed clip is gone, the trailer clip is not",
          not os.path.exists(clip_main) and os.path.isfile(clip_short))

    print("\n== ...a playlist that cannot be read is refused, not guessed ==")
    bad = os.path.join(base, "bd_bad", "Broken Disc")
    bstream = os.path.join(bad, "BDMV", "STREAM")
    bpl = os.path.join(bad, "BDMV", "PLAYLIST")
    badclip = touch(os.path.join(bstream, "00000.m2ts"))
    trailer = touch(os.path.join(bstream, "00001.m2ts"))
    fake.declare(badclip, 600)
    fake.declare(trailer, 100)
    write_mpls(os.path.join(bpl, "00000.mpls"), [("00000", 0, 600)], corrupt=True)
    write_mpls(os.path.join(bpl, "00001.mpls"), [("00001", 0, 100)])

    bdisc2 = videodisc.recognize(bad)
    check("the playlists that DID parse are still listed",
          [t.key for t in bdisc2.titles] == ["00001.mpls"], bdisc2.titles)
    check("...but the structure says the disc's titles are not all known",
          "could not be read" in bdisc2.note and "00000.mpls" in bdisc2.note, bdisc2.note)
    with Patched(fake, exe, probe):
        t2, r2 = videodisc.pick(bdisc2, remux._duration_probe(probe))
        check("the pick refuses rather than letting a trailer be the feature",
              t2 is None and "00000.mpls" in r2, (t2, r2))
        stats_bad, _ = captured_logs(remux.run_remux_videos, cfg_for(bad))
    check("nothing was converted and nothing touched",
          stats_bad["converted"] == 0 and os.path.isfile(badclip)
          and os.path.isfile(trailer), stats_bad)
    check("...and the folder was asked about",
          any(p.get("kind") == "video" for p in import_autonomy.prompts(cfg_for(bad))))

    print("\n== ...a playlist that plays part of a clip is refused ==")
    part = os.path.join(base, "bd_part", "Branching Disc")
    pstream = os.path.join(part, "BDMV", "STREAM")
    touch(os.path.join(part, "BDMV", "PLAYLIST", "00000.mpls"))
    pclip = touch(os.path.join(pstream, "00000.m2ts"))
    pclip2 = touch(os.path.join(pstream, "00001.m2ts"))
    fake.declare(pclip, 600)
    fake.declare(pclip2, 100)
    write_mpls(os.path.join(part, "BDMV", "PLAYLIST", "00000.mpls"),
               [("00000", 0, 300), ("00001", 0, 100)])
    with Patched(fake, exe, probe):
        t3, r3 = videodisc.pick(videodisc.recognize(part), remux._duration_probe(probe))
        check("a play item that plays half of its clip refuses",
              t3 is None and "not the title" in r3, (t3, r3))
        check("...so nothing is concatenated", r3 and "00000.m2ts" in r3, r3)

    print("\n== ...and one that plays a clip twice is refused ==")
    twice = os.path.join(base, "bd_twice", "Loop Disc")
    tclip = touch(os.path.join(twice, "BDMV", "STREAM", "00000.m2ts"))
    fake.declare(tclip, 600)
    write_mpls(os.path.join(twice, "BDMV", "PLAYLIST", "00000.mpls"),
               [("00000", 0, 600), ("00000", 0, 600)])
    with Patched(fake, exe, probe):
        t4, r4 = videodisc.pick(videodisc.recognize(twice), remux._duration_probe(probe))
        check("a playlist that reuses a clip refuses",
              t4 is None and "more than once" in r4, (t4, r4))

    # ================= 4. an .iso is refused ===============================
    print("\n== a disc image is not read, it is asked about ==")
    iso_dir = os.path.join(base, "iso", "A Disc")
    iso = touch(os.path.join(iso_dir, "A Disc.iso"), b"ISO")

    idisc = videodisc.recognize(iso)
    check("an .iso names itself a disc image",
          bool(idisc) and idisc.kind == "iso" and idisc.folder == iso_dir, idisc)
    ititle, ireason = videodisc.pick(idisc)
    check("the pick refuses it for the reason that matters",
          ititle is None and "mount" in ireason and ".iso" in ireason, ireason)
    with Patched(fake, exe, probe):
        events_mod._events.clear()
        stats_i, lines_i = captured_logs(remux.run_remux_videos, cfg_for(iso_dir))
    check("the .iso is not remuxed and not touched",
          stats_i["converted"] == 0 and os.path.isfile(iso), stats_i)
    check("...and the app asks, with the mounting reason",
          any("mount" in import_autonomy.body(p)
              for p in import_autonomy.prompts(cfg_for(iso_dir))
              if p.get("kind") == "video"))
    check("the log carries the reason too",
          any("no main feature" in ln and "mount" in ln for ln in lines_i), lines_i)

    # ================= 5. a single file is unchanged =======================
    print("\n== a single .vob / .m2ts takes the ordinary path ==")
    solo = os.path.join(base, "solo")
    single = touch(os.path.join(solo, "clip.vob"))
    single2 = touch(os.path.join(solo, "extra.m2ts"))
    fake.declare(single, 12)
    fake.declare(single2, 7)
    check("neither a lone .vob nor a lone .m2ts is a disc structure",
          videodisc.recognize(solo) is None and videodisc.recognize(single) is None
          and videodisc.recognize(single2) is None)
    with Patched(fake, exe, probe):
        fake.reset()
        stats_s = remux.run_remux_videos(cfg_for(solo))
    check("both converted file by file", stats_s["converted"] == 2, stats_s)
    check("...to their own same-stem MKVs",
          os.path.isfile(os.path.join(solo, "clip.mkv"))
          and os.path.isfile(os.path.join(solo, "extra.mkv")))
    check("...with NO concat demuxer anywhere",
          not fake.concat_calls() and not fake.lists_seen, fake.concat_calls())
    check("...and the originals removed as always",
          not os.path.exists(single) and not os.path.exists(single2))

    # ================= 6. the disc beats a compressed derivative ===========
    print("\n== a compressed derivative beside a disc structure is left alone ==")
    rip = os.path.join(base, "rip", "A Film")
    rts = os.path.join(rip, "VIDEO_TS")
    rparts = dvd_set(rts, 1, 3, 100)
    for p in rparts:
        fake.declare(p, 100)
    avi = touch(os.path.join(rip, "A Film.avi"))
    fake.declare(avi, 700, video="mpeg4", audio=("mp3",))
    with Patched(fake, exe, probe):
        fake.reset()
        events_mod._events.clear()
        stats_d, lines_d = captured_logs(remux.run_remux_videos, cfg_for(rip))
    check("the disc's own streams are the feature, not the rip beside them",
          stats_d["converted"] == 1 and os.path.isfile(os.path.join(rip, "A Film.mkv")),
          stats_d)
    check("the compressed derivative is left where it is",
          os.path.isfile(avi) and not os.path.isfile(os.path.join(rip, "A Film.avi.mkv")))
    check("...and the one MKV beside it is the DISC's title, not the rip's",
          os.path.isfile(os.path.join(rip, "A Film.mkv")))
    check("the disc's product is the concat of its own streams",
          bool(fake.muxed) and str(fake.muxed[0][0]).endswith(".ffconcat"), fake.muxed)
    check("...and never handed to ffmpeg",
          not any(src == avi for src, _ in fake.muxed), fake.muxed)
    check("the log says which file it left alone and why",
          any("beside a disc structure" in ln and "A Film.avi" in ln for ln in lines_d),
          lines_d)
    check("only the disc's title was muxed",
          [src for src, _ in fake.muxed] == list(fake.lists_seen)[-1:], fake.muxed)

    print("\n== prefer_disc_streams off: nothing is preferred (log says why) ==")
    rip2 = os.path.join(base, "rip2", "A Film")
    r2ts = os.path.join(rip2, "VIDEO_TS")
    r2parts = dvd_set(r2ts, 1, 3, 100)
    for p in r2parts:
        fake.declare(p, 100)
    avi2 = touch(os.path.join(rip2, "A Film.avi"))
    fake.declare(avi2, 700, video="mpeg4", audio=("mp3",))
    with Patched(fake, exe, probe):
        stats_off, lines_off = captured_logs(
            remux.run_remux_videos, cfg_for(rip2, prefer_disc_streams=False))
    check("the derivative is treated like any other video file",
          os.path.isfile(os.path.join(rip2, "A Film.mkv"))
          and not os.path.isfile(avi2), os.listdir(rip2))
    check("the title's parts take the ordinary per-file path",
          all(os.path.isfile(os.path.join(r2ts, f"VTS_01_{n}.mkv")) for n in (1, 2, 3)),
          os.listdir(r2ts))
    check("...and every file went through the per-file path",
          stats_off["converted"] == 5, stats_off)
    check("the log says why the preference did nothing",
          any("prefer_disc_streams is off" in ln for ln in lines_off), lines_off)

    print("\n== a re-encode with NO structure is never claimed as a disc ==")
    plaindir = os.path.join(base, "plain")
    only_avi = touch(os.path.join(plaindir, "A Film.avi"))
    fake.declare(only_avi, 700, video="mpeg4", audio=("mp3",))
    with Patched(fake, exe, probe):
        fake.reset()
        stats_p, lines_p = captured_logs(remux.run_remux_videos, cfg_for(plaindir))
    check("it takes the ordinary single-file path",
          stats_p["converted"] == 1
          and os.path.isfile(os.path.join(plaindir, "A Film.mkv")), stats_p)
    check("no structure was recognized and none was left alone",
          not any("disc structure(s) recognized" in ln
                  or "beside a disc structure" in ln for ln in lines_p), lines_p)

    # ================= 7. the layout scan ==================================
    print("\n== the layout: a rip's folder is a shape, not a stray ==")
    lib = os.path.join(base, "lib")
    good = os.path.join(lib, "Artists", "An Artist", "An Album")
    inside = os.path.join(good, "VIDEO_TS")
    touch(os.path.join(good, "01 track.flac"))
    touch(os.path.join(inside, "VTS_01_1.VOB"))
    touch(os.path.join(inside, "VIDEO_TS.IFO"))
    bad_album = os.path.join(lib, "Artists", "An Artist", "Empty Structure")
    touch(os.path.join(bad_album, "01 track.flac"))
    touch(os.path.join(bad_album, "VIDEO_TS", "notes.txt"))   # merely NAMED so

    report = layout.scan_library({"music_folder": lib})
    subs = [r["path"] for r in report["issues"] if r["kind"] == "unexpected_subfolder"]
    check("a recognized disc structure is not reported as a stray folder",
          not any("An Album/VIDEO_TS" in p for p in subs), subs)
    check("...while a folder merely NAMED VIDEO_TS still is",
          any("Empty Structure/VIDEO_TS" in p for p in subs), subs)
    check("the layout knows the disc vocabulary",
          paths.is_video_disc_dir("x/VIDEO_TS") and paths.is_video_disc_dir("x/BDMV")
          and not paths.is_video_disc_dir("x/Video"),
          paths.LIB_VIDEO_DISC_DIRS)

    # ================= 8. the prompt table itself ==========================
    print("\n== the prompt has one entry per question, not per run ==")
    ask_dir = os.path.join(base, "ask", "Ask Album")
    ats2 = os.path.join(ask_dir, "VIDEO_TS")
    dvd_set(ats2, 1, 1, 10)
    cfg_ask = cfg_for(ask_dir)
    events_mod._events.clear()
    entry = import_autonomy.raise_video_prompt(
        ask_dir, cfg_ask, candidates=["VTS_01 (1 part, 10s)"], reason="the app will not choose")
    check("the question is stored under the album",
          bool(entry) and entry["kind"] == "video"
          and entry["album"].endswith("Ask Album"), entry)
    check("...linking into the album's wizard",
          bool(entry) and entry["link"].startswith("/import?album="), entry and entry["link"])
    check("...with the candidate list in the body",
          "VTS_01 (1 part, 10s)" in import_autonomy.body(entry),
          import_autonomy.body(entry))
    check("dismissing it removes the row",
          import_autonomy.clear(ask_dir, cfg_ask) is True
          and import_autonomy.for_album(ask_dir, cfg_ask) == {})
    check("the wizard's ?missing= vocabulary does not have to know it",
          import_autonomy.VIDEO_FAMILY not in
          __import__("mlo.import_policy", fromlist=["x"]).FAMILY_IDS)

    shutil.rmtree(base, ignore_errors=True)
    print(f"\n{'FAILED ' + str(FAIL) if FAIL else 'all checks passed'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
