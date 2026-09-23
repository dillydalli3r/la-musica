"""The import service: what happens to an album once it is on disk.

Every import path (the wizard's upload/ingest, the downloads page, the bulk
queue, the sequential import queue, the Soulseek auto-importer and the wish /
artist-watch pipeline behind it) ends in the same place — :func:`finish_album`
runs the configured script chain over the new album folder, on the folder the
organizer left it at, and :func:`bulk_import` is the queue that moves staging
folders into the library first. There is no path that acquires an album and
then leaves it without the optimization/tagging pass: the one that used to
(auto-import "staged" the album and deferred every tag-writing script) is what
this module's `chained`/`chain_off`/`note` result reports on now. The Soulseek
auto-import additionally verifies the downloaded audio against the release it
was looking for (:func:`acoustid_match`) before handing it to the same call.

Config keys this module owns:

    import_auto_scripts     master switch for the post-import chain
    import_scripts          explicit chain ids ([] = DEFAULT_CHAIN)
    import_autonomy         automatic (whole chain, then one WARNING for what
                            is still missing — the album is in the library
                            either way, spec R166) | review (stop before the
                            first family that needs a decision)
    import_review_families  families the user decides even in automatic mode
    import_bulk_concurrency albums processed at once by bulk_import
    import_acoustid         run the AcoustID release check on an import
    acoustid_*              key / availability of the AcoustID lookup itself

Nothing here raises at a caller: an import that half-worked still reports
every album's outcome.
"""
import os
import re
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from mlo.config import DEFAULT_RUN_ALL_ORDER, load_config
from mlo.paths import library_root, move_path
# The shared worker-count policy (`worker_limit`): the per-track tag writes
# below fan out to the same lane count every other multi-file runner uses.
from mlo.stats import worker_count
from mlo import advisory
from mlo import import_policy

from server import import_autonomy
from server import script_runners
from server import tagcache

# The import chain IS the Run All order, taken from the one place that keeps it
# (`mlo.config.DEFAULT_RUN_ALL_ORDER`) — one order, one list, so a script added
# to Run All can never go missing from the import path. This used to be a
# hand-kept second list that had already drifted: 16 (Mood & Energy), 17
# (Lyrics transliterate (AI)) and 19 (Optimize artist images) ran everywhere
# Run All ran and never on an import, so the same album got them through
# Optimize → Run All and never when it was simply imported. The order's own
# reasons (path-changing scripts first — beets moves the folder — then the
# sidecar namers, 10 Format all, 4 Grade last) are documented beside the list
# in mlo/config.py; `import_scripts` still replaces this chain outright.
#
# The scripts an import does NOT run are DATA, each with its reason, so leaving
# one out is a declaration someone can read rather than a second list someone
# has to diff. Nothing else may be dropped: a script added to the run order
# lands in this chain unless it is named here.
#
# The tuple is empty today, and 20 (Optimize library layout) is why it was not: its
# runner used to walk the ENTIRE music folder and write ONE report describing
# the whole library, ignoring `targets` on purpose, so an import chain would
# have re-walked the library once per album for a report about the library
# rather than the album being imported. It now scopes itself to `targets` when
# it is given any (`mlo.layout._scope`) and stores nothing for a scoped run, so
# an import runs it over the album it just imported — after beets (14) has put
# the folder in its canonical place and before the grade (4), which is what
# makes the layout the grade reads the fixed one. A library-wide Run All still
# gets the whole-folder pass and its stored report.
LIBRARY_WIDE_SCRIPTS = ()
DEFAULT_CHAIN = [sid for sid in DEFAULT_RUN_ALL_ORDER
                 if sid not in LIBRARY_WIDE_SCRIPTS]

# The ids a configured chain may name, kept in step with the registry itself
# (a bound that still advertised a removed id let a saved one come back as an
# error entry at import time instead of being dropped), so adding a script to
# script_runners.RUNNERS is the only edit that widens it.
SCRIPT_ID_MIN, SCRIPT_ID_MAX = 1, max(script_runners.RUNNERS)


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #
def chain_for(cfg=None):
    """The script ids an import runs, in order.

    ``import_auto_scripts`` off is no chain at all. Otherwise an explicit
    ``import_scripts`` list replaces the built-in chain (ids outside the
    runner registry dropped, duplicates dropped, order kept); an empty one
    means DEFAULT_CHAIN. Ids a family under review needs — script 13 is the
    only one, the lyrics fetch — are dropped last, so the preview a caller
    shows and the run it describes can never disagree.
    """
    cfg = cfg or {}
    if not cfg.get("import_auto_scripts", True):
        return []
    configured = cfg.get("import_scripts")
    if isinstance(configured, str):
        configured = re.split(r"[;,\s]+", configured)
    if configured:
        ids = []
        for raw in configured:
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                continue
            if SCRIPT_ID_MIN <= sid <= SCRIPT_ID_MAX and sid not in ids:
                ids.append(sid)
        return _without_reviewed(ids, cfg)
    return _without_reviewed(list(DEFAULT_CHAIN), cfg)


def _without_reviewed(ids, cfg):
    """*ids* minus the scripts that would decide a family the user kept."""
    dropped = import_policy.dropped_chain_ids(cfg)
    if not dropped:
        return ids
    return [sid for sid in ids if sid not in dropped]


def _invalidate_caches(*paths):
    """The album's tags just changed — the tag/name caches are stale.

    Same two caches ``/api/run`` invalidates after a script run; a missing
    module (a stripped backend) is not a reason to fail an import.

    *paths* are the folders that were written — the album an import just
    finished, the albums a step touched. They are passed to
    `tagcache.invalidate_album`, which drops only THOSE folders' cached tags and
    cover: an import is one album's work, and clearing the whole tag cache for
    it (every track of the user's library, re-parsed on the next page) is the
    library-wide price this used to charge. With no path the whole cache goes,
    which is what a caller that cannot name what it touched still gets.
    """
    folders = [p for p in paths if str(p or "")]
    try:
        from server import tagcache
        if folders:
            tagcache.invalidate_album(*folders)
        else:
            tagcache.invalidate_all()
    except Exception:
        pass
    try:
        from server import mbresolve
        mbresolve.invalidate()
    except Exception:
        pass


def _phase(text):
    """One line saying what the import is doing BEFORE its chain starts.

    The steps between the press and the first script are the slow ones — the
    links, the metadata and the cover art are network lookups — and none of
    them is a script, so nothing was publishing anything: the header bar and
    the run's row kept whatever the last producer had left on them (measured on
    a throwaway album: 5.4 s from the press to the first script, all of it
    before the chain, none of it on screen — and a real album's lookups are
    longer). The chain press then read as one that did nothing, which is what
    the owner reported.

    Published exactly like every other frame (``job_locks.publish``), so the row
    (MAINTAIN → In progress) and the bar can never disagree — the row is this
    job's when the import runs inside one (the bulk queue, a download) — with
    the bar left alone while a CHAIN owns it: a script run's own numbers are
    the truth then, and the installed dispatcher drops a frame from a thread
    that is not one of its runs (see ``script_runners``'s bar block).

    Best-effort by design: this is a readout, and a headless caller with no
    relay at all must not have an import fail over one.
    """
    try:
        from server import job_locks
        job_locks.publish(0, 0, text, job=job_locks.current())
    except Exception:
        traceback.print_exc()


# The claim kinds that mean an album is being IMPORTED or FINISHED right now:
# the download's own chain (`auto-import`), the import queue and every
# single-album import (`import`), and a script run over the album (`scripts`).
# The same three `server.api_queue` reads to keep such a release in the queue's
# In progress section, so both surfaces agree about what "being imported" is.
IMPORT_CLAIM_KINDS = ("auto-import", "import", "scripts")


def _importing_now(album_dir):
    """The claim an in-flight import holds *album_dir* under, or None.

    A claim on the album whose kind is one of `IMPORT_CLAIM_KINDS` means the
    pipeline is on it: `server.job_locks` is the one registry that knows, and
    ``holder`` answers the claim itself (`{job, kind, label, ...}`).
    """
    try:
        from server import job_locks
        claim = job_locks.holder(album_dir)
    except Exception:
        return None
    if not isinstance(claim, dict):
        return None
    if str(claim.get("kind") or "") not in IMPORT_CLAIM_KINDS:
        return None
    return claim


def finish_album(album_dir, cfg=None, progress=None, force=None, release=None,
                 wait=True):
    """Run the configured chain over ONE album folder.

    THE claim of the whole import — not only of its chain — is taken here, for
    the length of the call
    (``script_runners.claim_paths``): every step below writes to the album (the
    arrived values are dropped, the links, the genres, the metadata, the cover
    art), so an album another job is on must be queued behind (``wait=True``,
    every autonomous path) or refused at once with the claim's own sentence
    (``wait=False``, the user's own press) — and the album has to be held from
    the FIRST tag write, not from the first script. Claiming only at the chain
    left the network lookups before it unprotected: two presses on one album
    could drop arrived values and write metadata into the same files at the same
    time, and an auto-import's background chain was unlocked for its whole
    look-up phase. A script that MOVES the album takes the claim with it
    (:func:`_resolve_moved_album`'s folder, and the chain's own follow), so the
    steps after the chain — the pending marker, the cache invalidation, the
    gap report — hold the album where it is NOW.

    The single call every import path makes after an album is on disk — the
    wizard's finish, the downloads page's one-click import, the sequential
    import queue, the bulk queue, the Soulseek auto-importer and the wish /
    artist-watch pipeline behind it, so what an album ends up as cannot depend
    on which button was pressed. Returns ``{"path", "chain", "scripts",
    "errors", "chained", "chain_off", "note", "autonomy", "dropped",
    "skipped_families"}``:
    ``scripts`` is one
    result per chain id (``server.script_runners`` shape), ``errors`` a flat
    list for a caller that only wants to know what went wrong, and ``path`` the
    folder the album actually ended at: the chain reports where it ended up
    (script 14 imports the album into the library and renames it, so the path
    handed in is a staging folder by then), and :func:`_resolve_moved_album` is
    the fallback for an album a script moved without the chain following it.
    The three chain keys say what happened to
    the chain itself, which is what an unattended path has to report instead
    of a bare "imported":

        ``chained``    the configured chain ran over this folder
        ``chain_off``  there was no chain to run (``import_auto_scripts`` off,
                       or every configured id held for review)
        ``note``       ONE honest line about it, for a log, a queue row or a
                       notification — :func:`chain_summary` is that wording,
                       and it is "" only for a result that says nothing about a
                       chain at all

    ``skipped_families`` is the other half of that honesty, and the one a
    grader cannot report: the families this import was CONFIGURED not to fetch
    (the lyric fetch without script 13, ``genre_autofill`` off, the advisory
    with ``advisory_auto_fetch`` off) — one reason each, in the wizard's step
    order, and the same reasons ride along in ``note`` (so a queue row, a
    notification and the wizard's Finish line all say it) and are printed to
    the log. The user's switch is never overruled to close the gap; it is only
    never silent. See :func:`_skipped_families`.

    A failing script is reported, never raised: the album is already imported.
    An import whose chain did not run is never reported as an ordinary success.
    TWO notifications are emitted from here — `import_started` on the way in and
    `import_done` (with ``chain_summary``'s wording) where every path ends, in
    `_report_gaps` — because this IS the single entry point every import path
    reaches, so emitting here announces all of them once and only once. Both are
    user-switchable (`notify_import_start` / `notify_import_done`, Settings →
    Notifications, on by default). Callers still report their own outcome (see
    ``server.events``) for the phases this function knows nothing about
    import.

    The staged work is gated by its OWN keys, not by the chain switch:
    ``metadata_auto_fetch`` / ``cover_auto_fetch`` decide the artist image,
    descriptions and cover art, ``rym_links_auto`` the links and
    ``genre_autofill`` the genres (the family's only fetcher — see the step
    itself), so an import with the chain switched off still does those (the
    unattended import has fetched them since it existed, and turning the
    scripts off must not silently take the cover art with it) — while
    ``advisory_auto_fetch`` / ``instrumental_auto_fetch`` only run when a chain
    is configured to read what they write.

    WHAT THE ALBUM ARRIVED WITH is dropped for the four families this import
    decides itself — lyrics, genre, advisory, embedded cover art — by
    :func:`drop_arrived_values`, and it happens HERE, before every writer
    below and before the chain. Those writers fill an EMPTY value rather than
    replacing a full one (a user's own edit must survive their /run), so
    emptying the slots is what makes an import land what the import found:
    the peer's genre, rating, lyrics and artwork are the download's, not the
    album's. It runs after the review stop, because a stopped import hands the
    album over exactly as it arrived, and it leaves every family the user kept
    for themselves alone. ``dropped`` is its result: the files it walked, the
    files whose tags it could not touch (they keep what they arrived with),
    and how many files lost an arrived value per family. ``import_keep_synced_lyrics``
    is the lyric family's one exception — see the key.

    WHAT IS LEFT is always reported, in both modes, by
    ``_report_gaps``: the album's ``autonomy`` block carries what it is still
    missing (per family, from the grader's own checks) and, when there is
    anything, the one prompt that was raised for it — a notification plus an
    entry the wizard lists, naming the families and linking to the album at
    the step where each decision is made. ``import_autonomy`` decides how far
    the import got before that report: ``automatic`` (the default) runs
    everything and reports what no source could supply, ``review`` stops
    before the first family that needs a decision. In neither mode is an album
    left out of the library, and a prompt is withdrawn by the next import of
    the same album (the call that resolves the gaps is the one that clears it).

    *wait* decides what the chain does when another job already holds the
    album. ``True`` (every import path — the bulk queue, the one-click
    downloads import, the Soulseek importer, a wish landing) queues behind it
    and SAYS so while it waits (``script_runners.run_chain``): an import must
    not skip its chain, because the album is already on disk and the user asked
    for it to be finished. ``False`` is the user's own press of "Run the import
    chain" (``POST /api/import/finish``): there is someone at the keyboard, the
    run it would be queueing behind is doing that same work, and a press parked
    for minutes behind it and then re-running the chain is exactly the "nothing
    happens for ages" this rule removes — so it answers at once instead
    (``RunBusy``, which the route returns as 409 naming the holder).
    """
    cfg = cfg or load_config()
    path = os.path.normpath(str(album_dir))
    # The claim's own name in MAINTAIN → In progress: the run this import is
    # about to do (the same wording a chain claims itself with, so a queued
    # import is not renamed the moment its scripts start), or the album when
    # this library configures no chain at all.
    chain = chain_for(cfg)
    label = (script_runners.run_label(chain) if chain
             else "Import " + (os.path.basename(path.rstrip("\\/")) or path))
    if wait:
        # ONE IMPORT PER ALBUM. A second autonomous caller used to QUEUE behind
        # the claim and then run the whole pipeline again — the six pre-chain
        # lookups and all 21 scripts, over an album the first caller had just
        # finished ("it's doing the scripts again", with nothing about the
        # second run wanted). The album is already being imported, so this call
        # answers that instead of duplicating the work; the user's own press
        # (``wait=False``) keeps its 409, whose sentence names the holder.
        # A job importing its OWN claim is not a duplicate — the download job
        # holds the album from its first byte and then runs this very import
        # under that same claim — so the guard only fires for a FOREIGN job.
        try:
            running = _importing_now(path)
            mine = script_runners.job_locks.current()
        except Exception:
            running, mine = None, None
        # Only an IDENTIFIED job that is not the holder is a duplicate. A caller
        # with no job of its own (an unmanaged background thread) keeps the old
        # wait-then-run behaviour: skipping there could leave an album the
        # download's own chain is holding unimported, which is far worse than
        # the repeat this guard removes.
        if running is not None and mine is not None and running.get("job") != mine:
            holder = str(running.get("label") or "An import")
            return {"path": path, "scripts": [], "chain": [], "errors": [],
                    "chained": False, "chain_off": False,
                    "note": f"{holder} is already importing this album",
                    "skipped_families": [], "already_importing": True}
    with script_runners.claim_paths([path], kind="import", label=label,
                                   wait=wait):
        # Announced only once the album is really this import's: a press that
        # is refused (or a queued import still waiting) has not started, and a
        # notification about it would be a lie either way.
        _announce_import("import_started", album_dir, cfg=cfg)
        return _finish_album(path, cfg, progress=progress, force=force,
                             release=release, wait=wait)


