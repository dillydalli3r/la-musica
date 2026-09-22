"""Disc structures — DVD-Video (``VIDEO_TS``) and Blu-ray (``BDMV``) rips.

A rip of a disc is not a video file: it is a FOLDER whose shape says what the
disc holds, and whose streams have to be put back in playback order before
anything can be remuxed. This module recognizes that shape, groups the streams
of every title, and picks the main feature — or refuses, with a reason a person
can act on, when the disc does not say which title the feature is.

What the two shapes are, and the evidence for each:

* **DVD-Video** — a folder named ``VIDEO_TS`` (or the loose files of one)
  holding ``VIDEO_TS.IFO``/``.BUP`` (the video manager) and one title set per
  ``VTS_nn_*``: ``VTS_nn_0.IFO``/``.BUP`` describe title set *nn*, and the
  title set's own program stream is cut at 1 GB into ``VTS_nn_1.VOB``,
  ``VTS_nn_2.VOB``, … The part index starts at **1**: ``VTS_nn_0.VOB`` is the
  title set's MENU, never the feature, so it is never grouped. Each VOB is an
  MPEG-2 program stream (``ffprobe``: format ``mpeg``, ``mpeg2video`` with
  AC-3/PCM/DTS audio) — evidence: the ``VTS_nn_m.VOB`` file-name grammar of the
  DVD-Video format, and the probe of a real part. A disc is sometimes shipped
  as one stray ``.vob``/``.mpg`` instead; that is a single file and stays on
  the ordinary path.
* **Blu-ray** — a folder named ``BDMV`` holding ``index.bdmv``,
  ``PLAYLIST/nnnnn.mpls``, ``CLIPINF/nnnnn.clpi`` and ``STREAM/nnnnn.m2ts``.
  Each ``.m2ts`` is an MPEG-2 transport stream (``ffprobe``: format
  ``mpegts``, ``mpeg2video``/``h264``/``vc1``/``hevc`` video), and the
  ``.mpls`` playlist — not the file names — says which clips form which title,
  in order, each play item carrying the clip's IN/OUT time in 45 kHz PTS
  ticks. Evidence: the MPLS header/play-item layout (``MPLS`` magic at 0, the
  PlayList and PlayListMark start addresses at 8 and 12, the play item's clip
  id at 2, codec id at 7, IN_time at 14 and OUT_time at 18) as documented for
  BD-ROM part 3 §5.3 and implemented by libbluray's ``mpls_parse.c``; the
  parser below re-checks the shape it reads (five-digit clip ids, play items
  that fit the file) and refuses anything that does not match. ONE playlist
  that cannot be read refuses the whole disc: a title the disc states but this
  code cannot read may be the feature, so the longest of the playlists that
  DID parse is not an answer.
* **A disc image** — an ``.iso`` of either. Nothing in this app can read inside
  one (and a Blu-ray image is usually AACS-encrypted), so an ``.iso`` is
  recognized only to say so.

What this module deliberately does NOT read: **chapter marks**. A DVD's are in
its IFO's program-chain table and a Blu-ray's in the ``.mpls`` PlayListMark
section; neither is read here, so a disc structure's remux carries no chapters
and none are invented (``mlo.remux``'s own rule, unchanged). The IFO's PGC
structure and the CLPI's EP map are likewise not read: the DVD title is its
``VTS_nn_1…N`` parts concatenated in part order, and the Blu-ray title is the
playlist's clips in playlist order.

Nothing here calls ffmpeg itself: durations are asked of a *probe* the caller
supplies (``mlo.remux`` passes its own ffprobe), so the module is pure logic
over names and bytes and the tests can stub the measurements.
"""

import os
import re
from dataclasses import dataclass

from .paths import LIB_VIDEO_DISC_DIRS

# --- DVD-Video -------------------------------------------------------------
# VTS_nn_m.VOB: *nn* is the title set, *m* the part. Part 0 is the title set's
# menu (VTS_nn_0.VOB), so grouping starts at 1.
VOB_RE = re.compile(r"^VTS_(\d{1,2})_(\d+)\.VOB$", re.IGNORECASE)

# --- Blu-ray ---------------------------------------------------------------
M2TS_RE = re.compile(r"^(\d{5})\.m2ts$", re.IGNORECASE)
MPLS_RE = re.compile(r"^(\d{5})\.mpls$", re.IGNORECASE)

ISO_EXT = ".iso"

# Blu-ray timestamps are 45 kHz PTS ticks (BD-ROM part 3 §5.3).
PTS_HZ = 45000.0

# A play item whose span differs from the clip's own duration by more than
# this much does not play the whole clip (seamless branching, a "play all"
# that re-uses a clip), and the concat demuxer cannot reproduce that.
CONCAT_ABS_S = 2.0
CONCAT_REL = 0.02

