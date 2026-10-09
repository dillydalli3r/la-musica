"""Cover art: serving, picking, writing, and the album's own artwork.

GET /api/cover serves the album's art (or its dominant colour for UI tinting),
GET /api/art proxies a remote image, POST /api/cover stores an upload and
/api/cover/fromurl stores the finder's pick; /api/cover/search|sources ask
``server.integrations``' meta-search, /api/cover/info reads the file's own
dimensions, and /api/cover/clear drops per-track mappings.

The bytes pipeline lives here too — magic-byte sniffing, compression to the
configured target and the atomic write (``_write_cover_bytes``) — plus the
debounced background pass that re-derives an album's cover after a manual
write (see the banner below). ``server.imports`` and the artist-image step in
``server.main`` write covers through these same helpers, so there is one
writer for the library's art.
"""
import asyncio
import os
import tempfile
import threading
import time
import traceback
from typing import List, Optional

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel

from mlo import cover_choice
from mlo.atomic import replace_locked
from mlo.paths import clear_track_covers, set_track_covers
from server import artcache, integrations as intg, job_locks, mbresolve, tagcache
from server.api_common import _guard_folder, re_safe_filename, load_config
from server.api_run import _invalidate_run

router = APIRouter(tags=["cover"])


@router.get("/api/cover")
def get_cover(request: Request, album: str = Query(...), file: Optional[str] = Query(None),
              color: int = Query(0), staged: bool = Query(False), w: int = Query(0)):
    """Serve an album's cover art, cached with ETag; ?color=1 returns the
    dominant color instead of the image bytes (UI tinting). Accepts an
    "mb:<release MBID>" album reference, and `staged=1` for the import
    wizard's album — the folder the library does not list yet, which the
    preview <img> therefore has to ask for the same way every other wizard
    call does (without it the folder guard refuses it, and the preview stays
    empty however well the cover was written).

    **Without `w`** the response tells caches to REVALIDATE, and the ETag is
    the byte hash of the file: a cover is replaced in place (cover.jpg stays
    cover.jpg), so "fresh for an hour" would keep serving the previous image
    long after the write. An unchanged cover still costs only a 304 — the bytes
    come from the mtime+size-keyed cache below, never a stale entry.

    **With `w`** the bytes are the file shrunk to that width
    (`artcache.cover_thumb`, cached on disk under the file's own stat), which
    is what a surface that draws a 74 px bar thumb or a 448 px fullscreen
    picture actually wants: the master is 1200-3000 px, so asking for it put
    megabytes and a full-size decode on the play path. A sized answer may be
    cached by the browser for a few minutes (the URL a cover write produces is
    a different one — see `api.coverUrl`'s `v` — so an in-app replacement is
    still a fresh fetch), and its ETag is the thumbnail's own bytes, so a
    revalidation is a 304 whichever entry the client holds.

    **How the art is resolved** (`file` is a hint, never a demand): the named
    file while it exists, else the album's own cover by the standard names
    (`tagcache.cover_path` — a payload that predates a re-encode's rename
    still resolves), else the embedded picture of the track that file named,
    or of the album's first track (`tagcache.embedded_cover_bytes`; there is no
    file to shrink there, so a `w` request serves that picture whole). The 404
    is what is left when the album has no art in either place.
    """
    alb = os.path.normpath(mbresolve.resolve_album(album) or album)
    if not os.path.isdir(alb):
        raise HTTPException(404, "album not found")
    _guard_folder(alb, staged, "album")
    if color:
        c = tagcache.cover_color(alb, file)
        if c is None:
            raise HTTPException(404, "no cover")
        return {"color": c, "album": alb.replace("\\", "/")}
    # A sized request must not even READ the master: `cover_thumb` either
    # answers from its own cache or decodes the file itself, and the master's
    # bytes (a 1.5 MB file, plus the LRU slot they take) stay out of the path
    # that runs on every play.
    p = tagcache.cover_path(alb, file)
    thumb = artcache.cover_thumb(p, w) if (w and p) else None
    if thumb:
        data, ctype, etag = thumb
        headers = {"Cache-Control": f"private, max-age={artcache.THUMB_MAX_AGE}",
                   "Accept-Ranges": "bytes"}
    else:
        data, ctype, etag = tagcache.cover_bytes(alb, file)
        if data is None:
            # No cover FILE at all — but an album's art can live inside its
            # audio (`embed_covers`, or a folder the import's cover step has
            # not reached yet). Serve the track's own picture rather than
            # nothing: the player has no other way to draw the album, and a
            # blank slot is the fact the owner reported. Only reached when
            # `cover_bytes` found no file, so a sidecar is never masked.
            data, ctype, etag = tagcache.embedded_cover_bytes(alb, file)
        headers = {"Cache-Control": "no-cache", "Accept-Ranges": "bytes"}
    if data is None:
        raise HTTPException(404, "no cover")
    from fastapi.responses import Response
    if etag:
        headers["ETag"] = f'"{etag}"'
        inm = request.headers.get("if-none-match")
        if inm and inm.strip('"') == etag:
            return Response(content=b"", status_code=304, headers=headers)
    return Response(content=data, media_type=ctype, headers=headers)


