"""MusicBrainz-driven automated Soulseek importing.

Given a specific MusicBrainz release, this module finds the best matching
folder on the Soulseek network, verifies it before committing to the full
download, downloads it, audits it, and imports it into the library — the
whole pipeline a careful human would do by hand:

  1. build search queries from the release's identifiable traits (catalog
     number + country for CD rips; title + year for digital media; every
     term is customizable in Settings → Soulseek),
  2. search slskd and score the candidate folders (completeness vs the
     release track list, cue/log presence per disc, lossless, free slot),
  3. download ONLY the .log file(s) first and grade them with Logchecker;
     any disc below the configured score (default 100) moves on to the
     next candidate without downloading the album,
  4. download the whole folder, verify every track against the .log CRCs
     (CD) or decode-check each file (digital media),
  5. import into the library: stamp the MusicBrainz release/recording IDs
     that drove the search into the tags, write MEDIA, organize with the
     naming script, then run the usual tagging chain.

Progress is reported through mlo.stats.progress_hook (the same relay the
WebSocket /ws/progress endpoint forwards to the UI) and mirrored into a
pollable job state for the Soulseek page.
"""
import os
import re
import shutil
import threading
import time
import traceback
import unicodedata

from mlo.config import load_config

# --------------------------------------------------------------------------- #
# Job state
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_job = {
    "state": "idle",       # idle | running | done | error | cancelled
    "stage": "",           # human-readable current step
    "release": None,       # compact release summary
    "log": [],             # [{t, msg}] progress lines (newest last)
    "attempts": [],        # [{username, dir, reason}] rejected candidates
    "result": None,        # {album_path, imported, organized}
    "cancel": False,
}


def job_state():
    with _lock:
        return {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                for k, v in _job.items() if k != "cancel"}


def cancel():
    with _lock:
        if _job["state"] == "running":
            _job["cancel"] = True
            _log("Cancellation requested — will stop after the current step.")
            return True
    return False


def _log(msg):
    with _lock:
        _job["log"].append({"t": time.strftime("%H:%M:%S"), "msg": str(msg)})
        del _job["log"][:-400]  # keep the ring buffer bounded
        if msg:
            _job["stage"] = str(msg)


def _finish(state, result=None):
    with _lock:
        _job["state"] = state
        _job["result"] = result
        if state == "done":
            _job["stage"] = "Done"


