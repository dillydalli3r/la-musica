"""AcoustID / Chromaprint fingerprint lookup (stdlib only).

Verified against https://acoustid.org/webservice (2026-09-16):

    POST https://api.acoustid.org/v2/lookup
      client=<application API key>   (required)
      duration=<seconds>
      fingerprint=<fpcalc fingerprint>
      meta=recordings releasegroups compress
    -> {"status": "ok", "results": [
          {"id": ..., "score": 0.9,
           "recordings": [{"id", "title", "duration",
                           "artists": [{"id", "name"}],
                           "releasegroups": [{"id", "title", "type"}]}]}]}
    rate limit: 3 requests/second.

Submissions are a second, independent operation (read 2026-09-21):

    POST https://api.acoustid.org/v2/submit
      client=<application API key>   (required - the same key as lookup)
      user=<the USER key>            (required - a different key, per account)
      duration.N=<seconds>  fingerprint.N=<fpcalc fingerprint>
      mbid.N=<MusicBrainz recording>  track.N / artist.N / album.N /
      albumartist.N / year.N / trackno.N / discno.N   (all optional)
      source.N=1 (tagged) | 3 (fingerprint only)
    -> {"status": "ok", "submissions": [{"index": n, "id": ..., "status": "pending"}]}
    at most 100 fingerprints per call, processed asynchronously.

The fingerprints themselves are produced by Chromaprint's `fpcalc`
(github.com/acoustid/chromaprint, pinned in mlo/fetchdeps.py). No API key is
hard-coded anywhere - it comes from config (`acoustid_api_key`).

Live checks (2026-09-16): `GET https://api.github.com/repos/acoustid/chromaprint/
releases` -> latest tag `v1.6.1`, asset `chromaprint-fpcalc-1.6.1-windows-x86_64
.zip` containing a single `chromaprint-fpcalc-1.6.1-windows-x86_64/fpcalc.exe`.
`fpcalc -json` prints `{"duration": 5.0, "fingerprint": "AQAAE0mU..."}`.

Nothing here raises at a caller, and nothing here is silent. Every stage
answers with ``{"ok": bool, "code": str, "reason": str, ...}``, where `code` is
one of the CODES below and `reason` is the sentence a UI can show verbatim:

    no_api_key / no_user_key / fpcalc_missing / no_tracks   what the config lacks
    not_audio / no_fingerprint / too_short    the file carries no fingerprintable audio (SKIPS)
    file_missing                              the path handed in is not a file
    fpcalc_failed                             fpcalc could not be run, or it failed on the file
    lookup_failed                             network, timeout, HTTP status, rejected request
    bad_response                              HTTP 200 whose body is not a usable AcoustID answer
    no_match                                  the service answered correctly and knew nothing
    conflict                                  the fingerprint and the tags name different releases

A tag WRITE answers in the same shape and names its own causes (`write_tags`),
because "0 tagged" on its own reads as "the files carry none of the tag
families it targets" - which is what the wizard used to tell users about files
it had just matched:

    unsupported_container   .ape/.wv/.dsf/... have no tag writer at all (mlo.audio)
    unreadable_file         the container has a writer but the file could not be read
    no_recording_id / no_fingerprint   the PAIR is never split: a lone
                            ACOUSTID_ID fails the grader's pair check, so a
                            missing fingerprint is reported, never written
                            around
    write_failed / verify_failed   the writer refused, or the pair did not read
                            back off the file afterwards

The submission contract (`submit_files`, `run_submit_fingerprints`) is the one
place this module gives something to somebody else's database, so it is
stricter than `v2/submit` itself allows. AcoustID accepts a fingerprint with no
metadata at all, but it is not useful (the service says so) and this app only
ever sends what a file STATES, so an entry needs BOTH halves:

    * a MusicBrainz recording id — the `mbid.N` a submission links the
      fingerprint to. MusicBrainz never receives a raw fingerprint; the link IS
      the recording id. Read off the file by `_recording_identity` (ACOUSTID_ID,
      MUSICBRAINZ_TRACKID, or the recording MBID this app's naming script wrote
      into the name), never guessed.
    * a fingerprint — the tag when the file carries one, else taken from the
      AUDIO by fpcalc (local, no key): the pair a CD rip needs is exactly the
      one no lookup could have given it.

and it is never sent twice: `pair_known` asks the lookup endpoint whether the
service already links that fingerprint to that recording, and the pairs this
app has already handed over are recorded under `<music>/.mlo/data` (the service
imports asynchronously, so "pending" a minute ago is already in flight). With
`acoustid_enabled` off nothing is read, fingerprinted or sent — and with no
`acoustid_user_key` the answer is the named NO_USER_KEY refusal, never a
request: the application key looks up and can never submit.

A fingerprint that could not be taken (`fpcalc_failed`) and a lookup that
could not be answered (`lookup_failed` / `bad_response`) are their OWN codes:
they are never reported as NO_MATCH, which means exactly "the service answered
and had no candidate above `acoustid_min_score`". `match_release` sorts the
same codes into an album verdict: the SKIPS in `skips`, everything else
fingerprint/lookup-shaped in `failures` (which make the verdict "error"), and
NO_MATCH counted as evidence, not as a failure.
"""

import hashlib
import json
import math
import os
import re
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .atomic import write_bytes
from .paths import LIB_AUDIO_EXTS, tools_dirs
from .stats import (is_audio_file, new_stats, _collect_targets, _find_albums,
                    _make_pbar, _pbar_skip, _pbar_update, worker_count)
from .subproc import run_tool
from .ui import Color, c, log, print_header

API_URL = "https://api.acoustid.org/v2/lookup"
# Submission is a SEPARATE endpoint and a separate credential: `client` is the
# same application key, `user` is the key AcoustID hands a signed-in person.
# An application key cannot submit anything, so a lookup-only config is a named
# refusal here, never a broken request.
SUBMIT_API_URL = "https://api.acoustid.org/v2/submit"
_UA = "la-musica/2.1"
_META = "recordings releasegroups compress"
_FPCALC_TIMEOUT = 120
_HTTP_TIMEOUT = 30
# AcoustID allows 3 requests/second; stay just under it.
_MIN_INTERVAL = 0.34
# AcoustID takes at most this many fingerprints in one v2/submit call, so a
# larger set is split into batches of this size (a submission carries its own
# `index` back, which is how a batch's answers are matched to their tracks).
MAX_SUBMIT = 100
# `source.N` in a submission: 1 = the fingerprint came from a file whose tags
# already named the recording, 3 = fingerprint only, no metadata. AcoustID's
# own vocabulary (the tquery sources libmusicbrainz defines).
SOURCE_TAGGED = 1
SOURCE_FINGERPRINT = 3
# A whole album is fingerprinted at most this far - the tail of a long album
# adds no release-group evidence worth the extra lookups.
MAX_TRACKS = 12
# Below this the fingerprint is noise: AcoustID cannot identify a clip this
# short, so the track is SKIPPED with a reason instead of being fingerprinted,
# looked up and then silently dropped.
MIN_DURATION = 5.0

# Outcome codes (see the module docstring).
OK = "ok"
DISABLED = "disabled"
NO_API_KEY = "no_api_key"
# Submitting needs the USER key on top of the application key; a config with
# only the latter can look up but can never give AcoustID anything back.
NO_USER_KEY = "no_user_key"
NO_FPCALC = "fpcalc_missing"
NO_TRACKS = "no_tracks"
NO_FILE = "file_missing"
NOT_AUDIO = "not_audio"
NO_FINGERPRINT = "no_fingerprint"
NO_DURATION = "no_duration"
TOO_SHORT = "too_short"
FPCALC_FAILED = "fpcalc_failed"
LOOKUP_FAILED = "lookup_failed"
BAD_RESPONSE = "bad_response"
NO_MATCH = "no_match"
CONFLICT = "conflict"
INTERNAL = "internal"

# Tag-write outcomes (`write_tags`). They name the CAUSE, because "nothing was
# written" on its own reads as "the file carries none of the tag families it
# targets" - which is exactly what the wizard used to say for a .wv, a .dsf or
# a write that failed.
NO_RECORDING_ID = "no_recording_id"
UNSUPPORTED = "unsupported_container"
UNREADABLE = "unreadable_file"
WRITE_FAILED = "write_failed"
VERIFY_FAILED = "verify_failed"

# Submission outcomes (`submit_files`), one per track in its `results`. The
# service's own answer for an entry it took is an id plus a status
# (asynchronous: "pending"); these are what THIS app reports, so a report can
# say "already known" for a pair AcoustID already holds without inventing a
# service answer for a request that was deliberately never sent.
ACCEPTED = "accepted"          # the service took this entry (see id / status)
ALREADY_KNOWN = "already_known"  # the pair is already there (or already sent)
REJECTED = "rejected"          # the service refused the batch it was in
OUTCOME_SKIPPED = "skipped"    # nothing to send - `code`/`reason` say why
SUBMISSION_OUTCOMES = frozenset({ACCEPTED, ALREADY_KNOWN, REJECTED,
                                 OUTCOME_SKIPPED})

_NOTES = {
    DISABLED: "AcoustID disabled in settings",
    NO_API_KEY: "no API key",
    NO_USER_KEY: "no user API key",
    NO_FPCALC: "fpcalc not installed",
    NO_TRACKS: "no audio files to fingerprint",
}
# Skips: the file itself carries no fingerprintable audio, so the album's vote
# goes on without it. Failures: the tooling or the service could not answer -
# the caller must surface those, they are never a verdict about the audio.
SKIP_CODES = frozenset({NOT_AUDIO, NO_FINGERPRINT, TOO_SHORT})
# A lookup that failed the same way will fail for the next ten tracks too: one
# timeout must not cost the import twelve of them.
FATAL_LOOKUP_CODES = frozenset({LOOKUP_FAILED, BAD_RESPONSE})

# Video containers the download side counts as audio (`_AUDIO_EXTS` includes
# .mp4/.mka): when fpcalc fails on one, "no audio stream" is the honest
# reason, not "the fingerprint tool is broken".
_VIDEO_EXTS = frozenset({".mp4", ".m4v", ".mkv", ".webm", ".mka", ".avi", ".mov",
                         ".mpg", ".mpeg", ".wmv", ".flv", ".ogv", ".3gp", ".vob",
                         ".ts"})

