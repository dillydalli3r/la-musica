#!/usr/bin/env python3
"""MusicBrainz metadata: everything the release states, written and kept.

The app pulled a MINIMAL slice of what MusicBrainz offers — the release's
identity, the recording ids and, at best, one ISRC — and dropped the rest of
the relationship table: no performer, no producer, no engineer, no mixer, no
arranger, no DJ-mixer, no conductor, no writer, no barcode, no ASIN, no
language, no disc title, and only the FIRST ISRC of a track that has several.
This file pins the contract of writing all of it:

  * the credit tags are part of the app's ONE vocabulary (mlo.audio.TAG_MAP)
    and every one of them survives the excess-tags predicate
    (mlo.grader.tag_key_allowed) — the same predicate Optimize FLACs and
    Format all strip with, so a tag this repo writes can never be a tag the
    strip pass deletes (or the grade fails);
  * a MULTI-VALUE credit is stored as REPEATED container fields, never as one
    "; "-joined blob: two performers are two Vorbis comments, two TMCL pairs,
    two MP4 atoms — and they read back as the same list on every container.
    A video container holds ONE string per key and so cannot repeat: there the
    list is stored as its "; "-joined value, never as a Python repr (`str(v)`
    put "['Alice', 'Bob']" in an MKV through /api/mb/assign and
    /api/tags/bulk), and tag_values() reads its parts back;
  * the ID3 spellings are Picard's own (TIPL for the involvement roles, TMCL
    for the musician credits, TPE3 for the conductor, TLAN/TSST for the
    language and disc title), including the v2.3 spelling of the people list
    (IPLS) — a v2.3 file must read back what was written to it;
  * a field the release does NOT state is never written as an empty tag; a
    SCALAR tag that already holds a value is never overwritten, a LIST tag it
    already holds is COMPLETED (the file's own values first, then the ones it
    does not state), and a re-run over a completed file writes nothing at all;
  * a tag a container REFUSES is reported per file instead of being silently
    skipped;
  * the excess check still catches a genuinely foreign tag in the same album
    (the check has not been widened into uselessness by the new vocabulary).

Run:  python tools/test_mb_metadata.py   (exit 0 = pass, 1 = failure)
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo.audio import TAG_MAP, AudioFile, _split_performer  # noqa: E402
from mlo.autotag import (mb_track_tags, write_mb_tags, _fill_release_tags,  # noqa: E402
                         _CREDIT_ROLES, _relation_credits, _track_credits,
                         _work_credits)
from mlo.grader import _grade_album, tag_key_allowed  # noqa: E402

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


def _dep_exe(pattern):
    for cand in glob.glob(os.path.join(ROOT, ".dependencies", "*", pattern)):
        if os.path.isfile(cand):
            return cand
    return None


FLAC_EXE = _dep_exe("flac.exe")
FFMPEG_EXE = _dep_exe("ffmpeg.exe")
TMP = tempfile.mkdtemp(prefix="mlo_mb_metadata_")


def make_flac(path):
    """A real (silent) FLAC, so the writes below go through the real writer."""
    assert FLAC_EXE, "flac.exe not found under .dependencies"
    wav = path + ".wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 4410)
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                   check=True, capture_output=True)
    os.remove(wav)


def make_mp3(path):
    """A minimal valid MPEG-1 Layer III stream (no bundled mp3 encoder)."""
    frame = bytes.fromhex("ff fb 10 00") + b"\x00" * 100
    with open(path, "wb") as f:
        f.write(frame * 60)
    from mutagen.mp3 import MP3
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    audio.save()


def make_m4a(path):
    """A real .m4a through ffmpeg, or None when ffmpeg is not installed."""
    if not FFMPEG_EXE:
        return False
    subprocess.run([FFMPEG_EXE, "-v", "quiet", "-y", "-f", "lavfi",
                    "-i", "anullsrc=r=44100:cl=stereo", "-t", "1",
                    "-c:a", "aac", path], check=True, capture_output=True)
    return True


def make_video(path, vcodec, acodec):
    """A real video container through ffmpeg (1 s of testsrc + a tone).

    Only encoders every ffmpeg build has (ffv1/mpeg4, flac/aac), so the
    fixture never depends on a libx264 this build may not carry. False when
    ffmpeg is not installed.
    """
    if not FFMPEG_EXE:
        return False
    subprocess.run([FFMPEG_EXE, "-v", "quiet", "-y",
                    "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=5",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                    "-t", "1", "-map", "0:v", "-map", "1:a",
                    "-c:v", vcodec, "-c:a", acodec, path],
                   check=True, capture_output=True)
    return True


def raw_video_tags(path):
    """The container's OWN metadata, read by ffprobe — what is really on disk.

    A video file has no mutagen reader, so this is the raw view: the string
    the tag write left in the file, not the app's reading of it.
    """
    probe = _dep_exe("ffprobe.exe")
    assert probe, "ffprobe.exe not found under .dependencies"
    out = subprocess.run([probe, "-v", "quiet", "-print_format", "json",
                          "-show_format", path], check=True,
                         capture_output=True, text=True).stdout
    tags = (json.loads(out).get("format") or {}).get("tags") or {}
    return {str(k).upper(): str(v) for k, v in tags.items()}


# --------------------------------------------------------------------------- #
print("== the vocabulary: one tag set, one allowance predicate ==")
# Every credit this app writes is a TAG_MAP entry — the repo's one vocabulary
# — and passes the excess predicate the grade and the strip passes share.
_CREDIT_TAGS = ("PERFORMER", "PRODUCER", "ENGINEER", "MIXER", "ARRANGER",
                "DJMIXER", "CONDUCTOR", "WRITER", "DIRECTOR", "COMPOSERSORT",
                "MUSICBRAINZ_COMPOSERID")
_RELEASE_TAGS = ("ASIN", "LANGUAGE", "DISCSUBTITLE")
for _tag in _CREDIT_TAGS + _RELEASE_TAGS:
    ok(_tag in TAG_MAP, f"{_tag} is part of the app's tag vocabulary")
    ok(tag_key_allowed(_tag),
       f"{_tag} survives the excess-tags predicate (what the strip passes use)")
# The ID3 frame each role is written to, spelled Picard's way. The frames
# themselves are not standalone allowed names (the predicate judges a tag by
# its semantic name — see the all_tags() assertions below), so this is about
# the WRITE half of each entry.
ok(TAG_MAP["PRODUCER"]["mp3"] == ("TIPL", "producer"),
   "PRODUCER is written to ID3's TIPL involvement list (Picard's spelling)")
ok(TAG_MAP["PERFORMER"]["mp3"] == ("TMCL", None),
   "PERFORMER is written to ID3's TMCL musician list")
ok(TAG_MAP["CONDUCTOR"]["mp3"] == ("TPE3", None),
   "CONDUCTOR uses the standard TPE3 frame, not a TXXX")
ok(TAG_MAP["LANGUAGE"]["mp3"] == ("TLAN", None)
   and TAG_MAP["DISCSUBTITLE"]["mp3"][0] == ("TSST", None),
   "LANGUAGE/DISCSUBTITLE use their standard ID3 frames (TLAN / TSST), "
   "with a v2.3-safe fallback for the disc title")
ok(TAG_MAP["PERFORMER"]["flac"] == "PERFORMER"
   and TAG_MAP["ISRC"]["flac"] == "ISRC",
   "and the Vorbis names are Picard's uppercase spellings")

# --------------------------------------------------------------------------- #
print("== one release payload, read for every credit it states ==")
# The shape MusicBrainz answers with (one recording's relation list, verbatim
# from a real release lookup): instrument and vocal relations with their
# attributes, the involvement roles, and the work with its songwriters.
_RELATIONS = [
    {"type": "producer", "target-type": "artist", "attributes": [],
     "artist": {"name": "Rick Rubin", "id": "mbid-producer"}},
    {"type": "engineer", "target-type": "artist", "attributes": [],
     "artist": {"name": "Sylvia Massy", "id": "mbid-engineer"}},
    {"type": "engineer", "target-type": "artist", "attributes": ["assistant"],
     "artist": {"name": "Greg Fidelman", "id": "mbid-engineer2"}},
    {"type": "mix", "target-type": "artist", "attributes": [],
     "artist": {"name": "David Sardy", "id": "mbid-mixer"}},
    {"type": "DJ-mix", "target-type": "artist", "attributes": [],
     "artist": {"name": "A DJ", "id": "mbid-dj"}},
    {"type": "arranger", "target-type": "artist", "attributes": [],
     "artist": {"name": "An Arranger", "id": "mbid-arranger"}},
    {"type": "conductor", "target-type": "artist", "attributes": [],
     "artist": {"name": "A Conductor", "id": "mbid-conductor"}},
    {"type": "remixer", "target-type": "artist", "attributes": [],
     "artist": {"name": "A Remixer", "id": "mbid-remixer"}},
    {"type": "instrument", "target-type": "artist",
     "attributes": ["drums (drum set)"],
     "artist": {"name": "John Dolmayan", "id": "mbid-drums"}},
    {"type": "instrument", "target-type": "artist", "attributes": ["guitar"],
     "artist": {"name": "Daron Malakian", "id": "mbid-guitar"}},
    {"type": "vocal", "target-type": "artist", "attributes": [],
     "artist": {"name": "Serj Tankian", "id": "mbid-vocals"}},
    {"type": "vocal", "target-type": "artist",
     "attributes": ["background vocals"],
     "artist": {"name": "Daron Malakian", "id": "mbid-guitar"}},
    # A relation type the app has no tag for (MusicBrainz's mastering credit)
    # is not invented into a wrong one: it is simply not read.
    {"type": "mastering", "target-type": "artist", "attributes": [],
     "artist": {"name": "A Mastering Engineer", "id": "mbid-master"}},
    {"type": "performance", "target-type": "work", "attributes": [],
     "number": "1",
     "work": {"id": "mbid-work", "title": "War?", "type": "Song",
              "relations": [
                  {"type": "composer", "target-type": "artist", "attributes": [],
                   "artist": {"name": "Daron Malakian", "id": "mbid-guitar"}},
                  {"type": "lyricist", "target-type": "artist", "attributes": [],
                   "artist": {"name": "Serj Tankian", "id": "mbid-vocals"}},
                  {"type": "writer", "target-type": "artist", "attributes": [],
                   "artist": {"name": "A Writer", "id": "mbid-writer"}},
              ]}},
]
_CREDITS = _relation_credits(_RELATIONS, _CREDIT_ROLES)
_WORK_CREDITS, _WORK, _WORK_MBID, _MOVEMENT, _MOVEMENT_NUMBER = _work_credits(
    {"relations": _RELATIONS})

ok(_CREDITS.get("PERFORMER") == [
    "John Dolmayan (drums (drum set))", "Daron Malakian (guitar)",
    "Serj Tankian (vocals)", "Daron Malakian (background vocals)"],
   f"every performer is read, instrument and all, in MusicBrainz's order "
   f"({_CREDITS.get('PERFORMER')})")
ok(_CREDITS.get("PRODUCER") == ["Rick Rubin"]
   and _CREDITS.get("ENGINEER") == ["Sylvia Massy", "Greg Fidelman"]
   and _CREDITS.get("MIXER") == ["David Sardy"]
   and _CREDITS.get("ARRANGER") == ["An Arranger"]
   and _CREDITS.get("DJMIXER") == ["A DJ"]
   and _CREDITS.get("CONDUCTOR") == ["A Conductor"]
   and _CREDITS.get("REMIXER") == ["A Remixer"],
   "every involvement role MusicBrainz states becomes its own credit list")
ok("MASTERING" not in _CREDITS and "A Mastering Engineer" not in json.dumps(_CREDITS),
   "a relationship with no tag in the vocabulary is not invented into one")
ok(_WORK_CREDITS.get("COMPOSER") == ["Daron Malakian"]
   and _WORK_CREDITS.get("LYRICIST") == ["Serj Tankian"]
   and _WORK_CREDITS.get("WRITER") == ["A Writer"],
   "the work's songwriters are read from its own relation list")
ok(_WORK_CREDITS.get("MUSICBRAINZ_COMPOSERID") == ["mbid-guitar"],
   "and the composer's MusicBrainz id rides beside the name")
ok(_WORK == "War?" and _WORK_MBID == "mbid-work" and not _MOVEMENT,
   f"the work is read as the work it is ({_WORK!r})")

# --------------------------------------------------------------------------- #
print("== the release payload, written onto real files ==")
_RELEASE = {
    "id": "release-1",
    "release_group_id": "group-1",
    "album_artist_mbid": "artist-1",
    "tracks": {(1, 1): {
        "recording_mbid": "recording-1",
        "artist_mbid": "artist-1",
        "release_track_mbid": "release-track-1",
        "isrcs": ["USSM19800758", "USSM19800763"],
        "credits": _track_credits({"relations": _RELATIONS}),
    }},
    "label": "American Recordings",
    "catalog_number": "SRCS 8757",
    "country": "JP",
    "countries": [{"code": "JP"}, {"code": "US"}],
    "status": "Official",
    "release_type": "Album",
    "date": "1998-10-01",
    "originaldate": "1998-06-30",
    "medium": "CD",
    "language": "eng",
    "script": "Latn",
    "barcode": "4988009875798",
    "asin": "B00000I78Y",
    "license": "https://creativecommons.org/licenses/by/3.0/",
    "medium_titles": {1: "Disc 1: The Album"},
}

_VALUES = mb_track_tags(_RELEASE, _RELEASE["tracks"][(1, 1)], disc=1,
                        album_artist_mbid="artist-1")
_WANT = {
    "MUSICBRAINZ_TRACKID": "recording-1",
    "MUSICBRAINZ_RELEASETRACKID": "release-track-1",
    "MUSICBRAINZ_ARTISTID": "artist-1",
    "MUSICBRAINZ_COMPOSERID": "mbid-guitar",
    "PERFORMER": ["John Dolmayan (drums (drum set))", "Daron Malakian (guitar)",
                  "Serj Tankian (vocals)", "Daron Malakian (background vocals)"],
    "PRODUCER": ["Rick Rubin"],
    "ENGINEER": ["Sylvia Massy", "Greg Fidelman"],
    "MIXER": ["David Sardy"],
    "ARRANGER": ["An Arranger"],
    "DJMIXER": ["A DJ"],
    "CONDUCTOR": ["A Conductor"],
    "REMIXER": ["A Remixer"],
    "COMPOSER": ["Daron Malakian"],
    "LYRICIST": ["Serj Tankian"],
    "WRITER": ["A Writer"],
    "WORK": "War?",
    "MUSICBRAINZ_WORKID": "mbid-work",
    "ISRC": ["USSM19800758", "USSM19800763"],
    "LABEL": "American Recordings",
    "CATALOGNUMBER": "SRCS 8757",
    "BARCODE": "4988009875798",
    "ASIN": "B00000I78Y",
    "LANGUAGE": "eng",
    "SCRIPT": "Latn",
    "DISCSUBTITLE": "Disc 1: The Album",
    "LICENSE": "https://creativecommons.org/licenses/by/3.0/",
    "RELEASESTATUS": "Official",
    "MEDIA": "CD",
    "DATE": "1998-10-01",
    "ORIGINALDATE": "1998-06-30",
    "RELEASETYPE": "Album",
    "MUSICBRAINZ_ALBUMID": "release-1",
    "MUSICBRAINZ_RELEASEGROUPID": "group-1",
    "MUSICBRAINZ_ALBUMARTISTID": "artist-1",
    "RELEASECOUNTRY": "JP; US",
}
ok(all([v for t, v in _VALUES if t == k] for k in _WANT),
   "mb_track_tags() offers a value for every field of the release")


def write(path, values=None):
    af = AudioFile(path)
    af.defer_save(True)
    written, refused = write_mb_tags(af, values if values is not None else _VALUES,
                                     None)
    af.defer_save(False)
    return af, written, refused


def read_back(path):
    """{tag: [values]} + the excess verdict, through the app's own reader."""
    af = AudioFile(path)
    values = {tag: af.tag_values(tag) for tag in _WANT}
    excess = sorted(k for k in af.all_tags() if not tag_key_allowed(k))
    return values, excess


