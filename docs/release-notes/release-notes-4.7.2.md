# la musica 4.7.2 — the lyrics verdict says what is actually wrong

## The grade names the reason, and names a script only when a script can fix it

A file whose stored words carry **no timestamps** kept failing with *"Lyrics not
optimally formatted (run Lyrics script)"* — and running the Lyrics script left
the failure exactly where it was, because script 1 rewrites what is stored and
never invents timing (the owner's report: "Running format lyrics script doesn't
fix this error"). The same was true of three other conditions the format check
lumps together.

`grade_check_lyrics_format` fails for several different reasons, and the verdict
now collects the failing one per branch:

- **what the formatter repairs** keeps the script's name and carries the list:
  *"Lyrics not optimally formatted (run Lyrics script) — the stored text is not
  in the configured form"*, *"… the first line does not match the [00:00.00]
  rule"*, *"… word timestamps are misformatted"*. `_lyrics_formatted` is the
  formatter's own idempotency predicate, so "the formatter would change this
  text" IS "this is repairable";
- **what nothing can repair** is said as such, with the way out named instead:
  - *"Lyrics cannot be repaired by a script: the lyrics have no timestamps and
    plain lyrics are not accepted (lyrics_allow_plain is off) — no script can add
    timing; fetch a synced version (clear these words and run Fetch lyrics) or
    turn on \"Accept plain (unsynced) lyrics\" in Settings → Lyrics & CUEs"*;
  - *"… the lyrics are line-synced where the required sync level is WORD — no
    script can add word timing; fetch a word-synced version"*;
  - *"… two timestamps share one line (Extended LRC keeps them together, so no
    script splits them — fetch a version with one line per timestamp)"*;
  - *"… word timestamps are out of order — no script re-orders them; fetch the
    lyrics again"*.

Both halves are still one finding and one `LYRICS` code, so the check count and
the score do not move.

## Verified

- Reproduced on the owner's own file (Nonagon Infinity, "Road Train"): its
  `LYRICS` tag carries 44 untimed lines and no `.lrc`, and the album's verdict
  was the unactionable "run Lyrics script". With the change the same album
  grades as *"Lyrics cannot be repaired by a script: the lyrics have no
  timestamps and plain lyrics are not accepted (lyrics_allow_plain is off) …"*,
  and a file with repairable drift (`[id:…]` headers, stray blank lines) still
  grades as *"Lyrics not optimally formatted (run Lyrics script) — the stored
  text is not in the configured form"*.
- `tools/test_lyrics_fix.py` (an untimed leftover on an `INSTRUMENTAL=0` track
  fails with the plain reason and script 1 still leaves it alone),
  `tools/test_arrived_lyrics.py` (a kept untimed arrival's message names the
  plain state), `tools/test_import_pipeline.py` (the finding is asserted as a
  finding — either wording — never one spelling), plus
  `test_lyrics_kind.py`, `test_lyrics_extra.py`, `test_grading_paths.py`,
  `test_tag_hygiene.py`, `test_credits.py`.
- `python tools/check_versions.py v4.7.2` — all 10 copies agree.
