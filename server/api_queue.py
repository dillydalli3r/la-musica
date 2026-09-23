"""ONE queue view for the whole download pipeline.

Every way a release gets into the library — a wish from MusicBrainz, an
"everything by this artist" bulk run, a folder grabbed off the Soulseek page, a
finished download waiting to be imported — used to be visible only on the panel
that started it: the wish list showed wishes, the Auto-import panel showed the
job, the Downloads tab showed transfers, and nothing tied them together. This
module builds the single list the Soulseek page renders: what is waiting, what
is happening now (with per-item progress), what finished (with its import
outcome) and what failed (with the reason).

It is a VIEW, not a second queue: the rows are read out of the registries that
already exist (server.wishes, server.soulseek_auto, server.import_queue,
server.import_autonomy and the download folder) and acting on a row calls the
primitive that owns it — cancelling calls the owner's cancel, retrying re-arms
the row the retry policy declared terminal (`POST /api/queue/retry`), and a
prompt row's own two actions are the wizard (a link it carries) and
`POST /api/import/prompts/dismiss`.

A row says what the user has to do next, because "no acquisition can end
silently" is a property of this list as much as of the notifications: what
failed and why (`reason`), what the state means (`note`), what is missing
(`missing`), which action the row offers (`action`/`action_link`) and whether
that action can be pressed at all (`retryable`, `dismissable`).

ONE album is ONE row, and the row's section is its CURRENT state: a wish and
the job filling it are one row while the job runs, and a stalled album's prompt
(needs you) supersedes the settled row that reported the same folder — an album
never reads as both "Needs you" and "Completed", and the history returns, still
clearable, once the prompt is answered.

The stage each row carries is the vocabulary in server.soulseek_auto.STAGES —
`queued`, `searching`, `downloading`, `verifying`, `importing`, `completed`,
`failed`, `needs_attention` — so a wish from the MusicBrainz queue and a job
started from the Soulseek search bar are described in the same words. ONE stage
is not in that list (`pending_albums.STAGE_RESOLVING`): an "Add to library" that
answered before MusicBrainz did is waiting for THE SERVER, not for the network,
and calling it `queued` would say a download was on its way when nothing has
been searched for yet.
"""
import os
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mlo.config import load_config
from server import pending_albums, soulseek_auto, wishes

router = APIRouter()

# Every row carries a `release` block — the release identity in
# server.wishes.RELEASE_KEYS (catalog number, medium, country, date, track
# count, disambiguation, status …), on the rows that know one and EMPTY on the
# rows that cannot (a prompt about an album already in the library, a finished
# download waiting in the folder). A row is never missing the block and never
# invents a fact: the UI renders what is there and shows nothing where there is
# nothing. What fills it, and why no row ever costs a MusicBrainz request of
# its own: server/wishes' release identity section.
#
# `clearable` is the OTHER per-row verdict: whether this row may be taken off
# the list (POST /api/queue/clear). Clearing is for rows whose work is OVER —
# an imported wish, a failed job, a wish nothing was found for — and never for
# one that is still going: a running row is CANCELLED, which is a different
# thing that happens to the item, not to the list. Two rows the user can still
# act on are deliberately NOT clearable here: a finished download waiting in
# the download folder (its clear would delete the bytes the user is about to
# import — the staging card and the Downloads tab own that, and they say so),
# and an import prompt (its own "Mark complete" dismisses it).


def _clock(ts):
    """A retry deadline as the clock the user reads (local time, minutes)."""
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except Exception:
        return "later"

# Sections, in the order the page shows them: what waits, what runs, what is
# still being watched for in the background, what needs a human, what is done,
# what went wrong. `needs_attention` is its own section rather than a flavour of
# failed — an item parked on a decision (a lossy-only copy, a release with no
# usable folder) has not failed and hiding it under failures is how a queue ends
# up with rows nobody ever answers. `background` is its own section for the other
# reason: the release has asked every ranked candidate it may (spec R150-R153)
# and is STILL being searched on the worker's own ticks, so it belongs neither
# with the releases being walked right now nor with the ones waiting on a person.
SECTIONS = ("queued", "in_progress", "background", "needs_attention",
            "completed", "failed")


def _section_of(stage):
    """Which section a row's STAGES name belongs in.

    `pending_albums.STAGE_RESOLVING` belongs with the waiting rows for the same
    reason `searching` does: work is running for it right now, and it is not
    yet doing anything the network would see.
    """
    if stage in ("queued", "searching", pending_albums.STAGE_RESOLVING):
        return "queued"
    if stage in ("downloading", "verifying", "importing"):
        return "in_progress"
    if stage == "background":
        return "background"
    if stage == "needs_attention":
        return "needs_attention"
    if stage == "failed":
        return "failed"
    return "completed"


def _wish_source(wish):
    """Who put this wish in the queue, as (key, label).

    `source` is the column the MusicBrainz "Add to library" path writes
    ("musicbrainz" for a framework album, "soulseek" for a manual save, "auto"
    for the auto-importer's own "add to wishes?" offer). A row saved before
    that column existed has none, and a wish whose release came off a
    MusicBrainz page is still a MusicBrainz wish — the release id is the tell."""
    key = str(wish.get("source") or "").strip().lower()
    if key not in ("musicbrainz", "soulseek", "auto"):
        key = "musicbrainz" if wish.get("release_mbid") else "soulseek"
    return key, {"musicbrainz": "MusicBrainz", "soulseek": "Soulseek",
                 "auto": "Auto-import"}[key]