@router.get("/api/art")
async def proxy_art(url: str = Query(...), artist: str = Query(""),
                    album: str = Query(""), rg: str = Query("")):
    """Serve provider artwork through the app instead of hotlinking it.

    The UI is handed CDN URLs (Deezer, iTunes, the Cover Art Archive, …) by
    every recommendation row, cover-search result and artist-image candidate.
    This route fetches one of those — cached for a month under
    `<music>/.mlo/data/art_cache`, and fallen back to a provider that answers
    when the CDN refuses us (`server/artcache.py`) — so a shelf renders the
    same whether or not the network can reach the provider directly.

    `artist`/`album`/`rg` (a release-group MBID) are the identity the fallback
    is asked about; without them only the original URL is tried. Only
    allowlisted provider hosts are accepted, and a total failure answers 404,
    which is what keeps the UI's placeholder.
    """
    data, ctype, source = await asyncio.to_thread(
        artcache.fetch_art, url, artist=artist.strip(), album=album.strip(),
        release_group_mbid=rg.strip())
    if not data:
        raise HTTPException(404, "no artwork")
    return Response(content=data, media_type=ctype or "image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400",
                             "X-Art-Source": source or "url"})


def _cover_url_bytes(url, artist="", substitute=True):
    """Cover bytes for a caller-supplied URL.

    A provider URL goes through the art cache — its per-host headers are what
    gets past a CDN that refuses the app, its cache is what makes a re-pick
    instant, and `artist` is the identity the fallback is asked about when the
    CDN refuses everybody on this network (the artist-image path, where the
    stand-in is the same artist's picture from another provider). Any other
    public image URL is fetched directly, the way it always was.

    `substitute=False` is what a cover WRITE asks for (`/api/cover/fromurl`,
    the import chain's cover step): the bytes must be the picture the caller
    named — the one the user picked, or the candidate the policy chose for the
    album. Another provider's image standing in for it would be written to the
    library as if it had been picked, which is exactly how a wrong cover lands
    on an album; the caller gets an error instead.
    """
    if artcache.allowed(url):
        data, ctype, _source = artcache.fetch_art(
            url, artist=artist, substitute=substitute)
        if not data:
            raise ValueError("that image could not be fetched — nothing was "
                             "written in its place")
        return data, ctype
    return intg.fetch_image_bytes(url)


@router.post("/api/cover")
@job_locks.holds(lambda album, **_: [album], kind="cover", label="Cover write")
async def upload_cover(album: str = Query(...), file: UploadFile = File(...),
                       track: Optional[str] = Query(None),
                       tracks: Optional[str] = Query(None),
                       staged: bool = Query(False)):
    """Upload cover art.

    No `track`/`tracks` replaces the album's cover.*; `track=<audio filename>`
    writes a per-track sidecar named after the track stem (e.g.
    '01 - Song.jpg'); `tracks=a.flac,b.flac` writes ONE image — the sidecar of
    the first selected track — and maps every selected track to it, so tracks
    can share art without the file being duplicated.
    """
    alb = os.path.normpath(album)
    if not os.path.isdir(alb):
        raise HTTPException(404, "album not found")
    _guard_folder(alb, staged, "album")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".jxl", ".webp", ".bmp"):
        ext = ".jpg"
    stem, selected = _cover_write_target(alb, track, tracks)
    data = await file.read()
    if not _is_image_bytes(data):
        raise HTTPException(400, "uploaded file is not a readable image")
    res = _write_cover_bytes(alb, stem, ext, data)
    if selected:
        set_track_covers(alb, selected, os.path.basename(res["path"]))
    if not staged:
        # A library album the user just re-covered: process the new image now
        # (see _schedule_cover_process). A staged write is the import wizard's
        # folder — the chain finishing that import carries script 5 itself.
        _schedule_cover_process(alb)
    return res


