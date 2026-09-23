# la musica 3.22.1 — the queue suite's second row

3.22.0's tag failed CI, so nothing was published; the app code here is 3.22.0's,
unchanged.

`tools/test_queue_view.py` asserts two completed rows — the one whose naming
script could not finish, and the lossy one. It waited for them with `_wait_for`,
which returns the moment its condition holds, and the condition was *"either row
is present"*. On a loaded runner the second row landed after the wait, and the
very next line indexed a row that did not exist yet:

```
File "tools/test_queue_view.py", line 535, in <module>
    lossy_row = [r for r in got if r["title"] == "Copy"][0]
IndexError: list index out of range
```

It passed here and failed twice on CI — the second attempt is what reddens a
shard. The wait now asks for the **set** (`{"Move", "Copy"}`), so each row is
asserted once it is really there. The same shape was checked across the suites
this release touched and appears nowhere else: every other wait either targets a
single row by id or waits for an exact state.

## Upgrading

Nothing to do. This is 3.22.0 — issue #48's import completeness, remote push, the
audit re-run fix and the acquisition agility — with one test that waits properly.
Its notes are in `release-notes-3.22.0.md`.