def _finish_album(path, cfg, progress=None, force=None, release=None,
                  wait=True):
    """The body of :func:`finish_album`, already holding *path*.

    Split out for the claim: the album is held for the WHOLE import (see the
    entry point), and every early return below — a review stop, a missing
    folder, no configured chain — has to release it on the way out.
    """
    out = {"path": path, "scripts": [], "chain": [], "errors": [],
           "chained": False, "chain_off": False, "note": "",
           "skipped_families": []}
    chain = chain_for(cfg)
    out["chain"] = chain
    # Nothing to run: `import_auto_scripts` off, or every configured id is one
    # the user kept for themselves. Not the same as "the chain ran", which is
    # the whole point of the flag — see `note` at every exit below.
    out["chain_off"] = not chain
    if not os.path.isdir(path):
        out["errors"].append("album folder not found")
        out["note"] = "the album folder is not there"
        return out
    # How far this import goes on its own, decided BEFORE anything runs:
    # `run_cfg` is what every family step below reads, so a family the user
    # kept for themselves (`import_review_families`) is left alone by the very
    # switches that would otherwise decide it, and `stop` is the family a
    # review hands the album over at — read from the album AS IT ARRIVED, which
    # is why the plan is made here and not after the chain
    # (`mlo.import_policy.plan`).
    policy = import_policy.plan(cfg, path)
    run_cfg = import_policy.effective_config(cfg)
    # Which of the three FETCHED families this import is configured not to
    # fetch — the lyrics (script 13 in the chain), the genres (`genre_autofill`)
    # and the advisory (`advisory_auto_fetch`). Read off `run_cfg`, i.e. the
    # config the steps below actually run with, so the report and the run can
    # never disagree. Nothing is overruled to close a gap: the switch stays as
    # the user set it, and an import that lands without a family says so in its
    # own result and its own note (`_skipped_families`).
    out["skipped_families"] = _skipped_families(chain, run_cfg, policy["review"])
    for _reason in out["skipped_families"]:
        print(f"[mlo] import: {_reason} — {os.path.basename(path)}")
    # A FRAMEWORK album's placeholder cover is NOT dropped here any more. It
    # used to be, before the cover step below could say whether it had anything
    # better: the step then fetched the release's own artwork — and an import
    # whose every candidate `mlo.cover_choice` refuses (its floor is the
    # library's minimum, and a release whose CAA front is smaller than it has
    # nothing that clears) was left with NO cover file at all, which is the
    # grade's "Missing cover image" on an album the ADD had already given a real
    # release-group image. The placeholder is OUR temporary image only in the
    # sense that the release's own front is preferred — so the cover step is
    # what knows: it treats ours as no cover (so it still fetches) and drops it
    # once it has written a real one (`pending_albums.placeholder_cover_present`,
    # `drop_placeholder_cover`), and the end of an import drops it too — never
    # as the folder's last cover. The album's PENDING MARKER is untouched here,
    # as before: it stays until the configured chain has run (see the ends of
    # this function), so a folder whose import never got there keeps being
    # reported as pending rather than being dressed up as a finished album.
    if policy["stop"]:
        # Review: the album is handed over before the first step that needs a
        # decision, so nothing tag-writing runs past it. It keeps what it
        # arrived with and the wizard picks up where this stops — which is also
        # why a framework album stays pending: a configured chain has NOT run.
        out["chain"] = []
        out["note"] = (f"stopped for review at {policy['stop']} — the script "
                       "chain has not run")
        return _report_gaps(out, cfg, policy, path)
    # WHAT IT ARRIVED WITH goes now, before every writer below and before the
    # chain: the four families this import decides are the import's, and each
    # of their writers fills an empty slot rather than replacing a full one
    # (see `drop_arrived_values`, which is where the reasoning lives). After
    # the review stop on purpose — a stopped import hands the album over as it
    # arrived — and over `run_cfg`, so a family the user kept for themselves is
    # left exactly as it is in every mode.
    out["dropped"] = drop_arrived_values(path, run_cfg, chain)
    # The album's identity, read while it is still where the caller put it:
    # the chain's beets/organize step renames the folder to its canonical
    # layout, and after that the old path is the only handle this function has
    # on the album (see the re-resolve at the end).
    try:
        album_mbid, album_rgid = _album_mbids(path)
    except Exception:
        album_mbid = album_rgid = ""

    # RateYourMusic album + artist links: resolved and written once, only for
    # the links the album does not carry yet. Deliberately BEFORE the chain
    # check — an album imported with no scripts configured still gets its
    # links. Gated by rym_links_auto; a lookup that finds nothing is one log
    # line (the user pastes the URL in the links editor), never an error.
    # Announced first: what the user pressed was "Run the import chain", and
    # this is where the time before its first script goes (see `_phase` — a
    # family switched off finishes in the same breath and its line is replaced
    # by the next one, so nothing here can be left standing as a stale claim).
    _phase("Looking up links…")
    try:
        rym = stamp_rym_links(path, run_cfg)
        if rym["note"].startswith("could not resolve"):
            print(f"[mlo] rateyourmusic: {rym['note']} — {os.path.basename(path)}")
    except Exception:
        traceback.print_exc()

    # GENRES: the family's own automatic action — the same writer the wizard's
    # Genres step and the bulk release stamp call (`_stamp_release`), and the
    # ONLY thing that FETCHES a genre (script 8 trims and caps what is already
    # there, `mlo/autotag.py`). Without this step an unattended import landed
    # with no genre at all and graded GENRE_MISSING however many sources the
    # chain was allowed to ask: the acquisition paths never reached the one
    # automatic writer. `run_cfg`'s `genre_autofill` is False when the family is
    # one the user kept for themselves (`import_policy.effective_config`), so a
    # reviewed Genres family is left alone here exactly as it is by the wizard.
    # Runs BEFORE the chain, because script 8 reads what this writes. Never
    # fatal: a genre that cannot be resolved is a gap the report names.
    _phase("Fetching genres…")
    if run_cfg.get("genre_autofill", True):
        rel = release
        if not rel and album_mbid:
            # The identity the import just stamped is enough to ask for the
            # release the genres belong to — a release-GROUP id resolves to its
            # best edition through the same choice policy the wish path uses,
            # and the lookup is cached. No identity at all skips the step.
            try:
                from server import integrations as intg
                rel, _rid = intg.resolve_release(album_mbid)
            except Exception:
                rel = None
        if rel:
            try:
                written, failed = _stamp_release(path, rel, run_cfg)
                out["genres"] = {"written": written, "failed": failed}
                if failed:
                    out["errors"].append(
                        f"{failed} track(s) took no genre (see the genre sources)")
            except Exception:
                traceback.print_exc()

    # ---- the FILE-writing pair, beside the TAG-writing steps ---------------
    # Metadata (artist image, artist/album description) and cover art write
    # FILES — the review record they share, and the art the album folder keeps
    # — while links, genres, advisory and instrumentals write TAGS on the audio
    # files. Different files, so they go side by side: started here (the
    # album's identity is settled by the genre step above, which is what the
    # metadata lookup reads — `album_identity`) and joined before the chain,
    # which is the first thing that needs them on disk (script 5 processes the
    # images, the grade wants the cover). ONE worker for BOTH steps in their
    # own order, because they stage into ONE review record and must not
    # clobber each other (`stage_metadata` / the `covers` entry of the same
    # file).
    #
    # Both run on EVERY path, chain or no chain: `metadata_auto_fetch` /
    # `cover_auto_fetch` are their own switches, and the unattended import has
    # fetched them since it existed — the chain's own switch is about the
    # scripts, and turning it off must not silently take the cover art away
    # with it. Cover art: an album that arrived without one gets it now, found
    # by the identity the import just stamped and stored by the cover page's
    # own writer. With `*_review` on, the candidates are staged for the user
    # instead.
    _phase("Fetching metadata and cover art…")

    def _files_step():
        got = {}
        try:
            got["metadata"] = run_metadata_step(path, run_cfg)
        except Exception:
            traceback.print_exc()
        try:
            got["cover"] = run_cover_step(path, run_cfg)
        except Exception:
            traceback.print_exc()
        return got

    _files_pool = ThreadPoolExecutor(max_workers=1)
    _files = _files_pool.submit(_files_step)

    # Advisory BEFORE the chain: script 8 derives ALBUMITUNESADVISORY from the
    # per-track values, so writing ITUNESADVISORY afterwards would leave the
    # album tag stale. Gated by advisory_auto_fetch; never fatal. Only fetched
    # when a chain is configured to read them — they exist to feed script 8 and
    # the lyrics step, and a chain that is switched off must not leave those
    # tags behind as a side effect.
    _phase("Fetching advisories…")
    if chain and run_cfg.get("advisory_auto_fetch", True):
        try:
            out["advisory"] = fetch_advisories([path], run_cfg)
        except Exception:
            traceback.print_exc()

    # Instrumental detection sits next to it for the same reason (the chain's
    # lyrics step reads INSTRUMENTAL), and is independent of the advisory: a
    # track can be instrumental and explicit-rated. Gated by
    # instrumental_auto_fetch; never fatal.
    _phase("Checking instrumentals…")
    if chain and cfg.get("instrumental_auto_fetch", True):
        try:
            out["instrumental"] = fetch_instrumentals([path], cfg)
        except Exception:
            traceback.print_exc()

    # Artist image / descriptions: fetched here (the metadata step) so an
    # import leaves the album graded-ready. metadata_review on stages the
    # candidates instead of writing them. Staged BEFORE the chain on purpose:
    # the record is looked up by the album's own identity (see
    # `staged_metadata`), so it survives the chain moving the album to its
    # canonical folder — which it does, via beets/organize. Never fatal.
    # The two file steps had the whole advisory/instrumental pass to run in;
    # this is where their work is collected, before the chain (which reads what
    # they wrote) and before the no-chain return (whose result reports them).
    try:
        _files_got = _files.result()
        for _key in ("metadata", "cover"):
            if _files_got.get(_key) is not None:
                out[_key] = _files_got[_key]
    except Exception:
        traceback.print_exc()
    finally:
        _files_pool.shutdown(wait=True)

    if not chain:
        # `import_auto_scripts` off / every id held for review: deliberately
        # nothing to run. The album is finished by configuration — which is NOT
        # the same as the chain having run, so the result says which (`note`),
        # and every caller that reports the import says it too instead of
        # passing a bare "imported" on. A framework album's pending state ends
        # here as well: no chain is coming to end it later, and a folder the
        # user can never clear is a trap, not a warning.
        out["note"] = _with_families(_chain_off_note(cfg), out["skipped_families"])
        _invalidate_caches(path)        # the steps above wrote tags/files
        _clear_pending(path, cfg, chained=False, chain_off=True)
        return out

    # The folder the chain ends on, filled by `run_chain` (`final`): a script
    # that moves the album re-points the chain at its new folder, so what the
    # caller handed in is not necessarily where the album is when the chain is
    # done (an import hands in its staging folder; script 14 imports the album
    # into the library and renames it).
    final = []
    # The chain's own Grade step (script 4 — LAST in the shipped
    # `run_all_order`) grades this album seconds before this function has to
    # report what it is missing, and `_report_gaps` would grade the very same
    # album again. So the chain deposits what it graded in this private sink on
    # its own copy of the config (`run_grade_library` fills it; nothing else
    # reads it, and it never reaches a route or a payload) and the report reuses
    # that grade instead of paying for a second one.
    #
    # Only when the answer would really BE the same: with a family the user kept
    # for review, `mlo.import_policy.gaps` grades with that family's writer
    # switched back on (see its own comment) — a different question, to which
    # the chain's grade is not an answer. Then the sink is left empty and the
    # report grades as it always did.
    grade_sink = {}
    sink_armed = not import_policy.review_families(cfg)
    try:
        # *wait*: an import must not skip its chain just because a UI run
        # happens to hold the library lock — it queues behind it instead, and
        # says so while it waits (see the parameter's note above). The user's
        # own press passes False and is answered at once instead.
        # `final` is where the chain ended: script 14 moves the album into the
        # library and renames every file, so the folder this function was
        # handed is not the album any more (`_follow_moved_targets` follows it
        # and this is how the import hears about it).
        # The sink is armed on the run config ONLY for the chain call: the steps
        # BEFORE the chain (links, genres, advisory, instrumentals, metadata,
        # cover) are handed `run_cfg` too, and they must see exactly the config
        # they always saw — a step's own arguments are part of its contract (a
        # test double dispatches on them). Only the chain's Grade step reads this
        # key, and it is gone again before anything else can see it.
        if sink_armed:
            run_cfg["_grade_sink"] = grade_sink
        try:
            out["scripts"] = script_runners.run_chain(
                run_cfg, chain, targets=[path], force=force, progress=progress,
                wait=wait, final=final)
        finally:
            run_cfg.pop("_grade_sink", None)
    except script_runners.RunBusy as e:
        if not wait:
            # A caller that asked NOT to queue (the wizard's own "Run the
            # import chain") hears this at once, and the route answers 409 with
            # the claim's own sentence naming the holder. Queueing instead is
            # what made that press look dead: it sat behind whatever was
            # finishing the album and then ran the very same chain again.
            raise
        # Only reachable after the (1 h) wait timed out: report it so the
        # caller marks the album unfinished instead of "imported".
        out["errors"] = [str(e)]
        out["note"] = f"the script chain could not start: {e}"
        return out
    out["chained"] = True
    out["errors"] = [f"script {r.get('id')}: {r['error']}"
                     for r in out["scripts"] if r.get("error")]
    out["path"] = _resolve_moved_album(final[0] if final else out["path"],
                                       album_mbid, album_rgid)
    # The album MOVED (script 14 imports it into the library and renames it):
    # this import's claim follows it, so the steps below — the pending marker,
    # the cache invalidation, the gap report — hold the album where it is NOW
    # instead of a folder the audio has left. The chain's own follow has almost
    # always done this already (`_follow_moved_targets` re-points the whole job,
    # and this is then a no-op); this is the fallback for a mover that reported
    # no destination AND a library walk that missed it — the case the chain
    # ends with its own WARNING about.
    if os.path.normcase(out["path"]) != os.path.normcase(path):
        from server import job_locks
        try:
            # The import's OWN wait rule: an autonomous import queues for the
            # album (it is already in flight), the user's press is answered
            # instead of parked behind a foreign holder of the folder the album
            # moved into.
            job_locks.move(job_locks.current(), path, out["path"], wait=wait)
        except job_locks.PathLocked as e:
            print(f"[mlo] import: {e} — the album moved to "
                  f"{os.path.basename(out['path'])} and is not claimed there")
    # Scoped to the album that was imported (and to the folder it started in —
    # a script may have moved it): the tag and cover caches for anything else in
    # the library are still valid, so they stay.
    _invalidate_caches(path, out["path"])
    # The configured chain has run, over the folder it left the album at — only
    # now is a framework album finished (`chained=True`), and only on the FINAL
    # path: the marker lives inside the album, so clearing it on a stale one
    # would leave the real folder pending forever.
    _clear_pending(out["path"], cfg, chained=True)
    out["note"] = chain_summary(out)
    return _report_gaps(out, cfg, policy, out["path"],
                        grade=grade_sink.get(_dir_key(out["path"]))
                        or grade_sink.get(_dir_key(path)))


# The three families an import FETCHES with its own writers — the lyrics (the
# chain's script 13), the genres (`_stamp_release`, the family's only fetcher)
# and the advisory (`fetch_advisories`) — are gated by switches a user can turn
# off: an `import_scripts` list without 13, `genre_autofill`, and
# `advisory_auto_fetch`. Off, the import finishes WITHOUT that family and
# nothing else says so: the grader never requires a tag whose writer is
# switched off, so there is no gap and no prompt for it. So the import reports
# the skip itself — in its result, in the ONE line every surface prints
# (`chain_summary`) and in the log. The switch is never overruled to close the
# gap; it is only never silent.
SKIPPED_LYRICS = ("lyrics were not fetched: script 13 is not in the configured "
                  "chain (import_scripts)")
SKIPPED_GENRES = ("genres were not fetched: genre_autofill is off "
                  "(Settings → Import pipeline)")
SKIPPED_ADVISORY = ("advisory ratings were not fetched: advisory_auto_fetch is "
                    "off (Settings → Import pipeline)")


def _skipped_families(chain, cfg, review=()):
    """The families this import is configured NOT to fetch, with their switch.

    One reason per family, in the wizard's step order (genres, lyrics,
    advisory). Only a family whose fetch is a step of THIS pipeline appears:
    the lyric fetch needs script 13 in the chain, and the genre and advisory
    steps answer to their own switches. A family the USER kept for themselves
    (`import_review_families`, or review mode — *review*) is left out: that
    decision has its own report already (`import_policy.gaps` and the prompt
    that sends the user to the step), and naming it here would read as a second
    problem.

    A chain that is not going to run at all is reported by the chain's own keys
    (`chain_off`/`note`), so the two steps that ride on the chain — the lyric
    fetch and the advisory — are only named when there IS a chain that does not
    reach them.
    """
    out = []
    if not cfg.get("genre_autofill", True) and "genres" not in review:
        out.append(SKIPPED_GENRES)
    if chain and 13 not in chain and "lyrics" not in review:
        out.append(SKIPPED_LYRICS)
    if chain and not cfg.get("advisory_auto_fetch", True) and "advisory" not in review:
        out.append(SKIPPED_ADVISORY)
    return out