def check_container(path, label, repeats=("PERFORMER", "ENGINEER", "ISRC")):
    """Every wanted field present, lists repeated, nothing excess.

    *repeats* names the tags whose VALUES must come back as that many pieces.
    A container that physically cannot hold repeats is checked apart: ID3v2.3
    has one string per text frame, so mutagen writes a multi-value ISRC in
    that version's own "/"-separated spelling (see the v2.3 block below).
    """
    values, excess = read_back(path)
    missing = [tag for tag in _WANT if not values.get(tag)]
    ok(not missing, f"{label}: every MusicBrainz field is on the file "
                   f"(missing {missing})")
    for tag in repeats:
        ok(len(values.get(tag) or []) == len(_WANT[tag]),
           f"{label}: {tag} survives as {len(_WANT[tag])} repeated values, "
           f"not one joined blob ({values.get(tag)})")
    ok(not excess, f"{label}: nothing reads as an excess tag ({excess})")
    return values


flac = os.path.join(TMP, "track.flac")
make_flac(flac)
_af, _written, _refused = write(flac)
ok(_written >= len(_WANT) and not _refused,
   f"the FLAC took {_written} tags without a refusal ({_refused})")
check_container(flac, "FLAC")

# The container itself holds REPEATED comments — the point of the whole rule.
from mutagen.flac import FLAC  # noqa: E402