def _resolving(wish):
    """Whether this wish's framework album is still having its MusicBrainz
    identity resolved (`pending_albums.is_resolving`: the marker says so, and
    it is fresh enough to believe)."""
    path = str(wish.get("album_path") or "")
    return bool(path) and pending_albums.is_resolving(path)


def _progress_from_search(search):
    """The queued/searching row's own progress: what the network has answered.

    No clock: a search is a ceiling that a good candidate ends early, so a
    percentage would be a lie. The counts are what slskd really reported."""
    if not search:
        return None
    return {
        "text": f"{int(search.get('responses') or 0)} peer(s), "
                f"{int(search.get('files') or 0)} file(s)",
        "done": int(search.get("responses") or 0),
        "total": 0,
        "percent": None,
    }


def _progress_from_download(progress, stage):
    """The in-progress row's progress block, byte-weighted like the panel's."""
    if not progress:
        return None
    total = int(progress.get("size") or 0)
    done = int(progress.get("bytes") or 0)
    return {
        "text": str(progress.get("dir") or progress.get("phase") or stage),
        "done": done,
        "total": total,
        "percent": progress.get("percent"),
        "files_done": int(progress.get("files_done") or 0),
        "files_total": int(progress.get("files_total") or 0),
        "speed": progress.get("speed"),
        "eta_s": progress.get("eta_s"),
    }


def _job_row(job, wish_id=None):
    """One auto-import job as a queue row."""
    stage = soulseek_auto.job_stage(job)
    if stage == "cancelled":
        return None                       # the user's own cancel: nothing to show
    release = job.get("release") or {}
    artist = str(release.get("artist") or "")
    title = str(release.get("title") or "")
    label = str(job.get("label") or "")
    if not artist and not title and label:
        artist, _, title = label.partition(" — ")
    result = job.get("result") or {}
    leftovers = [str(p) for p in (result.get("leftovers") or [])]
    action, action_link = "", ""
    if stage == "needs_attention":
        reason = str((job.get("confirm") or {}).get("reason") or "")
        note = {"lossy_only": "Only lossy copies found — waiting for your go-ahead",
                "no_logs": "No rip log — waiting for your go-ahead",
                "no_results": "Nothing usable found — add it to the wishes?"}.get(
                    reason, "Waiting for your answer")
        progress = None
        # The question is answered by accept/decline on the auto-import tab, so
        # the row points there rather than pretending the queue can decide it.
        action, action_link = "answer", "/soulseek?tab=auto"
    elif stage in ("downloading", "verifying", "importing"):
        progress = _progress_from_download(job.get("progress"), stage)
        note = ""
    else:
        progress = _progress_from_search(job.get("search"))
        # With nothing in flight the job's own stage line is the only thing
        # that says WHY it is still queued ("Waiting for the album folder …").
        note = "" if progress else str(job.get("stage") or "")
    if stage == "failed":
        # Every give-up path ends here with its reason AND its classification
        # (server/wishes.outcome_of): "not found" is not going to be retried by
        # itself, everything else is worth another press once the cause is
        # fixed — and partial bytes the sweep could not free are named instead
        # of being left for the user to find in the download folder.
        note = ("Nothing found — retry it by hand when the network has it"
                if str(result.get("outcome") or "") == "not_found"
                else "Gave up — retry it by hand once the cause is fixed")
        note += (" · still in the download folder: "
                 + ", ".join(os.path.basename(p) for p in leftovers[:3])
                 + (f" (+{len(leftovers) - 3} more)" if len(leftovers) > 3 else "")
                 if leftovers else "")
    if stage == "completed":
        # The import outcome, in the row: "in the download folder" is a
        # different thing from "in your library" and the queue says which.
        if result.get("imported"):
            note = ("Imported into the library"
                    + ("" if result.get("organized") is not False
                       else " (the naming script failed — see the job log)"))
        elif result.get("wished"):
            note = "Nothing was found — it is on the queue to keep looking"
        else:
            note = "Downloaded — waiting in the download folder to be imported"
    return {
        "id": f"job:{job.get('id')}",
        "kind": "job",
        "job_id": job.get("id"),
        "wish_id": wish_id if wish_id is not None else job.get("wish_id"),
        "stage": stage,
        "source_key": str(job.get("source") or "soulseek"),
        "source": {"musicbrainz": "MusicBrainz", "soulseek": "Soulseek",
                   "auto": "Auto-import"}.get(str(job.get("source") or ""), "Soulseek"),
        "title": title,
        "artist": artist,
        "release_mbid": str(release.get("id") or ""),
        "album_path": str(result.get("album_path") or result.get("staging_path") or ""),
        "progress": progress,
        "reason": str(result.get("error") or (job.get("stage") if stage == "failed" else "") or ""),
        "note": note,
        # What a row needs to be ACTIONABLE (see server/api_queue's docstring):
        # what went wrong, what the manual action is, and where it happens.
        "outcome": str(result.get("outcome") or ""),
        "leftovers": leftovers,
        "action": action,
        "action_link": action_link,
        "retryable": stage == "failed" and bool(str(release.get("id") or "")),
        # WHICH edition this job is fetching, straight off the job (no per-row
        # lookup — this route is polled every few seconds): the catalogue
        # number and medium that identify the pressing, where and when it came
        # out, how much it carries, and the edition's own disambiguation and
        # MusicBrainz status. `release_identity` reads the compact summary the
        # job carries as well as a full MusicBrainz payload, so the shape is
        # the ONE documented block (server.wishes.RELEASE_KEYS) either way.
        "release": wishes.release_identity(release, str(release.get("id") or "")),
        "created_at": float(job.get("started_at") or 0),
        "updated_at": float(job.get("ended_at") or job.get("started_at") or 0),
        "cancelable": stage in ("queued", "searching", "downloading", "verifying",
                                "importing", "needs_attention"),
        # A SETTLED job is history the user can take off the list (the album it
        # imported is in the library; a running one is cancelled, not cleared).
        "clearable": stage in ("completed", "failed"),
        "log_tail": [l.get("msg") for l in (job.get("log") or [])[-4:]],
    }