_rate_lock = threading.Lock()
_last_call = 0.0


# --------------------------------------------------------------------------- #
# fpcalc
# --------------------------------------------------------------------------- #
def fpcalc_path(cfg=None):
    """Path to the fpcalc executable, or None.

    Order: explicit `acoustid_fpcalc_path` in cfg, then a versioned
    chromaprint* install in a tools folder (the folder the Dependencies
    installer writes and mlo.tools reads — the music folder's .mlo/tools, plus
    the app's pre-move .dependencies, see mlo.paths.tools_dirs), then PATH.
    """
    cfg = cfg or {}
    explicit = cfg.get("acoustid_fpcalc_path")
    if explicit and os.path.isfile(explicit):
        return explicit

    for root in tools_dirs():
        if not os.path.isdir(root):
            continue
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            if not entry.lower().startswith("chromaprint"):
                continue
            for name in ("fpcalc.exe", "fpcalc"):
                cand = os.path.join(root, entry, name)
                if os.path.isfile(cand):
                    return cand

    return shutil.which("fpcalc") or shutil.which("fpcalc.exe")


def _short(text, limit=160):
    """The first line of a tool/service message, whitespace collapsed."""
    first = ""
    for line in str(text or "").splitlines():
        if line.strip():
            first = line.strip()
            break
    if not first:
        return ""
    return first if len(first) <= limit else first[: limit - 3] + "..."


def _parse_fpcalc(out):
    """fpcalc output -> (duration, fingerprint), or None when unreadable."""
    out = (out or "").strip()
    if out.startswith("{"):
        try:
            data = json.loads(out)
            return float(data["duration"]), str(data["fingerprint"] or "")
        except Exception:
            pass  # fall through to the pre-1.4 key=value form
    values = {}
    for line in out.splitlines():
        key, _, val = line.partition("=")
        values[key.strip().upper()] = val.strip()
    if "FINGERPRINT" not in values and "DURATION" not in values:
        return None
    try:
        return float(values.get("DURATION") or 0), values.get("FINGERPRINT", "")
    except (TypeError, ValueError):
        return None


def fingerprint(path, cfg=None):
    """Fingerprint one file with fpcalc.

    -> {"ok", "code", "reason", "duration", "fingerprint"}: a taken
    fingerprint, or the named reason it could not be taken.
    """
    exe = fpcalc_path(cfg)
    if not exe:
        return _result(False, NO_FPCALC, _NOTES[NO_FPCALC],
                       duration=0.0, fingerprint="")
    if not path or not os.path.isfile(path):
        return _result(False, NO_FILE, f"file not found: {path}",
                       duration=0.0, fingerprint="")
    base = os.path.basename(str(path))
    empty = {"duration": 0.0, "fingerprint": ""}

    try:
        res = run_tool(
            [exe, "-json", "-length", "120", path],
            capture_output=True, text=True, timeout=_FPCALC_TIMEOUT,
        )
    except Exception as e:
        # fpcalc that cannot even start (a bad acoustid_fpcalc_path, a
        # non-executable file, a sandbox that forbids subprocesses) has to say
        # so: reporting "no match" would blame the audio for a broken tool.
        return _result(False, FPCALC_FAILED,
                       f"fpcalc could not run ({exe}): {_short(e)}", **empty)

    if res.returncode != 0:
        err = _short(getattr(res, "stderr", "") or "")
        # fpcalc/ffmpeg say it in their own words when a file (a video
        # container, or a mislabelled track) has no decodable audio: that is a
        # SKIP - there is no fingerprint to take - not a broken tool.
        if (os.path.splitext(str(path))[1].lower() in _VIDEO_EXTS
                or "audio stream" in err.casefold()):
            return _result(False, NOT_AUDIO,
                           f"{base} has no fingerprintable audio stream"
                           + (f": {err}" if err else ""), **empty)
        return _result(False, FPCALC_FAILED,
                       f"fpcalc failed on {base} (exit {res.returncode}"
                       + (f": {err})" if err else ")"), **empty)

    parsed = _parse_fpcalc(getattr(res, "stdout", "") or "")
    if parsed is None:
        return _result(False, FPCALC_FAILED,
                       f"fpcalc printed output that could not be read for {base}: "
                       f"{_short(res.stdout)}", **empty)
    duration, value = parsed
    if not value:
        return _result(False, NO_FINGERPRINT,
                       f"fpcalc found no audio to fingerprint in {base}", **empty)
    if duration < MIN_DURATION:
        return _result(False, TOO_SHORT,
                       f"{base} is too short to identify ({duration:.1f}s, "
                       f"the fingerprint needs {MIN_DURATION:.0f}s)", **empty)
    return _result(True, OK, "", duration=duration, fingerprint=value)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def _result(ok, code, reason="", **extra):
    out = {"ok": bool(ok), "code": code, "reason": reason}
    out.update(extra)
    return out


def _throttle():
    """Keep the module's request rate under AcoustID's 3 req/s."""
    global _last_call
    with _rate_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _service_error(data):
    """AcoustID's own error sentence from an error body/payload, or "".

    The documented error shape is {"status": "error", "error": {"code": 4,
    "message": "invalid API key"}}; a proxy's HTML 404 is not that, and is
    reported by its HTTP status instead.
    """
    if isinstance(data, (bytes, bytearray)):
        try:
            data = json.loads(bytes(data).decode("utf-8", "replace"))
        except ValueError:
            return ""
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        msg = str(err.get("message") or "").strip()
        code = err.get("code")
        if msg and code is not None:
            return f"{msg} (code {code})"
        return msg
    if isinstance(err, str):
        return err.strip()
    return ""


def _post(url, form, what):
    """One form POST to an AcoustID endpoint -> {"ok", "code", "reason", "payload"}.

    The transport half of both operations: build, throttle, send, decode, and
    turn every transport/HTTP/body failure into a named code with `what`
    ("lookup" / "submission") in the sentence. `payload` is the service's JSON
    object on success — the caller validates the part of it its own operation
    promises (`results` / `submissions`).
    """
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"User-Agent": _UA,
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    _throttle()
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = _service_error(e.read())
        except Exception:
            detail = ""
        return _result(False, LOOKUP_FAILED,
                       f"AcoustID {what} failed: HTTP {e.code}"
                       + (f" - {detail}" if detail else f" {_short(getattr(e, 'reason', ''))}"),
                       payload=None)
    except (socket.timeout, TimeoutError):
        return _result(False, LOOKUP_FAILED,
                       f"AcoustID {what} timed out after {_HTTP_TIMEOUT}s",
                       payload=None)
    except (urllib.error.URLError, OSError) as e:
        return _result(False, LOOKUP_FAILED,
                       f"AcoustID {what} failed: {_short(e)}", payload=None)

    try:
        payload = json.loads(bytes(raw).decode("utf-8", "replace"))
    except ValueError:
        return _result(False, BAD_RESPONSE,
                       f"AcoustID returned a body that is not JSON "
                       f"({_short(raw)})", payload=None)
    if not isinstance(payload, dict):
        return _result(False, BAD_RESPONSE,
                       "AcoustID returned JSON that is not an object", payload=None)
    if payload.get("status") != "ok":
        detail = _service_error(payload)
        return _result(False, BAD_RESPONSE,
                       "AcoustID answered with an error"
                       + (f": {detail}" if detail else f" (status {payload.get('status')!r})"),
                       payload=None)
    return _result(True, OK, "", payload=payload)


def _request(cfg, fp):
    """One POST to the lookup endpoint.

    -> {"ok", "code", "reason", "results"}; `results` is the service's list
    on success, and every transport, HTTP and body failure is a named code.
    """
    key = str((cfg or {}).get("acoustid_api_key") or "").strip()
    if not key:
        return _result(False, NO_API_KEY, _NOTES[NO_API_KEY], results=[])
    got = _post(API_URL, {
        "client": key,
        "duration": int(round(float(fp.get("duration") or 0))),
        "fingerprint": fp.get("fingerprint") or "",
        "meta": _META,
    }, "lookup")
    if not got["ok"]:
        return _result(False, got["code"], got["reason"], results=[])
    results = got["payload"].get("results")
    if not isinstance(results, list):
        return _result(False, BAD_RESPONSE,
                       "AcoustID's response carried no result list", results=[])
    return _result(True, OK, "", results=results)


