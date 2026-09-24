# la musica 3.23.4 — the same app, with its own rules under test

3.23.4 is 3.23.3 with two test-only changes, cut so that nothing sits outside a
release: the image, the installers and the zip carry the same behaviour as
3.23.3, and the code that keeps it that way is under test in the artifacts too.

- **The grading strip's rule is pinned.** `server.recommendations.grade_warning`
  decides what the Home and Library strips say, and nothing asserted it: the
  shape of a finding (one failing track is shown AS the track; two or more as
  the album, with how many tracks fail and the union of their codes; a failure
  the grader recorded against the album itself as an album row carrying its
  sentence), the two albums that are never a finding (a PENDING framework album
  and an album with no checks), the totals the Home header prints beside it,
  worst-first order, and the 12-row cap with the rest counted. All of it is
  asserted in `tools/test_home.py` now, including that Home's payload carries
  that same object rather than a second count of the library.
- **The Logchecker fallback is tested as logic, not against the machine it runs
  on.** `tools/test_cd_audit.py` stubbed the phar's *output* but not the tool
  table that decides whether the fallback runs at all, so it passed on a box
  with the app's own installer behind it and failed on a bare CI runner with
  `(None: eac-logchecker not installed)` — which is what had skipped CI on
  v3.23.2 and, with it, that release's docker image and bundles. The table is
  faked on paths that exist, so the four verdicts are tested as verdicts.

If you are updating from 3.23.3 there is nothing to see beyond that: the
behaviour is identical, byte for byte, and 3.23.3's own changes are in the
notes beside this file.