def _cover_stem(track):
    """Sidecar stem for a track filename: '01 - Song.flac' -> '01 - Song'."""
    tstem = os.path.splitext(os.path.basename(track or ""))[0]
    return re_safe_filename(tstem) or "cover"


def _resolve_selected_tracks(alb, tracks):
    """(resolved, unknown) album filenames for a comma-separated selection.

    Names are matched case-insensitively against the album folder so the
    caller can reject an unknown one instead of recording a map entry that
    names a file which is not there.
    """
    names = [os.path.basename(t.strip()) for t in str(tracks or "").split(",") if t.strip()]
    try:
        listing = {f.lower(): f for f in os.listdir(alb)}
    except OSError:
        listing = {}
    resolved, unknown = [], []
    for n in names:
        real = listing.get(n.lower())
        if real and os.path.isfile(os.path.join(alb, real)):
            resolved.append(real)
        else:
            unknown.append(n)
    return resolved, unknown


def _cover_write_target(alb, track, tracks):
    """(stem, selected tracks) for a cover write: a multi-track selection
    wins, then a single `track`, then the album cover."""
    if str(tracks or "").strip():
        selected, unknown = _resolve_selected_tracks(alb, tracks)
        if unknown:
            raise HTTPException(400, f"unknown track(s) in album: {', '.join(unknown)}")
        if not selected:
            raise HTTPException(400, "no tracks selected")
        return _cover_stem(selected[0]), selected
    if track:
        return _cover_stem(track), []
    return "cover", []


