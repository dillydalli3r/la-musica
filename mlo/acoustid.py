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

    no_api_key / fpcalc_missing / no_tracks   what the config lacks
    not_audio / no_fingerprint / too_short    the file carries no fingerprintable audio (SKIPS)
    file_missing                              the path handed in is not a file
    fpcalc_failed                             fpcalc could not be run, or it failed on the file
    lookup_failed                             network, timeout, HTTP status, rejected request
    bad_response                              HTTP 200 whose body is not a usable AcoustID answer
    no_match                                  the service answered correctly and knew nothing
    conflict                                  the fingerprint and the tags name different releases

A fingerprint that could not be taken (`fpcalc_failed`) and a lookup that
could not be answered (`lookup_failed` / `bad_response`) are their OWN codes:
they are never reported as NO_MATCH, which means exactly "the service answered
and had no candidate above `acoustid_min_score`". `match_release` sorts the
same codes into an album verdict: the SKIPS in `skips`, everything else
fingerprint/lookup-shaped in `failures` (which make the verdict "error"), and
NO_MATCH counted as evidence, not as a failure.
"""

import json
import math
import os
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .paths import DEPS_DIR
from .subproc import run_tool

API_URL = "https://api.acoustid.org/v2/lookup"
_UA = "la-musica/2.1"
_META = "recordings releasegroups compress"
_FPCALC_TIMEOUT = 120
_HTTP_TIMEOUT = 30
# AcoustID allows 3 requests/second; stay just under it.
_MIN_INTERVAL = 0.34
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
NO_FPCALC = "fpcalc_missing"
NO_TRACKS = "no_tracks"
NO_FILE = "file_missing"
NOT_AUDIO = "not_audio"
NO_FINGERPRINT = "no_fingerprint"
TOO_SHORT = "too_short"
FPCALC_FAILED = "fpcalc_failed"
LOOKUP_FAILED = "lookup_failed"
BAD_RESPONSE = "bad_response"
NO_MATCH = "no_match"
CONFLICT = "conflict"
INTERNAL = "internal"

_NOTES = {
    DISABLED: "AcoustID disabled in settings",
    NO_API_KEY: "no API key",
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
    .dependencies/chromaprint* install (the folder the Dependencies installer
    and mlo.tools both read), then PATH.
    """
    cfg = cfg or {}
    explicit = cfg.get("acoustid_fpcalc_path")
    if explicit and os.path.isfile(explicit):
        return explicit

    if os.path.isdir(DEPS_DIR):
        try:
            entries = sorted(os.listdir(DEPS_DIR))
        except OSError:
            entries = []
        for entry in entries:
            if not entry.lower().startswith("chromaprint"):
                continue
            for name in ("fpcalc.exe", "fpcalc"):
                cand = os.path.join(DEPS_DIR, entry, name)
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


def _request(cfg, fp):
    """One POST to the lookup endpoint.

    -> {"ok", "code", "reason", "results"}; `results` is the service's list
    on success, and every transport, HTTP and body failure is a named code.
    """
    key = str((cfg or {}).get("acoustid_api_key") or "").strip()
    if not key:
        return _result(False, NO_API_KEY, _NOTES[NO_API_KEY], results=[])
    body = urllib.parse.urlencode({
        "client": key,
        "duration": int(round(float(fp.get("duration") or 0))),
        "fingerprint": fp.get("fingerprint") or "",
        "meta": _META,
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=body,
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
                       f"AcoustID lookup failed: HTTP {e.code}"
                       + (f" - {detail}" if detail else f" {_short(getattr(e, 'reason', ''))}"),
                       results=[])
    except (socket.timeout, TimeoutError):
        return _result(False, LOOKUP_FAILED,
                       f"AcoustID lookup timed out after {_HTTP_TIMEOUT}s", results=[])
    except (urllib.error.URLError, OSError) as e:
        return _result(False, LOOKUP_FAILED,
                       f"AcoustID lookup failed: {_short(e)}", results=[])

    try:
        payload = json.loads(bytes(raw).decode("utf-8", "replace"))
    except ValueError:
        return _result(False, BAD_RESPONSE,
                       f"AcoustID returned a body that is not JSON "
                       f"({_short(raw)})", results=[])
    if not isinstance(payload, dict):
        return _result(False, BAD_RESPONSE,
                       "AcoustID returned JSON that is not an object", results=[])
    if payload.get("status") != "ok":
        detail = _service_error(payload)
        return _result(False, BAD_RESPONSE,
                       "AcoustID answered with an error"
                       + (f": {detail}" if detail else f" (status {payload.get('status')!r})"),
                       results=[])
    results = payload.get("results")
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
    what Picard writes, and what the opt-in grading check looks for. Returns
    False when the file is unreadable or either write fails; never raises.
    """
    if not recording_id:
        return False
    try:
        from .audio import AudioFile
        af = AudioFile(path)
        if af.audio is None:
            return False
        ok = bool(af.set_tag("ACOUSTID_ID", str(recording_id)))
        if fingerprint:
            ok = bool(af.set_tag("ACOUSTID_FINGERPRINT", str(fingerprint))) and ok
        return ok
    except Exception:
        return False


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


def match_release(cfg, paths, progress=None, expect=None):
    """Modal release group across an album's tracks, and why it has none.

    Rule: a release group must own at least `max(2, ceil(total * 0.4))` of the
    tracks attempted (`total`, capped at MAX_TRACKS) and their mean score must
    reach `acoustid_min_score`. Single-track hits therefore never decide an
    album. A track with no fingerprintable audio (a video container, a clip
    under MIN_DURATION seconds, an empty fingerprint) is a SKIP with a reason;
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

    quorum = max(2, math.ceil(total * 0.4))
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
