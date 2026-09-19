"""Container-level metadata readers/writers for FLAC, JXL, JPEG and PNG.

Implements the ENCODER marker tag standard (see package docstring).
"""
import os
import re
import tempfile
import zlib

from .config import should_write_audio_tag
from .deps import FLAC


def _atomic_write(target_path, data: bytes):
    """Write data atomically via temp + fsync + os.replace."""
    dir_name = os.path.dirname(os.path.abspath(target_path)) or "."
    fd = None
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=dir_name)
        with os.fdopen(fd, "wb") as f:
            fd = None
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, target_path)
        tmp = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass

ENCODER_KEYS = ("ENCODER_PROGRAM", "ENCODER_QUALITY", "ENCODER_VERSION")


def _enabled(enabled, key):
    """Whether `key` should be written, per the encoder_tags config dict."""
    if not enabled:
        return True
    return bool(enabled.get(key, True))


def _build_xmp_packet(quality, version, program, enabled=None):
    lines = ["<?xpacket begin='\ufeff' id='W5M0MpCehiHzreSzNTczkc9d'?>\n",
             "<x:xmpmeta xmlns:x='adobe:ns:meta/'>\n",
             "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>\n",
             "<rdf:Description rdf:about=''\n"
             "  xmlns:enc='http://ns.example.com/encoder/1.0/'>\n"]
    if _enabled(enabled, "ENCODER_PROGRAM"):
        lines.append(f"<enc:ENCODER_PROGRAM>{program}</enc:ENCODER_PROGRAM>\n")
    if _enabled(enabled, "ENCODER_QUALITY"):
        lines.append(f"<enc:ENCODER_QUALITY>{quality}</enc:ENCODER_QUALITY>\n")
    if _enabled(enabled, "ENCODER_VERSION"):
        lines.append(f"<enc:ENCODER_VERSION>{version}</enc:ENCODER_VERSION>\n")
    lines.append("</rdf:Description>\n</rdf:RDF>\n</x:xmpmeta>\n"
                 "<?xpacket end='w'?>")
    return "".join(lines)


def _parse_xmp_tags(xmp_str):
    if not xmp_str:
        return None, None, None

    q = re.search(
        r"<enc:ENCODER_QUALITY>(.*?)</enc:ENCODER_QUALITY>",
        xmp_str,
        re.IGNORECASE | re.DOTALL,
    )
    v = re.search(
        r"<enc:ENCODER_VERSION>(.*?)</enc:ENCODER_VERSION>",
        xmp_str,
        re.IGNORECASE | re.DOTALL,
    )
    p = re.search(
        r"<enc:ENCODER_PROGRAM>(.*?)</enc:ENCODER_PROGRAM>",
        xmp_str,
        re.IGNORECASE | re.DOTALL,
    )
    return (q.group(1) if q else None, v.group(1) if v else None, p.group(1) if p else None)


def _read_flac_tags(filepath):
    """Return (quality, version, program) from the ENCODER marker tags."""
    try:
        audio = FLAC(filepath)
        q = audio.get("ENCODER_QUALITY", [None])[0]
        v = audio.get("ENCODER_VERSION", [None])[0]
        p = audio.get("ENCODER_PROGRAM", [None])[0]
        return q, v, p
    except Exception:
        return None, None, None


def _write_flac_tags(filepath, quality, version, enabled=None):
    audio = FLAC(filepath)
    if audio.tags is None:
        try:
            audio.add_tags()
        except Exception:
            pass
    if _enabled(enabled, "ENCODER_PROGRAM"):
        audio["ENCODER_PROGRAM"] = "FLAC reference encoder"
    if _enabled(enabled, "ENCODER_QUALITY"):
        audio["ENCODER_QUALITY"] = str(quality)
    if _enabled(enabled, "ENCODER_VERSION"):
        audio["ENCODER_VERSION"] = str(version)
    audio.save()


