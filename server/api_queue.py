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

The stage each row carries is the vocabulary in server.soulseek_auto.STAGES —
`queued`, `searching`, `downloading`, `verifying`, `importing`, `completed`,
`failed`, `needs_attention` — so a wish from the MusicBrainz queue and a job
started from the Soulseek search bar are described in the same words.
"""
import os
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mlo.config import load_config
from server import soulseek_auto

router = APIRouter()


def _clock(ts):
    """A retry deadline as the clock the user reads (local time, minutes)."""
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except Exception:
        return "later"

# Sections, in the order the page shows them: what waits, what runs, what needs
# a human, what is done, what went wrong. `needs_attention` is its own section
# rather than a flavour of failed — an item parked on a decision (a lossy-only
# copy, a release with no usable folder) has not failed and hiding it under
# failures is how a queue ends up with rows nobody ever answers.
SECTIONS = ("queued", "in_progress", "needs_attention", "completed", "failed")


def _section_of(stage):
    """Which section a row's STAGES name belongs in."""
    if stage in ("queued", "searching"):
        return "queued"
    if stage in ("downloading", "verifying", "importing"):
        return "in_progress"
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
            note = "Nothing was found — saved to the wishes list"
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
        # The release's own facts, straight off the job (no per-row lookup —
        # this route is polled every few seconds): the row can say WHICH
        # edition was searched for without a MusicBrainz request.
        "release": {"id": str(release.get("id") or ""),
                    "date": release.get("date"),
                    "media": [m for m in (release.get("media_formats")
                                          or [release.get("media")]) if m],
                    "track_count": release.get("tracks")},
        "created_at": float(job.get("started_at") or 0),
        "updated_at": float(job.get("ended_at") or job.get("started_at") or 0),
        "cancelable": stage in ("queued", "searching", "downloading", "verifying",
                                "importing", "needs_attention"),
        "log_tail": [l.get("msg") for l in (job.get("log") or [])[-4:]],
    }


def _wish_rows(wishes_list, jobs_by_wish):
    """Wish rows, merged with the job filling them when one is running.

    A wish being downloaded is ONE row with the download's stage and progress —
    not a "searching" wish and a "downloading" job side by side, which is how a
    single album looked like two items doing unrelated things."""
    rows = []
    for w in wishes_list:
        status = str(w.get("status") or "")
        stage = {"wanted": "queued", "searching": "queued", "imported": "completed",
                 "failed": "failed", "available": "needs_attention",
                 # A wish whose searches found nothing is TERMINAL (see
                 # server/wishes' retry policy): it stops by itself, so it is
                 # not "failed" — it needs the user, either to retry it or to
                 # fill it by hand. needs_attention is exactly that section.
                 "not_found": "needs_attention"}.get(status, "queued")
        job = jobs_by_wish.get(w["id"])
        progress, note, reason = None, "", str(w.get("last_error") or "")
        if job is not None:
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
            # Empty searches so far, and when the next automatic one may run
            # (0 = it will not: the wish is terminal until the user retries).
            "not_found": int(w.get("not_found") or 0),
            "retry_at": retry_at,
            "retryable": (stage in ("failed", "needs_attention")
                          and status not in ("imported", "available", "searching")),
            # True while a framework album (the "Add to library" folder) is on
            # disk with no audio yet — cancelling such a row must remove that
            # folder too, which is its own endpoint (server.api_library).
            "pending": bool(w.get("pending")),
            "created_at": float(w.get("added_at") or 0),
            "updated_at": float(w.get("updated_at") or 0),
            # A wish still waiting can be taken back off the queue; one that is
            # being downloaded is cancelled as ITS JOB (same button, and the
            # wish returns to 'wanted' by itself when the job stops).
            "cancelable": stage in ("queued", "searching", "downloading",
                                    "verifying", "importing", "needs_attention"),
            "log_tail": (job or {}).get("log_tail") or [],
        })
    return rows