def _no_release():
    """The EMPTY identity block, for a row with no release to name (a stalled
    album already in the library, a finished download in the folder): every
    documented key with nothing in it, so the UI never has a field to guard and
    never has a fact to guess."""
    return wishes.release_identity({})


def _album_key(path):
    """One album's identity inside the payload, for the rows that name a
    folder: the same normalization the prompt table keys its entries by, so a
    row and a prompt about one album compare equal however each side spelled
    the path."""
    return os.path.normpath(str(path or "")).replace("\\", "/").lower()


def _wish_rows(wishes_list, jobs_by_wish, cfg=None):
    """Wish rows, merged with the job filling them WHILE THAT JOB RUNS.

    A wish being downloaded is ONE row with the download's stage and progress —
    not a "searching" wish and a "downloading" job side by side, which is how a
    single album looked like two items doing unrelated things.

    A job that has SETTLED is history, not state: the wish's own status is what
    the store holds now. Letting the finished job's stage win put a wish whose
    searches found nothing (terminal, waiting for the user) in the failed
    section as if the album had been given up on, and a wish that was already
    re-armed for its next attempt read as still failing — the row contradicted
    the store it was built from. The settled job still CLAIMS its id (so it
    does not draw a second row of its own) and still lends its log tail."""
    rows = []
    for w in wishes_list:
        status = str(w.get("status") or "")
        stage = {"wanted": "queued", "searching": "queued", "imported": "completed",
                 "failed": "failed", "available": "needs_attention",
                 # A wish whose searches found nothing is TERMINAL (see
                 # server/wishes' retry policy): it stops by itself, so it is
                 # not "failed" — it needs the user, either to retry it or to
                 # fill it by hand. needs_attention is exactly that section.
                 "not_found": "needs_attention",
                 # A spent WALK is not "needs you" at all (spec R153): the
                 # release is still searched for on the worker's own ticks, it
                 # just asked every ranked candidate it may, so it has its own
                 # section instead of reading as a search that gave up.
                 "background": "background"}.get(status, "queued")
        job = jobs_by_wish.get(w["id"])
        live = job is not None and job["stage"] not in ("completed", "failed")
        progress, note, reason = None, "", str(w.get("last_error") or "")
        if live:
            stage = job["stage"]
            progress = job["progress"]
            note = job["note"]
            if job["reason"]:
                reason = job["reason"]
        if stage == "queued" and status == "searching":
            stage = "searching"
        if stage == "completed" and w.get("album_path"):
            note = note or "Imported into the library"
        if stage == "needs_attention" and not note:
            note = ("Nothing found — not searched again unless you retry it"
                    if status == "not_found" else (reason or "Needs a manual import"))
        retry_at = float(w.get("retry_at") or 0)
        if stage == "queued" and retry_at > time.time():
            note = note or f"Retrying after a failure at {_clock(retry_at)}"
        elif stage == "queued" and not note and wishes.outcome_of(reason) == "not_found":
            # A search that came back empty does NOT end a wish — the shipped
            # policy keeps looking (0 = never give up: the network is not a
            # fixed catalogue) — so the row says it is still being searched and
            # when the next look is, instead of leaving a reason line that
            # reads like a give-up the user has to go and check.
            note = ("Nothing usable so far — still looking, next search around "
                    + _clock(wishes.due_at(w, cfg or {})))
        # The store's own verdict, read once: whether the worker will search
        # this wish again on its own is what decides every action the row has
        # (see server/wishes' retry policy). The WALK does not change it — a
        # background wish is still searched — so it is read before the row is
        # assembled and used by both the wording below and `clearable`.
        terminal = bool(wishes.is_terminal(w, cfg or {}))
        # WHICH ranked candidate this row is asking for (spec R150-R154): the
        # walk's own block out of the store, so the page re-derives no position
        # and the row says where a long search is instead of looking stuck. ONE
        # row per release whatever the walk does — the candidates are asked one
        # at a time INSIDE this wish (`wishes_worker._run_one`), so there is
        # never a row per candidate to merge away, and never more than one job
        # for the wish at a time.
        walk = wishes.candidate_state(w, cfg)
        if walk:
            here = walk["title"] or walk["mbid"]
            if stage == "background":
                # The owner's own wording for the resting phase — the position,
                # the fact that nothing landed yet, and when the next pass looks
                # again. The section it sits in says the rest ("Background").
                note = " · ".join(x for x in (
                    f"tried {walk['index'] + 1} of {walk['total']}",
                    note or "no usable copy yet",
                    "searched again automatically around "
                    + _clock(wishes.due_at(w, cfg or {}))) if x)
            else:
                # A live job is asking for THIS candidate right now; a row
                # between attempts is about to ask the BEST one (the walk
                # restarts at the top every pass, spec R150), so it says which
                # candidate the next search starts with rather than naming the
                # one the last attempt happened to end on.
                where = (f"{walk['label']}: {here}" if live
                         else f"next: {walk['label']}: {here}")
                note = f"{note} · {where}" if note else where
        if not live and not terminal and stage in ("queued", "searching") \
                and _resolving(w):
            # The add answered before MusicBrainz did
            # (`pending_albums.create_from_request`): the row says what the
            # SERVER is doing, instead of "queued" — which reads as waiting for
            # a download nothing has searched for. A wish the store has ENDED
            # keeps its own state, and so does one a job is already working on:
            # a resolution that never landed must not make the row claim work
            # for ever (the marker's own age bound is in `pending_albums`).
            stage = pending_albums.STAGE_RESOLVING
            note = "Asking MusicBrainz what this release is — the search " \
                   "starts as it answers"
        if stage == "failed" and not terminal:
            # A failed ATTEMPT is not a given-up wish — with attempts left in
            # its budget the worker searches it again by itself — and the row
            # says so instead of reading as a dead end nobody can act on. It is
            # cancelled (the standing request goes), never "cleared": clearing
            # is for rows whose work is over.
            note = (note + " · " if note else "") + (
                "failed this attempt — searched again automatically at "
                + _clock(wishes.due_at(w, cfg or {})))
        key, label = _wish_source(w)
        rows.append({
            "id": f"wish:{w['id']}",
            "kind": "wish",
            "wish_id": w["id"],
            "job_id": job["job_id"] if job else None,
            "stage": stage,
            "source_key": key,
            "source": label,
            "title": str(w.get("title") or ""),
            "artist": str(w.get("artist") or ""),
            "release_mbid": str(w.get("release_mbid") or ""),
            "album_path": str(w.get("album_path") or (job or {}).get("album_path") or ""),
            "progress": progress,
            "reason": reason,
            "note": note,
            "attempts": int(w.get("attempts") or 0),
            # WHICH pressing this wish is waiting for, resolved once and kept
            # on the wish itself (server/wishes' release identity section):
            # every key is present and a fact nobody could resolve is empty, so
            # a wish whose release was never looked up still renders. A row read
            # out of a store that predates the block still gets the documented
            # shape with the id the wish is keyed by.
            "release": w.get("release")
            or wishes.release_identity({}, w.get("release_mbid") or ""),
            # Empty searches so far, and when the next automatic one may run
            # (0 = it will not: the wish is terminal until the user retries).
            "not_found": int(w.get("not_found") or 0),
            "retry_at": retry_at,
            # The ranked-candidate walk this release is being searched with
            # (spec R150-R154): `{index, total, label, mbid, title, tried}`, or
            # null for a wish with nothing to walk (one candidate, or none).
            # The row's ONE position for the release, so a client renders it
            # from data rather than parsing `note`.
            "walk": walk,
            "retryable": (stage in ("failed", "needs_attention", "background")
                          and status not in ("imported", "available", "searching")),
            # True while a framework album (the "Add to library" folder) is on
            # disk with no audio yet — cancelling such a row must remove that
            # folder too, which is its own endpoint (server.api_library).
            "pending": bool(w.get("pending")),
            "created_at": float(w.get("added_at") or 0),
            "updated_at": float(w.get("updated_at") or 0),
            # A wish still waiting can be taken back off the queue; one that is
            # being downloaded is cancelled as ITS JOB (same button, and the
            # wish returns to 'wanted' by itself when the job stops). A wish
            # that FAILED an attempt but is still within its budget belongs
            # here too: the worker will search it again by itself, so the row
            # is still in the pipeline and the way out is to stop wanting the
            # release — cancel, not clear. Without this the row sat in Failed
            # with no action at all until the attempts cap gave up for good.
            "cancelable": stage in ("queued", "searching", "downloading",
                                    "verifying", "importing", "needs_attention",
                                    "failed", "background",
                                    pending_albums.STAGE_RESOLVING),
            # A TERMINAL wish (imported, nothing found, or failed for good —
            # `wishes.is_terminal`) is one the user may take off the list
            # entirely; one that is still wanted or being searched is not
            # cleared but CANCELLED, which is its own button and its own
            # meaning (the wish goes back off the queue only because the user
            # says so, not because "clear" sounded harmless).
            "clearable": terminal,
            "log_tail": (job or {}).get("log_tail") or [],
        })
    return rows