def _with_families(text, skipped):
    """*text* plus what this import was configured not to fetch.

    The import's own line has to carry both halves: what ran (or did not) and
    what was never asked to run. No skipped family, no change — so a shipped
    config keeps the note it always had.
    """
    extra = "; ".join(str(s) for s in (skipped or []) if s)
    if not extra:
        return text
    return f"{text} — {extra}" if text else extra


def chain_summary(result):
    """The ONE honest line about what an import's script chain did.

    Every surface that reports an import the caller did not watch — the job
    log, the wish notification, the queue row — says it with this, so the
    claim is the same everywhere and a chain that did not run can never be
    reported as one that did. "" for a result that says nothing about a chain
    at all (a caller's own fallback dict, the auto-importer's job result before
    the chain's background thread has finished), never a claim either way.

    A result that carries ``skipped_families`` (see :func:`_skipped_families`)
    says that too: an import which ran its chain and was configured not to
    fetch a family IS finished, and a reader of this line is the only one who
    can tell it from one that fetched everything.
    """
    res = result if isinstance(result, dict) else {}
    if isinstance(res.get("chain"), dict):
        # A caller that carries a whole finish_album result under "chain" —
        # the auto-importer's job result does — is asking about THAT result,
        # not about a chain field of its own.
        return chain_summary(res["chain"])
    if not any(k in res for k in ("chain", "scripts", "chained", "chain_off")):
        return ""
    skipped = res.get("skipped_families") or []
    scripts = list(res.get("scripts") or [])
    errors = [str(e) for e in (res.get("errors") or []) if str(e)]
    if not scripts:
        if res.get("note"):
            return str(res["note"])
        if res.get("chain_off") or not res.get("chain"):
            return _with_families("no script chain was run (import_auto_scripts is off)",
                                  skipped)
        return _with_families("the script chain did not run"
                              + (f": {errors[0]}" if errors else ""), skipped)
    failed = [s for s in scripts if isinstance(s, dict) and s.get("error")]
    total = len(scripts)
    if not failed:
        return _with_families(
            f"the script chain ran {total} script" + ("" if total == 1 else "s"),
            skipped)
    names = ", ".join(str(s.get("label") or s.get("id")) for s in failed[:3])
    return _with_families(
        f"the script chain ran {total - len(failed)} of {total} scripts — "
        f"{len(failed)} failed ({names})", skipped)


def _chain_off_note(cfg):
    """Why nothing ran, for a config that configures no chain."""
    if not cfg.get("import_auto_scripts", True):
        return "no script chain was run: import_auto_scripts is off"
    return ("no script chain was run: every configured script decides a family "
            "you kept for yourself")


def _clear_pending(path, cfg, *, chained, chain_off=False):
    """End a framework album's pending state — never fatal to the import.

    `server.pending_albums` owns the rule (the marker goes once the chain ran,
    or when this config runs none at all; a folder with no audio stays
    pending); this only makes the call safe from an import that must not fail
    because a marker could not be removed.
    """
    try:
        from server import pending_albums
        pending_albums.clear_if_filled(path, cfg, chained=chained,
                                       chain_off=chain_off)
    except Exception:
        traceback.print_exc()


def _announce_import(kind, path, out=None, cfg=None):
    """One notification for the import phase — never fatal to the import.

    `import_started` when `finish_album` picks an album up, `import_done` with
    the chain's own one-line summary when it has been over it. Emitted from the
    ONE entry point and the ONE exit every path passes (`_report_gaps`, which
    emits the notice LAST — after the gaps and the prompt it raises — so "done"
    never arrives while the pipeline is still deciding what to ask the user) —
    so the wizard, the bulk queue, the panel's import and the auto-import's
    chain all announce the same way, and a config that switched them off says so
    through `events._notify_configured`, like every other kind.
    """
    try:
        from server import events
        name = os.path.basename(os.path.normpath(path or "")) or path
        if kind == "import_started":
            events.emit("import_started", f"Importing {name}",
                        "Moving the album into the library and running the "
                        "configured chain.",
                        data={"path": path, "link": f"/album/{path}"}, config=cfg)
            return
        summary = chain_summary(out or {}) or "Import finished."
        events.emit("import_done", f"Imported {name}", summary,
                    data={"path": path, "link": f"/album/{path}"}, config=cfg)
    except Exception:
        traceback.print_exc()


def _dir_key(path):
    """One album folder as the key a grade sink is looked up by."""
    return os.path.normcase(os.path.normpath(str(path or "")))


def _report_gaps(out, cfg, policy, path, grade=None):
    """End an import: report what it could not finish, and raise ONE prompt.

    The single place a finished import says what it is missing, called by both
    ways out of :func:`finish_album` — the review stop and the end of the
    chain — so an album that still lacks a family always announces it, and
    announcing it is the same call whatever the import's mode was. `automatic`
    has already decided everything the configured sources could answer, so what
    is left is what no source supplied; `review` stopped before it decided.

    The gaps themselves come from `mlo.import_policy.gaps`, i.e. from the
    grader's own checks, so a prompt here and a grading failure there are the
    same statement. *grade* is the grade the chain's own Grade step just
    produced for this album, when it is an answer to the same question (see
    `finish_album`): `gaps` then derives the families from it instead of
    grading the album a second time for the same result. ``out["autonomy"]`` is
    the caller's view of it:

        {mode, stopped, missing: {family: {...}}, prompt: {...}|None}

    and is only present when there was something to report at all: an import
    that ran no chain decided nothing, so it reports nothing (the same early
    return the function always had for `import_auto_scripts` off).

    The ``import_done`` notice goes out at the very END of this function, after
    the gaps and the prompt are decided, and not before them: "Imported <album>"
    means the pipeline is over, and the gaps are the LAST thing it does — the
    phase that grades the album and raises the prompt that sends the user to the
    one family it could not supply. Emitted first (as it was) the notice arrived
    while that phase was still running, so the one surface that says "your album
    is ready" spoke before the album's own report existed.
    """
    gaps = import_policy.gaps(path, import_policy.effective_config(cfg),
                              steps={"advisory": out.get("advisory"),
                                     "cover": out.get("cover")},
                              grade=grade)
    out["autonomy"] = {
        "mode": policy["mode"],
        "stopped": policy["stop"],
        "missing": gaps,
        "prompt": import_autonomy.raise_prompt(
            path, cfg, gaps, mode=policy["mode"],
            reason="stopped" if policy["stop"] else "missing"),
    }
    _announce_import("import_done", path, out, cfg=cfg)
    return out


def _resolve_moved_album(path, album_mbid, album_rgid=""):
    """The folder *path* points at NOW — itself while it still exists.

    The chain's beets tagging / organize steps rename an album folder to its
    canonical layout, so the path a caller handed in can be gone by the time
    the chain returns. Callers build links from what they get back (the
    wizard's "Open album", the bulk queue's row, the Soulseek importer's job
    result), and a link to a folder that no longer exists is a dead end on a
    page that cannot say why. Resolution follows the album's own MusicBrainz
    id through the library index — the same self-repair the favorites and
    playlists use — and falls back to the stored path when the id is unknown
    (an album imported without a MusicBrainz link keeps its folder anyway).
    """
    if not path or os.path.isdir(path):
        return path
    ref = (album_mbid or album_rgid or "").strip().lower()
    if not ref:
        return path
    try:
        from server import mbresolve

        # heal_row re-reads the index and forces a rescan (once per its own
        # TTL window) when the stored path has vanished — no explicit
        # invalidate here, so a bulk import of N moved albums cannot turn
        # into N full library scans.
        moved = mbresolve.heal_row("album", path, ref)
    except Exception:
        return path
    if moved and os.path.isdir(moved) and os.path.normcase(moved) != os.path.normcase(path):
        print(f"[mlo] album moved during import: {os.path.basename(path)} -> {moved}")
        return os.path.normpath(moved)
    return path

# --------------------------------------------------------------------------- #
# AcoustID release check
# --------------------------------------------------------------------------- #
def _album_dir(path):
    """A track path's album folder, or the folder itself."""
    p = os.path.normpath(str(path))
    return p if os.path.isdir(p) else os.path.dirname(p)


def _audio_files(folder):
    """Every audio file under *folder*, dot-dirs pruned.

    Uses the download-side extension set (``server.soulseek_auto``): an import
    can be a folder of raw WAVs/APEs that the chain converts afterwards, and
    ``mlo.paths``'s library sets do not cover those.
    """
    from server.soulseek_auto import _AUDIO_EXTS
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in _AUDIO_EXTS:
                out.append(os.path.join(root, f))
    return sorted(out)


_ACOUSTID_ROW = {"release_group_id": None, "release_group_title": None,
                 "release_group_type": None, "artists": [], "score": None,
                 "matched": 0, "total": 0, "recordings": []}

# The provenance a value the file ALREADY carried is reported with when this
# run did not ask anyone about it (`force=False`). `sources` is normally "who
# stated this value"; an echoed value was stated by the file's own tag, and
# saying so is what keeps the readout from showing "source unknown" beside a
# 0 that a provider may well disagree with.
EXISTING_TAG = "existing-tag"

# `status[path]` — what this run did to the value it reports for *path*.
STATUS_WRITTEN = "written"       # the tag now holds what this run wrote
STATUS_UNCHANGED = "unchanged"   # decided, and the tag already read that
STATUS_EXISTING = "existing"     # echoed, nobody was asked this run
STATUS_GATED = "gated"           # the write gate refused the file


def fetch_advisories(paths, cfg=None, force=False):
    """Resolve and write ITUNESADVISORY for these albums / tracks.

    Each track is identified by EVERY ISRC its own tag states — a file may
    carry several (";"-joined, the way `AudioFile.get_tag` reads a repeated
    field), and they are all asked — or by the MusicBrainz recording ID the
    import just stamped, whose ISRCs MusicBrainz supplies. It is rated by
    `integrations.resolve_advisory_route`, which asks EVERY applicable source
    in one pass — Deezer and Spotify by ISRC, Apple's explicit-edition album
    route, Apple's exact-title song search — and merges them with
    `integrations.merge_advisory`: explicit (1) when any source states it,
    else a plain 0, else a clean edition's 2.

    When NO source states anything, the track is not assumed clean:
    `mlo.advisory.decide_advisory` runs the rest of the ladder — an
    instrumental track is 0, then the configured AI provider judges the lyrics
    (when one is set up and `advisory_ai_classify` is on, and only when every
    source came up with nothing at all), and finally `advisory_fallback`
    decides what an unstated advisory becomes (0 by default, 2, or nothing at
    all). A source that STATED a value ends the question: it is written as it
    stands, with that source's provenance, and the AI is never asked to
    second-guess it — see `mlo.advisory.decide_advisory`. When the AI does
    answer, its answer is recorded in that track's `answers` beside the
    providers', while `sources` keeps naming the one source that decided the
    value.
    A file that already carries a valid 0/1/2 is ECHOED, not re-asked — a
    rating the user or an earlier run settled is not overruled behind their
    back — and the echo carries its provenance: `sources[path]` reads
    "existing-tag" and `status[path]` "existing". Leaving `sources` empty put
    "source unknown" in the readout beside a value, which reads as "a source
    answered and the answer was lost" when the truth is "nobody was asked".

    `force=True` is the re-rate: such a file is asked anyway and what the
    sources state IS written (the write gate still applies, the album tag
    still derives as below). It is the way out of a value that an earlier run
    invented — the fallback writes 0 for a track nobody rated, and that 0 then
    outlived every provider that later knew better. Only EVIDENCE lowers a
    stored rating: a decided value that is that INVENTED `advisory_fallback`
    leaves an existing 0/1/2 standing, still reported as the file's own tag.
    A decided value equal to the one stored is decided, not rewritten.

    ALBUMITUNESADVISORY is DERIVED here for every album folder the call
    touched, from the per-track values by script 8's own rule
    (`mlo.autotag._derive_advisory`: any explicit → 1, else any clean edition
    → 2, else 0). Script 8 runs it behind the import chain, but the manual
    surfaces — the wizard's advisory step, the album page's Check, the
    tag-actions item — have no script 8 behind them, and leaving it to the
    script left every track rated and the album tag empty, which the grader
    reports as "Missing album tag ALBUMITUNESADVISORY".

    Returns ``{"updated": n, "values": {path: 0|1|2}, "sources": {path:
    provider}, "answers": {path: {source: 0|1}}, "albums": {folder: 0|1|2},
    "album_updated": n, "gated": n, "status":
    {path: "written"|"unchanged"|"existing"|"gated"}}``
    — `updated`/`values`/`sources`/`answers` are the per-track writes
    (`sources` is who stated each value — "instrumental", "ai-lyrics", "ai"
    and "fallback" included, so a value NOBODY stated is
    distinguishable from one a provider stated: the ladder's stage is named —
    and "existing-tag" when the reported value is the file's own), `answers`
    names every source that answered the track, the AI included, `albums`/
    `album_updated` are the album tag derived from them, and `gated` counts
    the files the ADVISORY write gate refused. `status` says what happened to
    each reported value THIS run — `written` (the tag now holds what this run
    wrote), `unchanged` (decided, and the tag already read it), `existing`
    (echoed without asking anyone: `force=False` on a file that had a value)
    or `gated` — so `updated == 0` can never be read as a re-rate that found
    nothing. When the gate refused EVERY file, `skipped` carries the reason
    instead of an empty result that would read as "nobody stated anything".
    """
    from mlo.audio import AudioFile
    from mlo.config import should_write_audio_tag
    from server import integrations as intg

    cfg = cfg or load_config()
    if not cfg.get("advisory_auto_fetch", True):
        return {"updated": 0, "values": {}, "sources": {}, "answers": {},
                "status": {},
                "skipped": "advisory_auto_fetch is off"}
    targets = []
    for p in paths or []:
        p = os.path.normpath(str(p))
        if os.path.isdir(p):
            targets.extend(_audio_files(p))
        elif os.path.isfile(p):
            targets.append(p)

    # Apple's album route prefers the collection whose trackCount matches the
    # album, which is the number of audio files sitting in each folder.
    per_folder = {}
    for path in targets:
        folder = os.path.dirname(path)
        per_folder[folder] = per_folder.get(folder, 0) + 1

    # ONE TRACK AT A TIME, deliberately. The pass was fanned out per file (the
    # shape `drop_arrived_values` and `_stamp_release` use for their own
    # per-file writes) and MEASURED slower on this album: Apple's interval is
    # global, and the sources are asked in a fixed order whose album-level
    # answers the first track warms for the rest. Eight lanes arriving at
    # `_apple_json` together turned a spacing that the serial pass never even
    # reached — it was already three seconds between calls — into 38 s of
    # sleep for the same twelve tracks, and the pass went 24 s to 41 s. The
    # concurrency that pays is one level up: the metadata/cover pair runs
    # beside this pass (`_files_step`), and instrumentals fan out per file
    # because LRCLIB's interval is 0.4 s, where overlap does win.
    updated = 0
    gated = 0
    values = {}
    sources = {}
    answers = {}
    status = {}
    for path in targets:
        try:
            af = AudioFile(path)
            if af.audio is None:
                continue
            current = str(af.get_tag("ITUNESADVISORY") or "").strip()
            if current in ("0", "1", "2") and not force:
                # Echo it, WITH its provenance: the value is the file's own
                # tag, this run asked nobody about it, and `status` says
                # exactly that. Reporting a value with no source at all
                # rendered as "source unknown" — which reads as "a source
                # answered and we lost it" when the truth is "nobody was
                # asked" (`force` is what asks).
                values[path] = int(current)
                sources[path] = EXISTING_TAG
                status[path] = STATUS_EXISTING
                continue
            if not should_write_audio_tag(cfg, "ITUNESADVISORY", filepath=path):
                gated += 1
                status[path] = STATUS_GATED
                continue
            route = intg.resolve_advisory_route(
                # The file's own tag, as stored: every ISRC on it is asked.
                isrc=str(af.get_tag("ISRC") or ""),
                recording_mbid=str(af.get_tag("MUSICBRAINZ_TRACKID") or "").strip(),
                title=str(af.get_tag("TITLE") or ""),
                artist=str(af.get_tag("ARTIST") or af.get_tag("ALBUMARTIST") or ""),
                album=str(af.get_tag("ALBUM") or ""),
                disc=af.get_tag("DISCNUMBER"),
                track=af.get_tag("TRACKNUMBER"),
                track_count=per_folder.get(os.path.dirname(path)),
                cfg=cfg,
            )
            # The provider route stated something, or nobody did — the ladder
            # settles the second case (an instrumental is 0, the AI judges the
            # lyrics when one is configured, and `advisory_fallback` is the last
            # resort; None means "write nothing", which is a legitimate answer).
            # A source that stated a value ends the question: it is written as
            # it stands and the AI is NOT asked about it. When the AI does
            # answer, it records it into the route's own map, so the reply names
            # every source behind the value — the AI included — while `sources`
            # keeps naming the one that decided it.
            track_answers = dict(route.get("answers") or {})
            decision = advisory.decide_advisory(
                cfg, value=route.get("value"), source=route.get("source") or "",
                answers=track_answers, path=path, af=af)
            value = decision.get("value")
            if value is None:
                continue
            if decision.get("fallback") and current in ("0", "1", "2"):
                # A re-rate (`force`) of a file that already carries a value,
                # for which NOBODY stated anything: the ladder's answer is the
                # INVENTED `advisory_fallback`, and an invented value must not
                # overwrite a real one — the stored rating stands, reported as
                # the file's own. Only evidence lowers a rating.
                values[path] = int(current)
                sources[path] = EXISTING_TAG
                status[path] = STATUS_UNCHANGED
                continue
            values[path] = int(value)
            if decision.get("source"):
                sources[path] = decision["source"]
            if track_answers:
                answers[path] = track_answers
            if str(value) == current:
                status[path] = STATUS_UNCHANGED
            elif af.set_tag("ITUNESADVISORY", str(value)):
                updated += 1
                status[path] = STATUS_WRITTEN
            else:
                # `set_tag` refused: the file still reads what it did, and a
                # write that did not happen is not counted as one.
                status[path] = STATUS_UNCHANGED
        except Exception:
            continue

    if gated and not values:
        # Nothing at all was recorded because the write gate refused every file
        # (Settings → "Set advisory automatically" is the ADVISORY family's
        # master switch, or the per-filetype toggle). Say so: the all-zero
        # reply this used to return reads exactly like "nobody stated
        # anything", and a caller cannot act on a lie. Nothing was looked up
        # and nothing was written, so there is no album tag to derive either.
        return {"updated": 0, "values": {}, "sources": {}, "answers": {},
                "status": status, "albums": {}, "album_updated": 0,
                "gated": gated,
                "skipped": (f"{gated} track(s) not rated: writing "
                            "ITUNESADVISORY is off for their file type "
                            "(auto_advisory / audio_tag_writes['ADVISORY'])")}

    # The album tag, from the values this pass just settled (and from whatever
    # the album's other tracks already carried — the rule is album-wide).
    from mlo.autotag import _derive_advisory
    albums = {}
    album_updated = 0
    album_gated = 0
    for folder in sorted(per_folder):
        try:
            files = _audio_files(folder)
            handles = {}
            for p in files:
                try:
                    af = AudioFile(p)
                except Exception:
                    continue
                if af.audio is None:
                    continue
                handles[p] = af
            if not handles:
                continue
            advisories = [str(values[p]) if p in values
                          else str(handles[p].get_tag("ITUNESADVISORY") or "").strip()
                          for p in handles]
            want = _derive_advisory(advisories)
            albums[folder] = want
            for p, af in handles.items():
                if str(af.get_tag("ALBUMITUNESADVISORY") or "").strip() == str(want):
                    continue
                if not should_write_audio_tag(cfg, "ALBUMITUNESADVISORY", filepath=p):
                    album_gated += 1
                    continue
                if af.set_tag("ALBUMITUNESADVISORY", str(want)):
                    album_updated += 1
        except Exception:
            continue

    if updated or album_updated:
        # Scoped to the albums this pass wrote (spec R155): every other album's
        # cached tags are still valid, so they stay.
        _invalidate_caches(*albums)
    out = {"updated": updated, "values": values, "sources": sources,
           "answers": answers, "status": status, "albums": albums,
           "album_updated": album_updated, "gated": gated,
           "album_gated": album_gated}
    if album_gated and not album_updated:
        # The per-track values landed and the ALBUM tag was refused: that tag
        # answers to script 8's derivation switch ("Auto Album Advisory",
        # `auto_advisory`), which is off. Say it — an album whose tracks are all
        # rated and whose album tag is empty fails `grade_check_album_tags`,
        # and the caller has to know which switch to flip.
        out["skipped"] = (f"{album_gated} album tag(s) not written: "
                          "ALBUMITUNESADVISORY derivation is off (auto_advisory)")
    return out