def _bulk_rows(queued):
    """Releases waiting in the pipeline's own bulk queue (not started yet)."""
    rows = []
    for item in queued:
        release = item.get("release") or {}
        artists = release.get("artists") or []
        artist = str(artists[0].get("name") or "") if artists and isinstance(artists[0], dict) else ""
        key = str(item.get("key") or "")
        rows.append({
            "id": f"pipeline:{key}",
            "kind": "pipeline",
            "job_id": None,
            "wish_id": None,
            "stage": "queued",
            "source_key": "musicbrainz",
            "source": "MusicBrainz",
            "title": str(release.get("title") or ""),
            "artist": artist,
            "release_mbid": str(item.get("release_mbid") or release.get("id") or ""),
            "album_path": "",
            "progress": None,
            "reason": "",
            "note": "Waiting for a free slot in the pipeline",
            "created_at": 0.0,
            "updated_at": 0.0,
            "cancelable": True,
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
        "album_path": current,
        "progress": {"text": current, "done": done, "total": total,
                     "percent": (100.0 * done / total) if total else None},
        "reason": "",
        "note": f"{done}/{total} album(s) imported",
        "created_at": float(status.get("started_at") or 0),
        "updated_at": float(status.get("finished_at") or status.get("started_at") or 0),
        "cancelable": True,
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
            "log_tail": [],
        })
    return rows


def _prompt_rows(prompts):
    """Albums an import could not finish, as rows in the ONE queue.

    These are the STALLED rows: the album is already in the library (a wizard
    import, a bulk run, an unattended download — every path ends in
    ``imports.finish_album``) and something no script can invent is still
    missing. The row carries what is missing and the two things the user can do
    about it, which is what makes a stalled album actionable instead of a red
    grading line somewhere else:

    * ``action_link`` — the wizard at this album and AT the step that decides
      the first missing family (``mlo.import_policy.wizard_link``, the very
      link the ``import_needs_data`` notification carries),
    * ``dismissable`` — "the album is fine as it is", which is
      ``POST /api/import/prompts/dismiss`` (a later import of the album
      recomputes the gaps and raises it again if the family is still missing).
    """
    from server import import_autonomy

    rows = []
    for p in prompts:
        album = str(p.get("album") or "")
        families = [f for f in (p.get("families") or []) if isinstance(f, dict)]
        rows.append({
            "id": f"prompt:{p.get('id') or album}",
            "kind": "prompt",
            "job_id": None,
            "wish_id": None,
            "stage": "needs_attention",
            "source_key": "import",
            "source": "Import",
            "title": str(p.get("album_name") or os.path.basename(album.rstrip("/")) or album),
            "artist": "",
            "release_mbid": "",
            "album_path": album,
            "progress": None,
            # What is missing, in the wizard's own order: the ids the wizard's
            # `?missing=` parameter takes and the labels a row can show.
            "missing": [str(f.get("id") or "") for f in families],
            "missing_labels": [str(f.get("label") or "") for f in families],
            "wizard_link": str(p.get("link") or ""),
            "action": "manual",
            "action_link": str(p.get("link") or ""),
            "dismissable": True,
            "reason": import_autonomy.body(p),
            "note": "Enter what is missing by hand, or dismiss it",
            "created_at": float(p.get("at") or 0),
            "updated_at": float(p.get("at") or 0),
            # Nothing to cancel: the album is IN the library. The row's actions
            # are the two above.
            "cancelable": False,
            "log_tail": [],
        })
    return rows


def build_queue(cfg=None):
    """The whole pipeline as one payload, grouped into SECTIONS."""
    from server import import_queue, soulseek, wishes

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
    rows = _wish_rows(wishes.list_wishes(), jobs_by_wish)
    # A job whose wish is no longer in the list (deleted mid-download, or a
    # wish_id nothing matches) keeps its OWN row: it is still downloading, and
    # hiding real work because the row it belonged to went away is exactly the
    # dishonesty this view exists to remove.
    shown = {r["job_id"] for r in rows if r.get("job_id")}
    rows += [r for r in job_rows if r.get("job_id") not in shown]
    rows += _bulk_rows(soulseek_auto.queued())
    rows += _import_rows(import_queue.status())
    # Albums an import could not finish (server/import_autonomy): the STALLED
    # ones, in the same needs-you section as a download parked on a question —
    # one place to see everything that is waiting on a person, whether it is
    # waiting for a search or for a decision about an album already on disk.
    try:
        from server import import_autonomy
        rows += _prompt_rows(import_autonomy.prompts(cfg))
    except Exception:
        # A prompt file that cannot be read must not blank the whole queue.
        pass
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
    counts = {
        "queued": len(sections["queued"]),
        "in_progress": len(sections["in_progress"]),
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
        started = wishes_worker.trigger(wid)
        if not started.get("ok"):
            raise HTTPException(409, str(started.get("error") or "already running"))
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