_raw = FLAC(flac)
ok(_raw["PERFORMER"] == _WANT["PERFORMER"],
   f"the file holds one PERFORMER comment per credit ({_raw['PERFORMER']})")
ok(_raw["ISRC"] == _WANT["ISRC"],
   f"and one ISRC comment per ISRC ({_raw['ISRC']})")

mp3 = os.path.join(TMP, "track.mp3")
make_mp3(mp3)
_af, _written, _refused = write(mp3)
ok(_written >= len(_WANT) and not _refused,
   f"the MP3 took {_written} tags without a refusal ({_refused})")
check_container(mp3, "MP3")

from mutagen.id3 import ID3  # noqa: E402

_frames = ID3(mp3)
_tmcl = _frames.getall("TMCL")
ok(len(_tmcl) == 1 and [list(p) for p in _tmcl[0].people] == [
    ["drums (drum set)", "John Dolmayan"], ["guitar", "Daron Malakian"],
    ["vocals", "Serj Tankian"], ["background vocals", "Daron Malakian"]],
   f"MP3 stores the performers in ONE TMCL frame, one pair per credit "
   f"({[list(p) for p in _tmcl[0].people] if _tmcl else None})")
_tipl = _frames.getall("TIPL")
ok(len(_tipl) == 1 and [list(p) for p in _tipl[0].people] == [
    ["producer", "Rick Rubin"], ["engineer", "Sylvia Massy"],
    ["engineer", "Greg Fidelman"], ["mix", "David Sardy"],
    ["DJ-mix", "A DJ"], ["arranger", "An Arranger"]],
   f"and the involvement roles in ONE TIPL frame, keyed by role "
   f"({[list(p) for p in _tipl[0].people] if _tipl else None})")