def fetch_instrumentals(paths, cfg=None):
    """Resolve and write INSTRUMENTAL (0/1) for these albums / tracks.

    The detection itself is ``server.instrumental.detect_instrumental``, which
    cross-references every available source (LRCLIB, Spotify audio-features,
    the file's own title marker, lyrics evidence) and merges them: any source
    saying instrumental wins, otherwise any source saying not-instrumental,
    otherwise NO answer and no write. A file that already carries 0/1 is left
    alone (the user's manual edit wins).

    Returns ``{"updated": n, "values": {path: 0|1}, "evidence": {path:
    {source: 0|1}}}``.
    """
    from mlo.audio import AudioFile
    from mlo.config import should_write_audio_tag
    from server import instrumental as inst

    cfg = cfg or load_config()
    if not cfg.get("instrumental_auto_fetch", True):
        return {"updated": 0, "values": {}, "evidence": {},
                "skipped": "instrumental_auto_fetch is off"}
    targets = []
    for p in paths or []:
        p = os.path.normpath(str(p))
        if os.path.isdir(p):
            targets.extend(_audio_files(p))
        elif os.path.isfile(p):
            targets.append(p)

    updated = 0
    values = {}
    evidence = {}

    # The same fan-out the advisory pass above uses, for the same reason: one
    # file per worker (its own container rewrite), while the sources behind
    # `detect_instrumental` — LRCLIB's own flag, Spotify's audio features when
    # it is configured — answer per track and overlap each other's spacing
    # instead of queueing up behind each other's latency. The per-source
    # answers are memoized (`integrations._advisory_cached`), so a source asked
    # twice in a run still costs one request.
    def _one(path):
        """(path, row) for one file; the fold below is the only mutator."""
        row = {"updated": 0, "value": None, "answers": None}
        try:
            hits = inst.detect_instrumental([path], cfg)
            hit = next(iter(hits.values()), None) or {}
            row["answers"] = hit.get("answers") or None
            value = hit.get("value")
            if value is None:
                return path, row
            af = AudioFile(path)
            if af.audio is None:
                return path, row
            current = str(af.get_tag("INSTRUMENTAL") or "").strip()
            if current in ("0", "1"):
                row["value"] = int(current)
                return path, row
            if not should_write_audio_tag(cfg, "INSTRUMENTAL", filepath=path):
                return path, row
            if str(value) != current and af.set_tag("INSTRUMENTAL", str(value)):
                row["updated"] = 1
            row["value"] = value
        except Exception:
            return path, row
        return path, row

    workers = worker_count(cfg, default=8, maximum=8, items=len(targets))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(_one, targets))
    for path, row in rows:
        updated += row["updated"]
        if row["answers"]:
            evidence[path] = row["answers"]
        if row["value"] is not None:
            values[path] = row["value"]
    if updated:
        _invalidate_caches(*[os.path.dirname(p) for p in values])
    return {"updated": updated, "values": values, "evidence": evidence}


# --------------------------------------------------------------------------- #
# Metadata step — artist image / artist description / album description
# --------------------------------------------------------------------------- #
# With metadata_review ON the candidates are STAGED here (nothing is written
# until the user applies one through POST /api/metadata/apply); with it OFF
# the best candidate is saved as part of the import.
_METADATA_REVIEW_NAME = "metadata_review.json"


def _metadata_review_path(cfg=None):
    from mlo.paths import app_data_dir
    cfg = cfg or load_config()
    d = app_data_dir(str(cfg.get("music_folder") or "") or None)
    return os.path.join(d, _METADATA_REVIEW_NAME) if d else None


def _review_key(album_dir):
    return os.path.normpath(str(album_dir)).replace("\\", "/").lower()


def _review_load(path):
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _album_mbids(album_dir):
    """(album id, release-group id) from an album's own tags, either "".

    Lowercased for comparison; read off `_tag_candidate`, which caps its scan
    at five files for the same reason (album-level tags are uniform across the
    tracks). A FRAMEWORK album has no tags at all, so the ids its marker was
    CREATED with stand in — otherwise the add-time pre-fetch could only search
    the cover sources by name, and the release's own front cover (asked by id)
    would never be one of the candidates.
    """
    cand = _tag_candidate(album_dir)
    album = cand["release_id"].lower()
    rg = cand["release_group_id"].lower()
    if album or rg:
        return album, rg
    try:
        from mlo.paths import load_pending
        info = load_pending(album_dir) or {}
    except Exception:
        return album, rg
    return (str(info.get("release_id") or "").lower(),
            str(info.get("release_group_id") or "").lower())


def staged_metadata(album_dir, cfg=None):
    """The staged review entry for an album ({} when none).

    Looked up three ways, in order: the exact path key, the album FOLDER name,
    then the album's MusicBrainz identity (release id, then release-group id).
    The identity is what makes the record survive the import chain moving the
    album — beets/organize relocates it to the canonical folder, so the path
    the import staged under can be a folder that no longer exists by the time
    the album page asks. Two albums sharing a folder name is the one ambiguous
    case; the first match wins, which is no worse than a record nobody finds.
    """
    path = _metadata_review_path(cfg)
    if not path or not os.path.isfile(path):
        return {}
    data = _review_load(path)
    hit = data.get(_review_key(album_dir)) or {}
    if hit:
        return hit
    leaf = os.path.basename(os.path.normpath(str(album_dir))).lower()
    if leaf:
        for key, entry in data.items():
            if os.path.basename(key.rstrip("/")) == leaf:
                return entry or {}
    album_id, rg_id = _album_mbids(album_dir)
    if album_id or rg_id:
        for entry in data.values():
            covers = (entry or {}).get("covers") or {}
            staged_album = str(covers.get("album_id") or "").strip().lower()
            staged_rg = str(covers.get("release_group") or "").strip().lower()
            if album_id and staged_album == album_id:
                return entry or {}
            if rg_id and staged_rg and staged_rg == rg_id:
                return entry or {}
    return {}


def stage_metadata(album_dir, entry, cfg=None):
    """Record an album's staged candidates (entry=None clears the entry)."""
    import json
    path = _metadata_review_path(cfg)
    if not path:
        return
    data = _review_load(path) if os.path.isfile(path) else {}
    key = _review_key(album_dir)
    if entry is None:
        data.pop(key, None)
    else:
        data[key] = entry
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        traceback.print_exc()


def _album_identity(album_dir):
    """(artist, album) from the folder's own tags (folder name as fallback)."""
    from mlo.audio import AudioFile
    for path in _audio_files(album_dir):
        try:
            af = AudioFile(path)
            if af.audio is None:
                continue
            artist = str(af.get_tag("ALBUMARTIST") or af.get_tag("ARTIST") or "").strip()
            album = str(af.get_tag("ALBUM") or "").strip()
            if artist or album:
                return artist, album
        except Exception:
            continue
    return "", os.path.basename(str(album_dir).rstrip("\\/"))


def album_identity(album_dir, cfg=None):
    """(artist, album) for an album folder, tags first, marker second.

    A folder whose audio has arrived states its own identity; a FRAMEWORK album
    (`server.pending_albums`, added to the library before any audio exists) has
    no tags to read at all, and its marker carries the release's artist and
    title. Without this the add path's pre-fetch would search every provider
    for ""/"" — and the metadata and cover steps would report "nothing found"
    for an album whose identity the app itself wrote down.
    """
    artist, album = _album_identity(album_dir)
    if artist:
        return artist, album
    try:
        from mlo.paths import load_pending
        info = load_pending(album_dir) or {}
    except Exception:
        info = {}
    artist = str(info.get("artist") or "").strip() or artist
    album = str(info.get("title") or "").strip() or album
    return artist, album


def apply_metadata(album_dir, cfg=None):
    """Fetch and store the best artist image / descriptions for an album.

    The import chain's metadata step and the manual apply route share this:
    it honours the per-feature switches (artist_image_enabled,
    artist_description_enabled, album_description_enabled — what the app may
    fetch on the user's behalf) and NEVER overwrites stored content. Returns
    {"artist_image", "artist_description", "album_description"} with the paths
    written (None where nothing was written). Never raises."""
    from mlo import artistdata
    from server import discovery
    from server import integrations as intg

    cfg = cfg or load_config()
    out = {"artist_image": None, "artist_description": None, "album_description": None}
    artist, album = album_identity(album_dir, cfg)
    if not artist:
        return out
    folder = artistdata.artist_dir(cfg, artist)

    if folder and cfg.get("artist_image_enabled", True) and not artistdata.has_image(folder):
        hit = discovery.artist_image(artist, mbid=artistdata.folder_mbid(folder), cfg=cfg)
        if hit and hit.get("url"):
            try:
                data, _ctype = intg.fetch_image_bytes(hit["url"])
                out["artist_image"] = artistdata.save_image(
                    folder, data, cfg, source=hit.get("source") or "auto",
                    source_url=hit.get("url"), kind="artist",
                    label=hit.get("label"))
            except Exception:
                traceback.print_exc()

    if folder and cfg.get("artist_description_enabled", True) and not artistdata.has_description(folder):
        found = discovery.artist_description(artist, mbid=artistdata.folder_mbid(folder), cfg=cfg)
        if found and str(found.get("text") or "").strip():
            out["artist_description"] = artistdata.write_description(
                folder, found["text"], cfg=cfg, source=found.get("source"),
                source_url=found.get("source_url"), kind="artist")
            artistdata.write_provenance(folder, {
                "description_source": found.get("source"),
                "description_source_url": found.get("source_url"),
                "description_title": found.get("title")}, kind="artist", cfg=cfg)

    if (album and cfg.get("album_description_enabled", True)
            and not artistdata.has_description(album_dir)):
        found = discovery.album_description(artist, album, cfg=cfg)
        if found and str(found.get("text") or "").strip():
            out["album_description"] = artistdata.write_description(
                album_dir, found["text"], cfg=cfg, source=found.get("source"),
                source_url=found.get("source_url"), kind="album")
            artistdata.write_provenance(album_dir, {
                "description_source": found.get("source"),
                "description_source_url": found.get("source_url"),
                "description_title": found.get("title")}, kind="album", cfg=cfg)

    if any(out.values()):
        # The album it wrote, and the artist folder when it filled one: the
        # artist image and the description are cached art like an album's, so a
        # write there is scoped the same way (spec R155).
        _invalidate_caches(album_dir, folder)
    return out


def run_metadata_step(album_dir, cfg=None):
    """The import chain's metadata step (metadata_auto_fetch / metadata_review).

    auto-fetch off → nothing happens. On with review ON → the candidates are
    staged for the user and nothing is written until their apply call. On with
    review off → the best candidate is saved now. Never fatal."""
    from server import discovery

    cfg = cfg or load_config()
    if not cfg.get("metadata_auto_fetch", True):
        return {"staged": False, "applied": {}}
    if cfg.get("metadata_review", False):
        artist, album = album_identity(album_dir, cfg)
        if _staged_metadata_held(album_dir, cfg, artist, album):
            # The ADD ran this same candidate fetch for the SAME artist and
            # album and staged the result for the user to pick from (spec
            # R154): fetching it again would replace one identical record with
            # another. The staged entry stands, and the pick screen the user
            # already has is the answer this step would produce.
            return {"staged": True, "applied": {}}
        try:
            candidates = discovery.metadata_candidates(artist, album, cfg=cfg)
        except Exception:
            traceback.print_exc()
            return {"staged": False, "applied": {}}
        stage_metadata(album_dir, {"artist": artist, "album": album,
                                   "candidates": candidates}, cfg)
        return {"staged": True, "applied": {}}
    try:
        return {"staged": False, "applied": apply_metadata(album_dir, cfg)}
    except Exception:
        traceback.print_exc()
        return {"staged": False, "applied": {}}


# Cover auto-fetch: which album this is decides who is asked. A release-group
# id is an identity — the Cover Art Archive answers about it by id, with no
# name guessing at all — while an album without one is found by its names.
# Both are asked when both are known: the meta-search carries the big store
# artwork, the identity carries the release's own front cover, and
# `mlo.cover_choice` (the ONE cover policy) decides between them.
COVER_FETCH_TIMEOUT = 30.0

# How many candidates the finder is asked for. A review hands the user a
# screenful to pick from; the unattended path ranks the SAME set and writes the
# winner, so the cover that lands is the best of what exists rather than
# whatever answered first. 20+ CDNs is what the finder deals in; a screenful
# plus its ranked tail is the whole point of a pick-one screen.
COVER_REVIEW_LIMIT = 12


