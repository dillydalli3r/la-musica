"""Unified tag abstraction over FLAC / OGG / Opus / MP3 / MP4 audio files."""
import os

from .deps import (
    FLAC, OggVorbis, OggOpus, MP3, MP4, MP4FreeForm,
    TXXX, USLT, COMM, Encoding, TextFrame, Frames, APIC, MP4Cover, Picture,
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
        "flac": "ORIGINALDATE", "mp3": ("TDRL", None),
        "mp4": ("freeform", "com.apple.iTunes", "originaldate"),
    },
    "ORIGINALYEAR": {
        "flac": "ORIGINALYEAR", "mp3": ("TXXX", "ORIGINALYEAR"),
        "mp4": ("freeform", "com.apple.iTunes", "ORIGINALYEAR"),
    },
    "RELEASETYPE": {
        "flac": "RELEASETYPE", "mp3": ("TXXX", "RELEASETYPE"),
        "mp4": ("freeform", "com.apple.iTunes", "RELEASETYPE"),
    },
    "RELEASECOUNTRY": {
        "flac": "RELEASECOUNTRY", "mp3": ("TXXX", "RELEASECOUNTRY"),
        "mp4": ("freeform", "com.apple.iTunes", "RELEASECOUNTRY"),
    },
    "CATALOGNUMBER": {
        "flac": "CATALOGNUMBER", "mp3": ("TXXX", "CATALOGNUMBER"),
        "mp4": ("freeform", "com.apple.iTunes", "CATALOGNUMBER"),
    },
    # Record label (Picard writes "LABEL"; beets writes the same vorbis /
    # TXXX / MP4 freeform keys, so both tagging paths stay interchangeable).
    "LABEL": {
        "flac": "LABEL", "mp3": ("TXXX", "LABEL"),
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
        "flac": "ISRC", "mp3": "TSRC",
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
    "MEDIA": {
        "flac": "MEDIA",
        "mp3": ("TXXX", "MEDIA"),
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
    # Rip-log score (0-100) from AudioAuditor's cambia grading, written
    # to the tracks of MEDIA=CD releases only.
    "LOG_GRADE": {
        "flac": "LOG_GRADE",
        "mp3": ("TXXX", "LOG_GRADE"),
        "mp4": ("freeform", "com.apple.iTunes", "LOG_GRADE"),
    },
    # Integrity verification tags (see mlo/integrity.py):
    #   AUDIO_MD5  MD5 hex of the audio data (PCM for FLAC via decode,
    #              tag-stable audio region for MP3/MP4/OGG).
    #   INTEGRITY  OK / FAIL verdict of the audio integrity test.
    #   LOG_CRC    CD rips: OK / MISMATCH vs the rip log's per-track CRC.
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
    "MUSICBRAINZ_TRACKID": {
        "flac": "MUSICBRAINZ_TRACKID",
        "mp3": ("TXXX", "MusicBrainz Track Id"),
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
}

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


class VideoHandle:
    """Lightweight stand-in for the mutagen object on video files so the
    rest of the codebase can treat `af.audio is not None` as "readable"."""

    def __init__(self, tags, tech):
        self.tags = tags
        self.info = None
        self.tech = tech


class AudioFile:
    """Unified abstraction over FLAC / OGG / Opus / MP3 / MP4 (and, read via
    ffprobe, every music-video container in paths.LIB_VIDEO_EXTS)."""

    def __init__(self, path):
        self.path = path
        self.ext = os.path.splitext(path)[1].lower()
        self.kind = self._kind()
        self.audio = None
        self.error = None
        self._tag_cache = None
        # Video containers: True once ffprobe supplied tags/tech, and the
        # path actually holding the data after a tag write (tagging a VOB
        # remuxes it into a same-stem MKV, so the file name can change).
        self.is_video = self.kind == "video"
        self.tag_output_path = None
        self._video_tags = {}
        self._load()

    def _invalidate_cache(self):
        """Drop the vorbis tag-read cache after any tag write."""
        self._tag_cache = None

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
                try:
                    from mutagen.aac import AAC
                    self.audio = AAC(self.path)
                except ImportError:
                    self.audio = None
                    self.error = "mutagen.aac is not available"
                except Exception as e:
                    self.audio = None
                    self.error = f"{type(e).__name__}: {e}"
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
        (each individual write would be a full stream-copy rewrite)."""
        clean = {}
        for k, v in (mapping or {}).items():
            if v is None:
                continue
            v = str(v).strip()
            if not v:
                continue
            key = str(k).upper()
            if key in ("LYRICS", "UNSYNCEDLYRICS"):
                continue
            clean[key] = v
        if not clean:
            return True
        # MKV muxers expose the track number as PART_NUMBER and the disc
        # as DISC; every other semantic name is stored verbatim.
        mkv_keys = {"TRACKNUMBER": "PART_NUMBER", "DISCNUMBER": "DISC"}
        return self._set_video_tags_batch({mkv_keys.get(k, k): v for k, v in clean.items()})

    def _set_video_tags_batch(self, mkv_meta):
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
                "-map_metadata", "0", *meta_args,
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
        self._load_video()
        self.is_video = True
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

    @staticmethod
    def _mp4_text(value):
        if isinstance(value, (tuple, list)) and value:
            value = value[0]
        if isinstance(value, tuple) and len(value) >= 2:
            return f"{value[0]}/{value[1]}"
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

            elif kind == "mp3":
                frame_type, desc = spec["mp3"]
                if self.audio.tags is None:
                    return None
                if frame_type == "TXXX":
                    for frame in self.audio.tags.getall("TXXX"):
                        if frame.desc.upper() == desc.upper():
                            return self._id3_text(frame)
                    return None
                if frame_type == "USLT":
                    return self.get_lyrics()
                for frame in self.audio.tags.getall(frame_type):
                    if frame_type == "COMM" and getattr(frame, "lang", "eng") != "eng":
                        continue
                    return self._id3_text(frame)
                return None

            elif kind == "mp4":
                atom = spec["mp4"]

                if isinstance(atom, tuple) and atom[0] == "freeform":
                    _, mean, name2 = atom
                    key = f"----:{mean}:{name2}"
                    vals = self.audio.tags.get(key) if self.audio.tags else None
                    if not vals:
                        # Freeform atom names are case-sensitive in the
                        # container but taggers disagree on case (rsgain
                        # writes UPPER, simple-dr-meter lower) — match
                        # case-insensitively on the subname.
                        prefix = f"----:{mean}:".lower()
                        want = str(name2).lower()
                        for k in (self.audio.tags.keys() if self.audio.tags else []):
                            kl = str(k).lower()
                            if kl.startswith(prefix) and kl[len(prefix):] == want:
                                vals = self.audio.tags.get(k)
                                break
                    if not vals:
                        return None
                    return _decode_mp4_value(vals[0])

                return self._mp4_text(self.audio.get(atom))

        except Exception:
            return None

        return None

    def has_tag(self, name):
        v = self.get_tag(name)
        return v is not None and str(v).strip() != ""

    def get_lyrics_transform(self, kind, lang=None):
        """Stored translation / transliteration text for this track.

        kind is "TRANSLATION" or "TRANSLITERATION". The specific tags are
        language-suffixed — TRANSLATION-EN, TRANSLITERATION-JA-LATN — so the
        stored language is explicit and gradeable; the bare legacy names
        still read. *lang* ("en") prefers that language when several are
        stored (first subtag match: JA-LATN satisfies "ja").
        """
        if self.audio is None:
            return None
        want = str(kind).upper()
        found = {}
        try:
            for key, val in (self.all_tags() or {}).items():
                k = str(key).upper()
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
                    val = v[0] if isinstance(v, list) and v else v
                    if isinstance(val, bytes):
                        continue
                    raw = str(k)
                    canonical = next(
                        (name for name, spec in TAG_MAP.items()
                         if str(spec.get("flac", "")).lower() == raw.lower()),
                        raw,
                    )
                    out[canonical] = str(val)

            elif self.kind == "mp3":
                for frame in self.audio.tags.values():
                    fid = getattr(frame, "FrameID", None)
                    if not fid:
                        continue
                    if fid == "TXXX":
                        desc = str(frame.desc)
                        canonical = next(
                            (name for name, spec in TAG_MAP.items()
                             if spec.get("mp3") == ("TXXX", desc)
                             or (spec.get("mp3", (None, None))[0] == "TXXX"
                                 and str(spec["mp3"][1]).upper() == desc.upper())),
                            f"TXXX:{desc}",
                        )
                        out[canonical] = self._id3_text(frame) or ""
                    elif fid == "USLT":
                        out["LYRICS"] = self.get_lyrics() or ""
                    elif fid == "APIC":
                        continue
                    elif fid == "COMM":
                        out.setdefault("COMMENT", self._id3_text(frame) or "")
                    elif isinstance(frame, TextFrame):
                        canonical = next(
                            (name for name, spec in TAG_MAP.items()
                             if spec.get("mp3", (None, None))[0] == fid),
                            fid,
                        )
                        out[canonical] = self._id3_text(frame) or ""

            elif self.kind == "mp4":
                for k, v in self.audio.tags.items():
                    if k == "covr":
                        continue
                    val = v[0] if isinstance(v, list) and v else v
                    if isinstance(val, MP4FreeForm):
                        val = _decode_mp4_value(val)
                    if isinstance(val, bytes):
                        continue
                    raw = str(k)
                    canonical = raw
                    if raw.startswith("----:com.apple.iTunes:"):
                        name = raw.rsplit(":", 1)[-1]
                        canonical = next(
                            (n for n, spec in TAG_MAP.items()
                             if isinstance(spec.get("mp4"), tuple)
                             and spec["mp4"][0] == "freeform"
                             and spec["mp4"][2].lower() == name.lower()),
                            raw,
                        )
                    else:
                        canonical = next(
                            (n for n, spec in TAG_MAP.items()
                             if spec.get("mp4") == raw), raw
                        )
                    out[canonical] = self._mp4_text(val) or ""
        except Exception:
            return {}
        return out

    def set_any_tag(self, key, value):
        """Write an arbitrary tag key (raw container key).

        Used by the GUI tag editor: flac/ogg/opus take raw vorbis
        comment names, mp3 takes "TXXX:desc" custom frames or plain
        frame IDs, mp4 takes "----:mean:name" freeform atoms or plain
        atom names.
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
                self.audio.save()
                return True

            elif self.kind == "mp3":
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
                else:
                    self.audio.tags.delall(str(key))
                    frame_cls = Frames.get(str(key))
                    if frame_cls is None:
                        frame_cls = type(
                            str(key), (TextFrame,), {"FrameID": str(key)})
                    self.audio.tags.add(
                        frame_cls(encoding=Encoding.UTF8, text=[value])
                    )
                self.audio.save()
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
                    self.audio[str(key)] = [value]
                self.audio.save()
                return True

        except Exception as e:
            self.error = f"set_any_tag: {e}"
            return False
        return False

    def delete_any_tag(self, key):
        """Remove an arbitrary tag key (raw container key). MP4 atoms
        are matched case-insensitively over the full key list."""
        self._invalidate_cache()
        if self.audio is None:
            return False

        try:
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
                    self.audio.save()
                return True

            elif self.kind == "mp3":
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
                else:
                    self.audio.tags.delall(str(key))
                self.audio.save()
                return True

            elif self.kind == "mp4":
                target = str(key).lower()
                changed = False
                for k in list(self.audio.keys()):
                    if str(k).lower() == target:
                        del self.audio[k]
                        changed = True
                if changed:
                    self.audio.save()
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
        # User request: all written tags must have no leading/trailing spaces.
        # Trim every value (except LYRICS which is handled separately) and
        # enforce ITUNESADVISORY 0/1/2.
        value = str(value).strip()
        if name == "ITUNESADVISORY" and value not in ("0", "1", "2"):
            # Still write the trimmed value, but grading will flag invalid
            # values (non-0/1/2) as failure; we don't silently coerce.
            pass
        spec = TAG_MAP.get(name)
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
                self.audio.tags[spec["flac"]] = value
                self.audio.save()
                return True

            elif kind == "mp3":
                frame_type, desc = spec["mp3"]
                if self.audio.tags is None:
                    self.audio.add_tags()

                if frame_type == "USLT":
                    return self.set_lyrics(value)
                if frame_type == "TXXX":
                    for frame in list(self.audio.tags.getall("TXXX")):
                        if frame.desc.upper() == desc.upper():
                            try:
                                del self.audio.tags[frame.HashKey]
                            except Exception:
                                pass
                    self.audio.tags.add(
                        TXXX(encoding=Encoding.UTF8, desc=desc, text=[value])
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
                            frame_cls(encoding=Encoding.UTF8, text=[value])
                        )
                self.audio.save()
                return True

            elif kind == "mp4":
                atom = spec["mp4"]

                if isinstance(atom, tuple) and atom[0] == "freeform":
                    _, mean, name2 = atom
                    key = f"----:{mean}:{name2}"

                    fmt = getattr(MP4FreeForm, "FORMAT_UTF8", 1)

                    try:
                        self.audio[key] = [
                            MP4FreeForm(
                                value.encode("utf-8"),
                                dataformat=fmt,
                            )
                        ]
                    except TypeError:
                        self.audio[key] = [
                            MP4FreeForm(value.encode("utf-8"))
                        ]
                elif atom in ("trkn", "disk"):
                    pair = self._mp4_pair(value)
                    if pair is None:
                        return False
                    self.audio[atom] = [pair]
                elif atom == "tmpo":
                    try:
                        self.audio[atom] = [int(value)]
                    except (TypeError, ValueError):
                        return False
                else:
                    self.audio[atom] = [value]

                self.audio.save()
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
                    self.audio.save()

                return True

            elif kind == "mp3":
                frame_type, desc = spec["mp3"]
                if self.audio.tags is None:
                    return True

                changed = False
                if frame_type == "USLT":
                    return self.delete_lyrics()
                if frame_type == "TXXX":
                    for frame in list(self.audio.tags.getall("TXXX")):
                        if frame.desc.upper() == desc.upper():
                            try:
                                del self.audio.tags[frame.HashKey]
                                changed = True
                            except Exception:
                                pass
                else:
                    if frame_type == "COMM":
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
                    self.audio.save()

                return True

            elif kind == "mp4":
                atom = spec["mp4"]

                if isinstance(atom, tuple) and atom[0] == "freeform":
                    _, mean, name2 = atom
                    key = f"----:{mean}:{name2}"
                    if key in self.audio:
                        del self.audio[key]
                        self.audio.save()
                    return True

                if atom in self.audio:
                    del self.audio[atom]
                    self.audio.save()

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

            elif self.kind == "mp3":
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
                self.audio.save()
                return True

            elif self.kind == "mp3":
                self.audio.tags.delall("USLT")
                self.audio.tags.add(
                    USLT(encoding=Encoding.UTF8, lang="eng", desc="", text=text)
                )
                self.audio.save()
                return True

            elif self.kind == "mp4":
                self.audio["\xa9lyr"] = [text]
                self.audio.save()
                return True

        except Exception as e:
            self.error = f"set_lyrics: {e}"
            return False

        return False

    def delete_lyrics(self):
        self._invalidate_cache()
        try:
            if self.kind in ("flac", "ogg", "opus"):
                if self.audio.tags is None:
                    return True  # nothing to delete
                for k in list(self.audio.tags.keys()):
                    if str(k).lower() in ("lyrics", "unsyncedlyrics"):
                        del self.audio.tags[k]
                self.audio.save()
                return True

            elif self.kind == "mp3":
                self.audio.tags.delall("USLT")
                self.audio.save()
                return True

            elif self.kind == "mp4":
                if "\xa9lyr" in self.audio:
                    del self.audio["\xa9lyr"]
                    self.audio.save()
                return True

        except Exception as e:
            self.error = f"delete_lyrics: {e}"
            return False

        return False

    # ------------------------------------------------------------------
    # Embedded cover art — FLAC pictures, MP3 APIC, MP4 covr and the OGG/
    # Opus METADATA_BLOCK_PICTURE base64 form. Videos (attachments) and
    # raw AAC are not supported and return empty results.
    # ------------------------------------------------------------------
    def embedded_pictures(self):
        """[(mime, bytes)] of the embedded cover art currently in the file."""
        try:
            if self.kind == "flac" and self.audio is not None:
                return [(p.mime, bytes(p.data)) for p in self.audio.pictures]

            if self.kind == "mp3" and self.audio is not None and self.audio.tags:
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
                self.audio.save()
                return True

            if self.kind == "mp3" and self.audio is not None and self.audio.tags:
                if not self.audio.tags.getall("APIC"):
                    return False
                self.audio.tags.delall("APIC")
                self.audio.save()
                return True

            if self.kind == "mp4" and self.audio is not None and self.audio.tags:
                if "covr" not in self.audio.tags:
                    return False
                del self.audio.tags["covr"]
                self.audio.save()
                return True

            if self.kind in ("ogg", "opus") and self.audio is not None and self.audio.tags:
                keys = [k for k in self.audio.tags.keys()
                        if str(k).lower() == "metadata_block_picture"]
                if not keys:
                    return False
                for k in keys:
                    del self.audio.tags[k]
                self.audio.save()
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
                self.audio.save()
                return True

            if self.kind == "mp3" and self.audio is not None:
                if self.audio.tags is None:
                    self.audio.add_tags()
                self.audio.tags.delall("APIC")
                self.audio.tags.add(APIC(encoding=3, mime=mime, type=3, data=data))
                self.audio.save()
                return True

            if self.kind == "mp4" and self.audio is not None:
                fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
                self.audio.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
                self.audio.save()
                return True

            if self.kind in ("ogg", "opus") and self.audio is not None:
                import base64
                pic = Picture()
                pic.type = 3
                pic.mime = mime
                pic.data = data
                self.audio.tags["METADATA_BLOCK_PICTURE"] = base64.b64encode(pic.write()).decode("ascii")
                self.audio.save()
                return True
            return False
        except Exception as e:
            self.error = f"add_embedded_picture: {e}"
            return False

