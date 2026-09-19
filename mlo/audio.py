"""Unified tag abstraction over FLAC / OGG / Opus / MP3 / AAC / MP4 files."""
import os

from .deps import (
    FLAC, OggVorbis, OggOpus, MP3, MP4, MP4FreeForm,
    TXXX, USLT, COMM, UFID, Encoding, TextFrame, Frames, APIC, MP4Cover, Picture,
)
from .stats import _decode_mp4_value

TAG_MAP = {
    # Standard sorting/display fields. These are semantic names; the
    # container-specific keys below keep tag editing safe across formats.
    "TITLE": {
        "flac": "TITLE", "mp3": ("TIT2", None), "mp4": "\xa9nam",
    },
    "ALBUM": {
        "flac": "ALBUM", "mp3": ("TALB", None), "mp4": "\xa9alb",
    },
    "ARTIST": {
        "flac": "ARTIST", "mp3": ("TPE1", None), "mp4": "\xa9ART",
    },
    "ALBUMARTIST": {
        "flac": "ALBUMARTIST", "mp3": ("TPE2", None), "mp4": "aART",
    },
    "ALBUMARTISTSORT": {
        "flac": "ALBUMARTISTSORT", "mp3": ("TSO2", None), "mp4": "soaa",
    },
    "ARTISTSORT": {
        "flac": "ARTISTSORT", "mp3": ("TSOP", None), "mp4": "soar",
    },
    "TITLESORT": {
        "flac": "TITLESORT", "mp3": ("TSOT", None), "mp4": "sonm",
    },
    "TRACKNUMBER": {
        "flac": "TRACKNUMBER", "mp3": ("TRCK", None), "mp4": "trkn",
    },
    "DISCNUMBER": {
        "flac": "DISCNUMBER", "mp3": ("TPOS", None), "mp4": "disk",
    },
    "DATE": {
        "flac": "DATE", "mp3": ("TDRC", None), "mp4": "\xa9day",
    },
    "ORIGINALDATE": {
        # ID3 keeps the ORIGINAL release date in TDOR ("original release
        # time"), which is what Picard and beets' mediafile read — beets
        # never looks at TDRL, so a file tagged there carried an original
        # date the beets import (script 14) could not see, and the album
        # folder it computed lacked the date the MLO organizer's script
        # expects. TDRL (this app's older spelling) is still read, and is
        # replaced on the next write.
        "flac": "ORIGINALDATE",
        "mp3": (("TDOR", None), ("TDRL", None)),
        # Uppercase atom name: beets' mediafile lists ORIGINALDATE /
        # "ORIGINAL YEAR" (case-sensitive reads), so a lowercase atom was
        # invisible to beets — including the path it computes for an import.
        # Reading stays case-insensitive (_mp4_freeform_keys), so files this
        # app previously wrote keep their value.
        "mp4": ("freeform", "com.apple.iTunes", "ORIGINALDATE"),
    },
    "ORIGINALYEAR": {
        "flac": "ORIGINALYEAR", "mp3": ("TXXX", "ORIGINALYEAR"),
        "mp4": ("freeform", "com.apple.iTunes", "ORIGINALYEAR"),
    },
    # Release identity. The first ID3/MP4 spelling is the MusicBrainz spec
    # one (what Picard and beets write, so both see the app's tags and the
    # app reads theirs — including the folder name each side computes); the
    # second is what earlier versions of this app wrote and is still read
    # and cleaned up on the next write (see _mp3_specs).
    "RELEASETYPE": {
        "flac": "RELEASETYPE",
        "mp3": (("TXXX", "MusicBrainz Album Type"), ("TXXX", "RELEASETYPE")),
        "mp4": (("freeform", "com.apple.iTunes", "MusicBrainz Album Type"),
                ("freeform", "com.apple.iTunes", "RELEASETYPE")),
    },
    "RELEASESTATUS": {
        "flac": "RELEASESTATUS",
        "mp3": ("TXXX", "MusicBrainz Album Status"),
        "mp4": ("freeform", "com.apple.iTunes", "MusicBrainz Album Status"),
    },
    "RELEASECOUNTRY": {
        "flac": "RELEASECOUNTRY",
        "mp3": (("TXXX", "MusicBrainz Album Release Country"),
                ("TXXX", "RELEASECOUNTRY")),
        "mp4": (("freeform", "com.apple.iTunes",
                 "MusicBrainz Album Release Country"),
                ("freeform", "com.apple.iTunes", "RELEASECOUNTRY")),
    },
    "CATALOGNUMBER": {
        "flac": "CATALOGNUMBER", "mp3": ("TXXX", "CATALOGNUMBER"),
        "mp4": ("freeform", "com.apple.iTunes", "CATALOGNUMBER"),
    },
    # Barcode of the release (MusicBrainz `barcode`), Picard/beets spelling.
    "BARCODE": {
        "flac": "BARCODE", "mp3": ("TXXX", "BARCODE"),
        "mp4": ("freeform", "com.apple.iTunes", "BARCODE"),
    },
    # Writing system of the release's language (MusicBrainz `script`).
    "SCRIPT": {
        "flac": "SCRIPT", "mp3": ("TXXX", "SCRIPT"),
        "mp4": ("freeform", "com.apple.iTunes", "SCRIPT"),
    },
    # Record label. ID3 keeps it in the standard TPUB frame (what beets and
    # Picard write; TXXX:LABEL is not part of the MusicBrainz ID3 TXXX set),
    # MP4 in the freeform atom both write. Earlier versions of this app used
    # TXXX:LABEL, which is still read and replaced on the next write.
    "LABEL": {
        "flac": "LABEL",
        "mp3": (("TPUB", None), ("TXXX", "LABEL")),
        "mp4": ("freeform", "com.apple.iTunes", "LABEL"),
    },
    "TRACKTOTAL": {
        "flac": "TRACKTOTAL", "mp3": ("TXXX", "TRACKTOTAL"),
        "mp4": ("freeform", "com.apple.iTunes", "TRACKTOTAL"),
    },
    "DISCTOTAL": {
        "flac": "DISCTOTAL", "mp3": ("TXXX", "DISCTOTAL"),
        "mp4": ("freeform", "com.apple.iTunes", "DISCTOTAL"),
    },
    "ISRC": {
        "flac": "ISRC", "mp3": ("TSRC", None),
        "mp4": ("freeform", "com.apple.iTunes", "ISRC"),
    },
    "LICENSE": {
        "flac": "LICENSE", "mp3": ("TXXX", "LICENSE"),
        "mp4": ("freeform", "com.apple.iTunes", "LICENSE"),
    },
    "UNSYNCEDLYRICS": {
        "flac": "UNSYNCEDLYRICS", "mp3": ("TXXX", "UNSYNCEDLYRICS"),
        "mp4": ("freeform", "com.apple.iTunes", "UNSYNCEDLYRICS"),
    },
    "COMPOSER": {
        "flac": "COMPOSER", "mp3": ("TCOM", None), "mp4": "\xa9wrt",
    },
    "LYRICIST": {
        "flac": "LYRICIST", "mp3": ("TEXT", None),
        "mp4": ("freeform", "com.apple.iTunes", "LYRICIST"),
    },
    "REMIXER": {
        "flac": "REMIXER", "mp3": ("TPE4", None),
        "mp4": ("freeform", "com.apple.iTunes", "REMIXER"),
    },
    "COMMENT": {
        "flac": "COMMENT", "mp3": ("COMM", None), "mp4": "\xa9cmt",
    },
    "BPM": {
        "flac": "BPM", "mp3": ("TBPM", None), "mp4": "tmpo",
    },
    "INITIALKEY": {
        "flac": "INITIALKEY",
        "mp3": ("TXXX", "INITIALKEY"),
        "mp4": ("freeform", "com.apple.iTunes", "INITIALKEY"),
    },
    # Mood word computed by script 8 (see mlo.moods) — same freeform/TXXX
    # shape as INITIALKEY, so MP3/MP4 carry a real frame instead of the
    # synthetic one set_any_tag would invent.
    "MOOD": {
        "flac": "MOOD",
        "mp3": ("TXXX", "MOOD"),
        "mp4": ("freeform", "com.apple.iTunes", "MOOD"),
    },
    # Arousal the MOOD verdict was scored from, written next to it by
    # mlo.moods as an integer 0-100 (same freeform/TXXX shape as MOOD).
    "ENERGY": {
        "flac": "ENERGY",
        "mp3": ("TXXX", "ENERGY"),
        "mp4": ("freeform", "com.apple.iTunes", "ENERGY"),
    },
    # AcoustID identity (Picard-compatible). Written during import when a
    # fingerprint match is accepted; graded only when a file already carries
    # one of the two, so a library that never fingerprinted anything is never
    # failed for their absence.
    "ACOUSTID_ID": {
        "flac": "ACOUSTID_ID",
        "mp3": ("TXXX", "ACOUSTID_ID"),
        "mp4": ("freeform", "com.apple.iTunes", "ACOUSTID_ID"),
    },
    "ACOUSTID_FINGERPRINT": {
        "flac": "ACOUSTID_FINGERPRINT",
        "mp3": ("TXXX", "ACOUSTID_FINGERPRINT"),
        "mp4": ("freeform", "com.apple.iTunes", "ACOUSTID_FINGERPRINT"),
    },
    # Classical work/movement (Picard-compatible: ID3 MVNM/MVIN, TXXX:WORK)
    "WORK": {
        "flac": "WORK",
        "mp3": ("TXXX", "WORK"),
        "mp4": ("freeform", "com.apple.iTunes", "WORK"),
    },
    "MOVEMENT": {
        "flac": "MOVEMENT",
        "mp3": ("MVNM", None),
        "mp4": ("freeform", "com.apple.iTunes", "MOVEMENT"),
    },
    "MOVEMENTNUMBER": {
        "flac": "MOVEMENTNUMBER",
        "mp3": ("MVIN", None),
        "mp4": ("freeform", "com.apple.iTunes", "MOVEMENTNUMBER"),
    },
    "COPYRIGHT": {
        "flac": "COPYRIGHT", "mp3": ("TCOP", None), "mp4": "cprt",
    },
    "LYRICS": {
        "flac": "LYRICS", "mp3": ("USLT", None), "mp4": "\xa9lyr",
    },
    # Line-aligned lyric transforms (script 15): the romanized original and
    # the translated lyrics stored next to the main LYRICS tag. Freeform
    # TXXX / iTunes atoms because no standard ID3/MP4 frame exists for them.
    "TRANSLITERATION": {
        "flac": "TRANSLITERATION", "mp3": ("TXXX", "TRANSLITERATION"),
        "mp4": ("freeform", "com.apple.iTunes", "TRANSLITERATION"),
    },
    "TRANSLATION": {
        "flac": "TRANSLATION", "mp3": ("TXXX", "TRANSLATION"),
        "mp4": ("freeform", "com.apple.iTunes", "TRANSLATION"),
    },
    "GENRE": {
        "flac": "GENRE",
        "mp3": ("TCON", None),
        "mp4": "\xa9gen",
    },
    "ITUNESADVISORY": {
        "flac": "ITUNESADVISORY",
        "mp3": ("TXXX", "ITUNESADVISORY"),
        "mp4": ("freeform", "com.apple.iTunes", "ITUNESADVISORY"),
    },
    "ALBUMITUNESADVISORY": {
        "flac": "ALBUMITUNESADVISORY",
        "mp3": ("TXXX", "ALBUMITUNESADVISORY"),
        "mp4": ("freeform", "com.apple.iTunes", "ALBUMITUNESADVISORY"),
    },
    "REPLAYGAIN_TRACK_GAIN": {
        "flac": "REPLAYGAIN_TRACK_GAIN",
        "mp3": ("TXXX", "REPLAYGAIN_TRACK_GAIN"),
        "mp4": ("freeform", "com.apple.iTunes", "replaygain_track_gain"),
    },
    "REPLAYGAIN_TRACK_PEAK": {
        "flac": "REPLAYGAIN_TRACK_PEAK",
        "mp3": ("TXXX", "REPLAYGAIN_TRACK_PEAK"),
        "mp4": ("freeform", "com.apple.iTunes", "replaygain_track_peak"),
    },
    "REPLAYGAIN_ALBUM_GAIN": {
        "flac": "REPLAYGAIN_ALBUM_GAIN",
        "mp3": ("TXXX", "REPLAYGAIN_ALBUM_GAIN"),
        "mp4": ("freeform", "com.apple.iTunes", "replaygain_album_gain"),
    },
    "REPLAYGAIN_ALBUM_PEAK": {
        "flac": "REPLAYGAIN_ALBUM_PEAK",
        "mp3": ("TXXX", "REPLAYGAIN_ALBUM_PEAK"),
        "mp4": ("freeform", "com.apple.iTunes", "replaygain_album_peak"),
    },
    "DYNAMIC RANGE": {
        "flac": "DYNAMIC RANGE",
        "mp3": ("TXXX", "DYNAMIC RANGE"),
        "mp4": ("freeform", "com.apple.iTunes", "dynamic range"),
    },
    "ALBUM DYNAMIC RANGE": {
        "flac": "ALBUM DYNAMIC RANGE",
        "mp3": ("TXXX", "ALBUM DYNAMIC RANGE"),
        "mp4": ("freeform", "com.apple.iTunes", "album dynamic range"),
    },
    "INSTRUMENTAL": {
        "flac": "INSTRUMENTAL",
        "mp3": ("TXXX", "INSTRUMENTAL"),
        "mp4": ("freeform", "com.apple.iTunes", "INSTRUMENTAL"),
    },
    # Medium of the release. ID3's standard frame is TMED (what beets and
    # Picard write); TXXX:MEDIA is this app's older spelling, still read.
    "MEDIA": {
        "flac": "MEDIA",
        "mp3": (("TMED", None), ("TXXX", "MEDIA")),
        "mp4": ("freeform", "com.apple.iTunes", "MEDIA"),
    },
    "SOURCE": {
        "flac": "SOURCE",
        "mp3": ("TXXX", "SOURCE"),
        "mp4": ("freeform", "com.apple.iTunes", "SOURCE"),
    },
    # AudioAuditor verdict written by the Audit Library script.
    # Values: REAL / FAKE.
    "AUDIT": {
        "flac": "AUDIT",
        "mp3": ("TXXX", "AUDIT"),
        "mp4": ("freeform", "com.apple.iTunes", "AUDIT"),
    },
    # Manual REAL/FAKE override written from the track details editor. It is
    # documented as authoritative (it wins over every derived verdict), the
    # grader reads it by this name, and without an entry here get_tag()
    # returned None for it — so the override never reached the grader and the
    # app's own tag showed up as an EXCESS tag instead.
    "AUDIOAUDITOR_OVERRIDE": {
        "flac": "AUDIOAUDITOR_OVERRIDE",
        "mp3": ("TXXX", "AUDIOAUDITOR_OVERRIDE"),
        "mp4": ("freeform", "com.apple.iTunes", "AUDIOAUDITOR_OVERRIDE"),
    },
    # Compilation flag (1 on a various-artists release). Picard, beets and
    # iTunes all write it, the grader's vocabulary allows it — but with no
    # entry here get_tag() read nothing and set_tag() fell through to
    # set_any_tag(), which refuses "COMPILATION" on MP3/MP4 (not an ID3
    # frame ID, not a 4-character atom): the tag was visible to every other
    # tagger and unwritable to this one. ID3 keeps it in the iTunes TCMP
    # frame, MP4 in the boolean cpil atom.
    "COMPILATION": {
        "flac": "COMPILATION",
        "mp3": ("TCMP", None),
        "mp4": "cpil",
    },
    # Rip-log score (0-100) from AudioAuditor's cambia grading, written
    # to the tracks of MEDIA=CD releases only.
    "LOG_GRADE": {
        "flac": "LOG_GRADE",
        "mp3": ("TXXX", "LOG_GRADE"),
        "mp4": ("freeform", "com.apple.iTunes", "LOG_GRADE"),
    },
    # Integrity verdict tags:
    #   INTEGRITY  OK / FAIL verdict of the audio integrity test.
    #   LOG_CRC    CD rips: OK / MISMATCH vs the rip log's per-track CRC.
    # AUDIO_MD5 is accepted for legacy files (nothing writes it any more).
    "AUDIO_MD5": {
        "flac": "AUDIO_MD5",
        "mp3": ("TXXX", "AUDIO_MD5"),
        "mp4": ("freeform", "com.apple.iTunes", "AUDIO_MD5"),
    },
    "INTEGRITY": {
        "flac": "INTEGRITY",
        "mp3": ("TXXX", "INTEGRITY"),
        "mp4": ("freeform", "com.apple.iTunes", "INTEGRITY"),
    },
    "LOG_CRC": {
        "flac": "LOG_CRC",
        "mp3": ("TXXX", "LOG_CRC"),
        "mp4": ("freeform", "com.apple.iTunes", "LOG_CRC"),
    },
    # MusicBrainz identifiers (Picard-compatible names).
    "MUSICBRAINZ_ALBUMID": {
        "flac": "MUSICBRAINZ_ALBUMID",
        "mp3": ("TXXX", "MusicBrainz Album Id"),
        "mp4": ("freeform", "com.apple.iTunes", "MusicBrainz Album Id"),
    },
    "MUSICBRAINZ_ALBUMARTISTID": {
        "flac": "MUSICBRAINZ_ALBUMARTISTID",
        "mp3": ("TXXX", "MusicBrainz Album Artist Id"),
        "mp4": ("freeform", "com.apple.iTunes", "MusicBrainz Album Artist Id"),
    },
    "MUSICBRAINZ_ARTISTID": {
        "flac": "MUSICBRAINZ_ARTISTID",
        "mp3": ("TXXX", "MusicBrainz Artist Id"),
        "mp4": ("freeform", "com.apple.iTunes", "MusicBrainz Artist Id"),
    },
    # The RECORDING id. ID3 keeps it in a UFID frame owned by
    # musicbrainz.org (the MusicBrainz spec; mutagen's own
    # `musicbrainz_trackid`, beets and Picard all read/write it there, so a
    # TXXX:desc spelling is invisible to them). The TXXX spelling this app
    # used before is still read and dropped on the next write.
    "MUSICBRAINZ_TRACKID": {
        "flac": "MUSICBRAINZ_TRACKID",
        "mp3": (("UFID", "http://musicbrainz.org"),
                ("TXXX", "MusicBrainz Track Id")),
        "mp4": ("freeform", "com.apple.iTunes", "MusicBrainz Track Id"),
    },
    "MUSICBRAINZ_RELEASEID": {
        "flac": "MUSICBRAINZ_RELEASEID",
        "mp3": ("TXXX", "MusicBrainz Release Id"),
        "mp4": ("freeform", "com.apple.iTunes", "MusicBrainz Release Id"),
    },
    "MUSICBRAINZ_RELEASEGROUPID": {
        "flac": "MUSICBRAINZ_RELEASEGROUPID",
        "mp3": ("TXXX", "MusicBrainz Release Group Id"),
        "mp4": ("freeform", "com.apple.iTunes", "MusicBrainz Release Group Id"),
    },
    # The id of this track's POSITION on this release (distinct from
    # MUSICBRAINZ_TRACKID, the recording): the naming script offers both, and
    # beets/Picard write this one to every file they touch.
    "MUSICBRAINZ_RELEASETRACKID": {
        "flac": "MUSICBRAINZ_RELEASETRACKID",
        "mp3": ("TXXX", "MusicBrainz Release Track Id"),
        "mp4": ("freeform", "com.apple.iTunes", "MusicBrainz Release Track Id"),
    },
    # The work (composition) a classical track performs; its movement tags
    # below are only meaningful next to it.
    "MUSICBRAINZ_WORKID": {
        "flac": "MUSICBRAINZ_WORKID",
        "mp3": ("TXXX", "MusicBrainz Work Id"),
        "mp4": ("freeform", "com.apple.iTunes", "MusicBrainz Work Id"),
    },
    # RateYourMusic links (URLs).
    "RATEYOURMUSIC_ALBUM": {
        "flac": "RATEYOURMUSIC_ALBUM",
        "mp3": ("TXXX", "RATEYOURMUSIC_ALBUM"),
        "mp4": ("freeform", "com.apple.iTunes", "RATEYOURMUSIC_ALBUM"),
    },
    "RATEYOURMUSIC_TRACK": {
        "flac": "RATEYOURMUSIC_TRACK",
        "mp3": ("TXXX", "RATEYOURMUSIC_TRACK"),
        "mp4": ("freeform", "com.apple.iTunes", "RATEYOURMUSIC_TRACK"),
    },
    "RATEYOURMUSIC_ARTIST": {
        "flac": "RATEYOURMUSIC_ARTIST",
        "mp3": ("TXXX", "RATEYOURMUSIC_ARTIST"),
        "mp4": ("freeform", "com.apple.iTunes", "RATEYOURMUSIC_ARTIST"),
    },
}

