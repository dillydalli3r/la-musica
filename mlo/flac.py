"""FLAC re-encoding via the reference flac.exe toolchain, and conversion of
the whole library to the configured codec target (`library_codec`)."""
import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import DEFAULT_CONFIG
from .containers import (
    CODECS, CODEC_KEEP, codec_extra_args, codec_is_lossless, encoder_args,
    file_codec, _read_flac_tags, _write_flac_tags, _identity_missing, _enabled,
)
from .subproc import run_tool
from .paths import AUDIO_EXTS, tools_dir, trash_file
from .tools import detect_all_tools, _version_is_older
from .stats import (
    new_stats, _make_pbar, _pbar_skip, _pbar_update, _diff_bytes, _walk_files,
    _collect_targets, worker_count,
)
from .ui import print_header, log, c, Color

# Lossless but uncompressed (or externally compressed) sources that script 3
# converts to FLAC so the whole library is losslessly compressed. ffmpeg
# decodes all of them; tags are copied from ffprobe metadata.
LOSSLESS_SOURCE_EXTS = (".wav", ".aif", ".aiff", ".ape", ".wv", ".shn", ".tta")

# Every extension the conversion pass looks at: the library's own audio
# containers (the PCM targets included) plus the lossless sources ffmpeg
# decodes but this library never keeps (APE/WV/SHN/TTA). A music VIDEO is not
# here on purpose — .mp4/.m4v are library video (paths.LIB_VIDEO_EXTS), and
# rewriting one to its audio would throw the video away.
CONVERSION_EXTS = tuple(dict.fromkeys(tuple(AUDIO_EXTS) + LOSSLESS_SOURCE_EXTS))

# How many refused files a run spells out (see _report_refusals).
_REFUSAL_LOG_CAP = 20

# Config `library_codec` -> the codec every converted file ends up in. FLAC
# is the shipped default and what the rest of the pipeline is built around
# (metaflac optimizer, ENCODER identity tags, `flac -t` verification); the
# table of every target (extensions, encoder args, lossless flag) is
# mlo.containers.CODECS.
CODEC_POLICIES = ("all", "lossless_to_lossy", CODEC_KEEP)


def target_codec(cfg):
    """Normalized `library_codec`: a CODECS key, or CODEC_KEEP for no target.

    An unknown or missing value falls back to the shipped default (flac), so
    a hand-edited config can never leave the pass without a target.
    """
    name = str((cfg or {}).get("library_codec") or "flac").strip().lower()
    return name if name in CODECS or name == CODEC_KEEP else "flac"


def library_codec_policy(cfg):
    """Normalized `library_codec_optimize` (the default when unusable).

    See DEFAULT_CONFIG: the default converts LOSSLESS sources only and never
    re-encodes a lossy file.
    """
    name = str((cfg or {}).get("library_codec_optimize") or "").strip().lower()
    return name if name in CODEC_POLICIES else "lossless_to_lossy"


def is_alac(path):
    """True when an .m4a/.mp4 file's audio track is ALAC.

    The extension cannot tell ALAC from AAC (both live in MP4 containers),
    and re-containerizing an AAC file as "lossless" would silently keep the
    lossy audio — mutagen reports the codec ('alac', 'mp4a.40.2'), see
    mlo.containers.file_codec.
    """
    return file_codec(path) == "alac"

# ffprobe metadata key -> semantic tag name. ffprobe echoes an ID3 TXXX
# frame as its description verbatim ("MusicBrainz Album Id") and a Vorbis
# comment as stored ("MUSICBRAINZ_ALBUMID"), so the table is matched through
# _ffprobe_key, which ignores case, spaces and underscores.
_FFPROBE_TAG_MAP = {
    "title": "TITLE",
    "artist": "ARTIST",
    "album": "ALBUM",
    "album_artist": "ALBUMARTIST",
    "date": "DATE",
    "originaldate": "ORIGINALDATE",
    "genre": "GENRE",
    "track": "TRACKNUMBER",
    "disc": "DISCNUMBER",
    "composer": "COMPOSER",
    "comment": "COMMENT",
    # TPUB (the ID3 label frame, "publisher" to ffprobe) and LABEL name the
    # same thing in this app — the record label the naming script uses.
    "publisher": "LABEL",
    "label": "LABEL",
    "copyright": "COPYRIGHT",
    "isrc": "ISRC",
    "bpm": "BPM",
    "initialkey": "INITIALKEY",
    "media": "MEDIA",
    "script": "SCRIPT",
    "source": "SOURCE",
    "catalognumber": "CATALOGNUMBER",
    "barcode": "BARCODE",
    # Loudness measurement tags. Their names contain a SPACE, so the
    # pass-through (which upper-cases a key into a container key) would
    # invent "DYNAMIC_RANGE" — a tag no reader of this app finds.
    "dynamic range": "DYNAMIC RANGE",
    "album dynamic range": "ALBUM DYNAMIC RANGE",
    # Release identity. Both spellings appear above — the Vorbis names and
    # the MusicBrainz ID3 TXXX descriptions — so a source tagged by either
    # this app or Picard converts with its release data intact.
    "releasetype": "RELEASETYPE",
    "musicbrainz_albumtype": "RELEASETYPE",
    "releasestatus": "RELEASESTATUS",
    "musicbrainz_albumstatus": "RELEASESTATUS",
    "releasecountry": "RELEASECOUNTRY",
    "musicbrainz_album_release_country": "RELEASECOUNTRY",
    "musicbrainz_trackid": "MUSICBRAINZ_TRACKID",
    "musicbrainz_albumid": "MUSICBRAINZ_ALBUMID",
    "musicbrainz_artistid": "MUSICBRAINZ_ARTISTID",
    "musicbrainz_albumartistid": "MUSICBRAINZ_ALBUMARTISTID",
    "musicbrainz_releasetrackid": "MUSICBRAINZ_RELEASETRACKID",
    "musicbrainz_releasegroupid": "MUSICBRAINZ_RELEASEGROUPID",
    "musicbrainz_workid": "MUSICBRAINZ_WORKID",
    "replaygain_track_gain": "REPLAYGAIN_TRACK_GAIN",
    "replaygain_track_peak": "REPLAYGAIN_TRACK_PEAK",
    "replaygain_album_gain": "REPLAYGAIN_ALBUM_GAIN",
    "replaygain_album_peak": "REPLAYGAIN_ALBUM_PEAK",
}


def _ffprobe_key(key):
    """Normalized ffprobe metadata key (case/space/underscore-insensitive)."""
    return re.sub(r"[\s_]+", "", str(key)).upper()


_FFPROBE_TAG_LOOKUP = {_ffprobe_key(k): v for k, v in _FFPROBE_TAG_MAP.items()}

# TAG_MAP's own spelling per normalized name, filled on first use: the
# pass-through builds a container key from an ffprobe key, but a semantic
# name is not always that key's upper form (a name with a space in it).
_TAG_NAME_CACHE = {}