ok([f.text for f in _frames.getall("TPE3")] == [["A Conductor"]]
   and [f.text for f in _frames.getall("TPE4")] == [["A Remixer"]],
   "the conductor and the remixer keep their own standard frames "
   "(TPE3 / TPE4), as Picard writes them")
ok([f.text for f in _frames.getall("TSRC")][0] == _WANT["ISRC"],
   "the ID3 ISRC frame holds both ISRCs")

# A file that arrives as ID3v2.3 stores the people list as IPLS (v2.3 has no
# TMCL) — and must still read back as the same credits, or the app's own write
# would be invisible to its own reader.
mp3_v23 = os.path.join(TMP, "track_v23.mp3")
make_mp3(mp3_v23)
_af = AudioFile(mp3_v23)
_af.id3_version = 3
_af.defer_save(True)
write_mb_tags(_af, _VALUES, None)
_af.defer_save(False)
_v23_values = check_container(mp3_v23, "MP3 (ID3v2.3)",
                              repeats=("PERFORMER", "ENGINEER"))
# ID3v2.3 has ONE string per text frame, so its own spelling of a multi-value
# ISRC frame is the "/"-separated list (which is how the version defines
# TSRC) — both ISRCs are still on the file, in the version's shape.
ok(sorted(str(_v23_values["ISRC"][0]).replace("/", " ").split())
   == sorted(_WANT["ISRC"]),
   f"a v2.3 file keeps BOTH ISRCs, in v2.3's own '/' spelling "
   f"({_v23_values['ISRC']})")
