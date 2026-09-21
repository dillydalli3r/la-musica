"""The download side of the media path: full-body file responses, and the bulk
stream the browser's offline-download queue fetches several tracks with.

`/api/stream` is the PLAYER's endpoint. It advertises `Accept-Ranges: bytes`
and a Range request is answered with 206 Partial Content (Starlette's
FileResponse), because that is what seeking needs. The browser's offline cache
is the other consumer, and Cache Storage accepts a full 200 and NOTHING else:
`Cache.put()` refuses a 206 outright ("Cache got basic response with bad status
206"). The download path used to fetch that very URL, so every download ended
in that TypeError with nothing cached — the bug the user hit.

Two answers live here:

- `full_body_response()` — one file as one 200 body, ranges refused. This is
  what `/api/stream?download=1` answers with (see server/main.py), so the
  browser's full-body fetch has a URL the range path can never turn into a
  206. It is a flag on the existing route rather than a second route because a
  player and a downloader differ only in how much of the file they want.
- `POST /api/media/bulk` — several files in ONE response, so a 100-track
  selection is not 100 requests. The body is one JSON header line (each file's
  path, size, MIME and, when it cannot be read, why) followed by the files'
  bytes back to back in request order, read through a bounded pool of reader
  threads that runs ahead of the socket. A path that cannot be read is
  reported in the header and skipped, so one dead file never costs the caller
  the rest of the batch, and the pool width is the config key the client's own
  queue uses (`download_concurrency`, clamped here as well: it is a fan-out
  bound).

Both are read-only, both stay inside the music folder through the same guard
the stream endpoint uses, and both serve the file's own bytes — the download
path never transcodes.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Callable, Iterator, Sequence

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from mlo import load_config

from server import job_locks

router = APIRouter(tags=["media"])

# The framing's content type: one JSON header line, then the files' bytes. The
# client parses the body itself, so this only has to name what it is.
BULK_MIME = "application/x-mlo-bulk"
BULK_VERSION = 1

# At most this many files in one bulk request. The client chunks its queue at a
# fraction of it; the cap is what keeps ONE request from holding a whole
# selection's worth of paths (and their errors) in memory.
MAX_BULK = 500

# The pool width `download_concurrency` defaults to — the same 3 the config
# ships, so a server that never had the key set behaves like one that did.
DEFAULT_WINDOW = 3
MAX_WINDOW = 8

# How much of a file a reader hands over at a time. Big enough that the copy
# cost disappears next to the socket, small enough that a pool of readers
# cannot buffer an album.
CHUNK = 1 << 20

# A file a tagger or ffprobe holds open mid-write is worth one more try before
# the batch reports it as unreadable — the write is usually done milliseconds
# later, and the alternative is a 502-shaped hole in a 100-track download.
OPEN_ATTEMPTS = 2
OPEN_RETRY_S = 0.25

# Starlette's GZipMiddleware compresses everything it does not recognise as
# already-compressed, at compresslevel 9. On a FLAC that is a full-core zlib
# pass per download (and per batch) for zero bytes saved. `identity` is the
# one signal it honours (RFC 9110 §8.4.1: no transformation applied), and
# `no-store` keeps any proxy between the two from keeping a copy of the bytes.
_NO_TRANSFORM = {"Content-Encoding": "identity", "Cache-Control": "no-store"}


class BulkRequest(BaseModel):
    """`paths` are library paths exactly as the client holds them
    (`track.path`), in the order their bytes are wanted."""

    paths: list[str] = []


class BulkReadError(Exception):
    """A file's promised bytes could not be delivered.

    Raised mid-stream, where no status code can be sent any more. The header
    already told the client how many bytes to expect for that file, so ending
    the body EARLY is the only honest answer: the client reads the shortfall,
    reports that track, and asks for it (and the rest of the batch) again on
    its own.
    """


def clamp_window(value, default: int = DEFAULT_WINDOW) -> int:
    """`download_concurrency` as a pool width, clamped to 1..MAX_WINDOW.

    `normalize_config` clamps a stored value too; this is the second line of
    defence for a hand-edited config.json or a direct call, because the width
    is a FAN-OUT bound — 0 would stall the pool forever and a stray 500 would
    open 500 files at once.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_WINDOW, n)) if n > 0 else default