def _canonical_tag_name(name):
    """TAG_MAP's spelling of *name* when it is one of its names, else *name*."""
    if not _TAG_NAME_CACHE:
        try:
            from .audio import TAG_MAP
            _TAG_NAME_CACHE.update({_ffprobe_key(k): k for k in TAG_MAP})
        except Exception:
            _TAG_NAME_CACHE[""] = ""
    return _TAG_NAME_CACHE.get(_ffprobe_key(name), name)


def _set_semantic_tag(af, name, value):
    """Write one semantic tag onto a conversion output.

    set_tag maps *name* onto the target container's own frame (Vorbis
    comment, ID3 frame, MP4 atom/freeform). set_any_tag takes the name as a
    RAW container key, and an MP4 target silently truncates anything longer
    than four characters to an atom of its first four — losing the value —
    so it is only the fallback for names that have no mapping.
    """
    if af.set_tag(name, value):
        return True
    key = name
    if af.kind == "mp4" and not name.startswith("----:"):
        # Still this app's own tag: keep an unmapped name in the freeform
        # space rather than writing a truncated 4-char atom.
        key = f"----:com.apple.iTunes:{name}"
    return af.set_any_tag(key, value)


# The verdicts that describe the LOSSLESS master's own PCM: AudioAuditor's
# real/fake-lossless read, the integrity test, and the CD rip log's per-track
# CRC. A lossy re-encode invalidates every one of them (decoded MP3 frames can
# never match the CRC the rip printed), so they are dropped from a lossy
# output and the audit pass re-derives them from the file that now exists.
# MEDIA/SOURCE are release facts rather than byte facts and travel with the
# file; AUDIOAUDITOR_OVERRIDE is the user's OWN verdict and is never dropped
# silently.
_LOSSLESS_VERDICT_TAGS = frozenset({"AUDIT", "INTEGRITY", "LOG_CRC",
                                    "AUDIO_MD5"})


# ----------------------------------------------------------------------
# The audio's own identity: the stream MD5, and whether it is TRUE
# ----------------------------------------------------------------------
# A FLAC's STREAMINFO carries the MD5 of its DECODED audio — the one identity
# a tag write cannot move (that is what mlo.discs' CRC memo, mlo.accurip's
# evidence and mlo.audit's verdict evidence are all filed under). It is a
# CLAIM about the audio, not proof of it: only a decode says whether it is
# true, and a file that states none (the all-zero field: "unknown", not
# "fine") has nothing to compare at all. The states below are that answer,
# named once so the audit (script 6), the grader (script 4) and this
# module's own conversions all report the same thing about the same bytes.
MD5_OK = "ok"                 # states a digest; its audio hashes to it
MD5_MISMATCH = "md5-mismatch"  # states a digest; the audio hashes elsewhere
MD5_ABSENT = "md5-absent"     # states none (all zeros) — unknown, not verified
MD5_FAILED = "error"          # could not be decoded/verified (corrupt stream)
MD5_UNKNOWN = "unknown"       # nothing could be established (no decoder)
MD5_NOT_FLAC = ""             # this container states no stream MD5 at all

# The words every report uses for the two findings that matter, so the audit
# log, the grader's issue line and a conversion's failure all name the same
# problem the same way.
MD5_FINDINGS = {
    MD5_MISMATCH: ("FLAC MD5 mismatch",
                   "the digest its STREAMINFO states is not the digest of its "
                   "own audio"),
    MD5_ABSENT: ("FLAC MD5 absent",
                 "the stream states no MD5 (all zero), so nothing verifies "
                 "its audio"),
}