def _bulk_rows(queued):
    """Releases waiting in the pipeline's own queue (not started yet).

    `waiting` and `position` are what tell these rows apart from the rest of
    the "queued" section: a wish waiting for the network is being searched,
    while these are waiting for one of the releases at the concurrency ceiling
    to finish — the UI groups them as Waiting and says where in the line they
    are. The id is `pipeline:<key>`, and the key is STABLE for the life of the
    item (the release id, or `#<ticket>` for a grab that has none), which is
    what lets the page key a selection by it while it re-polls every second."""
    rows = []
    for item in queued:
        release = item.get("release") or {}
        artists = release.get("artists") or []
        artist = str(artists[0].get("name") or "") if artists and isinstance(artists[0], dict) else ""
        key = str(item.get("key") or "")
        position = int(item.get("position") or 0)
        src = str(item.get("source") or "")
        if not src:
            # A release queued by MusicBrainz (the bulk routes) versus a peer's
            # folder grabbed by hand, which has no release to name.
            src = "musicbrainz" if (release or item.get("release_mbid")) else "soulseek"
        rows.append({
            "id": f"pipeline:{key}",
            "kind": "pipeline",
            "job_id": None,
            "wish_id": item.get("wish_id"),
            "stage": "queued",
            "source_key": src,
            "source": {"musicbrainz": "MusicBrainz", "soulseek": "Soulseek",
                       "auto": "Auto-import"}.get(src, "Soulseek"),
            # A release nobody has resolved yet has no title; a folder grab has
            # no release at all and is named by the peer·folder label instead.
            "title": str(release.get("title") or "") or str(item.get("label") or ""),
            "artist": artist,
            "release_mbid": str(item.get("release_mbid") or release.get("id") or ""),
            "album_path": "",
            "progress": None,
            "reason": "",
            # Where it is in the line, and why it is there at all.
            "waiting": True,
            "position": position,
            "note": f"Waiting for a free slot — position {position} in the queue"
                    if position else "Waiting for a free slot in the pipeline",
            # The queued item carries the release it was queued with (a
            # MusicBrainz payload when the route resolved it), so a row that has
            # not started yet still names the exact edition it is waiting to
            # fetch.
            "release": wishes.release_identity(
                release, str(item.get("release_mbid") or "")),
            "created_at": 0.0,
            "updated_at": 0.0,
            "cancelable": True,
            # Not "clearable": this release has not run yet — taking it off the
            # list is its cancel (the drop from the bulk queue), which is a
            # different word for a different thing.
            "clearable": False,
            "log_tail": [],
        })
    return rows