def parse_payload(payload, min_score=0.0):
    """AcoustID JSON -> candidate rows, best score first (pure parser).

    Rows: {"score", "recording_id", "title", "artists", "release_group_id",
    "release_group_title", "release_group_type"}. An unusable payload is an
    empty list; the named reason lives in `_request`, not here.
    """
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []

    rows = []
    for result in results:
        if not isinstance(result, dict):
            continue
        try:
            score = float(result.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if score < min_score:
            continue
        for rec in result.get("recordings") or []:
            if not isinstance(rec, dict):
                continue
            groups = [g for g in (rec.get("releasegroups") or [])
                      if isinstance(g, dict)]
            group = groups[0] if groups else {}
            rows.append({
                "score": score,
                "recording_id": str(rec.get("id") or ""),
                "title": str(rec.get("title") or ""),
                "artists": [str(a.get("name")) for a in (rec.get("artists") or [])
                            if isinstance(a, dict) and a.get("name")],
                "release_group_id": str(group["id"]) if group.get("id") else None,
                "release_group_title": group.get("title") or None,
                "release_group_type": group.get("type") or None,
            })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def min_score(cfg):
    try:
        return float((cfg or {}).get("acoustid_min_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def lookup(cfg, path):
    """Fingerprint one track and ask AcoustID what it is.

    -> {"ok", "code", "reason", "rows", "fingerprint", "duration"}. `rows` are
    the candidate rows, best score first, each carrying the fingerprint it was
    matched from (accepting a match writes it as ACOUSTID_FINGERPRINT, and
    re-running fpcalc for that would double the cost of every import).
    """
    chk = check(cfg)
    if not chk["available"]:
        return _result(False, chk["code"], chk["reason"],
                       rows=[], fingerprint="", duration=0.0)
    fp = fingerprint(path, cfg)
    if not fp["ok"]:
        return _result(False, fp["code"], fp["reason"],
                       rows=[], fingerprint="", duration=0.0)
    carry = {"fingerprint": fp["fingerprint"], "duration": fp["duration"]}
    resp = _request(cfg, fp)
    if not resp["ok"]:
        return _result(False, resp["code"], resp["reason"], rows=[], **carry)
    rows = parse_payload({"status": "ok", "results": resp["results"]},
                         min_score(cfg))
    if not rows:
        return _result(False, NO_MATCH,
                       f"AcoustID knows no recording above score "
                       f"{min_score(cfg):.2f} for this track", rows=[], **carry)
    for row in rows:
        row["fingerprint"] = fp["fingerprint"]
    return _result(True, OK, "", rows=rows, **carry)


# A Chromaprint fingerprint of a 30 s 440 Hz tone, taken once with the bundled
# fpcalc (v1.6.1, -length 120 - the same call the import makes). It matches no
# recording, which is exactly the point: it lets the live service be asked
# about the KEY with no audio file and no fpcalc, because the answer can only
# be about the key ("invalid API key") or about nothing at all (no match).
PROBE_FINGERPRINT = ("AQAA3UmUaEkSZSoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
                    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
                    "AAA")
PROBE_DURATION = 30


def verify_key(cfg=None, fingerprint=None, duration=None):
    """Does the live service accept this API key? No audio, no fpcalc.

    One lookup carrying PROBE_FINGERPRINT (or the caller's own fingerprint,
    when it has one): a working key answers "ok" - a no-match is a fine answer,
    the key was accepted - and a rejected one answers with the service's own
    sentence ("HTTP 400 - invalid API key (code 4)") verbatim, which is the
    whole reason this exists.

    -> {"ok", "code", "reason", "results"}: `ok` means the KEY was ACCEPTED,
    `results` is how many candidates the probe matched (normally 0), and `code`
    is no_api_key / disabled when the config cannot try at all, or
    lookup_failed / bad_response when the service refused or mis-answered.
    Rate-limited like every other call here, bounded by _HTTP_TIMEOUT, and
    never raising.
    """
    chk = check(cfg, need_fpcalc=False)
    if not chk["available"]:
        return _result(False, chk["code"], chk["reason"], results=0)
    fp = {"duration": PROBE_DURATION, "fingerprint": PROBE_FINGERPRINT}
    if fingerprint:
        fp = {"duration": duration or PROBE_DURATION,
              "fingerprint": str(fingerprint)}
    resp = _request(cfg, fp)
    if not resp["ok"]:
        return _result(False, resp["code"], resp["reason"], results=0)
    return _result(True, OK, "", results=len(resp["results"]))


def write_tags(path, recording_id, fingerprint=None, cfg=None):
    """Write the AcoustID identity tags for one accepted match.

    `ACOUSTID_ID` (the MusicBrainz recording) and `ACOUSTID_FINGERPRINT` are
    what Picard writes, and what the opt-in grading check looks for. The two go
    in as ONE write (a second save would remux a video container twice), and
    the PAIR is never split: the grader fails a file carrying one half alone
    (`mlo.grader`), so a missing fingerprint is reported instead of an ID being
    written by itself.

    -> {"path", "ok", "code", "reason", "output"}. `ok` is only true once both
    tags READ BACK off the file — a .wv, a .dsf or a .ape has no writer at all
    (`AudioFile.kind is None`), and reporting that file as tagged is exactly
    the silent lie this shape exists to end. `code` is one of the write codes
    (unsupported_container / unreadable_file / no_recording_id /
    no_fingerprint / write_failed / verify_failed), `reason` is the sentence a
    UI shows verbatim, and `output` names the file that now holds the data when
    a video container was remuxed (that can change the extension). Never
    raises.
    """
    out = {"path": path, "ok": False, "code": INTERNAL, "reason": "",
           "output": None}
    if not str(recording_id or "").strip():
        out.update(code=NO_RECORDING_ID,
                   reason="no AcoustID recording id to write")
        return out
    if not str(fingerprint or "").strip():
        out.update(code=NO_FINGERPRINT,
                   reason=("refusing to write ACOUSTID_ID without "
                           "ACOUSTID_FINGERPRINT (the fingerprint is missing)"))
        return out
    if not path or not os.path.isfile(path):
        out.update(code=NO_FILE, reason=f"no such file: {path}")
        return out
    base = os.path.basename(str(path))
    try:
        from .audio import AudioFile

        af = AudioFile(path)
        if af.kind is None:
            # `server.soulseek_auto._AUDIO_EXTS` fingerprints a dozen formats
            # (ape/wv/dsf/dff/alac/oga/mka/wma/shn/tta/mpc/mp2) that mlo.audio
            # has no writer for: a match on one of those is real, and the tag
            # can only ever be written by converting the file first.
            out.update(code=UNSUPPORTED,
                       reason=(f"unsupported container: {os.path.splitext(str(path))[1].lower()}"
                               f" (this app cannot write tags to {base})"))
            return out
        if af.audio is None:
            out.update(code=UNREADABLE,
                       reason=(f"cannot read {base}: "
                               f"{af.error or 'no tag reader for this file'}"))
            return out

        tags = {"ACOUSTID_ID": str(recording_id),
                "ACOUSTID_FINGERPRINT": str(fingerprint)}
        if af.is_video:
            # One ffmpeg stream copy for both tags; set_tag would rewrite the
            # whole container once per tag.
            ok = bool(af.set_video_tags(tags))
        else:
            # Batching, the mlo/audio.py way: defer_save(on) marks the writes
            # dirty and defer_save(False) flushes them in a single container
            # write, whose result is the thing reported.
            af.defer_save(True)
            ok = bool(af.set_tag("ACOUSTID_ID", tags["ACOUSTID_ID"]))
            ok = bool(af.set_tag("ACOUSTID_FINGERPRINT",
                                 tags["ACOUSTID_FINGERPRINT"])) and ok
            ok = bool(af.defer_save(False)) and ok
        if not ok:
            out.update(code=WRITE_FAILED,
                       reason=(f"could not write {base}: "
                               f"{af.error or 'the tag writer refused'}"))
            return out

        # Read the pair BACK off the file: a writer that answered True and
        # stored nothing (the other spelling of the tag, a container that
        # dropped it) must not be reported as a tagged file.
        got_id = str(af.get_tag("ACOUSTID_ID") or "").strip()
        got_fp = str(af.get_tag("ACOUSTID_FINGERPRINT") or "").strip()
        if got_id != tags["ACOUSTID_ID"] or got_fp != tags["ACOUSTID_FINGERPRINT"]:
            out.update(
                code=VERIFY_FAILED,
                reason=(f"{base} does not carry the pair it was written: "
                        f"ACOUSTID_ID={got_id or 'missing'}, ACOUSTID_FINGERPRINT="
                        f"{('present' if got_fp else 'missing')}"))
            return out
        out.update(ok=True, code=OK, reason="",
                   output=af.tag_output_path if af.container_changed else None)
        return out
    except Exception as e:
        out.update(code=WRITE_FAILED, reason=f"could not write {base}: {e}")
        return out


# --------------------------------------------------------------------------- #
# Submission (v2/submit)
# --------------------------------------------------------------------------- #
def check_submit(cfg=None):
    """{"available", "code", "reason"} — why fingerprints can(not) be sent.

    Submitting needs BOTH keys: `client` is the application key lookups already
    use, `user` is the one AcoustID hands a signed-in person — a different key,
    and the only thing that proves whose submission it is. `submit_fingerprints`
    and `verify_user_key` both start here, so a missing key is a named answer
    and never a request the service has to refuse.
    """
    cfg = cfg or {}
    if not cfg.get("acoustid_enabled"):
        return _result(False, DISABLED, _NOTES[DISABLED], available=False)
    if not str(cfg.get("acoustid_api_key") or "").strip():
        return _result(False, NO_API_KEY, _NOTES[NO_API_KEY], available=False)
    if not str(cfg.get("acoustid_user_key") or "").strip():
        return _result(False, NO_USER_KEY, _NOTES[NO_USER_KEY], available=False)
    return _result(True, OK, "", available=True)


def _submit_form(user, items):
    """The indexed form fields for one batch of submissions.

    Indices are per batch (0..n-1), which is the shape the service documents,
    and its answer's `index` is therefore a position in this batch.
    """
    form = {"client": str(user["client"]), "user": str(user["user"])}
    for n, item in enumerate(items):
        form[f"duration.{n}"] = int(round(float(item.get("duration") or 0)))
        form[f"fingerprint.{n}"] = str(item.get("fingerprint") or "")
        mbid = str(item.get("mbid") or item.get("recording_id") or "").strip()
        if mbid:
            form[f"mbid.{n}"] = mbid
        for key, field in (("track", "track"), ("artist", "artist"),
                           ("album", "album"),
                           ("album_artist", "albumartist"),
                           ("year", "year"), ("track_no", "trackno"),
                           ("disc_no", "discno")):
            value = str(item.get(key) or "").strip()
            if value:
                form[f"{field}.{n}"] = value
        # 1 = the fingerprint came from a file whose tags named the recording,
        # 3 = fingerprint only. A source may be forced by the caller.
        try:
            source = int(item.get("source"))
        except (TypeError, ValueError):
            source = SOURCE_TAGGED if mbid else SOURCE_FINGERPRINT
        form[f"source.{n}"] = source
    return form


def submit_fingerprints(cfg, items, require_mbid=True):
    """Give AcoustID the fingerprints + metadata a set of tracks carries.

    `items` are `{"path", "fingerprint", "duration", "recording_id"/"mbid",
    "track", "artist", "album", "album_artist", "year", "track_no",
    "disc_no"}` — everything but `path`/`fingerprint`/`duration` is optional,
    and the caller reads them off the files (nothing is fingerprinted here).
    A track without a fingerprint or without a duration is SKIPPED with its own
    code and reason before any request: the service cannot store half an entry,
    and a guessed duration would put a wrong one in a public database.

    `require_mbid` is the submission's own rule, not the service's: AcoustID
    accepts a fingerprint with no metadata, but the pair it stores is a
    FINGERPRINT + a MusicBrainz RECORDING ID (MusicBrainz itself never sees a
    fingerprint), so an entry that names no recording is refused here with
    NO_RECORDING_ID rather than published as a fingerprint nothing points at.
    `verify_user_key` is the one caller that turns it off, and only for its
    no-metadata credential probe (see there).

    Batches of MAX_SUBMIT (AcoustID's own per-call limit) go out in order, and
    the service's answer is reported as it gives it: `submitted` counts the
    submissions it accepted (each with its id and status), a refused user key
    is its own sentence ("invalid user API key (code 8)") carried verbatim, and
    the first refused batch stops the rest — ten more batches cannot be
    accepted by a key the first one was refused for.

    -> {"ok", "code", "reason", "submitted", "failed", "skips", "submissions",
        "results", "batches"}. `results` carries one row per track that went
    anywhere, in order: {"path", "outcome" (ACCEPTED / REJECTED /
    OUTCOME_SKIPPED), "code", "reason", "id", "status", "recording_id",
    "index"}. Never raises.
    """
    cfg = cfg or {}
    items = [dict(item) for item in (items or []) if isinstance(item, dict)]
    ready, skips, results = [], [], []
    for item in items:
        if not str(item.get("fingerprint") or "").strip():
            skips.append({"path": item.get("path"), "code": NO_FINGERPRINT,
                          "reason": "no fingerprint to submit"})
            results.append(_sub_row(item, OUTCOME_SKIPPED,
                                    code=NO_FINGERPRINT,
                                    reason="no fingerprint to submit"))
            continue
        try:
            duration = float(item.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            skips.append({"path": item.get("path"), "code": NO_DURATION,
                          "reason": "the track's duration is unknown, so its "
                                    "fingerprint cannot be submitted"})
            results.append(_sub_row(item, OUTCOME_SKIPPED, code=NO_DURATION,
                                    reason="the track's duration is unknown, so "
                                           "its fingerprint cannot be submitted"))
            continue
        if require_mbid and not str(item.get("mbid")
                                    or item.get("recording_id") or "").strip():
            reason = ("the file names no MusicBrainz recording id to submit — "
                      "AcoustID stores a fingerprint WITH the recording it is, "
                      "and this app does not send a fingerprint nothing points at")
            skips.append({"path": item.get("path"), "code": NO_RECORDING_ID,
                          "reason": reason})
            results.append(_sub_row(item, OUTCOME_SKIPPED,
                                    code=NO_RECORDING_ID, reason=reason))
            continue
        item["duration"] = duration
        ready.append(item)

    chk = check_submit(cfg)
    if not chk["available"]:
        return _result(False, chk["code"], chk["reason"], submitted=0,
                       failed=len(ready), skips=skips, submissions=[],
                       results=results, batches=[], available=False)
    if not ready:
        return _result(False, NO_TRACKS,
                       "no track carries a fingerprint to submit",
                       submitted=0, failed=0, skips=skips, submissions=[],
                       results=results, batches=[], available=True)

    who = {"client": str(cfg.get("acoustid_api_key")).strip(),
           "user": str(cfg.get("acoustid_user_key")).strip()}
    batches, failed = [], 0
    code, reason = OK, ""
    for start in range(0, len(ready), MAX_SUBMIT):
        batch = ready[start:start + MAX_SUBMIT]
        got = _post(SUBMIT_API_URL, _submit_form(who, batch), "submission")
        batches.append({"ok": got["ok"], "code": got["code"],
                        "reason": got["reason"], "count": len(batch)})
        if not got["ok"]:
            # The service's own sentence IS the report for every entry of the
            # batch, and the batches after this one are not sent: a key the
            # service just refused will refuse them too.
            code, reason = got["code"], got["reason"]
            for i, item in enumerate(batch):
                results.append(_sub_row(item, REJECTED, index=start + i,
                                        code=code, reason=reason))
            failed += len(ready) - start
            break
        submissions = got["payload"].get("submissions")
        if not isinstance(submissions, list):
            code, reason = (BAD_RESPONSE,
                            "AcoustID answered ok but carried no submission list")
            batches[-1].update(ok=False, code=code, reason=reason)
            for i, item in enumerate(batch):
                results.append(_sub_row(item, REJECTED, index=start + i,
                                        code=code, reason=reason))
            failed += len(ready) - start
            break
        answers = {}
        for sub in submissions:
            if not isinstance(sub, dict):
                continue
            try:
                answers[int(sub.get("index", 0))] = sub
            except (TypeError, ValueError):
                continue
        for i, item in enumerate(batch):
            sub = answers.get(i)
            if sub is None:
                # The batch was accepted but this entry has no answer: report
                # it as unwritten rather than as a tag that landed.
                failed += 1
                code, reason = (BAD_RESPONSE,
                                f"AcoustID accepted the batch but answered for "
                                f"{len(answers)} of {len(batch)} submissions")
                results.append(_sub_row(item, REJECTED, index=start + i,
                                        code=code, reason=reason))
                continue
            results.append(_sub_row(item, ACCEPTED, index=start + i,
                                    id=sub.get("id"), status=sub.get("status")))
    landed = [row for row in results
              if row["outcome"] == ACCEPTED and row.get("id") is not None]
    return _result(not failed and code == OK, code, reason,
                   submitted=len(landed), failed=failed, skips=skips,
                   submissions=landed, results=results, batches=batches,
                   available=True)


def _sub_row(item, outcome, index=None, id=None, status=None, code=OK,
             reason=""):
    """One track's own line of a submission report (`submit_fingerprints`)."""
    return {"path": item.get("path"), "outcome": outcome, "index": index,
            "id": id, "status": status, "code": code, "reason": reason,
            "recording_id": str(item.get("mbid")
                                or item.get("recording_id") or "").strip()}


def pair_known(cfg, fingerprint, duration, recording_id):
    """Does AcoustID already link this fingerprint to this recording?

    The dedupe half of a submission, and the only honest place to ask it:
    `v2/submit` reports only that a batch was TAKEN, so "the service already
    has this pair" is a question for the lookup endpoint — `meta=recordings`
    is the recordings a fingerprint's cluster is linked to, at EVERY score.
    That is deliberately not the configured `acoustid_min_score`: this asks
    what the database holds, not what the app would show a user as a match.

    -> {"ok", "code", "reason", "known", "rows"}. `ok` False means the question
    could not be asked at all (no key, network, unusable body) — `known` is
    then NOT a verdict, and `submit_files` refuses to send blind. Never raises.
    """
    rid = str(recording_id or "").strip()
    if not rid:
        return _result(False, NO_RECORDING_ID,
                       "no MusicBrainz recording id to look for",
                       known=False, rows=0)
    if not str(fingerprint or "").strip():
        return _result(False, NO_FINGERPRINT, "no fingerprint to ask about",
                       known=False, rows=0)
    try:
        seconds = float(duration or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds <= 0:
        return _result(False, NO_DURATION,
                       "the track's duration is unknown, so its fingerprint "
                       "cannot be asked about", known=False, rows=0)
    resp = _request(cfg, {"fingerprint": str(fingerprint), "duration": seconds})
    if not resp["ok"]:
        return _result(False, resp["code"], resp["reason"], known=False, rows=0)
    rows = parse_payload({"status": "ok", "results": resp["results"]})
    want = rid.casefold()
    known = any(str(row.get("recording_id") or "").strip().casefold() == want
                for row in rows)
    return _result(True, OK, "", known=known, rows=len(rows))


# --------------------------------------------------------------------------- #
# What this app has already given AcoustID (the local half of "don't re-send")
# --------------------------------------------------------------------------- #
# AcoustID imports a submission asynchronously, so a pair sent a minute ago is
# still "pending" when the lookup endpoint is asked about it again: without a
# local note, a second press — or the next run over the same album — would send
# the very same pair. One small JSON file under the music folder's .mlo/data
# records the pairs the service ACCEPTED (with its submission id and status);
# an entry the service refused is deliberately NOT recorded, so it is retried,
# and nothing here is ever read as a verdict about the audio.
SUBMISSIONS_FILE = "acoustid_submissions.json"
# Bounded: a library-wide run over a huge library must not grow this forever.
# The oldest entries drop first — the service check covers what falls out.
_SUBMISSIONS_MAX = 20000


def submission_key(fingerprint, recording_id):
    """The identity of one submission: sha1 of fingerprint + recording id.

    A fingerprint is thousands of characters, so the pair is hashed rather than
    stored whole: the same file fingerprinted twice gives the same fingerprint,
    so the same key, which is exactly the re-send this is here to catch.
    """
    material = f"{str(fingerprint or '').strip()}|" \
               f"{str(recording_id or '').strip()}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


def submissions_file(cfg=None):
    """<music>/.mlo/data/acoustid_submissions.json, or "" when there is none."""
    try:
        from .paths import app_data_dir

        return os.path.join(app_data_dir((cfg or {}).get("music_folder")),
                            SUBMISSIONS_FILE)
    except Exception:
        return ""


def load_submissions(cfg=None):
    """{key: {"recording_id", "fingerprint", "id", "status", "at"}}; {} on any
    failure — a record that cannot be read must not stop a submission."""
    path = submissions_file(cfg)
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, row in data.items():
        if isinstance(row, dict) and str(key).strip():
            out[key] = row
    return out


def record_submissions(cfg, rows):
    """Remember the pairs the service accepted. Atomic, bounded, never raises.

    `rows` are this module's own result rows ({"fingerprint", "recording_id",
    "id", "status", "path"}) — only the ones the service gave an id to.
    """
    rows = [row for row in (rows or [])
            if isinstance(row, dict) and row.get("id") is not None
            and str(row.get("fingerprint") or "").strip()
            and str(row.get("recording_id") or "").strip()]
    if not rows:
        return
    path = submissions_file(cfg)
    if not path:
        return
    data = load_submissions(cfg)
    at = time.strftime("%Y-%m-%dT%H:%M:%S")
    for row in rows:
        data[submission_key(row["fingerprint"], row["recording_id"])] = {
            "recording_id": str(row["recording_id"]),
            "fingerprint": str(row["fingerprint"]),
            "id": row.get("id"), "status": row.get("status"), "at": at,
            "path": row.get("path"),
        }
    if len(data) > _SUBMISSIONS_MAX:
        keep = sorted(data.items(),
                      key=lambda kv: str(kv[1].get("at") or ""),
                      reverse=True)[: _SUBMISSIONS_MAX]
        data = dict(keep)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_bytes(path, json.dumps(data, indent=0).encode("utf-8"))
    except Exception:                             # pragma: no cover - disk
        pass


# The probe submission `verify_user_key` sends, when the caller has no
# fingerprint of its own: PROBE_FINGERPRINT with no mbid, no title and no
# artist (source 3, "fingerprint only"), so nothing in it can ever attach wrong
# metadata to a recording — which is the one thing a credential probe must not
# do. A fingerprint-only submission with no recording to point at is what
# AcoustID's own unmatched queue is made of, and it is the ONE place this app
# turns `require_mbid` off: a key can only be proved by a real submission, and
# the probe has nothing to attach (record_submissions is never reached with
# it).
def verify_user_key(cfg=None, fingerprint=None, duration=None):
    """Does the live service accept this USER key? One probe submission.

    A lookup proves the APPLICATION key; only a submission can prove the user
    key, so the probe is the cheapest honest one: PROBE_FINGERPRINT submitted
    as a fingerprint-only entry (source 3, no mbid, no title, no artist), which
    cannot attach wrong metadata to any recording. A working key answers "ok"
    with the submission AcoustID took (its id and status — "pending" is the
    documented answer, submissions are processed asynchronously) and a refused
    one answers with the service's own sentence ("HTTP 400 - invalid user API
    key (code 8)") verbatim, which is the whole reason this exists.

    -> {"ok", "code", "reason", "id", "status", "submitted"}: `ok` means the
    KEY was ACCEPTED, `code` is no_user_key / no_api_key / disabled when the
    config cannot try at all and lookup_failed / bad_response when the service
    refused or mis-answered. Never raises.
    """
    chk = check_submit(cfg)
    if not chk["available"]:
        return _result(False, chk["code"], chk["reason"], submitted=0,
                       id=None, status=None)
    body = {"fingerprint": str(fingerprint or PROBE_FINGERPRINT),
            "duration": float(duration or PROBE_DURATION),
            "source": SOURCE_FINGERPRINT}
    got = submit_fingerprints(cfg, [body], require_mbid=False)
    if not got["ok"]:
        return _result(False, got["code"], got["reason"],
                       submitted=got.get("submitted") or 0, id=None,
                       status=None)
    first = (got.get("submissions") or [{}])[0]
    return _result(True, OK, "", submitted=got.get("submitted") or 0,
                   id=first.get("id"), status=first.get("status"))


# --------------------------------------------------------------------------- #
# One submission pass over a set of FILES (the route and the script runner)
# --------------------------------------------------------------------------- #
def prepare_submission(cfg, path):
    """(item, skip) for ONE file: exactly one of the two is set.

    What a submission needs is what the FILE says, read in the order the rest of
    this module trusts (`_recording_identity`): the recording id from
    ACOUSTID_ID, then MUSICBRAINZ_TRACKID, then the one bracketed recording UUID
    in the file's own name; the fingerprint from the ACOUSTID_FINGERPRINT tag
    when there is one, else taken from the AUDIO with fpcalc (local, no key, no
    request). That second half is what makes the CD rip AcoustID has never heard
    of submittable at all — a lookup cannot identify audio the database has
    never seen, and this app's own library is full of exactly that.

    A file that names no recording is SKIPPED (`no_recording_id`), never
    fingerprinted and sent: a submission stores a fingerprint WITH the recording
    it is (MusicBrainz itself never receives a fingerprint), and this app sends
    only what a file states. Reads only — `get_tag`, the stream's own length,
    fpcalc — so a submission never writes to the file it describes.
    """
    from .audio import AudioFile

    base = os.path.basename(str(path))
    try:
        af = AudioFile(path)
    except Exception as e:
        return None, {"path": path, "code": UNREADABLE,
                      "reason": f"could not read {base}: {e}"}
    try:
        if af.audio is None:
            return None, {"path": path, "code": UNREADABLE,
                          "reason": (f"cannot read {base}: "
                                     f"{af.error or 'no tag reader for this file'}")}
        rid, where = _recording_identity(path, af)
        value = str(af.get_tag("ACOUSTID_FINGERPRINT") or "").strip()
        info = getattr(getattr(af, "audio", None), "info", None)
        duration = 0.0
        for candidate in (getattr(info, "length", None),
                          (getattr(af, "tech", None) or {}).get("length")):
            try:
                duration = float(candidate or 0)
            except (TypeError, ValueError):
                duration = 0.0
            if duration > 0:
                break
        if not value:
            if not rid:
                return None, {"path": path, "code": NO_RECORDING_ID,
                              "reason": (f"{base} carries no "
                                         f"ACOUSTID_FINGERPRINT and names no "
                                         f"MusicBrainz recording to submit one "
                                         f"with")}
            taken = fingerprint(path, cfg)
            if not taken["ok"]:
                return None, {"path": path, "code": taken["code"],
                              "reason": taken["reason"]}
            value = taken["fingerprint"]
            duration = float(taken.get("duration") or 0)
        if not rid:
            return None, {"path": path, "code": NO_RECORDING_ID,
                          "reason": (f"{base} names no MusicBrainz recording to "
                                     f"submit its fingerprint with (ACOUSTID_ID, "
                                     f"MUSICBRAINZ_TRACKID, or the recording id "
                                     f"in the file name)")}
        if duration <= 0:
            return None, {"path": path, "code": NO_DURATION,
                          "reason": (f"{base} states no duration, and AcoustID "
                                     f"stores one beside every fingerprint")}
        item = {"path": path, "fingerprint": value, "duration": duration,
                "recording_id": rid, "identity_from": where}
        for key, tag in (("track", "TITLE"), ("artist", "ARTIST"),
                         ("album", "ALBUM"), ("album_artist", "ALBUMARTIST"),
                         ("year", "DATE"), ("track_no", "TRACKNUMBER"),
                         ("disc_no", "DISCNUMBER")):
            text = str(af.get_tag(tag) or "").strip()
            if text:
                item[key] = text.split("-")[0].strip() if key == "year" else text
        return item, None
    except Exception as e:      # a reader that fails mid-ask is not a crash
        return None, {"path": path, "code": INTERNAL,
                      "reason": f"could not read {base}: {e}"}


def _known_row(path, item, reason):
    """A track AcoustID already has (either half of the dedupe)."""
    return {"path": path, "outcome": ALREADY_KNOWN, "code": ALREADY_KNOWN,
            "reason": reason, "recording_id": str(item.get("recording_id") or ""),
            "id": None, "status": None, "index": None}


def submit_files(cfg, files, progress=None):
    """Give AcoustID the fingerprint + recording id every one of *files* states.

    The one pass both callers use (the route's `POST /api/import/acoustid/submit`
    and script 22): per file `prepare_submission`, then the TWO dedupes — what
    the service already links (`pair_known`, one lookup per candidate) and what
    this app has already handed over (`load_submissions`) — and only then ONE
    batched `v2/submit` for everything that is genuinely new. A track is
    reported either way: ACCEPTED (with the service's submission id + status),
    ALREADY_KNOWN (with which half said so), REJECTED (with the service's own
    sentence) or OUTCOME_SKIPPED (with a named cause: no recording id, no
    fpcalc, a duration the file does not state, or a dedupe question that could
    not be asked — this never sends blind to a service it could not ask).

    `acoustid_enabled` off means nothing at all happens: no file is read, no
    fingerprint is taken and no request is made — `check_submit` answers first.
    A missing user key is the same named refusal, and neither is ever a silent
    skip. `progress(row)` is called per file's own report, as it is decided.

    -> {"available", "ok", "code", "reason", "note", "total", "submitted",
        "known", "skipped", "failed", "results", "submissions", "skips",
        "tracks"}. Never raises.
    """
    cfg = cfg or {}
    files = [str(f) for f in (files or []) if str(f).strip()]
    empty = {"available": True, "ok": False, "code": NO_TRACKS, "note": "",
             "reason": "", "total": len(files), "submitted": 0, "known": 0,
             "skipped": 0, "failed": 0, "results": [], "submissions": [],
             "skips": [],
             "tracks": {"total": len(files), "submitted": 0, "known": 0,
                        "skipped": 0, "failed": 0}}
    chk = check_submit(cfg)
    if not chk["available"]:
        # A gate, not a failure per file: with no key (or the feature off)
        # nothing is read or fingerprinted, and the reason is the named one.
        return dict(empty, available=False, code=chk["code"],
                    note=chk["reason"], reason=chk["reason"])
    if not files:
        return dict(empty, note="no audio file to submit")
    ledger = load_submissions(cfg)
    plan = {}
    rows = []
    seen = {}
    # The fingerprinted half of the work (fpcalc on every file without a tag)
    # runs in the same bounded lanes every other multi-file runner uses; the
    # dedupe questions and the batch below stay in order, because the service
    # layer's shared throttle — not the CPU — decides how fast they go.
    workers = worker_count(cfg, default=4, maximum=8, items=len(files))
    if len(files) == 1 or workers == 1:
        prepared = [prepare_submission(cfg, path) for path in files]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as ex:
            prepared = list(ex.map(lambda p: prepare_submission(cfg, p), files))
    for path, (item, skip) in zip(files, prepared):
        if skip:
            row = {"path": path, "outcome": OUTCOME_SKIPPED,
                   "code": skip.get("code"), "reason": skip.get("reason"),
                   "recording_id": "", "id": None, "status": None,
                   "index": None}
            rows.append(row)
            if progress:
                progress(row)
            continue
        key = submission_key(item["fingerprint"], item["recording_id"])
        if key in seen:
            row = _known_row(path, item,
                             "the same fingerprint and recording id appear "
                             "twice in this selection (also "
                             f"{os.path.basename(str(seen[key]))})")
        elif key in ledger:
            prior = ledger[key] or {}
            row = _known_row(
                path, item,
                "this app already submitted this pair"
                + (f" on {prior.get('at')}" if prior.get("at") else "")
                + (f" (submission {prior.get('id')}, {prior.get('status')})"
                   if prior.get("id") is not None else ""))
        else:
            seen[key] = path
            plan[len(rows)] = (item, key)
            rows.append(None)
            continue
        rows.append(row)
        if progress:
            progress(row)

    # What AcoustID already has: one lookup per candidate, at every score (a
    # question about the DATABASE, not about the app's display threshold).
    sending = []
    for index, row in enumerate(rows):
        if row is not None:
            continue
        item, _key = plan[index]
        asked = pair_known(cfg, item["fingerprint"], item["duration"],
                           item["recording_id"])
        if not asked["ok"]:
            # The question could not be asked, so "it is new" could not be
            # established either: the track is skipped with the reason rather
            # than sent to a service this run could not talk to.
            rows[index] = {
                "path": item["path"], "outcome": OUTCOME_SKIPPED,
                "code": asked["code"],
                "reason": ("could not ask AcoustID what it already knows: "
                           f"{asked['reason']}"),
                "recording_id": item["recording_id"], "id": None, "status": None,
                "index": None}
        elif asked["known"]:
            rows[index] = _known_row(
                item["path"], item,
                "AcoustID already links this fingerprint to recording "
                f"{item['recording_id']}")
        else:
            sending.append(index)
        if progress and rows[index] is not None:
            progress(rows[index])

    if sending:
        res = submit_fingerprints(cfg, [plan[i][0] for i in sending])
        answers = res.get("results") or []
        for n, index in enumerate(sending):
            if n < len(answers):
                rows[index] = dict(answers[n])
            else:
                # The batch layer answered nothing for this track (an
                # unavailable config, an empty batch): report it as unwritten
                # rather than as something the service took.
                rows[index] = {
                    "path": plan[index][0]["path"], "outcome": REJECTED,
                    "code": res.get("code") or BAD_RESPONSE,
                    "reason": res.get("reason") or "AcoustID answered nothing "
                                                   "for this track",
                    "recording_id": plan[index][0]["recording_id"], "id": None,
                    "status": None, "index": None}
            if progress:
                progress(rows[index])
        record_submissions(cfg, [
            {"fingerprint": plan[index][0]["fingerprint"],
             "recording_id": rows[index].get("recording_id")
                             or plan[index][0]["recording_id"],
             "id": rows[index].get("id"), "status": rows[index].get("status"),
             "path": rows[index].get("path")}
            for index in sending
            if rows[index] and rows[index].get("outcome") == ACCEPTED
            and rows[index].get("id") is not None])

    results = [row for row in rows if row is not None]
    counted = {name: len([r for r in results if r["outcome"] == name])
               for name in SUBMISSION_OUTCOMES}
    submitted = counted[ACCEPTED]
    known = counted[ALREADY_KNOWN]
    skipped = counted[OUTCOME_SKIPPED]
    failed = counted[REJECTED]
    rejected = [r for r in results if r["outcome"] == REJECTED]
    code = rejected[0].get("code") if rejected else OK
    note = rejected[0].get("reason") if rejected else ""
    return {"available": True, "ok": failed == 0, "code": code, "note": note,
            "reason": note, "total": len(files), "submitted": submitted,
            "known": known, "skipped": skipped, "failed": failed,
            "results": results,
            "submissions": [r for r in results
                            if r["outcome"] == ACCEPTED and r.get("id") is not None],
            "skips": [{"path": r.get("path"), "code": r.get("code"),
                       "reason": r.get("reason")}
                      for r in results if r["outcome"] == OUTCOME_SKIPPED],
            "tracks": {"total": len(files), "submitted": submitted,
                       "known": known, "skipped": skipped, "failed": failed}}


# --------------------------------------------------------------------------- #
# Album matching (import)
# --------------------------------------------------------------------------- #
def report(status, code, reason="", match=None, skips=(), failures=(),
           conflicts=(), total=0, fingerprinted=0, no_match=0):
    """One album's AcoustID outcome, in the shape every caller consumes.

    status: "matched" (with `match`), "no_match" (the service answered, no
    release group owned enough tracks), "skipped" (nothing could be
    fingerprinted) or "error" (tooling/service could not answer).
    `fingerprinted` counts the tracks AcoustID answered about; `no_match`
    counts those it answered "nothing" for — neither is a failure.
    """
    return {
        "status": status,
        "code": code,
        "reason": reason,
        "match": match,
        "conflict": bool(conflicts),
        "conflicts": list(conflicts),
        "skips": list(skips),
        "failures": list(failures),
        "tracks": {"total": total, "fingerprinted": fingerprinted,
                   "no_match": no_match, "skipped": len(skips),
                   "failed": len(failures)},
    }


def error_report(reason, code=INTERNAL, total=0):
    """An internal failure (an unreadable folder, a bug) as a report."""
    return report("error", code, reason, total=total)


def _quorum(total):
    """How many tracks of an album a release group must own to decide it.

    `max(2, ceil(total * 0.4))` for a real album: one track out of twelve is a
    comp track, not the album. A ONE-track album (a single, a one-track rip)
    has no second track to corroborate anything, and the old floor made it
    unmatchable BY CONSTRUCTION — AcoustID identified the only track and the
    wizard still said "no match". So the rule is: half the album, at least two,
    except when there is nothing to corroborate.
    """
    return 1 if total <= 1 else max(2, math.ceil(total * 0.4))


def match_release(cfg, paths, progress=None, expect=None):
    """Modal release group across an album's tracks, and why it has none.

    Rule: a release group must own at least `max(2, ceil(total * 0.4))` of the
    tracks attempted (`total`, capped at MAX_TRACKS) — or that one track when
    the album only has one — and their mean score must reach
    `acoustid_min_score`. A single-track album therefore decides on its one
    identified track; a single hit inside a larger album still never does. A
    track with no fingerprintable audio (a video container, a clip under
    MIN_DURATION seconds, an empty fingerprint) is a SKIP with a reason;
    a fingerprint or lookup that failed is a FAILURE with a reason, and the
    first transport failure ends the album's lookups (the service is down, the
    key is wrong or the rate limit hit - eleven more timeouts cannot help). A
    track the service answered "nothing" for is neither: it is counted in
    `tracks["no_match"]` and the album's verdict stays a plain "no_match".

    With *expect* (the tag-derived candidate: `release_group_id` /
    `release_group_title` / `artists`) the match is cross-checked and
    `conflicts` (+ `conflict`) carry the disagreements; the match itself is
    returned untouched, so a caller can never mistake a conflict for an
    overwrite.

    -> see `report()`.
    """
    chk = check(cfg)
    if not chk["available"]:
        return report("skipped", chk["code"], chk["reason"])
    paths = [p for p in (paths or []) if p]
    if not paths:
        return report("skipped", NO_TRACKS, _NOTES[NO_TRACKS])
    paths = paths[:MAX_TRACKS]
    floor = min_score(cfg)
    total = len(paths)

    groups = {}          # release_group_id -> {"rows": [...], "recordings": [...]}
    skips, failures = [], []
    fingerprinted = no_match = 0
    for done, path in enumerate(paths, 1):
        res = lookup(cfg, path)
        if res["ok"]:
            fingerprinted += 1
            row = res["rows"][0]        # one row per track: the best candidate
            gid = row.get("release_group_id")
            if gid and row["score"] >= floor:
                entry = groups.setdefault(gid, {"rows": [], "recordings": []})
                entry["rows"].append(row)
                entry["recordings"].append({
                    "path": path,
                    "recording_id": row["recording_id"],
                    "title": row["title"],
                    "score": row["score"],
                    "fingerprint": row.get("fingerprint"),
                })
        elif res["code"] == NO_MATCH:
            # the service answered and knew this track: evidence, not a failure
            no_match += 1
        else:
            item = {"path": path, "code": res["code"], "reason": res["reason"]}
            if res["code"] in SKIP_CODES:
                skips.append(item)
            else:
                failures.append(item)
                if res["code"] in FATAL_LOOKUP_CODES:
                    break
        if progress:
            try:
                progress(done, total, "AcoustID lookup")
            except Exception:
                pass

    quorum = _quorum(total)
    best = None
    for gid, entry in groups.items():
        matched = len(entry["recordings"])
        if matched < quorum:
            continue
        mean = sum(r["score"] for r in entry["rows"]) / matched
        if mean < floor:
            continue
        if best is None or matched > best[1]:
            best = (gid, matched, mean, entry)

    if best is None:
        if failures:
            # never "no match": we did not get to ask about every track
            return report("error", failures[0]["code"], _failure_reason(failures),
                          skips=skips, failures=failures, total=total,
                          fingerprinted=fingerprinted, no_match=no_match)
        if fingerprinted:
            return report("no_match", NO_MATCH,
                          f"no release group owned enough of the {fingerprinted} "
                          f"identified track(s) above score {floor:.2f}",
                          skips=skips, total=total, fingerprinted=fingerprinted,
                          no_match=no_match)
        if no_match:
            return report("no_match", NO_MATCH,
                          f"AcoustID identified none of the {total} track(s)",
                          skips=skips, total=total, no_match=no_match)
        if skips:
            return report("skipped", skips[0]["code"], _skip_reason(skips),
                          skips=skips, total=total)
        return report("skipped", NO_TRACKS, _NOTES[NO_TRACKS], total=total)

    gid, matched, mean, entry = best
    first = entry["rows"][0]
    artists = []
    for row in entry["rows"]:
        for name in row["artists"]:
            if name not in artists:
                artists.append(name)
    match = {
        "release_group_id": gid,
        "release_group_title": first["release_group_title"],
        "release_group_type": first["release_group_type"],
        "artists": artists,
        "score": mean,
        "matched": matched,
        "total": total,
        "recordings": entry["recordings"],
    }
    conflicts = cross_check(match, expect)
    reason = ""
    if failures:
        reason = (f"{len(failures)} of {total} track(s) could not be "
                  f"fingerprinted")
    return report("matched", CONFLICT if conflicts else OK, reason, match=match,
                  skips=skips, failures=failures, conflicts=conflicts,
                  total=total, fingerprinted=fingerprinted, no_match=no_match)


def _failure_reason(failures):
    first = failures[0]
    more = f" (+{len(failures) - 1} more track(s))" if len(failures) > 1 else ""
    return f"{first['reason']}{more}"


def _skip_reason(skips):
    first = skips[0]
    more = f" (+{len(skips) - 1} more track(s))" if len(skips) > 1 else ""
    return f"{first['reason']}{more}"


# --------------------------------------------------------------------------- #
# Cross-check against the tags
# --------------------------------------------------------------------------- #
def _norm(text):
    """Casefold and drop everything that is not a letter or a digit.

    "The Knife" / "the knife" / "The-Knife" are one artist; a localized title
    is not a different release, so punctuation and case must not decide.
    """
    return "".join(ch for ch in str(text or "").casefold() if ch.isalnum())


def _names(value):
    """Artist names from a list of strings / {"name": ...} dicts / a string."""
    if isinstance(value, str):
        value = [value]
    out = []
    for item in value or []:
        name = item.get("name") if isinstance(item, dict) else item
        name = str(name or "").strip()
        if name:
            out.append(name)
    return out


def _related(a, b):
    """Two normalized names for the same thing, or one of them empty.

    Equal, or one inside the other: a title carrying an edition suffix
    ("... (Deluxe Edition)", "... (Disc 2)") and an artist carrying a guest
    credit ("... feat. X") are the same release/act, not a disagreement.
    """
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def cross_check(match, expect):
    """Disagreements between a fingerprint match and the tags, or [].

    Fields compared, in this order and nothing else:

    * `release_group_id` - case-insensitively, but only when BOTH sides carry
      one; a missing id is not a disagreement;
    * `release_group_title` (or `title` / `album`) - only when the ids could
      not be compared, compared through `_norm` + `_related` so case,
      punctuation, translation and an edition suffix are not a different
      release;
    * the artist names - whenever both sides list any, compared as normalized
      sets with `_related`; nothing in common at all means the audio is by
      somebody else.

    -> [{"kind", "reason", "fingerprint", "tags"}]. The caller reports these;
    nothing here writes anything, so a fingerprint can never overwrite the
    release an import was matched to.
    """
    if not match or not expect:
        return []
    got_id = str(match.get("release_group_id") or "").strip()
    want_id = str(expect.get("release_group_id") or "").strip()
    got_title = str(match.get("release_group_title") or "").strip()
    want_title = str(expect.get("release_group_title")
                     or expect.get("title") or expect.get("album") or "").strip()
    got_artists = _names(match.get("artists"))
    want_artists = _names(expect.get("artists") or expect.get("artist"))

    out = []
    if got_id and want_id:
        if got_id.casefold() != want_id.casefold():
            out.append({
                "kind": "release_group",
                "reason": (f"the audio is release group {got_id}"
                           + (f" ({got_title})" if got_title else "")
                           + f" but the tags say {want_id}"
                           + (f" ({want_title})" if want_title else "")),
                "fingerprint": got_id, "tags": want_id,
            })
    elif got_title and want_title and not _related(_norm(got_title), _norm(want_title)):
        out.append({
            "kind": "title",
            "reason": (f"the audio's release group is titled \"{got_title}\" "
                       f"but the tags say \"{want_title}\""),
            "fingerprint": got_title, "tags": want_title,
        })
    if got_artists and want_artists:
        common = any(_related(_norm(a), _norm(b))
                     for a in got_artists for b in want_artists)
        if not common:
            out.append({
                "kind": "artist",
                "reason": (f"the audio is by {', '.join(got_artists)} but the "
                           f"tags say {', '.join(want_artists)}"),
                "fingerprint": got_artists, "tags": want_artists,
            })
    return out


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #
def check(cfg=None, need_fpcalc=True):
    """{"available", "code", "reason"} - why AcoustID can(not) be used.

    `reason` is "" when it is available, and otherwise the sentence the Import
    UI, the settings page and the Soulseek job log show. `need_fpcalc=False`
    asks only the switch/key question, for `verify_key`, which checks the key
    without ever fingerprinting anything.
    """
    cfg = cfg or {}
    if not cfg.get("acoustid_enabled"):
        return _result(False, DISABLED, _NOTES[DISABLED], available=False)
    if not str(cfg.get("acoustid_api_key") or "").strip():
        return _result(False, NO_API_KEY, _NOTES[NO_API_KEY], available=False)
    if need_fpcalc and fpcalc_path(cfg) is None:
        return _result(False, NO_FPCALC, _NOTES[NO_FPCALC], available=False)
    return _result(True, OK, "", available=True)


def available(cfg=None):
    """True when AcoustID can actually be used with this config."""
    return check(cfg)["available"]


def acoustid_enabled_note(cfg=None):
    """Why AcoustID is unavailable, or "" when it is available."""
    return check(cfg)["reason"]


# --------------------------------------------------------------------------- #
# Completing an incomplete tag pair (script runner)
# --------------------------------------------------------------------------- #
# A UUID exactly as the app's own naming script and every tagger spell it.
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# The identity tags whose ids the file NAME also spells (the naming script
# embeds `[%musicbrainz_albumid%]`, `[%musicbrainz_releasegroupid%]` and
# `[%musicbrainz_trackid%]`). None of these may be mistaken for the recording
# when the name is read as evidence.
_NAME_OTHER_ID_TAGS = ("MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_RELEASEGROUPID",
                       "MUSICBRAINZ_RELEASETRACKID", "MUSICBRAINZ_ARTISTID",
                       "MUSICBRAINZ_ALBUMARTISTID")


def _recording_identity(path, af):
    """(recording id, where it was read) this file states, or ("", "").

    The one question `fix_pair` answers is "which recording is this?", and a
    file that already answers it must never be sent to a service to be told
    again — the CD rips AcoustID does not know are exactly the library the
    grader is failing and the app could not repair. Read in the order the app
    itself trusts:

    * `ACOUSTID_ID` — the pair's own half; a file that already states one keeps
      it (`write_tags` refuses a lone id, but a library tagged elsewhere can
      carry one).
    * `MUSICBRAINZ_TRACKID` — THE recording tag of this app's own output:
      beets/autotag write it on every import, the naming script puts it in the
      file name and the wizard's match writes the same id as `ACOUSTID_ID`
      (see `write_tags`), so a track carrying it states its own recording.
    * the file NAME — the app's own naming script writes
      `[%musicbrainz_trackid%] [%musicbrainz_releasegroupid%]` into it, so a
      name carrying exactly ONE bracketed UUID that none of the file's other
      identity tags claims is the recording the app itself wrote there. Two
      candidates (or the id of another entity) mean the name does not say
      WHICH is the recording, and a coin toss is how a wrong identity gets
      written.
    """
    for tag in ("ACOUSTID_ID", "MUSICBRAINZ_TRACKID"):
        value = str(af.get_tag(tag) or "").strip()
        if value:
            return value, tag
    claimed = set()
    for tag in _NAME_OTHER_ID_TAGS + ("MUSICBRAINZ_TRACKID",):
        value = str(af.get_tag(tag) or "").strip().casefold()
        if value:
            claimed.add(value)
    stem = os.path.splitext(os.path.basename(str(path or "")))[0]
    found = [u for u in _UUID_RE.findall(stem) if u.casefold() not in claimed]
    if len(found) == 1:
        return found[0], "the file name"
    return "", ""


def fix_pair(path, cfg=None):
    """Complete — or create — one file's ACOUSTID_ID / ACOUSTID_FINGERPRINT pair.

    -> {"path", "status": "modified"|"unchanged"|"skipped"|"failed",
        "reason"} — never raises, so one bad file cannot stop a library run.

    What each half can be completed FROM decides how it is completed:

    * the recording id is on the file (see `_recording_identity`) and the
      fingerprint half is missing: the fingerprint is a property of the AUDIO,
      so fpcalc takes it locally — no network, nothing guessed, nothing asked
      — and the id the file states is kept. This is the CD rip AcoustID does
      not know: the id it needs is in the file's own `MUSICBRAINZ_TRACKID`
      (and in the name this app wrote), so the service was never the only way
      to complete the pair, only the only way this pass used to try.
    * a half pair whose file names no recording: only the service can say
      which recording this audio is (`lookup`, the configured key, fpcalc and
      the shared rate limit). The pair written is the one the returned
      identity was matched FROM, so both halves describe the same fingerprint
      by construction — and with no match or no answer, nothing is written at
      all.

    A file carrying BOTH halves is left alone, and one carrying NEITHER and
    naming no recording is not this pass's business: there is nothing on it to
    complete a pair from, and fingerprinting a whole library for a pair
    nothing asked for would make every run pay the service for it. Nothing is
    ever written from a guess.
    """
    out = {"path": path, "status": "skipped", "reason": ""}
    try:
        from .audio import AudioFile

        af = AudioFile(path)
        if af.audio is None:
            # No reader (or an unreadable file): whether a half pair is on it
            # cannot even be asked, and "it carries none" would be a guess.
            out.update(status="failed",
                       reason=(f"cannot read {os.path.basename(str(path))}: "
                               f"{af.error or 'no tag reader for this file'}"))
            return out
        aid = str(af.get_tag("ACOUSTID_ID") or "").strip()
        fp = str(af.get_tag("ACOUSTID_FINGERPRINT") or "").strip()
        if aid and fp:
            out["status"] = "unchanged"
            return out
        rid, where = _recording_identity(path, af)
        if rid:
            # The file names the recording (its own ACOUSTID_ID, its
            # MUSICBRAINZ_TRACKID, or the MBID this app's naming script wrote
            # into its name); the fingerprint is a property of the AUDIO and
            # fpcalc takes it here — no key, no request, no rate limit, so a
            # rip AcoustID has never seen is repairable.
            got = fingerprint(path, cfg)
            if not got["ok"]:
                out.update(status="failed", reason=got["reason"])
                return out
            wrote = write_tags(path, rid, got["fingerprint"], cfg)
        elif fp:
            # A half pair whose file names no recording: only the service can
            # say which recording this audio is.
            got = lookup(cfg, path)
            if not got["ok"]:
                out.update(status="failed", reason=got["reason"])
                return out
            wrote = write_tags(path, got["rows"][0]["recording_id"],
                               got["fingerprint"], cfg)
        else:
            # Nothing on the file to complete a pair FROM — no AcoustID tag,
            # no recording id anywhere. Fingerprinting a whole library for a
            # pair nothing asked for is not this pass's business, and
            # inventing an identity is the one thing it must never do.
            out["reason"] = ("carries no AcoustID tag and no recording id — "
                             "nothing to complete a pair from")
            return out
        if not wrote.get("ok"):
            out.update(status="failed",
                       reason=wrote.get("reason") or "the pair was not written")
            return out
        out.update(status="modified",
                   reason=(f"id read from {where}" if where else ""))
        return out
    except Exception as e:
        out.update(status="failed", reason=str(e))
        return out


def run_fix_pairs(cfg=None):
    """Script: complete the AcoustID pairs a library carries (half — or none).

    `write_tags` writes ACOUSTID_ID and ACOUSTID_FINGERPRINT in one save and
    refuses a lone id, so nothing in this app writes half a pair on purpose —
    but a library tagged elsewhere (Picard's own "generate fingerprints"
    writes the fingerprint alone, hand-tagging, an older pass, a restored
    backup) can carry one, and the grader fails every such track
    ("Missing ACOUSTID_ID (run Fix AcoustID pairs)", `mlo.grader`). This pass
    walks the configured targets and, for each track, COMPLETES the half pair
    — or CREATES the pair where the file names its own recording but carries
    no AcoustID tag at all, because "which recording is this" is a question
    the file usually answers itself (`_recording_identity`: ACOUSTID_ID,
    MUSICBRAINZ_TRACKID, or the recording MBID this app's naming script wrote
    into the file name) while the fingerprint is taken locally by fpcalc. The
    AcoustID service is asked only for a half pair whose file names no
    recording at all; see `fix_pair`.

    Stats, in the runner shape: `total_scanned` is every audio file examined,
    `modified_count` the pairs completed or created, `unchanged_count` the
    files that already carried both halves, `skipped_count` those carrying no
    AcoustID tag and naming no recording (nothing to complete from — a guess
    is never written), and
    `error_count`/`errors` the files whose pair could NOT be completed — each
    named with its own reason (a container this app cannot tag, a track too
    short for fpcalc, a missing fpcalc, no API key, no
    lookup answer). Nothing is written for those, and the run never raises.
    """
    cfg = cfg or {}
    stats = new_stats()
    print_header("AcoustID pairs")
    folder = str(cfg.get("music_folder") or "")

    if cfg.get("targets") is not None:
        files = sorted(_collect_targets(cfg["targets"], LIB_AUDIO_EXTS))
    else:
        if not os.path.isdir(folder):
            log(c(f"ERROR: folder does not exist: {folder}", Color.RED))
            return stats
        files = []
        for album_dir in _find_albums(folder):
            files.extend(sorted(
                os.path.join(album_dir, f)
                for f in os.listdir(album_dir) if is_audio_file(f)))
    if not files:
        log("No audio files found.")
        return stats

    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(total=len(files), desc="AcoustID pairs")

    def _finish(path, got):
        """Book one track's result on the runner thread (the workers share no
        state but the throttle inside the service layer)."""
        stats["total_scanned"] += 1
        status = got["status"]
        if status == "modified":
            stats["modified_count"] += 1
            _pbar_update(pbar, counts, "ok")
            return
        if status == "unchanged":
            stats["unchanged_count"] += 1
            _pbar_update(pbar, counts)
            return
        if status == "skipped":
            stats["skipped_count"] += 1
            _pbar_skip(pbar, counts)
            return
        stats["error_count"] += 1
        if len(stats["errors"]) < 25:
            stats["errors"].append(
                f"{os.path.basename(path)}: {got['reason']}")
        _pbar_update(pbar, counts, "fail")

    # Bounded parallelism: every track is its own tag read plus its own
    # fpcalc/lookup, and both layers serialise what must be serialised
    # (fpcalc runs as its own process, the service calls through the shared
    # throttle), so lanes overlap the wait instead of paying it once per file.
    workers = worker_count(cfg, default=4, maximum=8, items=len(files))
    try:
        if len(files) == 1 or workers == 1:
            for path in files:
                _finish(path, fix_pair(path, cfg))
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(fix_pair, p, cfg): p for p in files}
                for fut in as_completed(futures):
                    path = futures[fut]
                    try:
                        got = fut.result()
                    except Exception as e:  # a worker must never kill the run
                        got = {"status": "failed", "reason": str(e)}
                    _finish(path, got)
    finally:
        try:
            pbar.close()
        except Exception:
            pass

    log(c(f"AcoustID pairs completed {stats['modified_count']}"
          f" · already complete {stats['unchanged_count']}"
          f" · nothing to complete from {stats['skipped_count']}"
          f" · failed {stats['error_count']}",
          Color.GREEN if not stats["error_count"] else Color.YELLOW))
    return stats


# --------------------------------------------------------------------------- #
# Script 22: give AcoustID what this library's files state (v2/submit)
# --------------------------------------------------------------------------- #
# A submission's skips that are a FACT about the file rather than a failure:
# it names no recording, states no duration, carries nothing to submit. Every
# other cause (fpcalc could not run, the service could not be asked at all) is
# a failure and reaches `errors` beside the service's own refusals, exactly
# like the fingerprint passes treat tooling that would not answer.
_SUBMIT_SKIP_CODES = frozenset({NO_RECORDING_ID, NO_FINGERPRINT, NO_DURATION,
                                NOT_AUDIO, TOO_SHORT})


def run_submit_fingerprints(cfg=None):
    """Script: give AcoustID the fingerprint + recording id the files state.

    The whole-library / whole-selection half of the submission contract —
    `submit_files` is the per-file work, and this is the script registry's
    entry to it (scope "file": one fingerprint + one recording id belong to one
    file). Targets when the run was given any, else every album under the music
    folder, exactly like script 21.

    A config that cannot submit AT ALL raises with the named reason instead of
    reporting an empty run: the whole point of the run is to hand AcoustID
    something, and "nothing happened" without a reason is the one answer this
    must never give — the chain's own gate (`script_runners._DISABLED`) only
    covers `acoustid_enabled`, and a missing USER key is a thing the user has
    to fix (Settings → Import). Nothing is read, fingerprinted or sent in that
    case, and nothing is written locally by this script at all.

    Stats, in the runner shape: `total_scanned` every audio file examined,
    `submitted`/`modified_count` the pairs AcoustID took (each with its
    submission id in the route's report), `already_known`/`unchanged_count`
    those the service already had or this app had already sent,
    `skipped_count` nothing-to-submit with a named cause each (`by_cause`),
    and `error_count`/`errors` the service's own refusals plus anything that
    stopped the app asking.
    """
    cfg = cfg or {}
    stats = new_stats()
    stats.update({"submitted": 0, "already_known": 0, "by_cause": {}})
    print_header("Submit fingerprints (AcoustID)")

    chk = check_submit(cfg)
    if not chk["available"]:
        # A named, actionable state — never a silent skip: without the USER key
        # there is no submission at all (an application key only looks up), and
        # with the feature switched off the chain skips this script before it
        # is asked (script_runners._DISABLED).
        raise RuntimeError(
            f"{chk['reason']} — nothing was submitted. The USER key of your "
            "acoustid.org account (Settings → Import → 'AcoustID user key') is "
            "what submits fingerprints; an application key can only look up.")

    folder = str(cfg.get("music_folder") or "")
    if cfg.get("targets") is not None:
        files = sorted(_collect_targets(cfg["targets"], LIB_AUDIO_EXTS))
    else:
        if not os.path.isdir(folder):
            log(c(f"ERROR: folder does not exist: {folder}", Color.RED))
            return stats
        files = []
        for album_dir in _find_albums(folder):
            files.extend(sorted(
                os.path.join(album_dir, f)
                for f in os.listdir(album_dir) if is_audio_file(f)))
    if not files:
        log("No audio files found.")
        return stats

    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(total=len(files), desc="Submit to AcoustID")

    def _finish(row):
        """Book one track's own report (nothing here shares state)."""
        stats["total_scanned"] += 1
        outcome = row.get("outcome")
        if outcome == ACCEPTED:
            stats["submitted"] += 1
            stats["modified_count"] += 1
            _pbar_update(pbar, counts, "ok")
            return
        if outcome == ALREADY_KNOWN:
            stats["already_known"] += 1
            stats["unchanged_count"] += 1
            _pbar_skip(pbar, counts)
            return
        code = str(row.get("code") or "unknown")
        stats["by_cause"][code] = stats["by_cause"].get(code, 0) + 1
        if outcome == OUTCOME_SKIPPED and code in _SUBMIT_SKIP_CODES:
            stats["skipped_count"] += 1
            _pbar_skip(pbar, counts)
            return
        # A refusal from the service, or a step that could not be taken
        # (fpcalc, the dedupe question): both are failures, with their own
        # sentence, and a run that cannot say why it did nothing is the one
        # answer this script must never give.
        stats["error_count"] += 1
        if len(stats["errors"]) < 25:
            stats["errors"].append(
                f"{os.path.basename(str(row.get('path')))}: {row.get('reason')}")
        _pbar_update(pbar, counts, "fail")

    try:
        submit_files(cfg, files, progress=_finish)
    finally:
        try:
            pbar.close()
        except Exception:
            pass

    causes = " · ".join(f"{count} {code}"
                        for code, count in sorted(stats["by_cause"].items()))
    log(c(f"AcoustID submitted {stats['submitted']}"
          f" · already known {stats['already_known']}"
          f" · nothing to submit {stats['skipped_count']}"
          f" · failed {stats['error_count']}"
          + (f"  ({causes})" if causes else ""),
          Color.GREEN if not stats["error_count"] else Color.YELLOW))
    return stats
