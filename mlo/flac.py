"""Lossless FLAC re-encoding via the reference flac.exe toolchain."""
import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import DEFAULT_CONFIG
from .containers import (
    _read_flac_tags, _write_flac_tags, _identity_missing, _enabled,
)
from .subproc import run_tool
from .paths import DEPS_DIR
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

# Config `lossless_target_codec` -> (output extension, ffmpeg encoder args).
# FLAC is the default and what the rest of the pipeline is built around
# (metaflac optimizer, ENCODER identity tags, `flac -t` verification); ALAC
# is the compressed-lossless alternative for Apple-centric libraries.
LOSSLESS_TARGETS = {
    "flac": (".flac", ["-c:a", "flac", "-f", "flac"]),
    "alac": (".m4a", ["-c:a", "alac", "-f", "ipod"]),
}


def target_codec(cfg):
    """Normalized `lossless_target_codec` (unknown/missing -> flac)."""
    name = str((cfg or {}).get("lossless_target_codec") or "flac").strip().lower()
    return name if name in LOSSLESS_TARGETS else "flac"


def is_alac(path):
    """True when an .m4a/.mp4 file's audio track is ALAC.

    The extension cannot tell ALAC from AAC (both live in MP4 containers),
    and re-containerizing an AAC file as "lossless" would silently keep the
    lossy audio — mutagen reports the codec ('alac', 'mp4a.40.2')."""
    if os.path.splitext(str(path))[1].lower() not in (".m4a", ".mp4"):
        return False
    try:
        from mutagen.mp4 import MP4
        return str(MP4(path).info.codec or "").lower().startswith("alac")
    except Exception:
        return False

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