# Whitelist of Vorbis comment keys to keep when cleaning FLAC tags.
# Includes all semantic tags from TAG_MAP plus common aliases and the
# ENCODER family. Everything else is considered unused metadata and removed.
KEEP_VORBIS_KEYS = {
    "TITLE", "ALBUM", "ARTIST", "ALBUMARTIST", "TRACKNUMBER", "TRACKTOTAL",
    "DISCNUMBER", "DISCTOTAL", "DATE", "YEAR", "ORIGINALDATE", "ORIGINALYEAR",
    "GENRE", "COMPOSER", "LYRICIST", "COMMENT", "LYRICS", "UNSYNCEDLYRICS",
    "BPM", "COPYRIGHT", "MEDIA", "SOURCE", "INSTRUMENTAL", "ITUNESADVISORY",
    "ALBUMITUNESADVISORY", "REPLAYGAIN_TRACK_GAIN", "REPLAYGAIN_TRACK_PEAK",
    "REPLAYGAIN_ALBUM_GAIN", "REPLAYGAIN_ALBUM_PEAK", "DYNAMIC RANGE",
    "ALBUM DYNAMIC RANGE", "AUDIT", "LOG_GRADE", "AUDIO_MD5", "INTEGRITY",
    "LOG_CRC", "ENCODER", "ENCODER_PROGRAM", "ENCODER_QUALITY", "ENCODER_VERSION",
    "ENCODEDBY", "PERFORMER", "ALBUMARTIST", "ARTISTSORT", "ALBUMARTISTSORT",
    "TITLESORT", "COMPOSERSORT", "WORK", "MOVEMENT", "PART", "CONDUCTOR",
}


def _clean_flac_tags(filepath, config=None, enabled=None):
    """Conditionally clean Vorbis comments — now conservative by default.

    The app previously removed *any* tag not in KEEP_VORBIS_KEYS, which broke
    Picard recognition (MusicBrainz IDs etc. were deleted). New behavior:
    * Never remove tags except for the two lyric variants.
    * ``UNSYNCEDLYRICS`` is legacy (this app writes LYRICS), but
      ``AudioFile.get_lyrics`` still reads it and the grader grades what that
      returns — stripping it from a file whose only lyrics live there failed
      the album's lyrics grade. It follows the LYRICS family gate and the
      lyrics_format rule below instead of being removed unconditionally.
    * ``LYRICS`` is removed only when ``lyrics_format`` is ``LRC`` (embedded
      lyrics are not wanted) — otherwise it is kept.
    * ``ENCODER_PROGRAM`` is removed when it is disabled per-format via
      ``encoder_tags`` (off by default since v1.4.2).

    Returns True if any tags were removed.
    """
    try:
        audio = FLAC(filepath)
        if audio.tags is None:
            return False
        to_remove = []
        # Embedded lyrics are dropped only where this file may not carry
        # them: the LRC-sidecar-only setting, or the LYRICS family turned off
        # for this filetype. A container whose lyrics the grader expects must
        # keep BOTH spellings (a partial cfg with no lyrics_format keeps
        # them, like the app does when the setting is at its default).
        want_embedded = True
        if config is not None:
            try:
                fmt = str(config.get("lyrics_format", "EMBEDDED")).upper()
            except Exception:
                fmt = "EMBEDDED"
            want_embedded = (fmt != "LRC" and should_write_audio_tag(
                config, "LYRICS", filepath=filepath))
        if not want_embedded:
            for k in list(audio.tags.keys()):
                if str(k).lower() in ("lyrics", "unsyncedlyrics"):
                    to_remove.append(k)
        # Remove ENCODER_PROGRAM when disabled per-format
        if enabled is not None and not _enabled(enabled, "ENCODER_PROGRAM"):
            for k in list(audio.tags.keys()):
                if str(k).lower() == "encoder_program" and k not in to_remove:
                    to_remove.append(k)
        # EXCESS TAGS — everything outside the shared vocabulary the grader
        # fails a track for ("Excess tags") and Format All's canonical pass
        # strips. This path rewrites the file anyway, so leaving junk a vendor
        # or ripper wrote behind would mean the very next grade fails a file
        # the optimizer just touched. One predicate for both halves
        # (mlo.grader.tag_key_allowed: TAG_MAP names in every container
        # spelling, the encoder identity tags, beets/Picard's own spellings,
        # the app's AUDIOAUDITOR_OVERRIDE and the language-suffixed lyrics
        # transforms), so a strip can never delete a tag the grade requires or
        # keep one it flags. Off with `strip_unknown_tags` — the same switch
        # that silences the grade — and a partial cfg strips like the app does.
        if config is None or config.get("strip_unknown_tags", True):
            try:
                from .grader import tag_key_allowed
            except Exception:
                tag_key_allowed = None
            if tag_key_allowed is not None:
                for k in list(audio.tags.keys()):
                    if k in to_remove:
                        continue
                    if not tag_key_allowed(str(k)):
                        to_remove.append(k)
        if not to_remove:
            return False
        for k in to_remove:
            try:
                del audio.tags[k]
            except Exception:
                pass
        audio.save()
        return True
    except Exception:
        return False