# Bits per sample -> the ffmpeg codec that reproduces FLAC's own digest
# representation: signed, little-endian, ceil(bits/8) bytes a sample (24-bit
# as THREE bytes, not padded to four).
_PCM_CODECS = {8: "pcm_s8", 16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_s32le"}


def md5_finding(state, detail=""):
    """The one-line finding for a state, or "" when there is nothing to
    report: "FLAC MD5 mismatch" / "FLAC MD5 absent" plus what was seen."""
    named = MD5_FINDINGS.get(state)
    if not named:
        return ""
    return f"{named[0]}: {named[1]}" + (f" ({detail})" if detail else "")


def pcm_format(path):
    """(bits, sample_rate, channels) mutagen reports for *path*.

    Best effort on purpose: a container that cannot report one of them (an
    APE, an unheard-of extension) answers None for it, and a caller that
    cannot compare two files' sample formats must not claim an identity it
    cannot establish."""
    try:
        from .audio import AudioFile
        info = getattr(getattr(AudioFile(path), "audio", None), "info", None)
        if info is None:
            return (None, None, None)
        bits = getattr(info, "bits_per_sample", None)
        if bits is None:
            bits = getattr(info, "bits", None)
        rate = getattr(info, "sample_rate", None)
        channels = getattr(info, "channels", None)
        return (int(bits) if bits else None,
                int(rate) if rate else None,
                int(channels) if channels else None)
    except Exception:
        return (None, None, None)


def decoded_md5(path, ffmpeg_exe, bits=None):
    """(digest, error): the MD5 of *path*'s decoded PCM, as ffmpeg decodes it.

    *digest* is in the representation FLAC's STREAMINFO hashes (see
    _PCM_CODECS) when *bits* is one this can express, so a FLAC's digest
    compares directly with the digest it states; for any other width the
    decoded stream is 32-bit, which is exact for every source up to 32 bits
    and therefore still comparable with ITSELF (a source against its own
    conversion). None + a reason when the file cannot be decoded.
    """
    if not ffmpeg_exe or not os.path.isfile(ffmpeg_exe):
        return None, "no ffmpeg to decode with"
    codec = _PCM_CODECS.get(bits, "pcm_s32le")
    try:
        proc = run_tool(
            [ffmpeg_exe, "-v", "error", "-nostdin", "-i", path,
             "-map", "0:a:0", "-c:a", codec, "-f", "md5", "-"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600,
        )
    except Exception as e:
        return None, f"decode failed: {e}"
    if proc.returncode != 0:
        err = "; ".join((proc.stderr or "").strip().splitlines()[-2:])
        return None, f"decode failed: {err or proc.returncode}"
    match = re.search(r"MD5=([0-9a-fA-F]{32})", proc.stdout or "")
    if not match:
        return None, "no digest in ffmpeg's output"
    return match.group(1).lower(), None


def stream_md5_state(path, flac_exe=None, ffmpeg_exe=None, af=None):
    """(state, detail, digest) for one file's stated stream MD5.

    The reference check decides it: `flac -t` decodes the whole stream, and
    flac 1.5's own output tells the three cases apart — "ok" (the stated
    digest matched), "ERROR, MD5 signature mismatch", and "WARNING, cannot
    check MD5 signature since it was unset in the STREAMINFO" (the all-zero
    field, which flac accepts: an unverifiable stream is not an INVALID one,
    which is exactly why the app must report it rather than read rc=0 as
    "fine"). Without flac.exe the same question is answered by decoding the
    file with ffmpeg and hashing the samples against the digest the header
    states — a decode either way; nothing here trusts a header on its own.

    *digest* is the digest the file states ("" when it states none), and *af*
    an already-open AudioFile the caller holds (the same one-field read).
    """
    ext = os.path.splitext(str(path))[1].lower()
    stated = ""
    if ext == ".flac":
        from .accurip import stream_md5 as _stated
        stated = _stated(path, af)

    if ext == ".flac" and flac_exe and os.path.isfile(flac_exe):
        try:
            proc = run_tool([flac_exe, "-t", path], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", timeout=600)
        except subprocess.TimeoutExpired:
            return MD5_FAILED, "flac -t timeout", stated
        except Exception as e:
            return MD5_FAILED, str(e)[:200], stated
        out = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        last = next((ln.strip() for ln in reversed(out.splitlines())
                     if ln.strip()), "")
        if "MD5 signature mismatch" in out:
            return (MD5_MISMATCH,
                    last or f"flac -t rc={proc.returncode}", stated)
        if "cannot check MD5 signature" in out:
            # flac accepts it (rc=0): it decoded and hashed the audio itself
            # and had nothing to compare against.
            return MD5_ABSENT, last, ""
        if proc.returncode == 0:
            return MD5_OK, last, stated
        return MD5_FAILED, last or f"flac -t rc={proc.returncode}", stated

    if ext != ".flac":
        return MD5_NOT_FLAC, "", ""
    if not stated:
        return MD5_ABSENT, "the stream states no MD5 (all zero)", ""
    if not (ffmpeg_exe and os.path.isfile(ffmpeg_exe)):
        return (MD5_UNKNOWN,
                "no flac.exe or ffmpeg to verify the digest with", stated)
    bits = pcm_format(path)[0]
    if bits not in _PCM_CODECS:
        # Without the stream's own bit depth there is no representation the
        # stated digest can be compared against (32-bit would not be the
        # digest FLAC hashes), and guessing would read as a mismatch on a file
        # nothing is wrong with.
        return (MD5_UNKNOWN,
                f"the stream's bit depth ({bits or 'unknown'}) could not be "
                f"read", stated)
    digest, err = decoded_md5(path, ffmpeg_exe, bits)
    if digest is None:
        return MD5_FAILED, err or "decode failed", stated
    if digest != stated:
        return (MD5_MISMATCH,
                f"the audio hashes to {digest}, the header states {stated}",
                stated)
    return MD5_OK, f"decoded MD5 {digest}", stated


def convert_command(ffmpeg_exe, filepath, dest, cfg):
    """The ffmpeg command that converts *filepath* to the configured target.

    The ONE place the codec table, the quality/rate settings and
    `library_codec_args` meet, and pure enough that a test can assert the
    exact command line without encoding anything.
    """
    cmd = [ffmpeg_exe, "-y", "-v", "error", "-nostdin", "-i", filepath,
           "-map", "0:a:0"]
    # Embedded art is an attached_pic VIDEO stream, so mapping audio alone
    # dropped it before the original — the only copy of that artwork — left
    # the library. Copied only when the library is set to keep covers: the "?"
    # keeps an audio-only source working, and copy keeps the picture
    # bit-exact.
    if (bool(cfg.get("embed_covers", False))
            or bool(cfg.get("flac_preserve_picture", False))):
        cmd += ["-map", "0:v?", "-c:v", "copy"]
    return cmd + encoder_args(target_codec(cfg), cfg) + [dest]


def conversion_verdict(path, cfg):
    """(verdict, reason) for ONE file under the configured target + policy.

    "convert" — the pass converts this file.
    "skip"    — nothing to do: already the target, or the policy does not
                cover the file. Silent by design.
    "refuse"  — the policy asked for it and the pass must not. Reported.

    The rules are the whole policy:
      * `library_codec` or `library_codec_optimize` = "keep" converts nothing;
      * a file that already matches the target EXTENSION and CODEC is done.
        The extension alone cannot answer that — .m4a holds ALAC or AAC, .ogg
        holds Vorbis or Opus — so an AAC-in-MP4 under a lossless target used
        to read as "already .m4a" and stayed lossy;
      * a LOSSLESS source always converts to the target: that is the
        lossless -> lossy transcode the default policy is named for when the
        target is lossy, and a lossless re-container when it is not;
      * a LOSSY source under a LOSSLESS target is REFUSED whatever the policy
        says, "all" included: its samples are already gone, so encoding them
        again cannot restore one and can only lose more. The pass only asks
        this question about its CANDIDATES (_candidate_exts), so under the
        default policy a lossy file is never considered and never reported —
        the refusal is what "all" gets told when it tries anyway;
      * a LOSSY source otherwise converts only under "all" — lossy -> lossy is
        a generation loss the default policy refuses.
    """
    spec = CODECS.get(target_codec(cfg))
    if spec is None or library_codec_policy(cfg) == CODEC_KEEP:
        return ("skip", "nothing is converted — the library codec is kept "
                        "as it is")
    ext = os.path.splitext(str(path))[1].lower()
    src = file_codec(path)
    if ext == spec["ext"] and src == spec["codec"]:
        return ("skip", f"already {spec['codec']} in {spec['ext']}")
    if codec_is_lossless(src) or (not src and ext in LOSSLESS_SOURCE_EXTS):
        # An unparsable source whose extension is a lossless one still counts
        # as lossless: ffmpeg decodes it, and skipping it would strand the
        # file in a format the library is not supposed to hold.
        return ("convert", "")
    label = src or ext.lstrip(".") or "unknown"
    if spec["lossless"]:
        return ("refuse",
                f"{label} is lossy and {spec['codec']} is lossless — "
                f"re-encoding cannot restore a sample, so the file is left "
                f"as it is")
    if library_codec_policy(cfg) == "all":
        return ("convert", "")
    return ("skip", f"{label} is lossy — only the 'all' policy re-encodes "
                    f"a lossy source")


def _candidate_exts(cfg):
    """The extensions the conversion pass has to LOOK at under *cfg*.

    Under `all` that is every audio container the library tracks. Under the
    default policy only a LOSSLESS source can ever be converted, so the walk
    (and the codec probe behind it) covers the containers that can hold one,
    plus the target's own extension — whose files are the no-op "already
    there" case. This is what keeps a lossy source from being reported as a
    refusal on every run of a FLAC library: it is never a candidate, so the
    rule is never applied to it.
    """
    spec = CODECS.get(target_codec(cfg))
    out_ext = spec["ext"] if spec else ""
    if library_codec_policy(cfg) == "all":
        return CONVERSION_EXTS
    return tuple(dict.fromkeys(tuple(LOSSLESS_SOURCE_EXTS)
                               + (".flac", ".m4a") + (out_ext,)))


def conversion_candidates(target, targets, cfg, files=None):
    """(files to convert, refusals) for the whole run, in path order.

    *files* is a list the caller already walked (the FLAC pass scans the same
    tree for its own extensions, so a FLAC run hands its union over instead of
    walking the library a second time); it is filtered to the extensions this
    policy has to consider (see _candidate_exts).
    """
    exts = _candidate_exts(cfg)
    if files is None:
        files = (sorted(_walk_files(target, exts)) if targets is None
                 else sorted(_collect_targets(targets, exts)))
    out, refusals, seen = [], [], set()
    for p in files:
        if not str(p).lower().endswith(exts):
            continue
        key = os.path.normcase(p)
        if key in seen:
            continue
        seen.add(key)
        verdict, reason = conversion_verdict(p, cfg)
        if verdict == "convert":
            out.append(p)
        elif verdict == "refuse":
            refusals.append((p, reason))
    return out, refusals


def _report_refusals(refusals):
    """Print the files the policy wanted converted and the pass refused.

    Only the head of the list is spelled out: a library-wide "all" run over a
    lossless target refuses every lossy file in it, and a thousand identical
    lines explain nothing a count does not.
    """
    for path, reason in refusals[:_REFUSAL_LOG_CAP]:
        log(c(f"  {os.path.basename(path)}: not converted — {reason}",
              Color.YELLOW))
    if len(refusals) > _REFUSAL_LOG_CAP:
        log(c(f"  … and {len(refusals) - _REFUSAL_LOG_CAP} more file(s) "
              f"refused for the same reasons", Color.YELLOW))


def _trash_converted(path, cfg):
    """Move a converted ORIGINAL into the app's trash bin ("" on failure).

    The bin is per user and a script run carries no session, so the scope is
    the install's own auth_username — the bin its Trash page lists — falling
    back to the `default` scope of an unclaimed install. mlo.paths.trash_file
    records the origin, which is what lets the page restore the file.
    """
    return trash_file(path, music_folder=(cfg or {}).get("music_folder") or "",
                      user=(cfg or {}).get("auth_username") or "")


def _lossless_identity(src, dst, spec, ffmpeg_exe, src_stream):
    """(ok, message, note) for a conversion whose target is lossless.

    The identity across a conversion is the decoded MD5 (requirement: the
    converted file's MD5 must equal the MD5 of what the source decoded to).
    *ok* is False when it does not — the caller keeps the original — and
    *note* carries what could not be established (a target sample format that
    differs from the source's, so the conversion is not sample-identical by
    construction, or a bit depth with no comparable PCM representation). A
    note is NOT a failure: it says the pass has no proof either way, and the
    run says so instead of claiming one.
    """
    src_bits, src_rate, src_ch = src_stream
    out_bits, out_rate, out_ch = pcm_format(dst)

    if (src_bits and out_bits and out_rate and out_ch
            and (src_bits, src_rate, src_ch) != (out_bits, out_rate, out_ch)):
        # The target's own sample format (the PCM codecs force 16-bit) or the
        # user's `library_codec_args` changed the samples: there is no
        # identity to compare, and calling it a mismatch would fail a
        # conversion the app itself configured.
        return (True,
                "",
                f"sample format changed ({src_bits}-bit {src_rate} Hz {src_ch}ch"
                f" -> {out_bits}-bit {out_rate} Hz {out_ch}ch) — no audio "
                f"identity to compare")

    if not src_bits or src_bits not in _PCM_CODECS:
        return (True, "",
                f"the source's bit depth ({src_bits or 'unknown'}) has no "
                f"comparable PCM representation — the conversion was not "
                f"verified by digest")

    digest, err = decoded_md5(src, ffmpeg_exe, src_bits)
    if digest is None:
        # Nothing decoded the source, so nothing proved the conversion: an
        # unverifiable conversion must not replace the file it came from.
        return (False, f"could not decode the source to verify the "
                       f"conversion: {err}", "")

    if spec["codec"] == "flac":
        from .accurip import stream_md5 as _stated
        flac_exe = (detect_all_tools().get("flac") or {}).get("flac_exe")
        stated = _stated(dst)
        if stated and stated != digest:
            detail = (f"the converted file states {stated}, the source "
                      f"decodes to {digest}")
            return (False,
                    f"{md5_finding(MD5_MISMATCH, detail)} — the conversion "
                    f"changed the audio, so the original was kept", "")
        state, detail, _ = stream_md5_state(dst, flac_exe, ffmpeg_exe)
        if state != MD5_OK:
            return (False,
                    md5_finding(state, detail) if state in MD5_FINDINGS else
                    f"the converted file could not be verified ({detail})",
                    "")
        return (True, "", "")

    out_digest, out_err = decoded_md5(dst, ffmpeg_exe, out_bits)
    if out_digest is None:
        return (False, f"could not decode the converted file to verify it: "
                       f"{out_err}", "")
    if out_digest != digest:
        return (False,
                f"the converted audio hashes to {out_digest}, the source "
                f"decodes to {digest} — the conversion changed the audio, so "
                f"the original was kept", "")
    return (True, "", "")


def _convert_lossless_source(args):
    """Convert one source file to the configured target codec.

    ffmpeg decodes (every extension in LOSSLESS_SOURCE_EXTS, plus FLAC/ALAC
    when the target is another codec) and encodes the target codec — its
    rate/quality and `library_codec_args` come from the config (see
    mlo.containers.encoder_args); tags are copied from ffprobe metadata (minus
    the lossless-master verdicts when the target is lossy); the FLAC target
    additionally gets metaflac block stripping and this pipeline's ENCODER
    identity tags. The original moves to the app's trash only after a
    verified conversion, and only when lossless_remove_original is set.
    Returns (filename, ok, message, bytes_removed, bytes_added).
    """
    (
        ffmpeg_exe, ffprobe_exe, metaflac_exe, filepath,
        quality, target_version, enabled, config,
    ) = args
    filename = os.path.basename(filepath)
    codec = target_codec(config)
    spec = CODECS[codec]
    out_ext = spec["ext"]
    # Forward slashes: ffmpeg's demuxer probing is cleaner with them (VOB
    # phantom streams) and every Windows tool accepts them.
    filepath = str(filepath).replace("\\", "/")
    dest = os.path.splitext(filepath)[0] + out_ext
    # A codec change inside the SAME container (AAC-in-M4A -> ALAC) targets the
    # file it is decoded from: the pass reads the source, writes the temp
    # output, and only then replaces it (see the end of the function).
    in_place = (os.path.normcase(os.path.abspath(dest))
                == os.path.normcase(os.path.abspath(filepath)))
    if not in_place and os.path.exists(dest):
        return (filename, False, f"skipped (same-stem {out_ext} exists)", 0, 0)

    src_dur = 0.0
    src_stream = (None, None, None)
    raw_tags = {}
    try:
        src_probe = run_tool(
            [ffprobe_exe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", filepath],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        src_format = json.loads(src_probe.stdout or "{}").get("format") or {}
        src_dur = float(src_format.get("duration") or 0)
        # -show_format carries the tags too, so this one probe feeds both the
        # duration check below and the tag copy further down: asking ffprobe
        # for the same JSON a second time was one extra process spawn per
        # converted file.
        raw_tags = src_format.get("tags") or {}
        # …and -show_streams rides the same spawn for the lossless identity
        # check at the end of this function, which needs the SOURCE's sample
        # format to know whether the converted file can be sample-identical
        # at all (the PCM targets force 16-bit, so a 24-bit source against
        # them is a change by construction, not a lost audio).
        for stream in (json.loads(src_probe.stdout or "{}").get("streams") or []):
            if stream.get("codec_type") == "audio":
                bits = (stream.get("bits_per_raw_sample")
                        or stream.get("bits_per_sample"))
                try:
                    src_stream = (
                        int(bits) if bits else None,
                        int(stream["sample_rate"]) if stream.get("sample_rate") else None,
                        int(stream["channels"]) if stream.get("channels") else None,
                    )
                except (TypeError, ValueError):
                    src_stream = (None, None, None)
                break
    except Exception:
        src_dur = 0.0

    fd, tmp = tempfile.mkstemp(
        prefix=".conv_", suffix=out_ext, dir=os.path.dirname(filepath) or ".")
    os.close(fd)
    try:
        cmd = convert_command(ffmpeg_exe, filepath, tmp, config)
        try:
            proc = run_tool(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60 * 60)
        except Exception as e:
            return (filename, False, f"ffmpeg failed: {e}", 0, 0)
        if proc.returncode != 0:
            err = "; ".join((proc.stderr or "").strip().splitlines()[-2:])
            return (filename, False, f"convert failed: {err}", 0, 0)

        # Verify duration before touching anything else. The output container
        # is opened right below for the tag copy, so its own stream info
        # answers the question — one process spawn less per converted file
        # (ffprobe on a file this pipeline just wrote). A container mutagen
        # cannot read falls back to the probe, exactly as before.
        out_af = None
        out_dur = 0.0
        if src_dur:
            try:
                from .audio import AudioFile
                out_af = AudioFile(tmp)
                info = getattr(getattr(out_af, "audio", None), "info", None)
                out_dur = float(getattr(info, "length", 0) or 0)
            except Exception:
                out_af = None
                out_dur = 0.0
        if src_dur and not out_dur:
            try:
                out_probe = run_tool(
                    [ffprobe_exe, "-v", "error", "-print_format", "json",
                     "-show_format", tmp],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=30,
                )
                out_dur = float((json.loads(out_probe.stdout or "{}")
                                 .get("format") or {}).get("duration") or 0)
            except Exception:
                out_dur = 0.0
        if src_dur and out_dur and abs(src_dur - out_dur) > max(1.0, 0.005 * src_dur):
            return (filename, False,
                    f"duration changed ({src_dur:.2f}s -> {out_dur:.2f}s)", 0, 0)

        # Copy text tags from the source metadata.
        try:
            from .audio import AudioFile
            out_af = out_af or AudioFile(tmp)
            seen = set()
            out_af.defer_save(True)
            for k, v in raw_tags.items():
                name = _FFPROBE_TAG_LOOKUP.get(_ffprobe_key(k))
                if not name or name in seen:
                    continue
                if v is None or not str(v).strip():
                    continue
                if not spec["lossless"] and name in _LOSSLESS_VERDICT_TAGS:
                    continue
                val = str(v).strip()
                if name in ("TRACKNUMBER", "DISCNUMBER") and "/" in val:
                    val = val.split("/")[0].strip()
                if _set_semantic_tag(out_af, name, val):
                    seen.add(name)
            # Unknown keys that look intentional (uppercase-able) pass through
            for k, v in raw_tags.items():
                name = str(k).upper().replace(" ", "_")
                if (_ffprobe_key(k) in _FFPROBE_TAG_LOOKUP
                        or not name.replace("_", "").isalnum()
                        or name in seen or not str(v or "").strip()):
                    continue
                if len(name) > 40:
                    continue
                if not spec["lossless"] and name in _LOSSLESS_VERDICT_TAGS:
                    continue
                # The built name is a container key; write it under this
                # app's own name when TAG_MAP has one (the two differ for
                # names that contain a space).
                _set_semantic_tag(out_af, _canonical_tag_name(name),
                                  str(v).strip())
                seen.add(name)
            out_af.defer_save(False)
        except Exception:
            try:
                out_af.defer_save(False)
            except Exception:
                pass

        # Strip unwanted blocks + write our encoder identity, matching the
        # FLAC optimizer's output conventions. MP4 (ALAC) has no such blocks
        # and is tagged purely through mutagen above.
        if codec == "flac":
            if metaflac_exe:
                try:
                    # The same block policy the FLAC optimizer applies:
                    # PADDING and SEEKTABLE follow their settings. Stripping
                    # both unconditionally left a converted file violating
                    # add_seektables / flac_no_padding, so the next run had to
                    # touch it again (and never matched the encoder marker's
                    # own claim about the file).
                    parts = ["CUESHEET", "APPLICATION"]
                    if config.get("flac_no_padding", True):
                        parts.append("PADDING")
                    if not config.get("add_seektables", False):
                        parts.append("SEEKTABLE")
                    run_tool([metaflac_exe, "--dont-use-padding", "--remove",
                              "--block-type=" + ",".join(parts), tmp],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             text=True)
                    if config.get("add_seektables", False):
                        # ffmpeg's FLAC muxer writes no seektable at all, so
                        # when the setting asks for one it has to be added —
                        # 10 s spacing is what flac.exe uses itself.
                        run_tool([metaflac_exe, "--add-seekpoint=10s", tmp],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True)
                except Exception:
                    pass
            try:
                _write_flac_tags(tmp, quality, target_version, enabled)
            except Exception:
                pass

        try:
            out_size = os.path.getsize(tmp)
        except OSError as e:
            return (filename, False, f"cannot stat output: {e}", 0, 0)
        if out_size == 0:
            return (filename, False, "empty output", 0, 0)

        # ---- the audio identity across the conversion -------------------
        # The whole point of the pass is a file that IS the audio the source
        # held, so the conversion is proved, not assumed: the source is
        # decoded and hashed, and the converted file must state that same
        # digest — with the reference decoder (flac -t) confirming that its
        # stated digest is the digest of ITS audio, so "the encoder hashed
        # what it encoded" is verified rather than trusted. A mismatch is a
        # FAILED conversion: nothing is replaced, the original stays put, and
        # the run reports it (see run_optimize_flacs' conversion loop).
        audio_note = ""
        if spec["lossless"]:
            identity_ok, identity_msg, audio_note = _lossless_identity(
                filepath, tmp, spec, ffmpeg_exe, src_stream)
            if not identity_ok:
                return (filename, False, identity_msg, 0, 0)
            if audio_note:
                log(c(f"  [audio identity] {filename}: {audio_note}",
                      Color.YELLOW))

        try:
            src_size = os.path.getsize(filepath)
        except OSError:
            src_size = 0
        b_rem = b_add = 0
        moved = ""
        # Moved into the app's trash, never deleted: the source is the
        # lossless master (or the only copy of a lossy source the "all" policy
        # re-encoded), and the Trash page can put it back where it came from.
        # b_rem still counts it — the run's byte story is about the library
        # tree, and the bin is emptied by the user, not by script 3.
        keep_source = not config.get("lossless_remove_original", True)
        if in_place:
            if keep_source:
                # One path cannot hold two encodings, so "keep the original"
                # has nothing to keep here. Reported instead of silently
                # overwriting the file with itself.
                return (filename, False,
                        "skipped (the target container is the file's own — "
                        "keeping the original is impossible)", 0, 0)
            moved = _trash_converted(filepath, config)
            if not moved:
                return (filename, False,
                        "cannot move the original to the trash", 0, 0)
            b_rem = src_size
        os.replace(tmp, dest)
        tmp = None
        if not in_place and not keep_source:
            moved = _trash_converted(filepath, config)
            if moved:
                b_rem = src_size
        b_add = out_size
        src_label = os.path.splitext(filepath)[1].lstrip(".").upper()
        dst_label = out_ext.lstrip(".").upper()
        verified = " · audio identity verified" if spec["lossless"] else ""
        return (filename, True,
                f"{src_size // 1024} KB {src_label} -> {out_size // 1024} KB "
                f"{dst_label}" + (" (original moved to trash)" if moved else "")
                + verified,
                b_rem, b_add)
    except Exception as e:
        return (filename, False, f"exception: {e}", 0, 0)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

def _should_reencode_flac(filepath, target_quality, target_version, force,
                          enabled=None):
    """Return (should_reencode, reason, written_by_us).

    written_by_us is True when the ENCODER identity tags match this
    pipeline's own marker, meaning the file was encoded with
    --no-seektable and had foreign metadata blocks stripped already.
    """
    if force:
        return True, "force re-encode", False

    q, v, program = _read_flac_tags(filepath)

    if _identity_missing(enabled, q, v, program):
        return True, "missing ENCODER tags", False

    if _enabled(enabled, "ENCODER_QUALITY"):
        try:
            if int(q) < int(target_quality):
                return True, f"quality {q} < {target_quality}", False
        except (ValueError, TypeError):
            return True, f"quality not numeric: {q}", False

    if _enabled(enabled, "ENCODER_VERSION") and _version_is_older(v, target_version):
        # Like the quality compare above: with ENCODER_VERSION switched off the
        # marker is never rewritten (containers._identity_missing), so an old
        # version tag would re-encode this file on every run and never converge.
        return True, f"encoder {v} older than {target_version}", False

    ours = str(program or "").strip() == "FLAC reference encoder"
    return False, f"already at quality={q}, version={v}", ours


def _flac_has_seektable(filepath):
    """Whether *filepath* carries a SEEKTABLE block.

    mutagen reads only the metadata blocks, so this replaces a metaflac
    --list process spawn per file (one exe launch per foreign file on every
    run of the script). A file mutagen cannot parse reports True, which
    keeps the caller's old "assume the block is there" behaviour instead of
    silently doing nothing.
    """
    try:
        from mutagen.flac import FLAC, SeekTable
        return any(isinstance(b, SeekTable) for b in FLAC(filepath).metadata_blocks)
    except Exception:
        return True


def _optimize_flac(args):
    # Backwards compatible: older callers pass 8 args, new pass 9 with config
    if len(args) == 9:
        (
            flac_exe,
            metaflac_exe,
            filepath,
            quality,
            add_seektables,
            target_version,
            force,
            enabled,
            config,
        ) = args
    else:
        (
            flac_exe,
            metaflac_exe,
            filepath,
            quality,
            add_seektables,
            target_version,
            force,
            enabled,
        ) = args
        config = None

    filename = os.path.basename(filepath)
    temp_path = filepath + ".opttmp.flac"

    should_reencode, reason, _ours = _should_reencode_flac(
        filepath,
        quality,
        target_version,
        force,
        enabled,
    )

    # Only clean tags when we will re-encode or when file is already ours and
    # needs tag cleanup; otherwise don't mutate a file we will skip.
    # _clean_flac_tags is applied to the temp output after the flac re-encode.
    if not should_reencode:
        # Even when skipping the re-encode the seektable state has to be
        # applied: `add_seektables` is NOT part of the skip decision (the
        # ENCODER tags say nothing about it), so a foreign FLAC that already
        # carries our tags would otherwise never get the seektable the current
        # setting asks for — and one of ours encoded under the other setting
        # would keep the wrong state forever. The block list is the only
        # truth, and it is read from mutagen, so the pass costs no process
        # spawn when the state already matches.
        try:
            original_size = os.path.getsize(filepath)
        except OSError as e:
            return (filename, False, f"cannot stat file: {e}", 0, 0)

        if metaflac_exe and _flac_has_seektable(filepath) != bool(add_seektables):
            # "--add-seekpoint=10s", not "--add-seektable": metaflac has no
            # such option (flac 1.5 rejects it) — adding seekpoints IS how a
            # SEEKTABLE is created. 10 s is flac.exe's own default spacing, so
            # the file ends up with the table a fresh encode would have
            # written.
            cmd = ([metaflac_exe, "--add-seekpoint=10s", filepath]
                   if add_seektables
                   else [metaflac_exe, "--remove", "--block-type=SEEKTABLE",
                         filepath])
            try:
                result = run_tool(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )

                if result.returncode != 0:
                    err = (result.stderr or "").strip()
                    log(c(f"[strip warn] {filename}: metaflac failed: {err}",
                          Color.YELLOW))
                else:
                    # The BLOCK STATE, not the byte delta, says whether the
                    # pass changed anything: adding a seekpoint block can
                    # consume the padding block and leave the file exactly the
                    # size it was.
                    if _flac_has_seektable(filepath) != bool(add_seektables):
                        return (filename, False, f"skipped ({reason})", 0, 0)
                    b_rem, b_add = _diff_bytes(original_size,
                                               os.path.getsize(filepath))
                    return (
                        filename,
                        True,
                        "added seektable (skipped re-encode)"
                        if add_seektables
                        else "removed seektable (skipped re-encode)",
                        b_rem,
                        b_add,
                    )
            except Exception:
                pass

        return (filename, False, f"skipped ({reason})", 0, 0)

    try:
        original_size = os.path.getsize(filepath)
    except OSError as e:
        return (filename, False, f"cannot stat file: {e}", 0, 0)

    flac_no_pad = bool(config.get("flac_no_padding", True)) if config else True
    # -V makes flac.exe decode its own output and compare it with the source,
    # so a bad encode fails here (returncode != 0) and the verified original is
    # never replaced by a broken file.
    flac_args = [f"-{quality}", "-f", "-V"]
    # --padding is a CAP on the padding block flac.exe writes. flac_no_padding
    # (the default) asks for none: the file is as small as it can be, at the
    # price of a whole-file rewrite for every later tag pass (scripts 8/7/12/
    # 16/10 all write tags). Keeping padding (the flag off) asks for 8192
    # bytes — flac.exe's own default — which those passes swap in place.
    flac_args.append("--padding=0" if flac_no_pad else "--padding=8192")

    if not add_seektables:
        flac_args.append("--no-seektable")

    # library_codec_args: for a FLAC target these are flac.exe's own options
    # (see DEFAULT_CONFIG), appended verbatim after the flags this pass sets,
    # so a user's later flag wins wherever flac.exe honours the last one.
    flac_args += codec_extra_args(config) if config else []

    cmd = [flac_exe] + flac_args + ["-o", temp_path, filepath]

    try:
        result = run_tool(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        if result.returncode != 0:
            err = (result.stderr or "").strip()
            last = next((ln.strip() for ln in reversed(err.splitlines())
                         if ln.strip()), "") or f"rc={result.returncode}"
            # `-V` has flac decode its own output and compare it with what it
            # read, so this branch is the encode failing to reproduce the
            # source — and, one case sharper, the source's stated STREAMINFO
            # MD5 not being the MD5 of its own audio. That file is corrupt or
            # dishonestly written, so its audio is not a master anything
            # should be re-encoded from: the temp output is dropped and the
            # original stays exactly where it was. Named, because
            # "flac.exe failed" alone made the two look alike.
            if "MD5sum of input is different" in err:
                return (filename, False,
                        f"{md5_finding(MD5_MISMATCH, 'flac -V: ' + last)} — "
                        f"the original was kept and NOT re-encoded", 0, 0)
            if "Verify failed" in err or "verify failed" in err:
                return (filename, False,
                        f"the re-encode does not reproduce the source's audio "
                        f"({last}) — the original was kept", 0, 0)
            return (filename, False, f"flac.exe failed: {last}", 0, 0)

        if not os.path.exists(temp_path):
            return (filename, False, "flac.exe produced no output", 0, 0)

        # Clean FLAC tags on temp output (not original) - conservative
        try:
            from .containers import _clean_flac_tags
            _clean_flac_tags(temp_path, config=config, enabled=enabled)
        except Exception:
            pass

        # Strip unwanted metadata from the temporary output before replacing
        # the original file. This keeps failure handling safe and accurate.
        if metaflac_exe:
            # Embedded art follows the embed_covers setting: by default the
            # optimizer removes it; when embedding is on the picture stays
            # (script 10 embeds the album cover). flac_preserve_picture is
            # the legacy per-format override.
            preserve_pic = (
                bool(config.get("embed_covers", False))
                or bool(config.get("flac_preserve_picture", False))
            ) if config else False
            parts = []
            if not preserve_pic:
                parts.append("PICTURE")
            parts.extend(["CUESHEET", "APPLICATION"])
            if flac_no_pad:
                # --padding=0 already wrote none, and this removes any the
                # source carried, so the optimizer's own output is as small as
                # the codec allows. Note a later TAG write (the ENCODER marker
                # just above, script 10) makes mutagen add its own small
                # padding block back — that is the swap-in-place headroom the
                # following passes need, not something this setting controls.
                parts.append("PADDING")
            if not add_seektables:
                # SEEKTABLE already handled via flac --no-seektable, but also strip existing
                if "SEEKTABLE" not in parts:
                    parts.insert(1 if not preserve_pic else 0, "SEEKTABLE")
            blocks = ",".join(parts)

            try:
                result = run_tool(
                    [
                        metaflac_exe,
                        "--dont-use-padding",
                        "--remove",
                        "--block-type=" + blocks,
                        temp_path,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if result.returncode != 0:
                    err = (result.stderr or "").strip()
                    log(c(f"[strip warn] {filename}: metaflac failed: {err}",
                          Color.YELLOW))
            except Exception:
                pass

        try:
            _write_flac_tags(temp_path, quality, target_version, enabled)
        except Exception as e:
            log(c(f"[tag warn] {filename}: {e}", Color.YELLOW))

        final_size = os.path.getsize(temp_path)
        os.replace(temp_path, filepath)
        temp_path = None

        b_rem, b_add = _diff_bytes(original_size, final_size)
        info = f"{original_size // 1024} KB -> {final_size // 1024} KB"

        return (filename, True, info, b_rem, b_add)

    except Exception as e:
        return (filename, False, f"exception: {e}", 0, 0)

    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def convert_album_lossless(album_dir, cfg):
    """Convert one album's files to the configured library codec target.

    Scoped run of the script-3 conversion: only this folder is scanned, so an
    import never rewrites the rest of the library, and the POLICY still
    decides what may happen (a lossy source is never up-converted). Returns
    the optimizer stats (empty stats when the folder holds nothing to
    convert).
    """
    scoped = dict(cfg or {})
    scoped["targets"] = [str(album_dir)]
    scoped["force_reencode_flac"] = False
    return run_optimize_flacs(scoped)


def run_optimize_flacs(config):
    # `.get` with the shipped default, like the rest of this module: a partial
    # cfg (a test, the album-scoped conversion helper) must not KeyError here.
    quality = config.get("library_codec_quality",
                         DEFAULT_CONFIG["library_codec_quality"])
    add_seektables = config.get("add_seektables", DEFAULT_CONFIG["add_seektables"])
    force = config.get("force_reencode_flac", False)
    stats = new_stats()

    tools = detect_all_tools()
    flac_tool = tools.get("flac")

    codec = target_codec(config)
    spec = CODECS.get(codec)
    out_ext = spec["ext"] if spec else ""
    policy = library_codec_policy(config)
    # The flac.exe pass re-compresses FLACs IN PLACE — same codec, no
    # conversion — so it is this script's own job and runs whenever FLAC is
    # what the library holds, including both "keep" choices. With another
    # target the existing FLACs are re-containerized by the conversion step
    # below instead, and re-encoding them first would be pure wasted work.
    optimize_flac = codec in ("flac", CODEC_KEEP)
    convert = spec is not None and policy != CODEC_KEEP

    if not flac_tool and optimize_flac:
        log(c("ERROR: Could not auto-detect flac.exe in the tools folder.", Color.RED))
        log(f"Expected a folder like: {os.path.join(tools_dir(), 'flac v1.5.0')}")
        return stats

    flac_exe = (flac_tool or {}).get("flac_exe")
    metaflac_exe = (flac_tool or {}).get("metaflac_exe")
    target_version = (flac_tool or {}).get("version") or ""

    print_header("FLAC Optimizer")
    target_label = out_ext.lstrip(".").upper() if out_ext else CODEC_KEEP.upper()
    log(f"library codec: {target_label} · policy: {policy}")
    if codec == "flac":
        strip_msg = ("PICTURE, " if not (config or {}).get("embed_covers", False)
                     and not (config or {}).get("flac_preserve_picture", False) else "")
        strip_msg += "CUESHEET, APPLICATION"
        if config.get("flac_no_padding", True):
            strip_msg += ", PADDING"
        if not add_seektables:
            strip_msg += ", SEEKTABLE"
        log(
            f"level=-{quality} · seektables={'on' if add_seektables else 'off'} · "
            f"encoder={target_version} · force={'on' if force else 'off'} · "
            f"padding={'none' if config.get('flac_no_padding', True) else '8 KB'} · "
            f"strip={strip_msg}"
        )
    elif out_ext and config.get("library_codec_args"):
        log(f"extra encoder args: {config['library_codec_args']}")

    target = os.path.abspath(config["music_folder"] or os.getcwd())

    if not os.path.isdir(target):
        log(c(f"ERROR: TARGET_DIR does not exist: {target}", Color.RED))
        return stats

    log(f"target: {target}")
    log(f"flac.exe: {flac_exe} · metaflac.exe: {metaflac_exe or '(not found)'}")

    targets = config.get("targets")
    flac_files = []
    conv_scan = None
    if optimize_flac or convert:
        # One walk for both passes: the conversion step below needs the
        # lossless sources (+ ALAC-in-MP4) and used to walk the whole library
        # again for them after this one had finished. The union is collected
        # here and split, which is the same file set with one directory scan.
        walked = (sorted(_walk_files(target, CONVERSION_EXTS))
                  if targets is None
                  else sorted(_collect_targets(targets, CONVERSION_EXTS)))
        if optimize_flac:
            flac_files = sorted(
                f for f in walked
                if f.lower().endswith(".flac")
                and not f.lower().endswith(".opttmp.flac")
            )

            # Deduplicate (Select All checks album + tracks -> duplicates) + normcase for Windows
            if len(flac_files) != len(set(os.path.normcase(p) for p in flac_files)):
                log(c(f"WARNING: flac_files has duplicates: {len(flac_files)} vs {len(set(os.path.normcase(p) for p in flac_files))} unique", Color.YELLOW))
                seen = {}
                for p in flac_files:
                    seen[os.path.normcase(p)] = p
                flac_files = sorted(seen.values())

            if not flac_files:
                log("No FLAC files found.")
        conv_scan = walked if convert else None

    workers = worker_count(config, default=os.cpu_count() or 1,
                          items=len(flac_files))
    counts = {"ok": 0, "skip": 0, "fail": 0}

    args_list = [
        (
            flac_exe,
            metaflac_exe,
            fp,
            quality,
            add_seektables,
            target_version,
            force,
            (config.get("encoder_tags") or {}).get("flac") or {},
            config,
        )
        for fp in flac_files
    ]

    if args_list:
      with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_optimize_flac, a) for a in args_list]
        pbar = _make_pbar(len(futures), "FLAC")

        for future in as_completed(futures):
            try:
                filename, ok, info, b_rem, b_add = future.result()
            except Exception as e:
                stats["total_scanned"] += 1
                stats["error_count"] += 1
                stats["errors"].append(("<unknown FLAC>", str(e)))
                _pbar_update(pbar, counts, kind="fail")
                continue

            if ok:
                # A seektable-only change with zero byte difference is not
                # useful for byte metrics, so treat it as skipped.
                if info.startswith("removed seektable") and b_rem == 0 and b_add == 0:
                    stats["skipped_count"] += 1
                    _pbar_skip(pbar, counts)
                    continue

                stats["total_scanned"] += 1
                stats["modified_count"] += 1
                stats["total_bytes_removed"] += b_rem
                stats["total_bytes_added"] += b_add
                _pbar_update(pbar, counts, kind="ok")
            else:
                if info.startswith("skipped"):
                    stats["skipped_count"] += 1
                    _pbar_skip(pbar, counts)
                else:
                    stats["total_scanned"] += 1
                    stats["error_count"] += 1
                    stats["errors"].append((filename, info))
                    _pbar_update(pbar, counts, kind="fail")

        if pbar:
            pbar.close()

    # ---- Conversion -> the configured library codec target ----
    if convert:
        conv_files, refusals = conversion_candidates(target, targets, config,
                                                     conv_scan)
        if refusals:
            _report_refusals(refusals)
            # Refusals are skips, not failures: the file is exactly as it was
            # and nothing about the run went wrong — the policy just forbids
            # the conversion (see conversion_verdict).
            stats["skipped_count"] += len(refusals)

        if conv_files:
            ffmpeg_tool = (tools.get("ffmpeg") or {})
            ffmpeg_exe = ffmpeg_tool.get("ffmpeg_exe")
            ffprobe_exe = ffmpeg_tool.get("ffprobe_exe")
            if not ffmpeg_exe or not ffprobe_exe:
                log(c("WARNING: ffmpeg/ffprobe missing — conversion to "
                      f"{target_label} skipped.", Color.YELLOW))
            else:
                log(f"Converting {len(conv_files)} file(s) to {target_label} "
                    f"(policy: {policy})…")
                conv_args = [
                    (
                        ffmpeg_exe,
                        ffprobe_exe,
                        metaflac_exe,
                        fp,
                        quality,
                        target_version,
                        (config.get("encoder_tags") or {}).get("flac") or {},
                        config,
                    )
                    for fp in conv_files
                ]
                conv_workers = worker_count(config, default=min(4, os.cpu_count() or 1),
                                            items=len(conv_files))
                conv_counts = {"ok": 0, "skip": 0, "fail": 0}
                with ThreadPoolExecutor(max_workers=conv_workers) as ex:
                    futures = [ex.submit(_convert_lossless_source, a) for a in conv_args]
                    pbar2 = _make_pbar(len(futures), "Convert")
                    for future in as_completed(futures):
                        try:
                            filename, ok, info, b_rem, b_add = future.result()
                        except Exception as e:
                            stats["error_count"] += 1
                            stats["errors"].append(("<unknown convert>", str(e)))
                            _pbar_update(pbar2, conv_counts, kind="fail")
                            continue
                        if ok:
                            stats["total_scanned"] += 1
                            stats["modified_count"] += 1
                            stats["total_bytes_removed"] += b_rem
                            stats["total_bytes_added"] += b_add
                            _pbar_update(pbar2, conv_counts, kind="ok")
                        elif info.startswith("skipped"):
                            # A skip is by design, not a failure: the file is
                            # exactly as it was (the target container IS the
                            # file's own, so there is no original to keep).
                            stats["skipped_count"] += 1
                            _pbar_skip(pbar2, conv_counts)
                        else:
                            # A conversion that did not happen and was not a
                            # by-design skip — a failed encode, a duration
                            # that changed, or (the identity check) a
                            # converted file whose audio is not the audio the
                            # source decoded. The original is still exactly
                            # where it was, and the run's own error surface
                            # says which file and why: this used to be
                            # counted as a SKIP and logged nowhere, so a
                            # conversion that quietly did not happen looked
                            # like one that had.
                            stats["total_scanned"] += 1
                            stats["error_count"] += 1
                            stats["errors"].append((filename, info))
                            log(c(f"  ✕ {filename}: {info}", Color.RED))
                            _pbar_update(pbar2, conv_counts, kind="fail")
                    if pbar2:
                        pbar2.close()

                    # Repoint each album's cue sheets ONCE, after the batch:
                    # the conversion renamed the files a rip produced, and a
                    # per-file call re-read every sheet in the folder once per
                    # track — and could still run before the folder's other
                    # tracks had been converted, leaving their entries stale.
                    for folder in sorted({os.path.dirname(p) or "."
                                          for p in conv_files}):
                        try:
                            from .discs import fix_cue_filenames
                            fix_cue_filenames(folder, config=config)
                        except Exception:
                            pass

    return stats

