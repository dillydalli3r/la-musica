#!/usr/bin/env python3
"""Menu-consistency gate: every surface that lists the 20 scripts must agree.

Sources checked:
  * ``EXPECTED_SCRIPTS`` below — the frozen expected registry (number + name)
  * ``server/script_runners.py`` RUNNERS — the numbers /api/run accepts
  * ``web/src/lib/scripts.ts`` SCRIPTS    — the UI's single source of truth
  * ``README.md``            the 19-script table
  * ``web/src/lib/force.ts`` FORCE_SCRIPTS, ``SettingsPage`` FORCE_KEYS —
    the one-shot force switches must map onto /api/run's force dict keys

Run:  python tools/test_script_menus.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The registry every surface must agree on: number -> what the script is called.
# Kept here on purpose — a gate that reads its expectation out of the code under
# test cannot notice that code losing a script. Adding a script means adding it
# here as well as to server/script_runners.py, web/src/lib/scripts.ts and README.md.
EXPECTED_SCRIPTS = {
    1: "Format Lyrics",
    2: "Format CUEs",
    3: "Optimize FLACs",
    4: "Grade Library",
    5: "Process Images",
    6: "Audit Library",
    7: "DR & ReplayGain",
    8: "Auto Tagging",
    9: "AccurateRip",
    10: "Format All",
    11: "Video Remux",
    12: "Key & BPM",
    13: "Fetch Lyrics",
    14: "Beets Tagging",
    # 15 took over the id the removed lyrics xlit/translate script had — it
    # writes each album's .mlo_expected.json release tracklist.
    15: "Release tracklist",
    # 16 is the mood/energy classifier on its own — script 8 runs the same
    # code as one of its stages.
    16: "Mood & Energy",
    # 17 is the AI pass: the lyric transliteration / translation script the
    # no-AI core removed, back under a fresh id (15 stayed with the tracklist).
    17: "Lyrics transliterate (AI)",
    # 18 gives back: this library's lyrics are submitted to LRCLIB for
    # recordings the database does not have yet.
    18: "Publish lyrics (LRCLIB)",
    # 19 re-fits the artist images already in the library to the configured
    # aspect and size — the remedy for the codes grade_check_artist_image
    # raises (mlo/artistdata.py run_optimize_artist_images).
    19: "Optimize artist images",
    # 20 reports the shape of the whole music folder (mlo/layout.py) and
    # stores that report under <music>/.mlo/data, which is what the Library
    # page warns from. Read-only: it is the one script that changes no file.
    20: "Scan library layout",
    # 21 completes an ACOUSTID_ID / ACOUSTID_FINGERPRINT pair a file only half
    # carries — the grading check that had no fixer in the registry at all
    # (mlo/acoustid.py run_fix_pairs).
    21: "Fix AcoustID pairs",
}


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def strip_accents(s):
    return s.replace("·", "-").replace("&", "and")


def server_runners():
    src = read("server/script_runners.py")
    block = re.search(r"RUNNERS[^=]*= \{(.*?)\n\}", src, re.S).group(1)
    return {int(n) for n in re.findall(r"(\d+):", block)}


def web_scripts():
    src = read("web/src/lib/scripts.ts")
    block = re.search(r"export const SCRIPTS[^=]*=\s*\[(.*?)\n\];", src, re.S).group(1)
    out = {}
    for m in re.finditer(r'ids:\s*\[(\d+)\],\s*label:\s*"([^"]+)"', block):
        out[int(m.group(1))] = m.group(2)
    return out


def web_default_run_all():
    src = read("web/src/lib/scripts.ts")
    m = re.search(r"DEFAULT_RUN_ALL = \[([^\]]+)\]", src)
    return [int(n) for n in re.findall(r"\d+", m.group(1))]


def python_default_run_all():
    src = read("mlo/config.py")
    m = re.search(r"DEFAULT_RUN_ALL_ORDER = \[([^\]]+)\]", src)
    return [int(n) for n in re.findall(r"\d+", m.group(1))]


# The surfaces that RUN a list of scripts (a chain / Run All), and the token
# each one has to read it from. `web/src/lib/scripts.ts` and `mlo/config.py`
# are where the order is DEFINED; the list here is everyone else, because a
# surface that keeps its own copy is how the wizard's on-Done list came to run
# Grade before the scripts that write the tags it grades, and how a newly added
# script could be missing from one menu and present in the next. A surface that
# runs a list belongs HERE — the discovery check at the bottom of
# `check_script_surfaces` fails on a web caller this table does not name.
RUN_ALL_SURFACES = {
    "mlo/cli.py": "run_all_order",
    "server/imports.py": "DEFAULT_RUN_ALL_ORDER",
    "web/src/pages/CheckStackPage.tsx": "run_all_order",
    "web/src/pages/ImportWizard.tsx": "run_all_order",
    "web/src/pages/LibraryPage.tsx": "run_all_order",
    "web/src/pages/OptimizationPage.tsx": "run_all_order",
    "web/src/pages/SettingsPage.tsx": "run_all_order",
    # The wizard's step renders the `run_all_order` field declared in
    # lib/configMeta.ts (its `value` arrives already loaded), so the canonical
    # names it works with here are the shared TS list and its anchors.
    "web/src/pages/SetupPage.tsx": "DEFAULT_RUN_ALL",
}

# The surfaces that offer ONE script at a time, and the registry each takes its
# names from ("" = the labels are action phrasings, so only the ids are
# checked). Ordering is meaningless for a single script; what can drift is the
# NUMBER a menu runs and the NAME it prints, so both are held against the
# canonical registry.
PER_SCRIPT_MENUS = {
    "web/src/components/TagActionsMenu.tsx": "",
    "web/src/pages/AlbumPage.tsx": "SCRIPT_LABEL",
    "web/src/pages/LibraryPage.tsx": "SCRIPTS",
}

ALL_SURFACES = set(RUN_ALL_SURFACES) | set(PER_SCRIPT_MENUS)

# Three or more DISTINCT script numbers in one literal is a list somebody
# typed. Distinctness is what separates one from a column of unrelated numbers
# (a worker-count dropdown, an RGB triple): those repeat or fall outside 1..21.
ID_LIST = re.compile(r"\[\s*(\d+)\s*(?:,\s*\d+\s*)+?\]")
RUN_ONE_ID = re.compile(r"(?:api\.run|runScripts)\(\[(\d+)\]")


def without_comments(rel):
    """*rel*'s source minus its comments: a list QUOTED in a comment is an
    explanation of what the code used to do, not a second list."""
    src = read(rel)
    if rel.endswith((".ts", ".tsx")):
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        return "\n".join(line.split("//")[0] for line in src.splitlines())
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def handwritten_id_lists(rel):
    """The script-id literals *rel* types into its own code (comments aside)."""
    out = []
    for m in ID_LIST.finditer(without_comments(rel)):
        ids = [int(n) for n in re.findall(r"\d+", m.group(0))]
        if len(ids) >= 3 and len(set(ids)) == len(ids) and all(1 <= n <= 21 for n in ids):
            out.append(ids)
    return out


def web_run_callers():
    """Every web source file that hands the runner a list of scripts."""
    out = {}
    root = os.path.join(ROOT, "web", "src")
    for folder, _dirs, files in os.walk(root):
        if "node_modules" in folder:
            continue
        for name in files:
            if not name.endswith((".ts", ".tsx")):
                continue
            rel = os.path.relpath(os.path.join(folder, name), ROOT).replace("\\", "/")
            if "api.run(" in read(rel):
                out[rel] = rel
    return out


def check_script_surfaces(check):
    """Every surface that builds a script list derives it from the canonical
    order — or, for a one-script menu, names registry ids and registry labels.

    The two tables above are the whole point: a list built anywhere else is
    what drifts, and the discovery check at the bottom refuses a web caller
    that is not in one of them, so the next surface has to be declared here
    instead of quietly running its own list."""
    for rel, token in sorted(RUN_ALL_SURFACES.items()):
        src = read(rel)
        check(f"{rel} reads the canonical order ({token})", token in src)
        typed = handwritten_id_lists(rel)
        check(f"{rel} keeps no run order of its own", not typed, str(typed))
    for rel, token in sorted(PER_SCRIPT_MENUS.items()):
        src = read(rel)
        if token:
            check(f"{rel} takes its script names from the registry ({token})",
                  token in src)
        ids = sorted({int(n) for n in RUN_ONE_ID.findall(src)})
        check(f"every id {rel} runs by itself is a real script",
              set(ids) <= set(EXPECTED_SCRIPTS),
              str(sorted(set(ids) - set(EXPECTED_SCRIPTS))))
        typed = handwritten_id_lists(rel)
        check(f"{rel} keeps no run order of its own", not typed, str(typed))
    undeclared = sorted(set(web_run_callers()) - ALL_SURFACES
                        - {"web/src/lib/scripts.ts",  # the canonical list itself
                           "web/src/api.ts"})         # the transport, not a menu
    check("every web surface that runs scripts is declared above", not undeclared,
          str(undeclared))


def readme_scripts():
    src = read("README.md")
    return {int(m.group(1)) for m in re.finditer(r"^\|\s*(\d+)\s*\|", src, re.M)}


def force_keys():
    src = read("web/src/lib/force.ts")
    keys = re.findall(r'key:\s*"(\w+)"', src)
    # server/script_runners.py maps these request keys onto the config
    # force_* flags (_FORCE_ALIASES) — the one place that knows the mapping
    # since /api/run delegates its execution there.
    server = read("server/script_runners.py")
    block = re.search(r"_FORCE_ALIASES = \{(.*?)\n\}", server, re.S)
    mapped = set(re.findall(r'"(\w+)":', block.group(1))) if block else set()
    return set(keys), mapped


def force_defaults_are_false():
    """A supplied force dict must be authoritative: unselected keys have to
    end up OFF, never left at the saved config value — otherwise unchecking a
    script in the one-shot Force menu still forces it.

    The semantics live in `server.script_runners._apply_force`, which clears
    every force flag before applying the supplied selection."""
    src = read("server/script_runners.py")
    block = re.search(r"def _apply_force\(.*?\n(.*?)\n    for raw, value",
                      src, re.S)
    body = block.group(1) if block else ""
    clears = re.findall(r"cfg\[key\] = False", body)
    # A "clear first" block must exist for both the whole-chain (sid None) and
    # the single-script case, and the dict loop must never seed a saved value.
    fallbacks = re.findall(r'cfg\[key\] = cfg\.get\(', body)
    return ([] if len(clears) >= 2 else ["no authoritative clear"]), fallbacks


def check_run_all_migration(check):
    """A saved Run All order must keep the user's sequence and be repaired.

    The ids added after a saved order was written are shed and re-inserted at
    their canonical anchors — never trusted to mean what they meant back then.
    The legacy list below names 15 where the REMOVED lyrics xlit/translate
    script sat (before beets); loading it must shed that stale entry (15 is
    the Tracklist script today) and re-anchor it after beets."""
    sys.path.insert(0, ROOT)
    import mlo.config as cfg  # noqa: PLC0415 - needs ROOT on sys.path first

    legacy = {
        "music_folder": "X",
        "run_all_order": [11, 14, 1, 2, 8, 13, 15, 12, 3, 5, 9, 6, 4, 7, 10],
    }
    got = cfg.normalize_config(legacy)["run_all_order"]
    # The saved 1..14 sequence survives verbatim; the later ids are anchored.
    saved_seq = [i for i in got if i <= 14]
    check("a saved order keeps its 1..14 sequence",
          saved_seq == [11, 14, 1, 2, 8, 13, 12, 3, 5, 9, 6, 4, 7, 10], str(saved_seq))
    check("the stale script-15 entry is shed and 15 is re-anchored after beets",
          got.count(15) == 1 and got.index(15) == got.index(14) + 1, str(got))
    check("every script lands exactly once in a normalized order",
          sorted(got) == list(range(1, 22)), str(sorted(got)))
    # 17/18 were never in a saved order before they existed; the same
    # shed-and-anchor rule has to place them after the fetch they read from.
    check("18 (publish) lands after 13 (fetch lyrics) in a normalized order",
          got.index(18) == got.index(13) + 1, str(got))
    check("17 (AI transforms) lands after 18 (publish) in a normalized order",
          got.index(17) == got.index(18) + 1, str(got))
    # 20 and 21 were not in any saved order yet either: they join at their
    # anchors, which are the final pass (10) and the report on it (20).
    check("20 (layout scan) lands after 10 (format all) for an existing install",
          got.count(20) == 1 and got.index(20) == got.index(10) + 1, str(got))
    check("21 (AcoustID pairs) lands right after 20 for an existing install",
          got.count(21) == 1 and got.index(21) == got.index(20) + 1, str(got))
    # ...and on a later load they are KEPT where the user put them: unlike 15,
    # their ids never meant anything else, so a saved position is a real choice
    # and re-anchoring them would silently undo the drag.
    moved = cfg.normalize_config({"music_folder": "X",
                                  "run_all_order": [sid for sid in got if sid not in (20, 21)] + [21, 20]})
    check("a saved position for 20 and 21 is kept, not re-anchored after 10",
          moved["run_all_order"][-2:] == [21, 20], str(moved["run_all_order"]))

    junk = cfg.normalize_config({"music_folder": "X", "run_all_order": [99, "a", 4, 4, -1]})
    check("unknown / duplicate run-all ids are dropped",
          all(1 <= n <= 21 for n in junk["run_all_order"]) and len(set(junk["run_all_order"])) == len(junk["run_all_order"]),
          str(junk["run_all_order"]))

    twice = cfg.normalize_config(cfg.normalize_config({"music_folder": "X"}))
    check("normalize_config is idempotent",
          twice["run_all_order"] == list(cfg.DEFAULT_RUN_ALL_ORDER), str(twice["run_all_order"]))
    # The shipped order is the one the pipeline actually means: Grade is the
    # last word, and the two read-outs it acts on come right before it.
    check("the shipped order ends with the layout report, the AcoustID pair and Grade",
          cfg.DEFAULT_RUN_ALL_ORDER[-3:] == [20, 21, 4], str(cfg.DEFAULT_RUN_ALL_ORDER[-3:]))


def check_import_chain(check):
    """The import chain's id filter is the registry, not a literal.

    Every id the chain returns has to exist in RUNNERS — which is what a
    hardcoded upper bound silently breaks (a bound that still advertised the
    removed script 15 let a saved "15" come back as an error row in the import
    report). 15 is a real runner again, so a saved 15 is KEPT now; anything
    outside the registry is dropped."""
    sys.path.insert(0, ROOT)
    from server import imports, script_runners  # noqa: PLC0415 - needs ROOT first

    runners = set(script_runners.RUNNERS)
    check("SCRIPT_ID_MAX is the registry's own last id",
          imports.SCRIPT_ID_MAX == max(runners),
          f"{imports.SCRIPT_ID_MAX} vs {max(runners)}")
    got = imports.chain_for({"import_scripts": [15, 4, 17, 3, 8, 99]})
    check("chain_for keeps the real scripts 15 and 17 and drops the unknown 99",
          got == [15, 4, 17, 3, 8], str(got))
    check("every id the chain returns exists in RUNNERS",
          bool(got) and set(got) <= runners,
          f"{got} vs {sorted(runners)}")
    # A saved import_scripts list is the user's own override and is honoured as
    # written — a subset stays a subset, in the order it was typed, rather than
    # being completed back to DEFAULT_CHAIN (the two are different decisions:
    # "drop these from my import" is not "run everything again").
    partial = imports.chain_for({"import_scripts": [4, 12]})
    check("a saved import_scripts subset is honoured as written",
          partial == [4, 12], str(partial))
    check("an id an older release removed cannot come back as a chain entry",
          all(1 <= sid <= imports.SCRIPT_ID_MAX for sid in
              imports.chain_for({"import_scripts": [99, -3, "x", 1]})),
          str(imports.chain_for({"import_scripts": [99, -3, "x", 1]})))
    check("the built-in chain is all real runners",
          set(imports.DEFAULT_CHAIN) <= runners, str(imports.DEFAULT_CHAIN))
    check("the built-in chain writes the tracklist after the tagging step",
          imports.DEFAULT_CHAIN.index(15) > imports.DEFAULT_CHAIN.index(14)
          and imports.DEFAULT_CHAIN.index(15) < imports.DEFAULT_CHAIN.index(4),
          str(imports.DEFAULT_CHAIN))


def check_cli_runners(check):
    """The terminal must be able to RUN every script its own menu lists.

    ``mlo.cli.SCRIPTS`` is what the menus print and ``build_script_runners()``
    is what Run All dispatches; nothing tied the two together, so 15 and then
    19 were each listed with no runner — Run All answered "Skipping unknown
    script id" for a script the menu was still advertising. A missing runner
    is a hard error in build_script_runners now, and this is the check that
    keeps the next id from being added to one half of that pair only."""
    sys.path.insert(0, ROOT)
    from mlo.cli import SCRIPTS, build_script_runners  # noqa: PLC0415 - needs ROOT first

    menu = {sid: name for sid, name, _desc in SCRIPTS}
    runners = build_script_runners()
    check("mlo.cli.SCRIPTS == canonical numbers", set(menu) == set(EXPECTED_SCRIPTS),
          f"cli={sorted(menu)}")
    check("build_script_runners covers every id the menu lists",
          set(runners) == set(menu),
          f"unrunnable={sorted(set(menu) - set(runners))}")
    check("every one of those runners can actually be called",
          all(callable(runner) and runner is not None for _label, runner in runners.values()),
          str([sid for sid, (_label, runner) in runners.items() if not callable(runner)]))
    check("the terminal names each script the way the menu does",
          all(label == menu[sid] for sid, (label, _runner) in runners.items()),
          str([sid for sid, (label, _r) in runners.items() if label != menu[sid]]))


def main():
    fail = 0
    canon = EXPECTED_SCRIPTS
    runners = server_runners()
    web = web_scripts()
    readme = readme_scripts()
    run_all = web_default_run_all()

    def check(label, ok, detail=""):
        nonlocal fail
        print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  " + detail))
        if not ok:
            fail += 1

    print("scripts registry")
    check("canonical registry has 21 scripts", len(canon) == 21, str(sorted(canon)))
    check("canonical numbers are 1..21", sorted(canon) == list(range(1, 22)))
    check("server RUNNERS == canonical numbers", runners == set(canon), f"server={sorted(runners)}")
    check("web SCRIPTS == canonical numbers", set(web) == set(canon), f"web={sorted(web)}")
    check("README table == canonical numbers", readme == set(canon), f"readme={sorted(readme)}")

    print("labels")
    # The UI phrasing and the CLI phrasing differ on purpose ("Grade Library"
    # vs "Grade"); what must not drift is which script a number names, so the
    # check is a word overlap rather than string equality.
    def words(s):
        return set(re.findall(r"[a-z0-9]+", strip_accents(s).lower()))

    for n in sorted(canon):
        shared = words(canon[n]) & words(web.get(n, ""))
        check(f"#{n} label still names the same script ({canon[n]!r} / {web.get(n)!r})",
              bool(shared), "no shared word")

    print("run-all order")
    py_run_all = python_default_run_all()
    check("DEFAULT_RUN_ALL covers every script", sorted(run_all) == list(range(1, 22)), str(sorted(run_all)))
    check("DEFAULT_RUN_ALL has no duplicates", len(run_all) == len(set(run_all)), str(run_all))
    check("mlo/config.py DEFAULT_RUN_ALL_ORDER covers every script",
          sorted(py_run_all) == list(range(1, 22)), str(sorted(py_run_all)))
    check("python and web run-all order agree", py_run_all == run_all,
          f"python={py_run_all} web={run_all}")

    print("force switches")
    keys, mapped = force_keys()
    check("every force switch maps to a /api/run force key",
          keys <= mapped, f"unmapped={sorted(keys - mapped)}")
    fallbacks, wrong = force_defaults_are_false()
    check("one-shot force treats unselected keys as off",
          not fallbacks and not wrong,
          f"falls back to saved config: {fallbacks + wrong}")

    print("run-all migration")
    check_run_all_migration(check)

    print("import chain")
    check_import_chain(check)

    print("script surfaces")
    check_script_surfaces(check)

    print("terminal runners")
    check_cli_runners(check)

    print(f"\n{'PASS' if not fail else 'FAIL'} — {fail} problem(s)")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