def _import_rows(status):
    """The "import everything downloaded" run: one row for the run itself."""
    if str(status.get("state") or "") != "running":
        return []
    done = int(status.get("done") or 0)
    total = int(status.get("total") or 0)
    current = str(status.get("current") or "")
    # The run's own row says how the LAST album it imported actually went: the
    # chain's one-line report when there is one (server/import_queue.py keeps it
    # per album), and the reason when a configured chain did not run at all —
    # an album that imported but came out unfinished must not read as a plain
    # success. One album's line, not all of them: the row renders on one line.
    last = (status.get("results") or [None])[-1] or {}
    chain_line = str(last.get("note") or last.get("error") or "")
    note = f"{done}/{total} album(s) imported" + (f" — {chain_line}" if chain_line else "")
    return [{
        "id": "import:run",
        "kind": "import",
        "job_id": None,
        "wish_id": None,
        "stage": "importing",
        "source_key": "soulseek",
        "source": "Soulseek",
        "title": os.path.basename(current.rstrip("\\/")) or "Importing downloads",
        "artist": "",
        "release_mbid": "",
        "release": _no_release(),
        "album_path": current,
        "progress": {"text": current, "done": done, "total": total,
                     "percent": (100.0 * done / total) if total else None},
        "reason": "",
        "note": f"{done}/{total} album(s) imported",
        "created_at": float(status.get("started_at") or 0),
        "updated_at": float(status.get("finished_at") or status.get("started_at") or 0),
        "cancelable": True,
        "clearable": False,       # a RUN is stopped, not cleared (see cancel)
        "log_tail": [],
    }]


def _done_rows(ready):
    """Finished downloads still sitting in the download folder.

    "Completed" means the DOWNLOAD is over; whether it is also in the library
    is the `note` — a row the user can still act on, unlike a job that imported
    its album and is done forever."""
    rows = []
    for path in ready:
        rows.append({
            "id": f"ready:{path}",
            "kind": "ready",
            "job_id": None,
            "wish_id": None,
            "stage": "completed",
            "source_key": "soulseek",
            "source": "Soulseek",
            "title": os.path.basename(str(path).rstrip("\\/")) or str(path),
            "artist": "",
            "release_mbid": "",
            "release": _no_release(),
            "album_path": "",
            # The download folder's own path: a "completed" row is imported
            # from HERE, and it is not a library album to link to.
            "path": str(path),
            "progress": None,
            "reason": "",
            "note": "In the download folder — ready to import",
            "created_at": 0.0,
            "updated_at": 0.0,
            "cancelable": False,      # nothing to cancel: the import is the action
            # Deliberately NOT clearable: "clear" on this row could only mean
            # deleting the downloaded bytes the user is about to import. The
            # staging card and the Downloads tab own those bytes and their own
            # clear says what it deletes; this row's action is the import.
            "clearable": False,
            "log_tail": [],
        })
    return rows


def _prompt_warning(p):
    """What one prompt says about its album — `import_autonomy.warning`, the
    ONE shape every surface carries it in (the queue row, the album page's
    banner, the wizard's own prompt banner)."""
    from server import import_autonomy

    return import_autonomy.warning(p)


