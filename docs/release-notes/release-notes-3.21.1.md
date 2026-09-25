# la musica 3.21.1 — the release 3.21.0 could not build

3.21.0 never reached anyone: its tag ran the release workflow, and the CI shard
that runs the Python suites failed, so the desktop bundles, the APK, the IPA and
the Docker image were all skipped. The code in this release is 3.21.0's, plus the
fix for the one test that failed.

## What failed, and why it was not the code

`tools/test_wishes_pipeline.py` asserts that three releases start at once, the
way `soulseek_search_concurrency` promises. Before that block it left the
pipeline empty — but "empty" was measured as *no RUNNING job*, and a slot belongs
to a job that is **running OR waiting at a confirmation prompt**
(`soulseek_auto._active_locked`). On a loaded CI box one of the earlier blocks
was still sitting at a prompt, so the three releases correctly *queued* instead
of starting, and the assertion that followed crashed on a row that had no `job`
to name.

The queue's own behaviour is right — a release over the ceiling keeps its place
and starts by itself (that is what the very next check in the same suite pins).
The test simply asked its question without waiting for the pipeline to be free.
It now waits (`_pipeline_free`) and guards the assertion, so a future mismatch
reports itself in words instead of a `KeyError`.

## Upgrading

Nothing to do. If you never got 3.21.0 artifacts, this is that release: the
phone player, the account control, the export file selection, the ADR rename, the
one-scroller lyrics view, the download that used to save `index.html`, and the
honest queue. Its notes are in `release-notes-3.21.0.md`.
