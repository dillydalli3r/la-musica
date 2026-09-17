"""MusicBrainz-driven automated Soulseek importing.

Given a specific MusicBrainz release, this module finds the best matching
folder on the Soulseek network, verifies it before committing to the full
download, downloads it, audits it, and imports it into the library — the
whole pipeline a careful human would do by hand:

  1. build search queries from the release's identifiable traits (catalog
     number + country for CD rips; title + year for digital media; every
     term is customizable in Settings → Soulseek),
  2. search slskd with every query template at once and score the candidate
     folders (completeness vs the release track list, cue/log presence per
     disc, lossless, free slot),
  3. queue the whole folder in ONE pass but grade the .log file(s) first with
     Logchecker; any disc below the configured score (default 100) drops the
     transfers again and moves on to the next candidate without waiting for
     the album,
  4. wait for the album (a file counts as arrived only once slskd itself says
     the transfer finished) and verify every track against the .log CRCs (CD)
     or decode-check each file (digital media),
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
# Reentrant: cancel() holds the lock while calling _log(), which locks again.
_lock = threading.RLock()
_job = {
    # idle | running | confirm | done | error | cancelled
    #   confirm = only lossy copies passed the search; waiting for the user
    "state": "idle",
    "stage": "",           # human-readable current step
    "release": None,       # compact release summary
    "log": [],             # [{t, msg}] progress lines (newest last)
    "attempts": [],        # [{username, dir, reason}] rejected candidates
    "result": None,        # {album_path, imported, organized}
    # {reason, formats, candidates} (lossy_only) or
    # {reason, waited, queries, formats, candidates} (no_results)
    "confirm": None,
    "search": None,        # live per-query progress while searching
    "progress": None,      # live download metrics while a transfer runs
    "cancel": False,
}
# A job parked on a prompt waits here for the user's answer (confirm()):
# "only lossy copies found" and "no usable results — add to wishes?".
_confirm_event = threading.Event()
_confirm_answer = {"accept": False}


def _job_search_done():
    """Clear the live search progress once a query has been scored."""
    with _lock:
        _job["search"] = None


def _job_search_progress(query, elapsed, wait_s, res):
    """Publish search progress so the UI can show a moving bar instead of a
    single frozen "Searching…" line.

    `wait` is the requested window; the search itself keeps being polled for
    `_SEARCH_GRACE_S` past it (slskd only hands back responses once a search
    has ENDED), so `deadline_s` names the real cap and `remaining` counts down
    to the requested window. `elapsed` is clamped to `wait` — the UI must
    never print a search that overran the window it advertised."""
    with _lock:
        _job["search"] = {
            "query": query,
            "elapsed": int(min(elapsed, wait_s)),
            "wait": int(wait_s),
            "deadline_s": int(wait_s + _SEARCH_GRACE_S),
            "remaining": int(max(0, wait_s - elapsed)),
            "state": str(res.get("state") or ""),
            "responses": int(res.get("responseCount") or 0),
            "files": int(res.get("fileCount") or 0),
        }


def _job_progress(payload):
    """Publish (or clear, with None) the live download block of the job."""
    with _lock:
        _job["progress"] = payload


def job_state():
    with _lock:
        return {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                for k, v in _job.items() if k != "cancel"}


def confirm(accept):
    """Answer the job's pending prompt.

    Both prompts a job can park on end here — "only lossy copies found" and
    "no usable results — add to wishes?" — because the waiter is one event
    either way; which question was asked is in job_state()["confirm"]["reason"]
    and the answer is only ever yes/no. Returns False when no prompt is pending
    (the job moved on, or was cancelled) — the caller must not read that as an
    accepted download."""
    with _lock:
        if _job["state"] != "confirm" or _job["cancel"]:
            return False
        _job["state"] = "running"
        _job["confirm"] = None
        _confirm_answer["accept"] = bool(accept)
    _confirm_event.set()
    return True


def cancel():
    with _lock:
        if _job["state"] in ("running", "confirm"):
            _job["cancel"] = True
            _log("Cancellation requested — will stop after the current step.")
            released = True
        else:
            released = False
    if released:
        _confirm_event.set()  # a job parked on a prompt must wake up
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
        _job["progress"] = None   # nothing is downloading any more
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
_AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".oga", ".opus",
               ".wav", ".wma", ".aiff", ".aif", ".alac", ".ape", ".wv", ".shn",
               ".tta", ".mpc", ".mp2", ".mka", ".dsf", ".dff"}
# Lossless codecs a candidate folder can hold; everything else the network
# offers (mp3, m4a/aac, ogg, opus, wma...) is lossy. Auto-import prefers
# these folders, and only downloads lossy after the user says so.
# `.m4a`/`.mp4` are listed under both — the container holds ALAC or AAC and
# only a probe of the downloaded file can tell them apart (mlo.flac.is_alac).
_LOSSLESS_EXTS = {".flac", ".wav", ".aiff", ".aif", ".alac", ".ape", ".wv",
                  ".shn", ".tta", ".dsf", ".dff"}
# slskd only hands back a search's responses once the search has ENDED (while
# it runs the API reports counts only), so a poll window that closes as the
# search's own timeout fires returns an empty result set for a search that
# saw hundreds of hits. Poll past the requested window until slskd reports a
# terminal state.
_SEARCH_GRACE_S = 45.0

_LEAD_NUM_RE = re.compile(r"^(\d{1,2})(?:\s*[-._]|\s+|\))\s*(\d{1,3})(?:\s*[-._]|\s+|\)|$)")  # 1-02 style
_TRACKNO_RE = re.compile(r"^(\d{1,3})(?:\s*[-._]|\s+|\)|$)")
# "Album - 01 - Title.flac": the flat share layout, track number mid-name.
_MID_NUM_RE = re.compile(r"[-_.]\s*(\d{1,3})\s*[-_.]\s*\S")
_TAIL_NUM_RE = re.compile(r"(\d{1,3})\s*$")   # "track01"
# The disc token a folder or file name can carry ("CD1", "Disc 2", "Disk1",
# "Volume 1"), ignoring the decoration shares wrap their folders in
# ("CD1 [FLAC]").
_DISC_NUM_RE = re.compile(r"(?:^|[^A-Za-z0-9])(?:cd|disc|disk|dvd|bd|volume|vol)"
                          r"\s*[-_.]?\s*(\d{1,2})(?![0-9])", re.IGNORECASE)
_DISC_DIR_RE = re.compile(r"(?:cd|disc|disk|dvd|bd|volume|vol)\s*[-_.]?\s*\d{1,2}",
                          re.IGNORECASE)
_DECOR_RE = re.compile(r"[\s._-]*[\(\[]([^\)\]]*)[\)\]][\s._-]*$")
_LEAD_DISC_RE = re.compile(r"^(\d{1,2})\s*-\s*\d{1,3}(?:\D|$)")     # 1-03 style
_BRACKET_DISC_RE = re.compile(r"^[\(\[]\s*(\d{1,2})\s*[\)\]]\s*")   # (1) 01 - Title


def _strip_decor(name):
    """"CD1 [FLAC]" -> "CD1"; "Album (1994)" -> "Album"."""
    name = str(name or "")
    while True:
        m = _DECOR_RE.search(name)
        if not m:
            return name.strip()
        name = name[:m.start()]


def _is_disc_dir(name):
    """True when a directory name is a disc folder — "CD1", "CD 1", "Disk1",
    "Disk 2", "Volume 1", "CD1 [FLAC]" — rather than an album folder that
    merely carries a decoration ("Album [FLAC]", "Album (1994)")."""
    bare = _strip_decor(name)
    return bool(bare) and _DISC_DIR_RE.fullmatch(bare) is not None


def _disc_number(name):
    """Disc number a folder or file NAME claims ("CD1", "Disc 2 [FLAC]",
    "1-03 rip", "(2) 01 x"), or None when the name says nothing."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    stem = _strip_decor(os.path.splitext(base)[0] or base)
    m = _DISC_NUM_RE.search(stem)
    if m:
        return int(m.group(1))
    m = _LEAD_DISC_RE.match(stem)
    if m:
        return int(m.group(1))
    m = _BRACKET_DISC_RE.match(stem)
    return int(m.group(1)) if m else None