def _convert_lossless_source(args):
    """Convert one lossless source file to the configured target codec.

    ffmpeg decodes (handles every extension in LOSSLESS_SOURCE_EXTS, plus
    FLAC when the target is ALAC) and encodes the target codec losslessly;
    tags are copied from ffprobe metadata; the FLAC target additionally gets
    metaflac block stripping and this pipeline's ENCODER identity tags. The
    original is removed only after a verified conversion when
    lossless_remove_original is set.
    Returns (filename, ok, message, bytes_removed, bytes_added).
    """
    (
        ffmpeg_exe, ffprobe_exe, metaflac_exe, filepath,
        flac_level, target_version, enabled, config,
    ) = args
    filename = os.path.basename(filepath)
    codec = target_codec(config)
    out_ext, enc_args = LOSSLESS_TARGETS[codec]
    # Forward slashes: ffmpeg's demuxer probing is cleaner with them (VOB
    # phantom streams) and every Windows tool accepts them.
    filepath = str(filepath).replace("\\", "/")
    dest = os.path.splitext(filepath)[0] + out_ext
    if os.path.exists(dest):
        return (filename, False, f"skipped (same-stem {out_ext} exists)", 0, 0)

    src_dur = 0.0
    raw_tags = {}
    try:
        src_probe = run_tool(
            [ffprobe_exe, "-v", "error", "-print_format", "json",
             "-show_format", filepath],
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
    except Exception:
        src_dur = 0.0

    fd, tmp = tempfile.mkstemp(
        prefix=".conv_", suffix=out_ext, dir=os.path.dirname(filepath) or ".")
    os.close(fd)
    try:
        if codec == "flac":
            enc_args = enc_args + ["-compression_level", str(flac_level)]
        cmd = [ffmpeg_exe, "-y", "-v", "error", "-nostdin", "-i", filepath,
               "-map", "0:a:0"]
        # Embedded art is an attached_pic VIDEO stream, so mapping audio alone
        # dropped it before the original — the only copy of that artwork — was
        # deleted. Copied only when the library is set to keep covers: the "?"
        # keeps an audio-only source working, and copy keeps the picture
        # bit-exact.
        if (bool(config.get("embed_covers", False))
                or bool(config.get("flac_preserve_picture", False))):
            cmd += ["-map", "0:v?", "-c:v", "copy"]
        cmd += enc_args + [tmp]
        try:
            proc = run_tool(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60 * 60)
        except Exception as e:
            return (filename, False, f"ffmpeg failed: {e}", 0, 0)
        if proc.returncode != 0:
            err = "; ".join((proc.stderr or "").strip().splitlines()[-2:])
            return (filename, False, f"convert failed: {err}", 0, 0)

        # Verify duration before touching anything else.
        if src_dur:
            try:
                out_probe = run_tool(
                    [ffprobe_exe, "-v", "error", "-print_format", "json",
                     "-show_format", tmp],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=30,
                )
                out_dur = float((json.loads(out_probe.stdout or "{}")
                                 .get("format") or {}).get("duration") or 0)
                if out_dur and abs(src_dur - out_dur) > max(1.0, 0.005 * src_dur):
                    return (filename, False,
                            f"duration changed ({src_dur:.2f}s -> {out_dur:.2f}s)", 0, 0)
            except Exception:
                pass

        # Copy text tags from the source metadata.
        try:
            from .audio import AudioFile
            out_af = AudioFile(tmp)
            seen = set()
            out_af.defer_save(True)
            for k, v in raw_tags.items():
                name = _FFPROBE_TAG_LOOKUP.get(_ffprobe_key(k))
                if not name or name in seen:
                    continue
                if v is None or not str(v).strip():
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
                    parts = ["PADDING", "CUESHEET", "APPLICATION", "SEEKTABLE"]
                    run_tool([metaflac_exe, "--dont-use-padding", "--remove",
                              "--block-type=" + ",".join(parts), tmp],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             text=True)
                except Exception:
                    pass
            try:
                _write_flac_tags(tmp, flac_level, target_version, enabled)
            except Exception:
                pass

        try:
            out_size = os.path.getsize(tmp)
        except OSError as e:
            return (filename, False, f"cannot stat output: {e}", 0, 0)
        if out_size == 0:
            return (filename, False, "empty output", 0, 0)

        try:
            src_size = os.path.getsize(filepath)
        except OSError:
            src_size = 0
        os.replace(tmp, dest)
        tmp = None
        b_rem = b_add = 0
        if config.get("lossless_remove_original", True):
            try:
                os.remove(filepath)
                b_rem = src_size
            except OSError:
                pass
        b_add = out_size
        src_label = os.path.splitext(filepath)[1].lstrip(".").upper()
        dst_label = out_ext.lstrip(".").upper()
        return (filename, True,
                f"{src_size // 1024} KB {src_label} -> {out_size // 1024} KB {dst_label}",
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
            flac_level,
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
            flac_level,
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
        flac_level,
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
    flac_args = [f"-{flac_level}", "-f", "-V"]
    # --padding is a CAP on the padding block flac.exe writes. flac_no_padding
    # (the default) asks for none: the file is as small as it can be, at the
    # price of a whole-file rewrite for every later tag pass (scripts 8/7/12/
    # 16/10 all write tags). Keeping padding (the flag off) asks for 8192
    # bytes — flac.exe's own default — which those passes swap in place.
    flac_args.append("--padding=0" if flac_no_pad else "--padding=8192")

    if not add_seektables:
        flac_args.append("--no-seektable")

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
            return (filename, False, f"flac.exe failed: {err}", 0, 0)

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
            _write_flac_tags(temp_path, flac_level, target_version, enabled)
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


def _lossless_conversion_sources(target, targets, out_ext, files=None):
    """Files whose lossless audio should be re-containerized into the target.

    LOSSLESS_SOURCE_EXTS (uncompressed or externally compressed lossless)
    always qualify; ALAC-in-MP4 qualifies for a FLAC target — `.m4a` is
    usually AAC, so those are probed rather than trusted by extension; and
    FLACs qualify when the target is another codec (the setting means the
    library ends up in that codec). Files already in the target are left
    alone.

    *files* is a candidate list the caller already walked (the FLAC pass
    scans the same tree for its own extensions, so a FLAC-target run hands
    its union list over instead of walking the library a second time).
    """
    if files is None:
        exts = LOSSLESS_SOURCE_EXTS + ((".m4a", ".mp4") if out_ext == ".flac" else (".flac",))
        files = (sorted(_walk_files(target, exts)) if targets is None
                 else sorted(_collect_targets(targets, exts)))
    out = []
    for p in files:
        low = str(p).lower()
        if low.endswith(out_ext):
            continue
        if low.endswith((".m4a", ".mp4")) and not is_alac(p):
            continue  # AAC in MP4 is lossy — never rewritten as "lossless"
        out.append(p)
    return out


def convert_album_lossless(album_dir, cfg):
    """Convert one album's lossless sources to the configured target codec.

    Scoped run of the script-3 conversion: only this folder is scanned, so an
    import never rewrites the rest of the library. Returns the optimizer stats
    (empty stats when the folder holds nothing to convert).
    """
    scoped = dict(cfg or {})
    scoped["targets"] = [str(album_dir)]
    scoped["force_reencode_flac"] = False
    return run_optimize_flacs(scoped)


def run_optimize_flacs(config):
    # `.get` with the shipped default, like the rest of this module: a partial
    # cfg (a test, the album-scoped conversion helper) must not KeyError here.
    flac_level = config.get("flac_level", DEFAULT_CONFIG["flac_level"])
    add_seektables = config.get("add_seektables", DEFAULT_CONFIG["add_seektables"])
    force = config.get("force_reencode_flac", False)
    stats = new_stats()

    tools = detect_all_tools()
    flac_tool = tools.get("flac")

    # With a non-FLAC target the existing FLACs are re-containerized by the
    # conversion step below, so the flac.exe optimizer is never needed and
    # re-encoding them first would be pure wasted work.
    codec = target_codec(config)
    out_ext = LOSSLESS_TARGETS[codec][0]

    if not flac_tool and codec == "flac":
        log(c("ERROR: Could not auto-detect flac.exe in .dependencies folder.", Color.RED))
        log(f"Expected a folder like: {os.path.join(DEPS_DIR, 'flac v1.5.0')}")
        return stats

    flac_exe = (flac_tool or {}).get("flac_exe")
    metaflac_exe = (flac_tool or {}).get("metaflac_exe")
    target_version = (flac_tool or {}).get("version") or ""

    print_header("FLAC Optimizer")
    log(f"lossless target codec: {out_ext.lstrip('.').upper()}")
    if codec == "flac":
        strip_msg = ("PICTURE, " if not (config or {}).get("embed_covers", False)
                     and not (config or {}).get("flac_preserve_picture", False) else "")
        strip_msg += "CUESHEET, APPLICATION"
        if config.get("flac_no_padding", True):
            strip_msg += ", PADDING"
        if not add_seektables:
            strip_msg += ", SEEKTABLE"
        log(
            f"level=-{flac_level} · seektables={'on' if add_seektables else 'off'} · "
            f"encoder={target_version} · force={'on' if force else 'off'} · "
            f"padding={'none' if config.get('flac_no_padding', True) else '8 KB'} · "
            f"strip={strip_msg}"
        )

    target = os.path.abspath(config["music_folder"] or os.getcwd())

    if not os.path.isdir(target):
        log(c(f"ERROR: TARGET_DIR does not exist: {target}", Color.RED))
        return stats

    log(f"target: {target}")
    log(f"flac.exe: {flac_exe} · metaflac.exe: {metaflac_exe or '(not found)'}")

    targets = config.get("targets")
    flac_files = []
    conv_scan = None
    if codec == "flac":
        # One walk for both passes: the conversion step below needs
        # LOSSLESS_SOURCE_EXTS (+ ALAC-in-MP4) and used to walk the whole
        # library again for them after this one had finished. The union is
        # collected here and split, which is the same file set with one
        # directory scan.
        walk_exts = (".flac",) + LOSSLESS_SOURCE_EXTS + (".m4a", ".mp4")
        walked = (sorted(_walk_files(target, walk_exts)) if targets is None
                  else sorted(_collect_targets(targets, walk_exts)))
        flac_files = sorted(
            f for f in walked
            if f.lower().endswith(".flac")
            and not f.lower().endswith(".opttmp.flac")
        )
        conv_scan = walked

        # Deduplicate (Select All checks album + tracks -> duplicates) + normcase for Windows
        if len(flac_files) != len(set(os.path.normcase(p) for p in flac_files)):
            log(c(f"WARNING: flac_files has duplicates: {len(flac_files)} vs {len(set(os.path.normcase(p) for p in flac_files))} unique", Color.YELLOW))
            seen = {}
            for p in flac_files:
                seen[os.path.normcase(p)] = p
            flac_files = sorted(seen.values())

        if not flac_files:
            log("No FLAC files found.")

    workers = worker_count(config, default=os.cpu_count() or 1,
                          items=len(flac_files))
    counts = {"ok": 0, "skip": 0, "fail": 0}

    args_list = [
        (
            flac_exe,
            metaflac_exe,
            fp,
            flac_level,
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

    # ---- Lossless source conversion -> configured target codec ----
    if config.get("optimize_convert_lossless", True):
        conv_files = _lossless_conversion_sources(target, targets, out_ext,
                                                  conv_scan)
        seen_conv = {}
        for p in conv_files:
            seen_conv.setdefault(os.path.normcase(p), p)
        conv_files = sorted(seen_conv.values())

        if conv_files:
            ffmpeg_tool = (tools.get("ffmpeg") or {})
            ffmpeg_exe = ffmpeg_tool.get("ffmpeg_exe")
            ffprobe_exe = ffmpeg_tool.get("ffprobe_exe")
            if not ffmpeg_exe or not ffprobe_exe:
                log(c("WARNING: ffmpeg/ffprobe missing — lossless source conversion skipped.", Color.YELLOW))
            else:
                log(f"Converting {len(conv_files)} lossless source file(s) to "
                    f"{out_ext.lstrip('.').upper()}…")
                conv_args = [
                    (
                        ffmpeg_exe,
                        ffprobe_exe,
                        metaflac_exe,
                        fp,
                        flac_level,
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
                        else:
                            stats["skipped_count"] += 1
                            _pbar_skip(pbar2, conv_counts)
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