def _album_cover_present(album_dir):
    """Whether the album already has cover art, so nothing is fetched.

    `COVER_NAMES` is the grader's own set: "has a cover" here means exactly
    what grading means by one. `tagcache.cover_bytes` is the reader the UI
    serves art from and also knows the formats it can hand out; either answer
    means there is nothing to do.
    """
    from mlo.grader import COVER_NAMES

    try:
        if {n.lower() for n in os.listdir(album_dir)} & COVER_NAMES:
            return True
    except OSError:
        pass
    try:
        return tagcache.cover_bytes(album_dir)[0] is not None
    except Exception:
        return False


def _album_track_count(album_dir):
    """How many tracks this album says it has, or None when it does not say.

    This is the third of the three facts a cover candidate is VERIFIED against
    (`mlo.cover_choice` rule 2): a row whose own release has a different
    tracklist is a different edition (a reissue, a compilation), and its
    artwork is only a candidate for this album, not the answer. Two sources,
    both the album's OWN statement of its tracklist:

    * the recorded release manifest (`mlo.paths.load_expected_tracks` — the
      release's own tracklist, written by the add path, the import and script
      15), which is the release's own count even while the folder is still
      filling up;
    * the files' own TRACKTOTAL/TOTALTRACKS tags, read off the same five files
      (and for the same reason) `_tag_candidate` reads.

    A folder's FILE COUNT is deliberately not used: a partial import or one
    disc of a set would contradict every correct release, which is the
    opposite of what this number is for. Nothing claiming a count leaves the
    check neutral — the row is neither rewarded nor blamed for it.
    """
    try:
        from mlo.paths import load_expected_tracks
        rows = (load_expected_tracks(album_dir) or {}).get("tracks") or []
    except Exception:
        rows = []
    if rows:
        return len(rows)
    from mlo.audio import AudioFile

    for path in _audio_files(album_dir)[:5]:
        try:
            af = AudioFile(path)
            if af.audio is None:
                continue
            for key in ("TRACKTOTAL", "TOTALTRACKS"):
                try:
                    n = int(str(af.get_tag(key) or "").strip())
                except (TypeError, ValueError):
                    continue
                if n > 0:
                    return n
        except Exception:
            continue
    return None


def _cover_side(candidate):
    """The shorter side a chosen candidate was measured at, or None.

    `width`/`height` are what the image's own header bytes said (the finder
    probes them, `mlo.cover_choice` ranks on them) — and `None` means the image
    was never measured, which is not the same thing as "big enough".
    """
    row = candidate if isinstance(candidate, dict) else {}
    try:
        w = int(row.get("width") or 0)
        h = int(row.get("height") or 0)
    except (TypeError, ValueError):
        return None
    return min(w, h) if (w > 0 and h > 0) else None


def cover_candidates(album_dir, cfg=None, *, limit=None):
    """The ranked cover candidates for one album, or None with nothing to ask.

    The ONE place the cover finder is asked on an album's behalf: the import
    chain's cover step, the add path's pre-fetch and the album page's cover
    search all rank the same candidate set with the same policy
    (`mlo.cover_choice`), so the image an unattended import lands and the image
    the user is offered first cannot disagree.

    The album's identity decides who is asked — the release's own id and its
    release group go to the Cover Art Archive by identity, the names go to the
    meta-search — and it is also what the candidates are CHECKED against
    (`mlo.cover_choice` rule 2): a name search answers with karaoke, tribute
    and other-album rows too, and a row whose own release names another artist
    or another album is rejected rather than ranked. The result is
    `mlo.cover_choice.cover_payload`'s body (chosen, ranked candidates, notes,
    policy, the identity they were checked against) plus the identity used, so
    a caller can record it (`stage_cover_candidates`) or show it as it is.
    """
    from mlo import cover_choice
    from server import integrations as intg

    cfg = cfg or load_config()
    artist, album = album_identity(album_dir, cfg)
    if not artist and not album:
        return None
    album_id, rg = _album_mbids(album_dir)
    if not rg and album_id:
        # `_album_mbids` reads TAGS (and a pending marker) only, on purpose: it
        # sits on hot paths that must stay offline. An album that states its
        # RELEASE but not its release group therefore arrived here with no group
        # to ask the Cover Art Archive about — and the group's own front cover is
        # the REFERENCE the cover policy is built around (`mlo.cover_choice` rule
        # 1), so the pick could only ever be a name-searched row or one edition's
        # sleeve. The release's own MusicBrainz record states its group, and one
        # cached lookup here buys the reference back.
        #
        # Scoped to this function for exactly that reason: no hot path pays for
        # it, and a lookup that fails (an outage, a rate limit, a release
        # MusicBrainz does not have) leaves the identity as it was — a cover
        # search must never fail because of this.
        try:
            rel = intg.release_lookup(album_id) or {}
            rg = str(rel.get("release_group_id") or "").strip().lower() or rg
        except Exception:
            pass
    found = intg.cover_search(
        artist, album, limit=limit or COVER_REVIEW_LIMIT,
        timeout=COVER_FETCH_TIMEOUT, cfg=cfg,
        release_group_mbid=rg, release_mbid=album_id)
    identity = {"artist": artist, "album": album,
                "tracks": _album_track_count(album_dir)}
    payload = cover_choice.cover_payload(
        found.get("results") or [], cfg,
        sources=found.get("sources"), provider=found.get("provider"),
        identity=identity)
    payload["artist"] = artist
    payload["album"] = album
    payload["album_id"] = album_id
    payload["release_group"] = rg
    return payload


def _staged_metadata_held(album_dir, cfg, artist, album):
    """Whether the ADD's staged METADATA candidates already answer this album.

    With `metadata_review` on, the add staged the artist/description candidates
    for the user to pick from (`discovery.metadata_candidates` — the image
    chain plus the description chain, 3.0-5.3 s measured) and the import used to
    fetch the same thing again. The staged entry IS that answer, so the step
    only fetches again when the entry does not describe THIS album: the artist
    and album the candidates were fetched for must be the ones this album has.
    """
    entry = staged_metadata(album_dir, cfg) or {}
    if not (entry.get("candidates") or {}):
        return False
    have = (str(entry.get("artist") or "").strip().lower(),
            str(entry.get("album") or "").strip().lower())
    want = (str(artist or "").strip().lower(), str(album or "").strip().lower())
    return have == want and any(have)


def stage_cover_candidates(album_dir, payload, cfg=None):
    """Record a ranked candidate set as the album's staged cover review.

    The pick screen's own order: the winner first with the reasons that put it
    there, the winner recorded as ``chosen``, every alternative behind it with
    the reason it lost, and the finder's notes — including any source that was
    skipped (a source needing a key says so rather than being worked around).
    The identity the candidates were checked against is recorded too, so the
    screen can say what a candidate had to be (and why a karaoke or
    other-album row sits at the bottom with its rejection). Other keys of a
    staged entry (the metadata step's own candidates) are kept.
    """
    cfg = cfg or load_config()
    ranked = payload.get("candidates") or []
    chosen = payload.get("chosen")
    notes = list(payload.get("notes") or [])
    if not ranked:
        # Nothing at all to pick from: a staged entry with no options is not a
        # review, it is an empty screen. Say so instead.
        return {"fetched": False, "applied": {}, "staged": False,
                "source": payload.get("provider"), "candidates": 0,
                "choice": None, "notes": notes,
                "note": ("no cover found — " + (notes[-1] if notes else
                                                "no source answered with a candidate"))}
    entry = dict(staged_metadata(album_dir, cfg))
    entry["covers"] = {
        "artist": payload.get("artist"),
        "album": payload.get("album"),
        # The release id too: with it (or the release group) the entry is found
        # again after the import chain relocates the album, so the review
        # record is never orphaned by a rename.
        "album_id": payload.get("album_id"),
        "release_group": payload.get("release_group"),
        "identity": payload.get("identity") or {},
        "provider": payload.get("provider"),
        "staged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": ranked,
        "chosen": chosen,
        "notes": notes,
        "policy": payload.get("policy") or {},
    }
    stage_metadata(album_dir, entry, cfg)
    count = int(payload.get("candidate_count") or 0)
    note = (f"{count} cover candidates to pick from — best: "
            f"{chosen['source']} ({chosen['reasons'][-1]})" if chosen else
            "no cover to pick — " + (notes[-1] if notes else ""))
    return {"fetched": False, "applied": {}, "staged": True,
            "source": payload.get("provider"), "candidates": count,
            "choice": chosen, "notes": notes, "note": note}


def run_cover_step(album_dir, cfg=None):
    """The import chain's cover step (cover_auto_fetch / cover_review).

    An album that arrived without cover art gets one, found the way the cover
    page finds them and stored by the same writer an upload goes through — so
    the file that lands is already at the library's own size and encoding.
    WHICH one lands is `mlo.cover_choice`'s answer and nothing else (see
    `cover_candidates`): the release's own front cover first, then the size
    against the configured target, then the configured source order, then
    format and aspect, with the provider's own order as the last tiebreak.
    Every source that refused, had nothing or was skipped says so in ``notes``.

    With `cover_review` on the ranked candidates are STAGED instead (the import
    writes the winner when it is off, which is the shipped default) —
    under ``entry["covers"]`` in the same review file the metadata step uses,
    other keys of the album's entry kept — and NOTHING is written; the user
    picks one and the UI writes it through ``POST /api/cover/fromurl``. With it
    off the winner is applied here, exactly as before.

    The floor is re-checked HERE, at the write, and not only where the winner
    was picked: `mlo.cover_choice` already refuses a candidate below the
    library's minimum (and one whose size was never measured while that minimum
    is set), so a below-minimum image can only arrive here if that ranking is
    ever wrong — and the file this step writes is the one that stays in the
    library and the grader then flags. Nothing is downloaded or stored when it
    does not clear the floor, and the note says so. A hand-applied cover
    (``POST /api/cover/fromurl``) is deliberately NOT gated: the user picked
    that exact image, and the picker lists below-floor rows for exactly that
    reason (`server.main._cover_metrics` reports the shortfall as a warning).

    A framework album's PLACEHOLDER cover does not count as "has a cover": it is
    the release group's own artwork, kept as a stand-in until this step finds
    the release's, and treating it as the album's cover would mean never
    fetching the real one. It is not deleted on the way in either (the folder
    must never be left without a cover between the two), only once a real image
    has been written over or beside it. When nothing clears the floor the
    placeholder STAYS — a below-floor candidate is a worse cover than the one
    already there — and the note says which of the two it kept.

    Never fatal, and never silent about a cover it could not get: the result is
    ``{"fetched", "applied", "source", "note", "staged", "candidates",
    "choice", "notes"}`` — the same shape `run_metadata_step` hands back to
    `finish_album`, ``note`` the line a caller can show (it names the source
    and why it won), ``staged``/``candidates`` how many options the user was
    handed instead of a written file, ``choice`` the winning candidate with its
    reasons, and ``notes`` every source's own outcome.
    """
    cfg = cfg or load_config()
    out = {"fetched": False, "applied": {}, "source": None, "note": "",
           "staged": False, "candidates": 0, "choice": None, "notes": []}
    # Whether the album's cover art is still only the framework album's own
    # placeholder (see the docstring). Declared out here because every way out
    # of this function reports it.
    placeholder = False
    if not cfg.get("cover_auto_fetch", True):
        return out
    try:
        # Our own placeholder is not a cover for this purpose (see the
        # docstring): it is what the ADD left standing in for the artwork this
        # step is here to fetch.
        from server import pending_albums

        placeholder = pending_albums.placeholder_cover_present(album_dir)
        if placeholder:
            out["notes"].append(
                "the framework album's placeholder cover does not count as the "
                "album's cover — the release's own artwork is fetched over it")
        if _album_cover_present(album_dir) and not placeholder:
            return out
        # The candidates are ALWAYS ranked fresh here, and that is deliberate
        # (spec R154): the staged review record the add leaves behind is a
        # PICK SCREEN (any surface may restage it, it is found by folder name or
        # MB id as well as by path, and it carries no proof of which search, for
        # which album, produced it), so ranking THIS import's set would mean
        # writing an image chosen from another search's rows — the pick must be
        # the best of what exists for the album being imported, which is the one
        # rule both modes share.
        payload = cover_candidates(album_dir, cfg)
        if payload is None:
            out["note"] = "no artist/album tags to search by"
            return _kept_placeholder(out, placeholder)
        if bool(cfg.get("cover_review", False)):
            return _kept_placeholder(
                stage_cover_candidates(album_dir, payload, cfg), placeholder)
        chosen = payload.get("chosen")
        out["notes"] = list(payload.get("notes") or []) + list(out["notes"])
        out["candidates"] = int(payload.get("candidate_count") or 0)
        out["source"] = payload.get("provider")
        out["choice"] = chosen
        if not chosen:
            out["note"] = ("no cover found" if not payload.get("candidates") else
                           "no candidate could be used — "
                           + ((payload.get("notes") or [""])[-1]))
            return _kept_placeholder(out, placeholder)
        # The WRITE gate (see the docstring): the floor is the same number the
        # pick was ranked with — `policy_config` is where `cover_minimum` is
        # derived, so this reads it from its one definition rather than from
        # the payload the ranking left behind.
        from mlo import cover_choice
        floor = int(cover_choice.policy_config(cfg).get("cover_minimum") or 0)
        side = _cover_side(chosen)
        if floor and (side is None or side < floor):
            measured = (f"{side}×{side} px, measured from the image itself"
                        if side is not None else "size never measured")
            out["note"] = (f"the chosen cover ({measured}) is below the minimum "
                           f"{floor}×{floor} — nothing was written")
            return _kept_placeholder(out, placeholder)
        url = str(chosen.get("big") or "")
        artist = payload.get("artist") or ""
        album = payload.get("album") or ""
        rg = payload.get("release_group") or ""
        # main.py imports this module, so the cover writer is reached back into
        # lazily — exactly how server.api_discovery reaches `_in_music_folder`.
        from server.main import _cover_url_bytes, _write_cover_bytes, _sniff_image_ext

        # `substitute=False`: the policy chose THIS candidate for the album, so
        # a provider that is not it must never answer — an image the ranking
        # never saw is not the pick, and writing one would put art on the album
        # that nobody (user or policy) asked for. Its own image, or nothing.
        try:
            data, ctype = _cover_url_bytes(url, substitute=False)
        except ValueError as e:
            # An ordinary outcome for a URL a CDN has stopped serving, not a
            # failure of the step: the note says what happened and why nothing
            # was written, and the next run (or the user's own pick) tries again.
            out["note"] = str(e)
            return _kept_placeholder(out, placeholder)
        if not data:
            out["note"] = ("the chosen cover's own image came back empty — "
                           "nothing was written in its place")
            return _kept_placeholder(out, placeholder)
        res = _write_cover_bytes(album_dir, "cover", _sniff_image_ext(data, ctype), data)
        # A real cover of the release's own is on disk now, so the framework
        # album's placeholder goes — and this is the ONLY moment it may: the
        # write above may have landed under another extension ("cover.png" over
        # our "cover.jpg"), which would otherwise leave the album with two
        # covers, and `pending_albums` refuses to take the folder's last cover
        # anyway. Never fatal: a placeholder left behind is a cover, not a
        # failure of the import.
        if placeholder:
            try:
                pending_albums.drop_placeholder_cover(album_dir)
            except Exception:
                traceback.print_exc()
        out["applied"] = {"cover": res.get("path")}
        out["fetched"] = True
        out["note"] = (f"cover fetched from {chosen['source'] or 'the image url'} "
                       f"(best of {out['candidates']} candidate(s) — "
                       f"{chosen['reasons'][-1]})")
    except Exception as e:
        traceback.print_exc()
        out["note"] = f"cover step failed: {e}"
    return _kept_placeholder(out, placeholder)


def _kept_placeholder(out, placeholder):
    """*out* with the line that says the album keeps the placeholder cover.

    Nothing of the album's own was written (the caller only reaches here with
    ``applied`` empty), so the folder still holds the release-group artwork the
    ADD fetched. That is the difference between "no cover found" and "no cover
    at all", and the owner's report was the second one — the note names which
    image is standing in the folder, so a user reading it knows the album has
    art and where it came from."""
    if not placeholder or out.get("applied"):
        return out
    note = str(out.get("note") or "").strip()
    kept = "the album keeps the framework album's own release-group artwork"
    out["note"] = f"{note} — {kept}" if note else kept
    return out