_v23_people = [tuple(p) for f in (ID3(mp3_v23).getall("TIPL")
                                  + ID3(mp3_v23).getall("IPLS"))
               for p in f.people]
ok(("guitar", "Daron Malakian") in _v23_people
   and ("producer", "Rick Rubin") in _v23_people,
   f"the v2.3 file keeps performers AND involvement roles in its one people "
   f"list ({_v23_people[:3]}…)")

m4a = os.path.join(TMP, "track.m4a")
if make_m4a(m4a):
    _af, _written, _refused = write(m4a)
    ok(_written >= len(_WANT) and not _refused,
       f"the MP4 took {_written} tags without a refusal ({_refused})")
    check_container(m4a, "MP4")
    _values, _excess = read_back(m4a)
    ok(len(_values["PERFORMER"]) == len(_WANT["PERFORMER"]),
       "the MP4 freeform atoms hold one value per credit too")
else:
    print("  -- skipped: ffmpeg is not installed, so no .m4a fixture")

# --------------------------------------------------------------------------- #
print("== a video container stores a list the one spelling it can ==")
# A video's metadata block is a flat key -> STRING map (ffmpeg's
# `-metadata name=value` takes one value), so a list written there has to be
# the "; "-joined value every reader of a repeated field joins to. The write
# used to stringify whatever it got: `set_tag("PERFORMER", ["Alice", "Bob"])`
# on an MKV — what /api/mb/assign and /api/tags/bulk hand over — stored the
# Python repr "['Alice', 'Bob']" as the credit's name, and tag_values() handed
# back nothing at all.
# Fixtures of their own folder: the excess-tag check below grades the FLAC
# album's whole directory, and a container ffmpeg wrote carries its own muxer
# metadata (ENCODER, DURATION) that has nothing to do with this contract.
_VIDEO_DIR = os.path.join(TMP, "video")
os.makedirs(_VIDEO_DIR, exist_ok=True)
mkv = os.path.join(_VIDEO_DIR, "video.mkv")
if make_video(mkv, "ffv1", "flac"):
    _af = AudioFile(mkv)
    ok(_af.kind == "video", f"an .mkv takes the ffmpeg video path ({_af.kind})")
    ok(_af.set_tag("PERFORMER", ["Alice", "Bob"]),
       f"a list writes to a real MKV ({_af.error})")
    ok(_af.set_tag("ISRC", ["USSM19800758", "USSM19800763"]),
       f"and so does a two-ISRC list ({_af.error})")
    _disk = raw_video_tags(mkv)
    ok(_disk.get("PERFORMER") == "Alice; Bob",
       f"the container holds the '; '-joined value, not a Python repr "
       f"({_disk.get('PERFORMER')!r})")
    ok(_disk.get("ISRC") == "USSM19800758; USSM19800763",
       f"every ISRC is on the file, '; '-joined ({_disk.get('ISRC')!r})")
    # …and the app's own reader sees the same value: the string, and the
    # pieces a writer needs back.
    _fresh = AudioFile(mkv)
    ok(_fresh.get_tag("PERFORMER") == "Alice; Bob",
       f"get_tag reads the joined value back ({_fresh.get_tag('PERFORMER')!r})")
    ok(_fresh.tag_values("PERFORMER") == ["Alice", "Bob"],
       f"tag_values hands the pieces back "
       f"({_fresh.tag_values('PERFORMER')})")
    ok(_fresh.tag_values("ISRC") == ["USSM19800758", "USSM19800763"],
       f"and the same for the ISRC list ({_fresh.tag_values('ISRC')})")
    ok(_fresh.all_tags().get("PERFORMER") == "Alice; Bob",
       f"all_tags agrees with get_tag ({_fresh.all_tags().get('PERFORMER')!r})")