def _album_root(path):
    """Fold per-disc subfolders (…/CD1/01 - x.flac, …/Disc 1/…,
    …/Disk 2/…, …/CD1 [FLAC]/…) into one album root."""
    d = os.path.dirname(path.replace("\\", "/"))
    parts = d.rstrip("/").split("/")
    if len(parts) >= 2 and _is_disc_dir(parts[-1]):
        d = "/".join(parts[:-1])
    return d if d.endswith("/") else d + "/"


def _parent_dir(root):
    """The directory above an album root (…/Album/{Log+Cue,Music} → …/Album/);
    "" at the top of the share."""
    p = root.replace("\\", "/").rstrip("/")
    parent = os.path.dirname(p)
    if not parent:
        return ""
    return parent if parent.endswith("/") else parent + "/"


def _ancestors(root, levels=2):
    """The root itself, then up to `levels` directories above it — the
    covering directories a release can hide in (per-track folders, a
    Log+Cue/Music split)."""
    out = [root]
    p = root
    for _ in range(levels):
        parent = _parent_dir(p)
        if not parent or parent == p:
            break
        out.append(parent)
        p = parent
    return out


def _named_disc(path):
    """Disc a folder/file path explicitly names — its folder first, then its
    own name (…/CD2/01 - x.flac, …/CD1.log, …/1-02 x.flac) — or None."""
    p = str(path).replace("\\", "/")
    return _disc_number(os.path.dirname(p)) or _disc_number(os.path.basename(p))


def _disc_of(path):
    """Disc a file belongs to (1 when nothing names one)."""
    return _named_disc(path) or 1


def _parse_trackno(path):
    """(disc, position) from a filename; the folder names the disc when the
    file name does not ("01 - x.flac" inside …/CD2/ is disc 2, track 1)."""
    p = str(path).replace("\\", "/")
    disc = _disc_of(p)
    base = os.path.basename(p)
    m = _BRACKET_DISC_RE.match(base)          # "(1) 01 - Title.flac"
    if m:
        disc = int(m.group(1))
        base = base[m.end():]
    m = _LEAD_NUM_RE.match(base)              # "1-02 Title" names the disc
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _TRACKNO_RE.match(base)               # "01 Title", "01.Title", "01_Title"
    if m:
        return disc, int(m.group(1))
    m = _MID_NUM_RE.search(os.path.splitext(base)[0])    # "Album - 01 - Title"
    if m:
        return disc, int(m.group(1))
    m = _TAIL_NUM_RE.search(os.path.splitext(base)[0])   # "track01.flac"
    return (disc, int(m.group(1))) if m else (disc, None)


def _rank(c):
    """Candidate order: lossless folders first, then best score, then queue.

    A lossy folder that scores perfectly is still worse than a lossless one
    that only just matched — the download is the irreversible part."""
    return (not c["lossless"], -c["score"], c["queue"])


def _same_folder(a, b):
    """Whether two slskd folder paths name the same directory (case- and
    separator-insensitive). Plain endswith() accepts …/NotAlbum for …/Album."""
    a = str(a or "").replace("\\", "/").rstrip("/").lower()
    b = str(b or "").replace("\\", "/").rstrip("/").lower()
    return a == b


def _under(path, folder):
    """Whether `path` sits inside `folder` (both slskd paths, component-aware:
    …/Album/CD1 is under …/Album, …/NotAlbum is not)."""
    p = str(path or "").replace("\\", "/").lower()
    f = str(folder or "").replace("\\", "/").rstrip("/").lower()
    return bool(f) and p.startswith(f + "/")


def _expected_tracks(release):
    """[{disc, pos, title, length}] from a release's track list."""
    return [{
        "disc": int(t.get("disc") or 1),
        "pos": int(t.get("position") or 0),
        "title": _norm_text(t.get("title") or ""),
        "length": float(t.get("length") or 0) / 1000.0,
    } for t in (release.get("media") or [])]


def _disc_numbers(release):
    """The disc numbers a release expects ([1] when it names none)."""
    return sorted({int(t.get("disc") or 1)
                   for t in (release.get("media") or [])}) or [1]


def _select_logs(logs, discs, root):
    """ONE .log per expected disc, picked deterministically.

    A log that names its disc (CD1 / Disc 2 / Disk 1 / Volume 1, "1-03 …", or
    the folder it sits in) wins for that disc; then a log in the album root;
    then any log that names no disc. Ties break on the shortest path, then
    lexicographically — so a folder holding two logs for one disc enqueues
    exactly one of them, and never a second log for the same disc."""
    picked, used = [], set()
    loose = [l for l in logs if _named_disc(l["file"]) is None]
    root_logs = [l for l in loose
                 if _same_folder(os.path.dirname(l["file"]), root)]
    for d in discs:
        named = [l for l in logs if _named_disc(l["file"]) == d]
        for tier in (named, root_logs, loose):
            fresh = [l for l in tier if l["file"] not in used]
            if fresh:
                pick = min(fresh, key=lambda l: (len(l["file"]), l["file"].lower()))
                used.add(pick["file"])
                picked.append(pick)
                break
    return picked


def _title_in(f, t):
    """Whether the expected track's title shows up in a file's name."""
    if not t["title"]:
        return False
    stem = _norm_text(os.path.splitext(os.path.basename(f["file"]))[0])
    return t["title"] in stem


def _image_rip_audio(audio, cues, expected):
    """The audio files of a disc-image rip ("Album.flac" + "Album.cue",
    "CD1.flac" + "CD1.cue"), or None.

    A share that offers one big file per disc plus its cue carries no
    per-track file names to match, so it is accepted on shape: exactly ONE
    audio file per expected disc — nothing else in the folder — and a cue
    sheet for each of them. Whether that file really is the release is
    settled later by the graded .log and the decode check (see _verify_album).
    ponytail: a search result carries no cue CONTENT, so the cue's TRACK
    count cannot be read here; fetch the cue and count its TRACK rows if a
    wrong-cue image rip ever slips through."""
    discs = sorted({t["disc"] for t in expected})
    if not cues or len(audio) != len(discs):
        return None
    out = []
    for d in discs:
        per_disc = ([f for f in audio if _named_disc(f["file"]) == d]
                    if len(discs) > 1 else list(audio))
        if len(per_disc) != 1:
            return None
        stem = os.path.splitext(os.path.basename(per_disc[0]["file"]))[0]
        if (_TRACKNO_RE.match(stem) or _LEAD_NUM_RE.match(stem)
                or _MID_NUM_RE.search(stem)):
            return None          # a stray track file, not a disc image
        out.append(per_disc[0])
    return out


