# la musica 4.4.1 — the first storage poll after a restart is free too

One number 4.4.0 left on the table, measured on the owner's own install minutes
after it updated: `GET /api/storage` answered in **6.97 s** the first time a
fresh process was asked for it, and **0.01 s** every time after. The snapshot is
memoized with a 60-second TTL and a stale entry is re-walked *behind* the
request — but a process that has just started has no entry at all, so the walk
landed on the one request the Home page's card makes first.

The walk is worked at startup now, beside the app's other background workers
(`_lifespan` → `storage-warm`), so even that first poll answers from memory.
Verified on a scratch server: the first `/api/storage` after startup took
**0.051 s**, with the snapshot's own `scanned_at` four seconds *before* the
request and `took_ms` naming the walk that happened in the background.

Nothing else changed: the snapshot is the same function the route calls, it is
cached under the same key, and `invalidate_storage_snapshot()` still drops it
(the storage suite passes unchanged). This is R334's own case — a page load
does not re-read the library — completed for the one process state that still
paid for it.

## Verified

- `python tools/check_versions.py v4.4.1` — all 10 copies agree.
- The full `tools/test_*.py` sweep passes, `tools/test_storage.py` included.
- The startup warm-up is observable in the payload (`scanned_at` before the
  first request); the walk it does is the same `_build_snapshot` the route
  would have run in-request.
