#!/usr/bin/env python3
"""The cover finder's flow — the question it asks, and the states it can be in.

Two things are checked, because the finder's bug lived between them:

  * BEHAVIOUR (``tools/test_cover_search_flow.cjs``, run from here): the logic
    in ``web/src/lib/coverSearch.ts`` is plain enough for Node to load the real
    module the app ships, and that script drives it — the identity resolves to
    one set of parameters, the automatic (on-open) search sends exactly what
    the Search button sends, it waits for the source list instead of asking a
    different question, it fires once per identity, and a request in flight, a
    real zero-candidate answer and a failed request are three states that
    cannot be mistaken for each other.

  * WIRING (this file): the component and the client can only ever ask through
    that module — one call site of ``api.coverSearch``, one URL builder, the
    automatic search on the module's own decision, and the "no covers found"
    copy rendered from the empty state alone. A component that stopped going
    through the tested logic would still pass the behaviour checks, which is
    exactly why the wiring is asserted too.

Run:  python tools/test_cover_search_flow.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []


def ok(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        FAILED.append(f"{label}{f' — {detail}' if detail else ''}")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def count(haystack, needle):
    return haystack.count(needle)


# --------------------------------------------------------------------------- #
# 1) The real module, driven by Node (no browser, no stubs: it is the shipped
#    code, and Node 24 strips its types)
# --------------------------------------------------------------------------- #
print("the finder's logic (node tools/test_cover_search_flow.cjs)")
run = subprocess.run(
    ["node", os.path.join(ROOT, "tools", "test_cover_search_flow.cjs")],
    capture_output=True, text=True, cwd=ROOT,
)
print(run.stdout.rstrip())
if run.returncode != 0:
    print(run.stderr.rstrip())
    FAILED.append("the cover-search logic checks failed (see above)")

# --------------------------------------------------------------------------- #
# 2) The wiring: one request builder, one call site, the states rendered from
#    the one decision function
# --------------------------------------------------------------------------- #
modal = read("web/src/components/CoverSearchModal.tsx")
api = read("web/src/api.ts")
lib = read("web/src/lib/coverSearch.ts")

print("\nthe wiring")
ok("the component imports its logic from lib/coverSearch", '"../lib/coverSearch"' in modal)
ok(
    "the request URL is built by ONE place (lib/coverSearch) — api.ts sends that path",
    "${API}${coverSearchPath(q)}" in api and "/cover/search?" not in api,
    "api.ts must not build the query string itself",
)
ok(
    "...and the offline copy's key IS that path (path + query)",
    "export function cacheKey" in read("web/src/lib/offlineCache.ts")
    and "coverSearchPath" in api,
)
ok(
    "the component has exactly ONE call site of api.coverSearch",
    count(modal, "api.coverSearch(") == 1,
    f"found {count(modal, 'api.coverSearch(')}",
)
ok(
    "...so the automatic search and the Search button share it",
    count(modal, "runQuery(") >= 4 and modal.count("coverQuery(") >= 2,
)

# The on-open path goes through the decision function the behaviour checks drive.
ok("the on-open search asks through autoCoverSearch", "autoCoverSearch({" in modal and "asked.current" in modal)
ok(
    "...and it waits for the source list before asking",
    "sourcesReady" in modal and ".finally(() => setSourcesReady(true))" in modal,
    "a request fired before /api/cover/sources answers carries no sources/country",
)
ok(
    "staged candidates suppress the automatic search only when they EXIST",
    "staged: Boolean(initialResults?.length)" in modal,
    "an empty staged array must not stand in for an answer",
)

# The states: every message the user can read comes from ONE phase decision.
ok("the states come from coverSearchPhase", "coverSearchPhase({" in modal)
ok(
    "the empty state is the only place the 'no covers found' copy is rendered",
    count(modal, 't("cover.empty")') == 1
    and modal.index('t("cover.empty")') > modal.index('phase.kind === "empty"'),
)
ok(
    "...and it names what was searched, from the answer's own query",
    't("cover.answered", { terms: termsOf(phase.query) })' in modal,
)
ok(
    "the failure state shows the message it was given, and a retry",
    't("cover.failed")' in modal and "{phase.message}" in modal and 't("cover.retry")' in modal,
)
ok(
    "the missing-identity state lists what is missing",
    't("cover.blocked")' in modal
    and "phase.gaps.map" in modal
    and 't(`cover.missing.${gap}`)' in modal,
)
ok(
    "an in-flight search shows the searching state (never the empty one)",
    'phase.kind === "searching"' in modal and 't("cover.searching")' in modal,
)
ok(
    "an answer that came off disk is labelled with the offline copy",
    "offlineFallback()" in modal and count(modal, 't("cover.offline")') == 2,
    "both the empty and the ready state must say when the answer is a stored one",
)
ok(
    "...which api.ts reports for the endpoint that could not be reached",
    "export function offlineFallback()" in api,
)

# The logic module itself must keep the two functions the wiring leans on.
ok(
    "the module decides the automatic search and the phase",
    "export function autoCoverSearch" in lib and "export function coverSearchPhase" in lib,
)

print()
if FAILED:
    print(f"FAILED ({len(FAILED)}):")
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
print("Cover search flow: all checks passed.")
sys.exit(0)