# Two titles within this much of each other are not "the longest one": the
# runner-up is a plausible feature too, so the pick is the user's.
#
# honey: the ABS floor is what keeps "longest" from being a coin toss on a
# disc of short clips (a menu PGC and a trailer 2 s apart are both tiny, and
# neither is a feature); the REL half is the real rule for a full-length disc.
# Raising either makes the app ask MORE often, never less — the refusal is the
# safe direction, so only lower them against a disc that is asked about too
# eagerly.
AMBIGUOUS_ABS_S = 30.0
AMBIGUOUS_REL = 0.05


@dataclass(frozen=True)
class PlayItem:
    """One entry of a Blu-ray playlist: a clip and the part of it played."""
    clip: str          # "00001" — STREAM/00001.m2ts
    in_time: int       # 45 kHz PTS ticks
    out_time: int

    @property
    def seconds(self):
        return max(0.0, (self.out_time - self.in_time) / PTS_HZ)


@dataclass(frozen=True)
class Title:
    """One title of a disc structure: its streams, in playback order.

    ``note`` is non-empty when the streams cannot be used (a broken part run, a
    clip that is not on disk) — the pick refuses such a title with that note.
    ``play_items`` is the Blu-ray playlist's own entries, empty for DVD.
    """
    key: str
    streams: tuple[str, ...]
    duration: float | None = None
    note: str = ""
    play_items: tuple[PlayItem, ...] = ()

    @property
    def parts(self):
        return len(self.streams)


@dataclass(frozen=True)
class Disc:
    """A recognized disc structure.

    ``folder`` is the folder the remuxed MKV belongs in (the structure's
    parent when the caller pointed at ``VIDEO_TS``/``BDMV`` itself);
    ``structure`` is the structure folder — or the ``.iso`` file.
    """
    kind: str          # "dvd" | "bluray" | "iso"
    folder: str
    structure: str
    titles: tuple[Title, ...] = ()
    note: str = ""     # why the structure itself cannot be used, else ""


# --------------------------------------------------------------------------- #
# Blu-ray playlists
# --------------------------------------------------------------------------- #
def _u16(data, off):
    return int.from_bytes(data[off:off + 2], "big")


def _u32(data, off):
    return int.from_bytes(data[off:off + 4], "big")


