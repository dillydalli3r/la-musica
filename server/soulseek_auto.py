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
     or decode-check each file (digital media); with AcoustID configured the
     audio is also matched against the release's group (a mismatch is logged,
     never fatal),
  5. import into the library: stamp the MusicBrainz release/recording IDs
     that drove the search into the tags, write MEDIA, organize with the
     naming script, then stage the album in the background — RateYourMusic
     links, artist metadata and cover art — via
     ``server.imports.finish_album(..., defer_tagging=True)``. Nothing
     unattended tags: the script chain is the wizard's / menu actions' job.

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

# Bulk import queue (artist / release-group "download all"): the pipeline runs
# ONE job at a time, so extra releases wait here and _finish() starts the next
# one — same machinery, no second pipeline.
#
# Reentrant: _start_next holds it while calling start_job(), which reports the
# new job through job_state() → queued() → this same lock. With a plain Lock
# that was a self-deadlock — EVERY enqueue (the release/release-group/artist
# Auto-import button) hung the request thread forever, holding _queue_lock so
# no later job could ever start. The same trap server.wishes documents.
_queue = []
_queue_lock = threading.RLock()


def _start_next():
    """Start the next queued release when the pipeline is idle."""
    with _queue_lock:
        if not _queue or job_active():
            return
        item = _queue.pop(0)
        if not start_job(**item).get("ok"):
            # the pipeline got taken between the check and the start: put it back
            _queue.insert(0, item)


def _release_key(release_mbid, release=None):
    """The identity a release is deduped by: its MusicBrainz id, whether it was
    handed in as an mbid or inside a resolved release dict."""
    return str(release_mbid or (release or {}).get("id") or "").strip().lower()


def _running_key():
    """The release id of the job the pipeline is currently on ("" when none)."""
    with _lock:
        return _release_key((_job.get("release") or {}).get("id"))


def _queued_keys():
    """Release ids already waiting in the bulk queue."""
    with _queue_lock:
        return {_release_key(i.get("release_mbid"), i.get("release"))
                for i in _queue} - {""}


def enqueue(release_mbid=None, release=None, queries=None, kind=None, mode=None):
    """Queue one release for auto-import; starts it immediately when idle.

    kind/mode describe an ID the HTTP route queued BEFORE resolving it (a
    release group or a whole artist): the job does that lookup — see _run.

    The same release id is never queued twice, and never while it is the job
    already running: the "download all" routes hand over a target list that can
    repeat (an artist page whose groups share an edition, a release already
    queued behind it), and a second copy of the same id just downloads and
    imports the same album again. A duplicate is logged with its reason and the
    live queue depth is returned.

    Returns the queue depth (0 when the release started right away)."""
    item = {"release_mbid": release_mbid, "release": release,
            "queries": queries, "kind": kind, "mode": mode}
    key = _release_key(release_mbid, release)
    # Read the running job's id BEFORE taking the queue lock. _start_next calls
    # job_state()/start_job() WITH the queue lock held, so queue → job is the
    # order this module locks in; taking them the other way round here would
    # deadlock against it.
    running = _running_key()
    dup = ""
    with _queue_lock:
        if not key:
            _queue.append(item)
        elif key == running:
            dup = "already being imported"
        elif any(_release_key(i.get("release_mbid"), i.get("release")) == key
                 for i in _queue):
            dup = "already queued"
        else:
            _queue.append(item)
    if dup:
        _log(f"{release_mbid or key}: {dup} — not queued again.")
        with _queue_lock:
            return len(_queue)
    _start_next()
    with _queue_lock:
        # our item is gone once it started → depth 0
        return len(_queue) if any(x is item for x in _queue) else 0


def queued():
    """Pending bulk-import releases (running job excluded)."""
    with _queue_lock:
        return [{"release_mbid": i.get("release_mbid"), "release": i.get("release")}
                for i in _queue]



def _job_search_done():
    """Clear the live search progress once a query has been scored."""
    with _lock:
        _job["search"] = None


def _job_search_progress(query, res):
    """Publish what the search stage is doing: which query, how many peers and
    files have answered.

    Deliberately NO countdown. The window is a ceiling that a usable candidate
    ends early, so a timer readout promised a duration the search does not
    serve — the UI shows the live response counts and an indeterminate
    "searching" instead, which is honest whether the wait ends in one second or
    in the full window."""
    with _lock:
        _job["search"] = {
            "query": query,
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
        st = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
              for k, v in _job.items() if k != "cancel"}
    st["queue"] = queued()
    return st


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
        # Stop the bulk queue too: the user pressed stop, so the whole
        # run stops (a single-job cancel is the same button).
        with _queue_lock:
            dropped = len(_queue)
            del _queue[:]
        if dropped:
            with _lock:
                _log(f"Stopped {dropped} queued release(s).")
        _confirm_event.set()  # a job parked on a prompt must wake up
        return True
    return False


def _log(msg):
    with _lock:
        _job["log"].append({"t": time.strftime("%H:%M:%S"), "msg": str(msg)})
        del _job["log"][:-400]  # keep the ring buffer bounded
        if msg:
            _job["stage"] = str(msg)


def _prune_downloads():
    """Drop the empty shells a finished job left in the download dir.

    The job's rejected/cancelled candidates and its imported album all take
    files away; slskd never removes a directory it created, so without this
    the `<user>/<batch id>/<album>` chains pile up. rmdir-only (see
    soulseek.prune_download_dirs) — nothing that still holds a byte goes."""
    try:
        from server import soulseek as slsk
        slsk.prune_download_dirs(slsk.download_dir())
    except Exception:
        pass


def _finish(state, result=None):
    with _lock:
        _job["state"] = state
        _job["result"] = result
        _job["progress"] = None   # nothing is downloading any more
        if state == "done":
            _job["stage"] = "Done"
    _prune_downloads()
    if state != "cancelled":
        # Bulk import: this release is over, start the next one in the queue.
        # A cancelled job stops the queue instead (the user said stop).
        _start_next()