else:
    print("  -- skipped: ffmpeg is not installed, so no .mkv fixture")

# The other video-flavoured container: .m4v is the MP4 family, which mutagen
# writes itself (repeated atoms, one per value) — the same list, read back the
# same way, so a credit is one value per container family.
m4v = os.path.join(_VIDEO_DIR, "video.m4v")
if make_video(m4v, "mpeg4", "aac"):
    _af = AudioFile(m4v)
    ok(_af.kind == "mp4",
       f"an .m4v is the MP4 container the tag layer writes itself ({_af.kind})")
    ok(_af.set_tag("PERFORMER", ["Alice", "Bob"]),
       f"the list writes to the .m4v ({_af.error})")
    _fresh = AudioFile(m4v)
    ok(_fresh.tag_values("PERFORMER") == ["Alice", "Bob"],
       f"the MP4 atoms hold one value per credit "
       f"({_fresh.tag_values('PERFORMER')})")
    ok(_fresh.get_tag("PERFORMER") == "Alice; Bob",
       f"and get_tag joins them like every other container "
       f"({_fresh.get_tag('PERFORMER')!r})")
else:
    print("  -- skipped: ffmpeg is not installed, so no .m4v fixture")

# --------------------------------------------------------------------------- #
print("== the grade: a credit passes, a foreign tag still fails ==")
# Every check but the excess one is off, so an issue list is ONLY about tags:
# a foreign tag MUST fail, and the same album's MusicBrainz credits MUST NOT.
_ONLY_EXCESS = {
    "music_folder": os.path.dirname(flac),
    "grade_check_excess_tags": True,
    "strip_unknown_tags": True,
}
_grade = _grade_album(os.path.dirname(flac), "EMBEDDED", _ONLY_EXCESS)
_tags_issues = [i for i in _grade["issues"] if i.startswith("Excess tags:")]
ok(not _tags_issues,
   f"the graded album carries the whole credit table with no excess tag "
   f"({_tags_issues})")

from mutagen.flac import FLAC as _FLAC  # noqa: E402

_junk = _FLAC(flac)
_junk["RIPPED_BY"] = ["some ripper"]
_junk["VENDOR_JUNK"] = ["1"]
_junk.save()
_grade = _grade_album(os.path.dirname(flac), "EMBEDDED", _ONLY_EXCESS)
_tags_issues = [i for i in _grade["issues"] if i.startswith("Excess tags:")]
ok(len(_tags_issues) == 1
   and "ripped_by" in _tags_issues[0].lower()
   and "vendor_junk" in _tags_issues[0].lower(),
   f"a genuinely foreign tag still fails the same check, by name "
   f"({_tags_issues})")
ok(all(tag_key_allowed(t) for t in _WANT),
   "…while every tag this app now writes stays inside the vocabulary")
_junk = _FLAC(flac)
for _key in ("RIPPED_BY", "VENDOR_JUNK"):
    del _junk[_key]
_junk.save()

# --------------------------------------------------------------------------- #
print("== preserve: an absent field is not written, a value is never replaced ==")
# A release that states no credits and no ISRC for the track: the album-level
# values are written, and nothing empty is invented.
_BARE = dict(_RELEASE, tracks={(1, 1): {
    "recording_mbid": "recording-1", "artist_mbid": "artist-1",
    "release_track_mbid": "release-track-1", "isrcs": [], "credits": {}}})
bare = os.path.join(TMP, "bare.flac")
make_flac(bare)
_af = AudioFile(bare)
_af.defer_save(True)
write_mb_tags(_af, mb_track_tags(_BARE, _BARE["tracks"][(1, 1)], disc=1,
                                 album_artist_mbid="artist-1"), None)
