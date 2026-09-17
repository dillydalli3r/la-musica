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

Every provider failure (no key, no fpcalc, network error, junk JSON, HTTP
429/5xx) is swallowed and reported as an empty result; callers never see an
exception from this module.
"""

import json
import math
import os
import shutil
import threading
import time
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

_rate_lock = threading.Lock()
_last_call = 0.0


# --------------------------------------------------------------------------- #
# fpcalc
# --------------------------------------------------------------------------- #
def fpcalc_path(cfg=None):
    """Path to the fpcalc executable, or None.

    Order: explicit `acoustid_fpcalc_path` in cfg, then a versioned
    .dependencies/chromaprint* install, then PATH.
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


def fingerprint(path, cfg=None):
    """`{"duration": float, "fingerprint": str}` for a file, or None."""
    exe = fpcalc_path(cfg)
    if not exe or not path or not os.path.isfile(path):
        return None
    try:
        result = run_tool(
            [exe, "-json", "-length", "120", path],
            capture_output=True, text=True, timeout=_FPCALC_TIMEOUT,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None

    out = (result.stdout or "").strip()
    if out.startswith("{"):
        try:
            data = json.loads(out)
            return {
                "duration": float(data["duration"]),
                "fingerprint": str(data["fingerprint"]),
            }
        except Exception:
            return None

    # fpcalc < 1.4 ignores -json and prints DURATION=/FINGERPRINT= lines.
    values = {}
    for line in out.splitlines():
        key, _, val = line.partition("=")
        values[key.strip().upper()] = val.strip()
    try:
        return {
            "duration": float(values["DURATION"]),
            "fingerprint": values["FINGERPRINT"],
        }
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def _throttle():
    """Keep the module's request rate under AcoustID's 3 req/s."""
    global _last_call
    with _rate_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _request(cfg, fp):
    """Raw POST to the lookup endpoint. Returns the `results` list ([] on any
    failure - network errors never escape this module)."""
    key = str((cfg or {}).get("acoustid_api_key") or "").strip()
    if not key:
        return []
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
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return []
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return []
    results = payload.get("results")
    return results if isinstance(results, list) else []


def parse_payload(payload, min_score=0.0):
    """AcoustID JSON -> candidate rows, best score first.

    Rows: {"score", "recording_id", "title", "artists", "release_group_id",
    "release_group_title", "release_group_type"}.
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
    """Fingerprint one track and return its candidate rows, best first."""
    if not available(cfg):
        return []
    fp = fingerprint(path, cfg)
    if not fp:
        return []
    rows = parse_payload({"status": "ok", "results": _request(cfg, fp)},
                         min_score(cfg))
    for row in rows:
        # Keep the fingerprint with the row: accepting a match writes it as
        # ACOUSTID_FINGERPRINT, and re-running fpcalc for that would double
        # the cost of every accepted import.
        row["fingerprint"] = fp.get("fingerprint")
    return rows


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
def match_release(cfg, paths, progress=None):
    """Modal release group across an album's tracks, or None.

    Rule: a release group must own at least `max(2, ceil(total * 0.4))` of the
    fingerprinted tracks (`total` = tracks attempted, capped at MAX_TRACKS) and
    their mean match score must reach `acoustid_min_score`. Single-track hits
    therefore never decide an album.
    """
    if not available(cfg):
        return None
    paths = [p for p in (paths or []) if p]
    if not paths:
        return None
    paths = paths[:MAX_TRACKS]
    floor = min_score(cfg)
    total = len(paths)

    groups = {}          # release_group_id -> {"rows": [...], "recordings": [...]}
    for done, path in enumerate(paths, 1):
        for row in lookup(cfg, path):
            gid = row.get("release_group_id")
            if not gid or row["score"] < floor:
                continue
            entry = groups.setdefault(gid, {"rows": [], "recordings": []})
            entry["rows"].append(row)
            entry["recordings"].append({
                "path": path,
                "recording_id": row["recording_id"],
                "title": row["title"],
                "score": row["score"],
                "fingerprint": row.get("fingerprint"),
            })
            break  # one row per track: the best-scoring candidate
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
        return None
    gid, matched, mean, entry = best
    first = entry["rows"][0]
    artists = []
    for row in entry["rows"]:
        for name in row["artists"]:
            if name not in artists:
                artists.append(name)
    return {
        "release_group_id": gid,
        "release_group_title": first["release_group_title"],
        "release_group_type": first["release_group_type"],
        "artists": artists,
        "score": mean,
        "matched": matched,
        "total": total,
        "recordings": entry["recordings"],
    }


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #
def available(cfg):
    """True when AcoustID can actually be used with this config."""
    cfg = cfg or {}
    if not cfg.get("acoustid_enabled"):
        return False
    if not str(cfg.get("acoustid_api_key") or "").strip():
        return False
    return fpcalc_path(cfg) is not None


def acoustid_enabled_note(cfg):
    """Why AcoustID is unavailable, or "" when it is available.

    Used by the Import UI and the settings page.
    """
    cfg = cfg or {}
    if not cfg.get("acoustid_enabled"):
        return "AcoustID disabled in settings"
    if not str(cfg.get("acoustid_api_key") or "").strip():
        return "no API key"
    if fpcalc_path(cfg) is None:
        return "fpcalc not installed"
    return ""
