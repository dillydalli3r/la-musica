#!/usr/bin/env python3
"""Regression: the first-run wizard can reach every setting the app has.

The wizard renders web/src/lib/configMeta.ts, so that module — not the page —
is the thing to hold to "covers everything": every key mlo/config.py ships must
be either a field in one of its groups or named in one of its two explicit
allowlists (the keys the wizard writes itself, and the ones it deliberately
hides), and every key and group SettingsPage renders must exist there too. Add a
setting on either side and this fails until the wizard accounts for it.

The two sides are read from source, not imported: the python side is the real
DEFAULT_CONFIG (so a key nothing else knows about still has to be covered), the
TS side is parsed out of the module's own text (so a field that never renders is
still coverage). web/src/pages/SettingsPage.tsx is the third check — it is the
page the wizard must stay a superset of, and it is read read-only.

Run:  python tools/test_setup_coverage.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo.config import DEFAULT_CONFIG

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META = os.path.join(ROOT, "web", "src", "lib", "configMeta.ts")
SETTINGS = os.path.join(ROOT, "web", "src", "pages", "SettingsPage.tsx")

for path in (META, SETTINGS):
    if not os.path.isfile(path):
        print(f"test_setup_coverage: SKIP — {path} is missing")
        sys.exit(2)

meta_src = open(META, encoding="utf-8").read()
settings_src = open(SETTINGS, encoding="utf-8").read()

FIELD_KEY = re.compile(r'\bk:\s*"([a-z0-9_]+)"')
GROUP_TITLE = re.compile(r'^\s*title:\s*"([^"]+)"', re.M)
STEP_GROUPS = re.compile(r"^  groups: \[([^\]]*)\]", re.M)
STRING_KEY = re.compile(r'"([a-z0-9_]+)"')


def array_body(src, marker):
    """The entries of the `export const <marker>: string[] = [ ... ]` list."""
    start = src.index(marker)
    start = src.index("[", start)
    end = src.index("];", start)
    return src[start:end]


def region(src, marker, closing):
    """The text from `marker` up to the sentinel that ends its declaration."""
    start = src.index(marker)
    return src[start : src.index(closing, start)]


def quoted_keys(body):
    return set(STRING_KEY.findall(body))


groups_src = region(meta_src, "export const CONFIG_GROUPS: CfgGroup[] = [", "\n];")
steps_src = region(meta_src, "export const SETUP_STEPS: SetupStep[] = [", "\n];")

covered = set(FIELD_KEY.findall(groups_src))
managed = quoted_keys(array_body(meta_src, "export const WIZARD_MANAGED_KEYS"))
hidden = quoted_keys(array_body(meta_src, "export const HIDDEN_KEYS"))

# The groups the steps render: `groups: [...]` entries plus every group title in
# the module, so a group no step names (a tab nothing reaches) fails below.
step_groups = set()
for block in STEP_GROUPS.findall(steps_src):
    step_groups.update(STRING_KEY.findall(block))
group_titles = set(GROUP_TITLE.findall(groups_src))

config_keys = set(DEFAULT_CONFIG)
settings_keys = set(FIELD_KEY.findall(settings_src))
settings_groups = set(
    re.findall(r"title:\s*\"([^\"]+)\"", settings_src[: settings_src.index("const GRADE_CHECK_KEYS")])
)
open_groups = quoted_keys(meta_src[meta_src.index("OPEN_GROUPS") : meta_src.index("export const SETUP_STEPS")])

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


# 1. Every key the app can configure is either a field of a group, a key the
#    wizard writes itself, or explicitly hidden — no key may be simply absent.
uncovered = config_keys - covered - managed - hidden
check(
    not uncovered,
    "config keys no setup step can reach and no allowlist names: " + ", ".join(sorted(uncovered)),
)

# 2. The allowlists have to be real keys, and must not paper over a field that
#    exists (a hidden key with a control in the wizard is a contradiction).
stale = (hidden | managed) - config_keys
check(not stale, "allowlisted keys that are not config keys: " + ", ".join(sorted(stale)))
check(not (hidden & covered), "keys both hidden and covered: " + ", ".join(sorted(hidden & covered)))
check(not (hidden & managed), "keys both hidden and wizard-managed: " + ", ".join(sorted(hidden & managed)))
check(not (covered - config_keys), "fields for keys the config does not have: " + ", ".join(sorted(covered - config_keys)))

# 3. Every key and group SettingsPage renders has to be here too: the wizard is
#    the superset, so a setting added to the page cannot be left out of a run.
missing_settings = settings_keys - covered - hidden
check(
    not missing_settings,
    "settings the wizard has no field for: " + ", ".join(sorted(missing_settings)),
)
missing_groups = settings_groups - group_titles
check(
    not missing_groups,
    "settings groups with no group in the wizard: " + ", ".join(sorted(missing_groups)),
)

# 4. Every group belongs to exactly one step: a group no step lists is
#    unreachable, and one listed twice is rendered twice.
for title in sorted(group_titles):
    steps = len(re.findall(r"groups: \[[^\]]*\"%s\"" % re.escape(title), steps_src))
    check(steps == 1, f'group "{title}" is rendered by {steps} steps, want exactly 1')

# 5. The named groups and the step list have to agree with the module.
check(not (step_groups - group_titles), "steps name groups that do not exist: " + ", ".join(sorted(step_groups - group_titles)))
check(not (open_groups - group_titles), "OPEN_GROUPS names groups that do not exist: " + ", ".join(sorted(open_groups - group_titles)))

# 5b. No key may appear twice inside one group: two controls for one setting
#     render twice and disagree about nothing — and this is what catches an
#     EXTRA field that the Settings page has since started carrying itself.
for block in re.findall(r"\{\s*\n\s*title: \"[^\"]+\",(.*?)\n    \},", groups_src, re.S):
    keys = FIELD_KEY.findall(block)
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    check(not duplicates, "the same setting twice in one group: " + ", ".join(duplicates))

# 6. The credentials are the point of the issue: none of them may be hidden or
#    merely written by the wizard — each needs a field of its own.
# A credential is something a person pastes in: keys, tokens, secrets, a
# cookie, a password. `auth_password_hash` is deliberately not one — it is a
# derived value the server refuses to accept back (see HIDDEN_KEYS).
CREDENTIAL = re.compile(r"(_api_key|_token|_secret|_password$|^rym_cookie$|spotify_client_id)")
credentials = {k for k in config_keys if CREDENTIAL.search(k)}
check(
    not (credentials - covered),
    "credentials with no field in the wizard: " + ", ".join(sorted(credentials - covered)),
)

# 7. Skippability is a property of the step list itself: the steps with nothing
#    to save (the folder picker and the closing screen) are the terminals, and
#    every other step renders at least one group.
folders = re.findall(r'panel: "folder"', steps_src)
dones = re.findall(r'panel: "done"', steps_src)
check(len(folders) == 1, f"want exactly one folder step, found {len(folders)}")
check(len(dones) == 1, f"want exactly one closing step, found {len(dones)}")

# 8. Grading ships STRICT: the shipped defaults ARE the "Strict" preset, so a
#    fresh install grades every check without anyone pressing anything.
#
#    The checks come from mlo/grader.py's own gate helper (check_gates() reads
#    the names that module mentions), never from a list kept here: a check
#    added to the grader is covered the day it exists, and one that ships off
#    is named in the failure instead of hiding. The Strict preset is defined as
#    "every real check on" (server/api_stack.preset_value), so the comparison
#    is the defaults' enabled count against that whole set.
from mlo.grader import check_gates  # noqa: E402 — needs ROOT on sys.path

CHECK_PREFIX = "grade_check_"
check_keys = [k for k in check_gates() if k.startswith(CHECK_PREFIX)]
shipped_on = [k for k in check_keys if DEFAULT_CONFIG.get(k)]
shipped_off = sorted(set(check_keys) - set(shipped_on))
check(
    bool(check_keys) and not shipped_off,
    "the shipped defaults grade at least as strictly as the Strict preset: "
    f"{len(shipped_on)}/{len(check_keys)} checks on"
    + (", shipped off: " + ", ".join(shipped_off) if shipped_off else ""),
)
check(
    DEFAULT_CONFIG.get("grade_check_audit") is True,
    "'Require audit tag' ships ON (the one check that used to ship off — the "
    "verdict it grades is the rip's own log-CRC / AccurateRip evidence, so it "
    "cannot fail a disc that verified itself)",
)
# A category is not a check (a preset leaves it alone), but a file class no
# category admits is a class the grade never looks at: they ship allowed.
cat_keys = sorted(k for k in DEFAULT_CONFIG if str(k).startswith("grade_include_"))
cat_off = [k for k in cat_keys if not DEFAULT_CONFIG[k]]
check(
    bool(cat_keys) and not cat_off,
    "every file category ships allowed, so no file class is outside the grade"
    + (", off: " + ", ".join(cat_off) if cat_off else ""),
)

if failures:
    print("test_setup_coverage: FAIL")
    for line in failures:
        print("  - " + line)
    sys.exit(1)

print(
    f"test_setup_coverage: OK — {len(config_keys)} config keys, "
    f"{len(covered)} fields, {len(hidden)} hidden, {len(managed)} wizard-managed, "
    f"{len(group_titles)} groups in {len(re.findall(r'^    label: ', steps_src, re.M))} steps"
)