def _prompt_rows(prompts):
    """Albums an import could not finish, as rows of their own.

    These rows exist for the album NOTHING else shows — one imported by the
    wizard months ago, an album whose wish row was cleared, a bulk import whose
    ticket is gone. An album whose own row is already in this payload does not
    get a second one: `build_queue` hands this row's warning to that row
    instead, because one album is one row (`_prompt_warning` is the payload
    both carry).

    The row is FINISHED, not stalled, whenever the entry is a WARNING: the
    album is in the library, so it sits where every other finished album sits
    and says what it is short of. A separate "waiting on you" section for it is
    exactly what made a warning read as a held import (R166). An entry that
    really IS a wait — a review import that has not run its chain, a disc
    structure whose feature is unpicked — keeps the waiting section, because
    that is what it is.
    """
    from server import import_autonomy

    rows = []
    for p in prompts:
        album = str(p.get("album") or "")
        warn = _prompt_warning(p)
        rows.append({
            "id": f"prompt:{p.get('id') or album}",
            "kind": "prompt",
            "job_id": None,
            "wish_id": None,
            "stage": "needs_attention" if warn["waiting"] else "completed",
            "source_key": "import",
            "source": "Import",
            "title": str(p.get("album_name") or os.path.basename(album.rstrip("/")) or album),
            "artist": "",
            "release_mbid": "",
            "release": _no_release(),
            "album_path": album,
            "progress": None,
            "needs": warn,
            # What the row's warning line renders (`missing_labels`) and the
            # ids the wizard's `?missing=` takes — the same two fields the rows
            # a warning ATTACHES to get, so one payload shape reaches the UI
            # whichever row a warning rides.
            "missing": list(warn["families"]),
            "missing_labels": list(warn["labels"]),
            "wizard_link": str(p.get("link") or ""),
            "action": "manual",
            "action_link": str(p.get("link") or ""),
            "dismissable": True,
            # A row that IS waiting says WHY in its own reason line (the
            # sentence the notification body is made of); a warning on a
            # finished album does not — its whole sentence rides `needs.detail`
            # and the row is not a failure.
            "reason": import_autonomy.body(p) if warn["waiting"] else "",
            "note": ("The import is waiting for this before it can finish"
                     if warn["waiting"]
                     else "In the library — the missing piece is entered by hand, "
                          "or this warning is dismissed"),
            "created_at": float(p.get("at") or 0),
            "updated_at": float(p.get("at") or 0),
            # Nothing to cancel: the album is IN the library and no job of ours
            # is running over it. Its actions are the two in `needs` — the
            # wizard and the dismiss, which IS how a warning is taken off the
            # list, so `clearable` would be a second word for the same button.
            "cancelable": False,
            "clearable": False,
            "log_tail": [],
        })
    return rows


def build_queue(cfg=None):
    """The whole pipeline as one payload, grouped into SECTIONS."""
    from server import import_queue, soulseek

    cfg = cfg or load_config()
    all_jobs = soulseek_auto.jobs()
    job_rows = []
    for job in all_jobs:
        row = _job_row(job)
        if row is None:
            continue
        job_rows.append(row)
    # A job filling a wish is that wish's row, not a second one.
    jobs_by_wish = {}
    for row in job_rows:
        wid = row.get("wish_id")
        if wid:
            jobs_by_wish[int(wid)] = row
    rows = _wish_rows(wishes.list_wishes(), jobs_by_wish, cfg)
    # A job whose wish is no longer in the list (deleted mid-download, or a
    # wish_id nothing matches) keeps its OWN row: it is still downloading, and
    # hiding real work because the row it belonged to went away is exactly the
    # dishonesty this view exists to remove.
    shown = {r["job_id"] for r in rows if r.get("job_id")}
    rows += [r for r in job_rows if r.get("job_id") not in shown]
    # A release parked in the pipeline that already has a row of its OWN is
    # that row, not a second one. A wish from MusicBrainz is queued in the
    # pipeline while it waits for a slot, so both builders describe it — one
    # add read as two releases being fetched, one of them named by the wish and
    # one by the pipeline ticket. Only a pipeline item nothing else is showing
    # (a grab with no wish, or a wish dropped while it waited) gets its own.
    wished = {int(r["wish_id"]) for r in rows if r.get("wish_id")}
    rows += [r for r in _bulk_rows(soulseek_auto.queued())
             if not (r.get("wish_id") and int(r["wish_id"]) in wished)]
    rows += _import_rows(import_queue.status())
    # Albums an import could not finish (server/import_autonomy): a WARNING on
    # the row the album already has, never a hold. The album is in the library,
    # so the row keeps its own stage (a finished release reads as finished) and
    # gains what is still missing plus the two things to do about it — the
    # wizard at the step that decides it, and the dismiss. A prompt whose album
    # no other row shows keeps a row of its own, in the same finished section,
    # so nothing is lost.
    warned = {}
    try:
        from server import import_autonomy
        for pr in _prompt_rows(import_autonomy.prompts(cfg)):
            key = _album_key(pr.get("album_path"))
            if key:
                warned[key] = pr
    except Exception:
        # A prompt file that cannot be read must not blank the whole queue.
        warned = {}
    if warned:
        attached = set()
        for row in rows:
            key = _album_key(row.get("album_path"))
            pr = warned.get(key) if key else None
            if pr is None:
                continue
            needs = pr["needs"]
            row["needs"] = needs
            row["missing"] = needs["families"]
            row["missing_labels"] = needs["labels"]
            row["wizard_link"] = needs["link"]
            row["action"] = "manual"
            row["action_link"] = needs["link"]
            row["dismissable"] = True
            attached.add(key)
        rows += [pr for key, pr in warned.items() if key not in attached]
    try:
        rows += _done_rows(soulseek.ready_albums(cfg))
    except Exception:
        # An unreachable slskd must not blank the whole page: the queue view
        # still answers with what the registries know.
        pass

    sections = {name: [] for name in SECTIONS}
    for row in rows:
        sections[_section_of(row["stage"])].append(row)
    for rows_ in sections.values():
        rows_.sort(key=lambda r: (-(r.get("created_at") or 0), r["id"]))
    # The counts are DERIVED from the rows, never a tally kept beside them: a
    # section header and the header line can then never disagree with the list
    # underneath (and a client that renders `sections` alone still shows the
    # numbers this says).
    counts = {
        "queued": len(sections["queued"]),
        "in_progress": len(sections["in_progress"]),
        "background": len(sections["background"]),
        "needs_attention": len(sections["needs_attention"]),
        "completed": len(sections["completed"]),
        "failed": len(sections["failed"]),
    }
    counts["total"] = sum(counts.values())
    return {
        "sections": sections,
        "counts": counts,
        "running": len([j for j in all_jobs if j["state"] in ("running", "confirm")]),
        "concurrency": soulseek_auto.concurrency(cfg),
        "candidate_slots": soulseek_auto.candidate_slots(cfg),
        "download_slots": int(cfg.get("soulseek_download_slots") or 0),
    }