def _image_magic_ext(data):
    """Image extension from magic numbers, or None when the bytes carry no
    recognised image container signature."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"\xff\x0a" or data[:12] == b"\x00\x00\x00\x0cJXL ":
        return ".jxl"
    if data[:2] == b"BM":
        return ".bmp"
    return None


def _is_image_bytes(data):
    """Whether upload bytes are an image.

    Pillow reading it counts, and so does a recognised image container magic:
    JXL/HEIC/AVIF need plugins Pillow may not carry, and such a file is still
    the cover the user meant to upload.
    """
    if not data:
        return False
    if _image_magic_ext(data):
        return True
    try:
        from PIL import Image
    except Exception:
        return True
    import io
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.size
        return True
    except Exception:
        return False


def _compress_cover_bytes(data: bytes, ext: str):
    """Downscale / crop / re-encode freshly added cover art.

    Returns (bytes, ext, info). A cover from the finder arrives at full CDN
    resolution (3000px+ and several MB) and an upload at whatever the user
    happened to have, while the library's own convention is
    `cover_target_size`, cropped square, JPEG at `cover_jpeg_quality`. Doing
    that the moment the image lands means a downloaded or uploaded cover is
    already library-conformant instead of sitting oversized until script 5
    happens to run.

    Best effort by design: anything PIL cannot read (or any encode failure)
    returns the original bytes untouched, so a stored cover is never lost or
    corrupted by this.
    """
    info = {"compressed": False, "original_bytes": len(data),
            "original_width": None, "original_height": None}
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    try:
        target = int(cfg.get("cover_target_size", 1200) or 0)
    except (TypeError, ValueError):
        target = 1200
    resize = bool(cfg.get("cover_resize_enabled", True))
    crop = bool(cfg.get("cover_crop_enabled", True))
    try:
        quality = max(70, min(100, int(cfg.get("cover_jpeg_quality", 90) or 90)))
    except (TypeError, ValueError):
        quality = 90
    # JXL is left alone: Pillow cannot write it without the plugin, and the
    # image script re-encodes it later.
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        return data, ext, info
    try:
        import io as _io
        from PIL import Image

        with Image.open(_io.BytesIO(data)) as img:
            img.load()
            ow, oh = img.size
            info["original_width"], info["original_height"] = ow, oh
            has_alpha = (img.mode in ("RGBA", "LA")
                         or (img.mode == "P" and "transparency" in img.info))
            out = img
            # Crop BEFORE resizing: the crop decides which pixels survive, so
            # scaling first would throw away resolution the crop should keep.
            if crop and ow != oh:
                side = min(ow, oh)
                left, top = (ow - side) // 2, (oh - side) // 2
                out = out.crop((left, top, left + side, top + side))
            w, h = out.size
            if resize and target > 0 and max(w, h) > target:
                scale = target / max(w, h)
                out = out.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                                 Image.LANCZOS)
            nw, nh = out.size
            buf = _io.BytesIO()
            if has_alpha:
                out.convert("RGBA").save(buf, format="PNG", optimize=True)
                new_ext = ".png"
            else:
                out.convert("RGB").save(buf, format="JPEG", quality=quality,
                                        optimize=True, progressive=True)
                new_ext = ".jpg"
            new_data = buf.getvalue()
            info["width"], info["height"] = nw, nh
            # Keep the re-encode only when it helps: it must not grow a file
            # (a small already-optimised PNG can) and must not be a no-op.
            if (nw, nh) != (ow, oh) or len(new_data) < len(data):
                info["compressed"] = True
                info["bytes"] = len(new_data)
                return new_data, new_ext, info
            info.pop("width", None)
            info.pop("height", None)
    except Exception:
        info.pop("width", None)
        info.pop("height", None)
    return data, ext, info


def _write_cover_bytes(alb: str, stem: str, ext: str, data: bytes):
    """Atomically write cover bytes as <stem><ext> into the album folder,
    bust the cover cache, and report the written image's dimensions.

    The image is compressed first (see _compress_cover_bytes), so a cover that
    was just downloaded or uploaded is stored at the library's own size and
    encoding rather than at whatever the source served."""
    orig_ext = ext
    info = {}
    try:
        data, ext, info = _compress_cover_bytes(data, ext)
    except Exception:
        info = {}
    dest = os.path.join(alb, f"{stem}{ext}")
    fd, tmp = tempfile.mkstemp(prefix=".cover_tmp_", suffix=ext, dir=alb)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass
        replace_locked(tmp, dest)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise HTTPException(500, str(e))
    # A re-encode can change the extension (PNG -> JPG). Remove exactly the
    # file this write superseded, so the album does not keep a stale duplicate
    # of the same stem in the old format.
    if ext != orig_ext:
        _drop_stale_cover(alb, f"{stem}{orig_ext}")
    tagcache.invalidate_album(alb)
    mbresolve.invalidate()
    out = {"ok": True, "path": dest.replace("\\", "/"), "token": _cover_token(dest)}
    out.update(_cover_metrics(dest))
    for k, v in info.items():
        out.setdefault(k, v)
    return out


_COVER_EXTS = (".jpg", ".jpeg", ".png", ".jxl", ".webp", ".bmp")


def _drop_stale_cover(alb: str, stale: str):
    """Remove one cover file that a re-encode has just superseded.

    Deliberately narrow: it removes exactly `stale` and nothing else. Only the
    file whose extension THIS write changed is superseded — an album may
    legitimately hold cover.png next to cover.jxl, and sweeping "other cover
    formats" would delete art the user still has.
    """
    try:
        path = os.path.join(alb, stale)
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _cover_token(path: str):
    """A value that changes whenever a cover file's bytes do — mtime + size.

    The UI puts it in the cover URL (`&v=<token>`), which is the only way a
    REPLACED cover is a different URL: a new image written over cover.jpg
    keeps the same album, the same file name and therefore the same URL, so
    neither the rendered <img> nor any HTTP cache would ever ask for it again.
    """
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return f"{st.st_mtime_ns}-{st.st_size}"


def _cover_metrics(path):
    """Dimensions (+ a below-target warning) for a just-written cover file.

    The same PIL read /api/cover/info does, but from the file: the write has
    already invalidated the byte cache this early in the request.

    The warning is the whole of the enforcement on this path: a cover the USER
    applies — an upload, or a pick from the finder's own list — is written
    whatever its size, and the reply says what is wrong with it. Refusing is
    deliberate only in the AUTONOMOUS path (`server.imports.run_cover_step`),
    which must not put a below-minimum image in the library without anyone
    asking for it; the finder's list shows below-floor rows for a hand apply
    precisely because the user may know better.
    """
    out = {"width": None, "height": None, "megapixels": None, "warning": None}
    try:
        from PIL import Image
        with Image.open(path) as img:
            w, h = img.size
    except Exception:
        return out
    if not (w and h):
        return out
    out["width"], out["height"] = w, h
    out["megapixels"] = round(w * h / 1_000_000, 2)
    try:
        target = int(load_config().get("cover_target_size", 1200) or 1200)
    except Exception:
        target = 1200
    out["target"] = target
    if 0 < target and min(w, h) < target:
        # The minimum IS the cover target the grader checks, so this is the
        # same number that will fail the album later — say that, rather than
        # only describing the image.
        out["below_target"] = True
        out["warning"] = (f"{w}×{h} is below the minimum {target}×{target} "
                          f"— grading will flag this cover")
    else:
        out["below_target"] = False
    return out


# --------------------------------------------------------------------------- #
# Process images after a MANUAL cover write
# --------------------------------------------------------------------------- #
# A manual cover write — the finder's pick (`/api/cover/fromurl`) or an upload
# (`/api/cover`) — stores the image the user chose exactly as it arrived.
# Script 5 ("Process images") is what re-encodes it to the library's own size,
# crop and format, and nothing else does that for an album the user just
# edited: a cover that arrives WITH an import is processed by that import's
# chain (script 5 sits in the default order the chain derives from), but a
# cover picked on an album already in the library would otherwise sit at
# whatever the provider served until the next Run All. So the write queues the
# script itself — for THAT album, in the background.
#
# Only the two human-driven routes call this. The importer's own cover step
# (`server.imports.run_cover_step`) writes through the same
# `_write_cover_bytes` and deliberately does not: its album is finished by an
# import chain that already carries script 5. A STAGED write (the import
# wizard's folder, which no chain has run over yet) is skipped for exactly the
# same reason — the chain finishing that album processes its cover.
_COVER_PROCESS_SCRIPT = 5
# Two presses in a row are one intent. A write to the same album inside this
# window is the same pick (a double click, an impatient re-apply), and the run
# the first one queued processes the folder as it stands when it gets there —
# so one run answers both.
_COVER_PROCESS_COALESCE_S = 10.0
_cover_process_lock = threading.Lock()
# album (normcased absolute path) -> monotonic stamp of the run queued for it.
_cover_process_queued: dict = {}


def _schedule_cover_process(alb):
    """Queue script 5 over ONE album, in the background, after a cover write.

    Returns whether a run was queued (the routes ignore it) — False when the
    same album was already written to inside `_COVER_PROCESS_COALESCE_S`.

    The run takes the same in-process path `/api/run` does —
    `script_runners.run_chain` with an album-scoped ``cfg["targets"]`` — so it
    claims that album in `server.job_locks` for the length of the run (it is
    what MAINTAIN → In progress lists, and every other writer of the album is
    refused while it works) and ends with the cache drop a run does
    (`_invalidate_run`), which is what makes the album page show the processed
    cover. The scope is this one folder: the library is never walked.
    """
    folder = os.path.normpath(alb)
    key = os.path.normcase(os.path.abspath(folder))
    now = time.monotonic()
    with _cover_process_lock:
        last = _cover_process_queued.get(key)
        if last is not None and now - last < _COVER_PROCESS_COALESCE_S:
            return False
        _cover_process_queued[key] = now
    threading.Thread(target=_process_cover_album, args=(folder,), daemon=True,
                     name="mlo-cover-process").start()
    return True


def _process_cover_album(folder):
    """The background half of :func:`_schedule_cover_process`.

    ``wait=True``: the request that queued this still holds the album for the
    instant it takes to build its response (its own `job_locks.holds` claim), so
    the run queues behind it — the way an import's chain queues behind the job
    it joins — instead of answering the 409 a one-shot `/api/run` would. The
    wait is bounded, so a wedged job cannot park this thread forever.

    Everything here is best-effort: the cover the user picked is already
    written, and a follow-up script that could not run must not turn that into
    an error on anyone's screen.
    """
    from server import script_runners

    try:
        cfg = load_config()
        cfg["targets"] = [folder]
        results = script_runners.run_chain(
            cfg, [_COVER_PROCESS_SCRIPT], targets=[folder], wait=True,
            timeout=job_locks.DEFAULT_WAIT)
        _invalidate_run(cfg, results)
    except script_runners.RunBusy as e:
        # Something else held the album past the wait (a library-wide sweep, an
        # import of it — that run processes this album too): say so, and leave
        # the cover exactly as the user wrote it.
        print(f"[mlo] Process images after a cover write did not start: {e}")
    except Exception:
        traceback.print_exc()


def _sniff_image_ext(data: bytes, content_type: str) -> str:
    """File-extension for image bytes, from magic numbers, then the
    Content-Type, defaulting to .jpg (the common cover-art case)."""
    magic = _image_magic_ext(data)
    if magic:
        return magic
    # split("/")[-1], not [1]: a missing or malformed Content-Type (an empty
    # string, or a bare "image") has no second part, and indexing it raised
    # IndexError -> a 500 out of /api/cover/fromurl for a remote image that
    # simply did not say what it was.
    ct = (content_type or "").split("/")[-1].strip().lower()
    if ct in ("jpeg", "jpg"):
        return ".jpg"
    if ct in ("png", "webp", "jxl", "bmp"):
        return f".{ct}"
    return ".jpg"


@router.get("/api/cover/search")
async def cover_search(artist: str = Query(""), album: str = Query(""),
                       limit: int = Query(cover_choice.SEARCH_LIMIT, ge=1, le=100),
                       sources: Optional[str] = Query(None),
                       country: Optional[str] = Query(None),
                       release_group_mbid: Optional[str] = Query(None),
                       release_mbid: Optional[str] = Query(None),
                       tracks: Optional[int] = Query(None, ge=1, le=1000)):
    """Album covers for artist/album, RANKED by the one cover policy.

    Sources: covers.musichoarders.xyz (aggregates Apple Music, Deezer, Qobuz,
    Tidal, Discogs, ...) by name, the Cover Art Archive by the identities the
    caller holds — the release's own front cover by `release_mbid`, its release
    group's stand-in by `release_group_mbid` — and, only when those have
    nothing, the keyless Deezer/iTunes fallbacks. `sources` in the reply is the
    per-source report (used / empty / error / skipped, with the reason), so a
    query that found nothing says what was tried and what was never asked.

    The search VERIFIES what it finds: `artist`/`album` (and `tracks`, when the
    caller knows how many the album has) are the identity each row's own
    release is checked against, so a karaoke, tribute or other-album row that
    answers to the same names is rejected rather than ranked — and pinned at
    the bottom of `results` with its rejection, where the finder still offers
    it for a hand apply. `identity` in the reply states what was checked
    against.

    `results` are `mlo.cover_choice`'s ranked candidates — best first, each row
    carrying the image's real pixel size, container and byte count as measured
    from the file, whether it is the release's own cover or a group stand-in,
    the reasons that put it where it is, and `rejected` when it cannot be the
    automatic pick (below the cover target, never measured while that target is
    the minimum, undecodable, an empty answer, another album's release) —
    with `chosen` (the winner) and `notes` alongside, so the finder's first
    row is first for a stated reason.

    `sources` (comma-separated ids) and `country` override the saved defaults
    for this one search — the finder's source picker and region dropdown.
    """
    if not artist.strip() and not album.strip():
        raise HTTPException(400, "artist or album is required")
    src = [s.strip() for s in (sources or "").split(",") if s.strip()] or None
    cfg = load_config()
    try:
        found = await asyncio.to_thread(
            intg.cover_search, artist.strip(), album.strip(), limit,
            60.0, src, country, cfg, (release_group_mbid or "").strip(),
            (release_mbid or "").strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"cover search failed: {e}")
    payload = cover_choice.cover_payload(
        found.get("results") or [], cfg, sources=found.get("sources"),
        provider=found.get("provider"),
        identity={"artist": artist.strip(), "album": album.strip(),
                  "tracks": tracks})
    return {"provider": found.get("provider"),
            "results": payload["candidates"],
            "chosen": payload["chosen"],
            "identity": payload["identity"],
            "notes": payload["notes"],
            "policy": payload["policy"],
            "candidate_count": payload["candidate_count"],
            "rejected_count": payload["rejected_count"]}


@router.get("/api/cover/sources")
def cover_sources():
    """Selectable cover sources + regions, and the saved defaults.

    `default_sources` / `default_country` are what Settings holds, so the
    finder opens on exactly the values a run without overrides would use."""
    cat = intg.cov_catalog()
    cfg = load_config()
    chosen, ctry = intg.resolve_cov_search(None, None, cfg)
    return {
        "sources": cat["sources"],
        "countries": cat["countries"],
        "active_source_limit": cat["active_source_limit"],
        "default_sources": chosen,
        "default_country": ctry,
        "saved_sources": [str(s) for s in (cfg.get("cover_sources") or [])],
        "saved_country": str(cfg.get("cover_country") or intg.COV_DEFAULT_COUNTRY),
    }


@router.post("/api/cover/fromurl")
@job_locks.holds(lambda album, **_: [album], kind="cover", label="Cover write")
async def cover_from_url(album: str = Query(...), url: str = Query(...),
                         track: Optional[str] = Query(None),
                         tracks: Optional[str] = Query(None),
                         staged: bool = Query(False)):
    """Download a cover image from a URL (e.g. a COV search result) and
    store it like an uploaded cover (album cover.*, one per-track sidecar, or
    one image mapped to a whole `tracks=` selection).

    The image at `url` is the image stored: no provider is asked in its place
    (see `_cover_url_bytes`), so a pick that cannot be fetched fails here and
    says so instead of landing some other cover on the album.
    """
    alb = os.path.normpath(album)
    if not os.path.isdir(alb):
        raise HTTPException(404, "album not found")
    _guard_folder(alb, staged, "album")
    stem, selected = _cover_write_target(alb, track, tracks)
    try:
        data, ctype = await asyncio.to_thread(
            _cover_url_bytes, url, substitute=False)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"cover download failed: {e}")
    if not data:
        raise HTTPException(502, "empty image response")
    res = _write_cover_bytes(alb, stem, _sniff_image_ext(data, ctype), data)
    if selected:
        set_track_covers(alb, selected, os.path.basename(res["path"]))
    if not staged:
        # The finder's pick on a library album: process the image the user
        # chose now (see _schedule_cover_process).
        _schedule_cover_process(alb)
    return res


class CoverClearRequest(BaseModel):
    album: str
    tracks: Optional[List[str]] = None  # None/empty = every per-track entry
    staged: bool = False                # the import wizard's staged album


@router.post("/api/cover/clear")
@job_locks.holds(lambda req: [req.album], kind="cover", label="Cover write")
def cover_clear(req: CoverClearRequest):
    """Drop per-track cover mappings for an album (no `tracks` = all of them).
    The image files stay on disk — clearing a mapping is not deleting art."""
    alb = os.path.normpath(mbresolve.resolve_album(req.album) or req.album)
    if not os.path.isdir(alb):
        raise HTTPException(404, "album not found")
    _guard_folder(alb, req.staged, "album")
    names = [os.path.basename(str(t)) for t in (req.tracks or []) if str(t).strip()]
    clear_track_covers(alb, names or None)
    tagcache.invalidate_album(alb)
    return {"ok": True}


@router.get("/api/cover/info")
def cover_info(album: str = Query(...), file: str = Query(None),
               staged: bool = Query(False)):
    """Image details for the album cover (resolution, aspect ratio,
    format, byte size) — powers the album page's Cover info dialog."""
    import io

    alb = os.path.normpath(mbresolve.resolve_album(album) or album)
    if not os.path.isdir(alb):
        raise HTTPException(404, "album not found")
    _guard_folder(alb, staged, "album")
    data, ctype, _etag = tagcache.cover_bytes(alb, file)
    if data is None:
        raise HTTPException(404, "no cover")
    # The file the bytes came from — the ONE resolution rule (`cover_path`:
    # the named file while it exists, else the album's own cover), so a stale
    # name reports the file that really answered.
    p = tagcache.cover_path(alb, file)
    info = {"file": os.path.basename(p) if p else None,
            "format": (ctype or "image").split("/")[-1].upper(),
            "bytes": len(data),
            "megapixels": None, "aspect": None, "aspect_label": None,
            "width": None, "height": None}
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        info["width"], info["height"] = w, h
        info["format"] = img.format or info["format"]
        if w and h:
            info["megapixels"] = round(w * h / 1_000_000, 2)
            from math import gcd
            g = gcd(w, h) or 1
            rw, rh = w // g, h // g
            if rw > 40 or rh > 40:
                # huge coprime pairs read badly — show a decimal ratio too
                info["aspect"] = f"{rw}:{rh}"
                info["aspect_label"] = f"{w / h:.2f}:1"
            else:
                info["aspect"] = f"{rw}:{rh}"
                info["aspect_label"] = f"{rw}:{rh}"
    except Exception:
        pass
    return info