def find_candidates(results, release, cfg):
    """Score search results against the release track list.

    Returns candidates sorted best-first: [{username, dir, files, audio,
    logs, cues, complete, score, lossless, slot, queue}]. A candidate is
    complete when every expected track (across all discs) has a matching
    file (matched by track number, falling back to normalized title +
    duration) and — for CD releases — every disc carries a .log and .cue.

    Files are grouped per directory, folded per disc (…/CD1/, …/Disc 1/,
    …/CD1 [FLAC]/), and then per covering directory: a group that does not
    carry the release on its own is re-evaluated one directory at a time
    upwards (per-track folders, a Log+Cue/Music split), so the candidate is
    the DEEPEST directory whose file set covers the expected tracklist.
    `files` is what stage 2 downloads: the matched tracks plus the selected
    logs and cues, so a covering root that holds more audio (a flat
    Artist/ folder) never pulls the rest of it in."""
    min_ratio = float(cfg.get("soulseek_auto_complete_ratio", 1.0) or 1.0)
    expected = _expected_tracks(release)
    discs_expected = _disc_numbers(release)
    is_cd = "CD" in (release.get("medium_formats") or [])
    if not expected:
        # matched == len(expected) and matched >= min_ratio * len(expected)
        # are both vacuously true for an empty track list, so any single-file
        # folder "completed" a release that has no tracks at all.
        raise ValueError("the release has no track list — a candidate folder "
                         "cannot be matched against it")

    def ext_of(f):
        return os.path.splitext(f.get("file") or "")[1].lower()

    groups = {}
    for f in results:
        if ext_of(f) not in (_AUDIO_EXTS | {".log", ".cue"}):
            continue
        groups.setdefault((f["username"], _album_root(f["file"])), []).append(f)

    by_user = {}   # username -> {root: files}
    for (username, root), files in groups.items():
        by_user.setdefault(username, {})[root] = files

    def files_under(username, prefix):
        out = []
        for root, flist in by_user[username].items():
            if _same_folder(root, prefix) or _under(root, prefix):
                out.extend(flist)
        return out

    def evaluate(username, root, files):
        audio = [f for f in files if ext_of(f) in _AUDIO_EXTS]
        logs = _select_logs([f for f in files if f["file"].lower().endswith(".log")],
                            discs_expected, root)
        cues = [f for f in files if f["file"].lower().endswith(".cue")]
        if not audio:
            return None
        log_discs = {_disc_of(f["file"]) for f in logs}
        cue_discs = {_disc_of(f["file"]) for f in cues}
        missing_logs = [d for d in discs_expected if d not in log_discs] if is_cd else []
        missing_cues = [d for d in discs_expected if d not in cue_discs] if is_cd else []

        # match expected tracks to files. A file carrying BOTH the track
        # number and the expected title wins over one that only carries the
        # number: a flat Artist/ folder is full of "01 - …" files belonging
        # to the artist's other albums.
        image = _image_rip_audio(audio, cues, expected) if not missing_cues else None
        used = set()
        matched_files = []
        for t in expected:
            by_num = [f for f in audio
                      if id(f) not in used
                      and _parse_trackno(f["file"]) == (t["disc"], t["pos"])]
            hit = next((f for f in by_num if _title_in(f, t)), None)
            if hit is None and by_num:
                hit = by_num[0]
            if hit is None:
                for f in audio:
                    if id(f) in used or not _title_in(f, t):
                        continue
                    dur = float(f.get("duration") or 0)
                    if not t["length"] or not dur or abs(dur - t["length"]) <= 15:
                        hit = f
                        break
            if hit is not None:
                used.add(id(hit))
                matched_files.append(hit)
        matched = len(matched_files)
        if image and matched < len(expected):
            matched_files = image
            matched = len(expected)
        complete = (matched == len(expected)) and not missing_logs and not missing_cues
        acceptable = matched >= min_ratio * len(expected) and not missing_logs and not missing_cues
        if not acceptable:
            return None
        lossless = any(ext_of(f) in _LOSSLESS_EXTS for f in audio)
        slot = bool(files[0].get("slot"))
        queue = min(int(f.get("queue") or 0) for f in files)
        # the peer's advertised upload rate (slskd reports one per response):
        # the SLOWEST figure of the folder, so the wait is never cut short
        speeds = [float(f.get("speed") or 0) for f in files]
        speeds = [s for s in speeds if s > 0]
        # Stage 2 takes the release's own files only: when the covering root
        # holds audio beyond the tracklist (a flat Artist/ folder, the merged
        # parent of a split layout) the rest of the folder stays unbought.
        plans = matched_files if len(matched_files) < len(audio) else audio
        if plans is audio:
            plan_cues = cues
        else:
            plan_dirs = {os.path.dirname(f["file"]) for f in plans}
            plan_cues = [f for f in cues if os.path.dirname(f["file"]) in plan_dirs]
        downloads, seen = [], set()
        for f in plans + logs + plan_cues:
            if f["file"] not in seen:
                seen.add(f["file"])
                downloads.append(f)
        total = sum(int(f.get("size") or 0) for f in downloads)
        score = (5 * matched + 2 * len(logs) + len(cues)
                 + 8 * lossless + 4 * slot - queue / 100.0)
        return {
            "username": username, "dir": root, "files": downloads, "audio": plans,
            "logs": logs, "cues": cues, "matched": matched, "expected": len(expected),
            "complete": complete, "lossless": lossless, "slot": slot,
            "queue": queue, "speed": min(speeds) if speeds else 0,
            "total_size": total, "score": round(score, 1),
        }

    candidates = []
    covered = {}
    for (username, root) in sorted(groups):
        if any(_same_folder(root, r) or _under(root, r)
               for r in covered.get(username, ())):
            continue
        # deepest covering directory first: the group's own root, then up
        for cand_root in _ancestors(root):
            c = evaluate(username, cand_root, files_under(username, cand_root))
            if c:
                candidates.append(c)
                covered.setdefault(username, set()).add(cand_root)
                break

    candidates.sort(key=_rank)
    return candidates


# --------------------------------------------------------------------------- #
# slskd helpers (search, wait, locate local files)
# --------------------------------------------------------------------------- #
_SEARCH_POLL_S = 0.75      # one loop polls EVERY outstanding search this often
_TRANSFER_POLL_S = 1.0     # download poll cadence


def _search_queries(slsk, queries, wait_s, usable=None):
    """Run every query template AT ONCE and poll them in one loop.

    All templates are POSTed up front, so the wall time is one window
    (`wait_s` + the grace tail) instead of one window per template — up to
    three minutes before the first byte became one minute. `usable(merged)`
    is asked on every tick and ends the wait the moment the merged responses
    already hold what we came for, so the remaining templates are never
    waited out. The per-request `timeout` is passed in MILLISECONDS because
    slskd counts it that way (see soulseek.search); responses are only
    retrievable after a search ends, hence the grace tail past `wait_s`.

    Returns (results, errors, skipped): results = [(query, response DTO)] for
    the searches that reached a terminal state, errors = one line per query
    that failed or never terminated, skipped = how many queries were still
    running when a usable candidate ended the wait."""
    started = time.time()
    deadline = started + wait_s + _SEARCH_GRACE_S
    display = " · ".join(queries)
    watch, errors = [], []
    for q in queries:
        try:
            watch.append([slsk.search(q, timeout_ms=int(wait_s * 1000)), q, None])
        except Exception as e:
            errors.append(f"Soulseek search could not be started for “{q}”: {e}")
    early = False
    while watch and time.time() < deadline:
        for e in watch:              # probe first: no dead sleep before it
            try:
                e[2] = slsk.search_results(e[0])
            except Exception:
                pass
        pending = [e for e in watch if not slsk.is_search_done(e[2] or {})]
        _job_search_progress(display, time.time() - started, wait_s, {
            "state": "InProgress" if pending else "Completed",
            "responseCount": sum(int((e[2] or {}).get("responseCount") or 0) for e in watch),
            "fileCount": sum(int((e[2] or {}).get("fileCount") or 0) for e in watch),
        })
        merged = [f for e in watch for f in ((e[2] or {}).get("responses") or [])]
        if usable is not None and merged and usable(merged):
            early = True
            break
        if not pending:
            break
        time.sleep(_SEARCH_POLL_S)

    results, skipped = [], 0
    for _sid, q, res in watch:
        if not slsk.is_search_done(res or {}):
            if early:
                skipped += 1     # in hand already — never waited out
            else:
                errors.append(f"Soulseek search did not finish within "
                              f"{int(wait_s + _SEARCH_GRACE_S)}s for “{q}”")
            continue
        err = slsk.search_error(res)
        if err:
            errors.append(f"Soulseek search failed ({err}) for “{q}”")
        else:
            results.append((q, res))
    return results, errors, skipped