@router.get("/api/queue")
def queue_view():
    """Queued, in-progress, needs-attention, completed and failed items —
    the whole download pipeline in one list (see this module's docstring)."""
    return build_queue()


class QueueCancelRequest(BaseModel):
    id: str


class QueueRetryRequest(BaseModel):
    id: str


class QueueClearRequest(BaseModel):
    """One queue row to clear (`id`), or a scope of finished ones.

    `scope` is "finished" (every finished row the queue owns), "wishes" (the
    same, narrowed to the wishlist — the Wishes tab's own "Clear finished"), or
    one section name from `SECTIONS` (that list's own button clears exactly
    what its header counted). Neither field is a required one in the sense that
    the other must be empty — the route refuses a body that names nothing (see
    queue_clear)."""
    id: str = ""
    scope: str = ""


def _drop_pipeline_item(item_id):
    """Take one release back out of the bulk queue (it has not started yet)."""
    return soulseek_auto.drop_queued(item_id.partition(":")[2])


@router.post("/api/queue/retry")
def queue_retry(req: QueueRetryRequest):
    """Retry ONE terminal row BY HAND — the other half of the retry policy.

    Nothing else brings a terminal item back: "not found" is never re-searched
    on a timer and a spent attempts cap is not spent again (see server/wishes'
    retry policy), so without this press the row would sit there forever with
    no way forward. What it does per kind:

    * ``wish:`` — re-arms the wish (``wishes.rearm``: attempts, empty-search
      budget and backoff all start over) and searches it NOW, interval ignored.
    * ``job:`` — puts the release back into the pipeline. A job that has no
      MusicBrainz id (a browsed folder) cannot be re-searched by id, and says so.
    * anything else (a row that is still running, a finished download whose
      action is the import, a prompt whose action is the wizard) is refused with
      the next step in the message.
    """
    from server import wishes, wishes_worker

    item_id = str(req.id or "")
    kind, _, ref = item_id.partition(":")
    if not kind or not ref:
        raise HTTPException(400, "id must be '<kind>:<ref>'")
    if kind == "wish":
        try:
            wid = int(ref)
        except ValueError:
            raise HTTPException(400, "bad wish id")
        wish = wishes.get_wish(wid)
        if wish is None:
            raise HTTPException(404, "wish not found")
        if wish.get("status") == "searching":
            raise HTTPException(409, "this wish is being searched right now")
        wishes.rearm(wid)
        # `trigger` never refuses: a pass already in flight takes this request
        # and runs it the moment it ends (server.wishes_worker's follow-ups), so
        # the retry is queued here instead of being answered with "a cycle is
        # already running" and silently doing nothing until the next tick.
        wishes_worker.trigger(wid)
        return {"ok": True, "retried": item_id}
    if kind == "job":
        try:
            jid = int(ref)
        except ValueError:
            raise HTTPException(400, "bad job id")
        job = soulseek_auto.job_state(jid)
        if job.get("state") in ("running", "confirm"):
            raise HTTPException(409, "that download is still running")
        mbid = str((job.get("release") or {}).get("id") or "")
        if not mbid:
            raise HTTPException(
                409, "this download has no MusicBrainz release to search again — "
                     "start it from the Soulseek page (search or browse)")
        soulseek_auto.enqueue(release_mbid=mbid)
        return {"ok": True, "retried": item_id}
    if kind == "prompt":
        raise HTTPException(
            409, "this album is already in the library — enter what is missing "
                 "from the wizard, or dismiss the prompt")
    raise HTTPException(400, f"'{kind}' rows are not retried from the queue")