# Containers whose tags are ID3v2 frames — mutagen's MP3 and ID3 objects share
# one frame API, so every ID3 branch below serves both. Raw ADTS .aac is the
# second one: mutagen's AAC reader cannot carry tags at all ("Tagging is not
# supported. Use the ID3/APEv2 classes directly instead"), so its tags live in
# a leading ID3v2 chunk — what Picard and foobar2000 write for .aac, and what
# ffmpeg/ffprobe skip past when decoding the stream.
_ID3_KINDS = ("mp3", "aac")

# Lyrics transforms carry the language in the tag NAME (TRANSLATION-EN,
# TRANSLITERATION-JA-LATN) — the same prefixes mlo.config keys the LYRICS
# family on — so they cannot be fixed TAG_MAP entries. See set_tag.
_LYRICS_TRANSFORM_PREFIXES = ("TRANSLATION-", "TRANSLITERATION-")


def _is_id3_frame_id(key):
    """True for a well-formed ID3v2 frame ID: four ASCII A-Z/0-9 bytes.

    Guards set_any_tag: mutagen happily accepts a frame whose ID is not a
    legal frame ID (an MP4 atom name like "©too" is four characters too) and
    then writes a header no parser can walk, which loses every tag on the
    file. Anything that is not a frame ID goes through TXXX:<name> instead."""
    key = str(key)
    return (len(key) == 4 and key.isascii() and key.isalnum()
            and key.isupper())


