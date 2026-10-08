"""Library grading summary — the "what fails" strip the pages draw.

Read off the library payload the pages already hold (`server.library`), so the
summary can never disagree with the album rows it describes. `GET
/api/grades/summary` is its one route.
"""
from mlo.grader import printed_pct

from server import job_locks

# The most findings the banner names; everything past it is the `more` count a
# reader clicks through to the Library's own Failing filter.
_GRADE_WARNING_MAX = 12
# The grader records an album-wide failure against one of these INSTEAD of a
# file name (mlo.grader's add_issue): "album" for a check that belongs to the
# folder (its cover, its .log), "album-wide" for one that spans every track in
# it (MEDIA, the album tags).
_ALBUM_WHERE = ("album", "album-wide")


def _artist_of(alb, fallback=""):
    meta = alb.get("meta") or {}
    return (str(meta.get("ALBUMARTIST") or "").strip()
            or str(alb.get("album_artist") or "").strip()
            or str(meta.get("ARTIST") or "").strip() or fallback)


def grade_warning(lib):
    """Whether the library passes its grading checks, and — when it does not —
    what fails, specifically: the object the warning strip is drawn from
    (`GET /api/grades/summary`).

    Read off the library payload the pages already hold, and totalled the way
    the library header totals it: every album's `pass_count` over its
    `total_checks`. `ok` is therefore the grader's own per-album rule (failed
    checks == 0, server.library.build_album) applied to the whole library, so
    the strip can never contradict the percentage printed beside it.

    Three albums are not a finding, and every one of them would be the loudest
    row on the page:
    - a PENDING framework album (the release the user added whose audio has
      not arrived): nothing was graded, because there was nothing to grade,
      and its row's failed check is the empty-folder placeholder — listing it
      would report a pending album as a broken album;
    - an album with NO checks (`total_checks` 0): it PASSES by the rule above
      (0 == 0), which is how an album whose checks are all switched off stops
      disagreeing with the Grade script;
    - an album a LIVE JOB holds (`job_locks.busy`): a chain writes an album
      across its steps, so an album mid-run is deliberately half-written — the
      tag the Auto tagging step is about to write is missing right up until
      that step runs — and a strip that reported it would be describing the
      process, not the library. The album's own row already says "Script run"
      for the same reason; when the claim goes the album is a finding again.
      The albums an IMPORT holds right now — `server.imports._importing_now`,
      the three claim kinds the queue's own In progress section reads, so both
      surfaces agree about what "being imported" is — are counted in
      `albums_importing`, which the strip prints in one clause: a reader whose
      failing album left the list the moment its chain started is told where it
      went instead of wondering which album the strip lost. It counts the
      FINDINGS left out on that account (graded, failing, mid-import), not
      every claim in the registry — an album that was never a finding has none
      to lose.

    `albums_failing`, `tracks_failing` and the `items` (with their `more`) are
    the findings that are LISTED, and a busy or mid-import album is in none of
    them — `albums_importing` counting the ones the import held back. The
    TOTALS are not: `pass_count`/`total_checks`/`grade_pct` keep counting the
    library's checks exactly as the header prints them, busy album included — a
    strip that quietly dropped a running album's checks from the sum would
    contradict the percentage printed beside it.

    `grade_pct` is the one number here the pages print as a claim, so it is
    kept honest: rounded to one decimal a library failing one check in ten
    thousand read `100.0` beside the album that failed it, and it now reads
    `99.9` — exactly 100 is printed only when `pass_count` equals
    `total_checks`, i.e. by a library with no failed check at all.

    The owner's rule for what a finding is: ONE failing track in an album is
    shown AS THAT TRACK — the album is only the frame around it, and the file
    is the thing to open — while two or more are shown AS THE ALBUM, which
    carries how many tracks fail and the union of their codes, because a dozen
    rows of one album say less than its name. A failure the grader recorded
    against the album itself (no file to name) always makes an album row and
    carries the grader's own sentence as `reason`.
    """
    # The ONE notion of "this album is being imported": server.imports reads
    # `job_locks.holder` and takes the three claim kinds the import queue's own
    # In progress section reads, so the strip and the queue cannot disagree
    # about it. Imported here rather than at module level: `server.imports`
    # drags in the whole chain service, and this module is imported from inside
    # routes that must not join that cycle.
    from server import imports
    albums = [alb for ar in (lib.get("artists") or [])
              for alb in (ar.get("albums") or [])]
    pass_count = sum(int(a.get("pass_count") or 0) for a in albums)
    total_checks = sum(int(a.get("total_checks") or 0) for a in albums)
    items = []
    tracks_failing = 0
    albums_importing = 0
    for alb in albums:
        # See the docstring: neither of these was graded, so neither is wrong.
        if alb.get("pending") or not (alb.get("total_checks") or 0) or alb.get("pass"):
            continue
        # An album an import holds is mid-write by definition — its chain is
        # filling the very tags the grader has just read as missing — so it is
        # not a finding, and it is COUNTED, because a strip that only dropped it
        # would look like it lost an album. A claim held by the CALLER's own job
        # is not a conflict to `_importing_now` (see job_locks.busy), so that
        # one falls through to the absolute rule below instead: held back all
        # the same, just not counted — and a second reading of the registry is
        # exactly what this pair is here to avoid.
        if imports._importing_now(alb.get("path")) is not None:
            albums_importing += 1
            continue
        # …and any other live job's album is mid-write rather than wrong. Asked
        # absolutely (job_locks.busy, not holder): the answer is a fact about
        # the album, not about whatever this request happens to be running
        # inside.
        if job_locks.busy(alb.get("path")):
            continue
        bad = [tr for tr in (alb.get("tracks") or []) if tr.get("issues")]
        issues = alb.get("issues") or {}
        sentences = [s for s, where in issues.items()
                     if any(w in _ALBUM_WHERE for w in (where or ()))]
        if not bad and not sentences:
            # A folder failure with no file against it and no album-wide
            # sentence either: the grader keys those by the folder itself (an
            # album folder that holds no audio), and its sentence is the only
            # thing there is to say.
            sentences = list(issues)
        tracks_failing += len(bad)
        meta = alb.get("meta") or {}
        album_path = str(alb.get("path") or "").replace("\\", "/")
        row = {
            "album_path": album_path,
            "artist": _artist_of(alb),
            "album": (str(meta.get("ALBUM") or "").strip()
                      or album_path.rsplit("/", 1)[-1]),
            "grade_pct": alb.get("grade_pct"),
        }
        if len(bad) == 1 and not sentences:
            tr = bad[0]
            items.append(dict(row,
                              kind="track",
                              track_path=str(tr.get("path") or ""),
                              # The tag the row shows, the file name standing
                              # in when it is empty — a row named "" is a track
                              # the reader cannot match to the album's table.
                              title=(str((tr.get("tags") or {}).get("TITLE") or "").strip()
                                     or str(tr.get("file") or "")),
                              codes=sorted(set(tr.get("issues") or ()))))
            continue
        items.append(dict(row,
                          kind="album",
                          failing_tracks=len(bad),
                          codes=sorted({c for tr in bad
                                        for c in (tr.get("issues") or ())}),
                          # EVERY album-wide sentence, not just the first: the
                          # row prints one ("— Missing MEDIA") and the tooltip
                          # next to it names the rest.
                          **({"reason": sentences[0],
                              "reasons": sentences} if sentences else {})))
    # Worst first: the lowest grade, then the album with the most failing
    # tracks (a single-track row counts as the one track it is).
    items.sort(key=lambda it: (it["grade_pct"] if it["grade_pct"] is not None else 0.0,
                               -(it.get("failing_tracks") or 1)))
    more = max(0, len(items) - _GRADE_WARNING_MAX)
    # Printed BESIDE the findings above, so it obeys the one rule for a printed
    # percentage (mlo.grader.printed_pct): 100 belongs to a library with no
    # failed check at all, and a strip that names a failing album while the
    # line next to it reads "100% of checks pass" contradicts itself in one
    # sentence.
    grade_pct = printed_pct(pass_count, total_checks)
    return {
        "ok": not items,
        "pass_count": pass_count,
        "total_checks": total_checks,
        "grade_pct": grade_pct,
        "albums_failing": len(items),
        # The failing albums an import is holding right now — not listed, and
        # the strip says so in one clause rather than looking short an album.
        "albums_importing": albums_importing,
        "tracks_failing": tracks_failing,
        "items": items[:_GRADE_WARNING_MAX],
        "more": more,
    }