def _encoder_dict(program, quality, version, enabled=None):
    tags = {}
    if _enabled(enabled, "ENCODER_PROGRAM"):
        tags["ENCODER_PROGRAM"] = program
    if _enabled(enabled, "ENCODER_QUALITY"):
        tags["ENCODER_QUALITY"] = str(quality)
    if _enabled(enabled, "ENCODER_VERSION"):
        tags["ENCODER_VERSION"] = str(version)
    return tags


def _identity_missing(enabled, q, v, p=None):
    """Whether any enabled ENCODER identity tag is missing.

    Used by skip-checks: a file can only be skipped when every identity
    tag that is actually written exists. If no identity tag is enabled,
    files can never be identified and always re-encode. Since v1.4.2
    ``ENCODER_PROGRAM`` is off by default, so it only gates skipping when
    the user has explicitly enabled it per format.
    """
    need_q = _enabled(enabled, "ENCODER_QUALITY")
    need_v = _enabled(enabled, "ENCODER_VERSION")
    need_p = _enabled(enabled, "ENCODER_PROGRAM")
    if not need_q and not need_v and not need_p:
        return True
    if need_q and (q is None or str(q).strip() == ""):
        return True
    if need_v and (v is None or str(v).strip() == ""):
        return True
    if need_p and (p is None or str(p).strip() == ""):
        return True
    return False


def _quality_meets(enabled, q, threshold):
    """Whether stored *q* meets *threshold*, gated by ENCODER_QUALITY being enabled.

    A disabled identity tag is never written, so its stored value is None on
    every file. Comparing that numerically would re-encode forever, so a
    disabled tag means "skip the quality comparison", not "fail" it.
    """
    if not _enabled(enabled, "ENCODER_QUALITY"):
        return True
    try:
        return int(q) >= int(threshold)
    except (TypeError, ValueError):
        return False


JXL_SIG = b"\x00\x00\x00\x0cJXL \r\n\x87\n"


def _jxl_iter_boxes(data):
    i = 0
    n = len(data)

    while i + 8 <= n:
        size = int.from_bytes(data[i:i + 4], "big")
        btype = data[i + 4:i + 8]
        header = 8

        if size == 1:
            if i + 16 > n:
                break
            size = int.from_bytes(data[i + 8:i + 16], "big")
            header = 16
        elif size == 0:
            size = n - i

        payload_start = i + header
        payload_end = i + size
        if payload_end > n:
            # Truncated box: stop iteration instead of yielding partial payload
            break

        yield btype, data[payload_start:payload_end], i, size
        i += size


def _read_jxl_tags(jxl_path):
    try:
        with open(jxl_path, "rb") as f:
            data = f.read()
    except Exception:
        return None, None, None

    if not data.startswith(JXL_SIG):
        return None, None, None

    for btype, payload, _, _ in _jxl_iter_boxes(data):
        if btype == b"xml ":
            try:
                xmp = payload.decode("utf-8", errors="replace")
                q, v, p = _parse_xmp_tags(xmp)
                if q is not None or v is not None or p is not None:
                    return q, v, p
            except Exception:
                continue

    return None, None, None