def _remote_rel(remote_path):
    """slskd's local relative path for a remote file (see
    _local_download_candidates for where that lands in the download dir).

    Two things the plain "strip the drive letter" version got wrong: the
    leading separator survived ('C:\\Music\\x' -> '/Music/x'), so joining the
    result onto the download dir produced an absolute path OUTSIDE it; and a
    '..' segment escaped the same way. Everything joining this path
    (<downloads>/<user>/<rel>) is later moved or rmtree'd, so it must stay a
    plain relative path.
    """
    p = remote_path.replace("\\", "/")
    if re.match(r"^[A-Za-z]:", p):
        p = p[2:]
    return "/".join(x for x in p.lstrip("/").split("/") if x not in ("", ".", ".."))


def _inside(path, folder):
    """True when `path` is strictly inside `folder` (boundary-aware).

    Same test server/main.py applies to its music-folder guard: a
    startswith() prefix check accepts both '..' paths and siblings that
    merely share a name prefix — and this one gates an rmtree()."""
    try:
        p = os.path.abspath(os.path.normpath(path))
        f = os.path.abspath(os.path.normpath(folder))
    except (OSError, ValueError, TypeError):
        return False
    if p == f:
        return False
    if os.path.normcase(os.path.splitdrive(p)[0]) != os.path.normcase(os.path.splitdrive(f)[0]):
        return False
    try:
        return os.path.normcase(os.path.commonpath([p, f])) == os.path.normcase(f)
    except (OSError, ValueError, TypeError):
        return False


def _leaf_of(remote):
    """The remote file's own album folder name ("" at the top of the share) —
    the directory slskd recreates under the download dir."""
    parts = _remote_rel(remote).split("/")
    return parts[-2] if len(parts) >= 2 else ""


def _index_download_tree(ddir, username, leaves):
    """basename -> [local path] under the candidate's OWN album trees.

    slskd keeps the remote folder structure under its download dir, but the
    layout depends on version/settings: 0.26 writes `<ddir>/<leaf>/…` with no
    username segment, older builds nest `<ddir>/<username>/<leaf>/…`. The
    caller builds this ONCE per poll tick instead of walking the tree once per
    pending file; a same-named file from another album must never satisfy a
    pending download, so the scan never walks the whole download dir.

    Dot-directories are pruned: partials now stage in a SIBLING `incomplete/`
    (see soulseek._incomplete_dir), but a pre-migration install still has them
    under `<ddir>/.incomplete`, and a half-written file must never be indexed
    as a completed one."""
    index = {}
    incomplete = (os.sep + ".incomplete").lower()
    for leaf in leaves:
        for scan_root in (os.path.join(ddir, leaf), os.path.join(ddir, username, leaf)):
            if not os.path.isdir(scan_root):
                continue
            for root, dirs, files in os.walk(scan_root):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                if incomplete in root.lower():
                    continue  # still downloading — never a completed file
                for f in files:
                    index.setdefault(f, []).append(
                        os.path.normpath(os.path.join(root, f)))
    return index


def _local_download_candidates(ddir, username, remote, size, index=None):
    """All plausible local locations for a remote file.

    Both exact shapes (`<ddir>/<rel>` and `<ddir>/<username>/<rel>`) are
    checked first, then the candidate's own album tree from `index` (see
    _index_download_tree) catches the file when slskd sanitised the share name
    or the file sits a level deeper; without an index the tree of THIS file's
    leaf is walked, so the function stays usable on its own. A missing file
    (slskd renamed or removed it between the isfile test and the size read) or
    an unreadable one counts as not-yet-present rather than raising out of the
    job."""
    rel = _remote_rel(remote)
    out = []
    for exact in (os.path.join(ddir, rel), os.path.join(ddir, username, rel)):
        exact = os.path.normpath(exact)  # rel uses "/" — keep one spelling
        if os.path.isfile(exact) and exact not in out:
            out.append(exact)
    leaf = _leaf_of(remote)
    if not leaf:
        return out
    base = os.path.basename(remote.replace("\\", "/"))
    if index is None:
        index = _index_download_tree(ddir, username, [leaf])
    for p in index.get(base, ()):
        if p in out:
            continue
        # a size mismatch means a different (or truncated) file — only when
        # the peer actually reported a size
        try:
            if size and os.path.getsize(p) != size:
                continue
        except OSError:
            continue     # gone/renamed under us: not present yet
        out.append(p)
    return out


def _local_album_root(ddir, files):
    """The album root on disk for a candidate: the highest directory below
    `ddir` that still holds EVERY downloaded file of it.

    slskd's default destination is `${SOURCE_DIRECTORY}` (the remote folder's
    leaf), so the files live at `<ddir>/<leaf>/…`, or `<ddir>/<user>/<leaf>/…`
    on older builds. Taking the first file's directory instead half-imported
    a multi-disc release: it verified one disc folder and stranded the rest.
    Returns None when the files do not share one directory below `ddir`
    (slskd split them across top-level folders, or they landed loose in it)
    — the caller rejects the candidate with a clear reason rather than
    verifying a directory it cannot name."""
    paths = [os.path.abspath(os.path.normpath(p)) for p in files or []]
    if not paths:
        return None
    try:
        common = os.path.commonpath(paths)
    except (ValueError, OSError):
        return None       # different drives
    if os.path.isfile(common):
        # a single downloaded file (an image rip): its own folder is the root
        common = os.path.dirname(common)
    return common if _inside(common, ddir) else None


_FAILED_TRANSFER_STATES = ("Cancelled", "TimedOut", "Errored", "Rejected",
                           "FileNotFound", "Aborted", "Failed")
# An InProgress transfer with no byte movement for this long is dead (a peer
# that stopped responding); waiting out the full per-candidate timeout for it
# is what made a stuck candidate look like a hung import.
_STALL_AFTER_S = 180.0
# The .log gate is a few kB: it must not be waited for as if it were the album.
_LOG_TIMEOUT_S = 180.0
# The old fixed guess, used only when the search never reported a peer speed.
_MIN_RATE = 200 * 1024


def _est_timeout(cand):
    """Per-candidate download timeout from the candidate's OWN numbers.

    The fixed 200 kB/s guess gave a 2 h ceiling to an album a 5 MB/s peer
    fetches in two minutes, and cut off a slow-but-honest peer mid-file. The
    search response carries the peer's upload speed and the number of files
    queued ahead of us, so the wait is the album at that speed plus what the
    queue costs at the same rate (each file ahead the size of an average file
    of the folder). Floor 900 s, cap 7200 s, as before."""
    size = float(cand.get("total_size") or 0)
    rate = float(cand.get("speed") or 0) or _MIN_RATE
    files = len(cand.get("files") or []) or 1
    queued = int(cand.get("queue") or 0) * (size / files)
    return int(min(7200, max(900, (size + queued) / rate)))


def _progress_snapshot(username, wanted, got, transfers, phase):
    """The live download block the Soulseek page renders.

    Aggregate counters plus one row per wanted file, straight from slskd's own
    transfer records (`_user_transfers`, consumed per candidate user+files —
    never unrelated downloads). A file slskd has not reported a size for
    contributes only what is known; nothing is invented, and `percent` is
    clamped so a peer's own bogus counter cannot print 240 %."""
    by_name = {t["filename"]: t for t in transfers}
    files, nbytes, nsize, speed, eta = [], 0, 0, 0.0, None
    for w in wanted:
        name = w["filename"]
        t = by_name.get(name) or {}
        done = name in got
        size = int(t.get("size") or w.get("size") or 0)
        b = size if done else int(t.get("bytes") or 0)
        pct = t.get("percent")
        if done:
            pct = 100
        elif pct is None:
            pct = int(b * 100 / size) if size else 0
        files.append({
            "name": os.path.basename(name.replace("\\", "/")),
            "bytes": b, "size": size, "percent": max(0, min(100, int(pct))),
            "speed": float(t.get("speed") or 0),
            "state": "Done" if done else str(t.get("state") or ""),
            "done": done,
        })
        nbytes += b
        nsize += size
        speed += float(t.get("speed") or 0)
        if not done and t.get("remaining") is not None:
            eta = max(eta or 0, int(t["remaining"]))
    if eta is None and speed > 0 and nsize > nbytes:
        eta = int((nsize - nbytes) / speed)
    return {
        "phase": phase,
        "username": username,
        "dir": os.path.dirname(wanted[0]["filename"].replace("\\", "/")) if wanted else "",
        "files_done": sum(1 for f in files if f["done"]),
        "files_total": len(files),
        "bytes": nbytes,
        "size": nsize,
        "percent": max(0, min(100, int(nbytes * 100 / nsize))) if nsize else 0,
        "speed": speed,
        "eta_s": eta,
        "files": files,
    }


