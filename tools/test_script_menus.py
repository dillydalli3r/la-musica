#!/usr/bin/env python3
"""Menu-consistency gate: every surface that lists the 14 scripts must agree.

Sources checked:
  * ``EXPECTED_SCRIPTS`` below — the frozen expected registry (number + name)
  * ``server/main.py``       RUNNERS      — the numbers /api/run accepts
  * ``web/src/lib/scripts.ts`` SCRIPTS    — the UI's single source of truth
  * ``README.md``            the 14-script table
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
# here as well as to server/main.py, web/src/lib/scripts.ts and README.md.
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
    """A saved Run All order from before script 15 was removed must shed it
    on load — 15 is not a runner any more, and a chain entry for it would
    come back as an error entry."""
    sys.path.insert(0, ROOT)
    import mlo.config as cfg  # noqa: PLC0415 - needs ROOT on sys.path first

    legacy = {
        "music_folder": "X",
        "run_all_order": [11, 14, 1, 2, 8, 13, 15, 12, 3, 5, 9, 6, 4, 7, 10],
    }
    got = cfg.normalize_config(legacy)["run_all_order"]
    check("a saved order naming the removed script 15 drops it", 15 not in got, str(got))
    check("the sanitised order matches DEFAULT_RUN_ALL_ORDER",
          got == list(cfg.DEFAULT_RUN_ALL_ORDER), str(got))

    junk = cfg.normalize_config({"music_folder": "X", "run_all_order": [99, "a", 4, 4, -1]})
    check("unknown / duplicate run-all ids are dropped",
          all(1 <= n <= 14 for n in junk["run_all_order"]) and len(set(junk["run_all_order"])) == len(junk["run_all_order"]),
          str(junk["run_all_order"]))

    twice = cfg.normalize_config(cfg.normalize_config({"music_folder": "X"}))
    check("normalize_config is idempotent",
          twice["run_all_order"] == list(cfg.DEFAULT_RUN_ALL_ORDER), str(twice["run_all_order"]))


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
    check("canonical registry has 14 scripts", len(canon) == 14, str(sorted(canon)))
    check("canonical numbers are 1..14", sorted(canon) == list(range(1, 15)))
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
    check("DEFAULT_RUN_ALL covers every script", sorted(run_all) == list(range(1, 15)), str(sorted(run_all)))
    check("DEFAULT_RUN_ALL has no duplicates", len(run_all) == len(set(run_all)), str(run_all))
    check("mlo/config.py DEFAULT_RUN_ALL_ORDER covers every script",
          sorted(py_run_all) == list(range(1, 15)), str(sorted(py_run_all)))
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

    print(f"\n{'PASS' if not fail else 'FAIL'} — {fail} problem(s)")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
