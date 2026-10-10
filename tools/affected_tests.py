#!/usr/bin/env python3
"""Which suites cover the files you changed — the fast half of the gate.

The handover gate runs EVERY suite (`for f in tools/test_*.py; do ...`), which is
the slow part of every edit. This answers the narrower question: **which suites
actually reach the modules I just touched?** It reads the imports out of the
suite files, so a change to `mlo/discs.py` prints the handful of suites that
import it instead of all of them.

Usage:
    python tools/affected_tests.py mlo/discs.py server/plays.py
    python tools/affected_tests.py --diff        # from `git diff --name-only`
    python tools/affected_tests.py --staged      # from `git diff --cached --name-only`

It reads IMPORTS only (plus a same-name `test_<stem>.py` match). A suite that
reaches your module transitively, through another module, is NOT listed — so a
short answer means "these certainly cover it", never "nothing else can break".
When in doubt (a shared protocol, `mlo/paths.py`, `mlo/config.py`,
`server/events.py`, `server/main.py`), still run the whole gate.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"

# `import server.plays` / `from mlo import grader` / `from mlo.grader import X`
IMPORT_DOTTED = re.compile(r"\b(?:from|import)\s+(server|mlo)((?:\.[A-Za-z_]\w*)*)")
IMPORT_FROM = re.compile(r"\bfrom\s+(server|mlo)\s+import\s+([A-Za-z0-9_,\s]+)")
ANY_MENTION = re.compile(r"\b(server|mlo)\.([A-Za-z_]\w*)")


def suite_files() -> list[Path]:
    """Every suite the gate knows: the .py and .cjs ones, sorted."""
    return sorted(TOOLS.glob("test_*.py")) + sorted(TOOLS.glob("test_*.cjs"))


def modules_in(text: str) -> set[str]:
    """The `server.x` / `mlo.x` module names a suite file references."""
    found: set[str] = set()
    for pkg, dotted in IMPORT_DOTTED.findall(text):
        if dotted:
            found.add(f"{pkg}{dotted}")
    for pkg, names in IMPORT_FROM.findall(text):
        for name in names.split(","):
            name = name.strip().split(" as ")[0].strip()
            if name:
                found.add(f"{pkg}.{name}")
    for pkg, name in ANY_MENTION.findall(text):
        found.add(f"{pkg}.{name}")
    return found


def build_index() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """module -> suites, and module-stem -> suites (the same-name fallback)."""
    by_module: dict[str, set[str]] = {}
    by_stem: dict[str, set[str]] = {}
    for path in suite_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for module in modules_in(text):
            by_module.setdefault(module, set()).add(path.name)
        # `tests/test_plays.py` covers `server/plays.py` even without an import.
        stem = path.stem[len("test_"):] if path.stem.startswith("test_") else path.stem
        by_stem.setdefault(stem, set()).add(path.name)
    return by_module, by_stem


def module_of(rel: str) -> str | None:
    """`server/plays.py` -> `server.plays`; anything else -> None."""
    parts = Path(rel).parts
    if len(parts) != 2 or parts[0] not in ("server", "mlo") or not parts[1].endswith(".py"):
        return None
    return f"{parts[0]}.{Path(parts[1]).stem}"


def changed_from_git(staged: bool) -> list[str]:
    flag = "--cached" if staged else "HEAD"
    out = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--name-only", flag],
        capture_output=True, text=True, check=False,
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    strict = "--strict" in argv
    from_diff = "--diff" in argv or "--staged" in argv
    if "--diff" in argv:
        args += changed_from_git(staged=False)
    if "--staged" in argv:
        args += changed_from_git(staged=True)
    if not args:
        if from_diff:
            print("# no changed files")
            return 0
        print(__doc__.strip())
        return 2

    by_module, by_stem = build_index()
    suites: set[str] = set()
    web_paths: list[str] = []
    not_a_module: list[str] = []
    unindexed: list[str] = []

    for rel in args:
        rel = rel.replace("\\", "/")
        if rel.startswith("web/"):
            web_paths.append(rel)
            continue
        module = module_of(rel)
        if module is None:
            not_a_module.append(rel)
            continue
        hit = by_module.get(module, set()) | by_stem.get(Path(rel).stem, set())
        if not hit:
            unindexed.append(rel)
        suites |= hit

    if suites:
        print("# suites that reach these files (run once, at the end):")
        for name in sorted(suites):
            runner = "python" if name.endswith(".py") else "node"
            print(f"{runner} tools/{name}")
    else:
        print("# no suite imports these files — run the whole gate for them")

    if web_paths:
        print("\n# web changes: npx tsc -b && npx oxlint src && npm run build (in web/)")
        print("# and the check for the surface you touched: node tools/check_<surface>.mjs")

    if unindexed:
        print("\n# no suite imports these (run the whole gate): " + ", ".join(sorted(set(unindexed))))
    if not_a_module:
        print("\n# not a flat server/ or mlo/ module (no index): " + ", ".join(sorted(set(not_a_module))))

    if strict and not suites and not web_paths:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))