def prefetch_links(album_dir, cfg=None):
    """Resolve an album's / artist's RateYourMusic links onto its marker.

    The tag half of the links step cannot run before the audio exists, but the
    RESOLUTION can: the same resolver the stamp uses (`integrations.rym_links`,
    MusicBrainz-relation first), the same switch (`rym_links_auto`), and a link
    RYM itself confirmed is recorded in the framework marker so the album's
    page shows it from the moment it is added. Nothing already recorded is ever
    overwritten (a link the user pasted wins), and nothing is written when the
    lookup resolves nothing. Returns ``{"album", "artist", "note", "saved"}``;
    never raises.
    """
    cfg = cfg or load_config()
    out = {"album": None, "artist": None, "note": "", "saved": False}
    if not cfg.get("rym_links_auto", True):
        out["note"] = "skipped: rym_links_auto is off"
        return out
    from mlo.paths import load_pending, save_pending
    from server import integrations as intg

    info = load_pending(album_dir) or {}
    if not info:
        out["note"] = "not a framework album — no marker to record a link in"
        return out
    have = dict(info.get("links") or {})
    artist, album = album_identity(album_dir, cfg)
    if not artist and not album:
        out["note"] = "no artist/album to look a link up for"
        return out
    if not (have.get("album") and have.get("artist")):
        links = {}
        try:
            links = intg.rym_links(
                artist, album, cfg=cfg,
                mbid=str(info.get("release_group_id")
                         or info.get("release_id") or "")) or {}
        except Exception:
            traceback.print_exc()
            out["note"] = "the link lookup failed"
            return out
        out["note"] = str(links.get("note") or "")
        merged = {"album": have.get("album") or links.get("album"),
                  "artist": have.get("artist") or links.get("artist")}
        if any(merged.values()) and merged != have:
            info["links"] = merged
            out["saved"] = bool(save_pending(album_dir, info))
            have = merged
    out["album"], out["artist"] = have.get("album"), have.get("artist")
    return out


def prefetch_album(album_dir, cfg=None):
    """What a freshly added album can have BEFORE its audio exists.

    "Add to library" already creates the folder, its release manifest and a
    placeholder cover; this is the rest of what the album's page shows, fetched
    the moment the album is asked for rather than when the download lands: its
    RateYourMusic links, its metadata (the ARTIST image, the artist and album
    descriptions — `metadata_review` staging them instead when that is on) and
    the ranked cover candidates with the policy's winner marked.

    Every step is the import chain's own function and its own switch
    (`rym_links_auto`, `metadata_auto_fetch`, `cover_auto_fetch`), so nothing
    here is a second fetcher and nothing is fetched that the import would not
    have fetched anyway. Never fatal: the folder is already a real library
    album, and a provider that refuses must not fail the add. The album's
    identity comes from the marker when there is no audio to read tags from
    (`album_identity`).
    """
    cfg = cfg or load_config()
    out = {"links": None, "metadata": None, "cover": None}
    # The same effective config the import chain runs under, so a family the
    # user kept for themselves is not decided here either.
    try:
        from mlo import import_policy
        run_cfg = import_policy.effective_config(cfg)
    except Exception:
        traceback.print_exc()
        run_cfg = cfg
    try:
        out["links"] = prefetch_links(album_dir, run_cfg)
    except Exception:
        traceback.print_exc()
    try:
        out["metadata"] = run_metadata_step(album_dir, run_cfg)
    except Exception:
        traceback.print_exc()
    try:
        if run_cfg.get("cover_auto_fetch", True):
            payload = cover_candidates(album_dir, run_cfg)
            if payload is not None:
                out["cover"] = stage_cover_candidates(album_dir, payload, run_cfg)
    except Exception:
        traceback.print_exc()
    # What the add pre-fetched is recorded in the framework marker: the page
    # (and the proof that this ran at add time, not at import time) reads it
    # from there, and the marker is cleared by the import anyway. The write
    # goes through `pending_albums.update_marker`, which refuses once the
    # folder holds audio — this runs in the BACKGROUND (a discography's add
    # pre-fetches dozens), so an import can fill and clear the album while
    # these fetches are in flight, and a plain save would put the marker back
    # and leave a finished album reading PENDING.
    try:
        from server import pending_albums
        applied = (out.get("metadata") or {}).get("applied") or {}
        chosen = (out.get("cover") or {}).get("choice") or {}
        pending_albums.update_marker(album_dir, {"prefetched": {
            "at": time.time(),
            "artist_image": applied.get("artist_image"),
            "artist_description": applied.get("artist_description"),
            "album_description": applied.get("album_description"),
            "cover_candidates": int((out.get("cover") or {}).get("candidates") or 0),
            "cover_pick": chosen.get("big"),
            "cover_source": chosen.get("source"),
            "links": {"album": (out.get("links") or {}).get("album"),
                      "artist": (out.get("links") or {}).get("artist")},
        }})
    except Exception:
        traceback.print_exc()
    return out


def acoustid_match(paths, cfg=None, progress=None, apply=False, expect=None,
                   match=None):
    """Which release group the audio in these albums really is (AcoustID).

    Album folders or track paths; the tracks of each folder are fingerprinted
    and voted on as one album (see ``mlo.acoustid.match_release``). Returns
    ``{"available", "note", "ok", "code", "albums": [{"path",
    "release_group_id", "release_group_title", "release_group_type", "artists",
    "score", "matched", "total", "recordings", "tagged", "writes", "status",
    "code", "reason", "conflict", "conflicts", "skips", "failures"}]}``.

    ``available`` False with a human-readable ``note`` when no key/fpcalc is
    configured, and never an exception. Each row always carries its own
    verdict: ``status`` is "matched", "no_match", "skipped" (no fingerprintable
    audio: a video container, a clip under five seconds) or "error" (fpcalc
    could not run or the lookup could not be answered) — a failed lookup is
    never dressed up as "no match", and ``reason`` is the sentence to show.
    ``skips``/``failures`` name the tracks behind each.

    *expect* is the tag-derived candidate (a release dict with
    ``release_group_id`` / ``release_group_title`` / ``artists``); the match is
    cross-checked against it and any disagreement comes back in ``conflicts``
    (``release_group_mismatch`` renders it). Without *expect* the album's own
    tags are read (`_tag_candidate`) and used the same way. The comparison
    never writes anything: the release the import was matched to is what the
    import keeps.

    With *apply* the accepted match is also written into the files
    (`ACOUSTID_ID` + `ACOUSTID_FINGERPRINT`, the tags Picard writes and the
    opt-in grading check reads), ``tagged`` reports how many went in and
    ``writes`` carries every track's own outcome (``mlo.acoustid.write_tags``:
    ``{path, ok, code, reason}``) — an album of .wv files is a real match that
    cannot be tagged, and "0 tagged" without the reason was exactly the silent
    answer that hid it. The wizard passes apply=True when the user accepts the
    match, which is the only moment "this is really that release" is a
    statement the app can act on.

    *match* is that same row handed BACK: the release group and the per-path
    recording ids (+ fingerprints) the apply=False pass already returned. With
    it (and *apply*) nothing is fingerprinted and nothing is looked up — the
    wizard's accept step used to re-run fpcalc and the whole lookup for a
    result it had already been shown, so one transient network failure turned a
    displayed match into zero tags. The recordings of a supplied match are
    written straight from the payload, and a path outside these albums is
    ignored.
    """
    cfg = cfg or load_config()
    try:
        from mlo import acoustid
    except ImportError as e:                      # pragma: no cover - stripped backend
        return {"available": False, "note": f"acoustid unavailable: {e}",
                "albums": [], "ok": False, "code": "unavailable"}

    albums = []
    for p in paths or []:
        album = _album_dir(p)
        if album and album not in albums:
            albums.append(album)

    supplied = _supplied_match(match) if apply else None
    if supplied is None and not acoustid.available(cfg):
        return {"available": False,
                "note": acoustid.acoustid_enabled_note(cfg) or "AcoustID unavailable",
                "albums": [], "ok": False, "code": acoustid.check(cfg)["code"]}

    rows = []
    for album in albums:
        row = {"path": album, "tagged": 0, "writes": [], **_ACOUSTID_ROW}
        candidate = (expect if isinstance(expect, dict)
                     else _tag_candidate(album))
        if supplied is not None:
            # Applying a match the caller was ALREADY shown: no fpcalc, no
            # lookup, no network (see the docstring).
            report = _supplied_report(supplied, album, candidate)
        else:
            try:
                tracks = _audio_files(album)[:acoustid.MAX_TRACKS]
            except Exception:
                traceback.print_exc()
                tracks = []
            try:
                report = acoustid.match_release(cfg, tracks, progress=progress,
                                                expect=candidate)
            except Exception as e:
                traceback.print_exc()
                report = acoustid.error_report(f"AcoustID check failed: {e}",
                                               total=len(tracks))
        match = report.get("match")
        if match:
            row.update({k: match.get(k) for k in _ACOUSTID_ROW})
            row["path"] = album
            if apply:
                # Every track's own outcome, not a bare count: an unsupported
                # container or a failed write is a fact the UI has to be able
                # to say instead of "the files carry none of the tag families
                # it targets".
                writes = [acoustid.write_tags(rec.get("path"),
                                              rec.get("recording_id"),
                                              rec.get("fingerprint"), cfg)
                          for rec in match.get("recordings") or []]
                row["writes"] = writes
                row["tagged"] = sum(1 for w in writes if w.get("ok"))
                if row["tagged"]:
                    tagcache.invalidate_all()
        for key in ("status", "code", "reason", "conflict", "conflicts",
                    "skips", "failures"):
            row[key] = report.get(key)
        row["total"] = report["tracks"]["total"]
        rows.append(row)

    errors = [r for r in rows if r.get("status") == "error"]
    return {"available": True,
            "note": errors[0].get("reason") if errors else "",
            "ok": not errors,
            "code": errors[0].get("code") if errors else acoustid.OK,
            "albums": rows}


def _supplied_match(match):
    """A caller's own match payload, normalized — or None when unusable.

    The shape is the album row `acoustid_match` itself answers with: the
    release-group fields plus `recordings` (each with `path`, `recording_id`
    and `fingerprint`). Anything without a usable recording list is not a
    match, and the caller falls back to a real fingerprint run rather than
    being told a write happened from an empty payload.
    """
    if not isinstance(match, dict):
        return None
    recs = []
    for rec in match.get("recordings") or []:
        if not isinstance(rec, dict) or not str(rec.get("path") or "").strip():
            continue
        recs.append({"path": os.path.normpath(str(rec["path"])),
                     "recording_id": str(rec.get("recording_id") or ""),
                     "fingerprint": str(rec.get("fingerprint") or ""),
                     "title": rec.get("title") or "",
                     "score": rec.get("score")})
    if not recs:
        return None
    out = {"recordings": recs}
    for key in ("release_group_id", "release_group_title",
                "release_group_type", "score", "matched", "total"):
        if match.get(key) is not None:
            out[key] = match[key]
    out["artists"] = [str(a) for a in (match.get("artists") or []) if str(a).strip()]
    return out


def _supplied_report(supplied, album, expect):
    """The report for one album of a supplied match (`_supplied_match`).

    Only the recordings that live in THIS album are written (a match may span
    several folders of one queue), and the cross-check runs exactly as the
    fingerprint pass ran it, so the apply reply's row is the same row the
    wizard already showed — plus what the writes did.
    """
    from mlo import acoustid

    recs = [dict(r) for r in supplied["recordings"] if _album_dir(r["path"]) == album]
    total = int(supplied.get("total") or len(recs))
    match = {
        "release_group_id": supplied.get("release_group_id"),
        "release_group_title": supplied.get("release_group_title"),
        "release_group_type": supplied.get("release_group_type"),
        "artists": supplied.get("artists") or [],
        "score": supplied.get("score"),
        "matched": len(recs),
        "total": total,
        "recordings": recs,
    }
    conflicts = acoustid.cross_check(match, expect)
    return acoustid.report("matched",
                           acoustid.CONFLICT if conflicts else acoustid.OK,
                           "" if recs else
                           "the supplied match carries no track of this album",
                           match=match, conflicts=conflicts,
                           total=total, fingerprinted=len(recs))


def acoustid_submit(paths, cfg=None):
    """Give AcoustID the fingerprints these files already carry.

    Album folders or track paths. The fingerprint and the recording id are read
    back OFF THE FILES (`ACOUSTID_FINGERPRINT` / `ACOUSTID_ID`, the pair
    accepting a match wrote) and never recomputed: the tag is the identity the
    user accepted, and re-fingerprinting would submit something the library
    does not claim to be. Nothing is written here — this is the one
    outward-facing step of the AcoustID path, and it owns no tag and no file,
    exactly like the LRCLIB publish.

    The duration comes from the file's own tech (AcoustID needs one and it is
    not part of the fingerprint); a track whose duration cannot be read is
    SKIPPED with its own reason rather than submitted with a guess. Returns
    ``{"available", "note", "ok", "code", "submitted", "failed", "skips",
    "submissions", "tracks"}``: ``submitted`` counts what the service accepted
    (each with its submission id and status), and a refused user key comes back
    in ``note`` with the service's own sentence. Never raises.
    """
    cfg = cfg or load_config()
    try:
        from mlo import acoustid
    except ImportError as e:                      # pragma: no cover - stripped backend
        return {"available": False, "note": f"acoustid unavailable: {e}",
                "ok": False, "code": "unavailable", "submitted": 0,
                "failed": 0, "skips": [], "submissions": [],
                "tracks": {"total": 0, "submitted": 0, "skipped": 0}}

    files = []
    for p in paths or []:
        try:
            found = _audio_files(_album_dir(p))
        except Exception:
            traceback.print_exc()
            found = []
        for f in found:
            if f not in files:
                files.append(f)

    chk = acoustid.check_submit(cfg)
    if not chk["available"]:
        return {"available": False, "note": chk["reason"], "ok": False,
                "code": chk["code"], "submitted": 0, "failed": len(files),
                "skips": [], "submissions": [],
                "tracks": {"total": len(files), "submitted": 0, "skipped": 0}}

    # Every file answers something: an item to submit, or a skip naming what it
    # lacks (no AcoustID pair, an unreadable container). A file that vanishes
    # from both lists would make `tracks.total` a number nobody can account for.
    items, skips = [], []
    for f in files:
        item, skip = _submit_item(f)
        if skip:
            skips.append(skip)
        elif item:
            items.append(item)
    res = acoustid.submit_fingerprints(cfg, items)
    skips.extend(res["skips"])
    return {"available": True,
            "note": res["reason"] if not res["ok"] else "",
            "ok": bool(res["ok"]),
            "code": res["code"],
            "submitted": res["submitted"],
            "failed": res["failed"],
            "skips": skips,
            "submissions": res["submissions"],
            "tracks": {"total": len(files), "submitted": res["submitted"],
                       "skipped": len(skips)}}


def _submit_item(path):
    """(item, skip) for one file: exactly one of the two is set.

    Reads only — `get_tag` for the pair and the text metadata, `tech` for the
    duration — so submitting never touches the file it describes. A file with
    no `ACOUSTID_FINGERPRINT` is a SKIP with that reason: there is nothing to
    give AcoustID, and inventing one would submit audio the library never
    claimed.
    """
    from mlo.audio import AudioFile

    try:
        af = AudioFile(path)
        if af.audio is None:
            return None, {"path": path, "code": "unreadable_file",
                          "reason": (f"cannot read {os.path.basename(path)}: "
                                     f"{af.error or 'no tag reader for this file'}")}
        fingerprint = str(af.get_tag("ACOUSTID_FINGERPRINT") or "").strip()
        if not fingerprint:
            return None, {"path": path, "code": "no_fingerprint",
                          "reason": "no ACOUSTID_FINGERPRINT tag to submit"}
        # Duration: mutagen's stream info for audio, ffprobe's for a video
        # container (a VideoHandle carries no `info`). AcoustID requires one and
        # it is not part of the fingerprint, so a file that cannot state its
        # own length is SKIPPED rather than submitted with a guess.
        info = getattr(getattr(af, "audio", None), "info", None)
        duration = 0.0
        for value in (getattr(info, "length", None), (af.tech or {}).get("length")):
            try:
                duration = float(value or 0)
            except (TypeError, ValueError):
                duration = 0.0
            if duration > 0:
                break
        item = {"path": path, "fingerprint": fingerprint,
                "duration": duration,
                "recording_id": str(af.get_tag("ACOUSTID_ID") or "").strip(),
                "track": str(af.get_tag("TITLE") or "").strip(),
                "artist": str(af.get_tag("ARTIST") or "").strip(),
                "album": str(af.get_tag("ALBUM") or "").strip(),
                "album_artist": str(af.get_tag("ALBUMARTIST") or "").strip()}
        for key, tag in (("year", "DATE"), ("track_no", "TRACKNUMBER"),
                         ("disc_no", "DISCNUMBER")):
            value = str(af.get_tag(tag) or "").strip()
            if value:
                item[key] = value.split("-")[0].strip() if key == "year" else value
        return item, None
    except Exception:
        traceback.print_exc()
        return None, {"path": path, "code": "unreadable_file",
                      "reason": f"could not read {os.path.basename(path)}"}