def _mime_for(path: str) -> str:
    """The container's media type, from the stream endpoint's own table."""
    from server.main import _CTYPES

    return _CTYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def plan_downloads(
    paths: Sequence[str],
    *,
    check: Callable[[str], tuple[str, str | None]] | None = None,
    is_file: Callable[[str], bool] = os.path.isfile,
    size_of: Callable[[str], int] = os.path.getsize,
    mime_for: Callable[[str], str] = _mime_for,
) -> list[dict]:
    """One header entry per requested path, in request order.

    `check` is the caller's guard (inside the music folder, a resolved path):
    it takes the requested path and returns `(path to read, error or None)`, so
    a resolver can rewrite a path that has been moved before anything opens it.
    Every filesystem call is injectable and defaults to the real one, which is
    what lets the plan be proven without a disk.

    A path that fails any step keeps its place in the list with the reason in
    `error` and a size of 0 — the caller reports that file and reads the rest,
    so one missing track cannot fail the batch.
    """
    entries: list[dict] = []
    for raw in paths or []:
        want = str(raw or "")
        read, err = (want, None)
        if not want:
            err = "no path given"
        elif check is not None:
            read, err = check(want)
        if err is None and not is_file(read):
            err = "the server has no file at that path"
        size = 0
        if err is None:
            try:
                size = int(size_of(read))
            except OSError:
                # Locked, vanished, or a directory: unreadable now, and the
                # size is what the framing promises, so there is nothing to
                # promise.
                err = "the file could not be read"
        entries.append({
            "path": want,
            "read": read,
            "size": size if err is None else 0,
            "mime": mime_for(read) if err is None else "",
            "error": err,
        })
    return entries


def header_line(entries: Sequence[dict]) -> bytes:
    """The first line of a bulk body: one JSON object for the whole batch.

    `read` (the resolved path a reader opens) is deliberately left out — it is
    the server's business, and the client only ever matches on the path it
    asked with. Newline-terminated and UTF-8, so a path with any character in
    it survives the wire.
    """
    files = [
        {
            "path": str(e.get("path") or ""),
            "size": int(e.get("size") or 0),
            "mime": str(e.get("mime") or ""),
            "error": e.get("error") or None,
        }
        for e in entries
    ]
    payload = {"v": BULK_VERSION, "count": len(files), "files": files}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def without_range(scope: dict) -> dict:
    """`scope` with every Range header dropped, so a file response cannot turn
    into a 206.

    Cache Storage takes a 200 and nothing else: the offline download path must
    never be answered with a partial body (see the module docstring). The
    request itself is otherwise untouched — the same scope dict is handed to
    the error handling, which must still see what arrived.
    """
    if scope.get("type") != "http":
        return scope
    out = dict(scope)
    out["headers"] = [(k, v) for k, v in scope.get("headers") or [] if k.lower() != b"range"]
    return out


class _Reader(threading.Thread):
    """One file, read in chunks into its own bounded queue.

    A thread per file is what makes the window a pool rather than a queue: the
    later files are already being read from disk while the socket drains the
    first. The queue is bounded, so a fast disk cannot buffer an album ahead
    of a slow client, and it is polled against `stop` so a client that hangs up
    mid-batch ends the readers instead of leaving them blocked on a full queue.
    """

    def __init__(self, entry: dict, *, opener, chunk: int, stop: threading.Event,
                 attempts: int = OPEN_ATTEMPTS, delay: float = OPEN_RETRY_S):
        super().__init__(daemon=True, name=f"mlo-bulk-{os.path.basename(str(entry.get('path')))}")
        self.entry = entry
        self.opener = opener
        self.chunk = chunk
        self.stop = stop
        self.attempts = attempts
        self.delay = delay
        self.queue: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=2)

    # -- the producer half -------------------------------------------------- #
    def _push(self, item: tuple[str, object]) -> bool:
        """Hand one item to the socket side; False once the batch is over."""
        while not self.stop.is_set():
            try:
                self.queue.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def run(self) -> None:
        path = str(self.entry.get("read") or "")
        want = int(self.entry.get("size") or 0)
        attempts = max(1, self.attempts)
        handle = None
        for attempt in range(attempts):
            try:
                handle = self.opener(path, "rb")
                break
            except Exception as e:  # noqa: BLE001 — see below
                # A reader that died without a word would leave the consumer
                # waiting on this queue forever, so every failure ends the file
                # with a reason. Only a plain OSError is worth retrying: that is
                # what a file another process holds open looks like, and it is
                # usually gone milliseconds later.
                if self.stop.is_set():
                    return
                if attempt + 1 >= attempts or not isinstance(e, OSError):
                    self._push(("fail", f"the file could not be opened ({type(e).__name__})"))
                    return
                time.sleep(self.delay)
        sent = 0
        try:
            while sent < want and not self.stop.is_set():
                data = handle.read(min(self.chunk, want - sent))
                if not data:
                    break
                sent += len(data)
                if not self._push(("data", data)):
                    return
        except Exception as e:  # noqa: BLE001 — same reason as the open above
            self._push(("fail", f"the file could not be read ({type(e).__name__})"))
            return
        finally:
            try:
                handle.close()
            except OSError:
                pass
        # The whole file, or the framing is a lie: a file that shrank under us
        # since the header was written desyncs every following file too, so it
        # is reported like any other unreadable one.
        self._push(("done", None) if sent >= want else ("fail", "the file changed while it was being sent"))

    # -- the consumer half -------------------------------------------------- #
    def chunks(self) -> Iterator[bytes]:
        """This file's bytes, in order; raises BulkReadError when it cannot."""
        while True:
            kind, payload = self.queue.get()
            if kind == "data":
                yield payload  # type: ignore[misc]
            elif kind == "done":
                return
            else:
                raise BulkReadError(str(payload))

    def close(self) -> None:
        """Stop the reader and let it release its file handle."""
        self.stop.set()
        self.join(timeout=5)


