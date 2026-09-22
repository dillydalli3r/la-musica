#!/usr/bin/env python3
"""Regression: no Settings row is invented.

Every field the Settings page renders carries a config key (`k: "…"`) and
`mlo/config.py` `DEFAULT_CONFIG` is what the server ships, migrates and writes
back. When the two drift, the failure is silent in BOTH directions: a row whose
key the server does not know renders with the UI's own idea of its default,
Save writes the key anyway, and a fresh install behaves as if the setting did
not exist — which is exactly what happened to `prefer_disc_streams`, whose row
shipped in `configMeta.ts` and `SettingsPage.tsx` while the key was absent from
`DEFAULT_CONFIG`, and every suite stayed green (the release-choice suite states
its own fixture config, so it never reads the shipped defaults).

So the check reads the two TypeScript sources and requires every `k: "…"`
literal to exist in `DEFAULT_CONFIG`. The reverse is deliberately NOT required:
the raw config box reaches keys that have no row (documented as such in §9 of
`docs/OPTIMIZATION-GRADING-SPEC.md`), so a key without a row is a choice, while
a row without a key is a bug.

Run:  python tools/test_config_ui_parity.py
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo.config import DEFAULT_CONFIG

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = ("web/src/lib/configMeta.ts", "web/src/pages/SettingsPage.tsx")
KEY_RE = re.compile(r'\bk:\s*"([a-z0-9_]+)"')
# A parse that finds nothing must fail loudly: a renamed binding would
# otherwise turn this whole suite into a no-op that always passes.
MIN_ROWS = 200

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else '  ' + str(detail)}")
    if not ok:
        failures += 1


found = {}
for rel in SOURCES:
    keys = set(KEY_RE.findall(io.open(os.path.join(ROOT, rel), encoding="utf-8").read()))
    found[rel] = keys
    check(f"{rel}: the config rows parse (≥ {MIN_ROWS})", len(keys) >= MIN_ROWS, len(keys))

ui = set().union(*found.values())
unknown = sorted(k for k in ui if k not in DEFAULT_CONFIG)
check("every Settings row's key exists in DEFAULT_CONFIG", not unknown, unknown)

print(f"\nconfig/UI parity: {len(ui)} UI keys, {len(DEFAULT_CONFIG)} shipped keys, "
      f"{len(unknown)} invented")
sys.exit(1 if failures else 0)