def _mp3_specs(spec):
    """ID3 spellings a TAG_MAP entry accepts, the written one first.

    Most fields have exactly one. A field may have several when taggers
    disagree on where it lives — the MusicBrainz spec's UFID frame vs. the
    older TXXX description — where every spelling must be READ (existing
    files carry either) but only one is written, and writing drops the other
    so a file never holds the same id twice.
    """
    raw = (spec or {}).get("mp3")
    if not raw:
        return ()
    if isinstance(raw[0], tuple):
        return tuple(raw)
    return (raw,)


def _mp4_specs(spec):
    """MP4 atom spellings a TAG_MAP entry accepts, the written one first.

    Same purpose as _mp3_specs: MP4 keeps some fields under more than one
    freeform atom name (Picard/beets vs. this app's older names). Values of
    the SAME atom name in another case are not aliases — freeform reads are
    case-insensitive already.
    """
    raw = (spec or {}).get("mp4")
    if not raw:
        return ()
    if isinstance(raw[0], tuple):
        return tuple(raw)
    return (raw,)


# Video containers routed through ffprobe/ffmpeg (MP4/M4V stay on mutagen).
VIDEO_FFMPEG_EXTS = (
    ".mkv", ".webm", ".mov", ".vob", ".mpg", ".mpeg", ".m2v",
    ".ts", ".m2ts", ".mts", ".avi", ".wmv", ".flv", ".ogv", ".3gp", ".3g2",
)

# Container-native aliases other taggers use inside video files, folded onto
# the semantic names (MKV muxers write MOV-style PART_NUMBER/DISC/SHOW keys).
_VIDEO_TAG_ALIASES = {
    "TRACK": "TRACKNUMBER",
    "TRACKNUM": "TRACKNUMBER",
    "PART_NUMBER": "TRACKNUMBER",
    "PART": "TRACKNUMBER",
    "TOTAL_PARTS": "TRACKTOTAL",
    "PART_TOTAL": "TRACKTOTAL",
    "DISC": "DISCNUMBER",
    "SHOW": "ALBUM",
    "COLLECTION": "ALBUM",
    "ALBUM_ARTIST": "ALBUMARTIST",
    "AARTIST": "ALBUMARTIST",
    "DATE_RELEASED": "DATE",
    "DATE_RELEASE": "DATE",
    "YEAR": "DATE",
    "RETAILDATE": "DATE",
    # Matroska tag names may not contain spaces: ffmpeg rewrites them with
    # underscores, so the DR pair is stored as DYNAMIC_RANGE — without these
    # the video tag writer was write-only for the two DR tags.
    "DYNAMIC_RANGE": "DYNAMIC RANGE",
    "ALBUM_DYNAMIC_RANGE": "ALBUM DYNAMIC RANGE",
}

# MKV muxers expose the track number as PART_NUMBER and the disc as DISC;
# every other semantic name is stored verbatim.
_MKV_TAG_KEYS = {"TRACKNUMBER": "PART_NUMBER", "DISCNUMBER": "DISC"}


def _to_mkv_meta(tags):
    """Container-native metadata keys for a semantic tag mapping."""
    return {_MKV_TAG_KEYS.get(str(k).upper(), k): v for k, v in tags.items()}


_FFPROBE_CACHE = {"exe": None, "checked": False}


def _ffprobe_exe():
    """Cached ffprobe path (tool detection walks the deps dir + PATH)."""
    if not _FFPROBE_CACHE["checked"]:
        try:
            from .tools import detect_all_tools
            _FFPROBE_CACHE["exe"] = (detect_all_tools().get("ffmpeg") or {}).get("ffprobe_exe")
        except Exception:
            _FFPROBE_CACHE["exe"] = None
        _FFPROBE_CACHE["checked"] = True
    return _FFPROBE_CACHE["exe"]


class AacHandle:
    """Tag + tech handle for a raw ADTS .aac stream.

    mutagen's AAC file type is read-only ("Tagging is not supported. Use the
    ID3/APEv2 classes directly instead"), so the tags live in a leading ID3v2
    chunk — what Picard and foobar2000 write for .aac, and what ffmpeg skips
    past when decoding the stream. This exposes the same surface the rest of
    this module uses on a mutagen FileType: `.info` for tech, `.tags` (an ID3
    object, so every ID3 frame branch works unchanged) and `.save()`.
    """

    def __init__(self, path, info, tag):
        self.path = path
        self.info = info
        self.tags = tag

    def save(self, v2_version=4, v1=1):
        # Nothing loaded and nothing added: never prepend an empty chunk.
        if not self.tags and not self.tags.size:
            return
        self.tags.save(self.path, v2_version=v2_version, v1=v1)


def _load_aac(path):
    """AacHandle for one .aac file, or None when mutagen cannot read it."""
    from mutagen.aac import AAC
    from mutagen.id3 import ID3, ID3NoHeaderError

    try:
        info = AAC(path).info
    except Exception:
        return None
    try:
        tag = ID3(path)  # an existing leading ID3v2 chunk
    except ID3NoHeaderError:
        tag = ID3()  # none yet: the first save() writes one (Picard does too)
    except Exception:
        return None
    return AacHandle(path, info, tag)


class VideoHandle:
    """Lightweight stand-in for the mutagen object on video files so the
    rest of the codebase can treat `af.audio is not None` as "readable"."""

    def __init__(self, tags, tech):
        self.tags = tags
        self.info = None
        self.tech = tech