def _cancel_rest(slsk, username, wanted, keep):
    """Drop the transfers we no longer want after a failed log grade: slskd
    otherwise keeps fetching files whose partials the caller deletes next."""
    names = {w["filename"] for w in wanted} - set(keep)
    from server.soulseek import _user_transfers
    try:
        ids = [t["id"] for t in _user_transfers(slsk, username, names) if t.get("id")]
        if ids:
            slsk.cancel_downloads(username, ids)
    except Exception:
        pass


def _wait_for_files(slsk, ddir, username, wanted, timeout_s, cancel_check=None,
                    phase="download", display_wanted=None):
    """Poll until every wanted remote path is present locally AND slskd says
    the transfer finished successfully.

    A file on disk is not proof on its own: the peer may never have reported a
    size for it (0/unknown must not satisfy the wait), and a same-named file
    left behind by an earlier attempt would otherwise count as arrived. The
    path must exist with the expected size AND slskd must report a successful
    terminal state for that transfer (`finished_transfer`) — which also
    removes the window where slskd still holds the file open.

    slskd's transfer states are read alongside the filesystem, so a peer that
    rejects the slot (or an errored transfer) costs a poll interval instead of
    the whole per-candidate timeout. A transfer that has moved no bytes for
    `_STALL_AFTER_S` while every live transfer is InProgress (a peer that
    stopped sending) OR while NOTHING is InProgress (a dropped request, a
    queue that never moves, no transfer record at all) is dead too — all of
    them otherwise hold the candidate for the full timeout. A peer with a mix
    of moving and waiting files, or a transfer that is still moving, is never
    abandoned early.

    Each tick publishes the download progress payload (phase 'logging' while
    the .log gate is being fetched, 'download' for the album) and clears it
    again on the way out — search, verify and import have no download in
    flight. wanted = [{filename, size}]. Returns {remote: local_path} for the
    files that arrived complete; missing entries are absent from the dict.

    `display_wanted` overrides what the progress block SHOWS without changing
    what is waited for. The .log gate uses it to display the whole album: the
    album transfers are queued in the same call as the logs, so they are
    already coming down while the log is graded, and showing only the log made
    the UI look like it was stuck on one file."""
    deadline = time.time() + timeout_s
    from server.soulseek import _user_transfers
    shown = list(display_wanted) if display_wanted else list(wanted)
    pending = {w["filename"]: w for w in wanted}
    # Transfers are fetched for the DISPLAY set, not just the waited-for set:
    # during the .log gate the album is already downloading and its live
    # bytes/speed/ETA are exactly what the progress block is showing, so
    # fetching only the log published a bar frozen at 0 % with no metrics.
    # `pending` still drives the wait and the stall detection below, so the
    # gate is still only satisfied by the logs.
    fetch_names = set(pending) | {w["filename"] for w in shown}
    leaves = {_leaf_of(w["filename"]) for w in wanted} - {""}
    got = {}
    live = []
    last_bytes = -1
    last_progress = time.time()
    try:
        while time.time() < deadline and pending:
            if cancel_check and cancel_check():
                break
            # one transfer-tree fetch per tick, and one tree walk per tick too
            # (only when slskd actually says a file is done)
            live = _user_transfers(slsk, username, fetch_names)
            done_states = {t["filename"] for t in live
                           if slsk.finished_transfer(t["state"])
                           and not any(x in t["state"] for x in _FAILED_TRANSFER_STATES)}
            ready = [r for r in pending if r in done_states]
            if ready:
                index = _index_download_tree(ddir, username, leaves)
                for remote in ready:
                    cands = _local_download_candidates(
                        ddir, username, remote, int(pending[remote].get("size") or 0),
                        index=index)
                    if cands:
                        got[remote] = cands[0]
                        del pending[remote]
            _job_progress(_progress_snapshot(username, shown, got, live, phase))
            if not pending:
                break
            moving = [t for t in live if t["filename"] in pending]
            if any(any(x in t["state"] for x in _FAILED_TRANSFER_STATES) for t in moving):
                break
            moved = sum(t["bytes"] for t in moving)
            inprog = [t for t in moving if "InProgress" in t["state"]]
            if moved > last_bytes:
                last_bytes, last_progress = moved, time.time()
            elif (time.time() - last_progress > _STALL_AFTER_S
                  and (not inprog or len(inprog) == len(moving))):
                # every live transfer InProgress and none moving = a peer that
                # stopped sending; NONE InProgress = a dropped/queued request
                # that never starts. A mix (one file moving, another still
                # queued) is a working peer and is left alone.
                _log(f"  transfer stalled for {int(_STALL_AFTER_S)}s with no progress — "
                     f"moving on")
                break
            time.sleep(_TRANSFER_POLL_S)
    finally:
        _job_progress(None)
    if pending:
        # giving up on this candidate: the caller deletes its partial files
        # next, and slskd re-downloads exactly those from its queue
        try:
            slsk.cancel_downloads(username,
                                  [t["id"] for t in _user_transfers(slsk, username, pending)])
        except Exception:
            pass
    return got


# --------------------------------------------------------------------------- #
# Verification helpers
# --------------------------------------------------------------------------- #
def _log_passes(score, state, min_score):
    """Whether a log on its own is good enough to accept the album: scorable,
    at or above the bar, and not a failed checksum (state 'invalid' means the
    log's own SHA256 does not verify — real evidence, never junk)."""
    return score is not None and score >= min_score and state != "invalid"


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


_CUE_TRACK_RE = re.compile(r"^\s*TRACK\s+\d+", re.IGNORECASE | re.MULTILINE)


def _has_image_rip(album_dir, audio):
    """True when the download is a disc-image rip (Album.flac + Album.cue):
    a cue sheet listing more TRACKs than there are audio files. The log's
    per-track CRCs describe tracks spliced out of ONE image, so they cannot
    be matched against a file — such a rip is verified by decoding it."""
    from mlo.discs import read_log_text
    for root, _dirs, files in os.walk(album_dir):
        for f in files:
            if not f.lower().endswith(".cue"):
                continue
            try:
                text = read_log_text(os.path.join(root, f))
            except Exception:
                continue
            if len(_CUE_TRACK_RE.findall(text or "")) > len(audio):
                return True
    return False


