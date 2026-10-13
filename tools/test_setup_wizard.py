#!/usr/bin/env python3
"""The setup wizard's Connections step, rendered for real.

The owner's ask was that the first-run wizard cover logging in to Last.fm and
MusicBrainz, and that a person be shown HOW to log in without leaving the app.
tools/check_setup_connections.mjs pins that against the real modules: the step
in `SETUP_STEPS` (its position, its label, its one group, and that nothing on it
can block Continue) and both Connect panels rendered through Vite with the
answer already in the query cache — the same in-memory SSR harness the repo's
other render checks use (tools/check_queue_view.mjs).

This is the python half that runs it, so CI's `tools/test_*.py` shards pick the
check up. Node or web/node_modules missing -> SKIPPED, loudly, exit 0 (the
repo's convention: exit 2 from a tools/check_*.mjs means "cannot run here").

Run: python tools/test_setup_wizard.py  (exit 0 pass, 1 fail)
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

print("the setup wizard's Connections step")
ui = subprocess.run(["node", os.path.join("tools", "check_setup_connections.mjs")],
                    cwd=ROOT, capture_output=True, text=True)
if ui.returncode == 0:
    print(ui.stdout.strip())
elif ui.returncode == 2:
    print("SKIPPED  the wizard render check (node or web/node_modules missing): "
          + ((ui.stderr.strip().splitlines() or [""])[0]))
    sys.exit(0)
else:
    print(ui.stdout)
    raise SystemExit("the wizard render check failed:\n" + ui.stderr)

print("\nall checks passed")