@router.post("/api/queue/clear")
def queue_clear(req: QueueClearRequest):
    """Take FINISHED rows off the queue — one row, or every finished one.

    The queue accumulates history: a download that imported, a job that gave up,
    a wish nothing was ever found for, a release whose wish the user is done
    with. Clearing is about the LIST, not about the library — nothing is
    un-imported, no download is deleted and no byte on disk is touched — and it
    is only ever offered where the work is over:

    * ``{"id": "wish:7"}`` — one row (``build_queue``'s ``clearable`` says
      which; every row carries it). A wish leaves the wishlist, with the
      framework album folder it created (``pending_albums.remove_for_wish``, the
      same pair ``DELETE /api/wishes/{id}`` uses); a settled job is forgotten by
      the registry (``soulseek_auto.forget``).
    * ``{"scope": "finished"}`` — every finished row the QUEUE owns: imported
      wishes, settled jobs, wishes nothing was found for, wishes that failed for
      good.
    * ``{"scope": "wishes"}`` — the same, narrowed to the wishlist (what the
      Wishes tab's own "Clear finished" clears).
    * ``{"scope": "<section>"}`` — one SECTION of the queue (one of
      ``build_queue``'s SECTIONS names: "completed", "failed", …): the finished
      rows THAT list shows, which is what a section header's own button means
      (it says how many it counted). A section holding both finished and
      still-standing rows (needs_attention: a wish nothing was found for beside
      an import prompt) loses only the finished ones — the prompt stays until
      it is dismissed or its family is supplied.

    What it REFUSES is the point: a row that is still running, parked on a
    question, or waiting (a bulk-queue release, a running import, a wish that is
    still wanted or being searched) answers 409 naming its real alternative —
    cancel is a different action with a different meaning, and "clear" must
    never be the button that quietly does it. A finished download sitting in the
    download folder is not clearable either: its row's action is the import, and
    deleting those bytes is the staging card's job (which says so).

    Answers how many rows went and which (``cleared``/``ids``), so the caller
    can refetch and say what happened."""
    from server import pending_albums, wishes

    item_id = str(req.id or "").strip()
    scope = str(req.scope or "").strip().lower()
    if not item_id and scope not in ("finished", "wishes") and scope not in SECTIONS:
        raise HTTPException(400, "id must be '<kind>:<ref>', or scope must be "
                                 "'finished', 'wishes' or a section name")
    sections = build_queue().get("sections", {})
    if item_id:
        rows = [r for section in sections.values() for r in section]
        wanted = [r for r in rows if r["id"] == item_id]
        if not wanted:
            raise HTTPException(404, f"no queue row '{item_id}'")
        if not wanted[0].get("clearable"):
            raise HTTPException(409, _clear_refusal(wanted[0]))
    elif scope in SECTIONS:
        # One section: exactly the finished rows IT shows (its header counts
        # them the same way — `clearable` off the same payload this reads).
        wanted = [r for r in sections.get(scope, []) if r.get("clearable")]
    else:
        wanted = [r for section in sections.values() for r in section
                  if r.get("clearable")
                  and (scope != "wishes" or r["kind"] == "wish")]

    cleared, ids = 0, []
    for row in wanted:
        if row["kind"] == "wish":
            wid = int(row["wish_id"])
            pending_albums.remove_for_wish(wid)     # a framework folder goes too
            # The settled job this row was merged with is the same album's
            # history: it was claimed by this row, so leaving it behind would
            # make it pop up as a row of its own the moment the wish goes.
            if row.get("job_id"):
                soulseek_auto.forget(row["job_id"])
            if wishes.delete_wish(wid):
                cleared += 1
                ids.append(row["id"])
        elif row["kind"] == "job":
            if soulseek_auto.forget(row["job_id"]):
                cleared += 1
                ids.append(row["id"])
    return {"ok": True, "cleared": cleared, "ids": ids}


def _clear_refusal(row):
    """Why this row cannot be cleared, in the words of what it IS."""
    if row["kind"] == "ready":
        return ("this download is waiting to be imported — import it, or delete "
                "its bytes from the staging card (the queue never deletes a "
                "download)")
    if row["kind"] == "prompt":
        return ("this album is already in the library — dismiss the prompt "
                "instead of clearing it")
    if row["kind"] in ("pipeline", "import"):
        return "this one has not run yet — cancel it instead of clearing it"
    return ("this row is still in the pipeline — cancel it instead of clearing "
            "it (clearing is for finished rows)")


@router.post("/api/queue/cancel")
def queue_cancel(req: QueueCancelRequest):
    """Cancel ONE row of the queue.

    Dispatches to whichever primitive owns the row: a running job is stopped
    (its wish goes back to the queue by itself), a waiting wish is removed from
    the wishlist, a bulk-queue entry is dropped and an import run stops after
    the album it is on. A row that is only a finished download is not
    cancelable — the queue does not pretend a delete button exists for it — and
    neither is a prompt: the album is already in the library, and its actions
    are the wizard and the dismiss endpoint."""
    from server import import_queue, wishes

    item_id = str(req.id or "")
    kind, _, ref = item_id.partition(":")
    if not kind or not ref:
        raise HTTPException(400, "id must be '<kind>:<ref>'")
    if kind == "job":
        try:
            ok = soulseek_auto.cancel(int(ref))
        except ValueError:
            raise HTTPException(400, "bad job id")
        if not ok:
            # Settled already (or cancelled twice): not an error the user can
            # act on, but nothing was cancelled either.
            raise HTTPException(409, "that download is not running any more")
        return {"ok": True, "cancelled": item_id}
    if kind == "wish":
        try:
            wid = int(ref)
        except ValueError:
            raise HTTPException(400, "bad wish id")
        wish = wishes.get_wish(wid)
        if wish is None:
            raise HTTPException(404, "wish not found")
        if wish.get("status") == "searching":
            # It is being searched right now: stop the job doing it, then take
            # the wish off the list — the order matters, or the worker would be
            # mid-download for a wish that no longer exists.
            job_id = None
            for j in soulseek_auto.jobs():
                if j.get("wish_id") == wid and j.get("state") in ("running", "confirm"):
                    job_id = j.get("id")
                    break
            if job_id:
                soulseek_auto.cancel(job_id)
        return {"ok": True,
                "cancelled": item_id, "removed": bool(wishes.delete_wish(wid))}
    if kind == "pipeline":
        if not _drop_pipeline_item(item_id):
            raise HTTPException(409, "that release already started")
        return {"ok": True, "cancelled": item_id}
    if kind == "import":
        if not import_queue.cancel():
            raise HTTPException(409, "no import run is in progress")
        return {"ok": True, "cancelled": item_id}
    if kind == "ready":
        # A finished download has nothing to cancel: its action is the import,
        # and its bytes are removed from the staging card, not from here.
        raise HTTPException(409, "a finished download is imported, not cancelled")
    if kind == "prompt":
        # An album an IMPORT could not finish: the album is in the library, so
        # there is no transfer to stop. Its own two actions are the wizard
        # ("Enter manually") and POST /api/import/prompts/dismiss.
        raise HTTPException(409, "this album is already in the library — enter "
                                 "what is missing, or dismiss the prompt")
    raise HTTPException(400, f"unknown queue item kind '{kind}'")