class AudioFile:
    """Unified abstraction over FLAC / OGG / Opus / MP3 / AAC / MP4 (and,
    read via ffprobe, every music-video container in paths.LIB_VIDEO_EXTS)."""

    def __init__(self, path):
        self.path = path
        self.ext = os.path.splitext(path)[1].lower()
        self.kind = self._kind()
        self.audio = None
        self.error = None
        self.tech = {}
        self._tag_cache = None
        # Video containers: True once ffprobe supplied tags/tech, and the
        # path actually holding the data after a tag write (tagging a VOB
        # remuxes it into a same-stem MKV, so the file name can change).
        self.is_video = self.kind == "video"
        self.tag_output_path = None
        # True when a tag write replaced the file with another container
        # (.vob/.avi/.webm/... -> .mkv, and an .mp4 reached through
        # set_video_tags -> .mkv). Callers report the swap to the user;
        # tag_output_path names the file that now holds the data.
        self.container_changed = False
        self._video_tags = {}
        # Tag writers save on every call by default. Bulk callers flip this
        # on so a 30-tag edit rewrites the container once, not 30 times.
        self._defer_save = False
        # ID3 write options (MP3 and raw .aac only): mutagen writes v2.4 and
        # no ID3v1 chunk by default, but older players — and most car
        # stereos — read v2.3, and a few very old ones read only ID3v1.
        # Callers that care (the exporter's compatibility options) set these
        # before the write; nothing else changes behaviour.
        self.id3_version = 4
        self.id3v1 = False
        self._dirty = False
        self._load()
        # Write ID3 back in the version the file ARRIVED in. mutagen's save()
        # defaults to v2.4 and its up-conversion drops frames v2.4 has no
        # spelling for, so an untouched v2.3 file (the version most car
        # stereos read) silently "upgraded" on the first tag write and lost
        # whatever the converter could not carry over. A file with no ID3
        # chunk yet keeps the 2.4 default; the exporter sets this explicitly.
        if self.kind in _ID3_KINDS and self.audio is not None:
            version = getattr(getattr(self.audio, "tags", None), "version", None)
            # Only the two versions mutagen can write (a v2.2 file keeps the
            # 2.4 default rather than asking for a version save() rejects).
            if isinstance(version, tuple) and len(version) > 1 and version[1] in (3, 4):
                self.id3_version = int(version[1])

    def _save_container(self):
        """Write the container through its own format writer.

        ID3 containers honour ``id3_version`` / ``id3v1``: the v2.3
        conversion runs here, once, over the frames this file actually holds
        (mutagen's default is v2.4). Every other format saves unchanged.
        """
        if self.kind in _ID3_KINDS and (self.id3_version != 4 or self.id3v1):
            if self.id3_version != 4:
                tags = getattr(self.audio, "tags", None)
                if tags is not None:
                    try:
                        tags.update_to_v23()
                    except Exception:
                        pass  # an unconvertible exotic frame: write it as-is
            self.audio.save(v2_version=self.id3_version,
                            v1=2 if self.id3v1 else 0)
            self._invalidate_cache()
            return True
        self.audio.save()
        return True

    def _invalidate_cache(self):
        """Drop the vorbis tag-read cache after any tag write."""
        self._tag_cache = None

    def defer_save(self, on=True):
        """Defer container writes until flush() (or defer_save(False)).

        Turning deferral OFF writes the pending change and returns flush()'s
        result, so a bulk caller can tell whether the one write it saved up
        actually landed (a full disk or a read-only file used to look like a
        success to every caller that ignores the return)."""
        self._defer_save = bool(on)
        if not on:
            return self.flush()
        return True

    def flush(self):
        """Write pending tag changes to disk (no-op when nothing changed)."""
        if not self._dirty:
            return True
        if self.audio is None:
            return False  # nothing loaded: a pending write could never land
        try:
            self._save_container()
            self._dirty = False
            return True
        except Exception as e:
            self.error = f"flush: {e}"
            return False

    def _save(self):
        """Write now, or mark dirty when deferred. Raises on immediate write
        failure so existing per-tag callers keep reporting it as before."""
        self._dirty = True
        if self._defer_save:
            return True
        self._save_container()
        self._dirty = False
        return True

    def _kind(self):
        if self.ext == ".flac":
            return "flac"
        if self.ext == ".ogg":
            return "ogg"
        if self.ext == ".opus":
            return "opus"
        if self.ext == ".mp3":
            return "mp3"
        if self.ext == ".aac":
            return "aac"
        if self.ext in (".m4a", ".mp4", ".m4v"):
            # MP4-family (M4V is the video-flavored same container) — mutagen
            # reads and writes tags for both audio and music-video files.
            return "mp4"
        if self.ext in VIDEO_FFMPEG_EXTS:
            return "video"
        return None

    def _load(self):
        try:
            if self.kind == "flac":
                self.audio = FLAC(self.path)
            elif self.kind == "ogg":
                self.audio = OggVorbis(self.path)
            elif self.kind == "opus":
                self.audio = OggOpus(self.path)
            elif self.kind == "mp3":
                self.audio = MP3(self.path)
                if self.audio.tags is None:
                    self.audio.add_tags()
            elif self.kind == "mp4":
                self.audio = MP4(self.path)
            elif self.kind == "video":
                self._load_video()
            elif self.kind == "aac":
                self.audio = _load_aac(self.path)
                if self.audio is None:
                    self.error = "mutagen cannot read this .aac stream"
        except Exception as e:
            self.audio = None
            self.error = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # Video containers (MKV / VOB / AVI / ...): ffprobe-backed reads,
    # ffmpeg stream-copy writes. Playback streams are never re-encoded.
    # ------------------------------------------------------------------
    def _load_video(self):
        """Probe a video container for tags + tech via ffprobe.

        Mutagen has no Matroska/VOB/AVI support, so tags come from the
        container's format metadata (upper-cased, common MOV/MKV-style
        aliases folded onto the semantic names) and tech carries the
        video codec plus dimensions alongside the usual audio fields.
        """
        try:
            import json as _json
            ffprobe = _ffprobe_exe()
            if not ffprobe:
                self.error = "ffprobe not available"
                return
            from .subproc import run_tool
            proc = run_tool(
                [ffprobe, "-v", "error", "-print_format", "json",
                 "-show_format", "-show_streams", self.path],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60,
            )
            if proc.returncode != 0 or not proc.stdout:
                self.error = (proc.stderr or "ffprobe failed").strip()[:200]
                return
            data = _json.loads(proc.stdout)
        except Exception as e:
            self.audio = None
            self.error = f"probe failed: {e}"
            return

        fmt = data.get("format") or {}
        raw_tags = {}
        for k, v in (fmt.get("tags") or {}).items():
            if isinstance(v, list):
                v = "; ".join(str(x) for x in v)
            raw_tags[str(k).upper()] = str(v)
        tags = {}
        for k, v in raw_tags.items():
            canonical = _VIDEO_TAG_ALIASES.get(k, k)
            if canonical and canonical not in tags:
                tags[canonical] = v.strip()
        # A bare YEAR only fills in when no full DATE was carried.
        if "DATE" not in tags and raw_tags.get("YEAR"):
            tags["DATE"] = raw_tags["YEAR"]
        self._video_tags = tags

        tech = {}
        try:
            tech["length"] = round(float(fmt.get("duration")), 3)
        except (TypeError, ValueError):
            pass
        try:
            # bits per second — the same unit mutagen reports for audio, so
            # the shared "1022k" formatter needs no special casing.
            tech["bitrate"] = round(float(fmt.get("bit_rate")), 1)
        except (TypeError, ValueError):
            pass
        for st in data.get("streams") or []:
            if st.get("codec_type") == "video":
                disp = st.get("disposition") or {}
                if st.get("codec_name") in ("mjpeg", "png") and disp.get("attached_pic"):
                    continue  # embedded cover art, not the program
                if "codec" not in tech:
                    tech["codec"] = str(st.get("codec_name") or "").upper() or None
                    try:
                        tech["width"] = int(st.get("width"))
                        tech["height"] = int(st.get("height"))
                    except (TypeError, ValueError):
                        pass
            elif st.get("codec_type") == "audio" and "sample_rate" not in tech:
                try:
                    tech["sample_rate"] = int(st.get("sample_rate"))
                    tech["channels"] = int(st.get("channels"))
                except (TypeError, ValueError):
                    pass
        self.tech = {k: v for k, v in tech.items() if v is not None}
        # Truthy stub: get_tag/tech consumers treat `audio is None` as an
        # unreadable file, so video files present a lightweight handle.
        self.audio = VideoHandle(tags, self.tech)

    def _video_canonical(self, name):
        """Semantic tag name for a video lookup: aliases -> canonical."""
        return _VIDEO_TAG_ALIASES.get(str(name).upper(), str(name).upper())

    def set_video_tags(self, mapping):
        """Write several tags into a video container in ONE ffmpeg pass
        (each individual write would be a full stream-copy rewrite).

        LYRICS round-trips like every other tag here: it is stored as the
        container metadata key that get_lyrics() reads back (music videos are
        graded tracks and the lyrics scripts walk them too, so a video lyric
        write has to work rather than be rejected).
        """
        clean = {}
        for k, v in (mapping or {}).items():
            if v is None:
                continue
            v = str(v).strip()
            if not v:
                continue
            clean[str(k).upper()] = v
        if not clean:
            return True
        return self._set_video_tags_batch(_to_mkv_meta(clean))

    def _delete_video_tag(self, name):
        """Drop one tag from a video container in ONE ffmpeg rewrite.

        Video containers have no per-key delete, so the metadata block is
        rewritten from the tags that stay (`-map_metadata -1` plus every
        remaining tag), stream-copied. False when nothing was stored under
        that name, so callers never report a removal that did not happen.
        """
        canonical = self._video_canonical(name)
        current = self._video_tags or {}
        if canonical not in current:
            return False
        remaining = {k: v for k, v in current.items() if k != canonical}
        return self._set_video_tags_batch(_to_mkv_meta(remaining),
                                          drop_existing=True)

    def _set_video_tags_batch(self, mkv_meta, drop_existing=False):
        import tempfile

        ffprobe = _ffprobe_exe()
        try:
            from .tools import detect_all_tools
            ffmpeg = (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")
        except Exception:
            ffmpeg = None
        if not ffmpeg:
            self.error = "ffmpeg not available — install Dependencies first"
            return False

        src = self.path
        in_place = self.ext == ".mkv"
        if in_place:
            fd, tmp = tempfile.mkstemp(prefix=".videotag_", suffix=".mkv",
                                       dir=os.path.dirname(src) or ".")
            os.close(fd)
            dest = tmp
        else:
            stem = os.path.splitext(src)[0]
            dest = stem + ".mkv"
            n = 2
            while os.path.exists(dest) and os.path.normcase(dest) != os.path.normcase(src):
                dest = f"{stem} ({n}).mkv"
                n += 1

        meta_args = []
        for k, v in mkv_meta.items():
            meta_args += ["-metadata", f"{k}={v}"]
        try:
            cmd = [
                ffmpeg, "-y", "-v", "error", "-nostdin",
                "-fflags", "+genpts", "-i", str(src).replace("\\", "/"),
                "-map", "0", "-c", "copy", "-ignore_unknown",
                # 0 copies the source metadata and layers mkv_meta on top;
                # -1 starts clean, so a delete rewrite keeps only mkv_meta.
                "-map_metadata", "-1" if drop_existing else "0", *meta_args,
                "-f", "matroska", str(dest).replace("\\", "/"),
            ]
            from .subproc import run_tool
            proc = run_tool(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60 * 60)
            if proc.returncode != 0:
                self.error = "; ".join((proc.stderr or "").strip().splitlines()[-2:]) or "ffmpeg failed"
                try:
                    if os.path.exists(dest):
                        os.remove(dest)
                except OSError:
                    pass
                return False
        except Exception as e:
            self.error = f"ffmpeg failed: {e}"
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
            return False

        ok = False
        if ffprobe:
            try:
                from .remux import _stream_info
                info = _stream_info(dest, ffprobe)
                ok = bool(info and info[0])
            except Exception:
                ok = False
        else:
            ok = os.path.getsize(dest) > 0
        if not ok:
            self.error = "tag write failed verification"
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
            return False

        if in_place:
            os.replace(dest, src)
            final = src
        else:
            try:
                os.remove(src)
            except OSError:
                pass
            final = dest

        self.path = final
        self.ext = os.path.splitext(final)[1].lower()
        self.tag_output_path = final
        # The output container can differ from the input one (.vob/.avi/...
        # -> .mkv). Recompute BOTH kind and is_video: a stale kind made every
        # later set_tag/delete_tag take the branch of the container the file
        # no longer is.
        self.kind = self._kind() or self.kind
        self.is_video = self.kind == "video"
        self.container_changed = self.ext != os.path.splitext(src)[1].lower()
        self._load_video()
        return True

    def _set_video_tag(self, name, value):
        """Write one tag into a video container (see set_video_tags).

        MKV files are rewritten in place (stream copy); every other
        container is remuxed losslessly into a same-stem MKV — so tagging
        a raw VOB hands back a tagged MKV in one step. Video, audio and
        subtitle streams are all mapped and copied bit-exact; nothing is
        re-encoded and captions are never dropped.
        """
        return self.set_video_tags({name: value})
    @staticmethod
    def _id3_text(frame):
        value = getattr(frame, "text", None)
        if isinstance(value, list):
            return "; ".join(str(item) for item in value)
        return str(value) if value is not None else None

    def _id3_read(self, frame_type, desc=None):
        """Value of one ID3 spelling (None when that frame is absent).

        UFID is the one binary frame a semantic tag maps to: its payload is
        the MusicBrainz id itself, the owner names the authority.
        """
        if frame_type == "USLT":
            return self.get_lyrics()
        if frame_type == "UFID":
            frame = self.audio.tags.get(f"UFID:{desc}")
            data = getattr(frame, "data", None) if frame is not None else None
            if not data:
                return None
            return data.decode("ascii", "replace").strip() or None
        if frame_type == "TXXX":
            for frame in self.audio.tags.getall("TXXX"):
                if frame.desc.upper() == str(desc).upper():
                    return self._id3_text(frame)
            return None
        for frame in self.audio.tags.getall(frame_type):
            if frame_type == "COMM" and getattr(frame, "lang", "eng") != "eng":
                continue
            return self._id3_text(frame)
        return None

    @staticmethod
    def _mp3_canonical(frame_type, desc=""):
        """TAG_MAP name for a stored ID3 frame, or None.

        Descriptions (and UFID owners) are compared case-insensitively:
        taggers disagree on their case and every spelling of a field must
        read back under the same semantic name.
        """
        want = "" if desc in (None, "") else str(desc).upper()
        for name, spec in TAG_MAP.items():
            for ft, od in _mp3_specs(spec):
                if ft != frame_type:
                    continue
                got = "" if od in (None, "") else str(od).upper()
                if got == want:
                    return name
        return None

    def _mp4_read(self, atom):
        """Value of one MP4 atom spelling (None when the atom is absent).

        A freeform atom can hold several values (the repeated fields
        set_tag writes for a list); they read back "; "-joined, exactly like
        the Vorbis and ID3 branches, instead of only the first one."""
        if isinstance(atom, tuple) and atom[0] == "freeform":
            _, mean, name = atom
            vals = []
            for k in self._mp4_freeform_keys(mean, name):
                vals.extend(_decode_mp4_value(v)
                            for v in (self.audio.tags.get(k) or []))
            vals = [v for v in vals if v not in (None, "")]
            return "; ".join(vals) if vals else None
        return self._mp4_text(self.audio.get(atom))

    def _mp4_delete(self, atom):
        """Remove one atom spelling; True when anything was removed."""
        if isinstance(atom, tuple) and atom[0] == "freeform":
            changed = False
            for k in self._mp4_freeform_keys(atom[1], atom[2]):
                del self.audio[k]
                changed = True
            return changed
        if atom in self.audio:
            del self.audio[atom]
            return True
        return False

    # Attributes that identify an ID3 frame carrying no .text, in probe
    # order: the first one present names the frame in all_tags()
    # (PRIV:owner, POPM:email, GEOB:desc, WXXX:desc, CHAP:element_id, ...).
    _ID3_ID_ATTRS = ("owner", "email", "desc", "element_id", "seller")
    # Attributes shown as the frame's value preview (bytes as a size, so a
    # multi-megabyte GEOB payload never turns into a megabyte of text).
    _ID3_PREVIEW_ATTRS = ("owner", "email", "desc", "url", "mime", "filename",
                          "count", "rating", "gain", "peak", "format", "lang",
                          "data", "start_time", "end_time",
                          "child_element_ids")

    @classmethod
    def _id3_frame_key(cls, frame):
        """(name, value preview) for an ID3 frame with no semantic mapping.

        Used by all_tags() so frames such as PRIV / POPM / WXXX / GEOB /
        RVA2 / PCNT / UFID / CHAP are visible to the tag editor and the
        excess-tag check instead of being silently dropped: the name is the
        frame ID plus its identifying field, the preview a short one-liner.
        """
        fid = str(getattr(frame, "FrameID", "") or "")
        sub = next((str(getattr(frame, attr)) for attr in cls._ID3_ID_ATTRS
                    if getattr(frame, attr, None)), "")
        name = f"{fid}:{sub}" if sub else fid
        parts = []
        for attr in cls._ID3_PREVIEW_ATTRS:
            val = getattr(frame, attr, None)
            if val in (None, "", b""):
                continue
            if isinstance(val, bytes):
                parts.append(f"{attr}={len(val)} bytes")
            else:
                parts.append(f"{attr}={str(val)[:60]}".replace("\n", " "))
        # Frames with none of those fields (SYLT, SYTC, ETCO…): the frame's
        # own repr, which stays short for payload-less types.
        return name, ", ".join(parts) or str(frame).replace("\n", " ")[:120]

    @staticmethod
    def _mp4_text(value):
        # Repeated values ("two genres") read as the "; "-joined string the
        # other container branches return: taking value[0] alone hid every
        # repeat from get_tag and all_tags.
        if isinstance(value, list):
            if len(value) > 1:
                return "; ".join(AudioFile._mp4_text(v) for v in value)
            if value:
                value = value[0]
        # A bare tuple is a trkn/disk pair and must render as "n/total"
        # (all_tags passes it unwrapped). A total of 0 is how MP4 spells
        # "no total" — writing "3" stored (3, 0), which read back as "3/0"
        # and so never round-tripped; a pair that HAS a total keeps it.
        if isinstance(value, tuple) and len(value) >= 2:
            if not value[1]:
                return str(value[0])
            return f"{value[0]}/{value[1]}"
        if isinstance(value, bool):
            # Boolean atoms (cpil, pgap, …) come back from mutagen as bools;
            # the app's spelling of a flag is "1"/"0" — str(True) is neither
            # what set_tag() wrote nor what any grader compares against.
            return "1" if value else "0"
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value) if value is not None else None

    @staticmethod
    def _mp4_pair(value):
        text = str(value).strip()
        parts = text.split("/", 1)
        try:
            first = int(parts[0])
            second = int(parts[1]) if len(parts) > 1 else 0
            return (first, second)
        except (TypeError, ValueError):
            return None

    def _mp4_freeform_keys(self, mean, name):
        """Stored freeform keys matching mean/name, ignoring case.

        Freeform atom names are case-sensitive in the container but taggers
        disagree on case (rsgain writes UPPER, simple-dr-meter lower), so
        reads, writes and deletes all match case-insensitively.
        """
        tags = self.audio.tags if self.audio is not None else None
        if not tags:
            return []
        prefix = f"----:{mean}:".lower()
        want = str(name).lower()
        return [k for k in tags.keys()
                if str(k).lower().startswith(prefix)
                and str(k).lower()[len(prefix):] == want]

    def get_tag(self, name):
        if self.audio is None:
            return None

        spec = TAG_MAP.get(name)
        if spec is None:
            return None

        kind = self.kind

        try:
            if kind == "video":
                return self._video_tags.get(self._video_canonical(spec["flac"]))

            if kind in ("flac", "ogg", "opus"):
                if self.audio.tags is None:
                    return None
                if self._tag_cache is None:
                    def _pick(v):
                        if isinstance(v, list) and v:
                            # multiple values (e.g. several GENREs) read like
                            # ID3 lists: "; "-joined, consistent across formats
                            if len(v) > 1:
                                return "; ".join(str(x) for x in v)
                            return v[0]
                        return v
                    self._tag_cache = {
                        str(k).lower(): _pick(v)
                        for k, v in self.audio.tags.items()
                    }
                return self._tag_cache.get(spec["flac"].lower())

            elif kind in _ID3_KINDS:
                if self.audio.tags is None:
                    return None
                for frame_type, desc in _mp3_specs(spec):
                    val = self._id3_read(frame_type, desc)
                    if val is not None:
                        return val
                return None

            elif kind == "mp4":
                for atom in _mp4_specs(spec):
                    val = self._mp4_read(atom)
                    if val:
                        return val
                return None

        except Exception:
            return None

        return None

    def has_tag(self, name):
        v = self.get_tag(name)
        return v is not None and str(v).strip() != ""

    def get_lyrics_transform(self, kind, lang=None, exact=False):
        """Stored translation / transliteration text for this track.

        kind is "TRANSLATION" or "TRANSLITERATION". The specific tags are
        language-suffixed — TRANSLATION-EN, TRANSLITERATION-JA-LATN — so the
        stored language is explicit and gradeable; the bare legacy names
        still read. *lang* ("en") prefers that language when several are
        stored (first subtag match: JA-LATN satisfies "ja"). With
        *exact* only that language's own suffix counts — no fallback to
        another stored language (used to decide whether a per-language
        translation still has to be generated).
        """
        if self.audio is None:
            return None
        want = str(kind).upper()
        found = {}
        try:
            for key, val in (self.all_tags() or {}).items():
                k = str(key).upper()
                # Taggers spell the same tag with a container prefix
                # (TXXX:TRANSLATION-EN, ----:com.apple.iTunes:TRANSLATION-EN);
                # the suffix after the prefix carries the language either way,
                # so strip it before matching.
                k = k.rsplit(":", 1)[-1]
                if k == want:
                    found.setdefault("", val)
                elif k.startswith(want + "-") and len(k) > len(want) + 1:
                    found.setdefault(k[len(want) + 1:], val)
        except Exception:
            return None
        if not found:
            return None
        if lang:
            want_lang = str(lang).strip().lower()
            for suffix, val in found.items():
                if suffix and suffix.lower().split("-")[0] == want_lang:
                    return val
            if exact:
                return None
        for suffix in sorted(found):
            if suffix:
                return found[suffix]
        return found.get("")