_af.defer_save(False)
_bare_tags = AudioFile(bare).all_tags()
_forbidden = ("PERFORMER", "PRODUCER", "ENGINEER", "MIXER", "ISRC", "PRODUCER",
              "COMPOSER", "LYRICIST", "WORK", "ARRANGER", "CONDUCTOR")
ok(not [t for t in _forbidden if t in _bare_tags],
   f"a release that states no credits writes no blank credit tags "
   f"({[t for t in _forbidden if t in _bare_tags]})")
ok(_bare_tags.get("LABEL") == "American Recordings",
   "…while the release facts it does state are still written")

# A SCALAR tag that already holds ANOTHER tagger's value is kept — a re-run
# over the same file must change nothing at all.
_af = AudioFile(flac)
_af.set_tag("LABEL", "Some Other Label")
_af.defer_save(True)
_done, _ref = write_mb_tags(_af, _VALUES, None)
_af.defer_save(False)
ok(_done == 0 and not _ref,
   f"a second run writes NOTHING: every tag is already filled ({_done})")
ok(AudioFile(flac).get_tag("LABEL") == "Some Other Label",
   "and the scalar value another tagger wrote is still there, not overwritten")
_af = AudioFile(flac)
_af.delete_tag("LABEL")
_af.set_tag("LABEL", _WANT["LABEL"])

# A LIST tag is DATA, not one slot: the file states ONE engineer and the
# release states two, so the answer COMPLETES the list — the file's own value
# first, then the ones it does not already state. The old blanket "a tag that
# already has a value is kept" cut the release's two engineers down to
# whichever single credit the file happened to arrive with.
_af = AudioFile(flac)
_af.set_tag("ENGINEER", "A Third Engineer")
_af.defer_save(True)
_done, _ref = write_mb_tags(_af, _VALUES, None)
_af.defer_save(False)
_eng = AudioFile(flac).tag_values("ENGINEER")
ok(_done == 1 and not _ref,
   f"only the short list is written, and nothing else ({_done}, {_ref})")
ok(_eng == ["A Third Engineer"] + _WANT["ENGINEER"],
   f"the release's engineers are ADDED to the one the file already stated, "
   f"in that order ({_eng})")
# …the list is complete now, so the same write again changes nothing: the
# completion rule is idempotent, which is what the prescan and the "nothing
# to fill" report line rely on.
_af = AudioFile(flac)
_af.defer_save(True)
_again, _ref2 = write_mb_tags(_af, _VALUES, None)
_af.defer_save(False)
ok(_again == 0 and not _ref2,
   f"a re-run over the completed list writes nothing ({_again})")
_af = AudioFile(flac)
_af.delete_tag("ENGINEER")
_af.set_tag("ENGINEER", _WANT["ENGINEER"])

# --------------------------------------------------------------------------- #
print("== the album pass: the same fields, through _fill_release_tags ==")
# The stage itself, over a real album directory with the server's release
# reader stubbed — no socket is opened.
class _Integrations:
    """The two names mlo.autotag imports from server.integrations."""

    @staticmethod
    def mb_get_cached(endpoint, params=None, **kw):
        assert endpoint == "release/release-1", endpoint
        return _MB_JSON

    @staticmethod
    def release_countries(node, preferred="", release_id=""):
        """The server's own reader answers NORMALISED events ({"code": …}) —
        the tag is written from those, so the stub must answer them too."""
        out = []
        for event in node.get("release-events") or []:
            for code in ((event.get("area") or {}).get("iso-3166-1-codes")
                         or []):
                out.append({"code": code, "date": event.get("date") or ""})
        return out


_MB_JSON = {
    "id": "release-1",
    "title": "System of a Down",
    "status": "Official",
    "barcode": "4988009875798",
    "asin": "B00000I78Y",
    "country": "JP",
    "date": "1998-10-01",
    "text-representation": {"language": "eng", "script": "Latn"},
    "release-events": [{"date": "1998-10-01",
                        "area": {"iso-3166-1-codes": ["JP"]}},
                       {"date": "1998-11-03",
                        "area": {"iso-3166-1-codes": ["US"]}}],
    "release-group": {"id": "group-1", "primary-type": "Album",
                      "first-release-date": "1998-06-30"},
    "label-info": [{"catalog-number": "SRCS 8757",
                    "label": {"name": "American Recordings"}}],
    "artist-credit": [{"artist": {"id": "artist-1", "name": "System of a Down"}}],
    # A licence musicbrainz.org states for the whole release (a url relation).
    "relations": [{"type": "license", "target-type": "url",
                   "url": {"resource": "https://creativecommons.org/licenses/by/3.0/"}}],
    "media": [{"position": 1, "format": "CD", "title": "Disc 1: The Album",
               "tracks": [{
                   "id": "release-track-1", "position": 1, "title": "Suite-Pee",
                   "artist-credit": [{"artist": {"id": "artist-1"}}],
                   "recording": {"id": "recording-1", "title": "Suite-Pee",
                                 "isrcs": ["USSM19800758", "USSM19800763"],
                                 "relations": _RELATIONS}}]}],
}

