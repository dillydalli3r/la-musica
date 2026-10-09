# la musica 4.5.1 — "Loading your library…" was a whole walk in the request

4.5.0 removed the library tree's rebuild from every page load. The **Home**
payload was the one page it did not cover, and the owner felt it right away:
a container that had just restarted answered the first `GET /api/home` in
**187.5 s** — the whole build, in the request, behind the Home page's own
"Loading your library…" — and then 0.08-0.09 s for every call after it. That
payload is a 15-minute memo that is BUILT from the library tree, so the same
thing happened on every TTL lapse, every Refresh (which drops it), and every
settings save.

Two changes, the same pair the tree already had (R338):

- **`recommendations.build_home` is stale-while-revalidate.** A payload that is
  merely OLD is served as it stands and rebuilt on a daemon thread
  (`_start_home_refresh`, single flight, never raises, stamped after the
  build). No page load ever waits for the walk again.
- **It is warmed at startup** (`_lifespan`'s `home-warm` thread, beside
  `library-warm` and `storage-warm`): `recommendations.warm_home` builds the
  payload for every user `auth.list_users()` knows — the key that person's page
  will actually ask for, since the memo is scoped by user — or for the default
  scope on an install with no users. The only blocking build left is a cold
  process's first ask, and that no longer happens in a request.

An explicit **Refresh still rebuilds in the call**: `invalidate()` drops the
payload outright, which is what "give me fresh rows" means from the button that
says so. The TTL path is the one that was hanging.

## Verified

- `tools/test_home.py` gained the behaviour: an expired payload answers in
  under 0.1 s with the rows that were already there, the refresh lands behind
  it, and an explicit invalidation still rebuilds once inside the call.
- `tools/test_home.py`, `tools/test_pending_rows.py` and
  `tools/test_tagindex.py` pass; the full sweep and `npx tsc -b` were run for
  4.5.0's code and re-run here for the Python change.
- The 187.5 s / 0.08 s pair is measured on the owner's own install (170 files,
  bind-mounted library in Docker Desktop on Windows), on the 4.5.0 image that
  watchtower had just started.