def _write_jxl_tags(jxl_path, quality, version, enabled=None):
    with open(jxl_path, "rb") as f:
        data = f.read()

    if not data.startswith(JXL_SIG):
        wrapped = bytearray(JXL_SIG)

        ftyp_payload = b"jxl \x00\x00\x00\x04jxl "
        ftyp_size = 8 + len(ftyp_payload)
        wrapped.extend(ftyp_size.to_bytes(4, "big"))
        wrapped.extend(b"ftyp")
        wrapped.extend(ftyp_payload)

        jxlc_size = 8 + len(data)
        wrapped.extend(jxlc_size.to_bytes(4, "big"))
        wrapped.extend(b"jxlc")
        wrapped.extend(data)

        data = bytes(wrapped)

    xmp = _build_xmp_packet(quality, version, "libjxl/cjxl", enabled).encode("utf-8")
    new_box_size = 8 + len(xmp)
    new_box = new_box_size.to_bytes(4, "big") + b"xml " + xmp

    out = bytearray()
    i = 0
    n = len(data)

    while i + 8 <= n:
        size = int.from_bytes(data[i:i + 4], "big")
        btype = data[i + 4:i + 8]

        if size == 1:
            if i + 16 > n:
                break
            size = int.from_bytes(data[i + 8:i + 16], "big")
        elif size == 0:
            size = n - i

        if btype == b"xml ":
            i += size
            continue

        out.extend(data[i:i + size])
        i += size

    out.extend(new_box)

    _atomic_write(jxl_path, bytes(out))


XMP_NS = b"http://ns.adobe.com/xap/1.0/\x00"


def _read_jpeg_xmp_tags(jpeg_path):
    try:
        with open(jpeg_path, "rb") as f:
            data = f.read()
    except Exception:
        return None, None, None

    if not data.startswith(b"\xff\xd8"):
        return None, None, None

    i = 2
    n = len(data)

    while i < n:
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            break

        marker = data[i]
        i += 1

        if marker == 0xDA or marker == 0xD9:
            break
        if 0xD0 <= marker <= 0xD7:
            continue
        if i + 1 >= n:
            break

        length = (data[i] << 8) | data[i + 1]
        payload = data[i + 2:i + length]
        i += length

        if marker == 0xE1 and payload.startswith(XMP_NS):
            xmp = payload[len(XMP_NS):].decode("utf-8", errors="replace")
            q, v, p = _parse_xmp_tags(xmp)
            if q is not None or v is not None or p is not None:
                return q, v, p

    return None, None, None


def _insert_jpeg_xmp(jpeg_path, quality, version, program, enabled=None):
    with open(jpeg_path, "rb") as f:
        data = f.read()

    if not data.startswith(b"\xff\xd8"):
        return False

    out = bytearray(b"\xff\xd8")
    i = 2
    n = len(data)

    while i < n:
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            break

        marker = data[i]
        i += 1

        if marker == 0xDA:
            if i + 1 < n:
                length = (data[i] << 8) | data[i + 1]
                out.extend(b"\xff\xda")
                out.extend(data[i:i + length])
                i += length
                out.extend(data[i:])
            break

        if marker == 0xD9:
            out.extend(b"\xff\xd9")
            break

        if 0xD0 <= marker <= 0xD7:
            out.extend(b"\xff")
            out.extend(bytes([marker]))
            continue

        if i + 1 >= n:
            break

        length = (data[i] << 8) | data[i + 1]
        payload = data[i + 2:i + length]
        i += length

        if marker == 0xE1 and payload.startswith(XMP_NS):
            continue

        out.extend(b"\xff")
        out.extend(bytes([marker]))
        out.extend(data[i - length:i])

    xmp = _build_xmp_packet(quality, version, program, enabled).encode("utf-8")
    app1_payload = XMP_NS + xmp
    app1_len = 2 + len(app1_payload)
    app1 = b"\xff\xe1" + app1_len.to_bytes(2, "big") + app1_payload

    new_data = bytes(out[:2]) + app1 + bytes(out[2:])

    _atomic_write(jpeg_path, new_data)

    return True


PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _iter_png_chunks(data):
    pos = 8
    n = len(data)

    while pos + 8 <= n:
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        total = 12 + length
        payload = data[pos + 8:pos + 8 + length]

        yield ctype, payload, pos, total

        if ctype == b"IEND":
            return

        pos += total