_ALBUM = os.path.join(TMP, "album")
os.makedirs(_ALBUM, exist_ok=True)
_stage = os.path.join(_ALBUM, "1-01 Suite-Pee.flac")
make_flac(_stage)
_af = AudioFile(_stage)
_af.set_tag("MUSICBRAINZ_ALBUMID", "release-1")
_af.set_tag("TITLE", "Suite-Pee")
_af.set_tag("TRACKNUMBER", "1")
_af.set_tag("DISCNUMBER", "1")
_saved = sys.modules.get("server.integrations")
sys.modules["server.integrations"] = _Integrations
try:
    _written, _note = _fill_release_tags([{"af": AudioFile(_stage)}], {}, _ALBUM)
finally:
    if _saved is None:
        del sys.modules["server.integrations"]
    else:
        sys.modules["server.integrations"] = _saved
# One field short of the whole set: the fixture's album id is already on the
# file (it is what the stage looks the release up BY), and a tag that is
# already there is never rewritten.
ok(_written >= len(_WANT) - 1,
   f"the Auto Tagging stage wrote the whole MusicBrainz field set "
   f"({_written} tags: {_note})")
_stage_values, _stage_excess = read_back(_stage)
_stage_missing = [t for t in _WANT if not _stage_values.get(t)]
ok(not _stage_missing and not _stage_excess,
   f"every field reads back off the staged file, nothing excess "
   f"(missing {_stage_missing}, excess {_stage_excess})")
ok(_stage_values["ISRC"] == _WANT["ISRC"],
   f"both ISRCs landed ({_stage_values['ISRC']})")

# A SECOND run of the stage: every prescan slot and every credit is already
# there, so nothing is written and nothing is asked for.
_CALLS = []


class _IntegrationsCounting(_Integrations):
    @staticmethod
    def mb_get_cached(endpoint, params=None, **kw):
        _CALLS.append(endpoint)
        return _MB_JSON


_saved = sys.modules.get("server.integrations")
sys.modules["server.integrations"] = _IntegrationsCounting
try:
    _again, _note2 = _fill_release_tags([{"af": AudioFile(_stage)}], {}, _ALBUM)
finally:
    if _saved is None:
        del sys.modules["server.integrations"]
    else:
        sys.modules["server.integrations"] = _saved
ok(_again == 0 and not _CALLS,
   f"a re-run writes nothing and asks MusicBrainz nothing "
   f"({_again}, calls={_CALLS}, note={_note2!r})")

# --------------------------------------------------------------------------- #
print("== report: a container that cannot be tagged is named, not skipped ==")


class _Untaggable:
    """A file whose container this app cannot tag at all (mlo.audio answers
    None for a format mutagen has no writer for) — the shape a real .wv
    album arrives in."""

    def __init__(self, path):
        self.path = path
        self.audio = None
        self.error = ""

    def get_tag(self, name):
        # The album id is READ off the file (it is what the stage looks the
        # release up by) — everything else about this file is unreachable.
        return "release-1" if name == "MUSICBRAINZ_ALBUMID" else None

    def set_tag(self, name, value):
        return False


_saved = sys.modules.get("server.integrations")
sys.modules["server.integrations"] = _Integrations
try:
    _n, _note3 = _fill_release_tags(
        [{"af": _Untaggable(os.path.join(_ALBUM, "1-02 Whatever.wv"))}], {},
        _ALBUM)
finally:
    if _saved is None:
        del sys.modules["server.integrations"]
    else:
        sys.modules["server.integrations"] = _saved
ok("1-02 Whatever.wv" in _note3 and ("cannot" in _note3 or "refused" in _note3),
   f"a file that cannot be tagged is reported by name ({_note3!r})")

# --------------------------------------------------------------------------- #
print("== a movement's work is filed as a movement ==")
_MOV = dict(_RELATIONS[-1])
_MOV["work"] = dict(_MOV["work"], type="Movement")
_credits2, _work2, _wmbid2, _movement2, _number2 = _work_credits({"relations": [_MOV]})
ok(_movement2 == "War?" and not _work2 and _number2 == "1",
   f"a work MusicBrainz calls a movement lands in MOVEMENT, not WORK "
   f"({_movement2!r}, {_work2!r}, {_number2!r})")
ok(_split_performer("John Dolmayan (drums (drum set))")
   == ("drums (drum set)", "John Dolmayan"),
   "a credit whose instrument carries its own parentheses splits correctly")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nPASS — {passed} assertion(s)")