def _tag_candidate(album_dir):
    """What an album's own tags claim, for the AcoustID cross-check.

    Release-group id, release id, album title and album artist off up to five
    tracks — album-level tags are uniform across an album's files, which is
    the same cap (and the same reason) :func:`_album_mbids` uses. Every field
    may be "" (an untagged download): a field only one side carries is not a
    disagreement.
    """
    from mlo.audio import AudioFile

    cand = {"release_group_id": "", "release_id": "", "title": "", "artists": []}
    for p in _audio_files(album_dir)[:5]:
        try:
            af = AudioFile(p)
            if af.audio is None:
                continue
            cand["release_group_id"] = cand["release_group_id"] or str(
                af.get_tag("MUSICBRAINZ_RELEASEGROUPID") or "").strip()
            cand["release_id"] = cand["release_id"] or str(
                af.get_tag("MUSICBRAINZ_ALBUMID") or "").strip()
            cand["title"] = cand["title"] or str(af.get_tag("ALBUM") or "").strip()
            artist = str(af.get_tag("ALBUMARTIST")
                         or af.get_tag("ARTIST") or "").strip()
            if artist and artist not in cand["artists"]:
                cand["artists"].append(artist)
        except Exception:
            continue
        if cand["release_group_id"] or cand["title"]:
            break
    return cand


def release_group_mismatch(match_row, release_group_id):
    """Warning text when an AcoustID result disagrees with the import, else "".

    The release-group id is what decides — the fingerprint names the group the
    audio is, the import was matched to *want* — and any further disagreement
    ``mlo.acoustid.cross_check`` found (a title or an artist only the tags
    claim) is appended. Used by the Soulseek auto-import as a *verification* of
    an already accepted download: a conflict is worth telling the user about,
    never worth throwing the album away over, and it never overwrites the
    release the import was matched to.
    """
    row = match_row or {}
    want = str(release_group_id or "").strip()
    got = str(row.get("release_group_id") or "").strip()
    parts = []
    if want and got and got != want:
        title = row.get("release_group_title") or "?"
        parts.append(f"AcoustID matched release group {got} ({title}, "
                     f"{row.get('matched')}/{row.get('total')} "
                     f"tracks) but the release being imported is {want}")
    for c in row.get("conflicts") or []:
        if isinstance(c, dict) and c.get("kind") != "release_group" and c.get("reason"):
            parts.append(str(c["reason"]))
    return " | ".join(parts)


# --------------------------------------------------------------------------- #
# What an import does NOT keep
# --------------------------------------------------------------------------- #
# An import decides four families for itself: the album's lyrics (script 13),
# its GENRE (the release's own chain, `_stamp_release`), its ITUNESADVISORY
# (`fetch_advisories`) and its cover art (`run_cover_step`). A download arrives
# carrying the PEER's values for all four, and every one of those writers is
# FILL-ONLY: `_stamp_release` writes a genre only into an empty one,
# `fetch_advisories` echoes a stored 0/1/2 instead of asking anybody, script 13
# skips a track that already carries lyrics, and art inside the file is art no
# source this app asked for produced. Without this pass the album therefore
# ends the import holding what the peer put there rather than what the import
# found.
#
# So `drop_arrived_values` is the import's FIRST tag-writing pass, and
# `finish_album` runs it before anything else touches the folder. Emptying the
# four slots is what lets those writers — which a user's OWN write still needs
# to fill-only (the wizard's genre pick, the tag editors, a manual `/run`) —
# land the import's values. That is the whole difference between an import and
# `/run`: the scripts themselves are unchanged, an import just does not hand
# them the peer's head start.
#
# `import_keep_synced_lyrics` is the one exception, and it is the lyric
# family's alone: a SYNCED lyric is work no source can reproduce, so with the
# switch on a file whose own lyric already carries timestamps keeps it (and
# script 13 skips that file, as it always did). Off — the shipped default — it
# is replaced like everything else.
def _arrived_lyric_is_synced(af, lrc_path):
    """Whether the lyric a file ARRIVED with carries real timestamps.

    Both places a lyric lives count: the embedded tag and the ``.lrc`` sidecar
    next to the file. "Synced" is `mlo.lyrics`'s own answer (`sync_level_of`:
    everything but a plain text is timed), and a stub with no words in it — a
    metadata-only or timestamp-only sidecar — is not a lyric at all
    (`has_lyrics_text`), so it can never be the thing the switch keeps.
    """
    from mlo.lyrics import has_lyrics_text, sync_level_of

    texts = [af.get_lyrics() or ""]
    try:
        with open(lrc_path, "r", encoding="utf-8", errors="replace") as fh:
            texts.append(fh.read())
    except OSError:
        pass
    return any(sync_level_of(t) != "plain" and has_lyrics_text(t) for t in texts)


def drop_arrived_values(album_dir, cfg=None, chain=None):
    """Drop what a downloaded album arrived carrying, for the four families an
    import decides itself: the LYRICS (embedded and the ``.lrc`` sidecar, plus
    the transliteration / translation half derived from them), GENRE,
    ITUNESADVISORY and the embedded cover art. See the note above for why the
    import's own writers need this and a user's write does not.

    Called on the album as it arrived, BEFORE any family step or chain script.
    Only the families THIS import is going to decide are cleared — a family the
    user kept is not the pipeline's to empty, which is what handing the album
    over means, so `import_review_families`, a review-mode run and the
    families' own switches are all read here exactly as the steps read them.
    The lyrics are the one family whose clearing also needs the CHAIN: script
    13 is the fetcher, so a chain that will not run it has nothing to put in
    place of the lyric it would remove. ``chain`` is the chain this import will
    run (`chain_for(cfg)` when the caller has not computed one).

    The lyric guard for a file with a described USLT frame is `mlo.audio`'s own
    (`delete_lyrics`): a named translation another tagger wrote is theirs and
    stays, like every other described frame in this app.

    Returns the counts: ``checked`` files walked, ``failed`` files whose tags
    could not be read or written (counted, never swallowed — such a file keeps
    what it arrived with, and a caller has to be able to say so), and one count
    per family of the FILES that lost an arrived value.
    """
    from mlo import import_policy
    from mlo.audio import AudioFile
    from mlo.config import should_write_audio_tag
    from mlo.lyrics import _lrc_for

    cfg = cfg or load_config()
    chain = chain_for(cfg) if chain is None else list(chain)
    # The families the user kept. `review_families` is the same answer the
    # steps themselves are gated on: the configured list, `cover_review`, and
    # every family in review mode.
    kept = set(import_policy.review_families(cfg))
    drop = {
        # 13 is the fetcher: no 13 in the chain, nothing to replace a lyric.
        "lyrics": 13 in chain and "lyrics" not in kept,
        "genre": "genres" not in kept,
        # Mirroring `finish_album`'s own gate: the advisory step only runs when
        # a chain is configured to read what it writes.
        "advisory": bool(chain) and bool(cfg.get("advisory_auto_fetch", True))
                    and "advisory" not in kept,
        # The cover step runs on every path (its switch is its own), and art
        # inside a file is nobody's but the downloader's until an import
        # decides the family.
        "cover": bool(cfg.get("cover_auto_fetch", True)) and "cover" not in kept,
    }
    if not any(drop.values()):
        return {"checked": len(_audio_files(album_dir)), "failed": 0,
                "lyrics": 0, "genre": 0, "advisory": 0, "cover": 0}
    keep_synced = bool(cfg.get("import_keep_synced_lyrics", False))

    def _one(path):
        """One file: which families lost an arrived value, or "failed"."""
        try:
            af = AudioFile(path)
            if af.audio is None:
                return "failed"
            # A stand-in AudioFile (a test double) may carry only get_tag /
            # set_tag: there is nothing in it to inspect or drop, exactly as
            # `_stamp_release` treats a double as one that cannot defer.
            if not all(hasattr(af, m) for m in ("has_tag", "delete_tag",
                                                "delete_lyrics",
                                                "embedded_pictures")):
                return ()
            gone = []
            if drop["lyrics"] and should_write_audio_tag(cfg, "LYRICS",
                                                         filepath=path):
                lrc = _lrc_for(path)
                if not (keep_synced and _arrived_lyric_is_synced(af, lrc)):
                    if str(af.get_lyrics() or "").strip():
                        af.delete_lyrics()
                        gone.append("lyrics")
                    if os.path.isfile(lrc):
                        os.remove(lrc)
                        gone.append("lyrics")
                    # A transliteration / translation is a transform OF the
                    # lyric: once the import replaces that lyric the stored
                    # ones describe words that are no longer in the file, and
                    # the grader would fail the pair (XLIT_UNNEEDED) rather
                    # than credit them. Dropped with their source, by the same
                    # helper that drops them when the lyric no longer needs
                    # them at all.
                    try:
                        from mlo import lyrics_xlit
                        for kind in ("TRANSLITERATION", "TRANSLATION"):
                            if lyrics_xlit._drop_stored_transforms(af, path, kind):
                                gone.append("lyrics")
                    except Exception:
                        traceback.print_exc()
            if drop["genre"] and af.has_tag("GENRE") \
                    and should_write_audio_tag(cfg, "GENRE", filepath=path):
                af.delete_tag("GENRE")
                gone.append("genre")
            if drop["advisory"] and af.has_tag("ITUNESADVISORY") \
                    and should_write_audio_tag(cfg, "ITUNESADVISORY", filepath=path):
                af.delete_tag("ITUNESADVISORY")
                gone.append("advisory")
            if drop["cover"] and af.embedded_pictures():
                if af.remove_embedded_pictures():
                    gone.append("cover")
            return gone
        except Exception:
            traceback.print_exc()
            return "failed"

    files = _audio_files(album_dir)
    out = {"checked": len(files), "failed": 0, "lyrics": 0, "genre": 0,
           "advisory": 0, "cover": 0}
    # Distinct FILES share nothing: every write above is one container rewrite
    # of its own file (mlo.audio, one flush per file), so an album's tracks go
    # side by side instead of one after another — the same fan-out
    # `_stamp_release` and the chain's own passes use.
    workers = worker_count(cfg, default=8, maximum=8, items=len(files))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        outcomes = list(ex.map(_one, files))
    for row in outcomes:
        if row == "failed":
            out["failed"] += 1
            continue
        for family in set(row or ()):
            out[family] += 1
    return out


# --------------------------------------------------------------------------- #
# Identity tags for a release-driven import
# --------------------------------------------------------------------------- #
def _release_for_stamping(release):
    """A release dict with the keys the Soulseek stamper reads.

    Callers hand over what they have (``release_mbid``/``title``/``artists``
    from the wizard or the discovery providers); the stamper wants
    ``id``/``media``/``artists``.
    """
    rel = dict(release or {})
    if not rel.get("id"):
        rel["id"] = rel.get("release_mbid") or ""
    return rel


def _stamp_release(album_dir, release, cfg):
    """Write the release identity into the album's tags.

    Returns ``(written, failed)`` — a file whose tags cannot be written is
    counted rather than silently skipped: a release-driven import that stamped
    nothing would otherwise surface much later as bare grading failures with
    nothing pointing at the stamp step. ``written`` counts the files whose
    GENRE actually landed ("skipped" files — nothing to say about them — are
    in neither number, as before).

    The MusicBrainz ids (+ per-track recording ids) come from
    ``server.soulseek_auto._stamp_mb_tags`` — the same stamper the Soulseek
    import uses, so a bulk import and an auto-import tag identically. On top:
    GENRE from the full per-track genre chain (RateYourMusic → ListenBrainz →
    MusicBrainz → iTunes → Wikidata → Last.fm → Discogs → Deezer, see
    ``integrations.genre_chain``), already merged per track and capped at
    ``mb_genre_count``. ITUNESADVISORY is not written here —
    ``finish_album`` resolves it from the ISRCs (``fetch_advisories``) before
    the chain runs, which is the only path that can state a real rating
    instead of echoing one.

    GENRE is written only into a track that carries none, which is what keeps
    a genre the user typed (the wizard's step, the tag editor) — and it is also
    why an IMPORT clears the arrived genre first (``drop_arrived_values``): for
    an import the release's genres are the album's, and they land here because
    the slot was emptied before this ran, not because this writer replaced it.
    """
    from mlo.audio import AudioFile
    from mlo.autotag import genre_count, trim_genres
    from mlo.genres import normalize_genres
    from server import integrations as intg
    from server import soulseek_auto

    rel = _release_for_stamping(release)
    try:
        soulseek_auto._stamp_mb_tags(album_dir, rel)
    except Exception:
        traceback.print_exc()
    files = _audio_files(album_dir)
    # The same per-track cap the other writers keep — the one helper reads
    # `mb_genre_count` (and clamps it to its ceiling), with the shipped default
    # behind it so an import can never leave a file the app's own grader would
    # fail.
    cap = genre_count(cfg)
    genres = {}
    if rel.get("release_group_id") or rel.get("id"):
        try:
            # The FULL per-track chain (RateYourMusic → ListenBrainz →
            # MusicBrainz → iTunes → Wikidata → Last.fm → Discogs → Deezer),
            # merged per track and capped: an import therefore writes the same
            # genres an Auto-tagging run would, from the release it has just
            # identified, and a per-track source (recording tags, Apple's
            # primaryGenreName) lands on the track it belongs to.
            chain = intg.genre_chain(
                artist=next((a.get("name") for a in rel.get("artists") or []
                             if a.get("name")), ""),
                album=rel.get("title") or "",
                release=rel, limit=cap, cfg=cfg, files=files)
            genres = chain.get("per_track") or {}
        except Exception:
            traceback.print_exc()

    def _stamp_one(path):
        """One track: its GENRE, the cap, and a SINGLE container rewrite.

        "written" is the flush's own verdict, because a deferred write that
        cannot land (a full disk, a read-only file) no longer raises out of
        set_tag — counting the file anyway would report tags it does not have.
        A file that cannot be read, or whose write failed, is "failed" and is
        never also counted as written; "skipped" is a file this step had
        nothing to say about. Those are the same two outcomes the row reports
        it always did, so the caller's readout does not change."""
        try:
            af = AudioFile(path)
            if af.audio is None:
                return "failed"
            # A stand-in for AudioFile (a test double) cannot defer: it writes
            # per tag, exactly as it did before.
            defer = hasattr(af, "defer_save")
            if defer:
                af.defer_save(True)
            wrote = False
            ok = True
            try:
                tags = {}
                disc, pos = soulseek_auto._parse_trackno(path)
                names = genres.get((disc, pos)) or []
                if names and not str(af.get_tag("GENRE") or "").strip():
                    # Canonical, and a LIST: `normalize_genres` resolves each
                    # name to MusicBrainz's own spelling, derives the family
                    # and puts it last, and set_tag writes one repeated GENRE
                    # field per name — the grader counts every genre this step
                    # wrote instead of reading one value literally named
                    # "A; B".
                    tags["GENRE"] = normalize_genres(names, cap)
                for key, value in tags.items():
                    af.set_tag(key, value)
                wrote = bool(tags)
                try:
                    # Cap on EVERY track, not just the ones written above: an
                    # album that arrived carrying more genres than the setting
                    # allows comes down to `mb_genre_count` here (the trimmer
                    # script 8/10 also use).
                    trim_genres(af, cap)
                except Exception:
                    pass
            except Exception:
                ok = False
            finally:
                # Everything above lands in ONE rewrite (each tag write used to
                # be a whole-file copy of its own — mlo.atomic.rewrite_via),
                # and the deferral is turned off even on the error path so a
                # file is never left holding changes nobody saved.
                if defer and af.defer_save(False) is False:
                    ok = False
            if not ok:
                return "failed"
            return "written" if wrote else "skipped"
        except Exception:
            return "failed"

    # Distinct FILES share nothing: every rewrite_via copies beside its OWN
    # target and swaps it in with os.replace (mlo.atomic), so an album's
    # tracks write side by side instead of one after another.
    workers = worker_count(cfg, default=8, maximum=8, items=len(files))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        outcomes = list(ex.map(_stamp_one, files))
    return (sum(1 for o in outcomes if o == "written"),
            sum(1 for o in outcomes if o == "failed"))