def _read_png_text(png_path):
    try:
        with open(png_path, "rb") as f:
            data = f.read()
    except Exception:
        return {}

    if not data.startswith(PNG_SIG):
        return {}

    result = {}

    for ctype, payload, _, _ in _iter_png_chunks(data):
        if ctype == b"tEXt":
            null = payload.find(b"\x00")
            if null != -1:
                key = payload[:null].decode("latin-1", errors="replace")
                val = payload[null + 1:].decode("latin-1", errors="replace")
                result[key] = val

    return result


# The PNG text keys THIS app writes itself (see _inject_png_text and
# _encoder_dict). Only these are stripped before fresh markers are injected —
# the old pass removed every tEXt/iTXt/zTXt/eXIf/tIME chunk, which wiped a
# caption, a colour-managed EXIF block and a timestamp some other tool wrote,
# on every optimization run, and no setting asked for that.
_PNG_OWN_TEXT_KEYS = {"ENCODER", "ENCODER_PROGRAM", "ENCODER_QUALITY",
                      "ENCODER_VERSION"}


def _png_text_keyword(ctype, payload):
    """The keyword of a PNG text chunk (uppercased), or None for any other
    chunk. tEXt/zTXt carry "keyword\0…"; iTXt carries
    "keyword\0compression flag\0method\0language\0translated\0text"."""
    if ctype not in (b"tEXt", b"zTXt", b"iTXt"):
        return None
    null = payload.find(b"\x00")
    if null <= 0:
        return None
    try:
        return payload[:null].decode("latin-1").upper()
    except Exception:
        return None


def _strip_png_encoder_tags(png_path):
    """Drop the encoder markers this app wrote, keep everyone else's chunks."""
    with open(png_path, "rb") as f:
        data = f.read()

    if not data.startswith(PNG_SIG):
        return False

    out = bytearray(PNG_SIG)
    removed = False

    for ctype, payload, _, _ in _iter_png_chunks(data):
        if _png_text_keyword(ctype, payload) in _PNG_OWN_TEXT_KEYS:
            removed = True
            continue

        out.extend(len(payload).to_bytes(4, "big"))
        out.extend(ctype)
        out.extend(payload)

        crc = zlib.crc32(ctype + payload) & 0xFFFFFFFF
        out.extend(crc.to_bytes(4, "big"))

    if not removed:
        return False  # nothing of ours in there: do not rewrite the file

    _atomic_write(png_path, bytes(out))

    return True


def _inject_png_text(png_path, tags_dict):
    tags_dict = {k: v for k, v in tags_dict.items() if v is not None}
    if not tags_dict:
        return False

    with open(png_path, "rb") as f:
        data = f.read()

    if not data.startswith(PNG_SIG):
        return False

    out = bytearray(PNG_SIG)
    inserted = False

    for ctype, payload, _, _ in _iter_png_chunks(data):
        out.extend(len(payload).to_bytes(4, "big"))
        out.extend(ctype)
        out.extend(payload)

        crc = zlib.crc32(ctype + payload) & 0xFFFFFFFF
        out.extend(crc.to_bytes(4, "big"))

        if ctype == b"IHDR" and not inserted:
            for k, v in tags_dict.items():
                # Use utf-8 with latin-1 fallback; PNG tEXt is latin-1 but we must not crash on unicode
                try:
                    k_b = k.encode("latin-1")
                except UnicodeEncodeError:
                    k_b = k.encode("utf-8", errors="replace")
                try:
                    v_b = str(v).encode("latin-1")
                except UnicodeEncodeError:
                    v_b = str(v).encode("utf-8", errors="replace")
                t_payload = k_b + b"\x00" + v_b

                out.extend(len(t_payload).to_bytes(4, "big"))
                out.extend(b"tEXt")
                out.extend(t_payload)

                crc = zlib.crc32(b"tEXt" + t_payload) & 0xFFFFFFFF
                out.extend(crc.to_bytes(4, "big"))

            inserted = True

    _atomic_write(png_path, bytes(out))

    return True

