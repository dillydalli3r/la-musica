#!/usr/bin/env python3
"""Menu-consistency gate: every surface that lists the 15 scripts must agree.

Sources checked:
  * ``mlo/cliapp.py``        SCRIPTS      — canonical name + number + label
  * ``server/main.py``       RUNNERS      — the numbers /api/run accepts
  * ``web/src/lib/scripts.ts`` SCRIPTS    — the UI's single source of truth
  * ``README.md``            the 15-script table
  * ``web/src/lib/force.ts`` FORCE_SCRIPTS, ``SettingsPage`` FORCE_KEYS —
    the one-shot force switches must map onto /api/run's force dict keys

Run:  python tools/test_script_menus.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def strip_accents(s):
    return s.replace("·", "-").replace("&", "and")


def cliapp_scripts():
    """Legacy console registry (mlo/cliapp.py). Returns {} if that module has
    been removed, so this gate keeps working without it."""
    path = os.path.join(ROOT, "mlo/cliapp.py")
    if not os.path.isfile(path):
        return {}
    block = re.search(r"^SCRIPTS = \{(.*?)^\}", read("mlo/cliapp.py"), re.S | re.M).group(1)
    out = {}
    for m in re.finditer(r'"\w+":\s*\((\d+),\s*"([^"]+)"', block):
        out[int(m.group(1))] = m.group(2)
    return out


def server_runners():
    src = read("server/main.py")
    block = re.search(r"RUNNERS = \{(.*?)\n    \}", src, re.S).group(1)
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
    # server/main.py maps these request keys onto config force_* flags
    server = read("server/main.py")
    block = re.search(r"if oneshot:(.*?)\n\n", server, re.S)
    mapped = set(re.findall(r'f\.get\("(\w+)"', block.group(1))) if block else set()
    return set(keys), mapped


def force_defaults_are_false():
    """A supplied force dict must be authoritative: unselected keys have to
    read ``f.get(key, False)``, never fall back to the saved config value —
    otherwise unchecking a script in the one-shot Force menu still forces it."""
    server = read("server/main.py")
    block = re.search(r"if oneshot:(.*?)\n\n", server, re.S)
    body = block.group(1) if block else ""
    fallbacks = re.findall(r'f\.get\("\w+",\s*cfg\.get\(', body)
    wrong_default = re.findall(r'f\.get\("\w+",\s*(?!False)\w', body)
    return fallbacks, wrong_default


def check_run_all_migration(check):
    """A saved Run All order from before script 15 existed must gain it on
    load — otherwise an upgrade silently stops running xlit/translate."""
    sys.path.insert(0, ROOT)
    import mlo.config as cfg  # noqa: PLC0415 - needs ROOT on sys.path first

    legacy = {
        "music_folder": "X",
        "run_all_order": [11, 14, 1, 2, 8, 13, 12, 3, 5, 9, 6, 4, 7, 10],
    }
    got = cfg.normalize_config(legacy)["run_all_order"]
    check("a saved order without script 15 gains it", 15 in got, str(got))
    check("script 15 lands after the lyrics fetch (13)",
          13 in got and 15 in got and got.index(15) == got.index(13) + 1, str(got))
    check("the migrated order matches DEFAULT_RUN_ALL_ORDER",
          got == list(cfg.DEFAULT_RUN_ALL_ORDER), str(got))

    junk = cfg.normalize_config({"music_folder": "X", "run_all_order": [99, "a", 4, 4, -1]})
    check("unknown / duplicate run-all ids are dropped",
          all(1 <= n <= 15 for n in junk["run_all_order"]) and len(set(junk["run_all_order"])) == len(junk["run_all_order"]),
          str(junk["run_all_order"]))

    twice = cfg.normalize_config(cfg.normalize_config({"music_folder": "X"}))
    check("normalize_config is idempotent",
          twice["run_all_order"] == list(cfg.DEFAULT_RUN_ALL_ORDER), str(twice["run_all_order"]))


def main():
    fail = 0
    canon = cliapp_scripts() or {n: web_scripts().get(n, "") for n in sorted(web_scripts())}
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
    check("canonical registry has 15 scripts", len(canon) == 15, str(sorted(canon)))
    check("canonical numbers are 1..15", sorted(canon) == list(range(1, 16)))
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
    check("DEFAULT_RUN_ALL covers every script", sorted(run_all) == list(range(1, 16)), str(sorted(run_all)))
    check("DEFAULT_RUN_ALL has no duplicates", len(run_all) == len(set(run_all)), str(run_all))
    check("mlo/config.py DEFAULT_RUN_ALL_ORDER covers every script",
          sorted(py_run_all) == list(range(1, 16)), str(sorted(py_run_all)))
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