def _verify_album(album_dir, cfg, is_cd):
    """Post-download verification. Returns (ok, problems[]).

    CD: every track's decoded-PCM CRC must match its .log checksum
    (mlo.discs.verify_album_checksums) — except for a disc-image rip, whose
    cue splits one file into the log's tracks, so it is decode-checked like
    digital media. Digital media: every file must decode cleanly (flac -t /
    ffmpeg decode-to-null). The full AudioAuditor pass runs later as part of
    the normal pipeline — only .log matching (CD) and decodability matter for
    accepting or rejecting a download.
    """
    problems = []
    from mlo.tools import detect_all_tools

    audio = []
    for root, _dirs, files in os.walk(album_dir):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in _AUDIO_EXTS:
                audio.append(os.path.join(root, f))
    if not audio:
        return False, ["no audio files found in the download"]

    if is_cd and not _has_image_rip(album_dir, audio):
        # verify_album_checksums keys off MEDIA=CD — the tags were just
        # written for exactly this reason.
        ffmpeg = (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")
        if not ffmpeg:
            return False, ["ffmpeg not available for CRC verification"]
        from mlo.discs import verify_album_checksums
        verdicts, unverified = verify_album_checksums(ffmpeg, album_dir, audio, cfg)
        if not verdicts and not unverified:
            # verify_album_checksums keys off the MEDIA tag: with none written
            # it reports nothing at all, which used to read as "verified".
            problems.append("no track's CRC was checked against the rip log "
                            "(the MEDIA tag is missing?)")
        for p, v in verdicts.items():
            if v != "REAL":
                problems.append(f"{os.path.basename(p)}: CRC mismatch vs .log (FAKE)")
        for p, reason in unverified.items():
            problems.append(f"{os.path.basename(p)}: not verifiable ({reason})")
        return not problems, problems
    if is_cd:
        _log("  disc-image rip (one file per disc plus its cue) — verifying by "
             "decode; per-track .log CRCs do not apply")

    # digital media / other: decode-check each file
    tools = detect_all_tools()
    ffmpeg = (tools.get("ffmpeg") or {}).get("ffmpeg_exe")
    flac = (tools.get("flac") or {}).get("flac_exe")
    if not (flac or ffmpeg):
        # every check_one() would return None and the album would be reported
        # as "Verification passed" without a single file being decoded
        return False, ["no decoder available (flac/ffmpeg) — the download "
                       "cannot be verified"]
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
def _stamp_mb_tags(album_dir, release):
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
def job_active():
    """True while a job holds the pipeline (running or parked on the lossy
    prompt) — no second job may start, and the wishes worker must stand down."""
    with _lock:
        return _job["state"] in ("running", "confirm")


def start_job(release_mbid=None, release=None, queries=None, username=None,
              target_dir=None, confirm_lossy=False):
    """Kick off an auto-import job in a daemon thread; returns the job state.

    release — a full release dict (from integrations.release_lookup); when
    only release_mbid is given it is fetched here. queries overrides the
    configured search templates for this run. username+target_dir downloads
    that exact user/folder without searching (manual entry).
    confirm_lossy — the interactive path (server.main's HTTP start route).
    When no lossless folder matched but lossy ones did, park the job and wait
    for confirm() instead of downloading, and likewise park when the search
    found no usable folder at all and the release could be wished instead.
    False (the background wishes worker) means lossy copies are never taken
    silently — and a wish is never asked to become a wish.
    """
    with _lock:
        if _job["state"] in ("running", "confirm"):
            return {"ok": False, "error": "a job is already running", "job": job_state()}
        _job.update({"state": "running", "stage": "Starting…", "log": [],
                     "attempts": [], "result": None, "confirm": None, "search": None,
                     "progress": None, "cancel": False,
                     "release": {"id": release_mbid} if release_mbid else None})
    _confirm_event.clear()
    _confirm_answer["accept"] = False
    threading.Thread(target=_run, name="mlo-soulseek-auto",
                     kwargs=dict(release_mbid=release_mbid, release=release,
                                 queries=queries, username=username,
                                 target_dir=target_dir,
                                 confirm_lossy=confirm_lossy),
                     daemon=True).start()
    return {"ok": True, "job": job_state()}


def _cancelled():
    with _lock:
        return _job["cancel"]


def _release_from_folder(username, target_dir, slsk):
    """A minimal release dict describing one browsed folder.

    Lets "download this folder I found by browsing" run the whole
    verify → import → organize pipeline without a MusicBrainz release: every
    audio file in the folder is one expected track, so completeness still
    means "every file arrived", and the medium is Digital Media (no .log/.cue
    requirement — a browsed folder is not a verified CD rip).
    """
    files = []
    try:
        for d in slsk.browse(username) or []:
            dpath = str(d.get("directory") or "")
            want = str(target_dir).replace("\\", "/").rstrip("/")
            # boundary-aware: …/NotAlbum must not answer for …/Album
            if _same_folder(dpath, want) or _under(dpath, want):
                files = list(d.get("files") or [])
                break
    except Exception:
        files = []
    media = []
    for i, f in enumerate(files, start=1):
        name = str(f.get("filename") or "")
        if os.path.splitext(name)[1].lower() not in _AUDIO_EXTS:
            continue
        media.append({
            "disc": 1, "position": len(media) + 1,
            "title": os.path.splitext(os.path.basename(name))[0],
            "length": (f.get("length") or 0) and int(f["length"]) * 1000 or None,
        })
    return {
        "id": None,
        "title": str(target_dir).replace("\\", "/").rstrip("/").split("/")[-1],
        "date": "", "country": "", "catalog_number": "", "label": "",
        "release_group_id": None, "artists": [],
        "medium_formats": ["Digital Media"],
        "media": media,
        "barcode": "",
    }


def _reject(username, folder, reason):
    with _lock:
        _job["attempts"].append({"username": username, "dir": folder, "reason": str(reason)[:200]})


def _run(release_mbid=None, release=None, queries=None, username=None,
         target_dir=None, confirm_lossy=False):
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
        if release is None and username and target_dir:
            # Browsed-folder grab: there is no MusicBrainz release to match
            # against, so the folder's own audio files ARE the track list. The
            # identity is filled in from their tags by the tagging chain.
            release = _release_from_folder(username, target_dir, slsk)
            _log(f"No MusicBrainz release — treating the folder as the track "
                 f"list ({len(release.get('media') or [])} file(s))")
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
        _log(f"Target: {(release.get('artists') or [{}])[0].get('name', '?')} — "
             f"{release.get('title')} ({release.get('date') or 'n/a'})"
             f"{', ' + release.get('catalog_number') if release.get('catalog_number') else ''}"
             f" · {len(release.get('media') or [])} track(s) · {'CD' if is_cd else 'Digital Media'}")

        # ---- candidates ------------------------------------------------------
        if username and target_dir:
            _log(f"Manual entry: {username} · {target_dir}")
            entries = slsk.browse(username)
            want = str(target_dir).replace("\\", "/").rstrip("/")
            files = []
            for d in entries or []:
                dpath = str(d.get("directory") or "")
                # boundary-aware (…/NotAlbum must not answer for …/Album) and
                # folder-recursive (the browsed folder plus its subfolders)
                if _same_folder(dpath, want) or _under(dpath, want):
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
            logs = _select_logs([f for f in files if f["file"].lower().endswith(".log")],
                                _disc_numbers(release), root)
            # same rule as the searched path: one log per disc, and never a
            # second, unselected log for a disc already covered
            picked = {l["file"] for l in logs}
            plan = [f for f in files
                    if not f["file"].lower().endswith(".log") or f["file"] in picked]
            candidates = [{
                "username": username, "dir": root, "files": plan, "audio": audio,
                "logs": logs,
                "cues": [f for f in files if f["file"].lower().endswith(".cue")],
                "matched": 0, "expected": len(release.get("media") or []),
                "complete": False, "lossless": True, "slot": False, "queue": 0,
                "total_size": sum(f["size"] for f in plan), "score": 0,
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
            # No forced minimum. A usable candidate already ends the wait the
            # moment it appears (see `usable` below), and an album with a live
            # source must start downloading immediately rather than sitting out
            # a window — the configured value is the CEILING a search may run,
            # not a floor it must serve.
            search_wait = int(cfg.get("soulseek_auto_search_wait", 15) or 15)
            # Every template goes out at once and the merged responses are
            # scored on each tick: the first complete+lossless folder ends the
            # wait for ALL of them, so a weak template never costs another full
            # window. `usable` is the same test the loop below applies.
            def _usable(merged):
                return [c for c in find_candidates(merged, release, cfg)
                        if c["complete"] and c["lossless"]]

            _log(f"Searching Soulseek with {len(queries_built)} query template(s) in "
                 f"parallel: “{'” · “'.join(queries_built)}” … (up to "
                 f"{int(search_wait + _SEARCH_GRACE_S)}s)")
            results, search_failed, skipped = _search_queries(
                slsk, queries_built, search_wait, usable=_usable)
            _job_search_done()
            for line in search_failed:
                # a query slskd errored on is not "no results": it is reported
                # whether or not the other templates found something.
                _log(f"  ✕ {line}")
            responses = [f for _q, res in results for f in (res.get("responses") or [])]
            candidates = find_candidates(responses, release, cfg)
            _log(f"  {len(candidates)} candidate folder(s) from {len(responses)} "
                 f"result file(s) across {len(results)} search(es)")
            candidates.sort(key=_rank)
            best = [c for c in candidates if c["complete"] and c["lossless"]]
            if best and skipped:
                _log(f"  Usable candidate found ({len(best)} complete lossless) — "
                     f"a good copy is in hand, so the search stopped here and the "
                     f"remaining {skipped} query template(s) were not waited out")
            if not candidates and search_failed:
                raise RuntimeError("; ".join(search_failed[:3]))
            if not candidates:
                # slskd answered, but no folder held the whole album. That used
                # to be a hard stop ("No candidate folder contained every
                # track"), which threw away a search the user just waited out
                # for a release that is merely rare right now. On the
                # interactive path the job instead parks on the SAME prompt the
                # lossy branch uses and offers to add the release to the wishes
                # list: the background worker then keeps searching for it with
                # the very queries this job used, so nothing is lost by parking
                # the job — the user only decides whether it is worth watching
                # for. A release with no MusicBrainz id has nothing to wish for
                # (the browsed-folder grab), and the background wishes path
                # (confirm_lossy=False) fails quietly as before.
                #
                # How long a search runs is decided in ONE place —
                # soulseek_auto_search_wait (the requested window) plus
                # _SEARCH_GRACE_S (the tail slskd needs to hand back responses
                # once a search ended) — and this prompt fires exactly when that
                # window has just ended with nothing usable. `waited` reports
                # that same cap to the UI instead of a second, competing timer.
                if (confirm_lossy and release.get("id")
                        and cfg.get("soulseek_auto_wish_prompt", True)):
                    from server import wishes
                    _log("No usable result — asking whether to add this release "
                         "to the wishes list.")
                    with _lock:
                        _job["state"] = "confirm"
                        _job["stage"] = "No usable results — add to wishes?"
                        _job["confirm"] = {
                            "reason": "no_results",
                            "waited": int(search_wait + _SEARCH_GRACE_S),
                            "queries": list(queries_built),
                            "formats": [],
                            "candidates": [],
                        }
                    _confirm_event.wait()  # released by confirm() or cancel()
                    _confirm_event.clear()
                    with _lock:
                        _job["confirm"] = None
                        _job["state"] = "running"
                        accepted = _confirm_answer["accept"]
                    if _cancelled():
                        return _finish("cancelled")
                    if accepted:
                        wish = wishes.add_wish(
                            release.get("id"), title=release.get("title") or "",
                            artist=((release.get("artists") or [{}])[0]
                                    .get("name", "")),
                            year=str(release.get("date") or "")[:4],
                            queries=list(queries_built))
                        _log(f"Added to wishes (#{wish['id']}) — the worker keeps "
                             f"searching for this release in the background with "
                             f"the same queries, so nothing is lost by parking "
                             f"this job.")
                        # Nothing landed in the library: report the wish, never a
                        # download. The import path's result keys are kept (as
                        # None/0) so a caller reading result["album_path"] sees
                        # one shape for every finished job.
                        return _finish("done", {
                            "wished": True, "wish_id": wish["id"],
                            "album_path": None, "staging_path": None,
                            "imported": 0, "organized": 0,
                            "organize_error": None})
                raise RuntimeError("No candidate folder contained every track "
                                   "(and cue/log per disc for CD). Try the "
                                   "manual entry or different search terms.")
            # ---- lossless preference ----------------------------------------
            # `_rank` already puts lossless folders first, so a lossless match
            # wins whenever one exists. When every candidate is lossy the
            # download changes what lands in the library — that decision is
            # the user's (interactive jobs ask; background wishes decline).
            if not any(c["lossless"] for c in candidates):
                formats = sorted({os.path.splitext(f["file"])[1].lstrip(".").upper()
                                  for c in candidates for f in c["audio"]})
                shown = ", ".join(f for f in formats if f)[:60] or "lossy"
                if not confirm_lossy:
                    raise RuntimeError(
                        f"Only lossy copies found ({shown}) — a lossless copy is "
                        f"preferred, so nothing was downloaded.")
                _log(f"Only lossy copies found ({shown}) — waiting for your go-ahead.")
                with _lock:
                    _job["state"] = "confirm"
                    _job["stage"] = "Waiting: only lossy copies found"
                    _job["confirm"] = {
                        "reason": "lossy_only",
                        "formats": formats,
                        "candidates": [{
                            "username": c["username"],
                            "dir": c["dir"],
                            "format": os.path.splitext(c["audio"][0]["file"])[1].lstrip(".").upper()
                                      if c["audio"] else "",
                            "matched": c["matched"],
                            "expected": c["expected"],
                            "size": c["total_size"],
                            "score": c["score"],
                        } for c in candidates[:5]],
                    }
                _confirm_event.wait()  # released by confirm() or cancel()
                _confirm_event.clear()
                with _lock:
                    _job["confirm"] = None
                    _job["state"] = "running"
                    accepted = _confirm_answer["accept"]
                if _cancelled():
                    return _finish("cancelled")
                if not accepted:
                    raise RuntimeError("Lossy-only download declined — nothing "
                                       "was downloaded.")
                _log("Lossy download approved — continuing with the lossy copy.")

        # ---- try candidates in order -----------------------------------------
        # Rejected candidates are the attempts that get capped: the first one
        # that verifies is imported immediately (the `return` below).
        max_attempts = int(cfg.get("soulseek_auto_max_attempts", 3) or 3)
        capped = False
        for cand in candidates:
            if _cancelled():
                return _finish("cancelled")
            if len(_job["attempts"]) >= max_attempts:
                _log(f"Attempt cap reached ({len(_job['attempts'])}/{max_attempts} "
                     f"rejected candidate(s)) — stopping the search for this album.")
                capped = True
                break
            uname, folder = cand["username"], cand["dir"]
            _log(f"Candidate: {uname} · …{folder[-60:]} ({cand['matched']}/{cand['expected']} tracks matched)")

            # --- ONE enqueue pass: logs and album together --------------------
            # Queueing both in one call means a peer's queue is joined once,
            # and the .log gate still runs first: a slow log is graded the
            # moment it lands while the album keeps downloading behind it.
            wanted, seen = [], set()
            for f in cand["files"]:
                if f["file"] in seen:
                    continue
                seen.add(f["file"])
                wanted.append({"filename": f["file"], "size": f["size"]})
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

            # The job is DOWNLOADING from this point, so say so immediately:
            # the search bar is cleared and a transfer block is published
            # before the first poll, so the UI switches to "downloading" the
            # moment the peer has the files instead of sitting on the finished
            # search until a byte count shows up.
            _job_search_done()
            _job_progress(_progress_snapshot(uname, wanted, set(), [], "queued"))

            # --- the log is the cheap gate (CD) --------------------------------
            if is_cd and cand["logs"]:
                # exactly one log per disc (candidate selection), so this wait
                # is over the wanted logs alone: it returns the moment they are
                # all local instead of waiting out a junk extra log's timeout.
                # The album transfers were queued in the SAME call above, so
                # they are already coming down: they are displayed alongside the
                # log (display_wanted) even though only the log gates progress.
                wanted_logs = [{"filename": f["file"], "size": f["size"]}
                               for f in cand["logs"]]
                _log("Downloading .log file(s) first for a quality check…")
                got_logs = _wait_for_files(slsk, ddir, uname, wanted_logs,
                                           timeout_s=_LOG_TIMEOUT_S,
                                           cancel_check=_cancelled, phase="logging",
                                           display_wanted=wanted)
                if len(got_logs) < len(wanted_logs):
                    _reject(uname, folder, f"only {len(got_logs)}/{len(wanted_logs)} log(s) arrived")
                    _cleanup_partial(ddir, uname, list(got_logs.values()))
                    _cancel_rest(slsk, uname, wanted, keep=got_logs)
                    continue
                if _cancelled():
                    return _finish("cancelled")
                _log("Grading rip log(s) with Logchecker…")
                scores = _score_logs(list(got_logs.values()), cfg)
                good, ignored = [], []
                for p, score, state, detail in scores:
                    _log(f"  {os.path.basename(p)}: score "
                         f"{score if score is not None else '?'}, checksum {state or '?'}")
                    (good if _log_passes(score, state, min_score) else ignored).append(
                        f"{os.path.basename(p)} — score "
                        f"{score if score is not None else 'unscorable'}"
                        f"{', checksum ' + state if state else ''}")
                # A folder can hold a second, junk log for the same disc (and a
                # manual/browse pick can hold several): the album rides on the
                # log that grades well, the rest are reported and ignored. A
                # checksum MISMATCH still costs the album its evidence — with
                # no good log left, the candidate is rejected and the album
                # transfers it just queued are dropped again.
                if not good:
                    _reject(uname, folder, "log rejected: " + "; ".join(ignored))
                    _cleanup_partial(ddir, uname, list(got_logs.values()))
                    _cancel_rest(slsk, uname, wanted, keep=got_logs)
                    continue
                for line in ignored:
                    _log(f"  ignoring {line}")
                _log(f"{len(good)} log(s) pass — downloading the full album…")

            # --- the album (already queued: no second queue wait) --------------
            est_timeout = _est_timeout(cand)
            _log(f"Downloading {len(wanted)} file(s) "
                 f"({cand['total_size'] / (1024 * 1024):.0f} MB, up to {est_timeout}s)…")
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
            # The album root on disk is the highest directory below the
            # download dir holding EVERY file of this candidate: slskd's
            # default `<ddir>/<leaf>/…` and the older `<ddir>/<user>/<leaf>/…`
            # both land there, and a multi-disc tree split over one parent
            # (…/CD1 + …/CD2) verifies as the union instead of half-importing.
            resolved = list(got.values())
            local_root = _local_album_root(ddir, resolved)
            if not local_root:
                _reject(uname, folder, "downloaded files did not land in one album "
                                       "folder under the download dir")
                _cleanup_partial(ddir, uname, resolved)
                continue
            _log("Verifying downloads against the rip log / decoders…")
            media = "CD" if is_cd else "Digital Media"
            _stamped, tag_problems = _stamp_media(local_root, media, cfg)
            ok, problems = _verify_album(local_root, cfg, is_cd)
            # A MEDIA tag that could not be written makes verify_album_checksums
            # skip every file, so it is a verification problem, never a warning.
            problems = list(tag_problems) + list(problems)
            ok = ok and not tag_problems
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

        if capped:
            raise RuntimeError(f"Stopped after {max_attempts} rejected candidate(s) "
                               f"— see the log.")
        raise RuntimeError("Every candidate was rejected "
                           f"({len(_job['attempts'])} attempt(s) — see the log).")

    except Exception as e:
        traceback.print_exc()
        _log(f"ERROR: {e}")
        _finish("error", {"error": str(e)})


def _build_from_templates(templates, release):
    return release_queries({"**": None, **release,
                            "medium_formats": release.get("medium_formats")},
                           {"soulseek_auto_cd_queries": templates,
                            "soulseek_auto_digital_queries": templates})


def _stamp_media(album_dir, media, cfg):
    """Write MEDIA (and clear SOURCE on a CD) before verification.

    Returns (stamped, problems). A file whose tag cannot be written is a
    problem, never a silent skip: verify_album_checksums keys off MEDIA, so a
    swallowed failure would let a CD rip "verify" without one CRC check."""
    from mlo.audio import AudioFile
    from server.main import is_audio_file
    n, problems = 0, []
    for root, _dirs, files in os.walk(album_dir):
        for f in sorted(files):
            if not is_audio_file(f):
                continue
            p = os.path.join(root, f)
            try:
                af = AudioFile(p)
                if af.audio is None:
                    problems.append(f"{f}: cannot be read for tagging")
                    continue
                if not str(af.get_tag("MEDIA") or "").strip():
                    af.set_tag("MEDIA", media)
                if media == "CD" and str(af.get_tag("SOURCE") or "").strip():
                    af.set_tag("SOURCE", "")
                n += 1
            except Exception as e:
                problems.append(f"{f}: MEDIA tag not written ({str(e)[:80]})")
    return n, problems


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
            if os.path.isdir(remove_root) and _inside(remove_root, ddir):
                shutil.rmtree(remove_root, ignore_errors=True)
        except Exception:
            pass
    try:
        upath = os.path.join(ddir, username)
        if os.path.isdir(upath) and not os.listdir(upath):
            os.rmdir(upath)
    except OSError:
        pass


def _import(local_root, release, cfg, media):
    """Move the verified download into the library and run the pipeline.

    `media` is the medium already detected for this candidate — MEDIA is
    stamped from it instead of re-detecting the medium of the whole folder."""
    from mlo.paths import library_root, move_path
    from server import main as srv
    from server.main import OrganizeRequest

    folder = str(cfg.get("music_folder") or "").strip()
    if not folder or not os.path.isdir(folder):
        raise RuntimeError("music_folder is not configured")

    name = f"{(release.get('artists') or [{}])[0].get('name', '')} - {release.get('title', '')}".strip(" -")
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip() or "Soulseek Import"
    # the library lives in <music folder>/Artists — the music folder root is
    # what the Soulseek network is shared from, not where albums belong.
    dest = os.path.join(library_root(folder), safe)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(library_root(folder), f"{safe} ({n})")
        n += 1

    if not move_path(local_root, dest, log=_log):
        # mlo.paths.move_path never copies-then-fails: it retries a locked file
        # (slskd still holding one) and gives up without half-moving the album.
        raise RuntimeError(
            f"could not move {os.path.basename(local_root)} into the library — "
            f"a file is still locked (Soulseek holding it open?); the download "
            f"is left intact at {local_root}")
    _log(f"Moved into the library: {os.path.basename(dest)}")

    # Any lossless source the folder arrived in (WAV/APE/ALAC...) becomes the
    # configured lossless codec BEFORE it is named and graded, so the naming
    # script and the grader both see the final extension.
    try:
        from mlo.flac import convert_album_lossless, target_codec
        s = convert_album_lossless(dest, cfg)
        if s.get("modified_count"):
            _log(f"Converted {s['modified_count']} lossless file(s) to "
                 f"{target_codec(cfg).upper()}.")
    except Exception:
        traceback.print_exc()

    # exact MusicBrainz identity before organizing; MEDIA itself was written
    # from the detected medium before verification (the CRC step needs it) and
    # only gets re-stamped if the conversion above rewrote files
    try:
        _stamp_mb_tags(dest, release)
    except Exception:
        traceback.print_exc()
    _stamped, tag_problems = _stamp_media(dest, media, cfg)
    for pr in tag_problems[:4]:
        _log("  ! " + pr)

    organized = False
    organize_error = None
    album_path = dest
    try:
        r = srv.organize(OrganizeRequest(paths=[dest], dry_run=False))
        res = (r.get("results") or [{}])[0] if isinstance(r, dict) else r.results[0]
        if isinstance(res, dict) and res.get("error"):
            organize_error = res["error"]
        else:
            organized = True
            # the naming script moved the album — report where it actually is
            # now, or the UI names a folder that no longer exists
            album_path = str(res.get("album_root") or dest)
            _log("Organized with the naming script.")
    except Exception as e:
        organize_error = str(e)

    srv._run_background_tagging()
    _log("Background tagging chain started (autotag → lyrics → grade).")
    return {"album_path": album_path, "imported": True,
            "staging_path": dest, "organized": organized,
            "organize_error": organize_error}