# --------------------------------------------------------------------------- #
# Query building (customizable trait templates)
# --------------------------------------------------------------------------- #
def _norm_text(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip()


def release_queries(release, cfg):
    """Search queries from the release's traits, in configured priority order.

    Each template is a space-separated list of field names; supported fields:
    artist, album, year, date, country, catalognumber, barcode, label.
    CD releases default to the catalog number (the only trait commonly
    present in rip folder names), digital media to "artist album year".
    """
    is_cd = "CD" in (release.get("medium_formats") or [])
    key = "soulseek_auto_cd_queries" if is_cd else "soulseek_auto_digital_queries"
    templates = cfg.get(key) or []
    if isinstance(templates, str):
        templates = [templates]
    if not templates:
        templates = (["catalognumber", "artist album catalognumber", "artist album"]
                     if is_cd else ["artist album year", "artist album"])

    fields = {
        "artist": _norm_text((release.get("artists") or [{}])[0].get("name", "")),
        "album": _norm_text(release.get("title", "")),
        "year": str(release.get("date") or "")[:4],
        "date": str(release.get("date") or ""),
        "country": str(release.get("country") or ""),
        "catalognumber": _norm_text(release.get("catalog_number", "")),
        "barcode": str(release.get("barcode") or ""),
        "label": _norm_text(release.get("label", "")),
    }
    queries = []
    for tpl in templates:
        parts = [fields.get(f.strip().lower(), "") for f in str(tpl).split()]
        q = _norm_text(" ".join(p for p in parts if p))
        if q and q not in queries:
            queries.append(q)
    return queries


# --------------------------------------------------------------------------- #
# Candidate folders from search results
# --------------------------------------------------------------------------- #
_AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma", ".aiff", ".alac", ".ape", ".wv"}
_DISCBASE_RE = re.compile(r"(?:cd|disc|dvd|bd)\s*\d+$", re.IGNORECASE)
_LEAD_NUM_RE = re.compile(r"^(\d{1,2})(?:\s*[-._]|\s+|\))\s*(\d{1,3})(?:\s*[-._]|\s+|\)|$)")  # 1-02 style
_TRACKNO_RE = re.compile(r"^(\d{1,3})(?:\s*[-._]|\s+|\)|$)")


def _album_root(path):
    """Fold per-disc subfolders (…/CD1/01 - x.flac) into one album root."""
    d = os.path.dirname(path.replace("\\", "/"))
    parts = d.rstrip("/").split("/")
    if len(parts) >= 2 and _DISCBASE_RE.search(parts[-1]):
        d = "/".join(parts[:-1])
    return d if d.endswith("/") else d + "/"


def _parent_root(root):
    """The directory above an album root (…/Wish You Were Here/{Log,Cue,Music}
    → …/Wish You Were Here/) — used to merge split releases."""
    p = root.replace("\\", "/").rstrip("/")
    parent = os.path.dirname(p)
    if not parent or parent == p:
        return root
    return parent if parent.endswith("/") else parent + "/"


def _disc_of(path):
    m = re.search(r"(?:cd|disc|dvd|bd)\s*(\d{1,2})", os.path.basename(os.path.dirname(path)), re.IGNORECASE)
    return int(m.group(1)) if m else 1


def _parse_trackno(path):
    base = os.path.basename(path)
    m = _LEAD_NUM_RE.match(base)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _TRACKNO_RE.match(base)
    return (1, int(m.group(1))) if m else (1, None)


def find_candidates(results, release, cfg):
    """Score search results against the release track list.

    Returns candidates sorted best-first: [{username, dir, files, audio,
    logs, cues, complete, score, lossless, slot, queue}]. A candidate is
    complete when every expected track (across all discs) has a matching
    file (matched by track number, falling back to normalized title +
    duration) and — for CD releases — every disc carries a .log and .cue.

    Split layouts (a parent folder holding SEPARATE sub-directories for the
    music, .log and .cue files) have no single directory that carries the
    release — when no per-directory candidate passes, each parent folder is
    re-evaluated as one merged candidate."""
    min_ratio = float(cfg.get("soulseek_auto_complete_ratio", 1.0) or 1.0)
    expected = []  # [{disc, pos, title, length}]
    for t in release.get("media") or []:
        expected.append({
            "disc": int(t.get("disc") or 1),
            "pos": int(t.get("position") or 0),
            "title": _norm_text(t.get("title") or ""),
            "length": float(t.get("length") or 0) / 1000.0,
        })
    discs_expected = sorted({t["disc"] for t in expected})
    is_cd = "CD" in (release.get("medium_formats") or [])

    groups = {}
    for f in results:
        ext = os.path.splitext(f.get("file") or "")[1].lower()
        if ext not in (_AUDIO_EXTS | {".log", ".cue"}):
            continue
        root = _album_root(f["file"])
        key = (f["username"], root)
        groups.setdefault(key, []).append(f)

    def evaluate(username, root, files):
        audio = [f for f in files if os.path.splitext(f["file"])[1].lower() in _AUDIO_EXTS]
        logs = [f for f in files if f["file"].lower().endswith(".log")]
        cues = [f for f in files if f["file"].lower().endswith(".cue")]
        if not audio:
            return None
        log_discs = {_disc_of(f["file"]) for f in logs}
        cue_discs = {_disc_of(f["file"]) for f in cues}
        missing_logs = [d for d in discs_expected if d not in log_discs] if is_cd else []
        missing_cues = [d for d in discs_expected if d not in cue_discs] if is_cd else []

        # match expected tracks to files
        used = set()
        matched = 0
        for t in expected:
            hit = None
            for f in audio:
                if id(f) in used:
                    continue
                d, pos = _parse_trackno(f["file"])
                if d == t["disc"] and pos == t["pos"]:
                    hit = f
                    break
            if hit is None and t["title"]:
                for f in audio:
                    if id(f) in used:
                        continue
                    stem = _norm_text(os.path.splitext(os.path.basename(f["file"]))[0])
                    dur = float(f.get("duration") or 0)
                    if t["title"] and t["title"] in stem:
                        if not t["length"] or not dur or abs(dur - t["length"]) <= 15:
                            hit = f
                            break
            if hit is not None:
                used.add(id(hit))
                matched += 1
        complete = (matched == len(expected)) and not missing_logs and not missing_cues
        acceptable = matched >= min_ratio * len(expected) and not missing_logs and not missing_cues
        if not acceptable:
            return None
        lossless = any(os.path.splitext(f["file"])[1].lower() in (".flac", ".wav", ".aiff", ".alac", ".ape", ".wv") for f in audio)
        slot = bool(files[0].get("slot"))
        queue = min(int(f.get("queue") or 0) for f in files)
        total = sum(int(f.get("size") or 0) for f in audio)
        score = (5 * matched + 2 * len(logs) + len(cues) + 8 * lossless + 4 * slot - queue / 100.0)
        return {
            "username": username, "dir": root, "files": files, "audio": audio,
            "logs": logs, "cues": cues, "matched": matched, "expected": len(expected),
            "complete": complete, "lossless": lossless, "slot": slot,
            "queue": queue, "total_size": total, "score": round(score, 1),
        }

    candidates = []
    for (username, root), files in groups.items():
        c = evaluate(username, root, files)
        if c:
            candidates.append(c)

    if not candidates:
        # split-layout rescue: merge every root group that shares a parent
        # (…/Album/{Log,Cue,Music}) and score the union — but only when at
        # least two different directories actually contribute, so normal
        # single-folder and multi-disc layouts are never double-counted.
        children = {}  # (username, parent_root) -> {root: files}
        for (username, root), files in groups.items():
            children.setdefault((username, _parent_root(root)), {}).setdefault(root, []).extend(files)
        for (username, proot), per_root in children.items():
            if len(per_root) < 2:
                continue
            union = [f for flist in per_root.values() for f in flist]
            c = evaluate(username, proot, union)
            if c:
                candidates.append(c)

    candidates.sort(key=lambda c: (-c["score"], c["queue"]))
    return candidates


# --------------------------------------------------------------------------- #
# slskd helpers (search, wait, locate local files)
# --------------------------------------------------------------------------- #
def _search_once(slsk, query, wait_s):
    """Run one search and collect responses for `wait_s` seconds.

    The per-request `timeout` (milliseconds, slskd-side) stretches the
    search itself — slskd's own default cuts searches off long before slow
    responders have reported, which starved candidate discovery."""
    sid = slsk.search(query, timeout_ms=int(wait_s * 1000))
    deadline = time.time() + wait_s
    best = {"responses": [], "state": None}
    while time.time() < deadline:
        time.sleep(2.0)
        try:
            res = slsk.search_results(sid)
        except Exception:
            continue
        best = res
        if getattr(slsk, "is_search_done", None) and slsk.is_search_done(res):
            # give late responses 2 more seconds, then stop
            time.sleep(2.0)
            try:
                best = slsk.search_results(sid)
            except Exception:
                pass
            break
        state_str = str(res.get("state") or "")
        if any(x in state_str for x in ("Completed", "TimedOut", "ResponseLimitReached", "Cancelled")):
            time.sleep(2.0)
            try:
                best = slsk.search_results(sid)
            except Exception:
                pass
            break
    return best.get("responses") or []


def _remote_rel(remote_path):
    """slskd mirrors <share>/<rel> under <downloads>/<username>/<rel>."""
    p = remote_path.replace("\\", "/").lstrip("/")
    drive, rest = p[:2], p[2:]
    if re.match(r"^[A-Za-z]:$", drive):
        return rest
    return p


def _local_download_candidates(ddir, username, remote, size):
    """All plausible local locations for a remote file: slskd mirrors the
    remote directory tree under <ddir>/<username>/, but path sanitization
    (drive letters, share prefixes) varies between versions, so fall back
    to a basename+size scan of the user's download tree."""
    out = []
    exact = os.path.join(ddir, username, _remote_rel(remote))
    if os.path.isfile(exact):
        out.append(exact)
    user_dir = os.path.join(ddir, username)
    base = os.path.basename(remote.replace("\\", "/"))
    for root, _dirs, files in os.walk(user_dir):
        if base in files:
            p = os.path.join(root, base)
            if p not in out and (not size or os.path.getsize(p) == size):
                out.append(p)
    return out


def _wait_for_files(slsk, ddir, username, wanted, timeout_s, cancel_check=None):
    """Poll until every wanted remote path exists locally (size verified).

    wanted = [{filename, size}]. Returns {remote: local_path} for the files
    that arrived complete; missing entries are absent from the dict.
    """
    deadline = time.time() + timeout_s
    pending = {w["filename"]: w for w in wanted}
    got = {}
    while time.time() < deadline and pending:
        if cancel_check and cancel_check():
            return got
        time.sleep(3.0)
        for remote in list(pending):
            cands = _local_download_candidates(ddir, username, remote,
                                               int(pending[remote].get("size") or 0))
            if cands:
                got[remote] = cands[0]
                del pending[remote]
        # still queued?  keep polling until the deadline
    return got


# --------------------------------------------------------------------------- #
# Verification helpers
# --------------------------------------------------------------------------- #
def _score_logs(local_logs, cfg):
    """Grade rip logs. Returns [(path, score_or_None, checksum_state, detail)]."""
    from mlo.discs import score_disc_log, check_log_checksum, read_log_text

    out = []
    for p in sorted(local_logs):
        try:
            score = score_disc_log(p)
        except Exception:
            score = None
        state, detail = check_log_checksum(p)
        if state is None and not re.search(r"====\s*Log checksum", read_log_text(p) or "", re.IGNORECASE):
            state = "unsupported"
        out.append((p, score, state, detail))
    return out


def _verify_album(album_dir, cfg, is_cd):
    """Post-download verification. Returns (ok, problems[]).

    CD: every track's decoded-PCM CRC must match its .log checksum
    (mlo.discs.verify_album_checksums). Digital media: every file must
    decode cleanly (flac -t / ffmpeg decode-to-null). The full AudioAuditor
    pass runs later as part of the normal pipeline — only .log matching (CD)
    and decodability matter for accepting or rejecting a download.
    """
    problems = []
    from mlo.audio import AudioFile
    from mlo.tools import detect_all_tools
    from mlo import stats as stats_mod

    audio = []
    for root, _dirs, files in os.walk(album_dir):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in _AUDIO_EXTS:
                audio.append(os.path.join(root, f))
    if not audio:
        return False, ["no audio files found in the download"]

    if is_cd:
        # verify_album_checksums keys off MEDIA=CD — the tags were just
        # written for exactly this reason.
        ffmpeg = (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")
        if not ffmpeg:
            return False, ["ffmpeg not available for CRC verification"]
        from mlo.discs import verify_album_checksums
        verdicts, unverified = verify_album_checksums(ffmpeg, album_dir, audio, cfg)
        for p, v in verdicts.items():
            if v != "REAL":
                problems.append(f"{os.path.basename(p)}: CRC mismatch vs .log (FAKE)")
        for p, reason in unverified.items():
            problems.append(f"{os.path.basename(p)}: not verifiable ({reason})")
        return not problems, problems

    # digital media / other: decode-check each file
    tools = detect_all_tools()
    ffmpeg = (tools.get("ffmpeg") or {}).get("ffmpeg_exe")
    flac = (tools.get("flac") or {}).get("flac_exe")
    from concurrent.futures import ThreadPoolExecutor
    from mlo.subproc import run_tool
    import subprocess

    def check_one(p):
        try:
            if p.lower().endswith(".flac") and flac:
                proc = run_tool([flac, "-t", "-s", p],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                timeout=120)
                return None if proc.returncode == 0 else f"flac -t failed (rc={proc.returncode})"
            if ffmpeg:
                proc = run_tool([ffmpeg, "-v", "error", "-i", p, "-f", "null", "-"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                timeout=120, text=True, errors="replace")
                err = (proc.stderr or "").strip()
                if proc.returncode != 0 or err:
                    return f"decode error: {err[:120]}" if err else f"decode rc={proc.returncode}"
            return None
        except Exception as e:
            return str(e)[:120]

    with ThreadPoolExecutor(max_workers=min(4, len(audio))) as ex:
        for p, err in zip(audio, ex.map(check_one, audio)):
            if err:
                problems.append(f"{os.path.basename(p)}: {err}")
    return not problems, problems


# --------------------------------------------------------------------------- #
# Tag stamping + import
# --------------------------------------------------------------------------- #
def _stamp_mb_tags(album_dir, release, cfg):
    """Write the exact MusicBrainz release identity into the tags so beets /
    grading work with the release that drove the search — album-level IDs on
    every track, plus the per-track recording ID matched by disc/position or
    title+duration."""
    from mlo.audio import AudioFile

    tracks_meta = release.get("media") or []
    artist_mbid = ((release.get("artists") or [{}])[0].get("mbid")) or ""
    album_tags = {
        "MUSICBRAINZ_ALBUMID": release.get("id", ""),
        "MUSICBRAINZ_RELEASEGROUPID": release.get("release_group_id", ""),
        "MUSICBRAINZ_ARTISTID": artist_mbid,
        "CATALOGNUMBER": release.get("catalog_number", ""),
        "LABEL": release.get("label", ""),
        "BARCODE": release.get("barcode", ""),
        "DATE": release.get("date", ""),
        "ORIGINALDATE": release.get("originaldate", ""),
        "COUNTRY": release.get("country", ""),
    }

    audio = []
    for root, _dirs, files in os.walk(album_dir):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in _AUDIO_EXTS:
                audio.append(os.path.join(root, f))

    # match release tracks -> files (position first, then title+duration)
    assign = {}
    used = set()
    for t in tracks_meta:
        hit = None
        for p in audio:
            if p in used:
                continue
            d, pos = _parse_trackno(p)
            if d == int(t.get("disc") or 1) and pos == int(t.get("position") or 0):
                hit = p
                break
        if hit is None:
            title = _norm_text(t.get("title") or "")
            for p in audio:
                if p in used:
                    continue
                stem = _norm_text(os.path.splitext(os.path.basename(p))[0])
                if title and title in stem:
                    hit = p
                    break
        if hit is not None:
            used.add(hit)
            assign[hit] = t

    n = 0
    for p in audio:
        try:
            af = AudioFile(p)
            if af.audio is None:
                continue
            for k, v in album_tags.items():
                if v and not str(af.get_tag(k) or "").strip():
                    af.set_tag(k, v)
            t = assign.get(p)
            if t and t.get("recording_mbid"):
                af.set_tag("MUSICBRAINZ_TRACKID", t["recording_mbid"])
            n += 1
        except Exception:
            continue
    return n


# --------------------------------------------------------------------------- #
# The orchestrator
# --------------------------------------------------------------------------- #
def start_job(release_mbid=None, release=None, queries=None, username=None,
              target_dir=None):
    """Kick off an auto-import job in a daemon thread; returns the job state.

    release — a full release dict (from integrations.release_lookup); when
    only release_mbid is given it is fetched here. queries overrides the
    configured search templates for this run. username+target_dir downloads
    that exact user/folder without searching (manual entry).
    """
    with _lock:
        if _job["state"] == "running":
            return {"ok": False, "error": "a job is already running", "job": job_state()}
        _job.update({"state": "running", "stage": "Starting…", "log": [],
                     "attempts": [], "result": None, "cancel": False,
                     "release": {"id": release_mbid} if release_mbid else None})
    threading.Thread(target=_run, name="mlo-soulseek-auto",
                     kwargs=dict(release_mbid=release_mbid, release=release,
                                 queries=queries, username=username,
                                 target_dir=target_dir),
                     daemon=True).start()
    return {"ok": True, "job": job_state()}


def _cancelled():
    with _lock:
        return _job["cancel"]


def _reject(username, folder, reason):
    with _lock:
        _job["attempts"].append({"username": username, "dir": folder, "reason": str(reason)[:200]})


def _run(release_mbid=None, release=None, queries=None, username=None, target_dir=None):
    from server import soulseek as slsk
    from server import integrations as intg

    cfg = load_config()
    try:
        if not (slsk.is_running() or slsk.web_up(cfg)):
            raise RuntimeError("slskd is not running — start Soulseek first")
        server = slsk.server_state(cfg)
        if not (server or {}).get("isLoggedIn"):
            raise RuntimeError("Soulseek is not logged in — set your Soulseek "
                               "username and password in Settings → Soulseek, "
                               "then restart slskd")
        min_score = int(cfg.get("soulseek_auto_log_min_score", 100) or 100)
        ddir = slsk.download_dir(cfg)

        # ---- release identity ------------------------------------------------
        if release is None:
            _log("Looking up the MusicBrainz release…")
            release = intg.release_lookup(release_mbid)
        is_cd = "CD" in (release.get("medium_formats") or [])
        with _lock:
            _job["release"] = {
                "id": release.get("id"), "title": release.get("title"),
                "artist": ((release.get("artists") or [{}])[0].get("name", "")),
                "date": release.get("date"), "country": release.get("country"),
                "catalog_number": release.get("catalog_number"),
                "media": is_cd and "CD" or "Digital Media",
            }
        _log(f"Target: {release.get('artists', [{}])[0].get('name', '?')} — "
             f"{release.get('title')} ({release.get('date') or 'n/a'})"
             f"{', ' + release.get('catalog_number') if release.get('catalog_number') else ''}"
             f" · {len(release.get('media') or [])} track(s) · {'CD' if is_cd else 'Digital Media'}")

        # ---- candidates ------------------------------------------------------
        if username and target_dir:
            _log(f"Manual entry: {username} · {target_dir}")
            entries = slsk.browse(username)
            prefix = target_dir.replace("\\", "/").rstrip("/").lower() + "/"
            files = []
            for d in entries or []:
                dpath = str(d.get("directory") or "").replace("\\", "/")
                if dpath.lower().rstrip("/") + "/" == prefix or dpath.lower().rstrip("/").endswith(prefix.rstrip("/")):
                    for f in d.get("files") or []:
                        files.append({
                            "username": username,
                            "file": f.get("filename") or "",
                            "size": int(f.get("size") or 0),
                            "duration": f.get("duration"),
                            "slot": False, "queue": 0, "speed": 0,
                        })
            root = _album_root(files[0]["file"]) if files else (
                target_dir if target_dir.endswith("/") else target_dir + "/")
            audio = [f for f in files if os.path.splitext(f["file"])[1].lower() in _AUDIO_EXTS]
            candidates = [{
                "username": username, "dir": root, "files": files, "audio": audio,
                "logs": [f for f in files if f["file"].lower().endswith(".log")],
                "cues": [f for f in files if f["file"].lower().endswith(".cue")],
                "matched": 0, "expected": len(release.get("media") or []),
                "complete": False, "lossless": True, "slot": False, "queue": 0,
                "total_size": sum(f["size"] for f in files), "score": 0,
            }]
        else:
            if queries:
                plans = [queries] if isinstance(queries, str) else list(queries)
                queries_built = _build_from_templates(plans, release)
            else:
                queries_built = release_queries(release, cfg)
            if not queries_built:
                raise RuntimeError("No usable search queries for this release "
                                   "(no catalog number / title available)")
            candidates = []
            search_wait = int(cfg.get("soulseek_auto_search_wait", 45) or 45)
            for q in queries_built:
                if _cancelled():
                    return _finish("cancelled")
                _log(f"Searching Soulseek: “{q}” … (waiting up to {search_wait}s)")
                responses = _search_once(slsk, q, wait_s=search_wait)
                found = find_candidates(responses, release, cfg)
                _log(f"  {len(found)} candidate folder(s) from "
                     f"{len(responses)} result file(s)")
                for c in found:
                    if not any(c["username"] == x["username"] and c["dir"] == x["dir"]
                               for x in candidates):
                        candidates.append(c)
            candidates.sort(key=lambda c: (-c["score"], c["queue"]))
            if not candidates:
                raise RuntimeError("No candidate folder contained every track "
                                   "(and cue/log per disc for CD). Try the "
                                   "manual entry or different search terms.")

        # ---- try candidates in order -----------------------------------------
        for cand in candidates:
            if _cancelled():
                return _finish("cancelled")
            uname, folder = cand["username"], cand["dir"]
            _log(f"Candidate: {uname} · …{folder[-60:]} ({cand['matched']}/{cand['expected']} tracks matched)")

            folder_files = cand["files"]

            # --- stage 1: logs only (CD) ------------------------------------
            if is_cd and cand["logs"]:
                _log("Downloading .log file(s) first for a quality check…")
                if _cancelled():
                    return _finish("cancelled")
                try:
                    slsk.enqueue_download(uname, [{"filename": f["file"], "size": f["size"]}
                                                  for f in cand["logs"]])
                except Exception as e:
                    _reject(uname, folder, f"log download failed to queue: {e}")
                    continue
                got_logs = _wait_for_files(slsk, ddir, uname,
                                           [{"filename": f["file"], "size": f["size"]}
                                            for f in cand["logs"]],
                                           timeout_s=180, cancel_check=_cancelled)
                if len(got_logs) < len(cand["logs"]):
                    _reject(uname, folder, f"only {len(got_logs)}/{len(cand['logs'])} log(s) arrived")
                    _cleanup_partial(ddir, uname, list(got_logs.values()))
                    continue
                if _cancelled():
                    return _finish("cancelled")
                _log("Grading rip log(s) with Logchecker…")
                scores = _score_logs(list(got_logs.values()), cfg)
                bad = []
                for p, score, state, detail in scores:
                    line = f"{os.path.basename(p)}: score {score if score is not None else '?'}, checksum {state or '?'}"
                    _log("  " + line)
                    if (score is None or score < min_score
                            or state in ("invalid",)):
                        bad.append(f"{os.path.basename(p)} — score {score if score is not None else 'unscorable'}"
                                   f"{', checksum ' + state if state in ('invalid',) else ''}")
                if bad:
                    _reject(uname, folder, "log rejected: " + "; ".join(bad))
                    _cleanup_partial(ddir, uname, list(got_logs.values()))
                    continue
                _log("Log(s) pass — downloading the full album…")

            # --- stage 2: full download --------------------------------------
            wanted = [{"filename": f["file"], "size": f["size"]} for f in folder_files]
            if not wanted:
                _reject(uname, folder, "no files listed for this folder")
                continue
            if _cancelled():
                return _finish("cancelled")
            try:
                slsk.enqueue_download(uname, wanted)
            except Exception as e:
                _reject(uname, folder, f"download failed to queue: {e}")
                continue
            est_timeout = max(900, int(cand["total_size"] / (200 * 1024)))  # ≥ 200 kB/s
            est_timeout = min(est_timeout, 3600 * 2)
            _log(f"Downloading {len(wanted)} file(s) "
                 f"({cand['total_size'] / (1024 * 1024):.0f} MB)…")
            got = _wait_for_files(slsk, ddir, uname, wanted,
                                  timeout_s=est_timeout, cancel_check=_cancelled)
            if _cancelled():
                return _finish("cancelled")
            if len(got) < len(wanted):
                missing = [w["filename"] for w in wanted if w["filename"] not in got]
                _reject(uname, folder, f"download incomplete: {len(missing)} file(s) missing/timed out")
                _cleanup_partial(ddir, uname, list(got.values()))
                continue

            # --- stage 3: audit -------------------------------------------------
            local_dir = os.path.dirname(list(got.values())[0])
            # the album root locally mirrors the remote folder structure
            local_root = os.path.join(ddir, uname, _remote_rel(folder).rstrip("/"))
            if not os.path.isdir(local_root):
                local_root = local_dir
            _log("Verifying downloads against the rip log / decoders…")
            media = "CD" if is_cd else "Digital Media"
            try:
                _stamp_media(local_root, media, cfg)
            except Exception:
                pass
            ok, problems = _verify_album(local_root, cfg, is_cd)
            if not ok:
                for pr in problems[:6]:
                    _log("  ✕ " + pr)
                _reject(uname, folder, f"verification failed ({len(problems)} problem(s))")
                _cleanup_partial(ddir, uname, [], remove_root=local_root)
                continue
            _log("Verification passed — importing into the library…")

            # --- stage 4: import -------------------------------------------------
            result = _import(local_root, release, cfg, is_cd)
            _finish("done", result)
            return

        raise RuntimeError("Every candidate was rejected "
                           f"({len(_job['attempts'])} attempt(s) — see the log).")

    except Exception as e:
        traceback.print_exc()
        _log(f"ERROR: {e}")
        _finish("error", {"error": str(e)})


class _log_capture:
    """Reserved for future per-template query overrides (unused today)."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _build_from_templates(templates, release):
    return release_queries({"**": None, **release,
                            "medium_formats": release.get("medium_formats")},
                           {"soulseek_auto_cd_queries": templates,
                            "soulseek_auto_digital_queries": templates})


def _stamp_media(album_dir, media, cfg):
    from mlo.audio import AudioFile
    from server.main import is_audio_file
    n = 0
    for root, _dirs, files in os.walk(album_dir):
        for f in sorted(files):
            if not is_audio_file(f):
                continue
            try:
                af = AudioFile(os.path.join(root, f))
                if af.audio is None:
                    continue
                if not str(af.get_tag("MEDIA") or "").strip():
                    af.set_tag("MEDIA", media)
                if media == "CD" and str(af.get_tag("SOURCE") or "").strip():
                    af.set_tag("SOURCE", "")
                n += 1
            except Exception:
                continue
    return n


def _cleanup_partial(ddir, username, local_files, remove_root=None):
    """Remove rejected partials so they don't linger in the download dir."""
    for p in local_files:
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass
    if remove_root:
        try:
            if os.path.isdir(remove_root) and remove_root.startswith(os.path.abspath(ddir)):
                shutil.rmtree(remove_root, ignore_errors=True)
        except Exception:
            pass
    try:
        upath = os.path.join(ddir, username)
        if os.path.isdir(upath) and not os.listdir(upath):
            os.rmdir(upath)
    except OSError:
        pass


def _import(local_root, release, cfg, is_cd):
    """Move the verified download into the library and run the pipeline."""
    from server import soulseek as slsk
    from server import main as srv
    from server.main import OrganizeRequest

    folder = str(cfg.get("music_folder") or "").strip()
    if not folder or not os.path.isdir(folder):
        raise RuntimeError("music_folder is not configured")

    name = f"{(release.get('artists') or [{}])[0].get('name', '')} - {release.get('title', '')}".strip(" -")
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip() or "Soulseek Import"
    dest = os.path.join(folder, safe)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(folder, f"{safe} ({n})")
        n += 1

    shutil.move(local_root, dest)
    _log(f"Moved into the library: {os.path.basename(dest)}")

    # exact MusicBrainz identity + MEDIA before organizing
    try:
        _stamp_mb_tags(dest, release, cfg)
    except Exception:
        traceback.print_exc()
    try:
        from server.main import _tag_media_for_albums
        _tag_media_for_albums([dest])
    except Exception:
        traceback.print_exc()

    organized = False
    organize_error = None
    try:
        r = srv.organize(OrganizeRequest(paths=[dest], dry_run=False))
        res = (r.get("results") or [{}])[0] if isinstance(r, dict) else r.results[0]
        if isinstance(res, dict) and res.get("error"):
            organize_error = res["error"]
        else:
            organized = True
            _log("Organized with the naming script.")
    except Exception as e:
        organize_error = str(e)

    srv._run_background_tagging()
    _log("Background tagging chain started (autotag → lyrics → grade).")
    return {"album_path": dest, "imported": True,
            "organized": organized, "organize_error": organize_error}