def read_playlist(path):
    """``(play_items, seconds)`` for one ``.mpls`` — or ValueError.

    The layout read is the one documented at the top of this module: the
    header's PlayList start address, the playlist header's play item count,
    then each play item's length/clip id/IN_time/OUT_time. Every read is
    bounds-checked and every clip id must be five digits, so a file that is
    not really an MPLS (or a layout this code has wrong) refuses instead of
    returning invented streams.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 40 or data[0:4] != b"MPLS":
        raise ValueError("not an MPLS playlist (no MPLS header)")
    pl_start = _u32(data, 8)
    if pl_start + 10 > len(data):
        raise ValueError("playlist section lies outside the file")
    count = _u16(data, pl_start + 6)
    if not count:
        raise ValueError("the playlist holds no play items")
    items = []
    pos = pl_start + 10
    for _ in range(count):
        if pos + 22 > len(data):
            raise ValueError("a play item runs past the end of the file")
        length = _u16(data, pos)
        if length < 20 or pos + 2 + length > len(data):
            raise ValueError("a play item's length is not a play item's")
        clip = data[pos + 2:pos + 7].decode("ascii", "replace")
        if not clip.isdigit():
            raise ValueError("a play item's clip id is not a number")
        items.append(PlayItem(clip=clip,
                              in_time=_u32(data, pos + 14),
                              out_time=_u32(data, pos + 18)))
        pos += 2 + length
    return tuple(items), sum(i.seconds for i in items)


# --------------------------------------------------------------------------- #
# Recognition
# --------------------------------------------------------------------------- #
def recognize(path, probe=None):
    """The disc structure at *path*, or None when there is none.

    *path* may be the folder holding ``VIDEO_TS``/``BDMV``, the structure
    folder itself, a folder of loose ``VTS_nn_m.VOB`` files, or an ``.iso``.
    *probe*, when given, is ``path -> seconds | None`` and fills in a DVD
    title's duration (its parts' durations summed); Blu-ray durations come from
    the playlist and need no probe.
    """
    p = os.path.normpath(str(path or ""))
    if not p or p == ".":
        return None
    if os.path.isfile(p):
        return _iso(p) if p.lower().endswith(ISO_EXT) else None
    if not os.path.isdir(p):
        return None
    name = os.path.basename(p).upper()
    if name == "VIDEO_TS":
        return _dvd(os.path.dirname(p), p, probe)
    if name == "BDMV":
        return _bluray(os.path.dirname(p), p)
    for sub in LIB_VIDEO_DISC_DIRS:
        d = os.path.join(p, sub)
        if not os.path.isdir(d):
            continue
        disc = _dvd(p, d, probe) if sub.upper() == "VIDEO_TS" else _bluray(p, d)
        if disc is not None:
            return disc
    # Loose title-set files: the folder itself is the structure.
    return _dvd(p, p, probe)


def _iso(path):
    return Disc(kind="iso", folder=os.path.dirname(path), structure=path)


def disc_image(folder):
    """The first disc image (``.iso``) directly inside *folder*, or None.

    Recognition's other half: a rip shipped as an image rather than as a
    folder has no structure to recognize, so the folder holding it is asked
    for the image itself — the same question `mlo.remux` asks when it walks a
    folder for disc structures.
    """
    try:
        names = sorted(os.listdir(str(folder)))
    except OSError:
        return None
    for name in names:
        if name.lower().endswith(ISO_EXT):
            return _iso(os.path.join(str(folder), name))
    return None


def _dvd(folder, structure, probe=None):
    """The DVD title sets of *structure*, or None when it holds no VOB part."""
    try:
        names = os.listdir(structure)
    except OSError:
        return None
    groups = {}
    for name in names:
        m = VOB_RE.match(name)
        if not m:
            continue
        set_no, part = int(m.group(1)), int(m.group(2))
        if part < 1:
            continue            # VTS_nn_0.VOB is the title set's MENU
        groups.setdefault(set_no, []).append((part, os.path.join(structure, name)))
    if not groups:
        return None
    titles = []
    for set_no in sorted(groups):
        parts = sorted(groups[set_no])
        streams = tuple(p for _, p in parts)
        note = ""
        if [n for n, _ in parts] != list(range(1, len(parts) + 1)):
            # A title's parts are 1…N with nothing missing: with a hole the
            # concatenation would silently be a different title.
            note = (f"the parts are not a 1…{len(parts)} run ("
                    + ", ".join(f"{n}" for n, _ in parts) + ")")
        titles.append(Title(
            key=f"VTS_{set_no:02d}", streams=streams, note=note,
            duration=_sum_durations(streams, probe) if not note else None))
    return Disc(kind="dvd", folder=folder, structure=structure,
                titles=tuple(titles))


def _bluray(folder, structure):
    """The Blu-ray titles *structure* names, or a Disc saying why it cannot."""
    stream_dir = os.path.join(structure, "STREAM")
    if not os.path.isdir(stream_dir):
        return None
    playlist_dir = os.path.join(structure, "PLAYLIST")
    if not os.path.isdir(playlist_dir):
        return Disc(kind="bluray", folder=folder, structure=structure,
                    note=("BDMV/PLAYLIST is missing — nothing on the disc says "
                          "which streams form which title"))
    titles, unreadable = [], []
    try:
        names = sorted(os.listdir(playlist_dir))
    except OSError as e:
        return Disc(kind="bluray", folder=folder, structure=structure,
                    note=f"BDMV/PLAYLIST cannot be read ({e})")
    for name in names:
        if not MPLS_RE.match(name):
            continue
        try:
            items, seconds = read_playlist(os.path.join(playlist_dir, name))
        except (OSError, ValueError) as e:
            unreadable.append(f"{name} ({e})")
            continue
        streams, note = [], ""
        for item in items:
            f = os.path.join(stream_dir, item.clip + ".m2ts")
            if not os.path.isfile(f):
                note = f"clip {item.clip}.m2ts is not in BDMV/STREAM"
                break
            streams.append(f)
        titles.append(Title(key=name, streams=tuple(streams),
                            duration=None if note else seconds, note=note,
                            play_items=items))
    # ONE unreadable playlist is enough to refuse the whole disc: a title the
    # disc states but this app cannot read may well be the feature, and taking
    # the longest of the playlists that DID parse would be a guess about which
    # title is the main one (a 2-minute trailer would win over an unreadable
    # two-hour feature). The disc is asked about instead.
    note = ""
    if unreadable:
        note = ("playlists in BDMV/PLAYLIST could not be read, so the disc's "
                "titles cannot be known: " + "; ".join(unreadable))
    elif not titles:
        note = "BDMV/PLAYLIST holds no playlist"
    return Disc(kind="bluray", folder=folder, structure=structure,
                titles=tuple(titles), note=note)


def _sum_durations(streams, probe):
    """The streams' total seconds, or None when one of them is unmeasurable."""
    if probe is None:
        return None
    total = 0.0
    for s in streams:
        try:
            d = probe(s)
        except Exception:
            return None
        if not d:
            return None
        total += float(d)
    return total