def _marker_links(album_dir, cfg=None):
    """The RateYourMusic links the ADD already resolved for this album, or {}.

    `imports.prefetch_links` runs the SAME resolver at add time (same switch,
    same MB-relation first ladder) and records what RYM confirmed in the
    framework marker, so an import that looks the links up again pays a second
    time for an answer the app itself wrote down.

    Read only while the marker still SPEAKS FOR this album, and only while the
    user's switch is on: the marker must carry the resolved `links` (the
    `prefetched` echo is accepted for a marker written before this existed) and
    the release GROUP the album's own tags state — when they state one — must
    be the group the marker was created with, so a marker left behind by a
    different album that landed in the same folder hands nothing over. The
    switch is checked here because `integrations.rym_links` is where it
    normally short-circuits: off must keep writing NO auto-resolved link, and
    that has to hold for the marker path too.
    """
    cfg = cfg if cfg is not None else load_config()
    if not cfg.get("rym_links_auto", True):
        return {}
    try:
        from mlo.paths import load_pending
        info = load_pending(album_dir) or {}
    except Exception:
        return {}
    if not info:
        return {}
    links = dict(info.get("links") or {})
    echo = (info.get("prefetched") or {}).get("links") or {}
    for key in ("album", "artist"):
        links[key] = str(links.get(key) or echo.get(key) or "").strip()
    if not (links["album"] or links["artist"]):
        return {}
    _album_id, rg = _album_mbids(album_dir)
    marker_rg = str(info.get("release_group_id") or "").strip().lower()
    if rg and marker_rg and rg != marker_rg:
        return {}
    return links


def stamp_rym_links(album_dir, cfg=None):
    """Resolve and write the album's / artist's RateYourMusic links.

    One RATEYOURMUSIC_ALBUM and one RATEYOURMUSIC_ARTIST on every track — the
    state the grader and the links editor read. A link already present on ANY
    track of the album is left alone and NOT looked up: the user's own link
    wins, and nothing here ever overwrites one. Only a link RYM itself
    confirmed (``server.integrations.rym_links``) is written, so a failed
    lookup writes nothing at all and reports "could not resolve" in its note
    instead of failing the import.

    Gated by `rym_links_auto` (config.py, default True). Returns
    ``{"album", "artist", "note", "written"}``; never raises.
    """
    from mlo.audio import AudioFile
    from server import integrations as intg

    cfg = cfg or load_config()
    out = {"album": None, "artist": None, "note": "", "written": 0}
    files = []
    for path in _audio_files(album_dir):
        try:
            af = AudioFile(path)
        except Exception:
            continue
        if af.audio is not None:
            files.append(af)
    if not files:
        return out

    def _tag(af, name):
        try:
            return str(af.get_tag(name) or "").strip()
        except Exception:
            return ""

    have_album = any(_tag(af, "RATEYOURMUSIC_ALBUM") for af in files)
    have_artist = any(_tag(af, "RATEYOURMUSIC_ARTIST") for af in files)
    if have_album and have_artist:
        return out                      # nothing missing: no lookup at all

    artist, album = _album_identity(album_dir)
    # The ADD already resolved these links and recorded them in the framework
    # marker (`prefetch_links` — the same resolver, the same switch, the same
    # artist/album), and this step is where an import would ask for them a
    # SECOND time: the lookup measured 2.7 s of the add's pre-fetch, re-paid at
    # import for an answer the app itself wrote down. The TAGS are still
    # written below; only the lookup is skipped, and only for a link the marker
    # really carries for THIS album.
    links = _marker_links(album_dir, cfg)
    if links.get("album") and links.get("artist"):
        out["note"] = "links already resolved when the album was added"
    else:
        # With the album's MusicBrainz id the lookup is a url-relation read on
        # MusicBrainz — no RYM scrape, no cookie, no guess — so the id is offered
        # first and the slug/search ladder is only the fallback.
        found = intg.rym_links(
            artist, album, cfg=cfg,
            mbid=(_tag(files[0], "MUSICBRAINZ_RELEASEGROUPID")
                  or _tag(files[0], "MUSICBRAINZ_ALBUMID")))
        out["note"] = found.get("note") or ""
        # A marker holding only ONE of the two is not a reason to throw that one
        # away: the lookup fills the other, the marker keeps what it has.
        links = {"album": found.get("album") or links.get("album"),
                 "artist": found.get("artist") or links.get("artist")}
    if not have_album:
        out["album"] = links.get("album")
    if not have_artist:
        out["artist"] = links.get("artist")
    if not out["album"] and not out["artist"]:
        return out

    def _write_links(af):
        """Write whichever of the two links is missing, in ONE rewrite.

        Both were a whole-file copy of their own before this (mlo.audio saves
        by writing a temp beside the file and renaming it over — mlo.atomic.
        rewrite_via), so two links cost two rewrites per track. The deferral
        holds them for a single flush, and its verdict IS the answer: deferred,
        a write that cannot land no longer raises out of set_tag, and a file
        that did not reach disk must not be counted as written. The deferral is
        turned off even when a write raised, so a half-written file is flushed
        rather than left holding changes nobody saved."""
        try:
            tags = {}
            if out["album"] and not _tag(af, "RATEYOURMUSIC_ALBUM"):
                tags["RATEYOURMUSIC_ALBUM"] = out["album"]
            if out["artist"] and not _tag(af, "RATEYOURMUSIC_ARTIST"):
                tags["RATEYOURMUSIC_ARTIST"] = out["artist"]
            if not tags:
                return False
            # A stand-in for AudioFile (a test double) cannot defer: it writes
            # per tag, exactly as it did before.
            defer = hasattr(af, "defer_save")
            if defer:
                af.defer_save(True)
            written = False
            try:
                for key, value in tags.items():
                    af.set_tag(key, value)
                written = True
            finally:
                if defer and af.defer_save(False) is False:
                    written = False
            return written
        except Exception:
            return False

    # Distinct FILES share nothing: every rewrite_via copies beside its OWN
    # target and swaps it in with os.replace (mlo.atomic), so the album's
    # tracks write side by side instead of one after another.
    workers = worker_count(cfg, default=8, maximum=8, items=len(files))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        out["written"] = sum(1 for ok in ex.map(_write_links, files) if ok)
    if out["written"]:
        _invalidate_caches(album_dir)
    return out


# --------------------------------------------------------------------------- #
# Bulk queue
# --------------------------------------------------------------------------- #
_job_lock = threading.Lock()
_job = {"id": None, "kind": None, "status": "idle", "started": None,
        "finished": None, "total": 0, "done": 0, "label": "", "items": [],
        "error": None}


def job_state():
    """The bulk job the UI polls: same shape as ``soulseek_auto.job_state()``."""
    with _job_lock:
        return dict(_job, items=[dict(x) for x in _job["items"]])


def _job_update(**fields):
    with _job_lock:
        _job.update(fields)


def _job_note(done, label, item):
    """Live progress for a running job; a no-op for a direct bulk_import call."""
    with _job_lock:
        if _job["status"] != "running":
            return
        _job["done"] = done
        _job["label"] = label
        # the per-script stats of a whole library are noise in a poll payload
        _job["items"].append({k: v for k, v in item.items() if k != "scripts"})


def _stats_hook(done, total, desc):
    """Mirror progress into mlo.stats.progress_hook — the WS relay's source.

    Stands down while a script CHAIN holds the hook (`script_runners` marks its
    own wrapper): the header would otherwise mix two albums' numbers, because
    the bulk queue's per-album frames would be scaled into the running chain's
    slice. The bulk job has its own row (`/api/import/bulk`, the ImportRunCard)
    and does not need the header to speak for it at the same time.
    """
    from mlo import stats as stats_mod
    hook = getattr(stats_mod, "progress_hook", None)
    if callable(hook) and not getattr(hook, "_mlo_chain", False):
        try:
            hook(done, total, desc)
        except Exception:
            pass


def _inside(path, folder):
    """Whether *path* is *folder* or below it (boundary-aware).

    Same test server.main's music-folder guard applies: ``C:\\Music2`` is not
    inside ``C:\\Music``, and paths on different drives never are.
    """
    try:
        ap = os.path.abspath(os.path.normpath(path))
        af = os.path.abspath(os.path.normpath(folder))
    except (OSError, ValueError, TypeError):
        return False
    if ap == af:
        return True
    if os.path.normcase(os.path.splitdrive(ap)[0]) != os.path.normcase(os.path.splitdrive(af)[0]):
        return False
    try:
        return os.path.normcase(os.path.commonpath([ap, af])) == os.path.normcase(af)
    except ValueError:
        return False


def _unique_dir(parent, name):
    """``<parent>/<name>``, deduplicated with " (2)", " (3)"…"""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name)).strip().rstrip(".") or "Import"
    dest = os.path.join(parent, safe)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(parent, f"{safe} ({n})")
        n += 1
    return dest


def _bulk_one(item, cfg):
    """Move one item into the library (when it is not there yet) and finish it.

    Idempotent by construction: a path that already sits inside the library is
    never moved (a second move would duplicate the album as "Album (2)"), it
    just re-runs the chain.
    """
    src = os.path.normpath(str(item.get("path") or ""))
    row = {"path": src, "status": "failed", "album_path": None,
           "error": None, "scripts": []}
    if not src or not os.path.exists(src):
        row["error"] = "path not found"
        return row

    folder = str(cfg.get("music_folder") or "").strip()
    if not folder or not os.path.isdir(folder):
        row["error"] = "music_folder not set or not found"
        return row

    root = library_root(folder)
    # Containment is not enough: the music folder itself, the library root and
    # the app's own .mlo state dir all live inside the music folder, so a
    # caller could queue the whole library (or config/playlists/trash) as if it
    # were an album — and a staging item would then be *moved* into a
    # subdirectory of itself. Album folders BELOW <music>/Artists are of course
    # legitimate, so only the two roots match exactly while the state dir
    # excludes its subtree. Checked before the "has audio?" test so the reason
    # is the real one, not "no audio files".
    def _same(a, b):
        try:
            return os.path.normcase(os.path.abspath(os.path.normpath(a))) == \
                   os.path.normcase(os.path.abspath(os.path.normpath(b)))
        except (OSError, ValueError, TypeError):
            return False

    for guard in (folder, root):
        if guard and _same(src, guard):
            row["error"] = "refusing to import the library root itself"
            return row
    try:
        from mlo.paths import app_data_dir
        # Scoped to THIS cfg's music folder: a bulk run must never treat the
        # config/playlists/trash of some other library as an album.
        state = app_data_dir(folder)
        if state and _inside(src, state):
            row["error"] = "refusing to import a library or app-state folder"
            return row
    except Exception:
        pass

    if os.path.isdir(src) and not _audio_files(src):
        # an empty/stray folder is not an album: importing it would publish a
        # library folder the import did not produce
        row["status"] = "skipped"
        row["error"] = "no audio files"
        return row

    move = item.get("move")
    if move is None:
        # staging paths are moved in; a folder that already IS in the library
        # (a re-run, an album imported by another path) is left where it is
        move = not _inside(src, root)
    if move and _inside(src, root):
        move = False
    album = src
    if move:
        dest = _unique_dir(root, os.path.basename(src.rstrip("\\/")))
        if not move_path(src, dest, log=lambda m: None):
            row["error"] = ("could not move into the library — a file inside "
                            "is still in use (stop playback and retry)")
            return row
        album = dest

    release = item.get("release")
    stamp_error = None
    if release:
        try:
            written, failed = _stamp_release(album, release, cfg)
            if failed:
                stamp_error = (f"release tags written to {written} file(s); "
                               f"{failed} file(s) could not be tagged")
        except Exception as e:
            traceback.print_exc()
            stamp_error = f"release stamping failed: {e}"

    try:
        # The release this row carries goes on to `finish_album` too. Its
        # genres step is the one that stamps GENRE, and the import's own pass
        # has just cleared whatever the album arrived with (see
        # `drop_arrived_values`), so that step has to stamp from the release in
        # hand: re-resolving it from the album's own MBID would be a second
        # lookup — with its own fresh choice of edition — and one that cannot
        # answer would leave the album with no genre at all.
        finished = finish_album(album, cfg, release=release)
    except Exception as e:                      # finish_album promises not to raise
        traceback.print_exc()
        finished = {"path": album, "scripts": [], "chain": [], "errors": [str(e)]}

    row["album_path"] = finished["path"].replace("\\", "/")
    row["scripts"] = finished["scripts"]
    row["error"] = "; ".join(filter(None, [stamp_error, *finished["errors"]])) or None
    # What the chain did, on the row: which scripts ran, what failed, or that
    # there was nothing to run. `_bulk_one` is the one place a bulk album's
    # outcome is recorded, so this is what the bulk queue reports.
    row["chained"] = bool(finished.get("chained"))
    row["chain_off"] = bool(finished.get("chain_off"))
    row["note"] = chain_summary(finished)
    # "Imported" means the album is in the library. It is only honest to say
    # so when the chain actually ran (or when nothing is configured to run):
    # an album that landed with none of its scripts executed is unfinished,
    # and reporting it as imported is how a silent gap becomes a mystery
    # grading failure later.
    if finished["errors"] and not finished["scripts"]:
        row["status"] = "failed"
        row["error"] = f"imported, but the script chain did not run: {row['error']}"
    else:
        row["status"] = "imported"
    return row


def bulk_import(items, cfg=None, progress=None):
    """Import a queue of albums (staging folders or library folders).

    *items*: ``{"path", "release": <optional MB release dict>, "move": bool}``
    — ``move`` defaults to True for a path outside the library and False for
    one already inside it. Each album is moved into
    ``<music folder>/Artists``, stamped with the release identity when one is
    supplied, then finished with the configured chain. ``import_bulk_concurrency``
    albums run at once (each on its own copy of the config).

    Progress goes to *progress(done, total, label, item_result)* and to
    ``mlo.stats.progress_hook`` (the websocket relay); a running job started by
    :func:`start_bulk` is updated too. Returns ``{"total", "ok", "failed",
    "skipped", "items": [...]}`` with the item rows in input order.

    A row's ``status``: ``imported`` — the album is in the library (a failing
    *script* rides along in ``error``, it does not un-import the album);
    ``skipped`` — nothing to import (no audio files); ``failed`` — it never
    got into the library, and ``error`` says why. ``chained``/``chain_off`` and
    ``note`` say what the configured chain did: a row that is ``imported`` with
    ``chain_off`` was imported without the optimization/tagging pass because
    this library configures none, and says so rather than looking like a run
    that happened.
    """
    cfg = cfg or load_config()
    items = [dict(it) for it in (items or [])]
    total = len(items)
    rows = [None] * total
    try:
        concurrency = max(1, int(cfg.get("import_bulk_concurrency") or 1))
    except (TypeError, ValueError):
        concurrency = 1

    def label_for(index):
        name = os.path.basename(str(items[index].get("path") or "").rstrip("\\/"))
        return f"Importing {name}" if name else f"Importing {index + 1}/{total}"

    if total:
        with ThreadPoolExecutor(max_workers=concurrency,
                                thread_name_prefix="mlo-import") as pool:
            futures = {pool.submit(_bulk_one, it, dict(cfg)): i
                       for i, it in enumerate(items)}
            done = 0
            for future in as_completed(futures):
                index = futures[future]
                try:
                    row = future.result()
                except Exception as e:          # _bulk_one reports, never raises
                    traceback.print_exc()
                    row = {"path": str(items[index].get("path") or ""),
                           "status": "failed", "album_path": None,
                           "error": str(e), "scripts": []}
                rows[index] = row
                done += 1
                label = label_for(index)
                _job_note(done, label, row)
                _stats_hook(done, total, label)
                if progress is not None:
                    try:
                        progress(done, total, label, row)
                    except Exception:
                        traceback.print_exc()

    rows = [r for r in rows if r is not None]
    return {
        "total": total,
        "ok": sum(1 for r in rows if r["status"] == "imported"),
        "failed": sum(1 for r in rows if r["status"] == "failed"),
        "skipped": sum(1 for r in rows if r["status"] == "skipped"),
        "items": rows,
    }


def start_bulk(items, cfg=None):
    """Start :func:`bulk_import` on a daemon thread; returns straight away.

    One bulk job at a time: a second start is refused with
    ``{"ok": False, "error": "bulk import already running"}``. Poll
    :func:`job_state` for progress.
    """
    with _job_lock:
        if _job["status"] == "running":
            return {"ok": False, "error": "bulk import already running"}
        _job.update({"id": uuid.uuid4().hex[:12], "kind": "bulk",
                     "status": "running", "started": time.time(),
                     "finished": None, "total": len(items or []), "done": 0,
                     "label": "", "items": [], "error": None})
        job = dict(_job, items=[])

    def run():
        try:
            result = bulk_import(items, cfg or load_config())
            _job_update(status="done", finished=time.time(),
                        done=result["total"], error=None)
        except Exception as e:
            traceback.print_exc()
            _job_update(status="failed", finished=time.time(), error=str(e))

    threading.Thread(target=run, name="mlo-bulk-import", daemon=True).start()
    return {"ok": True, "job": job}
