#!/usr/bin/env python3
"""Regression: the offline download path — why it used to fail, and the wire
contract the browser now depends on.

The user's error was

    Cache.put: Cache got basic response with bad status 206 while trying to
    add request http://localhost:8000/api/stream?path=...

i.e. the browser's offline cache was handed a 206 Partial Content: the download
fetched the very URL the player streams with a byte range (Starlette's
FileResponse answers a Range request with 206, and the service worker's media
branch answers one with a sliced 206), and Cache Storage accepts a 200 and
nothing else. Two things in server/api_media.py are the answer, and both are
proven here:

  * `without_range()` — the scope `/api/stream?download=1` is served through,
    with every Range header dropped, so a download is one 200 body;
  * the bulk body — one JSON header line naming every file (size, MIME, and a
    reason for any path that cannot be read) followed by those files' bytes in
    request order, read through a window of reader threads.

Nothing here touches a disk or a network: every filesystem call the framing
makes is injectable, so the plan, the frame and the pool run against in-memory
fakes, and the decoder at the bottom is the same walk the browser's reader
does (one header line, then exactly each file's byte count).

Run:  python tools/test_download_queue.py   (exit 0 pass, 1 fail)
"""
import io
import json
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import HTTPException  # noqa: E402

from mlo.config import DEFAULT_CONFIG, _INT_RANGES  # noqa: E402
from server import api_media as media  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{('  — ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILED.append(name)


# --------------------------------------------------------------------------- #
# An in-memory disk: what every reader's opener goes through, counting the
# handles open AT ONCE (the pool's bound) and failing a path on demand.
# --------------------------------------------------------------------------- #
class FakeDisk:
    """An in-memory disk whose reads can be made slow enough to observe.

    `open_gate` makes every read wait until that many files are open at once —
    with instant reads a window of threads finishes one at a time, so the
    overlap a pool exists for would never be visible. `read_delay` keeps a
    handle busy in a read, which is what a cancelled batch has to stop.
    """

    def __init__(self, files, *, open_gate=None, read_delay=0.0):
        self.files = dict(files)
        self.lock = threading.Lock()
        self.open_now = 0
        self.peak = 0
        self.opened = []
        self.attempts = []
        self.fail_times = {}
        self.break_at = {}
        self.closed = []
        self.open_gate = open_gate
        self.gate = threading.Event()
        self.read_delay = read_delay

    def broken(self, path, times):
        """Refuse the next `times` opens of `path` (a locked file)."""
        self.fail_times[path] = times

    def breaks(self, path, after):
        """Die after `after` bytes of `path` (a dropped disk/connection)."""
        if after is None:
            self.break_at.pop(path, None)
        else:
            self.break_at[path] = after

    def opener(self, path, mode="rb"):
        with self.lock:
            self.attempts.append(path)
            left = self.fail_times.get(path, 0)
            if left > 0:
                self.fail_times[path] = left - 1
                raise OSError(13, "another process has it open")
            self.open_now += 1
            self.peak = max(self.peak, self.open_now)
            self.opened.append(path)
            if self.open_gate and self.open_now >= self.open_gate:
                self.gate.set()
        return FakeHandle(self, path, self.files.get(path, b""))


class FakeHandle:
    def __init__(self, disk, path, data):
        self.disk = disk
        self.path = path
        self.buf = io.BytesIO(data)
        self.served = 0
        self.closed = False

    def read(self, n=-1):
        if self.disk.open_gate:
            self.disk.gate.wait(2.0)
        if self.disk.read_delay:
            time.sleep(self.disk.read_delay)
        limit = self.disk.break_at.get(self.path)
        if limit is not None and self.served >= limit:
            raise OSError(5, "input/output error")
        data = self.buf.read(n)
        self.served += len(data)
        return data

    def close(self):
        if self.closed:
            return
        self.closed = True
        with self.disk.lock:
            self.disk.open_now -= 1
            self.disk.closed.append(self.path)


def plan(disk, paths, **kw):
    """A plan over the fake disk — the real defaults are the disk calls."""
    return media.plan_downloads(
        paths,
        is_file=lambda p: p in disk.files,
        size_of=lambda p: len(disk.files[p]),
        mime_for=lambda p: "audio/x-test",
        **kw,
    )


def body(disk, paths, **kw):
    return b"".join(media.iter_bulk_body(plan(disk, paths), opener=disk.opener, **kw))


def drain(disk, entries, **kw):
    """Everything the body yields up to its end, plus how it ended."""
    out = bytearray()
    error = None
    try:
        for chunk in media.iter_bulk_body(entries, opener=disk.opener, **kw):
            out += chunk
    except Exception as e:  # the client sees the shortfall, not an exception
        error = e
    return bytes(out), error


class FrameError(Exception):
    """What the browser's reader raises on a body it cannot walk."""


def read_frame(data):
    """Decode a bulk body exactly as lib/mediaCache's reader does."""
    end = data.find(b"\n")
    if end < 0:
        raise FrameError("the body never named the files")
    header = json.loads(data[:end].decode("utf-8"))
    files = {}
    at = end + 1
    for row in header["files"]:
        if row["error"]:
            continue
        stop = at + row["size"]
        if stop > len(data):
            raise FrameError(f"ended early: {len(data) - at} of {row['size']} bytes")
        files[row["path"]] = data[at:stop]
        at = stop
    if at != len(data):
        raise FrameError("bytes nobody was told about")
    return header, files


# --------------------------------------------------------------------------- #
print("\npool width (`download_concurrency`)")
# --------------------------------------------------------------------------- #
check("the shipped config carries the key and its range",
      DEFAULT_CONFIG.get("download_concurrency") == 3
      and _INT_RANGES.get("download_concurrency") == (1, 8),
      f"{DEFAULT_CONFIG.get('download_concurrency')!r} {_INT_RANGES.get('download_concurrency')!r}")
check("a missing/junk/zero value falls back to the default",
      [media.clamp_window(v) for v in (None, "", "x", 0, -3)] == [3, 3, 3, 3, 3])
check("a real value passes through and the ceiling is enforced",
      [media.clamp_window(v) for v in (1, 2, 3, 8, 9, 500)] == [1, 2, 3, 8, 8, 8],
      str([media.clamp_window(v) for v in (1, 8, 9)]))
check("a float or numeric string still lands in range",
      [media.clamp_window(v) for v in (2.9, "4", "0")] == [2, 4, 3],
      str([media.clamp_window(v) for v in (2.9, "4", "0")]))

# --------------------------------------------------------------------------- #
print("\nthe plan: which files are readable, in the order they were asked for")
# --------------------------------------------------------------------------- #
disk = FakeDisk({
    "/music/01 first.flac": b"A" * 10,
    "/music/02 empty.mp3": b"",
    "/music/03 ünïcode — däsh.flac": "héllo wörld".encode("utf-8"),
})
entries = plan(disk, ["/music/01 first.flac", "/music/gone.flac", "/music/02 empty.mp3"])
check("every path keeps its place, and only readable ones carry a size",
      [(e["path"], e["size"], e["error"]) for e in entries]
      == [("/music/01 first.flac", 10, None), ("/music/gone.flac", 0, "the server has no file at that path"),
          ("/music/02 empty.mp3", 0, None)],
      str([(e["path"], e["size"], e["error"]) for e in entries]))

locked = FakeDisk({"/music/held.flac": b"x" * 4})


def refuse(_p):
    raise OSError(13, "in use")


stuck = media.plan_downloads(["/music/held.flac"], is_file=lambda p: True, size_of=refuse,
                            mime_for=lambda p: "audio/x-test")
check("a file whose size cannot be read is refused before any bytes are promised",
      stuck[0]["error"] == "the file could not be read" and stuck[0]["size"] == 0, str(stuck[0]))

resolved = media.plan_downloads(
    ["/music/moved.flac"],
    check=lambda p: ("/library/where it is now.flac", None),
    is_file=lambda p: p == "/library/where it is now.flac",
    size_of=lambda p: 7,
    mime_for=lambda p: "audio/flac",
)
check("the caller's guard can resolve a moved path before anything opens it",
      resolved[0]["read"] == "/library/where it is now.flac" and resolved[0]["path"] == "/music/moved.flac"
      and resolved[0]["size"] == 7 and resolved[0]["mime"] == "audio/flac", str(resolved[0]))

refused = media.plan_downloads(["/etc/passwd"], check=lambda p: (p, "path outside music folder"),
                              is_file=lambda p: True, size_of=lambda p: 4, mime_for=lambda p: "text/plain")
check("a refused path is reported with the guard's own reason",
      refused[0]["error"] == "path outside music folder" and refused[0]["size"] == 0, str(refused[0]))

# --------------------------------------------------------------------------- #
print("\nthe header line: one JSON object naming the whole batch")
# --------------------------------------------------------------------------- #
payload = media.header_line(plan(disk, ["/music/01 first.flac", "/music/gone.flac"]))
check("it is a single newline-terminated line",
      payload.endswith(b"\n") and payload.count(b"\n") == 1, payload[:80].decode("utf-8", "replace"))
parsed = json.loads(payload.decode("utf-8"))
check("it carries the framing version, the count and one row per file",
      parsed["v"] == media.BULK_VERSION and parsed["count"] == 2
      and [r["size"] for r in parsed["files"]] == [10, 0], str(parsed)[:120])
check("the server's own read path is not published",
      all("read" not in row for row in parsed["files"]), str(parsed["files"][0]))
check("a non-ASCII path survives the wire",
      json.loads(media.header_line(plan(disk, ["/music/03 ünïcode — däsh.flac"])).decode("utf-8"))
      ["files"][0]["path"] == "/music/03 ünïcode — däsh.flac")

# --------------------------------------------------------------------------- #
print("\nthe body: every file's bytes, in request order")
# --------------------------------------------------------------------------- #
order = ["/music/02 empty.mp3", "/music/01 first.flac", "/music/03 ünïcode — däsh.flac"]
header, files = read_frame(body(disk, order))
check("the decoder walks the whole body, nothing short and nothing extra",
      set(files) == set(order) and files["/music/01 first.flac"] == b"A" * 10
      and files["/music/03 ünïcode — däsh.flac"] == "héllo wörld".encode("utf-8"),
      str(sorted(files)))
check("a zero-byte file is a file too",
      header["files"][0]["size"] == 0 and files["/music/02 empty.mp3"] == b"")
check("the bytes arrive in the order they were asked for",
      body(disk, order).index(files["/music/03 ünïcode — däsh.flac"])
      > body(disk, order).index(files["/music/01 first.flac"]))
check("an unreadable path is named in the header and sends no bytes",
      read_frame(body(disk, ["/music/gone.flac", "/music/01 first.flac"]))[0]["files"][0]["error"]
      == "the server has no file at that path")
check("chunking changes how many reads happen, not the bytes",
      read_frame(body(disk, order, chunk=3))[1] == files, "chunk=3")

# --------------------------------------------------------------------------- #
print("\nthe pool: a window of readers, and it is really a window")
# --------------------------------------------------------------------------- #
many = FakeDisk({f"/music/{i:02d}.flac": bytes([i]) * 40 for i in range(6)})
paths = [f"/music/{i:02d}.flac" for i in range(6)]
wide = FakeDisk({p: many.files[p] for p in paths}, open_gate=2)
read_frame(body(wide, paths, window=3))
check("no more files are open at once than the window allows",
      wide.peak <= 3, f"peak {wide.peak} for window 3")
check("the window really runs ahead (several files in flight together)",
      wide.peak > 1, f"peak {wide.peak}")
narrow = FakeDisk({p: many.files[p] for p in paths})
read_frame(body(narrow, paths, window=1))
check("window 1 is strictly one file at a time, and still in order",
      narrow.peak == 1 and narrow.opened == paths, f"peak {narrow.peak}")

# --------------------------------------------------------------------------- #
print("\ntransient failures: an open retried once, a mid-file read reported")
# --------------------------------------------------------------------------- #
retry = FakeDisk({p: many.files[p] for p in paths})
retry.broken("/music/03.flac", 1)
check("a file another process held open is retried and delivered whole",
      read_frame(body(retry, paths))[1]["/music/03.flac"] == many.files["/music/03.flac"]
      and retry.attempts.count("/music/03.flac") == 2, str(retry.attempts))

stubborn = FakeDisk({p: many.files[p] for p in paths})
stubborn.broken("/music/02.flac", 5)
partial, error = drain(stubborn, plan(stubborn, paths))
check("a file that stays unreadable ends the body instead of desyncing it",
      isinstance(error, media.BulkReadError), repr(error))
check("what did arrive before that is the earlier files, complete",
      partial.startswith(media.header_line(plan(stubborn, paths)) + many.files["/music/00.flac"]
                         + many.files["/music/01.flac"]),
      f"{len(partial)} bytes")

shrunk = FakeDisk({"/music/a.flac": b"short"})
lie = media.plan_downloads(["/music/a.flac"], is_file=lambda p: True, size_of=lambda p: 99,
                           mime_for=lambda p: "audio/x-test")
short_body, short_error = drain(shrunk, lie)
check("a file that shrank since the header was written is detected",
      isinstance(short_error, media.BulkReadError), repr(short_error))
try:
    read_frame(short_body)
    check("the browser's reader sees that shortfall as a truncated body", False, "decoder accepted it")
except FrameError as e:
    check("the browser's reader sees that shortfall as a truncated body", "ended early" in str(e), str(e))

# A body that dies mid-file must be reported, must release what it had open,
# and must be recoverable: the browser's queue answers exactly this by asking
# for that track (and the rest of the batch) again, one request each. What
# makes that a RETRY rather than a repeat is that a second body over the same
# plan comes back whole once the failure clears — proven here.
dropped = FakeDisk({p: many.files[p] for p in paths})
dropped.breaks("/music/01.flac", 8)
# chunk=8 so the second read of that file is the one that dies (a whole file
# arriving in one read can never show a transfer that broke part-way).
partial, error = drain(dropped, plan(dropped, paths), chunk=8)
check("a read that dies mid-file ends the body there, with a reason",
      isinstance(error, media.BulkReadError) and str(error).startswith("the file could not be read"),
      repr(error))
check("the files that arrived before the drop are complete, in order, and nothing after it",
      partial == media.header_line(plan(dropped, paths)) + many.files["/music/00.flac"]
      + many.files["/music/01.flac"][:8],
      f"{len(partial)} bytes")
check("the reader that failed released its handle", dropped.open_now == 0, f"{dropped.open_now} open")
dropped.breaks("/music/01.flac", None)  # the disk came back
retried = read_frame(body(dropped, paths, chunk=8))[1]
check("the same batch, asked again after the failure cleared, arrives whole",
      retried == {p: many.files[p] for p in paths}, f"{sorted(retried.items())[:1]}")
check("...including the file whose transfer had died", retried["/music/01.flac"] == many.files["/music/01.flac"])

# --------------------------------------------------------------------------- #
print("\ncancelling: the readers stop and release their files")
# --------------------------------------------------------------------------- #
cancel = FakeDisk({p: many.files[p] for p in paths}, read_delay=0.8)
stream = media.iter_bulk_body(plan(cancel, paths), opener=cancel.opener, window=2)
next(stream)  # the header line
next(stream)  # the first file's first chunk: the readers are live and working
stream.close()
for _ in range(60):
    if cancel.open_now == 0 and not [t for t in threading.enumerate() if t.name.startswith("mlo-bulk")]:
        break
    time.sleep(0.05)
check("a client that hangs up leaves no file open",
      cancel.open_now == 0, f"{cancel.open_now} handles still open")
check("and no reader thread behind",
      not [t for t in threading.enumerate() if t.name.startswith("mlo-bulk")])
check("the read-ahead stayed inside the window, so cancelling skipped the rest",
      sorted(cancel.opened) == ["/music/00.flac", "/music/01.flac"], str(cancel.opened))
check("every handle it did open was closed",
      sorted(cancel.closed) == sorted(cancel.opened), f"{cancel.opened} vs {cancel.closed}")

# --------------------------------------------------------------------------- #
print("\ndownload responses: one 200 body, never a range")
# --------------------------------------------------------------------------- #
scope = {"type": "http", "headers": [(b"Range", b"bytes=0-"), (b"accept", b"*/*")]}
dropped = media.without_range(scope)
check("the Range header is dropped, case-insensitively",
      [k for k, _ in dropped["headers"]] == [b"accept"], str(dropped["headers"]))
check("the request it was called on is left alone",
      len(scope["headers"]) == 2, str(scope["headers"]))
check("a non-http scope passes through untouched",
      media.without_range({"type": "websocket"}) == {"type": "websocket"})
check("an http scope without headers is safe",
      media.without_range({"type": "http"})["headers"] == [])
check("the response tells the client ranges are not on offer",
      media.full_body_response("/tmp/x.flac", "audio/flac").headers["accept-ranges"] == "none")
check("the body is never handed to the gzip middleware as compressible",
      media.full_body_response("/tmp/x.flac", "audio/flac").headers["content-encoding"] == "identity")

# --------------------------------------------------------------------------- #
print("\nthe endpoint: routing and the refusals it answers with")
# --------------------------------------------------------------------------- #
routes = {getattr(r, "path", ""): sorted(getattr(r, "methods", []) or []) for r in media.router.routes}
check("the bulk route is POST /api/media/bulk", routes.get("/api/media/bulk") == ["POST"], str(routes))

try:
    media.media_bulk(media.BulkRequest(paths=[]))
    check("an empty request is refused before any path is resolved", False, "accepted")
except HTTPException as e:
    check("an empty request is refused before any path is resolved", e.status_code == 400, str(e.status_code))

try:
    media.media_bulk(media.BulkRequest(paths=["/music/x.flac"] * (media.MAX_BULK + 1)))
    check("a batch beyond the cap is refused rather than buffered", False, "accepted")
except HTTPException as e:
    check("a batch beyond the cap is refused rather than buffered",
          e.status_code == 413 and str(media.MAX_BULK) in str(e.detail), f"{e.status_code} {e.detail}")

check("the framing the browser reads is versioned and stable",
      media.BULK_VERSION == 1 and media.BULK_MIME == "application/x-mlo-bulk", media.BULK_MIME)

# --------------------------------------------------------------------------- #
print("\nthe download rendition: what a downloaded copy holds")
# --------------------------------------------------------------------------- #
# The shipped default is `copy`: the cached bytes are the file's own, so a
# track is never downloaded in a codec it is not already in — and the bulk
# route can still carry a whole queue.
check("a downloaded copy is the file's own codec by default",
      DEFAULT_CONFIG["download_codec"] == "copy" and DEFAULT_CONFIG["download_bitrate"] == 0)
check("the download rate spans what the library's does (0 = the codec's own)",
      _INT_RANGES["download_bitrate"] == (0, 512))
check("and the player streams rather than playing the copy by default",
      DEFAULT_CONFIG["playback_source"] == "stream")

from mlo.containers import codec_args  # noqa: E402

check("a re-encode carries the codec's own arguments and the configured rate",
      codec_args("mp3", None, 192) == ["-c:a", "libmp3lame", "-f", "mp3", "-b:a", "192k"],
      str(codec_args("mp3", None, 192)))
check("ogg takes libvorbis' own 0-10 scale, not kbps",
      codec_args("ogg", None, 8)[-2:] == ["-q:a", "8"], str(codec_args("ogg", None, 8)))
check("0 means the codec's own default, not its minimum",
      codec_args("mp3", None, 0)[-1] == "320k", str(codec_args("mp3", None, 0)))
check("a lossless target takes no rate at all",
      codec_args("flac", None, 320) == ["-c:a", "flac", "-f", "flac"],
      str(codec_args("flac", None, 320)))
check("and no compression level: the encoder's own is what ships",
      "-compression_level" not in codec_args("flac", None, 0))

try:
    media.download_rendition("/music/x.flac", "nonsense")
    check("an unknown download codec is refused, not guessed", False, "accepted")
except HTTPException as e:
    check("an unknown download codec is refused, not guessed",
          e.status_code == 400, f"{e.status_code} {e.detail}")

# The bulk route frames each file's SIZE up front, so it cannot carry a
# re-encode (no size until it is done) — it must say so rather than hand back
# the library's own bytes to a client told it is getting a smaller rendition.
real_load_config = media.load_config
try:
    media.load_config = lambda: {"download_codec": "opus", "download_bitrate": 128,
                                 "download_concurrency": 3}
    try:
        media.media_bulk(media.BulkRequest(paths=["/music/x.flac"]))
        check("the bulk route refuses while downloads are re-encoded", False, "accepted")
    except HTTPException as e:
        check("the bulk route refuses while downloads are re-encoded",
              e.status_code == 409 and "download_codec" in str(e.detail),
              f"{e.status_code} {e.detail}")
finally:
    media.load_config = real_load_config

print()
if FAILED:
    print(f"{len(FAILED)} check(s) FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all checks passed")