def iter_bulk_body(
    entries: Sequence[dict],
    *,
    window: int = DEFAULT_WINDOW,
    chunk: int = CHUNK,
    opener: Callable = open,
    attempts: int = OPEN_ATTEMPTS,
) -> Iterator[bytes]:
    """The whole bulk body: the header line, then every readable file.

    Readers are started in a sliding window of `window` and drained in request
    order, so the parallelism is real and the framing is still the caller's
    order (the client keys each file's bytes to its own track). Files the plan
    already refused carry no bytes at all and are skipped here.
    """
    yield header_line(entries)
    order = [i for i, e in enumerate(entries) if not e.get("error")]
    stop = threading.Event()
    live: list[_Reader] = []
    started = 0
    try:
        for index in order:
            while started < len(order) and len(live) < max(1, window):
                reader = _Reader(entries[order[started]], opener=opener, chunk=chunk,
                                 stop=stop, attempts=attempts)
                live.append(reader)
                reader.start()
                started += 1
            # Readers start in `order` and are drained in `order`, so the first
            # live one is always this index's.
            reader, live = live[0], live[1:]
            for data in reader.chunks():
                yield data
    finally:
        # Client hung up, or the batch is done: every reader still running has
        # a file open and a queue nobody will drain.
        stop.set()
        for reader in live:
            reader.close()


def full_body_response(path: str, media_type: str) -> FileResponse:
    """`path` as ONE 200 body, whatever the request asked for.

    Starlette's FileResponse honours a Range header because a player wants
    206s; Cache Storage refuses them, so the Range header is dropped from the
    request the body is built from and the response says `Accept-Ranges: none`
    — no client is told a range is available when answering with one would
    break the cache that asked.
    """

    class _FullBody(FileResponse):
        async def __call__(self, scope, receive, send):
            await super().__call__(without_range(scope), receive, send)

    return _FullBody(path, media_type=media_type,
                     headers={"Accept-Ranges": "none", **_NO_TRANSFORM})


def _resolve_and_guard(path: str) -> tuple[str, str | None]:
    """The readable path for `path`, or the reason it may not be read.

    The same rules as the stream endpoint: the file is resolved through
    MusicBrainz (a track moved by the organizer is still the same recording)
    and it has to sit inside the music folder.
    """
    from server import mbresolve
    from server.main import _in_music_folder, _music_folder

    full = os.path.normpath(mbresolve.resolve_track(path) or path)
    try:
        folder = _music_folder()
    except HTTPException as e:
        return full, str(e.detail)
    if not _in_music_folder(full, folder):
        return full, "path outside music folder"
    return full, None


def _download_guard(path: str) -> tuple[str, str | None]:
    """`_resolve_and_guard` plus the library lock: the same content guard, with
    a path a job is rewriting reported in this file's header entry.

    Offline downloads are the other reader of the player's own bytes, and the
    same collision applies from their side: caching a file a script is
    rewriting stores a torn copy that plays as garbage later, offline, with no
    server to re-fetch it from. Reported per file rather than as a 409 for the
    whole batch — one locked album in a 100-track selection must not cost the
    caller the other 99 — and the text is the registry's own refusal, which is
    what /api/stream would say for that path.
    """
    full, err = _resolve_and_guard(path)
    if err:
        return full, err
    holder = job_locks.holder(full)
    if holder:
        return full, job_locks.refusal(full, holder)
    return full, None


@router.post("/api/media/bulk")
def media_bulk(req: BulkRequest):
    """Several tracks' bytes in one response (see the module docstring).

    A one-line JSON header names every file with its size and MIME, then those
    files' bytes follow in the order they were asked for. The pool width is
    `download_concurrency`, and a path that cannot be read is reported in the
    header rather than failing the request: the client caches what arrived and
    says which tracks did not.
    """
    if not req.paths:
        raise HTTPException(400, "no paths given")
    if len(req.paths) > MAX_BULK:
        raise HTTPException(413, f"at most {MAX_BULK} files per request "
                                 f"({len(req.paths)} given) — send the queue in chunks")
    entries = plan_downloads(req.paths, check=_download_guard)
    window = clamp_window(load_config().get("download_concurrency"))
    return StreamingResponse(
        iter_bulk_body(entries, window=window),
        media_type=BULK_MIME,
        headers=dict(_NO_TRANSFORM),
    )