# ------------------------------------------------------------------
    # Generic tag enumeration / edit (semantic key mapping)
    # ------------------------------------------------------------------
    def all_tags(self):
        """Return semantic tag names and unknown raw keys for the file.

        Standard fields are normalized to names such as ``TITLE`` and
        ``TRACKNUMBER`` so callers can read or write the correct ID3 frame,
        Vorbis comment, or MP4 atom for each container. Binary artwork is
        omitted; textual custom tags remain available under their raw key.
        """
        if self.audio is None or self.audio.tags is None:
            return {}

        out = {}
        try:
            if self.kind == "video":
                # ffprobe tags already carry canonical semantic names.
                return dict(self._video_tags)

            if self.kind in ("flac", "ogg", "opus"):
                for k, v in self.audio.tags.items():
                    # Repeated values (several GENREs, two ARTISTs) read as
                    # the "; "-joined string get_tag returns. Keeping only
                    # the first one hid every repeat from all_tags consumers
                    # — the tag editor, the excess-tag check, Format All —
                    # and a writer that fed the single value back through
                    # set_tag dropped the rest of the list off the file.
                    val = ("; ".join(str(x) for x in v)
                           if isinstance(v, list) else v)
                    if isinstance(val, bytes):
                        continue
                    raw = str(k)
                    canonical = next(
                        (name for name, spec in TAG_MAP.items()
                         if str(spec.get("flac", "")).lower() == raw.lower()),
                        raw,
                    )
                    out[canonical] = str(val)

            elif self.kind in _ID3_KINDS:
                for frame in self.audio.tags.values():
                    fid = getattr(frame, "FrameID", None)
                    if not fid:
                        continue
                    if fid == "TXXX":
                        desc = str(frame.desc)
                        canonical = self._mp3_canonical("TXXX", desc)
                        out[canonical or f"TXXX:{desc}"] = self._id3_text(frame) or ""
                    elif fid == "USLT":
                        out["LYRICS"] = self.get_lyrics() or ""
                    elif fid == "APIC":
                        continue
                    elif fid == "COMM":
                        out.setdefault("COMMENT", self._id3_text(frame) or "")
                    elif fid == "UFID":
                        # Binary frame carrying an id (the MusicBrainz
                        # recording id lives here); name it by its owner so
                        # the mapped ones read as their semantic tag.
                        owner = str(getattr(frame, "owner", "") or "")
                        canonical = self._mp3_canonical("UFID", owner)
                        data = getattr(frame, "data", b"") or b""
                        out[canonical or f"UFID:{owner}"] = data.decode(
                            "ascii", "replace")
                    elif isinstance(frame, TextFrame):
                        canonical = self._mp3_canonical(fid)
                        out[canonical or fid] = self._id3_text(frame) or ""
                    else:
                        # Every OTHER ID3 frame (PRIV, POPM, WXXX, GEOB,
                        # RVA2, PCNT, UFID, CHAP, SYLT…). They used to be
                        # invisible here, so vendor/ripper junk survived both
                        # the tag editor and the grader's excess-tag check.
                        key, preview = self._id3_frame_key(frame)
                        out.setdefault(key, preview)

            elif self.kind == "mp4":
                for k, v in self.audio.tags.items():
                    if k == "covr":
                        continue
                    vals = v if isinstance(v, list) else [v]
                    # Freeform atoms carry their text inside MP4FreeForm;
                    # binary values (covr, or an atom a tool wrote as bytes)
                    # stay out of the tag list.
                    if vals and isinstance(vals[0], MP4FreeForm):
                        vals = [_decode_mp4_value(x) for x in vals]
                    if any(isinstance(x, bytes) for x in vals):
                        continue
                    # The whole list goes in: _mp4_text joins repeats with
                    # "; " (and renders a trkn/disk pair as "n/total")
                    # instead of dropping every value but the first.
                    raw = str(k)
                    canonical = raw
                    if raw.startswith("----:com.apple.iTunes:"):
                        name = raw.rsplit(":", 1)[-1]
                        canonical = next(
                            (n for n, spec in TAG_MAP.items()
                             if any(isinstance(a, tuple) and a[0] == "freeform"
                                    and a[2].lower() == name.lower()
                                    for a in _mp4_specs(spec))),
                            raw,
                        )
                    else:
                        canonical = next(
                            (n for n, spec in TAG_MAP.items()
                             if any(not isinstance(a, tuple) and a == raw
                                    for a in _mp4_specs(spec))), raw
                        )
                    out[canonical] = self._mp4_text(vals) or ""
        except Exception:
            return {}
        return out

    def tag_values(self, name):
        """EVERY stored value of one tag, in file order ([] when absent).

        get_tag()/all_tags() join repeated values with "; " so a reader sees
        them all in one string; a WRITER needs the pieces back, because the
        containers hold repeats natively (three GENREs, two ARTISTs) and
        set_tag() given the joined string would store ONE value literally
        named "Rock; Pop". *name* is a semantic TAG_MAP name or, for a
        custom tag, the raw key all_tags() emits ("TXXX:FOO",
        "----:com.apple.iTunes:FOO").
        """
        if self.audio is None:
            return []
        name = str(name)
        try:
            if self.kind in ("flac", "ogg", "opus"):
                spec = TAG_MAP.get(name.upper())
                want = str(spec["flac"] if spec else name).lower()
                for k, v in (self.audio.tags or {}).items():
                    if str(k).lower() != want:
                        continue
                    vals = v if isinstance(v, list) else [v]
                    return [str(x) for x in vals if not isinstance(x, bytes)]
                return []

            if self.kind in _ID3_KINDS:
                spec = TAG_MAP.get(name.upper())
                specs = _mp3_specs(spec) if spec else ()
                if not specs:
                    specs = ((("TXXX", name[5:]) if name.upper().startswith("TXXX:")
                              else (name, None)),)
                out = []
                for frame_type, desc in specs:
                    if frame_type in ("USLT", "UFID"):
                        value = self._id3_read(frame_type, desc)
                        return [value] if value else []
                    for frame in self.audio.tags.getall(frame_type):
                        if frame_type == "TXXX" and (
                                str(frame.desc).upper() != str(desc or "").upper()):
                            continue
                        # Another language's comment is not this tag's value.
                        if frame_type == "COMM" and getattr(frame, "lang", "eng") != "eng":
                            continue
                        text = getattr(frame, "text", None)
                        out.extend(str(x) for x in (text if isinstance(text, list)
                                                    else [text] if text is not None else []))
                    if out:
                        return out
                return []

            if self.kind == "mp4":
                spec = TAG_MAP.get(name.upper())
                atoms = _mp4_specs(spec) if spec else (name,)
                out = []
                for atom in atoms:
                    if isinstance(atom, tuple) and atom[0] == "freeform":
                        for k in self._mp4_freeform_keys(atom[1], atom[2]):
                            for v in self.audio.tags.get(k) or []:
                                out.append(_decode_mp4_value(v))
                    else:
                        for v in self.audio.get(atom) or []:
                            out.append(self._mp4_text(v))
                    if out:
                        return [str(x) for x in out if x is not None]
                return []
        except Exception:
            return []
        return []

    def set_any_tag(self, key, value):
        """Write an arbitrary tag key (raw container key).

        Used by the GUI tag editor: flac/ogg/opus take raw vorbis
        comment names, mp3 takes "TXXX:desc" custom frames or plain
        frame IDs, mp4 takes "----:mean:name" freeform atoms or plain
        atom names. A key the container cannot hold (a semantic name on
        mp3/mp4) is refused with self.error instead of being written as a
        truncated frame no reader finds — set_tag maps semantic names.
        """
        self._invalidate_cache()
        if self.audio is None:
            return False
        # Trim leading/trailing spaces for all arbitrary tags as well (user request)
        # Keep LYRICS content as-is (multi-line), but trim other fields
        if str(key).upper() not in ("LYRICS", "UNSYNCEDLYRICS", "TXXX:LYRICS"):
            value = str(value).strip()
        else:
            value = str(value)

        try:
            if self.kind in ("flac", "ogg", "opus"):
                if self.audio.tags is None:
                    if hasattr(self.audio, "add_tags"):
                        self.audio.add_tags()
                    else:
                        return False
                self.audio.tags[str(key)] = [value]
                self._save()
                return True

            elif self.kind in _ID3_KINDS:
                if self.audio.tags is None:
                    self.audio.add_tags()
                if str(key).startswith("TXXX:"):
                    desc = str(key)[5:]
                    for frame in list(self.audio.tags.getall("TXXX")):
                        if frame.desc.upper() == desc.upper():
                            try:
                                del self.audio.tags[frame.HashKey]
                            except Exception:
                                pass
                    self.audio.tags.add(
                        TXXX(encoding=Encoding.UTF8, desc=desc, text=[value])
                    )
                elif not _is_id3_frame_id(str(key)):
                    # An ID3 frame ID is exactly four A-Z/0-9 bytes. A longer
                    # key produced a malformed frame that no reader (this app
                    # included) can find, and a key that merely has four
                    # CHARACTERS — an MP4 atom name like "©too" — is worse:
                    # mutagen writes it as a frame header anyway and the whole
                    # tag block stops parsing, which silently wiped every tag
                    # on the file. Refuse both and name the working spelling.
                    self.error = (f"set_any_tag: {key!r} is not an ID3 frame "
                                  f"ID (A-Z, 0-9, four of them) — custom tags "
                                  f"use TXXX:{key}")
                    return False
                else:
                    self.audio.tags.delall(str(key))
                    frame_cls = Frames.get(str(key))
                    if frame_cls is None:
                        frame_cls = type(
                            str(key), (TextFrame,), {"FrameID": str(key)})
                    self.audio.tags.add(
                        frame_cls(encoding=Encoding.UTF8, text=[value])
                    )
                self._save()
                return True

            elif self.kind == "mp4":
                if str(key).startswith("----:"):
                    _, mean, name = str(key).split(":", 2)
                    fmt = getattr(MP4FreeForm, "FORMAT_UTF8", 1)
                    try:
                        self.audio[str(key)] = [
                            MP4FreeForm(value.encode("utf-8"), dataformat=fmt)
                        ]
                    except TypeError:
                        self.audio[str(key)] = [
                            MP4FreeForm(value.encode("utf-8"))
                        ]
                else:
                    if len(str(key)) != 4:
                        # An MP4 atom name is four bytes. mutagen silently
                        # truncates a longer key to its first four characters
                        # and stores the value under THAT name, where no
                        # reader looks for it (a semantic name like "TITLE"
                        # became atom "TITL"): refuse it instead, and point
                        # at the freeform spelling.
                        self.error = (f"set_any_tag: {key!r} is not a "
                                      f"4-character MP4 atom — custom tags use "
                                      f"----:com.apple.iTunes:{key}")
                        return False
                    self.audio[str(key)] = [value]
                self._save()
                return True

        except Exception as e:
            self.error = f"set_any_tag: {e}"
            return False
        return False

    def delete_any_tag(self, key):
        """Remove an arbitrary tag key (raw container key). MP4 atoms
        are matched case-insensitively over the full key list; video
        containers rewrite the metadata block in one ffmpeg pass."""
        self._invalidate_cache()
        if self.audio is None:
            return False

        try:
            if self.kind == "video":
                return self._delete_video_tag(key)

            if self.kind in ("flac", "ogg", "opus"):
                if self.audio.tags is None:
                    return True
                target = str(key).lower()
                changed = False
                for k in list(self.audio.tags.keys()):
                    if str(k).lower() == target:
                        del self.audio.tags[k]
                        changed = True
                if changed:
                    self._save()
                return True

            elif self.kind in _ID3_KINDS:
                if self.audio.tags is None:
                    return True
                if str(key).startswith("TXXX:"):
                    desc = str(key)[5:]
                    for frame in list(self.audio.tags.getall("TXXX")):
                        if frame.desc.upper() == desc.upper():
                            try:
                                del self.audio.tags[frame.HashKey]
                            except Exception:
                                pass
                elif ":" in str(key):
                    # Enumerated non-text frames (all_tags emits PRIV:owner,
                    # POPM:email, GEOB:desc, CHAP:element_id, …): drop the
                    # frames whose enumerated name matches, so the tag editor
                    # can delete what it lists.
                    target = str(key).lower()
                    for frame in list(self.audio.tags.values()):
                        if self._id3_frame_key(frame)[0].lower() == target:
                            try:
                                del self.audio.tags[frame.HashKey]
                            except Exception:
                                pass
                else:
                    self.audio.tags.delall(str(key))
                self._save()
                return True

            elif self.kind == "mp4":
                target = str(key).lower()
                changed = False
                for k in list(self.audio.keys()):
                    if str(k).lower() == target:
                        del self.audio[k]
                        changed = True
                if changed:
                    self._save()
                return True

        except Exception as e:
            self.error = f"delete_any_tag: {e}"
            return False
        return False

    # ------------------------------------------------------------------
    # Tag write / delete, used for SOURCE normalization
    # ------------------------------------------------------------------
    def set_tag(self, name, value):
        self._invalidate_cache()
        if self.audio is None:
            return False
        if self.kind == "video":
            return self._set_video_tag(name, value)

        name = str(name).upper()
        if name == "LYRICS":
            return self.set_lyrics(value)
        # A LIST means several answers for one field (two genres, say). They go
        # to disk as REPEATED fields — Vorbis comments, ID3v2.4 text frames and
        # MP4 atoms all carry repeats natively — because writing one
        # "Dance-Punk; Electronic; Funk Rock" string is exactly what makes every
        # player list a single genre of that name. get_tag() already joins
        # repeats back with "; ", so nothing downstream changes.
        values = None
        if isinstance(value, (list, tuple, set)):
            values = [str(v).strip() for v in value if str(v).strip()]
            if not values:
                return False
            value = "; ".join(values)
        # User request: all written tags must have no leading/trailing spaces.
        # Trim every value (except LYRICS which is handled separately) and
        # enforce ITUNESADVISORY 0/1/2.
        value = str(value).strip()
        if name == "ITUNESADVISORY" and value not in ("0", "1", "2"):
            # Still write the trimmed value, but grading will flag invalid
            # values (non-0/1/2) as failure; we don't silently coerce.
            pass
        spec = TAG_MAP.get(name)
        if spec is None and name.startswith(_LYRICS_TRANSFORM_PREFIXES):
            # TRANSLATION-EN / TRANSLITERATION-JA-LATN: the language lives in
            # the tag NAME, so there is no fixed TAG_MAP entry for them (the
            # set is open-ended). Without this they fell through to the
            # arbitrary-key writer, which refuses a name that is neither an
            # ID3 frame ID nor a 4-character MP4 atom — the transform was
            # silently lost on MP3/AAC/MP4 while the caller's return value
            # went unchecked. They use the same freeform spellings as the
            # bare TRANSLATION/TRANSLITERATION entries (TXXX: / iTunes atom),
            # which is where the readers look for them.
            spec = {"flac": name, "mp3": ("TXXX", name),
                    "mp4": ("freeform", "com.apple.iTunes", name)}
        if spec is None:
            # raw / unknown key: fall back to the arbitrary-key writer so
            # custom tags (TXXX:..., freeform atoms, vorbis comments) work
            return self.set_any_tag(name, value)

        kind = self.kind

        try:
            if kind in ("flac", "ogg", "opus"):
                if self.audio.tags is None:
                    if hasattr(self.audio, "add_tags"):
                        self.audio.add_tags()
                    else:
                        return False
                self.audio.tags[spec["flac"]] = values if values and len(values) > 1 else value
                self._save()
                return True

            elif kind in _ID3_KINDS:
                specs = _mp3_specs(spec)
                frame_type, desc = specs[0]
                if self.audio.tags is None:
                    self.audio.add_tags()

                # Drop the other spellings of the same field: one id written
                # twice leaves two answers on disk for every reader.
                for alt_type, alt_desc in specs[1:]:
                    try:
                        if alt_type == "TXXX":
                            for frame in list(self.audio.tags.getall("TXXX")):
                                if frame.desc.upper() == str(alt_desc).upper():
                                    del self.audio.tags[frame.HashKey]
                        elif alt_type == "UFID":
                            del self.audio.tags[f"UFID:{alt_desc}"]
                        else:
                            self.audio.tags.delall(alt_type)
                    except Exception:
                        pass

                if frame_type == "USLT":
                    return self.set_lyrics(value)
                if frame_type == "UFID":
                    self.audio.tags.add(
                        UFID(owner=str(desc),
                             data=value.encode("ascii", "replace"))
                    )
                elif frame_type == "TXXX":
                    for frame in list(self.audio.tags.getall("TXXX")):
                        if frame.desc.upper() == desc.upper():
                            try:
                                del self.audio.tags[frame.HashKey]
                            except Exception:
                                pass
                    self.audio.tags.add(
                        TXXX(encoding=Encoding.UTF8, desc=desc, text=values or [value])
                    )
                else:
                    if frame_type == "COMM":
                        # Only replace the English/undescribed comment so
                        # other-language translations are preserved.
                        for frame in list(self.audio.tags.getall("COMM")):
                            if frame.lang == "eng" and not frame.desc:
                                try:
                                    del self.audio.tags[frame.HashKey]
                                except Exception:
                                    pass
                        self.audio.tags.add(
                            COMM(encoding=Encoding.UTF8, lang="eng",
                                 desc="", text=value)
                        )
                    else:
                        self.audio.tags.delall(frame_type)
                        frame_cls = Frames.get(frame_type)
                        if frame_cls is None:
                            return False
                        self.audio.tags.add(
                            frame_cls(encoding=Encoding.UTF8, text=values or [value])
                        )
                self._save()
                return True

            elif kind == "mp4":
                atom = _mp4_specs(spec)[0]
                # One field, one atom: drop this entry's other spellings
                # (older freeform names) before writing the current one.
                for alt in _mp4_specs(spec)[1:]:
                    try:
                        self._mp4_delete(alt)
                    except Exception:
                        pass

                if isinstance(atom, tuple) and atom[0] == "freeform":
                    _, mean, name2 = atom
                    key = f"----:{mean}:{name2}"

                    # Replace, never append next to a differently-cased
                    # atom of the same name (taggers disagree on case).
                    self._mp4_delete(atom)

                    fmt = getattr(MP4FreeForm, "FORMAT_UTF8", 1)

                    # One atom PER value, like the Vorbis/ID3 branches: the
                    # list is repeated fields, not one atom holding "A; B".
                    try:
                        self.audio[key] = [
                            MP4FreeForm(v.encode("utf-8"), dataformat=fmt)
                            for v in (values or [value])
                        ]
                    except TypeError:
                        self.audio[key] = [
                            MP4FreeForm(v.encode("utf-8"))
                            for v in (values or [value])
                        ]
                elif atom == "cpil":
                    # The compilation flag is a BOOLEAN atom: mutagen renders
                    # it as an int and refuses a str on save.
                    self.audio[atom] = [
                        1 if str(value).strip().lower() in ("1", "true", "yes") else 0
                    ]
                elif atom in ("trkn", "disk"):
                    pair = self._mp4_pair(value)
                    if pair is None:
                        return False
                    if "/" not in str(value):
                        # A bare "4" carries no total: keep the stored one
                        # instead of rewriting 3/12 into 4/0.
                        cur = self.audio.get(atom) or []
                        old = cur[0] if isinstance(cur, list) and cur else cur
                        if isinstance(old, (tuple, list)) and len(old) > 1:
                            try:
                                pair = (pair[0], int(old[1]))
                            except (TypeError, ValueError):
                                pass
                    self.audio[atom] = [pair]
                elif atom == "tmpo":
                    try:
                        self.audio[atom] = [int(value)]
                    except (TypeError, ValueError):
                        return False
                else:
                    self.audio[atom] = values if values and len(values) > 1 else [value]

                self._save()
                return True

        except Exception as e:
            self.error = f"set_tag: {e}"
            return False

        return False

    def delete_tag(self, name):
        self._invalidate_cache()
        if self.audio is None:
            return False

        name = str(name).upper()
        if self.kind == "video":
            return self._delete_video_tag(name)
        if name == "LYRICS":
            return self.delete_lyrics()
        spec = TAG_MAP.get(name)
        if spec is None:
            return self.delete_any_tag(name)

        kind = self.kind

        try:
            if kind in ("flac", "ogg", "opus"):
                if self.audio.tags is None:
                    return True

                target = spec["flac"].lower()
                changed = False

                for k in list(self.audio.tags.keys()):
                    if str(k).lower() == target:
                        del self.audio.tags[k]
                        changed = True

                if changed:
                    self._save()

                return True

            elif kind in _ID3_KINDS:
                specs = _mp3_specs(spec)
                if self.audio.tags is None:
                    return True

                changed = False
                for frame_type, desc in specs:
                    if frame_type == "USLT":
                        return self.delete_lyrics()
                    if frame_type == "UFID":
                        try:
                            del self.audio.tags[f"UFID:{desc}"]
                            changed = True
                        except Exception:
                            pass
                    elif frame_type == "TXXX":
                        for frame in list(self.audio.tags.getall("TXXX")):
                            if frame.desc.upper() == str(desc).upper():
                                try:
                                    del self.audio.tags[frame.HashKey]
                                    changed = True
                                except Exception:
                                    pass
                    elif frame_type == "COMM":
                        # Remove only the English/undescribed comment.
                        for frame in list(self.audio.tags.getall("COMM")):
                            if frame.lang == "eng" and not frame.desc:
                                try:
                                    del self.audio.tags[frame.HashKey]
                                    changed = True
                                except Exception:
                                    pass
                    else:
                        before = len(self.audio.tags.getall(frame_type))
                        self.audio.tags.delall(frame_type)
                        changed = before > 0

                if changed:
                    self._save()

                return True

            elif kind == "mp4":
                changed = False

                # Every spelling the entry accepts: a field may sit under an
                # older freeform name as well as the current one.
                for atom in _mp4_specs(spec):
                    # Case-insensitive: the atom may be stored in another
                    # tagger's case (see _mp4_freeform_keys).
                    changed = self._mp4_delete(atom) or changed

                if changed:
                    self._save()

                # Absent tag is still True: delete is an idempotent no-op in
                # every branch — /api/mb/assign sends null for each optional
                # key and turns a False into a 500 for the whole request.
                return True

        except Exception as e:
            self.error = f"delete_tag: {e}"
            return False

        return False

    # ------------------------------------------------------------------
    # Lyrics
    # ------------------------------------------------------------------
    def get_lyrics(self):
        try:
            if self.kind == "video":
                # Stored in the container metadata block by set_lyrics /
                # set_video_tags, read back by ffprobe (_video_tags). Without
                # this branch a video lyric write was write-only: the tag
                # existed in the file but every reader saw None.
                for key in ("LYRICS", "UNSYNCEDLYRICS"):
                    val = self._video_tags.get(key)
                    if val and str(val).strip():
                        return str(val)
                return None

            if self.kind in ("flac", "ogg", "opus"):
                if self.audio.tags is None:
                    return None
                # Prefer LYRICS (synced) over UNSYNCEDLYRICS — files often carry
                # both (Picard writes unsynced + synced). Return the synced one
                # if it exists and contains a timestamp, otherwise fall back.
                cache = {str(k).lower(): (v[0] if isinstance(v, list) and v else v) for k, v in self.audio.tags.items()}
                # Try LYRICS first via TAG_MAP cache for consistency
                lyr = cache.get("lyrics")
                if lyr is not None and str(lyr).strip():
                    # If LYRICS looks synced (has [mm:ss]), prefer it
                    if "[" in str(lyr):
                        return str(lyr)
                    # Otherwise still return it, but check unsynced as fallback
                    # If unsynced exists and is different, prefer the synced one if available
                    # Already have lyr, so return it
                    return str(lyr)
                uns = cache.get("unsyncedlyrics")
                if uns is not None:
                    return str(uns)
                return None

            elif self.kind in _ID3_KINDS:
                for f in self.audio.tags.getall("USLT"):
                    if isinstance(f.text, list):
                        return "\n".join(f.text) if f.text else None
                    return str(f.text) if f.text else None
                return None

            elif self.kind == "mp4":
                v = self.audio.get("\xa9lyr")
                if isinstance(v, list) and v:
                    v = v[0]
                if isinstance(v, bytes):
                    return v.decode("utf-8", "replace")
                return str(v) if v is not None else None

        except Exception:
            return None

        return None

    def set_lyrics(self, text):
        self._invalidate_cache()
        try:
            if self.kind == "video":
                # One metadata-block rewrite; empty text clears the stored
                # lyrics (see delete_lyrics) instead of leaving the old text.
                if not str(text).strip():
                    return self.delete_lyrics()
                return bool(self.set_video_tags({"LYRICS": text}))

            if self.kind in ("flac", "ogg", "opus"):
                if self.audio.tags is None:
                    if hasattr(self.audio, "add_tags"):
                        self.audio.add_tags()
                    else:
                        return False
                for k in list(self.audio.tags.keys()):
                    if str(k).lower() in ("lyrics", "unsyncedlyrics"):
                        del self.audio.tags[k]

                self.audio.tags["LYRICS"] = [text]
                self._save()
                return True

            elif self.kind in _ID3_KINDS:
                # USLT carries ONE frame per language, so only the
                # undescribed frame this app writes is replaced: a described
                # one (a tagger's named translation) is left alone, exactly
                # like the COMM rule in set_tag. The replacement keeps the
                # language that frame declared, so rewriting a Japanese
                # lyric does not relabel it "eng".
                lang = "eng"
                for frame in list(self.audio.tags.getall("USLT")):
                    if frame.desc:
                        continue
                    lang = str(getattr(frame, "lang", "") or "eng")
                    try:
                        del self.audio.tags[frame.HashKey]
                    except Exception:
                        pass
                self.audio.tags.add(
                    USLT(encoding=Encoding.UTF8, lang=lang, desc="", text=text)
                )
                self._save()
                return True

            elif self.kind == "mp4":
                self.audio["\xa9lyr"] = [text]
                self._save()
                return True

        except Exception as e:
            self.error = f"set_lyrics: {e}"
            return False

        return False

    def delete_lyrics(self):
        self._invalidate_cache()
        try:
            if self.kind == "video":
                # Both lyric keys go in ONE ffmpeg rewrite (a video container
                # has no per-key delete): the metadata block is rebuilt from
                # what stays. Nothing stored -> nothing to rewrite.
                current = self._video_tags or {}
                remaining = {k: v for k, v in current.items()
                             if k not in ("LYRICS", "UNSYNCEDLYRICS")}
                if len(remaining) == len(current):
                    return True
                return bool(self._set_video_tags_batch(
                    _to_mkv_meta(remaining), drop_existing=True))

            if self.kind in ("flac", "ogg", "opus"):
                if self.audio.tags is None:
                    return True  # nothing to delete
                for k in list(self.audio.tags.keys()):
                    if str(k).lower() in ("lyrics", "unsyncedlyrics"):
                        del self.audio.tags[k]
                self._save()
                return True

            elif self.kind in _ID3_KINDS:
                # Only the undescribed frames are this app's own lyrics: a
                # USLT another tagger wrote under a description (a named
                # translation) belongs to them and stays, like set_tag does
                # for COMM.
                for frame in list(self.audio.tags.getall("USLT")):
                    if frame.desc:
                        continue
                    try:
                        del self.audio.tags[frame.HashKey]
                    except Exception:
                        pass
                self._save()
                return True

            elif self.kind == "mp4":
                if "\xa9lyr" in self.audio:
                    del self.audio["\xa9lyr"]
                    self._save()
                return True

        except Exception as e:
            self.error = f"delete_lyrics: {e}"
            return False

        return False

    # ------------------------------------------------------------------
    # Embedded cover art — FLAC pictures, ID3 APIC (MP3 and the leading
    # ID3v2 chunk of raw .aac), MP4 covr and the OGG/Opus
    # METADATA_BLOCK_PICTURE base64 form. Video containers store cover art
    # as attachments, which this abstraction does not touch: empty results.
    # ------------------------------------------------------------------
    def embedded_pictures(self):
        """[(mime, bytes)] of the embedded cover art currently in the file."""
        try:
            if self.kind == "flac" and self.audio is not None:
                return [(p.mime, bytes(p.data)) for p in self.audio.pictures]

            if self.kind in _ID3_KINDS and self.audio is not None and self.audio.tags:
                return [
                    ((f.mime or "image/jpeg"), bytes(f.data))
                    for f in self.audio.tags.getall("APIC")
                ]

            if self.kind == "mp4" and self.audio is not None and self.audio.tags:
                covers = self.audio.tags.get("covr") or []
                out = []
                for c in covers:
                    data = bytes(c)
                    fmt = getattr(c, "imageformat", None)
                    mime = "image/png" if fmt == 14 else "image/jpeg"
                    out.append((mime, data))
                return out

            if self.kind in ("ogg", "opus") and self.audio is not None and self.audio.tags:
                for k, v in self.audio.tags.items():
                    if str(k).lower() == "metadata_block_picture":
                        vals = v if isinstance(v, list) else [v]
                        import base64
                        out = []
                        for raw in vals:
                            try:
                                pic = Picture(base64.b64decode(str(raw)))
                                out.append((pic.mime, bytes(pic.data)))
                            except Exception:
                                pass
                        return out
            return []
        except Exception as e:
            self.error = f"embedded_pictures: {e}"
            return []

    def remove_embedded_pictures(self):
        """Strip every embedded picture. True when the file was changed."""
        self._invalidate_cache()
        try:
            if self.kind == "flac" and self.audio is not None:
                if not self.audio.pictures:
                    return False
                self.audio.clear_pictures()
                self._save_container()
                return True

            if self.kind in _ID3_KINDS and self.audio is not None and self.audio.tags:
                if not self.audio.tags.getall("APIC"):
                    return False
                self.audio.tags.delall("APIC")
                self._save_container()
                return True

            if self.kind == "mp4" and self.audio is not None and self.audio.tags:
                if "covr" not in self.audio.tags:
                    return False
                del self.audio.tags["covr"]
                self._save_container()
                return True

            if self.kind in ("ogg", "opus") and self.audio is not None and self.audio.tags:
                keys = [k for k in self.audio.tags.keys()
                        if str(k).lower() == "metadata_block_picture"]
                if not keys:
                    return False
                for k in keys:
                    del self.audio.tags[k]
                self._save_container()
                return True
            return False
        except Exception as e:
            self.error = f"remove_embedded_pictures: {e}"
            return False

    def add_embedded_picture(self, data, mime="image/jpeg"):
        """Embed one front-cover picture. True when the file was changed."""
        self._invalidate_cache()
        try:
            if self.kind == "flac" and self.audio is not None:
                pic = Picture()
                pic.type = 3  # front cover
                pic.mime = mime
                pic.data = data
                self.audio.add_picture(pic)
                self._save_container()
                return True

            if self.kind in _ID3_KINDS and self.audio is not None:
                if self.audio.tags is None:
                    self.audio.add_tags()
                self.audio.tags.delall("APIC")
                self.audio.tags.add(APIC(encoding=3, mime=mime, type=3, data=data))
                self._save_container()
                return True

            if self.kind == "mp4" and self.audio is not None:
                fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
                self.audio.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
                self._save_container()
                return True

            if self.kind in ("ogg", "opus") and self.audio is not None:
                import base64
                pic = Picture()
                pic.type = 3
                pic.mime = mime
                pic.data = data
                self.audio.tags["METADATA_BLOCK_PICTURE"] = base64.b64encode(pic.write()).decode("ascii")
                self._save_container()
                return True
            return False
        except Exception as e:
            self.error = f"add_embedded_picture: {e}"
            return False

