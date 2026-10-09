"""Tag hygiene — delete the tags a file should not carry (script 23).

Script 3 (Optimize FLACs) and script 10 (Format all) already remove the tags
this app would not write, but only as a step of their own work: 10 trims and
canonicalises every tag, 3 re-encodes a file. Clearing ONE album's junk tags
therefore meant running a lossless re-encode or a whole-library format pass,
and the album's details menu had nothing to offer for the job the grade was
failing the album for ("Excess tags: … (run Optimize FLACs (script 3) or
Format all (script 10) to strip them)").

This is that same strip on its own, scoped to what the user pressed. The list
of tags it deletes is the grader's own (`mlo.format_all.excess_tags`, which
asks `mlo.grader.tag_key_allowed`, `tag_value_excess` and
`alias_file_excess`), and the deletion is `mlo.format_all.strip_excess_tags` —
ONE stripper with two callers, so the two passes can never disagree about what
"excess" is, and the scoped run can never leave what the grade flags:

  * a tag NAME outside the shared vocabulary (TAG_MAP, the encoder identity
    tags, beets/Picard's spellings, the app's own AUDIOAUDITOR_OVERRIDE) —
    what a vendor or ripper left behind;
  * a `COMMENT` that carries a value — the one name the vocabulary HOLDS whose
    value nothing in this pipeline writes — or any value that names an
    external LINK (a URL such as a RateYourMusic page; a bare MusicBrainz id
    is not a link and is kept);
  * an ALIAS tag nothing needs (spec R16a/R16b): the name is one the
    configured locale already reads, the spelling is for a locale the app does
    not write (TITLEALIAS-JA in an `en` library), it is a second spelling of
    the same alias, or its value is the name itself.

Nothing is written, ever: this script only DELETES what the grader names. A
file with nothing excess is not opened for writing at all (the delete is what
marks the container dirty, and a clean file leaves nothing to flush), so a run
over a clean library touches no file — `tools/test_tag_hygiene.py` asserts that
with mtimes. Gated by `strip_unknown_tags`, the same switch script 10's strip
and the excess-tag grade read: with it off none of this is excess.

The tag cache is dropped for the folders this run rewrote and nothing else
(`server.tagcache.invalidate_album`, the scoped drop `/api/run` and the import
pipeline already use) — one album's hygiene must not cost the whole library a
re-parse on the next page.
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from .audio import AudioFile
from .format_all import strip_excess_tags
from .paths import AUDIO_EXTS
from .stats import (_collect_targets, _make_pbar, _pbar_skip, _pbar_update,
                    _walk_files, new_stats, worker_count)
from .ui import Color, c, log, print_header


def _shown(path, folder):
    """*path* as the run's lines show it: relative to the music folder when it
    is under it, the full path otherwise (a scoped target may sit anywhere the
    caller was allowed to name)."""
    try:
        return os.path.relpath(path, folder)
    except Exception:
        return path


def _clean_file(path, cfg):
    """Strip ONE file's excess tags; ``(path, removed, err)``.

    ``removed`` is the list of tag names that really went (empty for a file
    with nothing excess and for a container the tag layer cannot write).
    """
    try:
        af = AudioFile(path)
        if af.audio is None:
            # Nothing the tag layer can edit (an unreadable file, a container
            # it does not own): scanned, nothing to do, not a failure — the
            # verdict every other pass gives a file it cannot open.
            return (path, [], None)
        # One container write for the whole file, and NO write at all when the
        # list is empty: `defer_save(False)` flushes a dirty container only.
        af.defer_save(True)
        removed = strip_excess_tags(af, cfg)
        if not removed:
            return (path, [], None)
        if af.defer_save(False) is False:
            return (path, [], af.error or "tag write failed")
        return (path, removed, None)
    except Exception as e:
        return (path, [], str(e))


def _drop_tag_cache(folders):
    """Drop the tag cache of the albums this run rewrote — and only those.

    ``/api/run`` (server.api_run._invalidate_run) and the import pipeline already
    do this for the folders a run names; a terminal run has no cache at all.
    Calling it here as well costs a dict drop per album and makes the runner
    correct for every entry point, while `invalidate_all` — the whole library
    re-parsed on the next page — is exactly what a one-album hygiene pass must
    never charge (server.tagcache.invalidate_album's own rule).
    """
    folders = [f for f in folders if str(f or "").strip()]
    if not folders:
        return
    try:
        from server import tagcache
    except Exception:
        return                      # a terminal run: no server, no cache
    try:
        tagcache.invalidate_album(*folders)
    except Exception:
        pass


def run_tag_hygiene(config):
    """Script 23 — delete every tag the grader calls excess, nothing else.

    Deleting is the whole job, which is also why it has no force flag: its
    subject IS the excess tag, so a file that carries none is deliberately
    left alone on every run (the same reasoning as script 21's pair).
    """
    config = config or {}
    stats = new_stats()
    # Tag names this run deleted, always present so a caller reads a count
    # rather than a missing key (the way 10 reports genres_trimmed).
    stats["tags_removed"] = 0
    print_header("Tag hygiene")

    if not config.get("strip_unknown_tags", True):
        # With the switch off nothing is excess anywhere in the pipeline, so
        # this pass would delete nothing: say it instead of reporting an empty
        # run (mlo.scripts.SCRIPT_GATES / script_runners._DISABLED skip it in a
        # chain for the same reason).
        log("strip_unknown_tags is off — nothing is excess (see Configuration).")
        return stats

    folder = str(config.get("music_folder") or "")
    targets = config.get("targets")
    if targets is not None:
        files = sorted(_collect_targets(targets, AUDIO_EXTS))
    else:
        if not os.path.isdir(folder):
            log(c(f"ERROR: folder does not exist: {folder}", Color.RED))
            return stats
        # The same set the grader's excess check and script 10's tag pass look
        # at: a music video is never stripped (its tags are not graded, and
        # its container is remux's business).
        files = sorted(_walk_files(folder, AUDIO_EXTS))

    if not files:
        log("No files found to clean.")
        return stats
    log(f"music folder: {folder} · {len(files)} audio file(s) · "
        f"deletes excess and unneeded alias tags, writes nothing")

    workers = worker_count(config, maximum=16, items=len(files))
    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(len(files), "Tag hygiene", unit="file")
    cleaned = []

    def _finish(path, removed, err):
        stats["total_scanned"] += 1
        if err is not None:
            stats["error_count"] += 1
            stats["errors"].append((os.path.basename(path), err))
            _pbar_update(pbar, counts, kind="fail")
            log(c(f"  ✕ {os.path.basename(path)}: {err}", Color.RED))
            return
        if removed:
            stats["modified_count"] += 1
            stats["tags_removed"] += len(removed)
            cleaned.append(path)
            _pbar_update(pbar, counts, kind="ok")
            log(f"  ✓ {_shown(path, folder)} → removed {', '.join(removed)}")
        else:
            # Nothing excess on this file (or nothing the tag layer can
            # write): skipped, and NOT written — that is the pass's promise.
            stats["skipped_count"] += 1
            _pbar_skip(pbar, counts)

    try:
        if len(files) == 1 or workers == 1:
            for path in files:
                _finish(*_clean_file(path, config))
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(_clean_file, p, config): p for p in files}
                for fut in as_completed(futures):
                    _finish(*fut.result())
    finally:
        if pbar:
            pbar.close()

    _drop_tag_cache({os.path.dirname(p) for p in cleaned})
    log(c(f"tag hygiene: {stats['modified_count']} cleaned · "
          f"{stats['tags_removed']} tag(s) removed · "
          f"{stats['skipped_count']} already clean · "
          f"{stats['error_count']} failed",
          Color.GREEN if not stats["error_count"] else Color.YELLOW))
    return stats