# --------------------------------------------------------------------------- #
# Query building (customizable trait templates)
# --------------------------------------------------------------------------- #
def _norm_text(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip()


# A release can carry several catalog numbers (one per label/pressing). Each is
# searched as its OWN query — the caller runs every returned query as a
# separate search and merges the responses — but a release with a dozen numbers
# must not flood the network, so the expansion stops here.
_MAX_CATALOG_QUERIES = 4


def _catalog_numbers(release):
    """Every distinct, normalized catalog number of a release, in MB order.

    Older payloads carry only the singular `catalog_number`; it is used as the
    sole entry when the plural key is missing or empty.
    """
    raw = release.get("catalog_numbers") or [release.get("catalog_number", "")]
    out = []
    for v in raw:
        v = _norm_text(v)
        if v and v not in out:
            out.append(v)
        if len(out) >= _MAX_CATALOG_QUERIES:
            break
    return out


def _expand_template(tpl, fields, catalogs):
    """Render one template. A template naming `catalognumber` yields one query
    per catalog number (so `["catalognumber"]` on a two-number CD is two
    queries); any other template yields exactly one, as it always has."""
    names = [f.strip().lower() for f in str(tpl).split()]
    if "catalognumber" not in names:
        q = _norm_text(" ".join(p for p in (fields.get(f, "") for f in names) if p))
        return [q] if q else []
    out = []
    for cn in catalogs:
        parts = [cn if f == "catalognumber" else fields.get(f, "") for f in names]
        q = _norm_text(" ".join(p for p in parts if p))
        if q and q not in out:
            out.append(q)
    return out


def release_queries(release, cfg):
    """Search queries from the release's traits, in configured priority order.

    Each template is a space-separated list of field names; supported fields:
    artist, album, year, date, country, catalognumber, barcode, label.
    A CD defaults to its catalog number alone (the trait rip folders actually
    carry, and the one search that does not drag in every other pressing);
    digital media, which has no catalog number, to "artist album year".

    A CD whose release carries NO catalog number would otherwise have nothing
    to search for at all, so it falls back to the digital template instead of
    failing the job — one search, and the caller logs which one it was.

    A release carrying several catalog numbers (one per label/pressing) yields
    one query per number for every template that names `catalognumber`, capped
    at `_MAX_CATALOG_QUERIES`.
    """
    is_cd = "CD" in (release.get("medium_formats") or [])
    key = "soulseek_auto_cd_queries" if is_cd else "soulseek_auto_digital_queries"
    templates = cfg.get(key) or []
    if isinstance(templates, str):
        templates = [templates]
    if not templates:
        templates = ["catalognumber"] if is_cd else ["artist album year"]

    fields = {
        "artist": _norm_text((release.get("artists") or [{}])[0].get("name", "")),
        "album": _norm_text(release.get("title", "")),
        "year": str(release.get("date") or "")[:4],
        "date": str(release.get("date") or ""),
        "country": str(release.get("country") or ""),
        "barcode": str(release.get("barcode") or ""),
        "label": _norm_text(release.get("label", "")),
    }
    catalogs = _catalog_numbers(release)
    queries = []
    for tpl in templates:
        for q in _expand_template(tpl, fields, catalogs):
            if q not in queries:
                queries.append(q)
    if not queries and is_cd:
        # The catalog-number template resolved to nothing (MusicBrainz has no
        # CATALOGNUMBER for this release). `catalognumber` is the default for a
        # reason, but a release with none would have no search at all — one
        # broader query beats a job that cannot start, and the fallback is
        # logged so it is never a silent change of plan.
        fallback = _norm_text(" ".join(p for p in
                                      (fields["artist"], fields["album"], fields["year"]) if p))
        if fallback:
            _log("this release has no catalog number — searching by "
                 f"“{fallback}” instead")
            queries.append(fallback)
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
        # A fast peer both finishes sooner and is likelier to serve the whole
        # folder, so the rate the network advertised decides between otherwise
        # equal folders: the folder's SLOWEST file (a folder is only as fast as
        # its worst peer) earns up to 4.5 points at 1 MiB/s per point —
        # deliberately under one matched track (5), so speed breaks ties
        # between equally complete folders instead of buying an incomplete one.
        speed_mib = (min(speeds) if speeds else 0) / (1024 * 1024)
        score = (5 * matched + 2 * len(logs) + len(cues)
                 + 8 * lossless + 4 * slot - queue / 100.0
                 + min(4.5, speed_mib))
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


def _search_queries(slsk, queries, wait_s, usable=None, response_limit=0):
    """Run every query template AT ONCE and poll them in one loop.

    All templates are POSTed up front, so the wall time is one window
    (`wait_s` + the grace tail) instead of one window per template — up to
    three minutes before the first byte became one minute. The per-request
    `timeout` is passed in MILLISECONDS because slskd counts it that way (see
    soulseek.search), and it is QUIET time: a search only ends when the
    network stops answering. That is why `response_limit` matters — slskd
    serves `/searches/{id}/responses` only once a search has ENDED, so a
    popular album (peers replying for a minute straight) used to hand back
    nothing until the whole ceiling had elapsed even though hundreds of files
    were already counted. The limit ends the search as soon as enough peers
    have answered, which is what makes a good copy start downloading in
    seconds instead of a minute.

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
            watch.append([slsk.search(q, timeout_ms=int(wait_s * 1000),
                                      response_limit=response_limit), q, None])
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
        _job_search_progress(display, {
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
    for sid, q, res in watch:
        if not slsk.is_search_done(res or {}):
            # Still running when the window closed. Nothing readable will come
            # out of it (responses are served only after a search ends), so it
            # is cancelled rather than left occupying slskd's search slots.
            try:
                slsk.cancel_search(sid)
            except Exception:
                pass
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
    layout depends on version/settings: the generated config pins slskd's
    destination template to `${SOURCE_USERNAME}/${BATCH_ID}/${SOURCE_PATH}`
    (see soulseek.DESTINATION_SUBDIR), so a download lands at
    `<ddir>/<user>/<batch id>/<remote path>/…`; a pre-upgrade install still has
    `<ddir>/<leaf>/…` (slskd's old default) or `<ddir>/<username>/<leaf>/…`.
    The caller builds this ONCE per poll tick instead of walking the tree once
    per pending file; a same-named file from another album must never satisfy a
    pending download, so the scan never walks the whole download dir.

    Dot-directories are pruned: partials now stage in a SIBLING `incomplete/`
    (see soulseek._incomplete_dir), but a pre-migration install still has them
    under `<ddir>/.incomplete`, and a half-written file must never be indexed
    as a completed one."""
    index = {}
    incomplete = (os.sep + ".incomplete").lower()
    unreached = []
    for leaf in leaves:
        found = False
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
                    found = True
        if not found:
            # the batch id sits between the username and the leaf, so neither
            # scan root can name this download's tree
            unreached.append(leaf)
    if unreached:
        _index_user_tree(ddir, username, index)
    return index


def _index_user_tree(ddir, username, index):
    """Index THIS user's own subtree into `index` (basename -> [paths]).

    The batch-dir layout puts the download below the username but above the
    folder leaf, which the leaf-rooted scans in _index_download_tree cannot
    reach. One walk of that user alone — never of the whole download dir —
    finds it, with dot-dirs and `incomplete` pruned exactly as there."""
    user = str(username or "").strip()
    user_root = os.path.join(ddir, user)
    if not user or not _inside(user_root, ddir) or not os.path.isdir(user_root):
        return
    incomplete = (os.sep + ".incomplete").lower()
    for root, dirs, files in os.walk(user_root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if incomplete in root.lower():
            continue
        for f in files:
            index.setdefault(f, []).append(
                os.path.normpath(os.path.join(root, f)))


def _local_download_candidates(ddir, username, remote, size, index=None):
    """All plausible local locations for a remote file.

    The exact batch-dir shape (`<ddir>/<username>/<batch id>/<rel>`) cannot be
    named here — the batch id is slskd's own — so the two fixed shapes
    (`<ddir>/<rel>`, `<ddir>/<username>/<rel>`) are checked first and the
    candidate's own album tree from `index` (see _index_download_tree) then
    catches the file wherever the batch id put it, or where slskd sanitised a
    share name; without an index the tree of THIS file's leaf is walked, so the
    function stays usable on its own. A missing file (slskd renamed or removed
    it between the isfile test and the size read) or an unreadable one counts
    as not-yet-present rather than raising out of the job."""
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
    # The batch-dir layout keeps the remote path INTACT below the batch id, so
    # a hit ending in the full remote relative path is that very file; a hit
    # sharing only the basename is the fallback for the layouts that dropped
    # the remote parent directories. Callers take cands[0], so the exact ones
    # are offered first — a same-named file of the user's other album must not
    # win on tree order.
    full_tail = os.path.normcase(os.sep + rel.replace("/", os.sep))
    hits = sorted(index.get(base, ()),
                  key=lambda p: not os.path.normcase(p).endswith(full_tail))
    for p in hits:
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

    Files live under `<ddir>/<user>/<batch id>/<remote path>/…` (the pinned
    destination template, see soulseek.DESTINATION_SUBDIR), or at the older
    `<ddir>/<leaf>/…` / `<ddir>/<user>/<leaf>/…` shapes. The common parent of
    the candidate's own files is the album either way: taking the first file's
    directory instead half-imported a multi-disc release (it verified one disc
    folder and stranded the rest), and slskd's leaf-only default stranded them
    above `ddir` entirely. Returns None when the files do not share one
    directory below `ddir` (they landed loose in it) — the caller rejects the
    candidate with a clear reason rather than verifying a directory it cannot
    name."""
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


def _transfer_ok(state):
    """slskd reports this transfer finished SUCCESSFULLY — the terminal states
    minus the failures (finished_transfer() alone is true for Errored too)."""
    from server.soulseek import finished_transfer
    st = str(state or "")
    return (finished_transfer(st)
            and not any(x in st for x in _FAILED_TRANSFER_STATES))


def _transfer_active(state):
    """slskd still owes us this file: a state that is neither terminal nor a
    failure (Queued, Initializing, InProgress, Requested). This is the state a
    complete local file must NOT be sitting under for the wait to trust it."""
    from server.soulseek import finished_transfer
    st = str(state or "")
    return (not finished_transfer(st)
            and not any(x in st for x in _FAILED_TRANSFER_STATES))



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


def _queue_budget(cand):
    """How long this candidate's own queue is worth waiting for: the files the
    search reported queued ahead of us, at the peer's rate — the same figures
    _est_timeout budgets the queued part of its total from. Floored at
    _STALL_AFTER_S so a peer that reported no queue at all still gets the old
    grace, and capped by the wait's own deadline either way."""
    rate = float(cand.get("speed") or 0) or _MIN_RATE
    size = float(cand.get("total_size") or 0)
    files = len(cand.get("files") or []) or 1
    queued = int(cand.get("queue") or 0) * (size / files)
    return max(_STALL_AFTER_S, queued / rate)


def _progress_snapshot(username, wanted, got, transfers, phase, speed=None):
    """The live download block the Soulseek page renders.

    Aggregate counters plus one row per wanted file, straight from slskd's own
    transfer records (`_user_transfers`, consumed per candidate user+files —
    never unrelated downloads). A file slskd has not reported a size for
    contributes only what is known; nothing is invented, and `percent` is
    clamped so a peer's own bogus counter cannot print 240 %.

    The two file counts are deliberately different units and are labelled as
    such by the UI: `files_done` is what SLSKD calls complete (a terminal
    success, or 100 % of a known size) and `files_arrived` is what this job's
    wait has formally accepted on disk (a file's `done` flag). `percent`/
    `bytes`/`size` stay byte-weighted.

    `speed` is the instantaneous aggregate rate the wait measured from the
    byte delta between its own ticks. When there is no measurement yet (the
    first tick, the call made before any poll) the fallback is slskd's
    per-transfer `averageSpeed` summed over the ACTIVE transfers only — a
    finished file's frozen average is not throughput. `eta_s` is the remaining
    bytes over that rate, so it is null/nothing rather than the largest
    per-file guess (a queued file's `remainingTime` guess used to win) when no
    rate is known."""
    by_name = {t["filename"]: t for t in transfers}
    files, nbytes, nsize, arrived, complete, fallback = [], 0, 0, 0, 0, 0.0
    for w in wanted:
        name = w["filename"]
        t = by_name.get(name) or {}
        done = name in got
        size = int(t.get("size") or w.get("size") or 0)
        b = size if done else int(t.get("bytes") or 0)
        state = str(t.get("state") or "")
        slsk_pct = t.get("percent")
        pct = slsk_pct
        if done:
            pct = 100
        elif pct is None:
            pct = (b * 100 / size) if size else 0
        # `complete` is slskd's verdict, never ours: a file this wait accepted
        # from disk alone (b) has no transfer record to vouch for it.
        is_complete = (bool(t) and _transfer_ok(state)) or (
            slsk_pct is not None and float(slsk_pct) >= 100 and size > 0)
        files.append({
            "name": os.path.basename(name.replace("\\", "/")),
            "bytes": b, "size": size,
            # One decimal, not a whole percent: a big file's bar has to move
            # between two of slskd's own samples.
            "percent": round(max(0.0, min(100.0, float(pct))), 1),
            "speed": float(t.get("speed") or 0),
            "state": "Done" if done else state,
            "done": done, "complete": is_complete,
        })
        nbytes += b
        nsize += size
        arrived += 1 if done else 0
        complete += 1 if is_complete else 0
        if _transfer_active(state):
            fallback += float(t.get("speed") or 0)
    if speed is None:
        speed = fallback
    remaining = max(0, nsize - nbytes)
    return {
        "phase": phase,
        "username": username,
        "dir": os.path.dirname(wanted[0]["filename"].replace("\\", "/")) if wanted else "",
        "files_done": complete,
        "files_arrived": arrived,
        "files_total": len(files),
        "bytes": nbytes,
        "size": nsize,
        "percent": round(max(0.0, min(100.0, nbytes * 100 / nsize)), 1) if nsize else 0,
        "speed": speed,
        "eta_s": int(remaining / speed) if speed > 0 and remaining else None,
        "files": files,
    }


# A cancelled transfer is not instant. slskd answers the DELETE while a request
# is still in flight and flushes the bytes it already received, which re-created
# the very files the rejection had just deleted — and the next candidate, often
# the same album from another peer, downloaded the whole thing again. Settle
# before each sweep, then keep sweeping until a pass removes nothing (live runs
# showed temp writes still landing seconds after the cancel).
_CANCEL_SETTLE_S = 1.0
# Pass ceiling for that convergence loop: a peer that keeps dribbling bytes
# must not hold the job, and 3 passes of settle+delete cover the observed
# window (the writes landed within a second or two of the DELETE).
_CANCEL_SWEEPS = 3
# slskd's temp write: the target name plus `_<ticks>` before the extension
# ("01 - Bombtrack_639252322461155685.flac"). 6+ digits is deliberately looser
# than the 18-19 it emits today, and the stem still has to be a wanted file.
_TEMP_SUFFIX_RE = re.compile(r"^(.*)_(\d{6,})$")


def _cancel_candidate(slsk, username, wanted):
    """Cancel this candidate's transfers and confirm slskd really dropped them.

    soulseek.cancel_downloads reports the ids it did NOT confirm through its
    `failed` collector (a real refusal, or an unreachable slskd — never the
    404/400 of a transfer that was already gone). Those are retried once; the
    caller deletes only afterwards, because a transfer slskd still tracks would
    just re-fetch the files being removed. Returns the ids left unconfirmed."""
    names = {w["filename"] for w in wanted}
    if not names:
        return []
    from server.soulseek import _user_transfers
    try:
        ids = [t["id"] for t in _user_transfers(slsk, username, names) if t.get("id")]
    except Exception:
        return []
    for _ in range(2):
        if not ids:
            return []
        failed = []
        try:
            slsk.cancel_downloads(username, ids, failed=failed)
        except Exception:
            return []
        if not failed:
            return []
        ids = failed          # one retry for the ids slskd did not confirm
    return ids


def _candidate_dirs(ddir, username, wanted, known_files=()):
    """The candidate's OWN album folder(s) on disk — where slskd writes the
    temp-suffixed partials of this candidate's files.

    Named from the remote path, not from whatever happens to be on disk: the
    remote folder's own components must be a path's tail below this user's tree
    (the batch id sits in between, see _index_user_tree), which keeps the scan
    on the candidate's album and nowhere else. The parents of files already
    resolved (`known_files`) are added for the one layout that has no remote
    folder to match — a candidate whose files sit at the top of the share."""
    tails = set()
    for w in wanted:
        tail = _remote_rel(w["filename"]).split("/")[:-1]
        if tail:
            tails.add(tuple(os.path.normcase(x) for x in tail))
    out = []
    for tail in sorted(tails):
        for base in (os.path.join(ddir, *tail), os.path.join(ddir, username, *tail)):
            if os.path.isdir(base) and base not in out:
                out.append(base)
    user_root = os.path.join(str(ddir), str(username or "").strip())
    if str(username or "").strip() and _inside(user_root, ddir) and os.path.isdir(user_root):
        for root, dirs, _files in os.walk(user_root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            parts = os.path.normcase(os.path.normpath(root)).replace("\\", "/").split("/")
            for tail in tails:
                if tuple(parts[-len(tail):]) == tail and root not in out:
                    out.append(root)
    for p in known_files:
        d = os.path.dirname(str(p))
        if os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def _slskd_partial(name, wanted):
    """Whether `name` is slskd's temp write of one of THIS candidate's files:
    `<wanted stem>_<digits><same extension>` (`01 - Bombtrack_639252322461155685.flac`).

    slskd writes a transfer's bytes into the DESTINATION dir under that name
    while it runs and renames it on success. A cancelled transfer leaves the
    temp file behind — seconds after the DELETE answered — so a candidate that
    looked clean still held near-complete copies. The resolver cannot name them
    (the suffix breaks the basename test) and clear_transfer_files refuses
    download-dir bytes with no size to compare, so they are matched here: the
    stripped stem and the extension must be one of the candidate's own wanted
    files, and nothing else. A stem that is not wanted (another album's) never
    matches, whatever its suffix."""
    stem, ext = os.path.splitext(str(name))
    m = _TEMP_SUFFIX_RE.match(stem)
    if not m:
        return False
    base = m.group(1)
    for w in wanted:
        wstem, wext = os.path.splitext(os.path.basename(_remote_rel(w["filename"])))
        if (os.path.normcase(ext) == os.path.normcase(wext)
                and os.path.normcase(base) == os.path.normcase(wstem)):
            return True
    return False


def _drop_temp_partials(ddir, username, wanted, known_files=()):
    """Delete slskd's temp-suffixed partials of this candidate's own files.

    Only inside the candidate's own album folder(s) (see _candidate_dirs) and
    only for a stem in the candidate's wanted set (see _slskd_partial): a
    foreign peer's identically named temp file, and a same-suffix file of
    another album, are never touched. Returns how many files it removed."""
    removed = 0
    for d in _candidate_dirs(ddir, username, wanted, known_files):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not _slskd_partial(name, wanted):
                continue
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            try:
                os.remove(p)
                removed += 1
            except OSError:
                pass
    return removed


def _sweep_candidate(slsk, ddir, username, wanted, remove_root=None):
    """ONE delete pass over a rejected candidate: its own files, its staged
    partials under `incomplete/`, and slskd's temp-suffixed partials in its own
    album folder. Returns how many files it removed — 0 means the candidate is
    gone, which is what ends _drop_candidate's convergence loop."""
    files = _local_wanted_files(ddir, username, wanted)
    removed = _cleanup_partial(ddir, files, remove_root=remove_root)
    for w in wanted:
        # only the STAGING dirs (size 0 makes the helper's download-dir test
        # refuse every file there): its staged shapes are derived from this
        # file's own remote path, while its basename scan of the download dir
        # would reach a sibling candidate's leftover.
        try:
            removed += int(slsk.clear_transfer_files(ddir, username,
                                                     w["filename"], 0)["files_deleted"])
        except Exception:
            pass
    return removed + _drop_temp_partials(ddir, username, wanted, files)


def _drop_candidate(slsk, ddir, username, wanted, remove_root=None):
    """Make a rejection final: cancel, then delete — verified, until it holds.

    Cancelling first (and confirming it) stops slskd from re-requesting the
    files the next lines delete. Each pass then removes the candidate's WHOLE
    set — album audio, cue and the graded log — resolved by layout
    (`<ddir>/<user>/<batch id>/<remote path>` as well as the older shapes, see
    _local_wanted_files), its staged partials under `incomplete/`
    (clear_transfer_files, which owns that path) and slskd's temp-suffixed
    partials in the candidate's own album folder (_drop_temp_partials).

    The passes repeat over a settle until one removes NOTHING: slskd keeps
    flushing a cancelled transfer's bytes for a moment, and a live run showed
    temp writes landing in the destination dir seconds after the cancel — a
    fixed two passes missed those. _CANCEL_SWEEPS bounds it, and a pass count
    above one is logged so a peer that keeps dribbling bytes is visible.

    Scope is strictly the candidate's own remote paths under its own user tree:
    a sibling candidate's same-named file and an already-completed download are
    never touched (see _in_candidate_folder for why the resolver's basename
    fallback is not used on a delete)."""
    unconfirmed = _cancel_candidate(slsk, username, wanted)
    if unconfirmed:
        _log(f"  slskd did not confirm {len(unconfirmed)} cancelled transfer(s) — "
             f"removing what is on disk anyway")
    passes = 0
    for _ in range(_CANCEL_SWEEPS):
        time.sleep(_CANCEL_SETTLE_S)
        if not _sweep_candidate(slsk, ddir, username, wanted, remove_root):
            break            # nothing left: the candidate is really gone
        passes += 1          # this pass had work to do
    if passes > 1:
        _log(f"  candidate removed in {passes} pass(es) — slskd kept writing "
             f"after the cancel")


def _in_candidate_folder(path, remote):
    """Whether a LOCAL path is where THIS remote file lives: its last two
    components — "<album folder>/<file>", or just the file at the top of the
    share — must be the remote file's own.

    _local_download_candidates answers "has this file arrived?" and its
    basename fallback is right for that. "May I DELETE this file?" needs the
    candidate's own path back: on the second sweep the rejected candidate's
    bytes are already gone, so the fallback matched the SAME user's OTHER album
    — same basename, even the same size — and deleted a candidate that had not
    been tried yet. Kept case-insensitive, like the resolver's own comparisons;
    the last two components still hold when slskd drops the remote's parent
    directories below the batch id, which is why the tail, not the whole
    relative path, is the test."""
    tail = _remote_rel(remote).split("/")[-2:]
    parts = os.path.normpath(str(path)).replace("\\", "/").split("/")
    return [os.path.normcase(x) for x in parts[-len(tail):]] == \
           [os.path.normcase(x) for x in tail]


def _local_wanted_files(ddir, username, wanted):
    """Local paths of this candidate's OWN files that are on disk already —
    the partials a rejection must delete, the WHOLE set included.

    The .log gate queues the album in the same pass as the logs, so a rejected
    gate used to delete the logs only and leave the album bytes that had landed
    as orphans for the next candidate to download again. One entry per remote
    file, and two deliberate differences from what the download wait accepts:
    no size test (a truncated leftover IS what a rejected candidate leaves
    behind, and the peer's reported size is what the wait, not the delete,
    needs), and the candidate's own remote folder (see _in_candidate_folder —
    nothing outside the candidate's own tree may be removed)."""
    leaves = {_leaf_of(w["filename"]) for w in wanted} - {""}
    index = _index_download_tree(ddir, username, leaves)
    out = []
    for w in wanted:
        for p in _local_download_candidates(ddir, username, w["filename"], 0,
                                            index=index):
            if _in_candidate_folder(p, w["filename"]):
                if p not in out:
                    out.append(p)
                break
    return out


def _log_fail_reason(name, score, state, min_score):
    """Why one rip log did not clear the bar.

    The reason names the required score, so the bar this run is judging by
    (Settings → Auto-import) is discoverable from the attempt itself instead of
    a bare number the user has to guess at."""
    if score is None:
        return f"{name} — unscorable"
    if state == "invalid":
        return f"{name} — checksum invalid"
    return f"{name} — score {score} is below the required {min_score}"


def _wait_for_files(slsk, ddir, username, wanted, timeout_s, cancel_check=None,
                    phase="download", queue_budget_s=None):
    """Poll until every wanted remote path is present locally AND slskd vouches
    for it.

    A pending file is accepted when EITHER

      (a) slskd reports a successful terminal state for it AND a local file
          matches — path shapes first, then this candidate's own album tree at
          exactly the expected size — or
      (b) slskd has NO record in an ACTIVE state for it (_transfer_active: a
          transfer list pruned or cleared mid-job, or a record in a failed
          state) AND a local file sits at the expected size inside the
          candidate's OWN album trees with an mtime older than two poll
          intervals. Disk presence is not proof of a live transfer, so (b) is
          fenced: never while a recorded state is active, never on a size
          mismatch, never for a file still being written. It is logged per file
          so a missing slskd record is visible rather than silent. Without (b) a
          finished album whose slskd record was dropped was rejected, its files
          deleted, and the album downloaded again from the next peer.

    A file on disk is otherwise not proof on its own: the peer may never have
    reported a size for it (0/unknown must not satisfy the wait), and a
    same-named file left behind by an earlier attempt would otherwise count as
    arrived.

    slskd's transfer states are read alongside the filesystem, so a peer that
    rejects the slot (or an errored transfer) costs a poll interval instead of
    the whole per-candidate timeout.

    Stall rules, in order:
      * records exist and some are InProgress — a transfer that has moved no
        bytes for `_STALL_AFTER_S` while EVERY live transfer is InProgress (a
        peer that stopped sending) is dead. A peer with a mix of moving and
        waiting files, or a transfer that is still moving, is never abandoned
        early.
      * no live transfer is InProgress at all (every one Queued, a dropped
        request, or no record) — the peer is simply busy, and `_est_timeout`
        has always budgeted that queue wait, so the candidate is held for
        `queue_budget_s` (its own queue budget, see _queue_budget) instead of
        180 s. Defaults to `_STALL_AFTER_S` when the caller has no candidate
        budget (the .log gate, which is deliberately cheap and bounded).

    Each tick publishes the download progress payload (phase 'logging' while
    the .log gate is being fetched, 'download' for the album) and clears it
    again on the way out — search, verify and import have no download in
    flight. `speed` in that payload is the INSTANTANEOUS rate measured here
    from the byte delta between ticks, never slskd's per-transfer lifetime
    average. wanted = [{filename, size}]. Returns {remote: local_path} for the
    files that arrived complete; missing entries are absent from the dict.

    The progress block SHOWS exactly the `wanted` set: the wait is only ever
    called once the transfers it waits for are the only ones queued (the .log
    gate's album is requested after the gate passes, see _run), so there is
    nothing further to display."""
    deadline = time.time() + timeout_s
    started = time.time()
    from server.soulseek import _user_transfers
    shown = list(wanted)
    pending = {w["filename"]: w for w in wanted}
    # Transfers are fetched for the DISPLAY set and frozen at entry, so every
    # row in the progress block keeps its live state even after its file lands
    # (a row fetched per tick lost its bytes/speed the moment it completed).
    fetch_names = {w["filename"] for w in shown}
    leaves = {_leaf_of(w["filename"]) for w in wanted} - {""}
    got = {}
    live = []
    last_bytes = -1
    last_progress = time.time()
    rate = None            # measured bytes/s between the last two ticks
    disp_bytes = None
    disp_time = None
    queue_ceiling = _STALL_AFTER_S if queue_budget_s is None else queue_budget_s
    try:
        while time.time() < deadline and pending:
            tick = time.time()
            if cancel_check and cancel_check():
                break
            # one transfer-tree fetch per tick, and one tree walk per tick too
            # (only when slskd actually says a file is done, or when a file has
            # no live record and its local counterpart has to be looked for)
            live = _user_transfers(slsk, username, fetch_names)
            done_states = {t["filename"] for t in live if _transfer_ok(t["state"])}
            active = {t["filename"] for t in live if _transfer_active(t["state"])}
            ready = [r for r in pending if r in done_states]
            orphans = [r for r in pending if r not in done_states and r not in active]
            if ready or orphans:
                index = _index_download_tree(ddir, username, leaves)
                for remote in ready:
                    cands = _local_download_candidates(
                        ddir, username, remote, int(pending[remote].get("size") or 0),
                        index=index)
                    if cands:
                        got[remote] = cands[0]
                        del pending[remote]
                for remote in orphans:
                    if remote not in pending:
                        continue
                    size = int(pending[remote].get("size") or 0)
                    if not size:
                        continue     # an unknown size proves nothing
                    for p in _local_download_candidates(ddir, username, remote, size,
                                                        index=index):
                        try:
                            st = os.stat(p)
                        except OSError:
                            continue
                        if st.st_size != size or tick - st.st_mtime <= 2 * _TRANSFER_POLL_S:
                            continue     # another file, or one still being written
                        got[remote] = p
                        del pending[remote]
                        _log(f"  {os.path.basename(remote)}: slskd has no live "
                             f"transfer record — accepted the local file "
                             f"({size / (1024 * 1024):.1f} MB, unchanged for "
                             f"{int(2 * _TRANSFER_POLL_S)}s)")
                        break
            # Instantaneous throughput from the byte delta between ticks: slskd's
            # per-transfer averageSpeed is a LIFETIME average, so finished files
            # kept inflating the reported rate long after they stopped moving.
            moved_shown = sum(t["bytes"] for t in live)
            if disp_bytes is not None and tick > disp_time:
                rate = max(0.0, (moved_shown - disp_bytes) / (tick - disp_time))
            disp_bytes, disp_time = moved_shown, tick
            _job_progress(_progress_snapshot(username, shown, got, live, phase,
                                             speed=rate))
            if not pending:
                break
            moving = [t for t in live if t["filename"] in pending]
            if any(any(x in t["state"] for x in _FAILED_TRANSFER_STATES) for t in moving):
                break
            moved = sum(t["bytes"] for t in moving)
            inprog = [t for t in moving if "InProgress" in t["state"]]
            if moved > last_bytes:
                last_bytes, last_progress = moved, time.time()
            elif inprog:
                # every live transfer InProgress and none moving = a peer that
                # stopped sending. A mix (one file moving, another still
                # queued) is a working peer and is left alone.
                if (time.time() - last_progress > _STALL_AFTER_S
                        and len(inprog) == len(moving)):
                    _log(f"  transfer stalled for {int(_STALL_AFTER_S)}s with no "
                         f"progress — moving on")
                    break
            elif tick - started > queue_ceiling:
                # nothing InProgress at all: the peer is intact, we are just
                # behind its queue (or slskd never started the transfer). The
                # candidate's own queue budget — the wait _est_timeout already
                # paid for — is the only reason to give up on it.
                _log(f"  still queued after {int(queue_ceiling)}s (this peer's own "
                     f"queue budget) — moving on")
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
def _mb_release_type(rel):
    """'Album; Live' — MusicBrainz release types in Picard's casing.

    Same rule as server/beets/mloplugin.py (EP uppercased, the rest Title
    Case, '; '-joined) so the beets pass that may follow is a no-op."""
    types = [rel.get("primary_type") or ""]
    types += list(rel.get("secondary_types") or [])
    if not any(str(t).strip() for t in types):
        # a release dict built without the structured pair (e.g. a browsed
        # folder) — fall back to the historical lowercase '+'-joined string
        types = str(rel.get("release_type") or "").split("+")
    out = []
    for t in types:
        t = str(t).strip()
        if t:
            out.append("EP" if t.lower() == "ep" else t.title())
    return "; ".join(out)


def _stamp_mb_tags(album_dir, release):
    """Write the exact MusicBrainz release identity into the tags so beets /
    grading work with the release that drove the search.

    Identity tags (release/group/artist IDs, date, country, status, type,
    catalog number, label) are FORCE-written: a download that arrived
    carrying another pressing's IDs would otherwise keep them and drive beets
    matching, the naming script and grading off the wrong release. Per-track
    number/title/artist tags are corrected only when they contradict the
    chosen release, so a matching uploader's spelling survives."""
    from mlo.audio import AudioFile

    tracks_meta = release.get("media") or []
    artists = release.get("artists") or []
    artist_mbid = (artists[0].get("mbid") if artists else "") or ""
    artist_name = (artists[0].get("name") if artists else "") or ""
    album_title = str(release.get("title") or "")

    identity = {
        "MUSICBRAINZ_ALBUMID": release.get("id", ""),
        "MUSICBRAINZ_RELEASEGROUPID": release.get("release_group_id", ""),
        # the release's artist credit IS the album artist (Picard semantics)
        "MUSICBRAINZ_ARTISTID": artist_mbid,
        "MUSICBRAINZ_ALBUMARTISTID": artist_mbid,
        "CATALOGNUMBER": release.get("catalog_number", ""),
        "LABEL": release.get("label", ""),
        "BARCODE": release.get("barcode", ""),
        "DATE": release.get("date", ""),
        "ORIGINALDATE": release.get("originaldate", ""),
        # canonical spelling: mlo.audio maps RELEASECOUNTRY, and the naming
        # script's $releasecountry reads it (COUNTRY is a raw container key)
        "RELEASECOUNTRY": release.get("country", ""),
        "RELEASESTATUS": release.get("status", ""),
        "RELEASETYPE": _mb_release_type(release),
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

    def _write(af, key, value):
        """Set a tag unless it already carries exactly this value."""
        value = "" if value is None else str(value).strip()
        if not value:
            return
        if str(af.get_tag(key) or "").strip() == value:
            return
        af.set_tag(key, value)

    n = 0
    for p in audio:
        try:
            af = AudioFile(p)
            if af.audio is None:
                continue
            for k, v in identity.items():
                _write(af, k, v)
            # Album-level spelling of the chosen release (corrected when the
            # uploader's tags say something else).
            _write(af, "ALBUM", album_title)
            _write(af, "ALBUMARTIST", artist_name)
            t = assign.get(p)
            if t:
                _write(af, "MUSICBRAINZ_TRACKID", t.get("recording_mbid"))
                _write(af, "TRACKNUMBER", t.get("position"))
                _write(af, "DISCNUMBER", t.get("disc"))
                _write(af, "TITLE", t.get("title"))
                _write(af, "ARTIST", t.get("artist_credit"))
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
              target_dir=None, confirm_lossy=False, kind=None, mode=None):
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

    A release id already waiting in the bulk queue is refused: it is about to
    run anyway, and letting it through here both downloaded it twice and left
    the duplicate behind in the queue to run a third time."""
    # The queue lock is taken on its own (never while holding _lock): _finish
    # holds _lock and calls _start_next, which takes this one, so queue → job
    # is the order this module locks in and the reverse would deadlock.
    key = _release_key(release_mbid, release)
    if key and key in _queued_keys():
        return {"ok": False, "error": "this release is already queued for import",
                "job": job_state()}
    # Already in the library: nothing to import, and re-downloading it is what
    # left a second copy of the same album beside the first (observed live —
    # the HTTP start route reached this function with a release the library
    # already held). The bulk paths skip owned releases; so does this one now.
    # A folder grab (username + target_dir, no MusicBrainz id) has nothing to
    # compare and stays untouched.
    if key:
        try:
            from server import wishes
            if key in wishes.owned_mbids(load_config()):
                return {"ok": False, "error": "this release is already in your "
                                              "library — nothing to download",
                        "job": job_state()}
        except Exception:
            pass      # a library that cannot be read must not block the job
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
                                 confirm_lossy=confirm_lossy,
                                 kind=kind, mode=mode),
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


def _ask_to_wish(release, queries, waited, cfg, confirm_lossy):
    """Park the job on the "add to wishes?" prompt; add the wish if accepted.

    A job that ends with nothing imported used to leave the user a bare error
    and nothing else, even though the background wishes worker keeps searching
    for any release in the wish list — with these very queries. So the two
    dead ends (no usable folder at all, and every candidate rejected) both offer
    the SAME parking spot, through this one helper: one prompt per job, one
    payload shape (reason "no_results"), one place that decides when the offer
    does not apply — a release with no MusicBrainz id has nothing to wish for,
    and the background wishes path (confirm_lossy False) must never ask a wish
    to become a wish.

    Returns the finished-job result when the wish was added, else None (the
    prompt did not apply, or the user declined); a cancelled job is reported by
    the caller's own `_cancelled()` check, which is the same check every other
    prompt in this file uses."""
    if not (confirm_lossy and release.get("id")
            and cfg.get("soulseek_auto_wish_prompt", True)):
        return None
    from server import wishes
    _log("No usable result — asking whether to add this release to the wishes list.")
    with _lock:
        _job["state"] = "confirm"
        _job["stage"] = "No usable results — add to wishes?"
        _job["confirm"] = {
            "reason": "no_results",
            "waited": int(waited),
            "queries": list(queries),
            "formats": [],
            "candidates": [],
        }
    _confirm_event.wait()  # released by confirm() or cancel()
    _confirm_event.clear()
    with _lock:
        _job["confirm"] = None
        _job["state"] = "running"
        accepted = _confirm_answer["accept"]
    if not accepted:
        return None
    wish = wishes.add_wish(
        release.get("id"), title=release.get("title") or "",
        artist=((release.get("artists") or [{}])[0].get("name", "")),
        year=str(release.get("date") or "")[:4],
        queries=list(queries))
    # add_wish is idempotent: a wish from an earlier attempt comes back
    # unchanged, so refresh its queries — the worker hunts with the stored
    # set, and stale ones would silently drop this job's better templates.
    try:
        wishes.update_wish(wish["id"], {"queries": list(queries)})
    except Exception:
        pass
    _log(f"Added to wishes (#{wish['id']}) — the worker keeps searching for this "
         f"release in the background with the same queries, so nothing is lost "
         f"by parking this job.")
    # Nothing landed in the library: report the wish, never a download. The
    # import path's result keys are kept (as None/0) so a caller reading
    # result["album_path"] sees one shape for every finished job.
    return {"wished": True, "wish_id": wish["id"],
            "album_path": None, "staging_path": None,
            "imported": 0, "organized": 0, "organize_error": None}


def _run(release_mbid=None, release=None, queries=None, username=None,
         target_dir=None, confirm_lossy=False, kind=None, mode=None):
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
        if release is None and (kind or "") not in ("", "release"):
            # The HTTP route queued this ID before MusicBrainz resolved it
            # (it refuses to hold a request on a lookup): resolve what it
            # could not wait for — a release group to its edition(s), or a
            # whole artist to one best release per release group. The extra
            # editions go back on the queue and run after this job.
            _log("Resolving the MusicBrainz release(s) to import…")
            rows, skipped = intg.auto_import_targets(release_mbid, kind, mode or "best")
            for s in skipped[:5]:
                _log(f"  - skipped {s.get('mbid')}: {s.get('reason')}")
            if not rows:
                raise RuntimeError(skipped[0]["reason"] if skipped else
                                   "MusicBrainz resolved nothing to import")
            for extra in rows[1:]:
                enqueue(release_mbid=extra["mbid"])
            if len(rows) > 1:
                _log(f"  {len(rows) - 1} more release(s) queued behind this one")
            release_mbid = rows[0]["mbid"]
        if release is None:
            _log("Looking up the MusicBrainz release…")
            release, rid = intg.resolve_release(release_mbid)
            if not release or not rid:
                raise RuntimeError(
                    f"MusicBrainz has no importable release for {release_mbid} "
                    "— it is neither a release nor a release group, or every "
                    "edition of it is ineligible for auto-import")
            release_mbid = rid
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
        # The queries this job searched with and the window it waited out are
        # reported by the wish prompt, and the candidate loop below can reach
        # both on EITHER path (a browsed-folder grab searches with nothing).
        queries_built, search_wait = [], 0
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
                raise RuntimeError(
                    "Nothing to search by: this release has neither a catalog "
                    "number nor an artist + title that a query template can "
                    "use. Add one in Settings → Auto-import.")
            # How long a search may run, and how many peers it needs before it
            # is worth scoring. The window is QUIET time (slskd ends a search
            # when the network stops answering) and the response limit ends it
            # outright once that many peers have replied — without the limit a
            # popular album never goes quiet, so nothing is readable until the
            # whole ceiling has elapsed. A usable candidate ends the wait even
            # sooner, so both values are ceilings, never floors.
            search_wait = int(cfg.get("soulseek_auto_search_wait", 10) or 10)
            response_limit = int(cfg.get("soulseek_auto_response_limit", 15) or 15)
            # Every template goes out at once and the merged responses are
            # scored on each tick: the first complete+lossless folder ends the
            # wait for ALL of them, so a weak template never costs another full
            # window. `usable` is the same test the loop below applies.
            def _usable(merged):
                return [c for c in find_candidates(merged, release, cfg)
                        if c["complete"] and c["lossless"]]

            _log(f"Searching Soulseek with {len(queries_built)} query template(s) in "
                 f"parallel: “{'” · “'.join(queries_built)}” … (a good folder ends "
                 f"the search at once, otherwise {response_limit} responses or "
                 f"{int(search_wait + _SEARCH_GRACE_S)}s)")
            results, search_failed, skipped = _search_queries(
                slsk, queries_built, search_wait, usable=_usable,
                response_limit=response_limit)
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

            # ---- ONE broader query, run after the configured one(s) -----------
            # A release whose configured templates name it too precisely (a CD
            # searched by catalog number, whose Soulseek copies are all WEB
            # rips that never carry one) answered with hundreds of files and no
            # usable folder. This is not a return of the removed templates: it
            # fires only as a SECOND attempt, sequentially, when the first
            # produced nothing usable, and never runs alongside it (a competing
            # window would only cost the user another search).
            all_responses = list(responses)
            broad = [q for q in _build_from_templates(["artist album year"], release)
                     if q not in queries_built]
            if not best and broad:
                _log(f"No usable folder from the configured template(s) — one broader "
                     f"search: “{'” · “'.join(broad)}”")
                fb_results, fb_failed, _fb_skipped = _search_queries(
                    slsk, broad, search_wait, response_limit=response_limit)
                _job_search_done()
                for line in fb_failed:
                    _log(f"  ✕ {line}")
                fb_responses = [f for _q, res in fb_results
                                for f in (res.get("responses") or [])]
                all_responses += fb_responses
                candidates.extend(find_candidates(fb_responses, release, cfg))
                candidates.sort(key=_rank)
                best = [c for c in candidates if c["complete"] and c["lossless"]]
                _log(f"  broader query: {len(fb_responses)} result file(s), "
                     f"{len(candidates)} candidate folder(s) in total")

            # ---- a complete lossless copy that simply has no rip log ----------
            # (CD only.) The CD gate needs one .log + one .cue per disc, so a
            # WEB rip of a CD is dropped before it is even a candidate and the
            # release could never import at all. The folders are scored a second
            # time as the Digital Media release they would become — the same
            # view the rest of the job takes if the user says yes — and only
            # when NO gate-passing candidate exists, so a real rip with a log
            # always keeps the log gate.
            loose = []
            if is_cd and not best:
                loose = [c for c in find_candidates(
                    all_responses, {**release, "medium_formats": ["Digital Media"]}, cfg)
                    if c["complete"] and c["lossless"]]
                loose.sort(key=_rank)
            declined = False
            if loose and confirm_lossy:
                with _lock:
                    _job["state"] = "confirm"
                    _job["stage"] = "No rip log — import as Digital Media?"
                    _job["confirm"] = {
                        "reason": "no_logs",
                        "media": "Digital Media",
                        "candidates": [{
                            "username": c["username"],
                            "dir": c["dir"],
                            "format": os.path.splitext(c["audio"][0]["file"])[1].lstrip(".").upper()
                                      if c["audio"] else "",
                            "matched": c["matched"],
                            "expected": c["expected"],
                            "size": c["total_size"],
                            "score": c["score"],
                        } for c in loose[:5]],
                    }
                _log(f"No CD rip with a .log/.cue found — {len(loose)} complete "
                     f"lossless folder(s) hold every track but no log to grade. "
                     f"Waiting for your go-ahead to take one as Digital Media.")
                _confirm_event.wait()  # released by confirm() or cancel()
                _confirm_event.clear()
                with _lock:
                    _job["confirm"] = None
                    _job["state"] = "running"
                    accepted = _confirm_answer["accept"]
                if _cancelled():
                    return _finish("cancelled")
                if accepted:
                    _log("no CD rip with logs found — downloading the complete "
                         "lossless copy and stamping it as Digital Media")
                    # The one line that changes what this job believes it is
                    # importing: everything downstream reads medium_formats —
                    # _verify_album's CD log/CRC audit, _stamp_media's medium and
                    # _import's — so the switch happens BEFORE verify/import and
                    # nowhere later. Nothing else in the release dict is touched:
                    # the MusicBrainz ids, country and catalog number stay, so the
                    # album still carries its release identity.
                    release = {**release, "medium_formats": ["Digital Media"]}
                    is_cd = False
                    # The WHOLE offered set stays the working list, best first
                    # (already `_rank`-sorted): the accepted peer can still
                    # stall, fail to queue or be rejected, and a live run that
                    # kept only `loose[0]` ended with "Every candidate was
                    # rejected (1 attempt(s))" while three usable peers sat
                    # untouched. The next candidate is simply the next in this
                    # list; nothing is re-searched and no second prompt fires.
                    candidates = list(loose)
                else:
                    declined = True

            if not candidates and search_failed:
                raise RuntimeError("; ".join(search_failed[:3]))
            if not candidates or declined:
                # slskd answered, but no folder held the whole album. That used
                # to be a hard stop ("No candidate folder contained every
                # track"), which threw away a search the user just waited out
                # for a release that is merely rare right now. On the
                # interactive path the job instead parks and offers to add the
                # release to the wishes list (see _ask_to_wish): the background
                # worker then keeps searching for it with the very queries this
                # job used, so nothing is lost by parking the job — the user
                # only decides whether it is worth watching for.
                #
                # How long a search runs is decided in ONE place —
                # soulseek_auto_search_wait (the requested window) plus
                # _SEARCH_GRACE_S (the tail slskd needs to hand back responses
                # once a search ended) — and this prompt fires exactly when that
                # window has just ended with nothing usable. `waited` reports
                # that same cap to the UI instead of a second, competing timer.
                wished = _ask_to_wish(release, queries_built,
                                      search_wait + _SEARCH_GRACE_S, cfg, confirm_lossy)
                if _cancelled():
                    return _finish("cancelled")
                if wished:
                    return _finish("done", wished)
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
        # EVERY candidate the search scored is tried, best first: each rejected
        # peer costs only its own attempt (its partials are cleaned up), while
        # stopping early throws away copies that would have verified. The job
        # ends when a candidate imports, or when the list runs out.
        for cand in candidates:
            if _cancelled():
                return _finish("cancelled")
            uname, folder = cand["username"], cand["dir"]
            _log(f"Candidate: {uname} · …{folder[-60:]} ({cand['matched']}/{cand['expected']} tracks matched)")

            # --- CD: the .log is queued FIRST, the album only after it passes --
            # Queueing the album in the same call as the log meant a peer whose
            # log grades below the bar still sent (and was paid for) album
            # bytes, which then had to be swept as orphans. So the gate's own
            # files go out alone, and the rest of `wanted` is requested only
            # once the log has passed (see the gate below). A non-CD candidate
            # has no gate: everything goes out in this one call.
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
            wanted_logs = ([{"filename": f["file"], "size": f["size"]}
                            for f in cand["logs"]]
                           if is_cd and cand["logs"] else [])
            log_names = {w["filename"] for w in wanted_logs}
            album_wanted = [w for w in wanted if w["filename"] not in log_names]
            try:
                slsk.enqueue_download(uname, wanted_logs or wanted)
            except Exception as e:
                _reject(uname, folder, f"download failed to queue: {e}")
                # slskd refuses a folder one file at a time: the peers that DID
                # accept are already queued, so the attempt is dropped the same
                # way a rejected one is instead of leaving those bytes behind.
                _drop_candidate(slsk, ddir, uname, wanted)
                continue

            # The job is DOWNLOADING from this point, so say so immediately:
            # the search bar is cleared and a transfer block is published
            # before the first poll, so the UI switches to "downloading" the
            # moment the peer has the files instead of sitting on the finished
            # search until a byte count shows up. On a CD only the log is in
            # flight yet, so the block shows the log alone.
            _job_search_done()
            _job_progress(_progress_snapshot(uname, wanted_logs or wanted,
                                             set(), [], "queued"))

            # --- the log is the cheap gate (CD) --------------------------------
            if wanted_logs:
                # exactly one log per disc (candidate selection), so this wait
                # is over the wanted logs alone: it returns the moment they are
                # all local instead of waiting out a junk extra log's timeout.
                # They are also the ONLY transfers queued so far, so the block
                # shows them and nothing else.
                _log("Downloading .log file(s) first for a quality check…")
                got_logs = _wait_for_files(slsk, ddir, uname, wanted_logs,
                                           timeout_s=_LOG_TIMEOUT_S,
                                           cancel_check=_cancelled, phase="logging")
                if len(got_logs) < len(wanted_logs):
                    _reject(uname, folder,
                            f"{len(got_logs)} of {len(wanted_logs)} log(s) arrived "
                            f"within {int(_LOG_TIMEOUT_S)}s — trying the next candidate")
                    # The log is the only thing this candidate queued, but the
                    # WHOLE wanted set is dropped: a partial log, a transfer
                    # record or a temp file the peer is still flushing all live
                    # under this candidate's own album tree, and the next
                    # candidate downloaded them again.
                    _drop_candidate(slsk, ddir, uname, wanted)
                    continue
                if _cancelled():
                    return _finish("cancelled")
                _log("Grading rip log(s) with Logchecker…")
                scores = _score_logs(list(got_logs.values()), cfg)
                good, bad = [], []
                for p, score, state, detail in scores:
                    _log(f"  {os.path.basename(p)}: score "
                         f"{score if score is not None else '?'}, checksum {state or '?'}")
                    if _log_passes(score, state, min_score):
                        good.append(os.path.basename(p))
                    else:
                        bad.append((os.path.basename(p), score, state))
                # A folder can hold a second, junk log for the same disc (and a
                # manual/browse pick can hold several): the album rides on the
                # log that grades well, the rest are reported and ignored. A
                # checksum MISMATCH still costs the album its evidence — with
                # no good log left, the candidate is rejected and the log that
                # just landed is dropped again.
                if not good:
                    _reject(uname, folder,
                            "log rejected: " + "; ".join(
                                _log_fail_reason(n, s, st, min_score)
                                for n, s, st in bad)
                            + " (Settings → Auto-import); trying the next candidate")
                    _drop_candidate(slsk, ddir, uname, wanted)
                    continue
                for n, s, st in bad:
                    _log(f"  ignoring {_log_fail_reason(n, s, st, min_score)}")
                _log(f"{len(good)} log(s) pass — downloading the full album…")

                # The log cleared the bar, so the album has earned its bytes:
                # it is requested NOW, in a second call, and only its own files
                # — the logs are already local. A queue failure here is a
                # rejected candidate like any other, reason named as such.
                try:
                    slsk.enqueue_download(uname, album_wanted)
                except Exception as e:
                    _reject(uname, folder, f"album failed to queue: {e}")
                    _drop_candidate(slsk, ddir, uname, wanted)
                    continue

            # --- the album (queued above: the log first, the album after it) ----
            est_timeout = _est_timeout(cand)
            _log(f"Downloading {len(wanted)} file(s) "
                 f"({cand['total_size'] / (1024 * 1024):.0f} MB, up to {est_timeout}s)…")
            got = _wait_for_files(slsk, ddir, uname, album_wanted or wanted,
                                  timeout_s=est_timeout, cancel_check=_cancelled,
                                  queue_budget_s=_queue_budget(cand))
            if wanted_logs:
                # The logs are already on disk with terminal transfers, and the
                # album wait above did not cover them (they went out in the
                # first call, not the second): they are folded back in so the
                # album root, verification and import still see the whole
                # candidate.
                got = {**got, **got_logs}
            if _cancelled():
                return _finish("cancelled")
            if len(got) < len(wanted):
                missing = [w["filename"] for w in wanted if w["filename"] not in got]
                _reject(uname, folder, f"download incomplete: {len(missing)} file(s) missing/timed out")
                # the files that DID arrive are this candidate's, and the rest of
                # its transfers are still queued: both are dropped together
                _drop_candidate(slsk, ddir, uname, wanted)
                continue

            # --- stage 3: audit -------------------------------------------------
            # The album root on disk is the highest directory below the
            # download dir holding EVERY file of this candidate: the pinned
            # `<ddir>/<user>/<batch id>/<remote path>/…` layout lands there,
            # and so do the older `<ddir>/<leaf>/…` /
            # `<ddir>/<user>/<leaf>/…` ones; a multi-disc tree split over one
            # parent (…/CD1 + …/CD2) verifies as the union instead of
            # half-importing.
            resolved = list(got.values())
            local_root = _local_album_root(ddir, resolved)
            if not local_root:
                _reject(uname, folder, "downloaded files did not land in one album "
                                       "folder under the download dir")
                _drop_candidate(slsk, ddir, uname, wanted)
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
                # the album root is this candidate's own (resolved from its own
                # files below the download dir) — a junk file it also dropped
                # there goes with it, and the sweep still runs afterwards
                _drop_candidate(slsk, ddir, uname, wanted, remove_root=local_root)
                continue
            _log("Verification passed — importing into the library…")

            # --- stage 4: import -------------------------------------------------
            result = _import(local_root, release, cfg, is_cd)
            _finish("done", result)
            return

        # ---- every candidate was tried ---------------------------------------
        # Five peers, each one offline or never delivering its .log, and the job
        # died on this bare error with the release forgotten again. Same offer
        # as the no-usable-folder dead end (_ask_to_wish): the release goes into
        # the wish list — with the queries this job already searched with — and
        # the background worker keeps looking for it.
        wished = _ask_to_wish(release, queries_built,
                              search_wait + _SEARCH_GRACE_S, cfg, confirm_lossy)
        if _cancelled():
            return _finish("cancelled")
        if wished:
            _finish("done", wished)
            return
        raise RuntimeError("Every candidate was rejected "
                           f"({len(_job['attempts'])} attempt(s) — see the log).")

    except intg.MusicBrainzError as e:
        # MusicBrainz itself is down or rate-limiting — a per-item failure with
        # a reason the queue/UI shows ("MusicBrainz is busy…"), not a wedged
        # job: _finish frees the single-job slot so the next release runs. No
        # traceback: the reason says everything, and this is an outage, not a
        # bug in here.
        _log(f"MusicBrainz did not answer: {e}")
        _finish("error", {"error": f"MusicBrainz: {e}"})
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


def _cleanup_partial(ddir, local_files, remove_root=None):
    """Remove rejected partials so they don't linger in the download dir.

    Returns how many files (and, when `remove_root` went, the directory) it
    removed — 0 is what tells the caller's sweep loop the candidate is gone."""
    removed = 0
    for p in local_files:
        try:
            if os.path.isfile(p):
                os.remove(p)
                removed += 1
        except OSError:
            pass
    if remove_root:
        try:
            if os.path.isdir(remove_root) and _inside(remove_root, ddir):
                shutil.rmtree(remove_root, ignore_errors=True)
                removed += 1
        except Exception:
            pass
    # the album/batch/user shells the deleted files leave behind — slskd never
    # removes a directory it created (rmdir only takes the empty ones, so a
    # sibling download keeps its own)
    from server.soulseek import prune_download_dirs
    prune_download_dirs(ddir)
    return removed


def _start_import_chain(album_dir, cfg):
    """Stage the freshly imported album — links, metadata, covers — and stop.

    Delegates to ``server.imports.finish_album(..., defer_tagging=True)``: the
    album was moved, AcoustID-checked, converted, MB/MEDIA-stamped and named
    by ``_import`` before this runs, so all that is left is the identity
    stamping, the artist metadata and the cover art. The tag-writing chain
    (auto tagging, lyrics, DR & ReplayGain, grade, format all) is deliberately
    NOT run — an unattended download must not rewrite the user's tags, that is
    what the wizard and the menu actions are for.

    It runs in a daemon thread (a job's result is where the album is, not
    twenty minutes of re-encoding) and is failure-tolerant by design: a
    failure is written into the job log and the import still succeeds — the
    album is already in the library.

    ``_account_metadata`` runs after it: the artist image and the artist/album
    descriptions are the three things an unattended import has to account for,
    so the log always names each one's outcome even when the chain's own
    metadata step was switched off or staged them for review.
    """
    from server import imports

    def chain():
        try:
            result = imports.finish_album(album_dir, cfg, defer_tagging=True)
            for err in result["errors"]:
                _log("  ! " + err)
            _log("Album staged and placed: links, metadata and cover art. "
                 "Tagging (auto tagging, lyrics, DR, grade, format all) is "
                 "left to the wizard / menu actions.")
        except Exception:
            traceback.print_exc()
            _log("Import pipeline crashed: "
                 f"{traceback.format_exc().strip().splitlines()[-1]}")
        _account_metadata(album_dir, cfg)

    _log("Staging the imported album in the background "
         "(RateYourMusic links, metadata, cover art).")
    threading.Thread(target=chain, name="mlo-soulseek-import-chain",
                     daemon=True).start()


def _account_metadata(album_dir, cfg):
    """Find and account for the album's artist image and descriptions.

    One line per item — fetched from where, already there, switched off, or
    nothing found — the same step (and the same wording) the import menu
    drives, so an unattended import states what it did about all three
    instead of leaving them a silent gap the artist page later shows as
    missing. Never fatal: a provider being down is not an import failure.

    With the metadata step switched off, or set to stage candidates for
    review, the user's choice is reported instead of overruled — writing here
    would be exactly the automatic behaviour they turned off.
    """
    if not cfg.get("metadata_auto_fetch", True):
        _log("Artist image / descriptions: the metadata step is off "
             "(Settings → Metadata), so none were fetched.")
        return
    if cfg.get("metadata_review", False):
        _log("Artist image / descriptions: metadata review is on — candidates "
             "were staged for you instead of written. Apply them from the "
             "album's page.")
        return
    from server.api_discovery import ensure_artist_album_metadata, meta_line
    try:
        result = ensure_artist_album_metadata(album_dir, cfg,
                                              progress=_metadata_progress)
        for item, res in result.items():
            _log("  " + meta_line(item, res))
    except Exception:
        traceback.print_exc()
        _log("Artist image / descriptions: the metadata step failed — "
             "the album itself is fine.")


def _metadata_progress(done, total, label):
    """Live stage text while the metadata step is on the network.

    Sets the job's stage only — the per-item outcome is what the log carries,
    so the pending/finished pair would just double every line."""
    with _lock:
        _job["stage"] = f"Artist image / descriptions {done}/{total}: {label}"


def _verify_acoustid(album_dir, release, cfg):
    """Check the downloaded audio against the release the job searched for.

    AcoustID fingerprints what actually arrived and names its release group;
    a different pressing/edition downloads fine, passes the log/CRC gate and
    is still not the release the tags claim. That is a WARNING in the job log
    (``server.imports.release_group_mismatch``), never a rejected download —
    the user can see it on the Soulseek page and decide.
    """
    want = str(release.get("release_group_id") or "").strip()
    if not want or not cfg.get("import_acoustid"):
        return
    try:
        from mlo import acoustid
        from server import imports

        if not acoustid.available(cfg):
            _log(f"AcoustID check skipped: {acoustid.acoustid_enabled_note(cfg)}")
            return
        result = imports.acoustid_match([album_dir], cfg)
        row = (result.get("albums") or [{}])[0]
        warning = imports.release_group_mismatch(row, want)
        if warning:
            _log("WARNING: " + warning + " — the download may be a different "
                 "pressing or edition.")
        elif row.get("release_group_id"):
            _log(f"AcoustID confirmed the release group "
                 f"({row.get('matched')}/{row.get('total')} tracks).")
        else:
            _log("AcoustID check: no release group matched the audio "
                 "(inconclusive, not a rejection).")
    except Exception:
        traceback.print_exc()


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

    # AcoustID verification of what actually arrived, against the release this
    # job searched for: a warning in the job log, never a rejection.
    _verify_acoustid(dest, release, cfg)

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

    _start_import_chain(album_path, cfg)
    return {"album_path": album_path, "imported": True,
            "staging_path": dest, "organized": organized,
            "organize_error": organize_error}