# --------------------------------------------------------------------------- #
# The pick
# --------------------------------------------------------------------------- #
def pick(disc, probe=None):
    """``(title | None, reason)`` — the disc's main feature, or why not.

    The longest title wins, and the pick REFUSES rather than guesses whenever
    "longest" is not an answer:

    * an ``.iso`` — nothing here can read inside one, so it must be mounted or
      extracted first;
    * a structure whose playlist or parts cannot be read (including a Blu-ray
      with ONE unreadable playlist: the titles it names are not all known);
    * a title whose streams cannot be measured (no duration to compare);
    * the runner-up within ``AMBIGUOUS_ABS_S``/``AMBIGUOUS_REL`` of the
      longest (two plausible features: the user's call, not the app's);
    * a Blu-ray playlist that does not play each of its clips once and in full
      — concatenating the files would then not reproduce the title.
    """
    if disc.kind == "iso":
        return None, (f"{os.path.basename(disc.structure)} is a disc image: nothing "
                      f"here can read inside an .iso — mount it (or extract it) "
                      f"and remux the folder it shows")
    if disc.note:
        return None, disc.note
    usable = [t for t in disc.titles if not t.note]
    if not usable:
        if disc.titles:
            return None, ("no title's streams could be used: " + "; ".join(
                f"{t.key} ({t.note})" for t in disc.titles))
        return None, "no title could be found in the structure"
    unmeasured = [t.key for t in usable if not t.duration]
    if unmeasured:
        return None, ("the streams' durations could not be read, so no title is "
                      "longer than another (" + ", ".join(unmeasured) + ")")
    ordered = sorted(usable, key=lambda t: t.duration, reverse=True)
    best = ordered[0]
    if len(ordered) > 1:
        second = ordered[1]
        if best.duration - second.duration < max(AMBIGUOUS_ABS_S,
                                                 AMBIGUOUS_REL * best.duration):
            return None, (
                f"the two longest titles are within "
                f"{int(round(AMBIGUOUS_REL * 100))}% of each other — "
                f"{describe(best)} vs {describe(second)} — so which one is the "
                f"feature is your call")
    if disc.kind == "bluray":
        problem = _concat_problem(best, probe)
        if problem:
            return None, problem
    return best, ""


def _concat_problem(title, probe):
    """Why concatenating this title's streams would not BE the title, or ""."""
    if len(set(title.streams)) != len(title.streams):
        return ("the playlist plays one of its clips more than once — the files "
                "concatenated in order would not be the title")
    if not probe:
        return "the playlist's clips could not be measured (no ffprobe)"
    for item, path in zip(title.play_items, title.streams):
        try:
            measured = probe(path)
        except Exception:
            measured = None
        if not measured:
            return f"{os.path.basename(path)} could not be measured"
        if abs(item.seconds - measured) > max(CONCAT_ABS_S, CONCAT_REL * measured):
            return (f"the playlist plays {fmt_seconds(item.seconds)} of "
                    f"{os.path.basename(path)} ({fmt_seconds(measured)}), so the "
                    f"file is not the title and concatenating the streams would "
                    f"not reproduce it")
    return ""


# --------------------------------------------------------------------------- #
# Presentation and the concat list
# --------------------------------------------------------------------------- #
def fmt_seconds(seconds):
    """``1:32:10`` / ``12:04`` / ``48s`` for a duration, ``?`` when unknown."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "?"
    if total < 0:
        return "?"
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    if m:
        return f"{m}:{s:02d}"
    return f"{s}s"


def describe(title):
    """``VTS_01 (3 parts, 1:32:10)`` — one title, for a log line or a prompt."""
    return (f"{title.key} ({title.parts} part{'s' if title.parts != 1 else ''}, "
            f"{fmt_seconds(title.duration)})")


def candidates(disc):
    """Every title of *disc*, longest first, as ``describe`` strings."""
    ordered = sorted(disc.titles, key=lambda t: t.duration or 0, reverse=True)
    return [describe(t) for t in ordered]


def output_stem(disc):
    """The path whose stem names the remuxed MKV: ``<folder>/<folder>``.

    A disc structure becomes ONE file beside the structure it replaces, named
    after the folder that holds it — the folder the caller pointed at.
    """
    folder = os.path.normpath(disc.folder)
    return os.path.join(folder, os.path.basename(folder))


def write_concat_list(streams, path):
    """Write the ffmpeg concat demuxer list for *streams*, in order.

    The concat demuxer is how several files are handed to ffmpeg as ONE input
    with every stream copied (``-f concat -safe 0 -i list``): the streams are
    read in order and muxed, never re-encoded. Absolute paths with forward
    slashes, a single quote escaped the way the demuxer's own tokenizer wants.
    """
    lines = ["ffconcat version 1.0"]
    for s in streams:
        p = str(s).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{p}'")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return path
